"""Neo4j schema: constraints and indexes. Run once on startup."""

from backend.app.db.neo4j_client import run_write

CONSTRAINTS = [
    "CREATE CONSTRAINT problem_id IF NOT EXISTS FOR (p:Problem) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT symptom_id IF NOT EXISTS FOR (s:Symptom) REQUIRE s.id IS UNIQUE",
    "CREATE CONSTRAINT test_id IF NOT EXISTS FOR (t:Test) REQUIRE t.id IS UNIQUE",
    "CREATE CONSTRAINT result_id IF NOT EXISTS FOR (r:Result) REQUIRE r.id IS UNIQUE",
    "CREATE CONSTRAINT solution_id IF NOT EXISTS FOR (s:Solution) REQUIRE s.id IS UNIQUE",
    "CREATE CONSTRAINT step_id IF NOT EXISTS FOR (s:Step) REQUIRE s.id IS UNIQUE",
    "CREATE CONSTRAINT part_id IF NOT EXISTS FOR (p:Part) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT tool_id IF NOT EXISTS FOR (t:Tool) REQUIRE t.id IS UNIQUE",
    "CREATE CONSTRAINT vehicle_id IF NOT EXISTS FOR (v:Vehicle) REQUIRE v.id IS UNIQUE",
    "CREATE CONSTRAINT system_id IF NOT EXISTS FOR (s:System) REQUIRE s.id IS UNIQUE",
    "CREATE CONSTRAINT component_id IF NOT EXISTS FOR (c:Component) REQUIRE c.id IS UNIQUE",
]

INDEXES = [
    "CREATE INDEX problem_dtc_idx IF NOT EXISTS FOR (p:Problem) ON (p.dtc_codes)",
    "CREATE INDEX vehicle_make_model IF NOT EXISTS FOR (v:Vehicle) ON (v.make, v.model, v.year)",
    "CREATE INDEX problem_chunk_hash IF NOT EXISTS FOR (p:Problem) ON (p.chunk_hash)",
    "CREATE INDEX test_chunk_hash IF NOT EXISTS FOR (t:Test) ON (t.chunk_hash)",
    "CREATE INDEX solution_chunk_hash IF NOT EXISTS FOR (s:Solution) ON (s.chunk_hash)",
    "CREATE INDEX step_chunk_hash IF NOT EXISTS FOR (s:Step) ON (s.chunk_hash)",
    "CREATE INDEX symptom_chunk_hash IF NOT EXISTS FOR (s:Symptom) ON (s.chunk_hash)",
    "CREATE INDEX result_chunk_hash IF NOT EXISTS FOR (r:Result) ON (r.chunk_hash)",
]


def ensure_schema():
    """Create all constraints and indexes (idempotent)."""
    for stmt in CONSTRAINTS + INDEXES:
        run_write(stmt)
