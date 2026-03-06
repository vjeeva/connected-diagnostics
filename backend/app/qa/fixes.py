"""Automated fixes for common graph quality issues.

These fixes operate directly on Neo4j without re-ingesting — they're
post-processing cleanups that resolve issues the LLM+dedup pipeline missed.

Merge logic uses DIAGNOSTIC CONTEXT to determine true duplicates:
- Two "CHECK INTAKE SYSTEM" nodes under different Problem DTCs are NOT duplicates
- Two "CHECK INTAKE SYSTEM" nodes reachable from the SAME Problem are duplicates
"""

from __future__ import annotations

from backend.app.db.neo4j_client import get_driver


def merge_duplicate_nodes_native(chunk_hashes: list[str], dry_run: bool = False) -> list[dict]:
    """Merge nodes that are true duplicates: same title AND same diagnostic context.

    A node is a true duplicate if another node with the same label+title is
    reachable from the same Problem node (i.e. they're in the same diagnostic tree).
    Nodes with the same title under different Problems are left alone — they're
    distinct procedures that happen to share a generic name.
    """
    driver = get_driver()
    actions = []

    with driver.session() as session:
        # Find pairs of nodes with same label+title that share a parent Problem
        result = session.run(
            """
            MATCH (n) WHERE n.chunk_hash IN $hashes AND n.title IS NOT NULL
            AND NOT n:Vehicle AND NOT n:Problem
            WITH labels(n)[0] AS label, toLower(trim(n.title)) AS norm_title,
            collect(n) AS nodes
            WHERE size(nodes) > 1
            UNWIND nodes AS a
            UNWIND nodes AS b
            WITH label, norm_title, a, b WHERE a.id < b.id
            OPTIONAL MATCH (p:Problem)-[:LEADS_TO|NEXT_STEP*1..15]->(a)
            WITH label, norm_title, a, b, collect(DISTINCT p.id) AS a_problems
            OPTIONAL MATCH (p2:Problem)-[:LEADS_TO|NEXT_STEP*1..15]->(b)
            WITH label, norm_title, a, b, a_problems, collect(DISTINCT p2.id) AS b_problems
            WITH label, norm_title, a, b,
                 [x IN a_problems WHERE x IN b_problems] AS shared_problems
            WHERE size(shared_problems) > 0
            RETURN label, norm_title, a.id AS a_id, b.id AS b_id,
                   shared_problems,
                   COUNT { (a)-[]-() } AS a_rels,
                   COUNT { (b)-[]-() } AS b_rels
            """,
            {"hashes": chunk_hashes},
        )
        # Group merge pairs — keep the one with more relationships
        pairs = [
            (r["label"], r["norm_title"], r["a_id"], r["b_id"],
             r["a_rels"], r["b_rels"], r["shared_problems"])
            for r in result
        ]

        # Deduplicate: if A appears in multiple pairs, only process once
        already_removed: set[str] = set()

        for label, norm_title, a_id, b_id, a_rels, b_rels, shared in pairs:
            if a_id in already_removed or b_id in already_removed:
                continue

            if a_rels >= b_rels:
                keep_id, remove_id = a_id, b_id
            else:
                keep_id, remove_id = b_id, a_id

            action = {
                "label": label,
                "title": norm_title,
                "keep_id": keep_id,
                "remove_ids": [remove_id],
                "merged_count": 1,
                "shared_problems": shared,
            }
            actions.append(action)
            already_removed.add(remove_id)

            if not dry_run:
                _transfer_and_delete(session, keep_id, remove_id)

        # Also merge duplicate Problem nodes by DTC codes
        dtc_actions = _merge_duplicate_problems(session, chunk_hashes, dry_run)
        actions.extend(dtc_actions)

    return actions


def _merge_duplicate_problems(session, chunk_hashes: list[str], dry_run: bool) -> list[dict]:
    """Merge Problem nodes that share the same DTC code(s)."""
    actions = []

    result = session.run(
        "MATCH (p:Problem) WHERE p.chunk_hash IN $hashes AND p.dtc_codes IS NOT NULL "
        "UNWIND p.dtc_codes AS dtc "
        "WITH dtc, collect(p) AS problems "
        "WHERE size(problems) > 1 "
        "UNWIND problems AS prob "
        "WITH dtc, prob, COUNT { (prob)-[]-() } AS rels "
        "WITH dtc, collect({id: prob.id, title: prob.title, rels: rels}) AS node_info "
        "RETURN dtc, node_info",
        {"hashes": chunk_hashes},
    )

    already_removed: set[str] = set()

    for record in result:
        dtc = record["dtc"]
        node_info = record["node_info"]
        if len(node_info) < 2:
            continue

        # Sort: keep the one with more relationships
        sorted_nodes = sorted(node_info, key=lambda x: -x["rels"])
        keep = sorted_nodes[0]

        for node in sorted_nodes[1:]:
            if node["id"] in already_removed:
                continue

            action = {
                "label": "Problem",
                "title": f"DTC {dtc}: {keep['title']}",
                "keep_id": keep["id"],
                "remove_ids": [node["id"]],
                "merged_count": 1,
                "shared_problems": [dtc],
            }
            actions.append(action)
            already_removed.add(node["id"])

            if not dry_run:
                _transfer_and_delete(session, keep["id"], node["id"])

    return actions


def _transfer_and_delete(session, keep_id: str, remove_id: str):
    """Transfer all relationships from remove_id to keep_id, then delete remove_id."""
    # Get all incoming relationships of the duplicate
    incoming = session.run(
        "MATCH (other)-[r]->(n {id: $rid}) "
        "RETURN other.id AS from_id, type(r) AS rtype, properties(r) AS rprops",
        {"rid": remove_id},
    )
    for rec in incoming:
        if rec["from_id"] != keep_id:
            session.run(
                f"MATCH (a {{id: $from_id}}), (b {{id: $keep_id}}) "
                f"MERGE (a)-[:{rec['rtype']}]->(b)",
                {"from_id": rec["from_id"], "keep_id": keep_id},
            )

    # Get all outgoing relationships of the duplicate
    outgoing = session.run(
        "MATCH (n {id: $rid})-[r]->(other) "
        "RETURN other.id AS to_id, type(r) AS rtype, properties(r) AS rprops",
        {"rid": remove_id},
    )
    for rec in outgoing:
        if rec["to_id"] != keep_id:
            session.run(
                f"MATCH (a {{id: $keep_id}}), (b {{id: $to_id}}) "
                f"MERGE (a)-[:{rec['rtype']}]->(b)",
                {"keep_id": keep_id, "to_id": rec["to_id"]},
            )

    # Delete the duplicate
    session.run(
        "MATCH (n {id: $rid}) DETACH DELETE n",
        {"rid": remove_id},
    )


def delete_orphan_nodes(chunk_hashes: list[str], dry_run: bool = False) -> dict:
    """Delete nodes with no relationships (orphans).

    Returns summary of deleted nodes by label.
    """
    driver = get_driver()
    deleted: dict[str, int] = {}

    with driver.session() as session:
        result = session.run(
            "MATCH (n) WHERE n.chunk_hash IN $hashes "
            "AND NOT (n)-[]-() AND NOT n:Vehicle "
            "RETURN labels(n)[0] AS label, count(*) AS cnt, collect(n.id) AS ids",
            {"hashes": chunk_hashes},
        )
        for record in result:
            label = record["label"]
            cnt = record["cnt"]
            deleted[label] = cnt

        if not dry_run:
            session.run(
                "MATCH (n) WHERE n.chunk_hash IN $hashes "
                "AND NOT (n)-[]-() AND NOT n:Vehicle "
                "DELETE n",
                {"hashes": chunk_hashes},
            )

    return deleted


def delete_null_title_nodes(chunk_hashes: list[str], labels: list[str] | None = None, dry_run: bool = False) -> dict:
    """Delete nodes with NULL or empty titles.

    Args:
        labels: Optional list of labels to restrict deletion (e.g. ["Tool", "Part"])
    """
    driver = get_driver()
    deleted: dict[str, int] = {}

    label_filter = ""
    if labels:
        label_conditions = " OR ".join(f"n:{l}" for l in labels)
        label_filter = f" AND ({label_conditions})"

    with driver.session() as session:
        result = session.run(
            f"MATCH (n) WHERE n.chunk_hash IN $hashes "
            f"AND (n.title IS NULL OR trim(n.title) = '') "
            f"AND NOT n:Vehicle{label_filter} "
            f"RETURN labels(n)[0] AS label, count(*) AS cnt",
            {"hashes": chunk_hashes},
        )
        for record in result:
            deleted[record["label"]] = record["cnt"]

        if not dry_run:
            session.run(
                f"MATCH (n) WHERE n.chunk_hash IN $hashes "
                f"AND (n.title IS NULL OR trim(n.title) = '') "
                f"AND NOT n:Vehicle{label_filter} "
                f"DETACH DELETE n",
                {"hashes": chunk_hashes},
            )

    return deleted
