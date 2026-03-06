"""CLI command to ingest a service manual PDF into the knowledge graph."""

from __future__ import annotations

import hashlib
import uuid

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from sqlalchemy import create_engine, text

from backend.app.core.config import settings
from backend.app.db.init_db import init_postgres
from backend.app.graph.schema import ensure_schema
from backend.app.ingestion.pdf_parser import extract_pages
from backend.app.ingestion.chunker import chunk_pages
from backend.app.ingestion.extractor import extract_batch, enrich_node_with_ref
from backend.app.ingestion.graph_builder import build_vehicle, build_from_extraction
from backend.app.ingestion.xref_resolver import (
    build_page_index, search_pages, fetch_ref_content,
)
from backend.app.services.search_service import embed_texts
from backend.app.ingestion.enrichment import enrich_graph

console = Console()


def _hash_chunk(chunk_text: str) -> str:
    return hashlib.sha256(chunk_text.encode()).hexdigest()


def _get_existing_hashes(engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT content_hash FROM manual_chunks")).fetchall()
    return {row[0] for row in rows}


def _get_unembedded_chunk_ids(engine) -> list[tuple[str, str]]:
    """Return (id, chunk_text) for chunks stored but missing embeddings."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, chunk_text FROM manual_chunks WHERE embedding IS NULL")
        ).fetchall()
    return [(str(row[0]), row[1]) for row in rows]


@click.command()
@click.option("--pdf", required=True, help="Path to the service manual PDF")
@click.option("--make", required=True, help="Vehicle make (e.g. Lexus)")
@click.option("--model", required=True, help="Vehicle model (e.g. GX460)")
@click.option("--year-start", required=True, type=int, help="First model year covered")
@click.option("--year-end", required=True, type=int, help="Last model year covered")
@click.option("--start-page", default=1, type=int, help="First page to process")
@click.option("--end-page", default=None, type=int, help="Last page to process (default: all)")
@click.option("--dry-run", is_flag=True, help="Parse and chunk only, skip LLM extraction")
@click.option("--reextract", is_flag=True, help="Re-run LLM extraction on existing chunks from Postgres (skips PDF parsing)")
@click.option("--ocr", is_flag=True, help="Use Claude vision to OCR image-only pages")
@click.option("--extract-missing", is_flag=True, help="Extract only chunks that have no graph nodes yet (safe, non-destructive)")
def ingest(pdf: str, make: str, model: str, year_start: int, year_end: int,
           start_page: int, end_page: int | None, dry_run: bool, reextract: bool, ocr: bool,
           extract_missing: bool):
    """Ingest a service manual PDF into the knowledge graph and vector store."""

    console.print(f"\n[bold]Ingesting:[/bold] {pdf}")
    console.print(f"[bold]Vehicle:[/bold] {make} {model} ({year_start}-{year_end})\n")

    # Step 1: Initialize databases
    console.print("[dim]Initializing databases...[/dim]")
    init_postgres()
    ensure_schema()
    console.print("[green]Databases ready.[/green]\n")

    engine = create_engine(settings.postgres_sync_url)

    if reextract:
        # Load existing chunks from Postgres instead of parsing PDF
        console.print("[dim]Loading existing chunks from Postgres...[/dim]")
        page_filter = "WHERE page_number >= :start_page"
        params: dict = {"start_page": start_page}
        if end_page:
            page_filter += " AND page_number <= :end_page"
            params["end_page"] = end_page

        with engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT id, chunk_text, page_number, content_hash FROM manual_chunks {page_filter} ORDER BY page_number"),
                params,
            ).fetchall()

        if not rows:
            console.print("[red]No existing chunks found for this page range.[/red]")
            engine.dispose()
            return

        # Build Chunk objects from stored data
        from backend.app.ingestion.chunker import Chunk
        new_chunks = [Chunk(text=row[1], page_start=row[2], page_end=row[2]) for row in rows]
        chunk_db_ids = [str(row[0]) for row in rows]
        chunk_hashes = [row[3] for row in rows]
        new_indices = list(range(len(new_chunks)))
        console.print(f"Loaded [bold]{len(new_chunks)}[/bold] chunks for re-extraction.\n")

        # Clear old graph nodes: tagged nodes matching these hashes + untagged legacy nodes
        console.print("[dim]Clearing old graph data for these chunks...[/dim]")
        from backend.app.db.neo4j_client import get_driver
        driver = get_driver()
        with driver.session() as session:
            result = session.run(
                "MATCH (n) WHERE NOT n:Vehicle AND (n.chunk_hash IN $hashes OR n.chunk_hash IS NULL) "
                "WITH n DETACH DELETE n RETURN count(*) AS deleted",
                {"hashes": chunk_hashes},
            )
            deleted = result.single()["deleted"]
        console.print(f"[green]Cleared {deleted} graph nodes ({len(chunk_hashes)} chunks + legacy untagged).[/green]\n")
    elif extract_missing:
        # Load all chunks from Postgres, extract only those without graph nodes
        console.print("[dim]Loading chunks from Postgres...[/dim]")
        page_filter = "WHERE page_number >= :start_page"
        params: dict = {"start_page": start_page}
        if end_page:
            page_filter += " AND page_number <= :end_page"
            params["end_page"] = end_page

        with engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT id, chunk_text, page_number, content_hash FROM manual_chunks {page_filter} ORDER BY page_number"),
                params,
            ).fetchall()

        if not rows:
            console.print("[red]No chunks found for this page range.[/red]")
            engine.dispose()
            return

        all_hashes = [row[3] for row in rows]
        console.print(f"Found [bold]{len(rows)}[/bold] total chunks.")

        # Check which hashes already have graph nodes in Neo4j
        from backend.app.db.neo4j_client import get_driver
        driver = get_driver()
        with driver.session() as session:
            result = session.run(
                "MATCH (n) WHERE n.chunk_hash IN $hashes "
                "RETURN DISTINCT n.chunk_hash AS h",
                {"hashes": all_hashes},
            )
            extracted_hashes = {r["h"] for r in result}

        missing_indices = [i for i, row in enumerate(rows) if row[3] not in extracted_hashes]

        if not missing_indices:
            console.print("[green]All chunks already have graph nodes. Nothing to extract.[/green]")
            engine.dispose()
            enrich_graph()
            return

        from backend.app.ingestion.chunker import Chunk
        new_chunks = [Chunk(text=rows[i][1], page_start=rows[i][2], page_end=rows[i][2]) for i in missing_indices]
        chunk_hashes = [rows[i][3] for i in missing_indices]
        new_indices = list(range(len(new_chunks)))

        console.print(f"[yellow]Skipping {len(extracted_hashes)} chunks that already have graph nodes.[/yellow]")
        console.print(f"[bold]{len(new_chunks)}[/bold] chunks need extraction.\n")
    else:
        # Step 2: Extract text from PDF
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      transient=True) as progress:
            progress.add_task("Extracting text from PDF...", total=None)
            pages = extract_pages(pdf, start_page=start_page, end_page=end_page, ocr=ocr)
        console.print(f"Extracted [bold]{len(pages)}[/bold] pages with text.\n")

        if not pages:
            console.print("[red]No text found in PDF. Aborting.[/red]")
            engine.dispose()
            return

        # Step 3: Chunk pages
        chunks = chunk_pages(pages, max_chars=settings.chunk_max_chars, overlap_pages=settings.chunk_overlap_pages)
        console.print(f"Created [bold]{len(chunks)}[/bold] chunks.\n")

        if dry_run:
            console.print("[yellow]Dry run — showing first 3 chunks:[/yellow]\n")
            for chunk in chunks[:3]:
                console.print(f"[dim]Pages {chunk.page_start}-{chunk.page_end}[/dim]")
                console.print(chunk.text[:300] + "...\n")
            engine.dispose()
            return

        # Step 4: Filter out already-ingested chunks
        existing_hashes = _get_existing_hashes(engine)
        chunk_hashes = [_hash_chunk(c.text) for c in chunks]

        new_indices = [i for i, h in enumerate(chunk_hashes) if h not in existing_hashes]
        new_chunks = [chunks[i] for i in new_indices]

        if not new_chunks:
            console.print("[green]All chunks already ingested.[/green]")
            # Still backfill embeddings for chunks missing them
            unembedded = _get_unembedded_chunk_ids(engine)
            if unembedded:
                console.print(f"[dim]Backfilling embeddings for {len(unembedded)} chunks...[/dim]")
                chunk_ids = [row[0] for row in unembedded]
                chunk_texts = [row[1] for row in unembedded]
                embeddings = embed_texts(chunk_texts)
                with engine.connect() as conn:
                    for chunk_id, embedding in zip(chunk_ids, embeddings):
                        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
                        conn.execute(
                            text("UPDATE manual_chunks SET embedding = CAST(:embedding AS vector) WHERE id = :id"),
                            {"id": chunk_id, "embedding": embedding_str},
                        )
                    conn.commit()
                console.print(f"[green]Backfilled {len(embeddings)} embeddings.[/green]\n")
            else:
                console.print("[green]All embeddings present.[/green]\n")
            engine.dispose()
            enrich_graph()
            return

        skipped = len(chunks) - len(new_chunks)
        if skipped:
            console.print(f"[yellow]Skipping {skipped} already-ingested chunks.[/yellow]")
        console.print(f"[bold]{len(new_chunks)}[/bold] new chunks to process.\n")

    # Step 5: LLM extraction + graph building
    console.print("[dim]Creating vehicle nodes...[/dim]")
    vehicle_ids = build_vehicle(make, model, year_start, year_end)
    console.print(f"[green]Created {len(vehicle_ids)} vehicle nodes.[/green]\n")

    completed = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
    ) as progress:
        task = progress.add_task("Extracting from chunks...", total=len(new_chunks))

        def on_complete(idx, result):
            nonlocal completed
            completed += 1
            nodes = len(result.get("nodes", []))
            rels = len(result.get("relationships", []))
            progress.update(task, advance=1,
                            description=f"{completed}/{len(new_chunks)} done "
                            f"(chunk {idx+1}: {nodes} nodes, {rels} rels)")
            console.print(f"[dim]  [{completed}/{len(new_chunks)}] chunk {idx+1}: {nodes} nodes, {rels} rels[/dim]")

        extractions = extract_batch(new_chunks, max_workers=settings.ingestion_workers, on_complete=on_complete)

    # Step 5b: Second pass — resolve cross-references via semantic search
    unresolved_nodes = [
        (i, j, node)
        for i, ext in enumerate(extractions)
        for j, node in enumerate(ext.get("nodes", []))
        if node.get("unresolved_ref")
    ]
    if unresolved_nodes:
        console.print(f"[yellow]{len(unresolved_nodes)} nodes with unresolved cross-references.[/yellow]")
        console.print("[dim]Building page index for cross-reference search...[/dim]")
        source_hash = build_page_index(pdf)

        # Prepare enrichment tasks: resolve refs and fetch content (fast, no LLM)
        enrichment_tasks = []  # (ext_idx, node_idx, node, combined_ref)
        for ext_idx, node_idx, node in unresolved_nodes:
            ref_search = node.get("ref_search", "")
            if not ref_search:
                continue

            matches = search_pages(ref_search, source_hash, limit=2)
            if not matches:
                console.print(f"[dim]  No match for: {ref_search}[/dim]")
                continue

            ref_texts = [fetch_ref_content(pdf, page_num) for page_num, _, _ in matches]
            combined_ref = "\n\n---\n\n".join(ref_texts)
            enrichment_tasks.append((ext_idx, node_idx, node, combined_ref, matches))

        # Run LLM enrichment calls in parallel
        from concurrent.futures import ThreadPoolExecutor, as_completed

        enriched = 0
        lock = __import__("threading").Lock()

        def _enrich(task):
            ext_idx, node_idx, node, combined_ref, matches = task
            result = enrich_node_with_ref(node, combined_ref)
            return ext_idx, node_idx, node, result, matches

        with ThreadPoolExecutor(max_workers=settings.ingestion_workers) as pool:
            futures = {pool.submit(_enrich, t): t for t in enrichment_tasks}
            for future in as_completed(futures):
                try:
                    ext_idx, node_idx, node, result, matches = future.result()
                    with lock:
                        updated = result.get("updated_node", {})
                        if updated:
                            updated.pop("unresolved_ref", None)
                            updated.pop("ref_search", None)
                            extractions[ext_idx]["nodes"][node_idx] = updated
                        extractions[ext_idx].setdefault("nodes", []).extend(result.get("additional_nodes", []))
                        extractions[ext_idx].setdefault("relationships", []).extend(result.get("additional_relationships", []))
                        enriched += 1
                    page_num, header, dist = matches[0]
                    console.print(f"[dim]  Enriched: {node.get('title', '?')} ← p.{page_num} {header[:60]} (dist={dist:.3f})[/dim]")
                except Exception as e:
                    console.print(f"[red]Enrichment error: {e}[/red]")

        console.print(f"[green]Enriched {enriched}/{len(unresolved_nodes)} nodes from cross-references.[/green]\n")
    else:
        console.print("[dim]No unresolved cross-references found.[/dim]\n")

    # Build graph (sequential for dedup)
    title_to_id: dict[str, str] = {}
    console.print("[dim]Building graph from extractions...[/dim]")
    # Resolve hashes for each new chunk (reextract has chunk_hashes already, normal path uses new_indices)
    extraction_hashes = [chunk_hashes[new_indices[i]] for i in range(len(new_chunks))]

    for i, extraction in enumerate(extractions):
        try:
            title_to_id = build_from_extraction(
                extraction, vehicle_ids, title_to_id,
                chunk_hash=extraction_hashes[i] if i < len(extraction_hashes) else None,
            )
        except Exception as e:
            console.print(f"[red]Graph build error: {e}[/red]")

    total_nodes = sum(len(e.get("nodes", [])) for e in extractions)
    total_rels = sum(len(e.get("relationships", [])) for e in extractions)
    console.print(f"\n[green]Graph built:[/green] {total_nodes} nodes, {total_rels} relationships")
    console.print(f"[green]Unique nodes:[/green] {len(title_to_id)}\n")

    # Step 6: Store chunks in PostgreSQL (skip if reextract/extract-missing — chunks already exist)
    if not reextract and not extract_missing:
        console.print("[dim]Storing chunks in PostgreSQL...[/dim]")

        with engine.connect() as conn:
            for i, chunk in enumerate(new_chunks):
                chunk_type = "procedure"
                if i < len(extractions):
                    chunk_type = extractions[i].get("chunk_type", "procedure") or "procedure"

                conn.execute(
                    text("""
                        INSERT INTO manual_chunks
                            (id, vehicle_neo4j_id, content_hash, source_file, page_number,
                             chunk_text, chunk_type, metadata)
                        VALUES
                            (:id, :vehicle_id, :hash, :source, :page,
                             :text, :ctype, CAST(:meta AS jsonb))
                    """),
                    {
                        "id": str(uuid.uuid4()),
                        "vehicle_id": vehicle_ids[0] if vehicle_ids else "",
                        "hash": chunk_hashes[new_indices[i]],
                        "source": pdf,
                        "page": chunk.page_start,
                        "text": chunk.text,
                        "ctype": chunk_type,
                        "meta": "{}",
                    },
                )
            conn.commit()
        console.print(f"[green]Stored {len(new_chunks)} chunks.[/green]\n")

    # Step 7: Generate embeddings for all unembedded chunks
    unembedded = _get_unembedded_chunk_ids(engine)
    if not unembedded:
        console.print("[green]All chunks already have embeddings.[/green]\n")
        engine.dispose()
        enrich_graph()
        console.print("[bold green]Ingestion complete![/bold green]")
        return

    console.print(f"[dim]Generating embeddings for {len(unembedded)} chunks...[/dim]")
    chunk_ids = [row[0] for row in unembedded]
    chunk_texts = [row[1] for row in unembedded]
    embeddings = embed_texts(chunk_texts)
    console.print(f"[green]Generated {len(embeddings)} embeddings.[/green]\n")

    console.print("[dim]Writing embeddings to PostgreSQL...[/dim]")
    with engine.connect() as conn:
        for chunk_id, embedding in zip(chunk_ids, embeddings):
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
            conn.execute(
                text("""
                    UPDATE manual_chunks
                    SET embedding = CAST(:embedding AS vector)
                    WHERE id = :id
                """),
                {"id": chunk_id, "embedding": embedding_str},
            )
        conn.commit()

    engine.dispose()
    console.print(f"[green]Embedded {len(embeddings)} chunks.[/green]\n")

    # Step 8: Cross-link enrichment (procedures, connector pinouts)
    enrich_graph()

    console.print("[bold green]Ingestion complete![/bold green]")


if __name__ == "__main__":
    ingest()
