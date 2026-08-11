"""The Registries drill-down screen for the Fleet Manager TUI (Fleet Manager E11).

Three levels, each its own :class:`~textual.screen.Screen` pushed onto the
app's real screen stack (established in E7-T3) rather than one screen with
internal state -- so "Escape unwinds one level at a time" (the design's own
words) is a direct property of Textual's stack, not something this module
has to simulate:

* :class:`RegistriesScreen` -- every configured registry (E11-T1), sourced
  from :func:`conductor.providers.diagnostics.gather_registries`, which
  already wraps :func:`conductor.registry.config.load_config` and
  distinguishes a load *failure* (``error``) from a genuinely empty config
  -- surfaced here the same way, rather than collapsing both into "no
  registries".
* :class:`RegistryWorkflowsScreen` -- a registry's workflows (E11-T2), via
  :func:`conductor.registry.index.load_index` /
  ``RegistryIndex.workflows``. Index loading can hit the network for
  GitHub-backed registries, so it always runs as an awaited worker,
  triggered only by the explicit row-selection that pushes this screen --
  never automatically, and there is no poll timer on this screen to
  accidentally re-trigger it.
* :class:`WorkflowInputsScreen` -- one workflow's inputs (E11-T3), fetched
  with :func:`conductor.registry.cache.resolve_and_fetch` (also
  network-capable for a GitHub-backed registry) and parsed with
  :func:`conductor.config.loader.load_config`, rendering ``wf.input`` with
  the exact same field set ``conductor show`` uses (``cli/app.py``): type,
  required, default, description.
"""

from __future__ import annotations

import asyncio
import logging

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from conductor.console import styled
from conductor.providers.diagnostics import RegistryDiagnostic, gather_registries
from conductor.registry.config import RegistryEntry, get_registry
from conductor.registry.index import RegistryIndex, load_index

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registries (top level)
# ---------------------------------------------------------------------------


class RegistriesScreen(Screen):
    """Configured registries: name, type, source, default marker (E11-T1)."""

    BINDINGS = [("escape", "back", "Back")]

    def action_back(self) -> None:
        """Pop back to the Runs screen -- bound to ``escape``."""
        self.app.pop_screen()

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="registries-table")
        yield Static(id="registries-message")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Name", "Type", "Source", "Default")
        table.cursor_type = "row"
        self.load_registries()

    def load_registries(self) -> None:
        """Gather configured registries.

        ``gather_registries`` is synchronous and purely local (it only
        reads ``registries.toml`` off disk -- no network), so this is
        called directly rather than dispatched to a worker, unlike the
        network-capable drill-down levels below.
        """
        try:
            diag = gather_registries()
        except Exception as e:  # noqa: BLE001 - a screen must never crash the TUI
            logger.warning("Failed to gather registries", exc_info=True)
            diag = RegistryDiagnostic(default=None, registries=[], error=str(e))
        self._render_registries(diag)

    def _render_registries(self, diag: RegistryDiagnostic) -> None:
        table = self.query_one(DataTable)
        message = self.query_one("#registries-message", Static)
        table.clear()

        if diag.error:
            # A load failure (e.g. malformed registries.toml) is an error,
            # not "no registries" (E11-T1 / acceptance criterion).
            table.display = False
            message.display = True
            message.update(styled("[red]Failed to load registries:[/red] {}", diag.error))
            return

        if not diag.registries:
            # Genuinely empty config -- a normal state, not an error.
            table.display = False
            message.display = True
            message.update("[dim]No registries configured.[/dim]")
            return

        table.display = True
        message.display = False
        for reg in diag.registries:
            table.add_row(
                Text(reg.name),
                Text(reg.type),
                Text(reg.source),
                "✓" if reg.is_default else "",
                key=reg.name,
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Push the workflows drill-down for the selected registry."""
        name = event.row_key.value
        if name is None:
            return
        self.app.push_screen(RegistryWorkflowsScreen(name))


# ---------------------------------------------------------------------------
# A registry's workflows
# ---------------------------------------------------------------------------


class RegistryWorkflowsScreen(Screen):
    """Workflows listed in one registry's index (E11-T2)."""

    BINDINGS = [("escape", "back", "Back")]

    def __init__(self, registry_name: str) -> None:
        super().__init__()
        self._registry_name = registry_name
        self._entry: RegistryEntry | None = None
        self._index: RegistryIndex | None = None

    def action_back(self) -> None:
        """Pop back to the Registries screen -- bound to ``escape``. Only
        one level unwinds per press, matching the real screen stack."""
        self.app.pop_screen()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="workflows-title")
        yield DataTable(id="workflows-table")
        yield Static(id="workflows-message")
        yield Footer()

    def on_mount(self) -> None:
        title = self.query_one("#workflows-title", Static)
        title.update(styled("[bold]Workflows in '{}'[/bold]", self._registry_name))
        table = self.query_one(DataTable)
        table.add_columns("Name", "Description")
        table.cursor_type = "row"
        self.load_workflows()

    @work
    async def load_workflows(self) -> None:
        """Resolve the registry entry and load its index as an awaited worker.

        Index loading can hit the network for GitHub-backed registries
        (``registry/index.py::load_index``), so this is dispatched to a
        thread via ``asyncio.to_thread`` and awaited -- never run inline
        from a synchronous handler, and never on a poll timer (this screen
        has none). Triggered only by the explicit row-selection that
        pushed this screen.
        """
        message = self.query_one("#workflows-message", Static)
        table = self.query_one(DataTable)
        table.display = False
        message.display = True
        message.update("[dim]Loading workflows…[/dim]")
        try:
            entry = await asyncio.to_thread(get_registry, self._registry_name)
            index = await asyncio.to_thread(load_index, entry)
        except Exception as e:  # noqa: BLE001 - surfaced, not crashed
            logger.warning(
                "Failed to load index for registry %s", self._registry_name, exc_info=True
            )
            message.update(styled("[red]Failed to load workflows:[/red] {}", str(e)))
            return

        self._entry = entry
        self._index = index
        self._render_workflows(index)

    def _render_workflows(self, index: RegistryIndex) -> None:
        table = self.query_one(DataTable)
        message = self.query_one("#workflows-message", Static)
        table.clear()

        if not index.workflows:
            table.display = False
            message.display = True
            message.update(
                styled("[dim]No workflows found in registry '{}'.[/dim]", self._registry_name)
            )
            return

        table.display = True
        message.display = False
        for wf_name, info in index.workflows.items():
            table.add_row(Text(wf_name), Text(info.description or "-"), key=wf_name)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Push the inputs drill-down for the selected workflow."""
        wf_name = event.row_key.value
        if wf_name is None or self._entry is None:
            return
        self.app.push_screen(
            WorkflowInputsScreen(
                registry_name=self._registry_name, entry=self._entry, workflow_name=wf_name
            )
        )


# ---------------------------------------------------------------------------
# A workflow's inputs
# ---------------------------------------------------------------------------


class WorkflowInputsScreen(Screen):
    """A single workflow's inputs, fetched and parsed on demand (E11-T3).

    Field set (``type``/``required``/``default``/``description``) matches
    ``conductor show``'s Inputs table (``cli/app.py``) exactly.
    """

    BINDINGS = [("escape", "back", "Back")]

    def __init__(self, *, registry_name: str, entry: RegistryEntry, workflow_name: str) -> None:
        super().__init__()
        self._registry_name = registry_name
        self._entry = entry
        self._workflow_name = workflow_name

    def action_back(self) -> None:
        """Pop back to the workflows screen -- bound to ``escape``. Only
        one level unwinds per press, matching the real screen stack."""
        self.app.pop_screen()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="inputs-title")
        yield DataTable(id="inputs-table")
        yield Static(id="inputs-message")
        yield Footer()

    def on_mount(self) -> None:
        title = self.query_one("#inputs-title", Static)
        title.update(styled("[bold]Inputs for '{}'[/bold]", self._workflow_name))
        table = self.query_one(DataTable)
        table.add_columns("Name", "Type", "Required", "Default", "Description")
        self.load_inputs()

    @work
    async def load_inputs(self) -> None:
        """Fetch (network-capable) and parse the workflow file, awaited.

        ``resolve_and_fetch`` (for a GitHub-backed registry) and
        ``config/loader.py::load_config`` are both synchronous, so each is
        dispatched to a thread via ``asyncio.to_thread`` and awaited --
        never run inline from a synchronous handler, and never on a poll
        timer (this screen has none). Triggered only by the explicit
        row-selection that pushed this screen.
        """
        message = self.query_one("#inputs-message", Static)
        table = self.query_one(DataTable)
        table.display = False
        message.display = True
        message.update("[dim]Loading workflow…[/dim]")

        from conductor.registry.cache import resolve_and_fetch
        from conductor.registry.resolver import ResolvedRef

        resolved = ResolvedRef(
            kind="registry",
            workflow=self._workflow_name,
            registry_name=self._registry_name,
            registry_entry=self._entry,
            ref=None,
        )
        try:
            path = await asyncio.to_thread(resolve_and_fetch, resolved)
        except Exception as e:  # noqa: BLE001 - surfaced, not crashed
            logger.warning(
                "Failed to fetch workflow %s from registry %s",
                self._workflow_name,
                self._registry_name,
                exc_info=True,
            )
            message.update(styled("[red]Failed to fetch workflow:[/red] {}", str(e)))
            return

        try:
            from conductor.config.loader import load_config as load_workflow_config

            config = await asyncio.to_thread(load_workflow_config, path)
        except Exception as e:  # noqa: BLE001 - surfaced, not crashed
            logger.warning("Failed to parse workflow %s", path, exc_info=True)
            message.update(styled("[red]Failed to parse workflow:[/red] {}", str(e)))
            return

        self._render_inputs(config.workflow.input)

    def _render_inputs(self, inputs: dict) -> None:
        table = self.query_one(DataTable)
        message = self.query_one("#inputs-message", Static)
        table.clear()

        if not inputs:
            # No inputs defined -- a normal state (mirrors `conductor
            # show`, which simply omits the Inputs table), not an error.
            table.display = False
            message.display = True
            message.update("[dim]This workflow defines no inputs.[/dim]")
            return

        table.display = True
        message.display = False
        for name, input_def in inputs.items():
            required = "✓" if input_def.required else ""
            default = str(input_def.default) if input_def.default is not None else "-"
            table.add_row(
                Text(name),
                Text(input_def.type),
                required,
                Text(default),
                Text(input_def.description or "-"),
                key=name,
            )
