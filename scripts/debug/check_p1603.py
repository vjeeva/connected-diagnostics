"""Walk the P1603 diagnostic tree and report completeness."""
from neo4j import GraphDatabase
from backend.app.core.config import settings

driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))

with driver.session() as s:
    # Find P1603 Problem node
    p = s.run(
        'MATCH (p:Problem) WHERE "P1603" IN p.dtc_codes '
        'RETURN p.id AS id, p.title AS title, p.dtc_codes AS dtcs, p.description AS desc'
    ).data()
    print(f"P1603 Problem nodes: {len(p)}")
    for node in p:
        print(f"  ID: {node['id']}")
        print(f"  Title: {node['title']}")
        print(f"  DTCs: {node['dtcs']}")
        print()

    if not p:
        print("No P1603 node found!")
        driver.close()
        exit()

    pid = p[0]["id"]

    # BFS walk the tree
    visited = set()
    frontier = {pid}
    level = 0
    tree_nodes = []

    while frontier:
        level += 1
        result = s.run(
            "MATCH (a)-[r:LEADS_TO|NEXT_STEP]->(b) "
            "WHERE a.id IN $ids AND NOT b.id IN $visited "
            "RETURN DISTINCT b.id AS bid, labels(b)[0] AS label, b.title AS title, "
            "type(r) AS rtype, r.condition AS condition, "
            "NOT (b)-[:LEADS_TO|NEXT_STEP]->() AS is_leaf",
            {"ids": list(frontier), "visited": list(visited)},
        )

        next_frontier = set()
        records = result.data()

        if not records:
            break

        for rec in records:
            next_frontier.add(rec["bid"])
            tree_nodes.append({
                "level": level,
                "id": rec["bid"],
                "label": rec["label"],
                "title": rec["title"],
                "rtype": rec["rtype"],
                "condition": rec["condition"],
                "is_leaf": rec["is_leaf"],
            })

        visited.update(frontier)
        frontier = next_frontier - visited

    visited.update(frontier)

    print(f"Tree depth: {level}")
    print(f"Total nodes: {len(visited)}")
    print()

    # Count by type
    label_counts = {}
    leaf_count = 0
    for n in tree_nodes:
        label_counts[n["label"]] = label_counts.get(n["label"], 0) + 1
        if n["is_leaf"]:
            leaf_count += 1
    print(f"Node types: {label_counts}")
    print(f"Leaf nodes: {leaf_count}")
    print()

    # Print tree by level
    for lvl in range(1, level + 1):
        level_nodes = [n for n in tree_nodes if n["level"] == lvl]
        print(f"--- Level {lvl} ({len(level_nodes)} nodes) ---")
        for n in level_nodes:
            leaf = " [LEAF]" if n["is_leaf"] else ""
            cond = f' (condition: {n["condition"]})' if n["condition"] else ""
            print(f'  {n["rtype"]} -> [{n["label"]}] {n["title"]}{cond}{leaf}')

    # Check leaf nodes — what types are they?
    print(f"\n--- LEAF NODE ANALYSIS ---")
    leaf_types = {}
    for n in tree_nodes:
        if n["is_leaf"]:
            leaf_types.setdefault(n["label"], []).append(n["title"])
    for label, titles in leaf_types.items():
        print(f"\n  {label} leaves ({len(titles)}):")
        for t in titles[:10]:
            print(f"    - {t}")
        if len(titles) > 10:
            print(f"    ... and {len(titles) - 10} more")

    # Check for unresolved cross-references
    print(f"\n--- CROSS-REFERENCE CHECK ---")
    all_ids = list(visited)
    xrefs = s.run(
        "MATCH (n) WHERE n.id IN $ids AND n.unresolved_ref IS NOT NULL "
        "RETURN n.title AS title, n.unresolved_ref AS ref, labels(n)[0] AS label",
        {"ids": all_ids},
    ).data()
    if xrefs:
        print(f"Unresolved cross-references: {len(xrefs)}")
        for x in xrefs:
            print(f"  [{x['label']}] {x['title']} -> ref: {x['ref']}")
    else:
        print("No unresolved cross-references.")

    # Check Tests without OK/NG branches
    print(f"\n--- DEAD-END TESTS ---")
    dead_tests = s.run(
        "MATCH (t:Test) WHERE t.id IN $ids AND NOT (t)-[:LEADS_TO]->() "
        "RETURN t.title AS title, t.id AS id",
        {"ids": all_ids},
    ).data()
    if dead_tests:
        print(f"Tests with no outgoing LEADS_TO: {len(dead_tests)}")
        for t in dead_tests:
            print(f"  - {t['title']}")
    else:
        print("All Tests have outgoing branches.")

driver.close()
