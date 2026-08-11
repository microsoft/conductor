"""The Runs (home) screen for the Fleet Manager TUI (Fleet Manager E7).

A flat list of every live run, sorted by recency — deliberately **not**
grouped by workflow definition, per the design's *Patterns adopted from
prior art*: "operators triage by which run needs attention, not by which
file it came from." A dedicated empty state renders the launch affordance
when nothing is running, rather than an empty table (E7-T5).

Refreshed on a ~2s poll timer (:data:`RunsScreen.POLL_INTERVAL_SECONDS`) via
Textual's ``set_interval`` — a full rescan of the run-record directory plus
a bounded event-log tail seek per live run (:mod:`conductor.fleet.summary`).
Per the design's *Refresh model*, there is deliberately no file watcher.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, ClassVar, cast

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from conductor.console import styled
from conductor.fleet.records import RunRecord, read_run_records
from conductor.fleet.summary import RunSummary, derive_run_summary
from conductor.fleet.tui.actions import (
    GateResolveOutcome,
    dashboard_disabled_reason,
    dashboard_url,
    gate_resolve_disabled_reason,
    kill_runs,
    open_dashboard,
    resolve_gate,
)
from conductor.fleet.tui.notify import TransitionNotifier, emit_terminal_notification

if TYPE_CHECKING:
    # Guarded to avoid a runtime circular import: app.py imports RunsScreen
    # from this module, so a top-level import of FleetApp here would cycle.
    from conductor.fleet.tui.app import FleetApp

logger = logging.getLogger(__name__)

# Per the design's mockup legend: "▲ = at human gate  ● = running". Other
# statuses in the vocabulary (`paused`/`completed`/`failed`) are rare for a
# row on this screen — every record here already passed the run record's own
# liveness check — but are given a badge too so a run caught in the narrow
# race the summary module documents (terminal event written, process not
# yet exited) still renders something sensible rather than a blank cell.
_STATUS_BADGES: dict[str, str] = {
    "running": "●",
    "at-gate": "▲",
    "paused": "⏸",
    "completed": "✓",
    "failed": "✗",
}

_EMPTY_STATE_TEXT = (
    "[bold]No workflows are currently running.[/bold]\n\n"
    "Launch one with:\n\n"
    "  [cyan]conductor run <workflow.yaml> --web-bg[/cyan]\n\n"
    "[dim]Press q to quit.[/dim]"
)


def _format_duration(seconds: float | None) -> str:
    """Render an elapsed duration compactly (``1h04``, ``18m``, ``42s``).

    Returns ``"—"`` for ``None`` (nothing to measure — e.g. no step is
    currently open, or the run's ``started_at`` couldn't be parsed).
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

    Per D5 / E6-T4, this total is completed-agent tokens only — there is no
    mid-flight usage event, so a currently-running agent never contributes
    here until it finishes.
    """
    if tokens <= 0:
        return "—"
    if tokens >= 1000:
        return f"{tokens / 1000:.0f}k tok"
    return f"{tokens} tok"


def _format_cost(summary: RunSummary) -> str:
    """Render the cost cell, never presenting a partial total as complete.

    Mirrors ``WorkflowUsage``'s ``~$X (N unpriced)`` convention (issue
    #265, reused by E6-T4): an unpriced agent is surfaced as a count
    alongside the total rather than silently summed in as zero.
    """
    if summary.total_cost_usd is None:
        if summary.has_unpriced:
            return f"({summary.unpriced_agent_count} unpriced)"
        return "—"
    if summary.has_unpriced:
        return f"~${summary.total_cost_usd:.2f} ({summary.unpriced_agent_count} unpriced)"
    return f"~${summary.total_cost_usd:.2f}"


def _workflow_cell(summary: RunSummary, record: RunRecord) -> str:
    """Render the Workflow column's badge + name (D4, E13-T3).

    A ``mode == "fg"`` run at a gate (no HTTP channel to resolve it
    remotely -- see ``conductor.fleet.tui.actions.gate_resolve_disabled_reason``)
    is marked ``(terminal · PID <pid>)`` so it reads as display-only at a
    glance, distinct from an ``fg-web``/``bg`` gate the ``g`` action can
    actually resolve.
    """
    badge = _STATUS_BADGES.get(summary.status, " ")
    cell = f"{badge} {summary.workflow_name}"
    if summary.status == "at-gate" and not summary.gate_resolvable:
        cell += f" (terminal · PID {record.pid})"
    return cell


def _notification_message(summary: RunSummary) -> str:
    """Build the terminal-bell/OSC 9 notification text for a fresh
    transition into ``at-gate`` or ``failed`` (E13-T4)."""
    if summary.status == "at-gate":
        gate = summary.gate
        if gate is not None and gate.agent_name:
            return f"{summary.workflow_name}: waiting at gate ({gate.agent_name})"
        return f"{summary.workflow_name}: waiting at gate"
    return f"{summary.workflow_name}: run failed"


def _gate_detail_text(summary: RunSummary) -> Text:
    """Render the gate-detail panel's text for a run with an open gate
    (E13-T1): its prompt and the available option labels/values -- the
    payload E6-T6 already carries on ``RunSummary.gate``, surfaced here
    rather than only implied by the row's ``current_step``/badge.

    ``gate.agent_name``/``gate.prompt``/``gate.options`` are workflow-
    controlled data, not authored Rich markup -- escaped so a value
    containing e.g. ``[/red]`` renders as literal text instead of raising
    MarkupError.
    """
    gate = summary.gate
    assert gate is not None
    parts = [styled("[bold]Gate:[/bold] {}", gate.agent_name), Text(gate.prompt)]
    if gate.options:
        parts.append(Text("Options: " + ", ".join(gate.options)))
    return Text("\n").join(parts)


class RunsScreen(Screen):
    """Home screen: every live run, sorted by recency, polled refresh."""

    POLL_INTERVAL_SECONDS: ClassVar[float] = 2.0
    """~2s poll per the design's *Refresh model*. Class attribute (rather
    than a hardcoded literal in :meth:`on_mount`) so tests can shrink it and
    observe a real poll tick pick up a newly-written record without waiting
    out the full interval."""

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("w", "open_dashboard", "Dashboard"),
        ("k", "kill", "Kill"),
        ("K", "kill_all", "Kill All"),
        ("p", "open_providers", "Providers"),
        ("r", "open_registries", "Registries"),
        ("n", "open_new_run", "New Run"),
        ("g", "resolve_gate", "Resolve Gate"),
        ("h", "open_history", "History"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._displayed_records: dict[str, RunRecord] = {}
        """Maps each DataTable row key (a run's ``run_id``, or the
        per-refresh fallback key for a legacy empty-``run_id`` record) back
        to the full :class:`RunRecord` behind that row -- ``RunSummary``
        (what the table actually renders) doesn't carry ``pid``, so the
        kill/dashboard actions (E8) need this to resolve a selected row
        back to something they can act on."""
        self._displayed_summaries: dict[str, RunSummary] = {}
        """Maps each DataTable row key to the :class:`RunSummary` derived
        for it on the most recent refresh -- the gate-resolve action
        (E13-T2) needs a selected row's ``gate``/``gate_resolvable``,
        which :attr:`_displayed_records` (plain ``RunRecord``\\ s) doesn't
        carry."""
        self._notifier = TransitionNotifier()
        """Debounces gate-entry/failure notifications (E13-T4) so a poll
        re-read of a run that stays ``at-gate``/``failed`` across
        multiple ticks doesn't re-fire on every tick."""
        self._resolving_gate = False
        """Guards against a second, concurrent ``g`` press starting a
        duplicate gate-resolve worker while one is already in flight
        (E13 review round 1) -- ``action_resolve_gate`` is a non-exclusive
        ``@work`` method, so without this a rapid double-press could open
        two option modals / post two responses for the same gate."""

    def action_quit(self) -> None:
        """Quit the app -- bound to ``q`` (Textual dispatches bindings to the
        focused screen, not the App, so this must live here to take effect)."""
        self.app.exit()

    # -----------------------------------------------------------------
    # Providers drill-down (E10-T4)
    # -----------------------------------------------------------------

    def action_open_providers(self) -> None:
        """Push the Providers drill-down screen -- bound to ``p``. Not tied
        to the currently-selected run row (unlike ``w``/``k``), since
        provider diagnostics are global, not per-run."""
        cast("FleetApp", self.app).push_providers()

    # -----------------------------------------------------------------
    # Registries drill-down (E11-T4)
    # -----------------------------------------------------------------

    def action_open_registries(self) -> None:
        """Push the Registries drill-down screen -- bound to ``r``. Not
        tied to the currently-selected run row (unlike ``w``/``k``), since
        configured registries are global, not per-run."""
        cast("FleetApp", self.app).push_registries()

    # -----------------------------------------------------------------
    # New-run launch (E12-T4)
    # -----------------------------------------------------------------

    def action_open_new_run(self) -> None:
        """Push the New-run screen -- bound to ``n``. Not tied to the
        currently-selected run row (unlike ``w``/``k``), since launching a
        new run is independent of whatever is already selected."""
        cast("FleetApp", self.app).push_new_run()

    # -----------------------------------------------------------------
    # History (E14-T3)
    # -----------------------------------------------------------------

    def action_open_history(self) -> None:
        """Push the History screen -- bound to ``h``. Not tied to the
        currently-selected run row (unlike ``w``/``k``), since run history
        is a separate, retrospective list, not a per-run action."""
        cast("FleetApp", self.app).push_history()

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="runs-table")
        yield Static(_EMPTY_STATE_TEXT, id="empty-state")
        yield Static(id="gate-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(
            "Workflow", "Current Step", "Elapsed", "On Step", "Tokens", "Cost", "Port"
        )
        table.cursor_type = "row"
        self.query_one("#gate-detail", Static).display = False
        self.refresh_runs()
        self.set_interval(self.POLL_INTERVAL_SECONDS, self.refresh_runs)

    def refresh_runs(self) -> None:
        """Rescan run records and repopulate the table (or show the empty state).

        Called once at mount and then on every poll tick. Best-effort: a
        failure reading records or deriving one run's summary is logged and
        that run is skipped rather than crashing the whole refresh —
        ``read_run_records()`` is already tolerant of individual bad files,
        but this is an extra backstop specifically so a poll loop can never
        take the TUI down.

        A transient failure reading the run-record directory itself (e.g.
        a momentary I/O error, not an individual bad record file --
        ``read_run_records()`` already tolerates those) is distinguished
        from a genuinely empty scan: it skips this tick entirely, leaving
        the previously displayed table, selection, and
        :attr:`_notifier` history untouched, rather than treating the
        failure as "zero records" and pruning all notifier history --
        which would make every gated/failed run look brand-new again on
        the next successful scan and re-fire its notification, violating
        the once-per-transition contract (E13 review round 1).
        """
        try:
            records = read_run_records()
        except Exception:
            logger.warning("Failed to read run records during TUI refresh", exc_info=True)
            return

        # Flat list sorted by recency (most-recently-started first) --
        # explicitly NOT grouped by workflow definition (Prefect lesson,
        # E7-T4). ISO 8601 timestamps sort correctly as plain strings.
        records = sorted(records, key=lambda r: r.started_at or "", reverse=True)

        table = self.query_one(DataTable)
        empty_state = self.query_one("#empty-state", Static)

        if not records:
            # First-class empty state (E7-T5): the launch affordance, not
            # an empty table.
            table.display = False
            empty_state.display = True
            table.clear()
            self._displayed_records = {}
            self._displayed_summaries = {}
            self._notifier.prune(set())
            self._update_gate_detail()
            return

        table.display = True
        empty_state.display = False

        # Preserve the operator's current selection across the rebuild --
        # otherwise every ~2s poll resets the cursor to the first row,
        # making a multi-row table effectively un-navigable.
        previous_key: str | None = None
        if table.row_count and table.cursor_coordinate is not None:
            try:
                previous_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            except Exception:
                previous_key = None

        table.clear()

        displayed: dict[str, RunRecord] = {}
        summaries: dict[str, RunSummary] = {}
        for index, record in enumerate(records):
            try:
                summary = derive_run_summary(record)
            except Exception:
                logger.warning(
                    "Failed to derive run summary for run_id=%s", record.run_id, exc_info=True
                )
                continue
            # Legacy .pid-derived records may carry an empty run_id, which
            # would collide on this row key across two such records and
            # raise DuplicateKey -- fall back to a per-refresh unique key.
            key = summary.run_id or f"_no-run-id-{index}"
            try:
                self._add_row(table, summary, record, key)
            except Exception:
                logger.warning("Failed to add row for run_id=%s", summary.run_id, exc_info=True)
                continue
            displayed[key] = record
            summaries[key] = summary
            # A run's `gate`/`failed` transition notification is keyed by
            # its real `run_id` -- a legacy blank-run_id record has no
            # stable identity across refreshes to debounce against, so it
            # is excluded rather than notifying on every poll tick.
            if summary.run_id and self._notifier.observe(summary.run_id, summary.status):
                emit_terminal_notification(self.app, _notification_message(summary))
        self._displayed_records = displayed
        self._displayed_summaries = summaries
        self._notifier.prune({r.run_id for r in records if r.run_id})

        if previous_key is not None:
            with contextlib.suppress(Exception):
                table.move_cursor(row=table.get_row_index(previous_key))

        self._update_gate_detail()

    def _add_row(self, table: DataTable, summary: RunSummary, record: RunRecord, key: str) -> None:
        """Add one run's row, formatted per the design's mockup columns."""
        table.add_row(
            _workflow_cell(summary, record),
            summary.current_step or "—",
            _format_duration(summary.total_elapsed_seconds()),
            _format_duration(summary.elapsed_on_step_seconds()),
            _format_tokens(summary.total_tokens),
            _format_cost(summary),
            str(summary.port) if summary.port is not None else "—",
            key=key,
        )

    def _selected_key(self) -> str | None:
        """Return the DataTable row key behind the currently highlighted row.

        ``None`` when the table is empty (the empty state is showing) or
        the cursor's row key can't be resolved (e.g. a stale cursor
        position mid-refresh). Shared by :meth:`_selected_record` and
        :meth:`_selected_summary` so both look up the same row.
        """
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_coordinate is None:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        return key

    def _selected_record(self) -> RunRecord | None:
        """Return the :class:`RunRecord` behind the currently highlighted row."""
        key = self._selected_key()
        if key is None:
            return None
        return self._displayed_records.get(key)

    def _selected_summary(self) -> RunSummary | None:
        """Return the :class:`RunSummary` behind the currently highlighted
        row (E13-T1/T2) -- carries ``gate``/``gate_resolvable``, which
        :attr:`_displayed_records`'s plain ``RunRecord``\\ s don't."""
        key = self._selected_key()
        if key is None:
            return None
        return self._displayed_summaries.get(key)

    def _update_gate_detail(self) -> None:
        """Show/hide and (re)render the gate-detail panel for the
        currently selected row (E13-T1).

        Called after every table rebuild (a poll tick may open/close the
        selected run's gate) and on every cursor move
        (:meth:`on_data_table_row_highlighted`), since the selected row
        can change independently of a poll tick.
        """
        panel = self.query_one("#gate-detail", Static)
        summary = self._selected_summary()
        if summary is None or summary.gate is None:
            panel.display = False
            panel.update("")
            return
        panel.display = True
        panel.update(_gate_detail_text(summary))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Refresh the gate-detail panel when the cursor moves to a
        different row -- independent of the next poll tick."""
        self._update_gate_detail()

    # -----------------------------------------------------------------
    # Run detail drill-down (E9-T1)
    # -----------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Push the run-detail screen for the selected row -- bound to
        DataTable's own ``enter``/click ``RowSelected`` message.

        A key not present in :attr:`_displayed_records` (e.g. a row
        selected in the narrow window between a poll refresh and this
        handler running) is silently ignored rather than pushing a detail
        screen for a run that may no longer exist.
        """
        key = event.row_key.value
        if key is None:
            return
        record = self._displayed_records.get(key)
        if record is None:
            return
        cast("FleetApp", self.app).push_run_detail(record)

    # -----------------------------------------------------------------
    # Dashboard action (E8-T2)
    # -----------------------------------------------------------------

    def action_open_dashboard(self) -> None:
        """Open the selected run's dashboard in a browser -- bound to ``w``.

        A portless record (``mode == "fg"``, no dashboard) disables the
        action with a visible reason (a Textual notification) rather than
        failing silently -- see
        ``conductor.fleet.tui.actions.dashboard_disabled_reason``.
        """
        record = self._selected_record()
        if record is None:
            return
        reason = dashboard_disabled_reason(record)
        if reason is not None:
            self.notify(f"Dashboard unavailable: {reason}", severity="warning")
            return
        open_dashboard(record)
        self.notify(f"Opened dashboard: {dashboard_url(record)}")

    # -----------------------------------------------------------------
    # Kill / kill-all actions (E8-T3)
    # -----------------------------------------------------------------

    @work
    async def action_kill(self) -> None:
        """Kill the selected run -- bound to ``k``. Always confirms first (D1)."""
        record = self._selected_record()
        if record is None:
            return
        await self._kill_and_refresh([record])

    @work
    async def action_kill_all(self) -> None:
        """Kill every displayed run -- bound to ``K``. Confirms exactly once (D1)."""
        targets = list(self._displayed_records.values())
        if not targets:
            return
        await self._kill_and_refresh(targets)

    async def _kill_and_refresh(self, targets: list[RunRecord]) -> None:
        """Confirm and kill ``targets`` via the shared implementation, then
        immediately refresh the table so a killed run disappears without
        waiting out the next ~2s poll tick."""
        outcome = await kill_runs(self.app, targets)
        if outcome.declined:
            self.notify("Kill cancelled.", severity="warning")
            return
        self.notify(f"Killed {len(outcome.stopped)} run(s).")
        self.refresh_runs()

    # -----------------------------------------------------------------
    # Gate resolution (D4, E13-T2/E13-T3)
    # -----------------------------------------------------------------

    @work
    async def action_resolve_gate(self) -> None:
        """Resolve the selected run's open gate -- bound to ``g``.

        A row with no open gate is a silent no-op (nothing to resolve).
        A ``mode == "fg"`` gate (``gate_resolvable is False``) is
        display-only by D4 -- its blocked ``Prompt.ask`` thread cannot be
        reached remotely, so the action is disabled with the PID-bearing
        reason visible via notification, never attempted (E13-T3).
        Otherwise presents the gate's options and posts the selection via
        the shared ``conductor gate respond`` HTTP path
        (``conductor.fleet.tui.actions.resolve_gate``, E13-T2); any
        failure -- including the underlying HTTP call raising
        ``typer.Exit`` -- surfaces as an in-UI notification rather than
        propagating.

        Guarded by :attr:`_resolving_gate` against a second, concurrent
        ``g`` press starting a duplicate resolution while one is already
        in flight (E13 review round 1) -- this is a non-exclusive
        ``@work`` method, so without the guard a rapid double-press could
        open two option modals for the same gate.
        """
        if self._resolving_gate:
            return
        record = self._selected_record()
        summary = self._selected_summary()
        if record is None or summary is None or summary.gate is None:
            self.notify("No gate is currently open for this run.", severity="warning")
            return

        reason = gate_resolve_disabled_reason(record)
        if reason is not None:
            self.notify(f"Cannot resolve gate here: {reason}", severity="warning")
            return

        self._resolving_gate = True
        try:
            outcome: GateResolveOutcome | None = await resolve_gate(self.app, record, summary.gate)
        finally:
            self._resolving_gate = False
        if outcome is None:
            self.notify("Gate resolution cancelled.", severity="warning")
            return
        if outcome.success:
            self.notify(outcome.message)
        else:
            self.notify(f"Gate resolution failed: {outcome.message}", severity="error")
        self.refresh_runs()
