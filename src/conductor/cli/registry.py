"""Typer subcommand group for workflow registry management."""

from __future__ import annotations

from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.table import Table

from conductor.registry.cache import clear_cache, prune_temp_dirs
from conductor.registry.config import (
    RegistryEntry,
    RegistryType,
    add_registry,
    get_registry,
    load_config,
    remove_registry,
    save_config,
)
from conductor.registry.errors import RegistryError
from conductor.registry.github import list_tags, parse_github_source
from conductor.registry.index import load_index
from conductor.registry.version_resolver import sort_tags

_MAX_DISPLAY_TAGS = 5

registry_app = typer.Typer(
    name="registry",
    help="Manage workflow registries.",
    no_args_is_help=True,
)

console = Console(stderr=True)
output_console = Console()


@registry_app.command("list")
def list_registries(
    name: Annotated[
        str | None,
        typer.Argument(help="Registry name to list workflows from."),
    ] = None,
) -> None:
    """List configured registries, or workflows in a specific registry."""
    try:
        if name is None:
            _list_all_registries()
        else:
            _list_registry_workflows(name)
    except RegistryError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None


def _list_all_registries() -> None:
    """Display a table of all configured registries."""
    config = load_config()

    if not config.registries:
        output_console.print("No registries configured.")
        output_console.print("Run [bold]conductor registry add <name> <source>[/bold] to add one.")
        return

    table = Table(title="Configured Registries")
    table.add_column("Name", style="cyan")
    table.add_column("Type", style="green")
    table.add_column("Source")
    table.add_column("Default", justify="center")

    for reg_name, entry in config.registries.items():
        is_default = "✓" if config.default == reg_name else ""
        table.add_row(reg_name, entry.type.value, entry.source, is_default)

    output_console.print(table)


def _list_registry_workflows(name: str) -> None:
    """Display a table of workflows in a specific registry."""
    entry = get_registry(name)
    index = load_index(entry)

    if not index.workflows:
        output_console.print(f"No workflows found in registry '{name}'.")
        return

    table = Table(title=f"Workflows in '{name}'")
    table.add_column("Name", style="cyan")
    table.add_column("Description")

    for wf_name, info in index.workflows.items():
        table.add_row(wf_name, info.description or "-")

    output_console.print(table)

    tags_line = _format_latest_tags(entry)
    if tags_line is not None:
        output_console.print(f"\nLatest tags: {tags_line}")

    output_console.print("\n[dim]Use 'conductor show <workflow>' to see inputs and details.[/dim]")


def _format_latest_tags(entry: RegistryEntry) -> str | None:
    """Return a formatted "Latest tags" string for a registry, or None for path registries.

    For github registries, fetches tags via the API and returns up to
    ``_MAX_DISPLAY_TAGS`` newest tags (semver-sorted). Returns
    ``"(unavailable: <reason>)"`` on a fetch failure (surfacing the
    underlying ``RegistryError`` message or the HTTP exception class name)
    and ``"(no tags)"`` if the repo has none.
    """
    if entry.type != RegistryType.github:
        return None

    try:
        owner, repo = parse_github_source(entry.source)
        tags = list_tags(owner, repo)
    except RegistryError as error:
        return f"(unavailable: {error})"
    except httpx.HTTPError as error:
        return f"(unavailable: {type(error).__name__})"

    if not tags:
        return "(no tags)"

    sorted_tags = sort_tags(tags)
    display = sorted_tags[:_MAX_DISPLAY_TAGS]
    suffix = ", ..." if len(sorted_tags) > _MAX_DISPLAY_TAGS else ""
    return ", ".join(display) + suffix


@registry_app.command()
def add(
    name: Annotated[str, typer.Argument(help="Name for the registry.")],
    source: Annotated[
        str,
        typer.Argument(help="Registry source (owner/repo for GitHub, path for local)."),
    ],
    type: Annotated[
        str | None,
        typer.Option("--type", "-t", help="Registry type: github or path."),
    ] = None,
    default: Annotated[
        bool,
        typer.Option("--default", "-d", help="Set as the default registry."),
    ] = False,
) -> None:
    """Add a new workflow registry."""
    try:
        reg_type = RegistryType(type) if type is not None else None
        add_registry(name, source, registry_type=reg_type, set_default=default)
        output_console.print(f"Registry '{name}' added ({source}).")
        if default:
            output_console.print(f"Set '{name}' as the default registry.")
    except RegistryError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None


@registry_app.command()
def remove(
    name: Annotated[str, typer.Argument(help="Name of the registry to remove.")],
) -> None:
    """Remove a workflow registry."""
    try:
        remove_registry(name)
        output_console.print(f"Registry '{name}' removed.")
    except RegistryError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None


@registry_app.command("set-default")
def set_default(
    name: Annotated[str, typer.Argument(help="Name of the registry to set as default.")],
) -> None:
    """Set the default workflow registry."""
    try:
        config = load_config()
        if name not in config.registries:
            raise RegistryError(
                f"Registry '{name}' not found",
                suggestion="Run 'conductor registry list' to see available registries.",
            )
        config.default = name
        save_config(config)
        output_console.print(f"Default registry set to '{name}'.")
    except RegistryError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None


@registry_app.command(
    help=(
        "Refresh registry index and clear cached workflows.\n\n"
        "Re-fetches the latest index from each configured registry and clears "
        "locally-cached workflow files. Index fetches always bypass GitHub's CDN "
        "cache (via SHA-based URLs), so this primarily clears cached workflow "
        "contents pinned to mutable refs (branches). Also prunes orphaned "
        "'.tmp-*' directories left behind by interrupted fetches."
    ),
)
def update(
    name: Annotated[
        str | None,
        typer.Argument(help="Registry to update (all if omitted)."),
    ] = None,
) -> None:
    """Refresh registry index and clear cached workflows.

    Re-fetches the latest index from each configured registry and clears
    locally-cached workflow files. Index fetches always bypass GitHub's CDN
    cache (via SHA-based URLs), so this primarily clears cached workflow
    contents pinned to mutable refs (branches). Additionally prunes any
    orphaned ``.tmp-*`` directories left behind by interrupted fetches.
    """
    try:
        config = load_config()

        if name is not None:
            if name not in config.registries:
                raise RegistryError(
                    f"Registry '{name}' not found",
                    suggestion="Run 'conductor registry list' to see available registries.",
                )
            clear_cache(name)
            pruned = prune_temp_dirs(name)
            if pruned > 0:
                output_console.print(f"Pruned {pruned} stale .tmp-* directories.")
            load_index(config.registries[name])
            output_console.print(f"Registry '{name}' updated.")
        else:
            if not config.registries:
                output_console.print("No registries configured.")
                return
            clear_cache()
            pruned = prune_temp_dirs()
            if pruned > 0:
                output_console.print(f"Pruned {pruned} stale .tmp-* directories.")
            for reg_name, entry in config.registries.items():
                load_index(entry)
                output_console.print(f"Registry '{reg_name}' updated.")
    except RegistryError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None


@registry_app.command()
def show(
    name: Annotated[
        str,
        typer.Argument(help="Registry name."),
    ],
) -> None:
    """Show details and workflows for a configured registry."""
    try:
        entry = get_registry(name)
        _show_registry(name, entry)
    except RegistryError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None


def _show_registry(name: str, entry: RegistryEntry) -> None:
    """Show a registry's details and its workflows."""
    output_console.print(f"[bold]Registry:[/bold]  {name}")
    output_console.print(f"[bold]Type:[/bold]      {entry.type.value}")
    output_console.print(f"[bold]Source:[/bold]    {entry.source}")
    output_console.print()

    index = load_index(entry)
    if not index.workflows:
        output_console.print("No workflows found.")
        return

    table = Table(title="Workflows")
    table.add_column("Name", style="cyan")
    table.add_column("Description")

    for wf_name, info in index.workflows.items():
        table.add_row(wf_name, info.description or "-")

    output_console.print(table)

    tags_line = _format_latest_tags(entry)
    if tags_line is not None:
        output_console.print(f"\nLatest tags: {tags_line}")

    output_console.print("\n[dim]Use 'conductor show <workflow>' to see inputs and details.[/dim]")
