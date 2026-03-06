"""CLI for quality analysis of the diagnostic knowledge graph.

Usage:
    python -m backend.cli.qa analyze --start-page 2400 --end-page 2500
    python -m backend.cli.qa history --start-page 2400 --end-page 2500
    python -m backend.cli.qa compare --start-page 2400 --end-page 2500
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from backend.app.qa.analyzer import analyze, discover_ingested_ranges, QAReport
from backend.app.qa.fixes import merge_duplicate_nodes_native, delete_orphan_nodes, delete_null_title_nodes
from backend.app.qa.tracker import log_run, get_history, compare_last_two

console = Console()


def _estimate_cost(report: QAReport) -> float:
    """Rough cost estimate for re-ingesting the chunks in this range.

    Based on Haiku pricing: $0.25/M input, $1.25/M output tokens.
    Average chunk ~4000 tokens input, ~2000 tokens output.
    """
    n_chunks = len(report.chunk_hashes)
    input_cost = n_chunks * 4000 * 0.25 / 1_000_000
    output_cost = n_chunks * 2000 * 1.25 / 1_000_000
    embedding_cost = n_chunks * 0.02 / 1_000_000 * 8000  # text-embedding-3-small
    return round(input_cost + output_cost + embedding_cost, 4)


def _print_report(report: QAReport):
    """Pretty-print a QA report to the console."""
    console.print()

    # Header
    console.print(Panel(
        f"Pages {report.page_range[0]}-{report.page_range[1]} | "
        f"{report.total_nodes} nodes | {report.total_relationships} rels | "
        f"{len(report.chunk_hashes)} chunks",
        title="QA Analysis Report",
        style="bold cyan",
    ))

    # Node counts table
    if report.node_counts:
        table = Table(title="Node Counts", show_header=True)
        table.add_column("Label", style="bold")
        table.add_column("Count", justify="right")
        for label, count in sorted(report.node_counts.items(), key=lambda x: -x[1]):
            table.add_row(label, str(count))
        console.print(table)
        console.print()

    # Trees table
    if report.trees:
        table = Table(title="Diagnostic Trees", show_header=True)
        table.add_column("Problem", style="bold", max_width=50)
        table.add_column("DTC", style="cyan")
        table.add_column("Depth", justify="right")
        table.add_column("Nodes", justify="right")
        table.add_column("Leaf Types")
        for tree in sorted(report.trees, key=lambda t: -t.max_depth):
            depth_style = "green" if tree.max_depth >= 5 else "yellow" if tree.max_depth >= 3 else "red"
            table.add_row(
                tree.problem_title,
                ", ".join(tree.dtc_codes) if tree.dtc_codes else "-",
                f"[{depth_style}]{tree.max_depth}[/{depth_style}]",
                str(tree.node_count),
                ", ".join(tree.leaf_types) if tree.leaf_types else "-",
            )
        console.print(table)
        console.print()

    # Issues
    if report.issues:
        table = Table(title=f"Issues ({report.error_count} errors, {report.warning_count} warnings)", show_header=True)
        table.add_column("Sev", style="bold", width=5)
        table.add_column("Check")
        table.add_column("Message", max_width=80)
        table.add_column("Nodes", justify="right")
        for issue in sorted(report.issues, key=lambda i: (0 if i.severity == "error" else 1, i.check)):
            sev_style = "red" if issue.severity == "error" else "yellow" if issue.severity == "warning" else "dim"
            table.add_row(
                f"[{sev_style}]{issue.severity.upper()[:3]}[/{sev_style}]",
                issue.check,
                issue.message,
                str(len(issue.node_ids)),
            )
        console.print(table)
    else:
        console.print("[green]No issues found![/green]")

    console.print()


def _print_comparison(delta: dict):
    """Pretty-print a comparison between two runs."""
    table = Table(title="Improvement Comparison (vs. previous run)", show_header=True)
    table.add_column("Metric", style="bold")
    table.add_column("Previous", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("Change", justify="right")

    for key in ["total_nodes", "total_relationships", "tree_count", "avg_depth", "max_depth", "errors", "warnings"]:
        d = delta[key]
        change = d["change"]
        # For errors/warnings, decrease is good. For depth/nodes, increase is good.
        if key in ("errors", "warnings"):
            style = "green" if change < 0 else "red" if change > 0 else "dim"
        else:
            style = "green" if change > 0 else "red" if change < 0 else "dim"
        change_str = f"[{style}]{'+' if change > 0 else ''}{change}[/{style}]"
        table.add_row(
            key.replace("_", " ").title(),
            str(d["old"]),
            str(d["new"]),
            change_str,
        )

    table.add_row("Total Runs", "", str(delta["run_count"]), "")
    table.add_row("Cumulative Cost", "", f"${delta['total_cost_usd']:.4f}", "")
    console.print(table)


@click.group()
def cli():
    """Quality analysis tools for the diagnostic knowledge graph."""
    pass


@cli.command()
@click.option("--start-page", required=True, type=int, help="First page of range")
@click.option("--end-page", required=True, type=int, help="Last page of range")
@click.option("--log/--no-log", default=True, help="Log this run to the tracker (default: yes)")
@click.option("--notes", default="", help="Optional notes for this run")
def analyze_cmd(start_page: int, end_page: int, log: bool, notes: str):
    """Analyze graph quality for a page range."""
    console.print(f"[dim]Analyzing pages {start_page}-{end_page}...[/dim]")
    report = analyze(start_page, end_page)
    _print_report(report)

    if log:
        cost = _estimate_cost(report)
        entry = log_run(report, run_type="analyze", cost_estimate_usd=cost, notes=notes)
        console.print(f"[dim]Run logged. Estimated re-ingest cost: ${cost:.4f}[/dim]")

        delta = compare_last_two((start_page, end_page))
        if delta:
            console.print()
            _print_comparison(delta)


@cli.command()
@click.option("--start-page", default=None, type=int, help="Filter by start page")
@click.option("--end-page", default=None, type=int, help="Filter by end page")
@click.option("--last", default=10, type=int, help="Show last N entries")
def history(start_page: int | None, end_page: int | None, last: int):
    """Show QA run history."""
    page_range = (start_page, end_page) if start_page and end_page else None
    entries = get_history(page_range)

    if not entries:
        console.print("[yellow]No QA history found.[/yellow]")
        return

    entries = entries[-last:]

    table = Table(title="QA Run History", show_header=True)
    table.add_column("Time", style="dim")
    table.add_column("Type")
    table.add_column("Pages")
    table.add_column("Nodes", justify="right")
    table.add_column("Trees", justify="right")
    table.add_column("Max Depth", justify="right")
    table.add_column("Errors", justify="right", style="red")
    table.add_column("Warnings", justify="right", style="yellow")
    table.add_column("Cost", justify="right")
    table.add_column("Notes", max_width=30)

    for e in entries:
        s = e["summary"]
        table.add_row(
            e["timestamp"][:19].replace("T", " "),
            e["run_type"],
            f"{e['page_range'][0]}-{e['page_range'][1]}",
            str(s.get("total_nodes", 0)),
            str(s.get("tree_count", 0)),
            str(s.get("max_depth", 0)),
            str(e["issue_counts"]["errors"]),
            str(e["issue_counts"]["warnings"]),
            f"${e.get('cost_estimate_usd', 0) or 0:.4f}",
            e.get("notes", "")[:30],
        )

    console.print(table)


@cli.command()
@click.option("--start-page", required=True, type=int, help="First page of range")
@click.option("--end-page", required=True, type=int, help="Last page of range")
def compare(start_page: int, end_page: int):
    """Compare the last two QA runs for a page range."""
    delta = compare_last_two((start_page, end_page))
    if not delta:
        console.print("[yellow]Need at least 2 runs for this page range to compare.[/yellow]")
        return
    _print_comparison(delta)


@cli.command()
@click.option("--start-page", required=True, type=int, help="First page of range")
@click.option("--end-page", required=True, type=int, help="Last page of range")
@click.option("--dry-run", is_flag=True, help="Show what would be fixed without making changes")
@click.option("--merge-dupes/--no-merge-dupes", default=True, help="Merge duplicate nodes")
@click.option("--delete-orphans/--no-delete-orphans", default=False, help="Delete orphan nodes")
@click.option("--delete-null-titles/--no-delete-null-titles", default=True, help="Delete nodes with NULL titles")
@click.option("--notes", default="", help="Optional notes for this run")
def fix(start_page: int, end_page: int, dry_run: bool,
        merge_dupes: bool, delete_orphans: bool, delete_null_titles: bool, notes: str):
    """Apply automated fixes to the graph for a page range.

    Runs analysis before and after to show improvement.
    """
    prefix = "[DRY RUN] " if dry_run else ""

    # Pre-fix analysis
    console.print(f"[dim]{prefix}Running pre-fix analysis...[/dim]")
    pre_report = analyze(start_page, end_page)
    chunk_hashes = pre_report.chunk_hashes

    if not chunk_hashes:
        console.print("[red]No chunks found for this page range.[/red]")
        return

    console.print(f"[dim]Pre-fix: {pre_report.error_count} errors, {pre_report.warning_count} warnings[/dim]\n")

    fixes_applied = []

    # Fix 1: Merge duplicates (only within the same diagnostic context)
    if merge_dupes:
        console.print(f"[bold]{prefix}Merging duplicate nodes (same title + same diagnostic tree)...[/bold]")
        actions = merge_duplicate_nodes_native(chunk_hashes, dry_run=dry_run)
        merged_count = 0
        if actions:
            for a in actions:
                console.print(f"  [dim]{a['label']}: \"{a['title']}\" — merged {a['merged_count']} (context: {a.get('shared_problems', [])})[/dim]")
                merged_count += a["merged_count"]
            if merged_count:
                fixes_applied.append(f"merged {merged_count} context-duplicate nodes")
        else:
            console.print("  [dim]No duplicates found.[/dim]")
        console.print()

    # Fix 2: Delete NULL title nodes (Tool, Part only by default)
    if delete_null_titles:
        console.print(f"[bold]{prefix}Deleting NULL-title Tool/Part nodes...[/bold]")
        deleted = delete_null_title_nodes(chunk_hashes, labels=["Tool", "Part"], dry_run=dry_run)
        if deleted:
            for label, cnt in deleted.items():
                console.print(f"  [dim]{label}: {cnt} deleted[/dim]")
            total = sum(deleted.values())
            fixes_applied.append(f"deleted {total} null-title nodes")
        else:
            console.print("  [dim]None found.[/dim]")
        console.print()

    # Fix 3: Delete orphans
    if delete_orphans:
        console.print(f"[bold]{prefix}Deleting orphan nodes...[/bold]")
        deleted = delete_orphan_nodes(chunk_hashes, dry_run=dry_run)
        if deleted:
            for label, cnt in deleted.items():
                console.print(f"  [dim]{label}: {cnt} deleted[/dim]")
            total = sum(deleted.values())
            fixes_applied.append(f"deleted {total} orphan nodes")
        else:
            console.print("  [dim]No orphans found.[/dim]")
        console.print()

    if dry_run:
        console.print("[yellow]Dry run — no changes made. Remove --dry-run to apply fixes.[/yellow]")
        return

    # Post-fix analysis
    console.print(f"[dim]Running post-fix analysis...[/dim]")
    post_report = analyze(start_page, end_page)
    _print_report(post_report)

    # Log the fix run
    cost = _estimate_cost(post_report)
    log_run(post_report, run_type="fix", cost_estimate_usd=0, fixes_applied=fixes_applied,
            notes=notes or f"Auto-fix: {', '.join(fixes_applied)}")

    # Show comparison
    delta = compare_last_two((start_page, end_page))
    if delta:
        console.print()
        _print_comparison(delta)


@cli.command()
@click.option("--json-output", is_flag=True, help="Output as JSON for programmatic use")
def audit(json_output: bool):
    """Discover all ingested ranges and report quality status for each.

    Designed for Claude Code to call, parse output, and decide what to fix.
    """
    import json as json_mod

    ranges = discover_ingested_ranges(bucket_size=100)
    if not ranges:
        if json_output:
            console.print(json_mod.dumps({"ranges": [], "status": "no_data"}))
        else:
            console.print("[yellow]No ingested chunks found.[/yellow]")
        return

    results = []
    for start, end, chunk_count in ranges:
        report = analyze(start, end)
        entry = {
            "start_page": start,
            "end_page": end,
            "chunks": chunk_count,
            "nodes": report.total_nodes,
            "relationships": report.total_relationships,
            "trees": len(report.trees),
            "avg_depth": report.summary.get("avg_depth", 0),
            "max_depth": report.summary.get("max_depth", 0),
            "errors": report.error_count,
            "warnings": report.warning_count,
            "issues": [
                {"check": i.check, "severity": i.severity,
                 "message": i.message, "node_count": len(i.node_ids)}
                for i in report.issues
            ],
            "proposed_fixes": _propose_fixes(report),
        }
        results.append(entry)
        log_run(report, run_type="audit", cost_estimate_usd=_estimate_cost(report))

    if json_output:
        console.print(json_mod.dumps({"ranges": results}, indent=2))
    else:
        # Human-readable summary
        table = Table(title="QA Audit — All Ingested Ranges", show_header=True)
        table.add_column("Pages", style="bold")
        table.add_column("Chunks", justify="right")
        table.add_column("Nodes", justify="right")
        table.add_column("Trees", justify="right")
        table.add_column("Depth", justify="right")
        table.add_column("Errors", justify="right", style="red")
        table.add_column("Warnings", justify="right", style="yellow")
        table.add_column("Proposed Fixes")

        for r in results:
            depth_style = "green" if r["max_depth"] >= 5 else "yellow" if r["max_depth"] >= 3 else "red"
            fixes_str = ", ".join(r["proposed_fixes"]) if r["proposed_fixes"] else "[green]clean[/green]"
            table.add_row(
                f"{r['start_page']}-{r['end_page']}",
                str(r["chunks"]),
                str(r["nodes"]),
                str(r["trees"]),
                f"[{depth_style}]{r['max_depth']}[/{depth_style}]",
                str(r["errors"]),
                str(r["warnings"]),
                fixes_str,
            )
        console.print(table)


def _propose_fixes(report: QAReport) -> list[str]:
    """Generate a list of proposed fix actions based on the report."""
    fixes = []
    for issue in report.issues:
        if issue.check == "duplicate_nodes":
            fixes.append(f"merge {issue.message}")
        elif issue.check == "duplicate_dtc":
            fixes.append(f"merge {issue.message}")
        elif issue.check == "null_title" and "Tool" in issue.message:
            fixes.append(f"delete {issue.message}")
        elif issue.check == "null_title" and "Part" in issue.message:
            fixes.append(f"delete {issue.message}")
    # Deduplicate
    return list(dict.fromkeys(fixes))


# Alias so `python -m backend.cli.qa analyze` works
analyze_cmd.name = "analyze"


if __name__ == "__main__":
    cli()
