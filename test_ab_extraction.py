"""A/B test: Compare extraction quality across LLM providers.

Picks sample chunks from already-ingested pages, re-extracts with a different
provider, and compares node/relationship counts and structure against the
Haiku baseline already in Neo4j.
"""
import json
import time
from sqlalchemy import create_engine, text
from backend.app.core.config import settings
from backend.app.services.llm.client import extract_json
from backend.app.services.llm.prompts import EXTRACTION_SYSTEM
from backend.app.ingestion.extractor import _parse_llm_json

# --- Config ---
SAMPLE_SIZE = 5  # chunks to test
PAGE_RANGE = (2400, 2500)  # known good Haiku-ingested range

# Providers to test
PROVIDERS = [
    {"name": "openai-gpt5-mini", "provider": "openai", "model": "gpt-5-mini"},
]


def get_sample_chunks():
    engine = create_engine(settings.postgres_sync_url)
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, chunk_text, page_number, content_hash
            FROM manual_chunks
            WHERE page_number BETWEEN :start AND :end
            ORDER BY page_number
            LIMIT :limit
        """), {"start": PAGE_RANGE[0], "end": PAGE_RANGE[1], "limit": SAMPLE_SIZE}).fetchall()
    engine.dispose()
    return rows


def get_haiku_baseline(chunk_hashes):
    """Get node/rel counts from Neo4j for chunks already extracted by Haiku."""
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    stats = {}
    with driver.session() as session:
        for h in chunk_hashes:
            nodes = session.run(
                "MATCH (n) WHERE n.chunk_hash = $h RETURN labels(n)[0] AS label, count(*) AS cnt",
                {"h": h}
            ).data()
            rels = session.run(
                "MATCH (n)-[r]->(m) WHERE n.chunk_hash = $h RETURN type(r) AS rtype, count(*) AS cnt",
                {"h": h}
            ).data()
            stats[h] = {
                "nodes": {r["label"]: r["cnt"] for r in nodes},
                "total_nodes": sum(r["cnt"] for r in nodes),
                "rels": {r["rtype"]: r["cnt"] for r in rels},
                "total_rels": sum(r["cnt"] for r in rels),
            }
    driver.close()
    return stats


def extract_with_provider(chunk_text, page_num, provider, model):
    """Run extraction with a specific provider/model."""
    # Temporarily override settings
    orig_provider = settings.extraction_provider
    orig_model = settings.extraction_model
    settings.extraction_provider = provider
    settings.extraction_model = model

    user_prompt = f"Pages: {page_num}-{page_num}\n\n--- MANUAL TEXT ---\n{chunk_text}\n--- END ---"

    start = time.time()
    try:
        raw = extract_json(
            system=EXTRACTION_SYSTEM,
            user_prompt=user_prompt,
            max_tokens=8192,
        )
        result = _parse_llm_json(raw)
        elapsed = time.time() - start
    finally:
        settings.extraction_provider = orig_provider
        settings.extraction_model = orig_model

    return result, elapsed


def analyze_extraction(result):
    """Count nodes and relationships from an extraction result."""
    nodes = result.get("nodes", [])
    rels = result.get("relationships", [])
    node_types = {}
    for n in nodes:
        label = n.get("type", "Unknown")
        node_types[label] = node_types.get(label, 0) + 1
    rel_types = {}
    for r in rels:
        rtype = r.get("type", "Unknown")
        rel_types[rtype] = rel_types.get(rtype, 0) + 1
    return {
        "total_nodes": len(nodes),
        "total_rels": len(rels),
        "nodes": node_types,
        "rels": rel_types,
    }


def main():
    print("=" * 60)
    print("A/B Extraction Quality Test")
    print("=" * 60)

    print(f"\nFetching {SAMPLE_SIZE} sample chunks from pages {PAGE_RANGE}...")
    chunks = get_sample_chunks()
    print(f"Got {len(chunks)} chunks\n")

    chunk_hashes = [row[3] for row in chunks]
    print("Getting Haiku baseline from Neo4j...")
    haiku_stats = get_haiku_baseline(chunk_hashes)

    results = {"haiku_baseline": haiku_stats}

    for provider_cfg in PROVIDERS:
        name = provider_cfg["name"]
        print(f"\n{'=' * 60}")
        print(f"Testing: {name} ({provider_cfg['model']})")
        print("=" * 60)

        provider_results = []
        total_time = 0

        for i, chunk in enumerate(chunks):
            chunk_id, chunk_text, page_num, chunk_hash = chunk
            print(f"  [{i+1}/{len(chunks)}] page {page_num}...", end=" ", flush=True)

            try:
                result, elapsed = extract_with_provider(
                    chunk_text, page_num,
                    provider_cfg["provider"], provider_cfg["model"]
                )
                stats = analyze_extraction(result)
                total_time += elapsed
                print(f"{stats['total_nodes']} nodes, {stats['total_rels']} rels ({elapsed:.1f}s)")
                provider_results.append({
                    "chunk_hash": chunk_hash,
                    "page": page_num,
                    "stats": stats,
                    "time": elapsed,
                })
            except Exception as e:
                print(f"ERROR: {e}")
                provider_results.append({
                    "chunk_hash": chunk_hash,
                    "page": page_num,
                    "error": str(e),
                })

        results[name] = provider_results
        print(f"\n  Total time: {total_time:.1f}s for {len(chunks)} chunks")
        print(f"  Avg time: {total_time/len(chunks):.1f}s per chunk")

    # Summary comparison
    print(f"\n{'=' * 60}")
    print("COMPARISON SUMMARY")
    print("=" * 60)
    print(f"\n{'Chunk':<12} {'Haiku nodes':<14} {'Haiku rels':<12}", end="")
    for p in PROVIDERS:
        print(f" {p['name']+' nodes':<20} {p['name']+' rels':<18}", end="")
    print()
    print("-" * 120)

    for i, chunk in enumerate(chunks):
        chunk_hash = chunk[3]
        h = haiku_stats.get(chunk_hash, {})
        print(f"p.{chunk[2]:<8} {h.get('total_nodes', 0):<14} {h.get('total_rels', 0):<12}", end="")
        for p in PROVIDERS:
            pr = results[p["name"]][i] if i < len(results.get(p["name"], [])) else {}
            s = pr.get("stats", {})
            print(f" {s.get('total_nodes', 'ERR'):<20} {s.get('total_rels', 'ERR'):<18}", end="")
        print()

    # Save full results
    with open("ab_test_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull results saved to ab_test_results.json")


if __name__ == "__main__":
    main()
