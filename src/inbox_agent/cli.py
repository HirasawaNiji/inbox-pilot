"""Command-line interface for InboxPilot."""

import typer
from rich.console import Console

app = typer.Typer(
    name="inbox-agent",
    help="An explainable email priority agent.",
)
console = Console()


@app.callback()
def main() -> None:
    """Run InboxPilot commands."""


@app.command()
def demo() -> None:
    """Run the offline demonstration."""
    console.print("[green]InboxPilot is ready.[/green]")


if __name__ == "__main__":
    app()
