"""Link Solution nodes (REPLACE X, REPAIR X) to their removal/installation
procedure content using semantic search over chunks."""

import re

from neo4j import GraphDatabase
from backend.app.core.config import settings
from backend.app.services.search_service import search_chunks

driver = GraphDatabase.driver(
    settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
)

# 1. Get all Solution nodes that say REPLACE/REPAIR but have no procedure linked
with driver.session() as s:
    solutions = s.run("""
        MATCH (sol:Solution)
        WHERE (sol.title CONTAINS 'REPLACE' OR sol.title CONTAINS 'REPAIR')
        AND NOT (sol)-[:NEXT_STEP]->()
        RETURN sol.id AS id, sol.title AS title
    """).data()

print(f"Solution nodes without procedures: {len(solutions)}")

# 2. For each Solution, search chunks for the matching removal/installation procedure
linked = 0
skipped = 0

for sol in solutions:
    title = sol["title"]
    sol_id = sol["id"]

    # Extract the component name from the title
    # "REPLACE EGR VALVE" -> "EGR VALVE"
    # "REPAIR OR REPLACE INTAKE SYSTEM" -> "INTAKE SYSTEM"
    component = title
    for prefix in ["REPLACE ", "REPAIR OR REPLACE ", "REPAIR "]:
        if component.startswith(prefix):
            component = component[len(prefix):]
            break

    # Strip parenthetical qualifiers: "CAMSHAFT OIL CONTROL VALVE (for Exhaust Side)" -> "CAMSHAFT OIL CONTROL VALVE"
    component_base = re.sub(r'\s*\(.*\)\s*$', '', component).strip()

    if not component_base or component_base in ("MALFUNCTIONING PARTS, COMPONENT AND AREA",
                                                  "MALFUNCTIONING PARTS",
                                                  "HARNESS OR CONNECTOR"):
        skipped += 1
        continue

    # Search for removal/installation procedure for this component
    search_query = f"{component_base} removal installation procedure torque bolt"
    try:
        results = search_chunks(search_query, limit=5)
    except Exception as e:
        print(f"  Search error for '{component}': {e}")
        skipped += 1
        continue

    if not results:
        skipped += 1
        continue

    # Pick the best result that actually mentions the component AND has procedure content
    component_lower = component_base.lower()
    best = None
    for candidate in results:
        chunk_text = candidate["chunk_text"]
        distance = candidate["distance"]
        has_component = component_lower in chunk_text.lower()
        has_procedure = any(kw in chunk_text.lower() for kw in
                            ["torque:", "remove the", "install the", "disconnect the",
                             "n·m", "ft·lbf", "bolt", "nut"])
        if has_component and has_procedure and distance < 0.6:
            best = candidate
            break

    if not best:
        skipped += 1
        continue

    chunk_text = best["chunk_text"]
    distance = best["distance"]
    page = best["page_number"]

    # Extract a focused procedure snippet (the relevant section)
    # Use full component name (with qualifier like "for Exhaust Side") for snippet extraction
    # so we get the side/bank-specific procedure, not the generic one
    lines = chunk_text.split("\n")
    relevant_lines = []
    capturing = False

    # Try qualifier text first (e.g. "for exhaust side"), then full component, then base name
    snippet_targets = []
    qualifier = component[len(component_base):].strip().strip("()")
    if qualifier:
        snippet_targets.append(qualifier.lower())
    if component != component_base:
        snippet_targets.append(component.lower())
    snippet_targets.append(component_lower)

    for target in snippet_targets:
        relevant_lines = []
        capturing = False
        for line in lines:
            if target in line.lower() or (capturing and line.strip()):
                capturing = True
                relevant_lines.append(line)
            elif capturing and not line.strip():
                relevant_lines.append(line)
                if len(relevant_lines) > 30:
                    break
            elif capturing and any(kw in line.lower() for kw in
                                   ["components;", "last modified:", "doc id:"]):
                break
        if relevant_lines:
            break

    procedure_text = "\n".join(relevant_lines[:50]).strip()
    if not procedure_text:
        procedure_text = chunk_text[:2000]

    # Update the Solution node with the procedure details
    with driver.session() as s:
        s.run("""
            MATCH (sol:Solution {id: $sol_id})
            SET sol.procedure = $procedure,
                sol.procedure_page = $page,
                sol.procedure_source = 'auto-linked from chunk'
        """, {
            "sol_id": sol_id,
            "procedure": procedure_text[:3000],
            "page": page,
        })

    linked += 1
    print(f"  [{linked}] {title} -> p.{page} (dist={distance:.3f}, {len(procedure_text)} chars)")

print(f"\nLinked: {linked}, Skipped: {skipped}, Total: {len(solutions)}")

# 3. Summary: check a few linked solutions
print("\n=== SAMPLE LINKED SOLUTION ===")
with driver.session() as s:
    sample = s.run("""
        MATCH (sol:Solution)
        WHERE sol.procedure IS NOT NULL
        RETURN sol.title AS title, sol.procedure AS procedure, sol.procedure_page AS page
        LIMIT 2
    """).data()
    for ss in sample:
        print(f"\n[Solution] {ss['title']} (procedure from p.{ss['page']})")
        print(f"  {ss['procedure'][:500]}")

driver.close()
