"""Build Neo4j graph from extracted structured data."""

from __future__ import annotations

import re

from backend.app.graph.mutations import create_node, create_relationship, create_vehicle


# Node types that should be linked to the vehicle
VEHICLE_LINKED_TYPES = {"Problem", "Solution"}

# Valid node types from our schema
VALID_NODE_TYPES = {"Problem", "Symptom", "Test", "Result", "Solution", "Step", "Part", "Tool"}

# Valid relationship types
VALID_REL_TYPES = {
    "LEADS_TO", "NEXT_STEP", "REQUIRES_PART", "REQUIRES_TOOL",
    "APPLIES_TO", "ALTERNATIVE", "SIMILAR_TO", "SHARED_PROCEDURE",
    "BELONGS_TO", "HAS_COMPONENT", "FITS", "SUBSTITUTES",
}


def build_vehicle(make: str, model: str, year_start: int, year_end: int) -> list[str]:
    """Create Vehicle nodes for each year in range. Returns list of vehicle node IDs."""
    vehicle_ids = []
    for year in range(year_start, year_end + 1):
        vid = create_vehicle(make=make, model=model, year=year)
        vehicle_ids.append(vid)
    return vehicle_ids


def build_from_extraction(
    extraction: dict,
    vehicle_ids: list[str],
    title_to_id: dict[str, str],
    chunk_hash: str | None = None,
) -> dict[str, str]:
    """Create nodes and relationships from a single chunk's extraction result.

    Args:
        extraction: The JSON dict from extractor (nodes, relationships)
        vehicle_ids: List of Vehicle node IDs to link Problem/Solution nodes to
        title_to_id: Shared dict mapping node titles to neo4j IDs for dedup
        chunk_hash: Content hash of the source chunk (for scoped queries/cleanup)

    Returns:
        Updated title_to_id mapping
    """
    nodes = extraction.get("nodes", [])
    relationships = extraction.get("relationships", [])

    # Map temp_ids to real neo4j IDs for this chunk
    temp_to_real: dict[str, str] = {}

    # Create nodes
    for node in nodes:
        node_type = node.get("type", "")
        if node_type not in VALID_NODE_TYPES:
            continue

        # Build properties, excluding meta fields
        props = {k: v for k, v in node.items()
                 if k not in ("type", "temp_id") and v is not None}

        if chunk_hash:
            props["chunk_hash"] = chunk_hash

        title = props.get("title") or props.get("name", "")
        dedup_key = _dedup_key(node_type, title, props)

        # Dedup: reuse existing node if same type+title (or same DTC codes)
        if dedup_key in title_to_id:
            temp_to_real[node.get("temp_id", "")] = title_to_id[dedup_key]
            continue

        node_id = create_node(node_type, props)
        temp_id = node.get("temp_id", "")
        if temp_id:
            temp_to_real[temp_id] = node_id
        if title:
            title_to_id[dedup_key] = node_id

        # Link Problem/Solution to all Vehicle nodes
        if node_type in VEHICLE_LINKED_TYPES:
            for vid in vehicle_ids:
                create_relationship(node_id, vid, "APPLIES_TO")

    # Auto-link Problems to the first Test in the chunk if no explicit link exists
    problem_ids = []
    first_test_id = None
    for node in nodes:
        temp_id = node.get("temp_id", "")
        if not temp_id or temp_id not in temp_to_real:
            continue
        if node.get("type") == "Problem":
            problem_ids.append(temp_to_real[temp_id])
        elif node.get("type") == "Test" and first_test_id is None:
            first_test_id = temp_to_real[temp_id]

    # Check if LLM already created Problem→Test links
    problem_linked = set()
    for rel in relationships:
        if rel.get("type") == "LEADS_TO":
            from_temp = rel.get("from_temp_id", "")
            from_real = temp_to_real.get(from_temp)
            if from_real in problem_ids:
                problem_linked.add(from_real)

    if first_test_id:
        for pid in problem_ids:
            if pid not in problem_linked:
                create_relationship(pid, first_test_id, "LEADS_TO",
                                    {"condition": "initial_diagnostic_step", "confidence": 0.9})

    # Create relationships
    for rel in relationships:
        rel_type = rel.get("type", "")
        if rel_type not in VALID_REL_TYPES:
            continue

        # Resolve temp IDs to real IDs
        from_id = _resolve_id(rel, "from_temp_id", "from", temp_to_real)
        to_id = _resolve_id(rel, "to_temp_id", "to", temp_to_real)

        if not from_id or not to_id:
            continue

        # Build relationship properties
        rel_props = {k: v for k, v in rel.items()
                     if k not in ("type", "from_temp_id", "to_temp_id", "from", "to",
                                  "from_solution", "to_step", "to_part", "to_tool")
                     and v is not None}

        create_relationship(from_id, to_id, rel_type, rel_props if rel_props else None)

    return title_to_id


def _normalize_title(title: str) -> str:
    """Normalize a title for dedup: lowercase, strip punctuation, collapse whitespace."""
    t = title.lower().strip()
    t = re.sub(r"[^a-z0-9\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t


def _dedup_key(node_type: str, title: str, props: dict) -> str:
    """Build a dedup key. Problems dedup by DTC codes if available, otherwise by normalized title."""
    if node_type == "Problem":
        dtc_codes = props.get("dtc_codes")
        if dtc_codes and isinstance(dtc_codes, list):
            return f"Problem:dtc:{','.join(sorted(dtc_codes))}"
    return f"{node_type}:{_normalize_title(title)}"


def _resolve_id(rel: dict, temp_key: str, alt_key: str, temp_to_real: dict) -> str | None:
    """Resolve a temp_id or alternative key to a real neo4j node ID."""
    temp_id = rel.get(temp_key) or rel.get(alt_key, "")
    # Also check keys like from_solution, to_step, to_part, to_tool
    if not temp_id:
        for k in ("from_solution", "to_step", "to_part", "to_tool"):
            if k in rel:
                temp_id = rel[k]
                break
    return temp_to_real.get(temp_id)
