"""Implementation of the 'conductor validate' command.

This module provides functionality to validate workflow YAML files
without executing them, displaying detailed error information.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from conductor.config.loader import load_config
from conductor.exceptions import ConductorError

if TYPE_CHECKING:
    from conductor.config.schema import WorkflowConfig


def validate_workflow(
    workflow_path: Path,
    console: Console | None = None,
) -> tuple[bool, WorkflowConfig | None]:
    """Validate a workflow YAML file.

    Attempts to load and validate the workflow configuration,
    reporting any errors encountered during the process.

    Args:
        workflow_path: Path to the workflow YAML file.
        console: Optional Rich console for output.

    Returns:
        A tuple of (is_valid, config_or_none).
    """
    output_console = console if console is not None else Console()

    try:
        config = load_config(workflow_path)
        return True, config
    except ConductorError as e:
        # Display structured error
        display_validation_error(e, workflow_path, output_console)
        return False, None
    except Exception as e:
        # Unexpected error
        output_console.print(
            Panel(
                f"[bold red]Unexpected Error[/bold red]\n\n{e}",
                title="[red]Validation Failed[/red]",
                border_style="red",
            )
        )
        return False, None


def display_validation_error(
    error: ConductorError,
    workflow_path: Path,
    console: Console,
) -> None:
    """Display a validation error with Rich formatting.

    Args:
        error: The ConductorError that occurred.
        workflow_path: Path to the workflow file.
        console: Rich console for output.
    """
    error_type = type(error).__name__
    error_msg = str(error.__cause__) if error.__cause__ else str(error)

    # Remove the suggestion from the main message (it's added in __str__)
    if error.suggestion:
        error_msg = error_msg.replace(f"\n\n💡 Suggestion: {error.suggestion}", "")

    content = f"[bold red]{error_type}[/bold red]\n\n"
    content += f"[dim]File:[/dim] {workflow_path}\n\n"
    content += f"{error_msg}"

    if error.suggestion:
        content += f"\n\n[yellow]💡 Suggestion:[/yellow] {error.suggestion}"

    console.print(
        Panel(
            content,
            title="[red]Validation Failed[/red]",
            border_style="red",
        )
    )


def display_validation_success(
    config: WorkflowConfig,
    workflow_path: Path,
    console: Console,
) -> None:
    """Display validation success with workflow summary.

    Args:
        config: The validated workflow configuration.
        workflow_path: Path to the workflow file.
        console: Rich console for output.
    """
    # Build summary info
    agent_count = len(config.agents)
    human_gate_count = sum(1 for a in config.agents if a.type == "human_gate")

    # Count conditional routes
    conditional_route_count = sum(1 for a in config.agents for r in a.routes if r.when)

    # Determine workflow patterns
    patterns = []
    if conditional_route_count > 0:
        patterns.append("conditional routing")

    # Check for loop-back patterns (agent routes to earlier agent)
    agent_names = [a.name for a in config.agents]
    has_loop = False
    for i, agent in enumerate(config.agents):
        for route in agent.routes:
            if route.to in agent_names:
                target_idx = agent_names.index(route.to)
                if target_idx <= i:
                    has_loop = True
                    break
        if has_loop:
            break

    if has_loop:
        patterns.append("loop-back")

    if human_gate_count > 0:
        patterns.append("human gates")

    if config.tools:
        patterns.append("tools")

    # Workflow info table
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value")

    table.add_row("Name", config.workflow.name)
    if config.workflow.description:
        table.add_row("Description", config.workflow.description)
    table.add_row("Entry Point", config.workflow.entry_point)
    table.add_row("Agents", str(agent_count))
    if human_gate_count > 0:
        table.add_row("Human Gates", str(human_gate_count))
    table.add_row("Max Iterations", str(config.workflow.limits.max_iterations))
    timeout_val = config.workflow.limits.timeout_seconds
    table.add_row("Timeout", f"{timeout_val}s" if timeout_val else "unlimited")
    if patterns:
        table.add_row("Patterns", ", ".join(patterns))

    console.print(
        Panel(
            table,
            title="[green]Validation Successful[/green]",
            border_style="green",
        )
    )

    # Show agent summary
    if agent_count > 0:
        agent_table = Table(title="Agents", show_lines=True)
        agent_table.add_column("Name", style="cyan")
        agent_table.add_column("Type", width=12)
        agent_table.add_column("Model", width=20)
        agent_table.add_column("Routes")

        for agent in config.agents:
            agent_type = agent.type or "agent"
            model = agent.model or config.workflow.runtime.default_model or "[dim]default[/dim]"

            if agent.routes:
                route_targets = [r.to for r in agent.routes]
                routes_str = ", ".join(route_targets[:3])
                if len(route_targets) > 3:
                    routes_str += f" (+{len(route_targets) - 3} more)"
            else:
                routes_str = "[dim]none[/dim]"

            agent_table.add_row(agent.name, agent_type, model, routes_str)

        console.print(agent_table)
