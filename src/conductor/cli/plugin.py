"""Typer subcommand group for plugin sources and their cache.

Two verbs, and the split between them is deliberate. ``fetch`` is the
only command that clones, so ``conductor validate`` can stay off the
network; ``list`` reads the cache and reports what a workflow would
actually load.

There is no ``update``. A source whose ref is a tag or branch is
re-resolved on every run, so it updates itself; a source pinned to a full
SHA is meant not to. Re-pinning is a one-character edit to the YAML,
which a command that rewrote the file for you would only obscure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table
from rich.text import Text

from conductor.console import make_console, styled

console = make_console(stderr=True)
output_console = make_console()

plugin_app = typer.Typer(
    name="plugin",
    help="Inspect and acquire the plugins a workflow uses.",
    no_args_is_help=True,
)


def _load(workflow: Path):  # noqa: ANN202  (WorkflowConfig, imported lazily)
    """Load and parse a workflow file, or exit with a readable error."""
    from conductor.config.loader import load_workflow
    from conductor.exceptions import ConductorError

    resolved = workflow.resolve()
    if not resolved.is_file():
        console.print(styled("[bold red]Error:[/bold red] Workflow file not found: {}", workflow))
        raise typer.Exit(code=1)
    try:
        return load_workflow(resolved), resolved
    except ConductorError as exc:
        console.print(styled("[bold red]Error:[/bold red] {}", exc))
        raise typer.Exit(code=1) from None


def _resolve(config, workflow_path: Path, *, allow_network: bool, strict: bool = True):  # noqa: ANN001, ANN202
    """Resolve declared sources, reporting failures as a CLI error.

    ``strict`` distinguishes the two verbs. ``fetch`` exists to acquire
    everything, so any failure is fatal. ``list`` is a report, and one
    source that cannot be read should cost its own line rather than the
    whole listing — including the healthy sources beside it.
    """
    from conductor.plugins.errors import PluginError
    from conductor.plugins.resolution import resolve_plugin_sources

    declared = config.workflow.runtime.plugin_sources
    if strict:
        try:
            return resolve_plugin_sources(
                declared,
                base_dir=workflow_path.parent,
                allow_network=allow_network,
                on_warning=lambda message: console.print(styled("[yellow]⚠[/yellow] {}", message)),
            )
        except (PluginError, OSError) as exc:
            # OSError too: the cache is created during acquisition, so an
            # unwritable home or a full disk surfaces here as a bare
            # PermissionError and would otherwise print a raw traceback.
            console.print(styled("[bold red]Error:[/bold red] {}", exc))
            raise typer.Exit(code=1) from None

    resolved: dict = {}
    for name, entry in declared.items():
        try:
            resolved.update(
                resolve_plugin_sources(
                    {name: entry},
                    base_dir=workflow_path.parent,
                    allow_network=allow_network,
                    on_warning=lambda message: console.print(
                        styled("[yellow]⚠[/yellow] {}", message)
                    ),
                )
            )
        except (PluginError, OSError) as exc:
            console.print(styled("[yellow]⚠[/yellow] {}: {}", name, exc))
    return resolved


@plugin_app.command("fetch")
def fetch_plugins(
    workflow: Annotated[
        Path,
        typer.Argument(help="Path to the workflow YAML file whose sources to acquire."),
    ],
) -> None:
    """Acquire every git-backed plugin source a workflow declares.

    Priming the cache as its own step is what lets CI keep cloning out of
    the run itself, and what makes ``conductor validate`` usable on a
    machine that has never seen the sources.

    A source pinned to a full commit SHA is fetched once and never
    re-checked. A source on a tag or branch is re-resolved every time, so
    running this again picks up a moved ref.

    \b
    Examples:
        conductor plugin fetch workflow.yaml
    """
    from conductor.plugins.fetch import clear_resolution_memo

    config, resolved_path = _load(workflow)
    declared = config.workflow.runtime.plugin_sources
    if not declared:
        output_console.print("This workflow declares no plugin sources.")
        return

    # An explicit fetch should ask the remote, not reuse an answer this
    # process happened to cache earlier.
    clear_resolution_memo()
    sources = _resolve(config, resolved_path, allow_network=True)

    fetched = sum(1 for entry in sources.values() if entry.fetched)
    for name, entry in sources.items():
        detail = entry.source.describe()
        if entry.sha:
            detail = styled("{} @ [cyan]{}[/cyan]", detail, entry.sha[:12])
        if entry.source.is_local:
            state = "local"
        elif entry.stale:
            state = "cached (ref not re-checked)"
        else:
            state = "fetched" if entry.fetched else "cached"
        # Passed as a value rather than concatenated into the template: the
        # template is meant to be a conductor literal, and building it from a
        # variable is the shape that becomes an injection site the day the
        # variable holds runtime data. ``styled`` splices a pre-styled
        # ``Text`` with its spans intact, which is what it is for.
        mark = (
            Text.from_markup("[yellow]⚠[/yellow]")
            if entry.stale
            else Text.from_markup("[green]✓[/green]")
        )
        output_console.print(
            styled(
                "  {} {} — {} — {}, {} plugin(s)",
                mark,
                name,
                detail,
                state,
                len(entry.marketplace.plugins),
            )
        )
    stale = sum(1 for entry in sources.values() if entry.stale)
    summary = Text(f"\n{len(sources)} source(s) ready ({fetched} newly fetched)")
    if stale:
        # The command exists to acquire; a row it could not verify must not
        # read as a green light, least of all in the CI step that gates on it.
        summary += styled(", [yellow]{} not re-checked[/yellow]", stale)
    output_console.print(summary + ".")


@plugin_app.command("list")
def list_plugins(
    workflow: Annotated[
        Path,
        typer.Argument(help="Path to the workflow YAML file whose plugins to list."),
    ],
) -> None:
    """List the plugins a workflow enables and what each one brings.

    Reads the cache only — never the network — so it reports the state a
    run would start from. Run ``conductor plugin fetch`` first if a
    source has not been acquired.

    The component counts are the part worth reading. A plugin name says
    nothing about how much it carries, and an unpinned source can gain a
    subagent or an MCP server between two runs. An MCP server is a
    subprocess launched with your credentials, so a change in that column
    is worth noticing.

    \b
    Examples:
        conductor plugin list workflow.yaml
    """
    config, resolved_path = _load(workflow)
    sources = (
        _resolve(config, resolved_path, allow_network=False, strict=False)
        if config.workflow.runtime.plugin_sources
        else {}
    )

    if sources:
        table = Table(title="Plugin sources")
        table.add_column("Marketplace", style="cyan")
        table.add_column("Source")
        table.add_column("Commit", style="green")
        table.add_column("Plugins", justify="right")
        for name, entry in sources.items():
            table.add_row(
                name,
                entry.source.describe(),
                entry.sha[:12] if entry.sha else "—",
                str(len(entry.marketplace.plugins)),
            )
        output_console.print(table)

    _list_enabled_plugins(config, resolved_path, sources)


def _list_enabled_plugins(config, workflow_path: Path, sources) -> None:  # noqa: ANN001
    """Print each enabled plugin and its component counts.

    Reports the *effective* set per agent: an agent that overrides
    ``plugins:`` gets a different list from one that inherits
    ``runtime.plugins``, and printing only the workflow default would
    describe a run that is not the one about to happen.

    Grouped by entry list *and* resolved plugin flavor (issue #497): two
    agents naming the same entries on different providers can resolve to
    different builds, so they are reported — and their component counts
    computed — separately rather than one silently standing in for both.
    """
    from conductor.plugins.errors import PluginError
    from conductor.plugins.manifest import PluginFlavor
    from conductor.plugins.registry import resolve_plugins
    from conductor.plugins.resolution import marketplaces_from
    from conductor.providers.capabilities import plugin_flavor_for
    from conductor.skills import SkillError

    marketplaces = marketplaces_from(sources)
    declared_names = set(config.workflow.runtime.plugin_sources) - set(sources)
    default_provider = config.workflow.runtime.provider.name

    # Group agents by the entry list *and* flavor they resolve, so a
    # workflow whose agents all inherit prints one section rather than one
    # per agent — but two providers sharing an entry list still get their
    # own section when they resolve to different builds.
    groups: dict[tuple[tuple[tuple[str, bool, bool, bool], ...], PluginFlavor | None], list[str]]
    groups = {}

    def _record(name: str, agent) -> None:  # noqa: ANN001
        if agent.type not in (None, "agent"):
            return
        entries = agent.plugins if agent.plugins is not None else config.workflow.runtime.plugins
        if not entries:
            return
        entries_key = tuple((e.name, e.skills, e.agents, e.mcp) for e in entries)
        provider_name = agent.provider or default_provider
        flavor = plugin_flavor_for(provider_name)
        groups.setdefault((entries_key, flavor), []).append(name)

    for agent in config.agents:
        _record(agent.name, agent)
    # A for_each group's inline agent is a full AgentDef with its own
    # ``plugins:``, and it is the one that starts N copies of whatever MCP
    # server the plugin declares — so omitting it under-reports in exactly
    # the place the counts matter most. ``config/validator.py`` walks
    # ``for_each`` for the same reason.
    for group in config.for_each:
        if group.agent is not None:
            _record(f"{group.name} (for_each)", group.agent)

    if not groups:
        output_console.print("No agent in this workflow enables plugins.")
        return

    from conductor.config.schema import PluginDef

    for (entries_key, flavor), agents in groups.items():
        entries = [
            PluginDef(name=name, skills=skills, agents=agents_on, mcp=mcp)
            for name, skills, agents_on, mcp in entries_key
        ]
        output_console.print(styled("\n[bold]Agents:[/bold] {}", ", ".join(agents)))
        if flavor is not None:
            output_console.print(styled("  [dim]flavor: {}[/dim]", flavor))
        try:
            resolved = resolve_plugins(
                entries,
                base_dir=workflow_path.parent,
                marketplaces=marketplaces,
                declared_sources=declared_names,
                on_warning=lambda message: console.print(styled("[yellow]⚠[/yellow] {}", message)),
                flavor=flavor,
            )
        except (PluginError, SkillError, OSError) as exc:
            output_console.print(styled("  [yellow]⚠[/yellow] {}", exc))
            continue

        for plugin in resolved:
            parts = (
                f"{len(plugin.skills)} skill(s), "
                f"{len(plugin.agents)} agent(s), "
                f"{len(plugin.mcp_servers)} MCP server(s)"
            )
            output_console.print(styled("  [cyan]•[/cyan] {} — {}", plugin.source, parts))
            output_console.print(styled("    [dim]{}[/dim]", plugin.root))
            if plugin.agents:
                names = ", ".join(item.qualified_name for item in plugin.agents)
                output_console.print(styled("    [dim]agents: {}[/dim]", names))
            if plugin.mcp_servers:
                output_console.print(
                    styled("    [dim]mcp: {}[/dim]", ", ".join(sorted(plugin.mcp_servers)))
                )
            if plugin.disabled:
                output_console.print(
                    styled(
                        "    [dim]disabled by this workflow: {}[/dim]", ", ".join(plugin.disabled)
                    )
                )
            if plugin.dropped:
                output_console.print(
                    styled(
                        "    [dim]not loaded: {}[/dim]", ", ".join(f"{d}/" for d in plugin.dropped)
                    )
                )


__all__ = ["plugin_app"]
