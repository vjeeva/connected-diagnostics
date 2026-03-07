"""CLI for running diagnostic chat evaluations.

Usage:
    python -m backend.cli.eval run                        # run all tests
    python -m backend.cli.eval run --case p2714_work_order_parts  # run one test
    python -m backend.cli.eval run --tag work_order       # run by tag
    python -m backend.cli.eval history                    # show past runs
    python -m backend.cli.eval compare                    # diff last two runs
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from backend.app.eval.runner import RESULTS_PATH, load_cases, run_all, save_results

console = Console()

HISTORY_PATH = Path("eval_history.json")


def _load_history() -> list[dict]:
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH) as f:
            return json.load(f)
    return []


def _save_history(history: list[dict]):
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2, default=str)


@click.group()
def cli():
    """Diagnostic chat evaluation framework."""
    pass


@cli.command()
@click.option("--case", default=None, help="Run only this test case ID")
@click.option("--tag", default=None, help="Run only cases with this tag")
@click.option("--verbose", "-v", is_flag=True, help="Show full LLM output for failures")
def run(case: str | None, tag: str | None, verbose: bool):
    """Run eval test cases and report results."""
    cases = load_cases(case_id=case, tag=tag)
    if not cases:
        console.print("[red]No matching test cases found.[/red]")
        return

    # Count unique conversations (cases sharing turns)
    unique_turns = set()
    for c in cases:
        unique_turns.add(json.dumps(c["turns"]))
    console.print(f"\n[bold]Running {len(cases)} test cases ({len(unique_turns)} unique conversation(s))...[/bold]\n")

    t0 = time.time()
    results = run_all(case_id=case, tag=tag)
    elapsed = time.time() - t0

    # Display results
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    errors = sum(1 for r in results if r.status == "ERROR")

    table = Table(title="Eval Results")
    table.add_column("Status", style="bold", width=6)
    table.add_column("Test ID", style="cyan")
    table.add_column("Name")
    table.add_column("Checks", justify="right")
    table.add_column("Time", justify="right")

    for r in results:
        check_passed = sum(1 for c in r.checks if c["passed"])
        check_total = len(r.checks)
        status_style = {"PASS": "green", "FAIL": "red", "ERROR": "yellow"}[r.status]
        table.add_row(
            f"[{status_style}]{r.status}[/{status_style}]",
            r.test_id,
            r.name,
            f"{check_passed}/{check_total}",
            f"{r.duration_s:.1f}s",
        )

    console.print(table)
    console.print(f"\n[bold]{passed} passed, {failed} failed, {errors} errors[/bold] in {elapsed:.1f}s\n")

    # Show failure details
    for r in results:
        if r.status == "ERROR":
            console.print(f"[yellow]ERROR {r.test_id}:[/yellow] {r.error}")
            continue
        if r.status != "FAIL":
            continue
        console.print(f"[red]FAIL {r.test_id}:[/red] {r.name}")
        for c in r.checks:
            if c["passed"]:
                continue
            failure_type = c.get("failure_type", "code")
            type_badge = f"[yellow][{failure_type}][/yellow]" if failure_type == "data" else f"[red][{failure_type}][/red]"
            console.print(f"  {type_badge} {c['message']}")
            if c.get("evidence"):
                console.print(f"    [dim]Evidence: {c['evidence'][:200]}[/dim]")
            if c.get("fix_hint"):
                console.print(f"    [dim]Fix: {c['fix_hint']}[/dim]")
        if verbose and r.final_output:
            console.print(f"\n  [dim]--- Full output ({len(r.final_output)} chars) ---[/dim]")
            for line in r.final_output.split("\n")[:80]:
                console.print(f"  [dim]{line}[/dim]")
            if len(r.final_output.split("\n")) > 80:
                console.print(f"  [dim]... ({len(r.final_output.split(chr(10))) - 80} more lines)[/dim]")
        console.print()

    # Save results
    out_path = save_results(results)
    console.print(f"[dim]Results saved to {out_path}[/dim]")

    # Save to history
    history = _load_history()
    history.append({
        "timestamp": datetime.now().isoformat(),
        "cases_run": len(results),
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "duration_s": round(elapsed, 1),
        "filter": {"case": case, "tag": tag},
        "failures": [
            {"test_id": r.test_id, "messages": [c["message"] for c in r.checks if not c["passed"]]}
            for r in results if r.status == "FAIL"
        ],
    })
    _save_history(history)

    # Exit code for CI / eval-fix loop
    if failed > 0 or errors > 0:
        raise SystemExit(1)


@cli.command()
def history():
    """Show eval run history."""
    hist = _load_history()
    if not hist:
        console.print("[yellow]No eval history yet. Run: python -m backend.cli.eval run[/yellow]")
        return

    table = Table(title="Eval History")
    table.add_column("When")
    table.add_column("Cases", justify="right")
    table.add_column("Passed", justify="right", style="green")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("Errors", justify="right", style="yellow")
    table.add_column("Duration", justify="right")

    for entry in hist[-20:]:
        table.add_row(
            entry["timestamp"][:19],
            str(entry["cases_run"]),
            str(entry["passed"]),
            str(entry["failed"]),
            str(entry["errors"]),
            f"{entry['duration_s']}s",
        )

    console.print(table)


@cli.command()
def compare():
    """Compare the last two eval runs."""
    hist = _load_history()
    if len(hist) < 2:
        console.print("[yellow]Need at least 2 runs to compare.[/yellow]")
        return

    prev, curr = hist[-2], hist[-1]
    console.print(f"\n[bold]Comparing:[/bold]")
    console.print(f"  Previous: {prev['timestamp'][:19]} — {prev['passed']}/{prev['cases_run']} passed")
    console.print(f"  Current:  {curr['timestamp'][:19]} — {curr['passed']}/{curr['cases_run']} passed")
    console.print()

    # Find newly fixed and newly broken tests
    prev_failures = {f["test_id"] for f in prev.get("failures", [])}
    curr_failures = {f["test_id"] for f in curr.get("failures", [])}

    fixed = prev_failures - curr_failures
    broken = curr_failures - prev_failures
    still_failing = prev_failures & curr_failures

    if fixed:
        console.print("[green]Fixed:[/green]")
        for tid in sorted(fixed):
            console.print(f"  [green]+[/green] {tid}")
    if broken:
        console.print("[red]New failures:[/red]")
        for tid in sorted(broken):
            console.print(f"  [red]-[/red] {tid}")
    if still_failing:
        console.print("[yellow]Still failing:[/yellow]")
        for tid in sorted(still_failing):
            console.print(f"  [yellow]~[/yellow] {tid}")
    if not fixed and not broken:
        delta = curr["passed"] - prev["passed"]
        console.print(f"No changes. Pass count delta: {delta:+d}")
    console.print()


if __name__ == "__main__":
    cli()
