"""Quick Gemini Flash extraction test using new google-genai library."""
import json
import os
import time
from dotenv import load_dotenv
load_dotenv()

from google import genai
from sqlalchemy import create_engine, text
from backend.app.core.config import settings
from backend.app.services.llm.prompts import EXTRACTION_SYSTEM
from backend.app.ingestion.extractor import _parse_llm_json

SAMPLE_SIZE = 5
PAGE_RANGE = (2400, 2500)
MODELS = ["gemini-2.5-flash"]


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
                "total_nodes": sum(r["cnt"] for r in nodes),
                "total_rels": sum(r["cnt"] for r in rels),
                "nodes": {r["label"]: r["cnt"] for r in nodes},
                "rels": {r["rtype"]: r["cnt"] for r in rels},
            }
    driver.close()
    return stats


def main():
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

    print("=" * 60)
    print("Gemini Flash Extraction Test (new google-genai library)")
    print("=" * 60)

    chunks = get_sample_chunks()
    print(f"Got {len(chunks)} chunks\n")

    chunk_hashes = [row[3] for row in chunks]
    print("Getting Haiku baseline from Neo4j...")
    haiku_stats = get_haiku_baseline(chunk_hashes)

    all_results = {}

    for model_name in MODELS:
        print(f"\n{'=' * 60}")
        print(f"Testing: {model_name}")
        print("=" * 60)

        model_results = []
        total_time = 0

        for i, chunk in enumerate(chunks):
            chunk_id, chunk_text, page_num, chunk_hash = chunk
            user_prompt = f"Pages: {page_num}-{page_num}\n\n--- MANUAL TEXT ---\n{chunk_text}\n--- END ---"
            print(f"  [{i+1}/{len(chunks)}] page {page_num}...", end=" ", flush=True)

            try:
                start = time.time()
                response = client.models.generate_content(
                    model=model_name,
                    contents=user_prompt,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=EXTRACTION_SYSTEM,
                        temperature=0.0,
                        max_output_tokens=65536,
                    ),
                )
                raw = response.text
                result = _parse_llm_json(raw)
                elapsed = time.time() - start
                total_time += elapsed

                nodes = result.get("nodes", [])
                rels = result.get("relationships", [])
                print(f"{len(nodes)} nodes, {len(rels)} rels ({elapsed:.1f}s)")
                model_results.append({
                    "chunk_hash": chunk_hash,
                    "page": page_num,
                    "nodes": len(nodes),
                    "rels": len(rels),
                    "time": elapsed,
                })
            except Exception as e:
                print(f"ERROR: {e}")
                model_results.append({"page": page_num, "error": str(e)})

        all_results[model_name] = model_results
        print(f"\n  Total time: {total_time:.1f}s for {len(chunks)} chunks")
        print(f"  Avg time: {total_time/len(chunks):.1f}s per chunk")

    # Summary
    print(f"\n{'=' * 60}")
    print("COMPARISON")
    print("=" * 60)
    print(f"\n{'Page':<8} {'Haiku nodes':<14} {'Haiku rels':<12}", end="")
    for m in MODELS:
        short = m.split("-")[-1]
        print(f" {short+' nodes':<14} {short+' rels':<12}", end="")
    print()
    print("-" * 90)

    for i, chunk in enumerate(chunks):
        h = haiku_stats.get(chunk[3], {})
        print(f"p.{chunk[2]:<5} {h.get('total_nodes', 0):<14} {h.get('total_rels', 0):<12}", end="")
        for m in MODELS:
            r = all_results[m][i] if i < len(all_results.get(m, [])) else {}
            print(f" {r.get('nodes', 'ERR'):<14} {r.get('rels', 'ERR'):<12}", end="")
        print()

    with open("gemini_test_results.json", "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved to gemini_test_results.json")


if __name__ == "__main__":
    main()
