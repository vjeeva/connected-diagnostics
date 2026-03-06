"""Post-extraction graph enrichment.

After the graph is built and chunk embeddings exist, this module cross-links
data that spans multiple chunks — things the LLM can't see because it only
processes one chunk at a time.

Current enrichments:
  1. Link Solution nodes (REPLACE X) to removal/installation procedure chunks
  2. Link Test nodes with generic terminal references to connector pinout data
"""

from __future__ import annotations

import re

from rich.console import Console

from backend.app.db.neo4j_client import get_driver
from backend.app.services.search_service import search_chunks

console = Console()


def enrich_graph():
    """Run all post-extraction enrichments."""
    console.print("[dim]Running post-extraction enrichment...[/dim]")

    linked_procedures = _link_solution_procedures()
    linked_pinouts = _link_connector_pinouts()

    total = linked_procedures + linked_pinouts
    if total:
        console.print(f"[green]Enrichment complete: {linked_procedures} procedures, {linked_pinouts} pinouts linked.[/green]\n")
    else:
        console.print("[dim]No new enrichments needed.[/dim]\n")


def _link_solution_procedures() -> int:
    """Link Solution nodes (REPLACE X, REPAIR X) to removal/installation procedures."""
    driver = get_driver()

    with driver.session() as s:
        solutions = s.run("""
            MATCH (sol:Solution)
            WHERE (sol.title CONTAINS 'REPLACE' OR sol.title CONTAINS 'REPAIR')
            AND sol.procedure IS NULL
            AND NOT (sol)-[:NEXT_STEP]->()
            RETURN elementId(sol) AS eid, sol.title AS title
        """).data()

    if not solutions:
        return 0

    console.print(f"[dim]  Linking procedures for {len(solutions)} Solution nodes...[/dim]")
    linked = 0

    for sol in solutions:
        title = sol["title"]

        # Extract component name
        component = title
        for prefix in ["REPLACE ", "REPAIR OR REPLACE ", "REPAIR "]:
            if component.startswith(prefix):
                component = component[len(prefix):]
                break

        # Strip parenthetical qualifiers for search, keep for snippet extraction
        component_base = re.sub(r'\s*\(.*\)\s*$', '', component).strip()

        if not component_base or component_base in (
            "MALFUNCTIONING PARTS, COMPONENT AND AREA",
            "MALFUNCTIONING PARTS",
            "HARNESS OR CONNECTOR",
        ):
            continue

        search_query = f"{component_base} removal installation procedure torque bolt"
        try:
            results = search_chunks(search_query, limit=5)
        except Exception:
            continue

        if not results:
            continue

        # Pick best result that mentions the component and has procedure content
        component_lower = component_base.lower()
        best = None
        for candidate in results:
            chunk_text = candidate["chunk_text"]
            has_component = component_lower in chunk_text.lower()
            has_procedure = any(kw in chunk_text.lower() for kw in [
                "torque:", "remove the", "install the", "disconnect the",
                "n·m", "ft·lbf", "bolt", "nut",
            ])
            if has_component and has_procedure and candidate["distance"] < 0.6:
                best = candidate
                break

        if not best:
            continue

        # Extract side-specific snippet using qualifier if present
        qualifier = component[len(component_base):].strip().strip("()")
        chunk_text = best["chunk_text"]
        lines = chunk_text.split("\n")

        snippet_targets = []
        if qualifier:
            snippet_targets.append(qualifier.lower())
        snippet_targets.append(component_lower)

        relevant_lines = []
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
                elif capturing and any(kw in line.lower() for kw in [
                    "components;", "last modified:", "doc id:",
                ]):
                    break
            if relevant_lines:
                break

        procedure_text = "\n".join(relevant_lines[:50]).strip()
        if not procedure_text:
            procedure_text = chunk_text[:2000]

        with driver.session() as s:
            s.run("""
                MATCH (sol) WHERE elementId(sol) = $eid
                SET sol.procedure = $procedure,
                    sol.procedure_page = $page,
                    sol.procedure_source = 'auto-linked from chunk'
            """, {
                "eid": sol["eid"],
                "procedure": procedure_text[:3000],
                "page": best["page_number"],
            })

        linked += 1

    if linked:
        console.print(f"[dim]  Linked {linked} Solution → procedure.[/dim]")
    return linked


def _link_connector_pinouts() -> int:
    """Link connector pinout data to Test nodes with generic terminal references."""
    driver = get_driver()

    with driver.session() as s:
        tests = s.run("""
            MATCH (t:Test)
            WHERE t.instruction IS NOT NULL
            AND t.connector_info IS NULL
            AND (t.instruction CONTAINS 'terminals 1-2' OR t.instruction CONTAINS 'terminal 1'
                 OR t.instruction CONTAINS 'terminals 1' OR t.instruction CONTAINS 'tester to terminals'
                 OR (t.instruction CONTAINS 'Measure the resistance'
                     AND NOT t.instruction =~ '.*[A-Z][0-9]+-[0-9]+.*'))
            RETURN elementId(t) AS eid, t.title AS title, t.instruction AS instruction
        """).data()

    if not tests:
        return 0

    console.print(f"[dim]  Linking connector pinouts for {len(tests)} Test nodes...[/dim]")
    linked = 0

    for test in tests:
        title = test["title"]
        component = _extract_component_from_test(title)

        if not component or len(component) < 3:
            continue

        search_query = f"{component} connector terminal pin voltage resistance front view"
        try:
            results = search_chunks(search_query, limit=5)
        except Exception:
            continue

        component_lower = component.lower()
        best_pinout = None
        best_page = None

        for r in results:
            text = r["chunk_text"]
            if component_lower not in text.lower():
                continue

            has_pins = bool(re.search(r'[A-Z]\d+-\d+', text))
            has_table = any(kw in text.lower() for kw in [
                "standard voltage:", "standard resistance:",
                "tester connection", "front view",
            ])

            if has_pins and has_table:
                pinout = _extract_pinout_section(text)
                if pinout and len(pinout) > 20:
                    best_pinout = pinout
                    best_page = r["page_number"]
                    break

        if not best_pinout:
            continue

        with driver.session() as s:
            s.run("""
                MATCH (t) WHERE elementId(t) = $eid
                SET t.connector_info = $pinout,
                    t.connector_info_page = $page,
                    t.connector_info_source = 'auto-linked from chunk'
            """, {
                "eid": test["eid"],
                "pinout": best_pinout[:3000],
                "page": best_page,
            })

        linked += 1

    if linked:
        console.print(f"[dim]  Linked {linked} Test → connector pinout.[/dim]")
    return linked


def _extract_component_from_test(title: str) -> str | None:
    """Extract component name from a Test title."""
    # CHECK HARNESS AND CONNECTOR (X - Y) -> Y is the component
    m = re.search(r'\(.*?-\s*(.+?)\)', title)
    if m:
        return m.group(1).strip()

    for prefix in ["INSPECT ", "CHECK "]:
        if title.startswith(prefix):
            return title[len(prefix):].strip()

    return title


def _extract_pinout_section(chunk_text: str) -> str | None:
    """Extract connector pin mapping and measurement tables from chunk text."""
    lines = chunk_text.split("\n")
    relevant_sections: list[str] = []
    current_section: list[str] = []
    capturing = False

    for line in lines:
        ll = line.lower().strip()

        is_start = any(kw in ll for kw in [
            "standard voltage:", "standard resistance:", "tester connection",
            "front view of wire harness", "front view of connector",
        ])

        has_pin_id = bool(re.search(r'[A-Z]\d+-\d+', line))

        if is_start:
            if current_section:
                relevant_sections.append("\n".join(current_section))
            current_section = [line]
            capturing = True
        elif capturing and has_pin_id:
            current_section.append(line)
        elif capturing and any(kw in ll for kw in [
            "specified condition", "condition", "always", "engine switch",
            "below 1", "10 k", "11 to 14", "body ground", "illustration",
        ]):
            current_section.append(line)
        elif capturing and ll in ("ok", "ng", ""):
            if current_section and len(current_section) > 1:
                relevant_sections.append("\n".join(current_section))
            current_section = []
            capturing = False
        elif capturing:
            current_section.append(line)

    if current_section and len(current_section) > 1:
        relevant_sections.append("\n".join(current_section))

    if not relevant_sections:
        return None

    return "\n\n".join(relevant_sections)
