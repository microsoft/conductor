"""Implementation of the 'conductor validate' command.

This module provides functionality to validate workflow YAML files
without executing them, displaying detailed error information.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from conductor.config.loader import load_config
from conductor.console import MarkupFreeConsole, make_console, styled
from conductor.exceptions import ConductorError

if TYPE_CHECKING:
    from conductor.config.schema import WorkflowConfig


def validate_workflow(
    workflow_path: Path,
    console: MarkupFreeConsole | None = None,
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
    output_console = console if console is not None else make_console()

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
                styled("[bold red]Unexpected Error[/bold red]\n\n{}", e),
                title=Text.from_markup("[red]Validation Failed[/red]"),
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
                output_console.print(styled("  [yellow]⚠[/yellow] {}", warning))
    except ConductorError as e:
        display_validation_error(e, workflow_path, output_console)
        return False, None

    _report_skill_discovery(config, workflow_path, output_console, already_reported=warnings)
    _report_plugins(config, workflow_path, output_console)

    return True, config


def _report_plugins(
    config: WorkflowConfig,
    workflow_path: Path,
    console: MarkupFreeConsole,
) -> None:
    """Print what each enabled plugin actually contributes.

    A plugin name in the YAML says nothing about how much it brings — the
    installed plugins on one machine range from a single skill to seven
    subagents plus an MCP server that authenticates with the user's own
    credentials. Printing the component counts is what turns "I enabled
    ``prs``" into something the author can review, and makes a change in
    what a plugin ships visible on the next validate rather than at run
    time.

    Reports the workflow-level set only. Per-agent ``plugins:`` overrides
    are resolved and *checked* by the validator, which knows each agent's
    provider; repeating them here without that context would list
    components a given agent never receives.

    Never fatal, but not because the failure was already reported: the
    validator resolves plugins per agent, so a workflow whose agents all
    override ``plugins:`` (including with ``[]``) never resolves the
    workflow-level list at all, and a failure surfaces here first. A
    summary is still the wrong place to fail a workflow that is otherwise
    valid — nothing inherits the list — so it degrades to a warning.

    Args:
        config: The validated workflow configuration.
        workflow_path: Path to the workflow file, anchoring relative
            plugin paths.
        console: Rich console for output.
    """
    entries = config.workflow.runtime.plugins
    if not entries:
        return

    from conductor.plugins.errors import PluginError, PluginFetchError
    from conductor.plugins.registry import resolve_plugins
    from conductor.plugins.resolution import marketplaces_from, resolve_plugin_sources
    from conductor.providers.capabilities import plugin_flavor_for
    from conductor.skills import SkillError

    base_dir = workflow_path.resolve().parent
    declared = config.workflow.runtime.plugin_sources
    sources: dict[str, Any] = {}
    # The workflow-level default provider's flavor, since this reports the
    # workflow-level ``runtime.plugins`` list only (see the docstring) —
    # there is no single per-agent flavor to prefer here.
    flavor = plugin_flavor_for(config.workflow.runtime.provider.name)
    if declared:
        # Cache-only, like everything else in ``conductor validate``: this
        # is a summary, and a summary must not clone. Resolved one at a
        # time so a single unfetched source degrades to one line rather
        # than discarding the summary for every healthy source beside it.
        for name, entry in declared.items():
            try:
                sources.update(
                    resolve_plugin_sources({name: entry}, base_dir=base_dir, allow_network=False)
                )
            except PluginFetchError:
                # Merely unfetched. Not reported here — the plugin lines
                # below already name ``conductor plugin fetch`` for the
                # entries that need it, and the validator said it once.
                continue
            except (PluginError, SkillError, OSError) as exc:
                console.print(
                    styled("  [yellow]⚠[/yellow] Plugin source {!r} is unusable: {}", name, exc)
                )

    # Printed before resolution is attempted: what a source resolved to is
    # worth seeing even when an entry referencing a *different* source
    # cannot be resolved, and this listing is the only place the resolved
    # commit appears.
    if sources:
        console.print(styled("  [dim]Plugin sources: {} declared[/dim]", len(sources)))
        for name, entry in sources.items():
            detail = entry.source.describe()
            if entry.sha:
                detail = f"{detail} @ {entry.sha[:12]}"
            if entry.stale:
                detail = f"{detail} (cached; ref not re-checked)"
            console.print(styled("    [dim]• {} — {}[/dim]", name, detail))

    # Resolved one entry at a time, for the same reason the sources above
    # are: ``resolve_plugins`` is all-or-nothing, so one entry whose source
    # is unfetched would erase the component counts for every healthy
    # plugin beside it — the listing this function exists to print.
    resolved = []
    for entry in entries:
        try:
            resolved.extend(
                resolve_plugins(
                    [entry],
                    base_dir=base_dir,
                    marketplaces=marketplaces_from(sources),
                    declared_sources=set(declared) - set(sources),
                    flavor=flavor,
                )
            )
        except (PluginError, SkillError, OSError) as exc:
            # Narrow: a genuine bug in the plugin layer (AttributeError,
            # KeyError) should surface as a crash, not as a soft yellow line
            # the reader scrolls past. This arm is reachable through ordinary
            # configuration — see the docstring — so it is not merely
            # defensive.
            console.print(styled("  [yellow]⚠[/yellow] Plugin {!r}: {}", entry.name, exc))

    console.print(styled("  [dim]Plugins: {} enabled[/dim]", len(resolved)))
    for plugin in resolved:
        parts = [
            f"{len(plugin.skills)} skill(s)",
            f"{len(plugin.agents)} agent(s)",
            f"{len(plugin.mcp_servers)} MCP server(s)",
        ]
        console.print(
            styled("    [dim]• {} — {} — {}[/dim]", plugin.name, ", ".join(parts), plugin.root)
        )
        if plugin.agents:
            names = ", ".join(item.qualified_name for item in plugin.agents)
            console.print(styled("      [dim]agents: {}[/dim]", names))
        if plugin.mcp_servers:
            console.print(styled("      [dim]mcp: {}[/dim]", ", ".join(sorted(plugin.mcp_servers))))
        if plugin.disabled:
            console.print(
                styled("      [dim]disabled by this workflow: {}[/dim]", ", ".join(plugin.disabled))
            )


def _report_skill_discovery(
    config: WorkflowConfig,
    workflow_path: Path,
    console: MarkupFreeConsole,
    already_reported: list[str],
) -> None:
    """Print what ``runtime.skill_discovery`` puts in effect on this machine.

    Discovery's one real cost is that the same YAML picks up a different
    skill set on a different machine or in CI. That is only defensible if
    the author can see the set, so listing it is part of the feature
    rather than a debugging aid.

    Resolves rather than merely scanning, so the listing is the set an
    inheriting agent actually gets: a skill with broken frontmatter or a
    name already claimed in ``skills:`` is excluded here exactly as it is
    at run time. Skills an individual provider then declines to load
    (``claude-agent-sdk`` outside a plugin) are per-agent and reported as
    warnings by the validator instead.

    Diagnostics are forwarded unless the validator already printed them.
    They cannot simply be discarded: the validator only resolves skills
    for agents that *inherit* the workflow list, so a workflow whose
    agents all declare their own ``skills:`` never runs discovery there,
    and this becomes the only place a broken ambient location is ever
    mentioned.

    Never fatal — turning a summary into a second source of validation
    errors would be worse than an incomplete summary.

    Args:
        config: The validated workflow configuration.
        workflow_path: Path to the workflow file, anchoring ``project``.
        console: Rich console for output.
        already_reported: Warnings the validator has printed, so the same
            line is not shown twice.
    """
    discovery = config.workflow.runtime.skill_discovery
    if not discovery.is_enabled:
        return

    from conductor.skills import (
        BYTES_PER_TOKEN_ESTIMATE,
        load_skill_content,
        resolve_effective_skills,
    )

    seen = set(already_reported)

    def _forward(message: str) -> None:
        if message in seen:
            return
        seen.add(message)
        console.print(styled("  [yellow]⚠[/yellow] {}", message))

    try:
        resolved = resolve_effective_skills(
            list(config.workflow.runtime.skills),
            sources=discovery.sources,
            exclude=discovery.exclude,
            base_dir=workflow_path.resolve().parent,
            on_warning=_forward,
        )
    except Exception as exc:  # pragma: no cover - defensive; a report must not crash
        console.print(
            styled("  [yellow]⚠[/yellow] Skill discovery could not be summarized: {}", exc)
        )
        return

    found = [skill for skill in resolved if skill.discovered]
    sources = ", ".join(discovery.sources)
    if not found:
        console.print(styled("  [dim]Skill discovery ({}): no skills found[/dim]", sources))
        return

    console.print(styled("  [dim]Skill discovery ({}): {} skill(s)[/dim]", sources, len(found)))
    for skill in found:
        console.print(styled("    [dim]• {} — {}[/dim]", skill.name, skill.source))

    try:
        content = load_skill_content([(skill.name, skill.directory) for skill in resolved])
    except Exception as exc:
        console.print(styled("  [yellow]⚠[/yellow] Skill content could not be measured: {}", exc))
        return
    size = len(content.encode("utf-8"))
    # Covers declared skills too, since an eager-injection provider would
    # be sent the whole set — the budget it is compared against is total.
    console.print(
        styled(
            "    [dim]Total if eagerly injected: {:,} bytes (~{:,} tokens)[/dim]",
            size,
            size // BYTES_PER_TOKEN_ESTIMATE,
        )
    )


def display_validation_error(
    error: ConductorError,
    workflow_path: Path,
    console: MarkupFreeConsole,
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

    content = styled("[bold red]{}[/bold red]\n\n", error_type)
    content += styled("[dim]File:[/dim] {}\n\n", workflow_path)
    content += f"{error_msg}"

    if error.suggestion:
        content += styled("\n\n[yellow]💡 Suggestion:[/yellow] {}", error.suggestion)

    console.print(
        Panel(
            content,
            title=Text.from_markup("[red]Validation Failed[/red]"),
            border_style="red",
        )
    )


def display_validation_success(
    config: WorkflowConfig,
    workflow_path: Path,
    console: MarkupFreeConsole,
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
    questions_count = sum(1 for a in config.agents if a.type == "questions")
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

    if questions_count > 0:
        patterns.append("questions")

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
    if questions_count > 0:
        table.add_row("Questions", str(questions_count))
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
            title=Text.from_markup("[green]Validation Successful[/green]"),
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
            model = (
                agent.model
                or config.workflow.runtime.default_model
                or Text.from_markup("[dim]default[/dim]")
            )

            if agent.routes:
                route_targets = [r.to for r in agent.routes]
                routes_str = ", ".join(route_targets[:3])
                if len(route_targets) > 3:
                    routes_str += f" (+{len(route_targets) - 3} more)"
            else:
                routes_str = Text.from_markup("[dim]none[/dim]")

            agent_table.add_row(agent.name, agent_type, model, routes_str)

        console.print(agent_table)
