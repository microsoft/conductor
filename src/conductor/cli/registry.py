"""Typer subcommand group for workflow registry management."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from conductor.registry.cache import clear_cache, fetch_workflow, get_cached_workflow_path
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
from conductor.registry.index import load_index
from conductor.registry.resolver import ResolvedRef, resolve_ref

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
    table.add_column("Versions", style="green")

    for wf_name, info in index.workflows.items():
        versions = ", ".join(info.versions) if info.versions else "-"
        table.add_row(wf_name, info.description or "-", versions)

    output_console.print(table)


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


@registry_app.command()
def update(
    name: Annotated[
        str | None,
        typer.Argument(help="Registry to update (all if omitted)."),
    ] = None,
) -> None:
    """Refresh registry index and clear cached workflows."""
    try:
        config = load_config()

        if name is not None:
            if name not in config.registries:
                raise RegistryError(
                    f"Registry '{name}' not found",
                    suggestion="Run 'conductor registry list' to see available registries.",
                )
            clear_cache(name)
            load_index(config.registries[name])
            output_console.print(f"Registry '{name}' updated.")
        else:
            if not config.registries:
                output_console.print("No registries configured.")
                return
            clear_cache()
            for reg_name, entry in config.registries.items():
                load_index(entry)
                output_console.print(f"Registry '{reg_name}' updated.")
    except RegistryError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None


@registry_app.command()
def show(
    ref: Annotated[
        str,
        typer.Argument(help="Workflow reference (name[@registry][@version]) or registry name."),
    ],
) -> None:
    """Show metadata and cached path for a workflow reference.

    If the argument matches a configured registry name, shows that registry's
    workflows instead (equivalent to ``conductor registry list <name>``).
    """
    # If the argument is a known registry name, show its workflows instead
    config = load_config()
    if ref in config.registries:
        try:
            _show_registry(ref, config.registries[ref])
        except RegistryError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            raise typer.Exit(code=1) from None
        return

    try:
        resolved = resolve_ref(ref)
        if resolved.kind == "file":
            output_console.print(f"Local file: {resolved.path}")
            return

        assert resolved.registry_entry is not None
        assert resolved.registry_name is not None
        assert resolved.workflow is not None

        index = load_index(resolved.registry_entry)
        if resolved.workflow not in index.workflows:
            raise RegistryError(
                f"Workflow '{resolved.workflow}' not found in registry '{resolved.registry_name}'",
                suggestion=(
                    f"Run 'conductor registry list {resolved.registry_name}' "
                    "to see available workflows."
                ),
            )

        info = index.workflows[resolved.workflow]
        version = resolved.version or (info.versions[-1] if info.versions else None)

        output_console.print(f"[bold]Workflow:[/bold]  {resolved.workflow}")
        output_console.print(f"[bold]Registry:[/bold]  {resolved.registry_name}")
        output_console.print(f"[bold]Description:[/bold] {info.description or '-'}")
        output_console.print(f"[bold]Path:[/bold]      {info.path}")
        output_console.print(
            f"[bold]Versions:[/bold]  {', '.join(info.versions) if info.versions else '-'}"
        )
        output_console.print(f"[bold]Version:[/bold]   {version or 'latest'}")

        if version:
            cached = get_cached_workflow_path(resolved.registry_name, resolved.workflow, version)
            output_console.print(f"[bold]Cached:[/bold]    {cached or 'not cached'}")

        # Fetch and display workflow inputs
        _show_workflow_inputs(resolved, version)
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
    table.add_column("Versions", style="green")

    for wf_name, info in index.workflows.items():
        versions = ", ".join(info.versions) if info.versions else "-"
        table.add_row(wf_name, info.description or "-", versions)

    output_console.print(table)


def _show_workflow_inputs(resolved: ResolvedRef, version: str | None) -> None:
    """Fetch the workflow and display its input parameters."""
    assert resolved.registry_entry is not None
    assert resolved.registry_name is not None
    assert resolved.workflow is not None

    try:
        workflow_path = fetch_workflow(
            registry_name=resolved.registry_name,
            registry_entry=resolved.registry_entry,
            workflow_name=resolved.workflow,
            version=version,
        )
    except RegistryError:
        return

    try:
        from conductor.config.loader import load_config as load_workflow_config

        config = load_workflow_config(workflow_path)
    except Exception:
        return

    inputs = config.workflow.input
    if not inputs:
        output_console.print("\n[bold]Inputs:[/bold]    (none)")
        return

    output_console.print()
    table = Table(title="Inputs")
    table.add_column("Name", style="cyan")
    table.add_column("Type", style="green")
    table.add_column("Required", justify="center")
    table.add_column("Default")
    table.add_column("Description")

    for name, input_def in inputs.items():
        required = "✓" if input_def.required else ""
        default = str(input_def.default) if input_def.default is not None else "-"
        table.add_row(name, input_def.type, required, default, input_def.description or "-")

    output_console.print(table)

    input_args = " ".join(f'--input {name}="..."' for name in inputs)
    output_console.print(
        f"\n[dim]Example: conductor run {resolved.workflow} {input_args}[/dim]"
    )
