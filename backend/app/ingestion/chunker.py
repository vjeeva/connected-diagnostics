"""Split extracted PDF pages into chunks for LLM processing."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.app.ingestion.pdf_parser import PageText


@dataclass
class Chunk:
    text: str
    page_start: int
    page_end: int
    section_title: str = ""
    chunk_index: int = 0
    metadata: dict = field(default_factory=dict)


def chunk_pages(
    pages: list[PageText],
    max_chars: int = 8000,
    overlap_pages: int = 2,
) -> list[Chunk]:
    """Group pages into chunks by character count with page-level overlap.

    The last `overlap_pages` pages of each chunk are repeated as the first
    pages of the next chunk, so procedures that span a chunk boundary are
    seen in full by at least one chunk.
    """
    if not pages:
        return []

    chunks: list[Chunk] = []
    current_pages: list[PageText] = []
    current_chars = 0
    chunk_idx = 0

    for page in pages:
        # If adding this page would exceed max_chars, flush
        if current_pages and current_chars + len(page.text) > max_chars:
            text = "\n\n".join(p.text for p in current_pages)
            chunks.append(Chunk(
                text=text.strip(),
                page_start=current_pages[0].page_number,
                page_end=current_pages[-1].page_number,
                chunk_index=chunk_idx,
            ))
            chunk_idx += 1

            # Keep last N pages as overlap for the next chunk
            overlap = current_pages[-overlap_pages:]
            current_pages = list(overlap)
            current_chars = sum(len(p.text) for p in current_pages)

        current_pages.append(page)
        current_chars += len(page.text)

    # Flush remaining
    if current_pages:
        text = "\n\n".join(p.text for p in current_pages)
        chunks.append(Chunk(
            text=text.strip(),
            page_start=current_pages[0].page_number,
            page_end=current_pages[-1].page_number,
            chunk_index=chunk_idx,
        ))

    return chunks
