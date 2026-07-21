"""Typer subcommand group for workflow checkpoint management."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

console = Console(stderr=True)
output_console = Console()

checkpoint_app = typer.Typer(
    name="checkpoint",
    help="Inspect workflow checkpoints.",
    no_args_is_help=True,
)


@checkpoint_app.command("list")
def list_checkpoints(
    workflow: Annotated[
        Path | None,
        typer.Argument(
            help="Path to a workflow YAML file. Filters checkpoints to this workflow only.",
        ),
    ] = None,
) -> None:
    """List available workflow checkpoints.

    Shows each checkpoint's workflow name, timestamp, trigger (failure or
    periodic), the agent that was running or about to run, the error type
    (failure checkpoints only), and file path. Optionally filter by
    workflow file.

    \b
    Examples:
        conductor checkpoint list
        conductor checkpoint list workflow.yaml
    """
    _list_checkpoints_impl(workflow)


def _list_checkpoints_impl(workflow: Path | None) -> None:
    """Render the checkpoints table.

    Shared implementation behind ``conductor checkpoint list`` and the
    deprecated ``conductor checkpoints`` alias, so both stay in lockstep.
    """
    from conductor.engine.checkpoint import CheckpointManager

    # Resolve workflow path for filtering
    resolved_workflow: Path | None = None
    if workflow is not None:
        resolved_workflow = workflow.resolve()
        if not resolved_workflow.exists():
            console.print(f"[bold red]Error:[/bold red] Workflow file not found: {workflow}")
            raise typer.Exit(code=1)

    checkpoint_list = CheckpointManager.list_checkpoints(resolved_workflow)

    if not checkpoint_list:
        if resolved_workflow:
            output_console.print(
                f"[dim]No checkpoints found for workflow: {resolved_workflow.name}[/dim]"
            )
        else:
            output_console.print("[dim]No checkpoints found.[/dim]")
        return

    table = Table(title="Workflow Checkpoints", show_lines=True)
    table.add_column("Workflow", style="cyan")
    table.add_column("Timestamp", style="green")
    table.add_column("Trigger", style="magenta")
    table.add_column("Agent", style="yellow")
    table.add_column("Error Type", style="red", no_wrap=True, min_width=13)
    # File path absorbs truncation so the triage columns stay readable.
    table.add_column("File", style="dim", overflow="ellipsis")

    for cp in checkpoint_list:
        workflow_name = Path(cp.workflow_path).stem
        timestamp = cp.created_at
        trigger = cp.trigger
        # For failure checkpoints this is the failed agent; for periodic
        # checkpoints it is the step that was about to run.
        agent = cp.failure.get("agent") or "unknown"
        # Periodic checkpoints have no error; show an em dash.
        error_type = cp.failure.get("error_type") or "—"
        file_path = str(cp.file_path)

        table.add_row(workflow_name, timestamp, trigger, agent, error_type, file_path)

    output_console.print(table)
    output_console.print(f"\n[dim]Total: {len(checkpoint_list)} checkpoint(s)[/dim]")
