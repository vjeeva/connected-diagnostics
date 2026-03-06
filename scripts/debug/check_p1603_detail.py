"""Check P1603 tree for real-world usability — bolt-level detail."""
from neo4j import GraphDatabase
from backend.app.core.config import settings

driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))

with driver.session() as s:
    pid = s.run(
        'MATCH (p:Problem) WHERE "P1603" IN p.dtc_codes RETURN p.id AS id'
    ).single()["id"]

    # Get ALL nodes in tree with full properties
    visited = set()
    frontier = {pid}
    all_ids = {pid}

    while frontier:
        result = s.run(
            "MATCH (a)-[r:LEADS_TO|NEXT_STEP]->(b) "
            "WHERE a.id IN $ids AND NOT b.id IN $visited "
            "RETURN DISTINCT b.id AS bid",
            {"ids": list(frontier), "visited": list(visited)},
        ).data()
        next_frontier = {r["bid"] for r in result}
        if not next_frontier:
            break
        visited.update(frontier)
        frontier = next_frontier - visited
        all_ids.update(frontier)
    all_ids.update(visited)

    # Walk a specific diagnostic path: the "engine speed slowly decreases" scenario
    print("=" * 70)
    print("P1603 WALKTHROUGH: Engine stalls while idling (lean condition)")
    print("=" * 70)

    # Follow a realistic path through the tree
    path_query = """
        MATCH (p:Problem {id: $pid})
        OPTIONAL MATCH (p)-[:REQUIRES_TOOL]->(tool)
        OPTIONAL MATCH (p)-[:REQUIRES_PART]->(part)
        RETURN p.title AS title, p.description AS desc,
               p.instructions AS instructions,
               collect(DISTINCT tool.title) AS tools,
               collect(DISTINCT part.title) AS parts
    """
    r = s.run(path_query, {"pid": pid}).single()
    print(f"\n[Problem] {r['title']}")
    print(f"  Description: {r['desc'][:300] if r['desc'] else 'N/A'}...")
    if r["tools"]:
        print(f"  Tools: {r['tools']}")
    if r["parts"]:
        print(f"  Parts: {r['parts']}")

    # Now get ALL nodes with their full detail + connected tools/parts
    detail_query = """
        MATCH (n) WHERE n.id IN $ids
        OPTIONAL MATCH (n)-[:REQUIRES_TOOL]->(tool)
        OPTIONAL MATCH (n)-[:REQUIRES_PART]->(part)
        RETURN n.id AS id, labels(n)[0] AS label, n.title AS title,
               n.description AS desc, n.instructions AS instructions,
               n.expected_result AS expected,
               collect(DISTINCT tool.title) AS tools,
               collect(DISTINCT part.title) AS parts
    """
    nodes = s.run(detail_query, {"ids": list(all_ids)}).data()
    nodes_by_id = {n["id"]: n for n in nodes}

    # Sample some Test and Step nodes to check detail level
    print("\n" + "=" * 70)
    print("SAMPLE TEST NODES — checking for bolt-level detail")
    print("=" * 70)

    tests = [n for n in nodes if n["label"] == "Test" and n["desc"]]
    for t in tests[:8]:
        print(f"\n[Test] {t['title']}")
        if t["desc"]:
            print(f"  Description: {t['desc'][:400]}")
        if t["instructions"]:
            print(f"  Instructions: {t['instructions'][:400]}")
        if t["expected"]:
            print(f"  Expected: {t['expected'][:200]}")
        if t["tools"]:
            print(f"  Tools: {t['tools']}")
        if t["parts"]:
            print(f"  Parts: {t['parts']}")

    print("\n" + "=" * 70)
    print("SAMPLE STEP NODES — checking for hands-on detail")
    print("=" * 70)

    steps = [n for n in nodes if n["label"] == "Step"]
    for st in steps[:8]:
        print(f"\n[Step] {st['title']}")
        if st["desc"]:
            print(f"  Description: {st['desc'][:400]}")
        if st["instructions"]:
            print(f"  Instructions: {st['instructions'][:400]}")
        if st["tools"]:
            print(f"  Tools: {st['tools']}")
        if st["parts"]:
            print(f"  Parts: {st['parts']}")

    print("\n" + "=" * 70)
    print("SAMPLE SOLUTION NODES — checking for repair detail")
    print("=" * 70)

    solutions = [n for n in nodes if n["label"] == "Solution"]
    for sol in solutions[:8]:
        print(f"\n[Solution] {sol['title']}")
        if sol["desc"]:
            print(f"  Description: {sol['desc'][:400]}")
        if sol["instructions"]:
            print(f"  Instructions: {sol['instructions'][:400]}")
        if sol["tools"]:
            print(f"  Tools: {sol['tools']}")
        if sol["parts"]:
            print(f"  Parts: {sol['parts']}")

    # Summary: how many nodes have instructions, tools, parts?
    print("\n" + "=" * 70)
    print("DETAIL COVERAGE SUMMARY")
    print("=" * 70)
    total = len(nodes)
    has_desc = sum(1 for n in nodes if n["desc"])
    has_instr = sum(1 for n in nodes if n["instructions"])
    has_tools = sum(1 for n in nodes if n["tools"])
    has_parts = sum(1 for n in nodes if n["parts"])
    has_expected = sum(1 for n in nodes if n["expected"])
    print(f"  Total nodes in tree: {total}")
    print(f"  With description: {has_desc} ({100*has_desc//total}%)")
    print(f"  With instructions: {has_instr} ({100*has_instr//total}%)")
    print(f"  With expected_result: {has_expected} ({100*has_expected//total}%)")
    print(f"  With tools: {has_tools} ({100*has_tools//total}%)")
    print(f"  With parts: {has_parts} ({100*has_parts//total}%)")

driver.close()
