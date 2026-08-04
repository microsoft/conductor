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

    # Semantic validation: cross-field references, template refs, etc.
    try:
        from conductor.config.validator import validate_workflow_config

        warnings = validate_workflow_config(config, workflow_path=workflow_path)
        if warnings:
            for warning in warnings:
                output_console.print(f"  [yellow]⚠[/yellow] {warning}")
    except ConductorError as e:
        display_validation_error(e, workflow_path, output_console)
        return False, None

    _report_skill_discovery(config, workflow_path, output_console)

    return True, config


def _report_skill_discovery(
    config: WorkflowConfig,
    workflow_path: Path,
    console: Console,
) -> None:
    """Print what ``runtime.skill_discovery`` finds on this machine.

    Discovery's one real cost is that the same YAML picks up a different
    skill set on a different machine or in CI. That is only defensible if
    the author can see the set, so listing it is part of the feature
    rather than a debugging aid.

    Silent when discovery is off, and never fatal — a broken ambient
    location has already been reported as a warning by the validator, and
    failing the command here would turn a reporting step into a second
    source of validation errors.

    Args:
        config: The validated workflow configuration.
        workflow_path: Path to the workflow file, anchoring ``project``.
        console: Rich console for output.
    """
    discovery = config.workflow.runtime.skill_discovery
    if not discovery.is_enabled:
        return

    from conductor.skills import BYTES_PER_TOKEN_ESTIMATE, discover_skills, load_skill_content

    # Diagnostics are dropped rather than reported: this summary re-runs a
    # scan the validator has already run and printed warnings for, so
    # forwarding them would duplicate every line. Anything genuinely wrong
    # is visible in the listing below as a missing skill.
    try:
        found = discover_skills(
            discovery.sources,
            base_dir=workflow_path.resolve().parent,
            exclude=discovery.exclude,
            on_warning=lambda _message: None,
        )
    except Exception as exc:  # pragma: no cover - defensive; discovery warns instead
        console.print(f"  [yellow]⚠[/yellow] Skill discovery could not be summarized: {exc}")
        return

    sources = ", ".join(discovery.sources)
    if not found:
        console.print(f"  [dim]Skill discovery ({sources}): no skills found[/dim]")
        return

    console.print(f"  [dim]Skill discovery ({sources}): {len(found)} skill(s)[/dim]")
    for skill in found:
        console.print(f"    [dim]• {skill.name} — {skill.root}[/dim]")

    try:
        content = load_skill_content([(skill.name, skill.directory) for skill in found])
    except Exception:
        # Unreadable content is the validator's business, not this summary's.
        return
    size = len(content.encode("utf-8"))
    console.print(
        f"    [dim]Total if eagerly injected: {size:,} bytes "
        f"(~{size // BYTES_PER_TOKEN_ESTIMATE:,} tokens)[/dim]"
    )


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
    parallel_group_count = len(config.parallel)
    for_each_group_count = len(config.for_each)

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
    if parallel_group_count > 0:
        table.add_row("Parallel Groups", str(parallel_group_count))
    if for_each_group_count > 0:
        table.add_row("For-each Groups", str(for_each_group_count))
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
