"""The Providers drill-down screen for the Fleet Manager TUI (Fleet Manager E10).

"Pure reuse of `doctor --json`" (the design's Phase 2): this screen consumes
:mod:`conductor.providers.diagnostics`'s dataclasses directly (never
touching provider internals -- no provider is imported or instantiated
here) via the exact same :func:`~conductor.providers.diagnostics.gather` /
:func:`~conductor.providers.diagnostics.gather_provider` functions
``conductor doctor`` uses.

Offline by default (E10-T1): on mount, only the *offline* fields
(``installed`` / ``tier`` / credential presence) are gathered -- no network
call, matching ``diagnostics.py``'s own "offline by default" contract.
Listing a provider's models implies a connection check (network), so it is
never done automatically or on a timer -- only when the user explicitly
expands a provider that hasn't been checked yet (E10-T2), as an awaited
Textual worker so the UI is never blocked waiting on it.

Collapsed by default, one row per provider (e.g. ``copilot — 14 models``
once checked); expanding a provider inserts a row per model showing
``supported_reasoning_efforts`` / ``default_reasoning_effort`` /
``max_context_window_tokens`` (E10-T2). Tier is always visible, with
``"experimental"`` marked the same way ``cli/doctor.py::_tier_cell`` does
(E10-T3); a ``connection_error``/``models_error`` is surfaced as text, never
silently rendered as an empty list.
"""

from __future__ import annotations

import logging

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header

from conductor.console import styled
from conductor.providers.diagnostics import (
    ModelDiagnostic,
    ProviderDiagnostic,
    gather,
    gather_provider,
)

logger = logging.getLogger(__name__)

# A "::" can never appear in a real provider name (see
# ``providers/capabilities.py::known_provider_names``, all lowercase-hyphen
# identifiers), so it safely delimits a sub-row's owning provider from its
# own kind/id in that row's DataTable key -- used by
# ``on_data_table_row_selected`` to distinguish a provider row (toggle
# expand/collapse) from a model/status sub-row (no action).
_SUB_ROW_DELIMITER = "::"


def _tier_cell(tier: str | None) -> Text:
    """Format the tier cell.

    Mirrors ``cli/doctor.py::_tier_cell`` exactly (E10-T3) so a provider's
    tier reads identically whether via ``conductor doctor`` or this screen.
    Duplicated rather than imported: it is presentation logic private to
    each renderer, matching the existing precedent in
    ``run_detail.py``'s own formatting helpers (which duplicate, rather
    than import, ``runs.py``'s ``_format_duration``).
    """
    if tier is None:
        return Text("—")
    if tier == "experimental":
        return Text.from_markup("[yellow]experimental[/yellow]")
    return Text(tier)


def _installed_cell(diag: ProviderDiagnostic) -> Text:
    """Format the installed/implemented cell."""
    if not diag.implemented:
        return Text.from_markup("[dim]n/a[/dim]")
    if diag.installed:
        return Text.from_markup("[green]✓[/green]")
    return Text.from_markup("[red]✗[/red]")


def _credentials_cell(diag: ProviderDiagnostic) -> Text:
    """Format credential presence as a compact ``present/total`` count.

    Presence only, never values -- matches
    ``ProviderDiagnostic.credential_env_vars``'s own contract.
    """
    if not diag.credential_env_vars:
        return Text("—")
    present = sum(1 for c in diag.credential_env_vars if c.present)
    total = len(diag.credential_env_vars)
    if present == total:
        return styled("[green]✓[/green] {}/{}", present, total)
    if diag.credentials_optional:
        return styled("[dim]{}/{}[/dim]", present, total)
    return styled("[red]✗[/red] {}/{}", present, total)


def _models_summary_cell(diag: ProviderDiagnostic, *, loading: bool) -> Text:
    """Format the collapsed models cell: the ``copilot — 14 models`` count.

    A ``connection_error`` (the check itself failed) or ``models_error``
    (the check succeeded but listing models failed) is surfaced as text
    here (E10-T3) rather than silently rendering as an empty/zero count.
    A completed check with ``connection_ok=False`` but no
    ``connection_error`` (a normal, non-exceptional connection failure) is
    distinguished from a genuinely unchecked provider -- ``checked=True``
    means "do not offer a retry hint". Likewise a checked provider whose
    ``models`` is ``None`` (listing not available/not enumerated) renders
    ``n/a``, not an unchecked hint. A provider with ``implemented=False``
    can never become ``checked`` (``diagnostics.py`` returns it unchanged
    regardless of ``check``/``list_models``), so it gets its own terminal
    "not implemented" state rather than the "enter to check" hint, which
    would otherwise never resolve.
    """
    if diag.connection_error:
        return styled("[red]✗[/red] {}", diag.connection_error)
    if diag.models_error:
        return styled("[red]✗[/red] {}", diag.models_error)
    if loading:
        return Text.from_markup("[dim]checking…[/dim]")
    if not diag.implemented:
        return Text.from_markup("[dim]not implemented[/dim]")
    if not diag.checked:
        return Text.from_markup("[dim]enter to check[/dim]")
    if diag.connection_ok is False:
        return Text.from_markup("[red]✗[/red] connection failed")
    if diag.connection_ok and diag.connection_note:
        return styled("[yellow]⚠[/yellow] {}", diag.connection_note)
    if diag.models is None:
        return Text.from_markup("[dim]n/a[/dim]")
    count = len(diag.models)
    if not count:
        return Text.from_markup("[dim](no models)[/dim]")
    return Text(f"{count} model{'s' if count != 1 else ''}")


def _format_tokens(value: int | None) -> Text:
    """Format a token-limit value with grouped digits, or ``—`` when unknown."""
    if value is None:
        return Text("—")
    return Text(f"{value:,}")


def _efforts_cell(model: ModelDiagnostic) -> Text:
    """Format the supported-reasoning-efforts cell.

    ``n/a`` when unknown (``None``), ``none`` for a definitive empty list
    (e.g. a non-thinking Claude model) -- the same distinction
    ``cli/doctor.py::_efforts_cell`` draws.
    """
    if model.supported_reasoning_efforts is None:
        return Text.from_markup("[dim]n/a[/dim]")
    if not model.supported_reasoning_efforts:
        return Text.from_markup("[dim]none[/dim]")
    return Text(", ".join(model.supported_reasoning_efforts))


def _default_effort_cell(model: ModelDiagnostic) -> Text:
    """Format the default-reasoning-effort cell."""
    if model.default_reasoning_effort is None:
        return Text("—")
    return Text(model.default_reasoning_effort)


class ProvidersScreen(Screen):
    """Providers drill-down: collapsed provider summary by default, expand
    for per-model reasoning-effort and context-window detail (E10)."""

    BINDINGS = [
        # Row-scoped -- toggles the highlighted provider's expand/collapse
        # state. Must be `priority`: `DataTable` binds `enter` itself (to
        # `select_cursor`, `show=False`) and, as the focused widget, sits
        # ahead of the screen in the binding chain -- so without priority
        # its hidden binding shadows this one and the key never appears in
        # the footer (see `runs.py`'s identical `BINDINGS` comment, issue #459).
        # The label is a single static "Expand/Collapse" rather than one
        # that tracks the highlighted row's current state -- honest in both
        # states without overriding `active_bindings` to rebuild Textual's
        # internal `ActiveBinding` tuples on every cursor move.
        Binding("enter", "toggle_provider", "Expand/Collapse", priority=True),
        ("escape", "back", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._providers: dict[str, ProviderDiagnostic] = {}
        self._order: list[str] = []
        self._expanded: set[str] = set()
        self._loading: set[str] = set()
        """Provider names with an in-flight (not yet resolved)
        ``check_provider_models`` worker -- prevents a second expand/enter
        from firing a duplicate network check while one is already running."""

    def action_back(self) -> None:
        """Pop back to the Runs screen -- bound to ``escape`` (matching the
        Escape-to-return convention established by E9's run-detail screen)."""
        self.app.pop_screen()

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="providers-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(
            "Provider",
            "Installed",
            "Tier / Effort",
            "Credentials / Default Effort",
            "Models / Context Window",
        )
        table.cursor_type = "row"
        self.load_providers()

    @work
    async def load_providers(self) -> None:
        """Gather **offline** provider diagnostics as a Textual worker (E10-T1).

        ``gather`` is ``async def`` (``diagnostics.py:534``), so this must
        run as an awaited worker rather than being called inline from a
        synchronous message handler (``on_mount`` here is sync, matching
        the pattern already established for async actions in
        ``runs.py``/``actions.py``). ``check=False`` (the default) keeps
        this a purely offline gather -- no network call, no per-provider
        connection probe -- consistent with the "Providers render offline
        by default" acceptance criterion.
        """
        try:
            report = await gather(sections=("providers",), check=False, list_models=False)
        except Exception:
            logger.warning("Failed to gather provider diagnostics", exc_info=True)
            return

        providers = report.providers or []
        self._providers = {p.name: p for p in providers}
        self._order = [p.name for p in providers]
        self._render_table()

    def _render_table(self) -> None:
        """Rebuild every row from :attr:`_providers`/:attr:`_expanded`/:attr:`_loading`.

        Never partially updates a row in place -- always a full rebuild,
        matching the same "clear and re-add" convention
        ``runs.py``/``run_detail.py`` already use for their own refreshes.
        """
        table = self.query_one(DataTable)
        table.clear()
        for name in self._order:
            diag = self._providers[name]
            arrow = "▼" if name in self._expanded else "▶"
            table.add_row(
                Text(f"{arrow} {diag.name}"),
                _installed_cell(diag),
                _tier_cell(diag.tier),
                _credentials_cell(diag),
                _models_summary_cell(diag, loading=name in self._loading),
                key=name,
            )
            if name in self._expanded:
                self._render_expanded_rows(table, diag, name)
        # Expanding/collapsing rewrites the row set beneath a stationary
        # cursor, which may not emit its own highlight message -- so the
        # footer is refreshed here too, not just on
        # `on_data_table_row_highlighted`, or `check_action` would keep
        # showing yesterday's answer for the row now under the cursor.
        self.refresh_bindings()

    def _render_expanded_rows(self, table: DataTable, diag: ProviderDiagnostic, name: str) -> None:
        """Add the sub-rows shown under an expanded provider (E10-T2/E10-T3).

        One row per model when models are available; otherwise a single
        explanatory sub-row (an error, a failed/unchecked connection, or
        "no models"/"n/a") rather than an empty gap under the provider row.
        A completed check with ``connection_ok=False`` but no
        ``connection_error`` and a checked provider whose ``models`` is
        ``None`` are each distinguished from a genuinely unchecked
        provider -- see :func:`_models_summary_cell`.
        """
        if diag.connection_error:
            table.add_row(
                styled("    [red]{}[/red]", diag.connection_error),
                "",
                "",
                "",
                "",
                key=f"{name}{_SUB_ROW_DELIMITER}connection-error",
            )
            return
        if diag.models_error:
            table.add_row(
                styled("    [red]{}[/red]", diag.models_error),
                "",
                "",
                "",
                "",
                key=f"{name}{_SUB_ROW_DELIMITER}models-error",
            )
            return
        if not diag.implemented:
            table.add_row(
                Text.from_markup("    [dim]not implemented[/dim]"),
                "",
                "",
                "",
                "",
                key=f"{name}{_SUB_ROW_DELIMITER}not-implemented",
            )
            return
        if not diag.checked:
            table.add_row(
                Text.from_markup("    [dim]not checked yet[/dim]"),
                "",
                "",
                "",
                "",
                key=f"{name}{_SUB_ROW_DELIMITER}unchecked",
            )
            return
        if diag.connection_ok is False:
            table.add_row(
                Text.from_markup("    [red]connection failed[/red]"),
                "",
                "",
                "",
                "",
                key=f"{name}{_SUB_ROW_DELIMITER}connection-failed",
            )
            return
        if diag.connection_ok and diag.connection_note:
            table.add_row(
                styled("    [yellow]⚠[/yellow] {}", diag.connection_note),
                "",
                "",
                "",
                "",
                key=f"{name}{_SUB_ROW_DELIMITER}connection-note",
            )
            return
        if diag.models is None:
            table.add_row(
                Text.from_markup("    [dim]n/a[/dim]"),
                "",
                "",
                "",
                "",
                key=f"{name}{_SUB_ROW_DELIMITER}no-listing",
            )
            return
        if not diag.models:
            table.add_row(
                Text.from_markup("    [dim](no models)[/dim]"),
                "",
                "",
                "",
                "",
                key=f"{name}{_SUB_ROW_DELIMITER}empty",
            )
            return
        for model in diag.models:
            table.add_row(
                Text(f"    {model.id}"),
                "",
                _efforts_cell(model),
                _default_effort_cell(model),
                _format_tokens(model.max_context_window_tokens),
                key=f"{name}{_SUB_ROW_DELIMITER}{model.id}",
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Toggle a provider row's expand/collapse state (mouse click) -- a
        model/status sub-row (its key contains :data:`_SUB_ROW_DELIMITER`)
        has no action of its own and is ignored."""
        key = event.row_key.value
        if key is None or _SUB_ROW_DELIMITER in key:
            return
        self._toggle_provider(key)

    def _selected_provider_name(self) -> str | None:
        """Return the provider name behind the currently highlighted row.

        ``None`` when the table is empty, the cursor's row key can't be
        resolved, or the highlighted row is a model/status sub-row (its
        key contains :data:`_SUB_ROW_DELIMITER`) -- mirrors ``runs.py``'s
        ``_selected_key``.
        """
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_coordinate is None:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        if key is None or _SUB_ROW_DELIMITER in key:
            return None
        return key

    def action_toggle_provider(self) -> None:
        """Toggle the highlighted provider's expand/collapse state -- the
        ``enter`` binding (E10-T2).

        This is a ``priority`` binding, so it runs *ahead* of ``DataTable``'s
        own hidden ``enter`` (``select_cursor``) and the keypress never
        becomes a ``RowSelected`` message. Mouse clicks still arrive that way
        and land in :meth:`on_data_table_row_selected`; both funnel through
        :meth:`_toggle_provider`, so keyboard and mouse each take exactly one
        path and ``enter`` cannot toggle twice.

        :meth:`check_action` hides this binding while a model sub-row is
        highlighted, so this is only reachable with a provider row
        selected -- but the same delimiter check as
        :meth:`_selected_provider_name` guards it anyway, defensively.
        """
        name = self._selected_provider_name()
        if name is None:
            return
        self._toggle_provider(name)

    def _toggle_provider(self, name: str) -> None:
        """Toggle ``name``'s expand/collapse state, checking its models on
        first expand if it hasn't been checked yet."""
        if name not in self._providers:
            return

        if name in self._expanded:
            self._expanded.discard(name)
            self._render_table()
            return

        self._expanded.add(name)
        diag = self._providers[name]
        if diag.implemented and not diag.checked and name not in self._loading:
            # Model listing implies a connection check (network) -- only
            # ever triggered here, by this explicit expand action, never
            # automatically or on a timer (E10-T2).
            self._loading.add(name)
            self._render_table()
            self.check_provider_models(name)
        else:
            self._render_table()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide ``toggle_provider`` while a model/status sub-row is highlighted.

        Mirrors ``runs.py``'s ``check_action``: returning ``False`` hides
        the key outright rather than greying it out, so ``enter`` is not
        advertised as "Expand/Collapse" on a row that can't be expanded
        (issue #459 acceptance criterion). When ``check_action`` returns
        ``False`` the screen binding is skipped entirely, so the keypress
        falls through to ``DataTable``'s own hidden ``enter`` ->
        ``RowSelected`` -> :meth:`on_data_table_row_selected`, which already
        ignores sub-rows -- sub-row behaviour is therefore unchanged, not
        merely hidden.
        """
        if action == "toggle_provider":
            return self._selected_provider_name() is not None
        return True

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Refresh the footer when the cursor moves to a different row.

        ``check_action`` is only consulted when bindings are refreshed, so
        without this the footer would keep showing yesterday's answer as
        the cursor moves between a provider row and its model sub-rows --
        mirrors ``runs.py``'s identical hook.
        """
        self.refresh_bindings()

    @work
    async def check_provider_models(self, name: str) -> None:
        """Explicitly check one provider's connection and list its models (E10-T2).

        Run as an awaited Textual worker so the UI is never blocked while
        this (network-bound) check is in flight. Only ever invoked from
        :meth:`on_data_table_row_selected` in response to an explicit user
        expand action -- never from the poll timer, and there is no poll
        timer on this screen to begin with.
        """
        try:
            diag = await gather_provider(name, check=True, list_models=True)
        except Exception:
            logger.warning("Failed to check provider %s", name, exc_info=True)
            diag = None
        finally:
            self._loading.discard(name)
        if diag is not None:
            self._providers[name] = diag
        self._render_table()
