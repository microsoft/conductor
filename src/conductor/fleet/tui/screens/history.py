"""The History screen for the Fleet Manager TUI (Fleet Manager E14).

Lists completed runs from retained event logs
(:func:`conductor.fleet.history.build_history_entries`) -- workflow,
outcome, duration, tokens, cost -- per the design's *Screens* section:
"History — completed runs, subject to retention." Per *What single-user
removes*, there is deliberately no long-horizon audit history and no
pagination: the list is exactly as bounded as
``build_history_entries``'s own retention-driven cap, nothing more.

**Depth is delegated, not re-implemented** (E14-T3, the design's *Division
of labor*: TUI = breadth, dashboard/replay = depth). Selecting a row does
not open any viewer inside this screen -- it surfaces the exact
``conductor replay <log>`` command for that run's event log via a Textual
notification, so the operator runs it themselves in a terminal. This
avoids taking on a second, TUI-owned subprocess/dashboard lifecycle for a
screen whose whole job is a retrospective list, not live supervision.

Loaded once on mount, like the Providers/Registries screens (E10/E11) --
unlike the Runs/run-detail screens' live ~2s poll, a completed run's
history does not change once written, so there is no live state here to
keep refreshed on a timer.
"""

from __future__ import annotations

import logging

from rich.text import Text
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from conductor.fleet.history import HistoryEntry, build_history_entries
from conductor.fleet.tui.theme import status_label

logger = logging.getLogger(__name__)

# Outcome badges/colours come from `tui/theme.py`, shared with the Runs and
# run-detail screens -- previously each defined its own glyph map, so the
# same run could read differently depending on which screen showed it, and
# none of them carried colour (a wall of "unknown" rows landed with the same
# weight as a failure).

_EMPTY_STATE_TEXT = (
    "[bold]No run history yet.[/bold]\n\n"
    "Completed runs appear here once their event log is written.\n\n"
    "[dim]Press escape to go back.[/dim]"
)


def _format_duration(seconds: float | None) -> str:
    """Render an elapsed duration compactly (``1h04``, ``18m``, ``42s``).

    Duplicated from ``conductor.fleet.tui.screens.runs`` (rather than
    imported) to keep each screen module's rendering self-contained --
    matching that module's own precedent (and ``run_detail.py``'s).
    """
    if seconds is None:
        return "—"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _format_tokens(tokens: int) -> str:
    """Render a token count compactly (``191k tok``), or ``"—"`` for zero.

    Duplicated from ``conductor.fleet.tui.screens.runs`` -- see that
    module's identical helper for the D5 rationale (completed-agent
    tokens only).
    """
    if tokens <= 0:
        return "—"
    if tokens >= 1000:
        return f"{tokens / 1000:.0f}k tok"
    return f"{tokens} tok"


def _format_cost(entry: HistoryEntry) -> str:
    """Render the cost cell, never presenting a partial total as complete.

    Duplicated from ``conductor.fleet.tui.screens.runs``'s
    ``_format_cost`` -- same ``~$X (N unpriced)`` convention (issue #265),
    adapted to read from a :class:`HistoryEntry` instead of a
    ``RunSummary``.
    """
    if entry.total_cost_usd is None:
        if entry.has_unpriced:
            return f"({entry.unpriced_agent_count} unpriced)"
        return "—"
    if entry.has_unpriced:
        return f"~${entry.total_cost_usd:.2f} ({entry.unpriced_agent_count} unpriced)"
    return f"~${entry.total_cost_usd:.2f}"


class HistoryScreen(Screen):
    """Completed-run history, bounded by retention (E14)."""

    BINDINGS = [("escape", "back", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self._displayed_entries: dict[str, HistoryEntry] = {}
        """Maps each DataTable row key back to the full
        :class:`~conductor.fleet.history.HistoryEntry` behind that row --
        the table itself only renders formatted cell text, so the
        row-selection handler (the replay-command action) needs this to
        recover the entry's log path."""

    def action_back(self) -> None:
        """Pop back to the Runs screen -- bound to ``escape``."""
        self.app.pop_screen()

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="history-table")
        yield Static(_EMPTY_STATE_TEXT, id="history-empty-state")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Workflow", "Outcome", "Duration", "Tokens", "Cost")
        table.cursor_type = "row"
        self.load_history()

    def load_history(self) -> None:
        """(Re-)build the history list and populate the table (or show the
        empty state).

        Best-effort: a failure building the list is logged and treated as
        "no history" (mirroring every other screen's degrade-gracefully
        convention) rather than crashing the screen --
        ``build_history_entries`` itself already never raises, but this
        is an extra backstop consistent with ``runs.py``'s own precedent.
        """
        try:
            entries = build_history_entries()
        except Exception:
            logger.warning("Failed to build run history", exc_info=True)
            entries = []

        table = self.query_one(DataTable)
        empty_state = self.query_one("#history-empty-state", Static)
        table.clear()

        if not entries:
            table.display = False
            empty_state.display = True
            self._displayed_entries = {}
            return

        table.display = True
        empty_state.display = False

        displayed: dict[str, HistoryEntry] = {}
        for index, entry in enumerate(entries):
            # `run_id` is not guaranteed unique across retained event logs:
            # a nested `conductor` invocation inherits `CONDUCTOR_RUN_ID`
            # from its parent (e.g. a `type: script` step launching a run
            # inside a `--web-bg` parent), so two distinct logs can share
            # one id -- and `run_id` may also be `None` for an
            # unrecognized filename shape. Always suffix with the
            # per-refresh index so the row key is unique regardless, then
            # guard `_add_row` itself (mirroring `runs.py`'s per-row
            # try/except) so one bad entry can never take the screen down.
            key = f"{entry.run_id or '_no-run-id'}-{index}"
            try:
                self._add_row(table, entry, key)
            except Exception:
                logger.warning(
                    "Failed to add history row for run_id=%s", entry.run_id, exc_info=True
                )
                continue
            displayed[key] = entry
        self._displayed_entries = displayed

    def _add_row(self, table: DataTable, entry: HistoryEntry, key: str) -> None:
        """Add one history entry's row: workflow, outcome, duration, tokens, cost."""
        table.add_row(
            Text(entry.workflow_name),
            status_label(entry.outcome),
            _format_duration(entry.duration_seconds),
            _format_tokens(entry.total_tokens),
            _format_cost(entry),
            key=key,
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Offer ``conductor replay <log>`` for the selected row (E14-T3).

        Depth is delegated, not re-implemented: this never opens a replay
        dashboard itself -- it surfaces the exact command via a
        notification so the operator can run it in a terminal (per the
        design's *Division of labor*, TUI = breadth). A key not present in
        :attr:`_displayed_entries` (e.g. a row selected in the narrow
        window between a refresh and this handler running) is silently
        ignored, mirroring ``runs.py``'s identical guard for its own
        row-selection handler.
        """
        key = event.row_key.value
        if key is None:
            return
        entry = self._displayed_entries.get(key)
        if entry is None:
            return
        self.notify(f"Replay with: conductor replay {entry.path}", markup=False)
