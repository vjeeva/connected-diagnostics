"""CLI diagnostic chat interface."""

from __future__ import annotations

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from backend.app.services.diagnostic_engine import (
    SessionState,
    continue_session,
    start_session,
)
from backend.app.services.estimate_service import format_estimate, generate_estimate

console = Console()


def print_welcome():
    console.print(Panel(
        "[bold]Connected Diagnostics[/bold]\n\n"
        "Describe your car problem and I'll help diagnose it.\n"
        "Type [bold]quit[/bold] to exit.",
        title="Diagnostic Assistant",
        border_style="blue",
    ))
    console.print()


def print_assistant(message: str):
    console.print()
    console.print(Panel(message, border_style="green", title="Assistant"))
    console.print()


class StreamPrinter:
    """Streams tokens to the terminal. Updates spinner text on status changes,
    stops the spinner on first token."""

    def __init__(self, status=None):
        self._parts: list[str] = []
        self._started = False
        self._status = status

    def on_status(self, msg: str):
        if self._status and not self._started:
            self._status.update(f"[bold green]{msg}")

    def on_token(self, token: str):
        if not self._started:
            if self._status:
                self._status.stop()
            console.print()
            console.print("[green]Assistant:[/green] ", end="")
            self._started = True
        console.print(token, end="", highlight=False)
        self._parts.append(token)

    def finalize(self) -> str:
        if self._started:
            console.print()  # newline after stream
            console.print()
        return "".join(self._parts)


def print_estimate(estimate_text: str):
    console.print()
    console.print(Panel(estimate_text, border_style="yellow", title="Repair Estimate"))
    console.print()


def print_path(state: SessionState):
    """Show the diagnostic path taken so far."""
    if not state.steps:
        return
    path_parts = []
    for step in state.steps:
        path_parts.append(f"{step['node_type']}")
    path_str = " -> ".join(path_parts)
    console.print(f"[dim]Path: {path_str}[/dim]")


@click.command()
@click.option("--vehicle", default=None, help="Vehicle info (e.g. '2017 Lexus GX460')")
def chat_cli(vehicle: str | None):
    """Start a diagnostic chat session."""
    print_welcome()

    state: SessionState | None = None

    while True:
        try:
            if state is None:
                prompt = "Describe your problem"
                if vehicle:
                    prompt += f" with your {vehicle}"
                user_input = console.input(f"[bold blue]{prompt}:[/bold blue] ").strip()
            else:
                user_input = console.input("[bold blue]You:[/bold blue] ").strip()

            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                console.print("\n[dim]Session ended.[/dim]")
                break

            if state is None:
                # Prepend vehicle info if provided
                full_input = f"{vehicle}: {user_input}" if vehicle else user_input

                status = console.status("[bold green]Checking knowledge base...")
                status.start()
                sp = StreamPrinter(status)
                state, response = start_session(full_input, on_token=sp.on_token, on_status=sp.on_status)
                status.stop()
                sp.finalize()

                print_path(state)
            else:
                status = console.status("[bold green]Checking knowledge base...")
                status.start()
                sp = StreamPrinter(status)
                state, response = continue_session(state, user_input, on_token=sp.on_token, on_status=sp.on_status)
                status.stop()
                sp.finalize()

                if state.phase == "estimate":

                    # Generate and display estimate
                    with console.status("[bold yellow]Generating estimate..."):
                        estimate = generate_estimate(state.current_node_id)
                        if "error" not in estimate:
                            estimate_text = format_estimate(estimate)
                        else:
                            estimate_text = (
                                "Could not generate a detailed estimate — "
                                "insufficient data in the knowledge graph.\n\n"
                                f"Solution: {state.steps[-1].get('neo4j_node_id', 'Unknown')}"
                            )

                    print_estimate(estimate_text)
                    print_path(state)

                    console.print("[dim]Diagnosis complete. Type 'quit' to exit "
                                  "or describe another problem to start a new session.[/dim]")
                    state = None  # Reset for a new session
                else:
                    # Response already streamed to terminal
                    print_path(state)

        except KeyboardInterrupt:
            console.print("\n[dim]Session interrupted.[/dim]")
            break
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/red]\n")


if __name__ == "__main__":
    chat_cli()
