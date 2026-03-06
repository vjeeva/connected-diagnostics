"""Neo4j write operations for creating nodes and relationships."""

import uuid

from backend.app.db.neo4j_client import run_write


def _new_id() -> str:
    return str(uuid.uuid4())


def create_vehicle(make: str, model: str, year: int, engine: str = "", trim: str = "") -> str:
    node_id = _new_id()
    run_write(
        "CREATE (v:Vehicle {id: $id, make: $make, model: $model, year: $year, "
        "engine: $engine, trim: $trim})",
        {"id": node_id, "make": make, "model": model, "year": year,
         "engine": engine, "trim": trim},
    )
    return node_id


def create_node(label: str, properties: dict) -> str:
    """Create a node with the given label and properties. Returns the node ID."""
    node_id = properties.get("id") or _new_id()
    properties["id"] = node_id
    props_str = ", ".join(f"{k}: ${k}" for k in properties)
    run_write(f"CREATE (n:{label} {{{props_str}}})", properties)
    return node_id


def create_relationship(
    from_id: str,
    to_id: str,
    rel_type: str,
    properties: dict | None = None,
) -> None:
    """Create a relationship between two nodes identified by their IDs."""
    props = properties or {}
    props_str = ""
    if props:
        props_str = " {" + ", ".join(f"{k}: ${k}" for k in props) + "}"
    query = (
        f"MATCH (a {{id: $from_id}}), (b {{id: $to_id}}) "
        f"CREATE (a)-[:{rel_type}{props_str}]->(b)"
    )
    params = {"from_id": from_id, "to_id": to_id, **props}
    run_write(query, params)


def merge_node(label: str, match_props: dict, set_props: dict | None = None) -> str:
    """MERGE a node (create if not exists, update if exists). Returns node ID."""
    node_id = match_props.get("id") or _new_id()
    match_props["id"] = node_id
    match_str = ", ".join(f"{k}: ${k}" for k in match_props)
    query = f"MERGE (n:{label} {{{match_str}}})"
    params = dict(match_props)

    if set_props:
        set_parts = ", ".join(f"n.{k} = $set_{k}" for k in set_props)
        query += f" ON CREATE SET {set_parts} ON MATCH SET {set_parts}"
        params.update({f"set_{k}": v for k, v in set_props.items()})

    run_write(query, params)
    return node_id
