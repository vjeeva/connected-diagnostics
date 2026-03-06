"""CLI for the technician contribution system."""

from __future__ import annotations

import json

import click
from rich.console import Console
from rich.table import Table

from backend.app.services.contribution_service import (
    invite_technician,
    submit_contribution,
    review_contribution,
    get_pending_reviews,
    get_annotations,
    get_user,
)

console = Console()


@click.group()
def contribute():
    """Technician contribution system."""


@contribute.command()
@click.option("--email", required=True)
@click.option("--name", required=True, help="Display name")
def invite(email: str, name: str):
    """Invite a technician (bootstrap mode — immediate Trusted status)."""
    user_id = invite_technician(email, name)
    console.print(f"[green]Invited {name} ({email})[/green]")
    console.print(f"  User ID: {user_id}")
    console.print(f"  Trust: trusted (invited)")


@contribute.command()
@click.option("--user-id", required=True, help="Your user ID")
@click.option("--node-id", required=True, help="Target node ID in the graph")
@click.option("--text", "annotation_text", required=True, help="Annotation text")
def annotate(user_id: str, node_id: str, annotation_text: str):
    """Add an annotation to a graph node (tip, correction, location info)."""
    result = submit_contribution(
        user_id=user_id,
        contribution_type="annotation",
        target_node_id=node_id,
        content={"text": annotation_text},
    )
    console.print(f"[green]{result['message']}[/green]")


@contribute.command()
@click.option("--user-id", required=True)
@click.option("--node-id", required=True, help="The existing node to offer an alternative to")
@click.option("--title", required=True, help="Title of the alternative test/step")
@click.option("--instruction", required=True, help="How to perform it")
@click.option("--node-type", default="Test", help="Node type (Test, Step)")
@click.option("--expected", default=None, help="Expected result")
@click.option("--tool", default=None, help="Tool required")
def alternative(user_id: str, node_id: str, title: str, instruction: str,
                node_type: str, expected: str | None, tool: str | None):
    """Add an alternative test or step alongside an existing one."""
    content = {
        "node_type": node_type,
        "title": title,
        "instruction": instruction,
    }
    if expected:
        content["expected_result"] = expected
    if tool:
        content["tool_required"] = tool

    result = submit_contribution(
        user_id=user_id,
        contribution_type="alternative",
        target_node_id=node_id,
        content=content,
    )
    console.print(f"[green]{result['message']}[/green]")
    if result.get("neo4j_node_id"):
        console.print(f"  New node ID: {result['neo4j_node_id']}")


@contribute.command()
@click.option("--node-id", required=True, help="Node ID to view annotations for")
def show_annotations(node_id: str):
    """Show all annotations on a node."""
    annotations = get_annotations(node_id)
    if not annotations:
        console.print("[dim]No annotations on this node.[/dim]")
        return

    for a in annotations:
        console.print(f"\n  [bold]{a.get('author', '?')}[/bold] ({a.get('created_at', '?')[:10]})")
        console.print(f"  {a.get('text', '')}")


@contribute.command()
def pending():
    """Show contributions pending review."""
    reviews = get_pending_reviews()
    if not reviews:
        console.print("[dim]No contributions pending review.[/dim]")
        return

    table = Table(title="Pending Contributions")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Type")
    table.add_column("Contributor")
    table.add_column("Target Node")
    table.add_column("Created")

    for r in reviews:
        table.add_row(
            str(r["id"])[:8],
            r["contribution_type"],
            r["contributor"],
            r.get("target_neo4j_node_id", "")[:20] or "—",
            str(r["created_at"])[:10],
        )

    console.print(table)


@contribute.command()
@click.option("--reviewer-id", required=True, help="Your user ID (must be trusted+)")
@click.option("--contribution-id", required=True, help="Contribution ID to review")
@click.option("--action", required=True, type=click.Choice(["approve", "reject", "flag"]))
@click.option("--notes", default=None, help="Review notes")
def review(reviewer_id: str, contribution_id: str, action: str, notes: str | None):
    """Review a pending contribution."""
    result = review_contribution(contribution_id, reviewer_id, action, notes)
    console.print(f"[green]{result['message']}[/green]")


if __name__ == "__main__":
    contribute()
