"""Resolve cross-references in service manual PDFs.

Brand-agnostic: builds a page-level embedding index in Postgres,
then uses semantic search to find referenced content when the LLM
flags unresolved cross-references.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import fitz  # pymupdf for fast page text extraction
from sqlalchemy import create_engine, text

from rich.console import Console

from backend.app.core.config import settings
from backend.app.services.search_service import embed_texts

console = Console()


def pdf_source_hash(pdf_path: str) -> str:
    """Generate a stable hash for a PDF based on content, not path."""
    h = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        # Read first and last 1MB + file size for a fast content-based hash
        h.update(f.read(1024 * 1024))
        f.seek(0, 2)
        size = f.tell()
        h.update(str(size).encode())
        if size > 1024 * 1024:
            f.seek(-1024 * 1024, 2)
            h.update(f.read())
    return h.hexdigest()


def build_page_index(pdf_path: str, batch_size: int = 100) -> str:
    """Extract text from every page, embed it, and store in page_index.

    Skips pages that are already indexed for this PDF.
    Returns the source_hash for this PDF.
    """
    source_hash = pdf_source_hash(pdf_path)
    engine = create_engine(settings.postgres_sync_url)

    # Check how many pages are already indexed
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT COUNT(*) FROM page_index WHERE source_hash = :hash"),
            {"hash": source_hash},
        )
        existing_count = result.scalar()

    doc = fitz.open(pdf_path)
    total = doc.page_count

    if existing_count >= total:
        console.print(f"[green]Page index already built ({existing_count} pages).[/green]")
        doc.close()
        engine.dispose()
        return source_hash

    # Find which pages are missing
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT page_number FROM page_index WHERE source_hash = :hash"),
            {"hash": source_hash},
        )
        existing_pages = {row[0] for row in result}

    missing_pages = [i + 1 for i in range(total) if (i + 1) not in existing_pages]
    console.print(f"[dim]Indexing {len(missing_pages)} pages ({existing_count} already done)...[/dim]")

    # Extract text for missing pages
    page_texts: list[tuple[int, str, str]] = []  # (page_num, header, full_text)
    for page_num in missing_pages:
        page = doc.load_page(page_num - 1)
        full_text = page.get_text("text")
        header = _extract_header(full_text)
        if full_text.strip():
            page_texts.append((page_num, header, full_text))

    doc.close()

    if not page_texts:
        console.print("[green]No text pages to index.[/green]")
        engine.dispose()
        return source_hash

    # Embed in batches
    all_texts = [t[2][:8000] for t in page_texts]  # Cap text length for embedding
    console.print(f"[dim]Generating embeddings for {len(all_texts)} pages...[/dim]")
    embeddings = embed_texts(all_texts, batch_size=batch_size)

    # Store in Postgres
    console.print("[dim]Storing page index...[/dim]")
    with engine.connect() as conn:
        for (page_num, header, _), embedding in zip(page_texts, embeddings):
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
            conn.execute(
                text("""
                    INSERT INTO page_index (id, source_hash, page_number, header_text, embedding)
                    VALUES (:id, :hash, :page, :header, CAST(:embedding AS vector))
                    ON CONFLICT (source_hash, page_number) DO NOTHING
                """),
                {
                    "id": str(uuid.uuid4()),
                    "hash": source_hash,
                    "page": page_num,
                    "header": header[:500] if header else None,
                    "embedding": embedding_str,
                },
            )
        conn.commit()

    engine.dispose()
    console.print(f"[green]Indexed {len(page_texts)} pages.[/green]")
    return source_hash


def _extract_header(page_text: str) -> str:
    """Extract a header/title from the first lines of a page."""
    lines = page_text.strip().split("\n")
    for line in lines[:5]:
        cleaned = line.strip()
        if len(cleaned) > 10 and not cleaned.startswith("Last Modified"):
            return cleaned[:500]
    return ""


def search_pages(
    query: str,
    source_hash: str,
    limit: int = 3,
) -> list[tuple[int, str, float]]:
    """Semantic search over page index. Returns [(page_number, header, distance)]."""
    from backend.app.services.search_service import embed_text

    query_embedding = embed_text(query)
    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    engine = create_engine(settings.postgres_sync_url)
    with engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT page_number, header_text,
                       embedding <=> CAST(:embedding AS vector) AS distance
                FROM page_index
                WHERE source_hash = :hash AND embedding IS NOT NULL
                ORDER BY embedding <=> CAST(:embedding AS vector)
                LIMIT :limit
            """),
            {"embedding": embedding_str, "hash": source_hash, "limit": limit},
        )
        rows = result.fetchall()

    engine.dispose()
    return [(row[0], row[1] or "", float(row[2])) for row in rows]


def fetch_ref_content(pdf_path: str, page_number: int, context_pages: int = 2) -> str:
    """Fetch the text content from a referenced page and surrounding context."""
    doc = fitz.open(pdf_path)
    total = doc.page_count
    start = max(0, page_number - 1)
    end = min(total, page_number - 1 + context_pages)
    texts = []
    for i in range(start, end):
        text = doc.load_page(i).get_text("text")
        if text.strip():
            texts.append(text)
    doc.close()
    return "\n\n".join(texts)
