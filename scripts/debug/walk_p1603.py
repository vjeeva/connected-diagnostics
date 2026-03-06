"""Interactive walk of the P1603 diagnostic tree, level by level."""
from neo4j import GraphDatabase
from backend.app.core.config import settings

driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))

with driver.session() as s:
    # Find P1603 Problem node
    p = s.run(
        'MATCH (p:Problem) WHERE "P1603" IN p.dtc_codes '
        'RETURN elementId(p) AS eid, p.title AS title, p.dtc_codes AS dtcs, '
        'p.description AS desc, p.instruction AS instruction'
    ).single()

    print("=" * 70)
    print(f"[Problem] {p['title']}")
    print(f"  DTCs: {p['dtcs']}")
    if p["desc"]:
        print(f"  Description: {p['desc'][:500]}")
    if p["instruction"]:
        print(f"  Instruction: {p['instruction'][:500]}")
    root_eid = p["eid"]

    # BFS walk — show each level with full detail
    frontier = [root_eid]
    visited = set(frontier)
    level = 0

    while frontier:
        level += 1
        children = s.run("""
            MATCH (a)-[r:LEADS_TO|NEXT_STEP]->(b)
            WHERE elementId(a) IN $eids
            RETURN elementId(b) AS eid, labels(b)[0] AS label, b.title AS title,
                   type(r) AS rtype, r.condition AS condition,
                   b.instruction AS instruction, b.expected_result AS expected,
                   b.tool_required AS tool, b.procedure AS procedure
            ORDER BY r.condition, b.title
        """, {"eids": frontier}).data()

        if not children:
            break

        print(f"\n{'=' * 70}")
        print(f"LEVEL {level} — {len(children)} nodes")
        print("=" * 70)

        next_frontier = []
        for c in children:
            if c["eid"] in visited:
                continue
            visited.add(c["eid"])
            next_frontier.append(c["eid"])

            cond = f' [{c["condition"]}]' if c["condition"] else ""
            print(f"\n  {c['rtype']}{cond} -> [{c['label']}] {c['title']}")
            if c["instruction"]:
                print(f"    Instruction: {c['instruction'][:300]}")
            if c["expected"]:
                print(f"    Expected: {c['expected'][:200]}")
            if c["tool"]:
                print(f"    Tool: {c['tool']}")
            if c["procedure"]:
                print(f"    Procedure (linked): {c['procedure'][:200]}...")

        frontier = next_frontier

    print(f"\n{'=' * 70}")
    print(f"Total levels: {level}, Total nodes visited: {len(visited)}")

driver.close()
