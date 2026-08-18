"""The Run detail screen for the Fleet Manager TUI (Fleet Manager E9).

Pushed on ``enter`` from the Runs screen's table, popped on ``escape`` back
to Runs via the real ``Screen`` push/pop stack established in E7-T3 (E9-T1,
E9-T4). Renders the run's topology (from ``workflow_started``, via E6-T5)
as a discrete per-agent step list — per the design's *Patterns adopted from
prior art*, discrete steps rather than scrolling text, and explicitly
**not a DAG** (per the design's *Non-goals*, also restated by this epic:
no DAG rendering, no agent messages, no tool output — only status, elapsed,
tokens, and cost per agent, with the current step highlighted (E9-T2)).

Unlike the Runs screen (which stays on the bounded tail-window read so its
~2s poll never grows with a run's age), this screen uses the bounded
**full**-log read (:func:`conductor.fleet.summary.derive_run_detail`,
E9-T3) so every agent's complete history is available, not just whichever
agents happen to still be inside the tail window.

Both derivations run in a single worker thread (:func:`asyncio.to_thread`),
not on the event loop, so the ~2s poll's larger read (a full-log scan on
top of the Runs screen's bounded tail read) never blocks keypresses or the
footer repaint (issue #437); a tick that overruns the poll interval is
dropped rather than queued.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, ClassVar, cast

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Static

from conductor.console import styled
from conductor.fleet.records import RunRecord
from conductor.fleet.summary import (
    AgentDetail,
    RunDetail,
    RunSummary,
    derive_run_detail,
    derive_run_summary,
)
from conductor.fleet.tui.theme import loading_text, muted, status_label
from conductor.fleet.tui.widgets import highlighted_row_key

if TYPE_CHECKING:
    # app.py imports this module, so a top-level import would cycle.
    from conductor.fleet.tui.app import FleetApp

logger = logging.getLogger(__name__)


def _agent_status_cell(status: str) -> Text:
    """Render an agent's status, deferring to the shared vocabulary.

    ``pending`` is rendered dim here rather than added to the shared map:
    it describes a step that has not started, which is an agent-level idea
    with no run-level equivalent, so putting it in the run-status
    vocabulary would make that map mean two different things.
    """
    if status == _PENDING_STATUS:
        return muted(status)
    return status_label(status)


# Agent-status badges/colours come from `tui/theme.py` (shared with Runs and
# History). "pending" is local: it is an agent-level state with no run-level
# counterpart, so it has no entry in the shared run-status vocabulary.
_PENDING_STATUS = "pending"

_PLACEHOLDER_TEXT = (
    "[bold]No topology available for this run.[/bold]\n\n"
    "The event log may be missing, unreadable, or hasn't recorded a "
    "workflow_started event yet.\n\n"
    "[dim]Press escape to go back.[/dim]"
)


def _format_duration(seconds: float | None) -> str:
    """Render an elapsed duration compactly (``1h04``, ``18m``, ``42s``).

    Duplicated from ``conductor.fleet.tui.screens.runs`` (rather than
    imported) to keep each screen module's rendering self-contained --
    matching that module's own precedent of owning its formatting helpers.
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


def _format_agent_tokens(tokens: int | None) -> str:
    """Render a single agent's token count, or ``"—"`` when unavailable
    (pending/running/failed rows carry no token count — per D5, there is
    no mid-flight or failure-path usage event)."""
    if tokens is None or tokens <= 0:
        return "—"
    if tokens >= 1000:
        return f"{tokens / 1000:.0f}k tok"
    return f"{tokens} tok"


def _format_agent_cost(cost_usd: float | None) -> str:
    """Render a single agent's cost, or ``"—"`` when unavailable (no
    completion yet, or the model was unpriced)."""
    if cost_usd is None:
        return "—"
    return f"~${cost_usd:.2f}"


def _collect_detail(record: RunRecord) -> tuple[RunSummary | None, RunDetail | None]:
    """Derive ``record``'s summary and detail in one call, off the event loop.

    The I/O half of :meth:`RunDetailScreen.refresh_detail` (issue #437) --
    touches no widget, so it is safe to run in a worker thread via
    :func:`asyncio.to_thread` and directly unit-testable. Both derivations
    are done here together (rather than as two separate ``to_thread`` hops)
    so one poll tick costs one thread round trip.

    Preserves the original per-derivation ``try/except`` + ``logger.warning``
    -> ``None`` behaviour verbatim: a failure deriving one does not prevent
    the other from being returned.
    """
    try:
        summary = derive_run_summary(record)
    except Exception:
        logger.warning("Failed to derive run summary for run_id=%s", record.run_id, exc_info=True)
        summary = None

    try:
        detail = derive_run_detail(record)
    except Exception:
        logger.warning("Failed to derive run detail for run_id=%s", record.run_id, exc_info=True)
        detail = None

    return summary, detail


class RunDetailScreen(Screen):
    """Per-run detail: topology as discrete per-agent rows, current step
    highlighted. Not a DAG; no agent messages; no tool output (E9-T2)."""

    POLL_INTERVAL_SECONDS: ClassVar[float] = 2.0
    """Matches ``RunsScreen.POLL_INTERVAL_SECONDS`` -- a class attribute
    (rather than a hardcoded literal in :meth:`on_mount`) so tests can
    shrink it and observe a real poll tick pick up new events without
    waiting out the full interval."""

    BINDINGS = [
        # Row-scoped -- operates on the highlighted agent. `priority` is
        # required or `DataTable`'s hidden `enter` shadows it in the
        # footer; same reasoning as `runs.py`'s `BINDINGS` comment, issue #459.
        Binding("enter", "open_step", "Step", priority=True),
        ("escape", "back", "Back"),
    ]

    def __init__(self, record: RunRecord) -> None:
        super().__init__()
        self._record = record
        self._poll_timer: Timer | None = None
        # Seeded from the record (the workflow file's stem) and upgraded to
        # the log's declared name as soon as the detail read supplies it,
        # so the title agrees with the Runs and History screens.
        self._display_name = record.workflow_name
        self._refreshing = False
        """Guards against a poll tick starting a second, concurrent
        ``_refresh_worker`` while one is already awaiting its
        ``asyncio.to_thread`` scan (issue #437) -- an overrunning refresh
        drops the next tick rather than queueing it, matching
        ``RunsScreen``'s identical guard."""

    def _update_title(self, title: Static) -> None:
        """Render the heading from the current best-known workflow name."""
        title.update(styled("[bold]{}[/bold] ({})", self._display_name, self._record.run_id or ""))

    def action_back(self) -> None:
        """Pop back to the Runs screen -- bound to ``escape`` (E9-T1)."""
        self.app.pop_screen()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the step drill-down for the selected agent (mouse click).

        The table says whether a step succeeded and what it cost; this is
        how you see what it actually *did* -- the prompt it was given and
        the output it produced (or, while it is still running and has no
        output yet, what it has been doing).
        """
        key = event.row_key.value
        if key is None:
            return
        self._push_step_for(self._agent_name_from_row_key(key))

    def _agent_name_from_row_key(self, key: str) -> str:
        """Strip a row's per-refresh index back off to recover the agent name.

        Row keys are ``<agent-name>-<index>`` (agent names are not unique:
        two for-each iterations, or a loop-back reusing an agent id, can
        legitimately share one -- see :meth:`_render_detail`), so the
        trailing index is stripped back off here.
        """
        return key.rsplit("-", 1)[0]

    def _selected_agent_name(self) -> str | None:
        """Return the agent name (not the raw row key) behind the currently
        highlighted row.

        ``None`` when the table is empty or the cursor's row key can't be
        resolved -- mirrors ``runs.py``'s ``_selected_key``.
        """
        key = highlighted_row_key(self.query_one(DataTable))
        if key is None:
            return None
        return self._agent_name_from_row_key(key)

    def action_open_step(self) -> None:
        """Open the highlighted agent's step detail -- the ``enter`` binding.

        This is a ``priority`` binding, so it runs *ahead* of ``DataTable``'s
        own hidden ``enter`` (``select_cursor``) and the keypress never
        becomes a ``RowSelected`` message. Mouse clicks still arrive that way
        and land in :meth:`on_data_table_row_selected`; both funnel through
        :meth:`_push_step_for`, so keyboard and mouse each take exactly one
        path and ``enter`` cannot push two screens.
        """
        agent_name = self._selected_agent_name()
        if agent_name is None:
            return
        self._push_step_for(agent_name)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide ``open_step`` while no agent row is selected (e.g. the
        placeholder is showing, or the table hasn't loaded yet), so the
        footer never advertises a key that does nothing."""
        if action == "open_step":
            return self._selected_agent_name() is not None
        return True

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Refresh the footer when the cursor moves to a different row."""
        self.refresh_bindings()

    def _push_step_for(self, agent_name: str) -> None:
        """Push the step-detail screen for ``agent_name``."""
        cast("FleetApp", self.app).push_step_detail(self._record, agent_name)

    def _update_inputs(self, summary: RunSummary | None) -> None:
        """Show the values this run was launched with, when the log has them.

        Two runs of the same workflow are otherwise distinguishable only by
        id; the inputs are what actually say which is which.
        """
        panel = self.query_one("#run-inputs", Static)
        inputs = summary.inputs if summary is not None else None
        if not inputs:
            panel.display = False
            panel.update("")
            return
        text = Text()
        text.append("Inputs", style="bold")
        for name, value in inputs.items():
            text.append("\n  ")
            text.append(f"{name} ", style="dim")
            text.append(str(value))
        panel.display = True
        panel.update(text)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="detail-title")
        yield Static(id="run-inputs")
        yield Static(loading_text(), id="detail-loading", classes="notice")
        yield DataTable(id="detail-table")
        yield Static(_PLACEHOLDER_TEXT, id="detail-placeholder")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Agent", "Type", "Status", "Elapsed", "Tokens", "Cost")
        table.cursor_type = "row"
        self.query_one("#run-inputs", Static).display = False
        # Hidden until the first collector result lands (issue #437); the
        # title is still painted immediately from the record, since it
        # needs no I/O -- "seeded from the record, then corrected" matching
        # `step_detail.py`'s convention.
        table.display = False
        self.query_one("#detail-placeholder", Static).display = False
        self._update_title(self.query_one("#detail-title", Static))
        self.refresh_detail()
        self._poll_timer = self.set_interval(self.POLL_INTERVAL_SECONDS, self.refresh_detail)

    def on_unmount(self) -> None:
        """Stop polling once this screen is popped -- otherwise the timer
        would keep firing (and querying widgets torn down with the screen)
        after the user has navigated back to the Runs screen."""
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None

    def refresh_detail(self) -> None:
        """(Re-)derive this run's detail and repopulate the table (or show
        the placeholder).

        A **synchronous dispatcher** (issue #437), with the same shape and
        the same rationale as
        :meth:`~conductor.fleet.tui.screens.runs.RunsScreen.refresh_runs`
        -- including why :attr:`_refreshing` is set here rather than as the
        worker's first line. Called from ``on_mount`` and ``set_interval``,
        neither of which awaits, so it stays synchronous. This screen has
        no explicit-refresh caller, so it needs no ``_refresh_pending``
        counterpart.
        """
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self._refresh_worker()
        except Exception:
            # The flag is set before the worker exists, so a failure to
            # dispatch would otherwise latch it True and silently stop this
            # screen refreshing for the rest of the session.
            self._refreshing = False
            raise

    @work
    async def _refresh_worker(self) -> None:
        """Do both derivations off the event loop, then render on it.

        Best-effort: a failure deriving detail is logged and treated the
        same as "no topology available" (E9-T5) rather than crashing the
        screen.
        """
        try:
            summary, detail = await asyncio.to_thread(_collect_detail, self._record)
            try:
                self._render_detail(summary, detail)
            except Exception:  # noqa: BLE001 - a render bug must not exit the app
                logger.warning("Failed to render run detail", exc_info=True)
        finally:
            self._refreshing = False

    def _render_detail(self, summary: RunSummary | None, detail: RunDetail | None) -> None:
        """Repopulate the table (or the placeholder) from a completed derivation.

        The render half of :meth:`refresh_detail` (issue #437) -- runs on
        the event loop, after the collector's ``asyncio.to_thread`` hop.

        An open gate is deliberately *not* repeated here. The Runs screen's
        preview already shows it along with the key that answers it, and
        repeating it above this table pushed the per-agent rows -- the
        reason to open this screen at all -- below the fold on a gated run.
        """
        title = self.query_one("#detail-title", Static)
        table = self.query_one(DataTable)
        placeholder = self.query_one("#detail-placeholder", Static)
        self.query_one("#detail-loading", Static).display = False

        self._update_inputs(summary)

        if detail is not None and detail.workflow_name:
            self._display_name = detail.workflow_name
        self._update_title(title)

        if detail is None or detail.topology is None or not detail.agents:
            # A missing/unreadable event log, or one with no (yet-visible)
            # workflow_started event, degrades gracefully to a placeholder
            # (E9-T5) rather than an empty table or a crash.
            table.display = False
            placeholder.display = True
            table.clear()
            self.refresh_bindings()
            return

        table.display = True
        placeholder.display = False

        # Preserve the cursor across the rebuild. Without this every poll
        # tick reset it to row 0, so holding `down` on a long agent list
        # fought the refresh and snapped back to the top -- the same
        # protection `runs.py` already applies to its own table.
        previous_row = table.cursor_coordinate.row if table.row_count else 0

        table.clear()

        for index, agent in enumerate(detail.agents):
            # `agent.name` is not guaranteed unique: `conductor validate`
            # currently accepts a `workflow_started` topology with two
            # agents sharing a name (e.g. two for-each iterations, or a
            # loop-back reusing an agent id). Suffix with the per-refresh
            # index so the row key is always unique, and guard the row
            # add itself (mirroring `runs.py`'s per-row try/except) so one
            # bad agent entry can never take the screen down.
            key = f"{agent.name}-{index}"
            try:
                self._add_row(table, agent, key)
            except Exception:
                logger.warning("Failed to add detail row for agent=%s", agent.name, exc_info=True)
                continue

        if previous_row and table.row_count:
            with contextlib.suppress(Exception):
                table.move_cursor(row=min(previous_row, table.row_count - 1))

        # The row set just changed shape (agents complete, fail, or start
        # running on every poll tick), so the footer's `enter` label needs
        # re-evaluating the same way a cursor move would.
        self.refresh_bindings()

    def _add_row(self, table: DataTable, agent: AgentDetail, key: str) -> None:
        """Add one agent's row: status, elapsed, tokens, cost -- the
        currently-running agent's row is visually highlighted (E9-T2)."""
        is_current = agent.status == "running"
        # `Text`, not an f-string: `DataTable` runs `Text.from_markup` on any
        # `str` cell unconditionally, and `agent.name`/`agent.type` are
        # workflow-authored -- an agent named `plan[wip]` would render as
        # `plan`. The `[bold]` below is conductor's own, so it is applied as
        # a style rather than as markup.
        name_cell = Text(f"▶ {agent.name}" if is_current else f"  {agent.name}")
        if is_current:
            name_cell.stylize("bold")
        table.add_row(
            name_cell,
            Text(agent.type),
            _agent_status_cell(agent.status),
            Text(_format_duration(agent.elapsed_seconds())),
            Text(_format_agent_tokens(agent.tokens)),
            Text(_format_agent_cost(agent.cost_usd)),
            key=key,
        )
