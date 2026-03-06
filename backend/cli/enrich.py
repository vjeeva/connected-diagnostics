"""CLI commands for post-ingestion graph enrichment."""

from __future__ import annotations

import re

import click
from rich.console import Console

from backend.app.core.config import settings
from backend.app.db.neo4j_client import get_driver, run_query
from backend.app.services.search_service import search_chunks

console = Console()


def _extract_component(title: str) -> str | None:
    """Extract the component name from a Test node title."""
    m = re.search(r'\(.*?-\s*(.+?)\)', title)
    if m:
        return m.group(1).strip()
    for prefix in ["INSPECT ", "CHECK "]:
        if title.startswith(prefix):
            return title[len(prefix):].strip()
    return title


def _extract_pinout_from_chunk(chunk_text: str) -> str | None:
    """Extract connector pin mapping and voltage/resistance tables from chunk text."""
    lines = chunk_text.split("\n")
    relevant_sections = []
    capturing = False
    current_section = []

    for line in lines:
        ll = line.lower().strip()
        is_measurement_start = any(kw in ll for kw in [
            "standard voltage:", "standard resistance:", "tester connection",
            "front view of wire harness", "front view of connector",
        ])
        has_pin_id = bool(re.search(r'[A-Z]\d+-\d+', line))

        if is_measurement_start:
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


@click.group()
def enrich_cli():
    """Post-ingestion graph enrichment commands."""


@enrich_cli.command("pinouts")
@click.option("--dry-run", is_flag=True, help="Show what would be linked without writing")
def link_pinouts(dry_run: bool):
    """Link connector pinout data to Test nodes that reference terminal measurements."""
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

    console.print(f"Test nodes needing connector pinout: [bold]{len(tests)}[/bold]")
    linked = 0
    skipped = 0

    for test in tests:
        title = test["title"]
        component = _extract_component(title)

        if not component or len(component) < 3:
            skipped += 1
            continue

        search_query = f"{component} connector terminal pin voltage resistance front view"
        try:
            results = search_chunks(search_query, limit=5)
        except Exception as e:
            console.print(f"  [red]Search error for '{component}': {e}[/red]")
            skipped += 1
            continue

        component_lower = component.lower()
        best_pinout = None
        best_chunk = None

        for r in results:
            text = r["chunk_text"]
            if component_lower not in text.lower():
                continue
            has_pins = bool(re.search(r'[A-Z]\d+-\d+', text))
            has_table = any(kw in text.lower() for kw in [
                "standard voltage:", "standard resistance:", "tester connection", "front view",
            ])
            if has_pins and has_table:
                pinout = _extract_pinout_from_chunk(text)
                if pinout and len(pinout) > 20:
                    best_pinout = pinout
                    best_chunk = r
                    break

        if not best_pinout:
            skipped += 1
            continue

        if dry_run:
            console.print(f"  [dim]Would link:[/dim] {title} -> p.{best_chunk['page_number']}")
        else:
            with driver.session() as s:
                s.run("""
                    MATCH (t) WHERE elementId(t) = $eid
                    SET t.connector_info = $pinout,
                        t.connector_info_page = $page,
                        t.connector_info_source = 'auto-linked from chunk'
                """, {
                    "eid": test["eid"],
                    "pinout": best_pinout[:3000],
                    "page": best_chunk["page_number"],
                })
            console.print(f"  [{linked + 1}] {title} -> p.{best_chunk['page_number']}")

        linked += 1

    console.print(f"\n{'Would link' if dry_run else 'Linked'}: [bold]{linked}[/bold], Skipped: {skipped}, Total: {len(tests)}")


@enrich_cli.command("procedures")
@click.option("--dry-run", is_flag=True, help="Show what would be linked without writing")
def link_procedures(dry_run: bool):
    """Link Solution nodes (REPLACE/REPAIR) to their removal/installation procedures."""
    driver = get_driver()

    with driver.session() as s:
        solutions = s.run("""
            MATCH (sol:Solution)
            WHERE (sol.title CONTAINS 'REPLACE' OR sol.title CONTAINS 'REPAIR')
            AND NOT (sol)-[:NEXT_STEP]->()
            RETURN sol.id AS id, sol.title AS title
        """).data()

    console.print(f"Solution nodes without procedures: [bold]{len(solutions)}[/bold]")
    linked = 0
    skipped = 0

    for sol in solutions:
        title = sol["title"]
        sol_id = sol["id"]

        component = title
        for prefix in ["REPLACE ", "REPAIR OR REPLACE ", "REPAIR "]:
            if component.startswith(prefix):
                component = component[len(prefix):]
                break

        component_base = re.sub(r'\s*\(.*\)\s*$', '', component).strip()

        if not component_base or component_base in (
            "MALFUNCTIONING PARTS, COMPONENT AND AREA",
            "MALFUNCTIONING PARTS", "HARNESS OR CONNECTOR",
        ):
            skipped += 1
            continue

        search_query = f"{component_base} removal installation procedure torque bolt"
        try:
            results = search_chunks(search_query, limit=5)
        except Exception as e:
            console.print(f"  [red]Search error for '{component}': {e}[/red]")
            skipped += 1
            continue

        if not results:
            skipped += 1
            continue

        component_lower = component_base.lower()
        best = None
        for candidate in results:
            chunk_text = candidate["chunk_text"]
            distance = candidate["distance"]
            has_component = component_lower in chunk_text.lower()
            has_procedure = any(kw in chunk_text.lower() for kw in [
                "torque:", "remove the", "install the", "disconnect the",
                "n·m", "ft·lbf", "bolt", "nut",
            ])
            if has_component and has_procedure and distance < 0.6:
                best = candidate
                break

        if not best:
            skipped += 1
            continue

        # Extract focused procedure snippet
        chunk_text = best["chunk_text"]
        page = best["page_number"]
        lines = chunk_text.split("\n")

        snippet_targets = []
        qualifier = component[len(component_base):].strip().strip("()")
        if qualifier:
            snippet_targets.append(qualifier.lower())
        if component != component_base:
            snippet_targets.append(component.lower())
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

        if dry_run:
            console.print(f"  [dim]Would link:[/dim] {title} -> p.{page} (dist={best['distance']:.3f})")
        else:
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
            console.print(f"  [{linked + 1}] {title} -> p.{page} (dist={best['distance']:.3f})")

        linked += 1

    console.print(f"\n{'Would link' if dry_run else 'Linked'}: [bold]{linked}[/bold], Skipped: {skipped}, Total: {len(solutions)}")


@enrich_cli.command("all")
@click.option("--dry-run", is_flag=True, help="Show what would be linked without writing")
@click.pass_context
def enrich_all(ctx, dry_run: bool):
    """Run all enrichment steps."""
    console.print("[bold]Running all enrichment steps...[/bold]\n")
    ctx.invoke(link_pinouts, dry_run=dry_run)
    console.print()
    ctx.invoke(link_procedures, dry_run=dry_run)


if __name__ == "__main__":
    enrich_cli()
