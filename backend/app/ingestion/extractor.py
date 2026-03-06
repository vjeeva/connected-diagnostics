"""LLM-based structured extraction from manual chunks."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from backend.app.ingestion.chunker import Chunk
from backend.app.services.llm.client import extract_json
from backend.app.services.llm.prompts import EXTRACTION_SYSTEM, XREF_ENRICH_SYSTEM


def _parse_llm_json(raw: str) -> dict:
    """Parse JSON from LLM response, handling markdown code blocks."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                pass
        return {"nodes": [], "relationships": [], "chunk_type": "spec", "_raw": raw}


def extract_from_chunk(chunk: Chunk) -> dict:
    """Send a chunk to Claude for structured extraction. Returns parsed JSON."""
    user_prompt = (
        f"Pages: {chunk.page_start}-{chunk.page_end}\n\n"
        f"--- MANUAL TEXT ---\n{chunk.text}\n--- END ---"
    )

    raw = extract_json(system=EXTRACTION_SYSTEM, user_prompt=user_prompt)
    return _parse_llm_json(raw)


def enrich_node_with_ref(node: dict, ref_content: str) -> dict:
    """Enrich a node that has an unresolved cross-reference using referenced content.

    Returns the enrichment result with updated_node, additional_nodes, and
    additional_relationships.
    """
    user_prompt = (
        f"--- EXISTING NODE ---\n{json.dumps(node, indent=2)}\n--- END ---\n\n"
        f"--- REFERENCED CONTENT ---\n{ref_content}\n--- END ---"
    )

    raw = extract_json(system=XREF_ENRICH_SYSTEM, user_prompt=user_prompt)
    return _parse_llm_json(raw)


def extract_batch(
    chunks: list[Chunk],
    max_workers: int = 8,
    on_complete=None,
) -> list[dict]:
    """Extract from multiple chunks concurrently. Returns extractions in chunk order."""
    results: dict[int, dict] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(extract_from_chunk, chunk): i
            for i, chunk in enumerate(chunks)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                results[idx] = {"nodes": [], "relationships": [], "chunk_type": "spec", "_error": str(e)}
            if on_complete:
                on_complete(idx, results[idx])

    return [results[i] for i in range(len(chunks))]
