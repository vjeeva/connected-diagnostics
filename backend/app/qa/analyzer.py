"""Quality analysis engine for the diagnostic knowledge graph.

Runs a suite of checks against Neo4j and Postgres for a given page range,
producing a structured report of issues found.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import create_engine, text as sa_text

from backend.app.core.config import settings
from backend.app.db.neo4j_client import get_driver


@dataclass
class Issue:
    check: str
    severity: str  # "error", "warning", "info"
    message: str
    node_ids: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


@dataclass
class TreeInfo:
    problem_id: str
    problem_title: str
    dtc_codes: list[str]
    max_depth: int
    node_count: int
    leaf_types: list[str]  # what types are at the leaves (Solution, Test, Result, etc.)


@dataclass
class QAReport:
    page_range: tuple[int, int]
    chunk_hashes: list[str]
    total_nodes: int
    total_relationships: int
    node_counts: dict[str, int]  # by label
    trees: list[TreeInfo]
    issues: list[Issue]
    summary: dict  # aggregated metrics for tracking

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")


def analyze(start_page: int, end_page: int) -> QAReport:
    """Run all quality checks for nodes derived from the given page range."""
    chunk_hashes = _get_chunk_hashes(start_page, end_page)
    if not chunk_hashes:
        return QAReport(
            page_range=(start_page, end_page),
            chunk_hashes=[],
            total_nodes=0,
            total_relationships=0,
            node_counts={},
            trees=[],
            issues=[Issue("no_data", "error", f"No chunks found for pages {start_page}-{end_page}")],
            summary={},
        )

    driver = get_driver()
    issues: list[Issue] = []

    # Gather basic counts
    node_counts = _count_nodes(driver, chunk_hashes)
    total_nodes = sum(node_counts.values())
    total_rels = _count_relationships(driver, chunk_hashes)

    # Run checks
    issues.extend(_check_duplicates(driver, chunk_hashes))
    issues.extend(_check_null_titles(driver, chunk_hashes))
    issues.extend(_check_orphan_nodes(driver, chunk_hashes))
    issues.extend(_check_dead_end_tests(driver, chunk_hashes))
    issues.extend(_check_unlinked_problems(driver, chunk_hashes))
    issues.extend(_check_property_types(driver, chunk_hashes))

    # Analyze tree depth and structure
    trees = _analyze_trees(driver, chunk_hashes)
    issues.extend(_check_shallow_trees(trees))

    summary = {
        "total_nodes": total_nodes,
        "total_relationships": total_rels,
        "node_counts": node_counts,
        "tree_count": len(trees),
        "avg_depth": round(sum(t.max_depth for t in trees) / len(trees), 1) if trees else 0,
        "max_depth": max((t.max_depth for t in trees), default=0),
        "errors": sum(1 for i in issues if i.severity == "error"),
        "warnings": sum(1 for i in issues if i.severity == "warning"),
    }

    return QAReport(
        page_range=(start_page, end_page),
        chunk_hashes=chunk_hashes,
        total_nodes=total_nodes,
        total_relationships=total_rels,
        node_counts=node_counts,
        trees=trees,
        issues=issues,
        summary=summary,
    )


def discover_ingested_ranges(bucket_size: int = 100) -> list[tuple[int, int, int]]:
    """Find all page ranges that have ingested chunks in Postgres.

    Groups pages into buckets of `bucket_size`. Returns list of
    (start_page, end_page, chunk_count) sorted by start_page.
    """
    engine = create_engine(settings.postgres_sync_url)
    with engine.connect() as conn:
        rows = conn.execute(
            sa_text(
                "SELECT MIN(page_number) AS mn, MAX(page_number) AS mx, "
                "COUNT(*) AS cnt FROM manual_chunks"
            ),
        ).fetchone()

        if not rows or rows[0] is None:
            engine.dispose()
            return []

        min_page, max_page, total = rows[0], rows[1], rows[2]

        # Get chunk counts per bucket
        ranges = []
        start = (min_page // bucket_size) * bucket_size
        while start <= max_page:
            end = start + bucket_size - 1
            result = conn.execute(
                sa_text(
                    "SELECT COUNT(*) FROM manual_chunks "
                    "WHERE page_number >= :start AND page_number <= :end"
                ),
                {"start": start, "end": end},
            ).scalar()
            if result > 0:
                ranges.append((start, end, result))
            start += bucket_size

    engine.dispose()
    return ranges


def _get_chunk_hashes(start_page: int, end_page: int) -> list[str]:
    """Get content hashes for chunks in the page range from Postgres."""
    engine = create_engine(settings.postgres_sync_url)
    with engine.connect() as conn:
        rows = conn.execute(
            sa_text(
                "SELECT DISTINCT content_hash FROM manual_chunks "
                "WHERE page_number >= :start AND page_number <= :end"
            ),
            {"start": start_page, "end": end_page},
        ).fetchall()
    engine.dispose()
    return [row[0] for row in rows]


def _count_nodes(driver, chunk_hashes: list[str]) -> dict[str, int]:
    """Count nodes by label for the given chunk hashes."""
    with driver.session() as session:
        result = session.run(
            "MATCH (n) WHERE n.chunk_hash IN $hashes "
            "UNWIND labels(n) AS label "
            "RETURN label, count(*) AS cnt ORDER BY cnt DESC",
            {"hashes": chunk_hashes},
        )
        return {record["label"]: record["cnt"] for record in result}


def _count_relationships(driver, chunk_hashes: list[str]) -> int:
    """Count relationships between nodes in the chunk hash set."""
    with driver.session() as session:
        result = session.run(
            "MATCH (a)-[r]->(b) WHERE a.chunk_hash IN $hashes "
            "RETURN count(r) AS cnt",
            {"hashes": chunk_hashes},
        )
        return result.single()["cnt"]


def _check_duplicates(driver, chunk_hashes: list[str]) -> list[Issue]:
    """Find true duplicate nodes: same title AND same diagnostic context.

    Two nodes with the same title under different Problem trees are NOT duplicates —
    they're distinct procedures that share a generic name (e.g. "CHECK INTAKE SYSTEM"
    under P1603 vs under P1604 are different tests with different specs).

    Only flags nodes reachable from the same Problem as duplicates.
    """
    issues = []
    with driver.session() as session:
        # Find non-Problem nodes with same title that share a parent Problem
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
            WITH label, norm_title, a.id AS a_id, b.id AS b_id,
                 [x IN a_problems WHERE x IN b_problems] AS shared
            WHERE size(shared) > 0
            RETURN label, norm_title, a_id, b_id, shared
            """,
            {"hashes": chunk_hashes},
        )
        # Group by label+title
        seen: dict[str, list[str]] = {}
        for record in result:
            key = f"{record['label']}:{record['norm_title']}"
            if key not in seen:
                seen[key] = []
            for nid in [record["a_id"], record["b_id"]]:
                if nid not in seen[key]:
                    seen[key].append(nid)

        for key, ids in seen.items():
            label, title = key.split(":", 1)
            issues.append(Issue(
                check="duplicate_nodes",
                severity="error",
                message=f"{len(ids)}x {label}: \"{title}\" (same diagnostic tree)",
                node_ids=ids,
                details={"label": label, "title": title},
            ))

        # Check Problem nodes by DTC codes
        result = session.run(
            "MATCH (p:Problem) WHERE p.chunk_hash IN $hashes AND p.dtc_codes IS NOT NULL "
            "UNWIND p.dtc_codes AS dtc "
            "WITH dtc, collect(p.id) AS ids, collect(p.title) AS titles "
            "WHERE size(ids) > 1 "
            "RETURN dtc, ids, titles",
            {"hashes": chunk_hashes},
        )
        for record in result:
            issues.append(Issue(
                check="duplicate_dtc",
                severity="error",
                message=f"DTC {record['dtc']} appears on {len(record['ids'])} Problem nodes",
                node_ids=list(record["ids"]),
                details={"dtc": record["dtc"]},
            ))

    return issues


def _check_null_titles(driver, chunk_hashes: list[str]) -> list[Issue]:
    """Find nodes with NULL or empty titles."""
    issues = []
    with driver.session() as session:
        result = session.run(
            "MATCH (n) WHERE n.chunk_hash IN $hashes "
            "AND (n.title IS NULL OR trim(n.title) = '') "
            "AND NOT n:Vehicle "
            "RETURN labels(n)[0] AS label, n.id AS id, "
            "keys(n) AS props",
            {"hashes": chunk_hashes},
        )
        by_label: dict[str, list[str]] = {}
        for record in result:
            label = record["label"]
            by_label.setdefault(label, []).append(record["id"])

        for label, ids in by_label.items():
            issues.append(Issue(
                check="null_title",
                severity="warning",
                message=f"{len(ids)} {label} node(s) with NULL/empty title",
                node_ids=ids,
                details={"label": label},
            ))
    return issues


def _check_orphan_nodes(driver, chunk_hashes: list[str]) -> list[Issue]:
    """Find nodes with no relationships at all (excluding Vehicle links)."""
    issues = []
    with driver.session() as session:
        result = session.run(
            "MATCH (n) WHERE n.chunk_hash IN $hashes "
            "AND NOT (n)-[]-() "
            "RETURN labels(n)[0] AS label, n.id AS id, n.title AS title",
            {"hashes": chunk_hashes},
        )
        orphans = [(record["label"], record["id"], record["title"]) for record in result]
        if orphans:
            by_label: dict[str, list] = {}
            for label, nid, title in orphans:
                by_label.setdefault(label, []).append((nid, title))
            for label, items in by_label.items():
                issues.append(Issue(
                    check="orphan_node",
                    severity="warning",
                    message=f"{len(items)} orphan {label} node(s) with no relationships",
                    node_ids=[i[0] for i in items],
                    details={"label": label, "titles": [i[1] for i in items[:5]]},
                ))
    return issues


def _check_dead_end_tests(driver, chunk_hashes: list[str]) -> list[Issue]:
    """Find Test nodes with no outgoing LEADS_TO (dead ends in diagnostic flow)."""
    issues = []
    with driver.session() as session:
        result = session.run(
            "MATCH (t:Test) WHERE t.chunk_hash IN $hashes "
            "AND NOT (t)-[:LEADS_TO]->() "
            "AND NOT (t)-[:NEXT_STEP]->() "
            "RETURN t.id AS id, t.title AS title",
            {"hashes": chunk_hashes},
        )
        dead_ends = [(record["id"], record["title"]) for record in result]
        if dead_ends:
            issues.append(Issue(
                check="dead_end_test",
                severity="warning",
                message=f"{len(dead_ends)} Test node(s) with no outgoing flow (dead ends)",
                node_ids=[d[0] for d in dead_ends],
                details={"titles": [d[1] for d in dead_ends[:10]]},
            ))
    return issues


def _check_unlinked_problems(driver, chunk_hashes: list[str]) -> list[Issue]:
    """Find Problem nodes not connected to any Test."""
    issues = []
    with driver.session() as session:
        result = session.run(
            "MATCH (p:Problem) WHERE p.chunk_hash IN $hashes "
            "AND NOT (p)-[:LEADS_TO]->(:Test) "
            "RETURN p.id AS id, p.title AS title, p.dtc_codes AS dtc",
            {"hashes": chunk_hashes},
        )
        unlinked = [(record["id"], record["title"], record["dtc"]) for record in result]
        if unlinked:
            issues.append(Issue(
                check="unlinked_problem",
                severity="error",
                message=f"{len(unlinked)} Problem node(s) not connected to any Test",
                node_ids=[u[0] for u in unlinked],
                details={"problems": [{"title": u[1], "dtc": u[2]} for u in unlinked[:10]]},
            ))
    return issues


def _check_property_types(driver, chunk_hashes: list[str]) -> list[Issue]:
    """Check for non-scalar property values that Neo4j can't store properly.

    Skips list properties (which Neo4j supports natively) and only flags
    properties where the value looks like a stringified JSON object.
    """
    issues = []
    with driver.session() as session:
        # Find nodes where string properties look like stringified objects
        result = session.run(
            "MATCH (n) WHERE n.chunk_hash IN $hashes "
            "WITH n, labels(n)[0] AS label, "
            "[k IN keys(n) WHERE n[k] IS :: STRING AND "
            "  (n[k] STARTS WITH '{' OR n[k] STARTS WITH '[{') "
            "] AS bad_keys "
            "WHERE size(bad_keys) > 0 "
            "RETURN label, n.id AS id, n.title AS title, bad_keys",
            {"hashes": chunk_hashes},
        )
        bad_props = [(record["label"], record["id"], record["title"], record["bad_keys"]) for record in result]
        if bad_props:
            issues.append(Issue(
                check="bad_property_type",
                severity="error",
                message=f"{len(bad_props)} node(s) with non-scalar property values",
                node_ids=[b[1] for b in bad_props],
                details={"nodes": [{"label": b[0], "title": b[2], "bad_keys": b[3]} for b in bad_props[:10]]},
            ))
    return issues


def _analyze_trees(driver, chunk_hashes: list[str]) -> list[TreeInfo]:
    """Analyze diagnostic tree depth and structure for each Problem node.

    Uses iterative BFS in Python to avoid expensive variable-length Cypher paths.
    """
    trees = []
    with driver.session() as session:
        # Get all Problem nodes
        problems = session.run(
            "MATCH (p:Problem) WHERE p.chunk_hash IN $hashes "
            "RETURN p.id AS id, p.title AS title, p.dtc_codes AS dtc",
            {"hashes": chunk_hashes},
        )
        problem_list = [(r["id"], r["title"], r["dtc"]) for r in problems]

        for pid, title, dtc in problem_list:
            depth, node_count, leaf_types = _bfs_tree_stats(session, pid)
            trees.append(TreeInfo(
                problem_id=pid,
                problem_title=title or "(no title)",
                dtc_codes=dtc or [],
                max_depth=depth,
                node_count=node_count,
                leaf_types=leaf_types,
            ))

    return trees


def _bfs_tree_stats(session, start_id: str, max_depth: int = 15) -> tuple[int, int, list[str]]:
    """BFS from a node, returning (max_depth, total_nodes_reached, leaf_types).

    Uses one Cypher query per level (cheap) instead of variable-length paths (expensive).
    """
    visited: set[str] = set()
    frontier = {start_id}
    depth = 0
    leaf_types: list[str] = []

    for level in range(1, max_depth + 1):
        if not frontier:
            break
        result = session.run(
            "MATCH (a)-[:LEADS_TO|NEXT_STEP]->(b) "
            "WHERE a.id IN $ids AND NOT b.id IN $visited "
            "RETURN DISTINCT b.id AS bid, labels(b)[0] AS label, "
            "NOT (b)-[:LEADS_TO|NEXT_STEP]->() AS is_leaf",
            {"ids": list(frontier), "visited": list(visited)},
        )
        next_frontier: set[str] = set()
        for record in result:
            bid = record["bid"]
            next_frontier.add(bid)
            if record["is_leaf"]:
                label = record["label"]
                if label and label not in leaf_types:
                    leaf_types.append(label)

        visited.update(frontier)
        frontier = next_frontier - visited
        if frontier:
            depth = level

    visited.update(frontier)
    # Subtract the start node from count
    node_count = len(visited) - 1
    return depth, max(node_count, 0), leaf_types


def _check_shallow_trees(trees: list[TreeInfo], min_depth: int = 3) -> list[Issue]:
    """Flag Problem nodes with very shallow diagnostic trees."""
    issues = []
    shallow = [t for t in trees if t.max_depth < min_depth]
    for t in shallow:
        dtc_str = ", ".join(t.dtc_codes) if t.dtc_codes else "no DTC"
        issues.append(Issue(
            check="shallow_tree",
            severity="warning" if t.max_depth > 0 else "error",
            message=f"Shallow tree (depth={t.max_depth}): {t.problem_title} ({dtc_str})",
            node_ids=[t.problem_id],
            details={
                "depth": t.max_depth,
                "node_count": t.node_count,
                "leaf_types": t.leaf_types,
            },
        ))
    return issues
