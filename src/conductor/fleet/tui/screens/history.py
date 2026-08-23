"""The History screen for the Fleet Manager TUI (Fleet Manager E14).

Lists completed runs from retained event logs
(:func:`conductor.fleet.history.build_history_entries`) -- workflow,
outcome, duration, tokens, cost -- per the design's *Screens* section:
"History — completed runs, subject to retention." Per *What single-user
removes*, there is deliberately no long-horizon audit history and no
pagination: the list is exactly as bounded as
``build_history_entries``'s own retention-driven cap, nothing more.

**Depth is delegated for viewing, not for resuming** (E14-T3, the design's
*Division of labor*: TUI = breadth, dashboard/replay = depth). Selecting a
row does not open any viewer inside this screen -- it surfaces the exact
``conductor replay <log>`` command for that run's event log via a Textual
notification, so the operator runs it themselves in a terminal. This
avoids taking on a second, TUI-owned subprocess/dashboard lifecycle for a
screen whose whole job is a retrospective list, not live supervision.

**Resume is the one exception** (issue #460): pressing ``r`` on a row that
correlates to an on-disk checkpoint (:mod:`conductor.fleet.resume`) launches
a resume of that run through the exact same ``launch_background_resume``
path ``conductor resume --web-bg`` uses (:func:`conductor.fleet.launch.
launch_resume`), then unwinds to Runs -- mirroring how
:class:`~conductor.fleet.tui.screens.new_run.NewRunScreen` launches a fresh
run. This is not a re-implementation of "depth": replay is a *viewer* and
stays delegated to a separate terminal invocation, while resume is an
*action* this screen already has the precedent for, the same way Runs kills
processes and resolves gates rather than delegating those to a shell
command. ``r`` is bound via :meth:`check_action`, which is checkpoint-driven
and never consults ``outcome`` -- see :mod:`conductor.fleet.resume`'s module
docstring for why an already-``completed`` row can offer Resume too.

Loaded once on mount, like the Providers/Registries screens (E10/E11) --
unlike the Runs/run-detail screens' live ~2s poll, a completed run's
history does not change once written, so there is no live state here to
keep refreshed on a timer.

The read is bounded by ``min(keep_last, _MAX_HISTORY_ENTRIES)`` -- the
200-entry display cap is independent of retention, so raising
``keep_last`` cannot grow this screen -- and happens off the event loop,
in a worker thread (issue #437); how much of each log is read once
retrieved is issue #436's concern, not this one. The checkpoint
correlation (:func:`conductor.fleet.resume.correlate_checkpoints`) runs the
same way, as a second thread hop in the same load, so ``r`` becomes
available a beat after the table itself paints rather than blocking it.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from conductor.console import styled
from conductor.fleet.history import HistoryEntry, build_history_entries
from conductor.fleet.launch import LaunchError, launch_resume
from conductor.fleet.resume import ResumableCheckpoint, correlate_checkpoints
from conductor.fleet.tui.actions import report_background_launch
from conductor.fleet.tui.theme import loading_text, status_label
from conductor.fleet.tui.widgets import highlighted_row_key

if TYPE_CHECKING:
    # The app module imports this screen module, so a top-level import of
    # FleetApp here would cycle (same reason new_run.py defers it).
    from conductor.fleet.tui.app import FleetApp

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

    BINDINGS = [
        # Row-scoped -- surfaces the replay command for the highlighted row.
        # `priority` is required or `DataTable`'s hidden `enter` shadows it
        # in the footer; same reasoning as `runs.py`'s `BINDINGS` comment,
        # issue #459.
        Binding("enter", "show_replay_command", "Replay cmd", priority=True),
        ("r", "resume", "Resume"),
        ("escape", "back", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._displayed_entries: dict[str, HistoryEntry] = {}
        """Maps each DataTable row key back to the full
        :class:`~conductor.fleet.history.HistoryEntry` behind that row --
        the table itself only renders formatted cell text, so the
        row-selection handler (the replay-command action) needs this to
        recover the entry's log path."""
        self._resumable: dict[Path, ResumableCheckpoint] = {}
        """Maps a :class:`~conductor.fleet.history.HistoryEntry.path` to
        the :class:`~conductor.fleet.resume.ResumableCheckpoint` that
        correlates to it, populated by :meth:`load_history`'s checkpoint
        scan. Empty until that scan lands, and permanently empty if it
        fails (see :meth:`load_history`) -- either way, an absent entry
        simply means Resume is not offered for that row, never a crash."""
        self._resuming = False
        """Synchronous guard against a second Resume keystroke starting a
        duplicate (potentially billable) background run while one launch
        is already in flight -- mirrors ``NewRunScreen._launching``. A
        fresh :class:`HistoryScreen` is constructed on every
        ``push_history()``, so this can never go stale across visits."""

    def action_back(self) -> None:
        """Pop back to the Runs screen -- bound to ``escape``."""
        self.app.pop_screen()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(loading_text(), id="history-loading", classes="notice")
        yield DataTable(id="history-table")
        yield Static(_EMPTY_STATE_TEXT, id="history-empty-state")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Workflow", "Outcome", "Duration", "Tokens", "Cost")
        table.cursor_type = "row"
        # Hidden until the load lands (issue #437) -- this is the screen
        # where it matters most: up to `keep_last` (default 200) logs are
        # read before anything else can paint.
        table.display = False
        self.query_one("#history-empty-state", Static).display = False
        self.load_history()

    @work
    async def load_history(self) -> None:
        """(Re-)build the history list and populate the table, off the event
        loop (issue #437).

        One-shot: called only from ``on_mount`` (this screen has no refresh
        binding and no poll), so unlike ``runs.py``/``run_detail.py`` it
        needs no re-entrancy guard. Add one before wiring any reload key.

        A failure building the list is **not** degraded into "no history".
        ``build_history_entries`` is documented never to raise, so if it
        does, rendering the empty state would make a positive claim of
        absence -- and because this screen loads once, that claim is
        permanent for its lifetime and the operator stops looking (issue
        #446 review). Surfaced as an error line instead, matching
        ``step_detail.py``/``registries.py``'s convention for this same
        worker pattern.
        """
        loading = self.query_one("#history-loading", Static)
        try:
            entries = await asyncio.to_thread(build_history_entries)
        except Exception as e:  # noqa: BLE001 - surfaced, not crashed
            logger.warning("Failed to build run history", exc_info=True)
            loading.update(styled("[red]Could not read run history:[/red] {}", str(e)))
            loading.display = True
            self.notify("Could not read run history.", severity="error", markup=False)
            return

        try:
            self._render_history(entries)
        except Exception:  # noqa: BLE001 - a render bug must not exit the app
            # Matches the guard on the two polled screens: a ``@work``
            # method defaults to ``exit_on_error``, so an exception here
            # would take the whole TUI down rather than log a warning.
            logger.warning("Failed to render run history", exc_info=True)
            return

        # A second, independent thread hop for the checkpoint correlation
        # (issue #460) -- kept separate from the history-entries load above
        # so a checkpoint-scan failure is caught on its own and never blanks
        # an otherwise-successful history listing (the "failed load
        # degraded into an empty state" mistake this screen's own docstring
        # already calls out, applied to Resume rather than to the list
        # itself). The table has already been rendered by the time this
        # runs, so `r` simply isn't offered during this window -- exactly
        # what `check_action` gating already means.
        try:
            self._resumable = await asyncio.to_thread(correlate_checkpoints, entries)
        except Exception:  # noqa: BLE001 - surfaced, not crashed
            logger.warning("Failed to correlate checkpoints for history", exc_info=True)
            self._resumable = {}
            self.notify(
                "Could not read checkpoints; Resume is unavailable.",
                severity="warning",
                markup=False,
            )
            return
        self.refresh_bindings()

    def _render_history(self, entries: list[HistoryEntry]) -> None:
        """Repopulate the table (or the empty state) from a completed load."""
        table = self.query_one(DataTable)
        empty_state = self.query_one("#history-empty-state", Static)
        self.query_one("#history-loading", Static).display = False
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
        # The row set just changed shape (a load can go from zero rows to
        # many, or vice versa on a rebuild), so the footer's `enter` label
        # needs to be re-evaluated the same way a cursor move would --
        # this screen has no poll timer, so nothing else would trigger it.
        self.refresh_bindings()

    def _add_row(self, table: DataTable, entry: HistoryEntry, key: str) -> None:
        """Add one history entry's row: workflow, outcome, duration, tokens, cost."""
        table.add_row(
            Text(entry.workflow_name),
            status_label(entry.outcome),
            # `Text`, not `str`: `DataTable` markup-parses every `str` cell.
            # These format helpers cannot emit a bracket today, but the sink
            # is the same one that silently truncated agent names.
            Text(_format_duration(entry.duration_seconds)),
            Text(_format_tokens(entry.total_tokens)),
            Text(_format_cost(entry)),
            key=key,
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Offer ``conductor replay <log>``, plus the outcome detail the
        design says this screen "currently cannot show" (E14-T3, E4-T5).

        Depth is still delegated, not re-implemented: this never opens a
        replay dashboard or a separate output viewer -- it surfaces the
        failure reason (for a failed run) or the rendered output (for a
        completed one), alongside the exact replay command, via a single
        notification so the operator can inspect further in a terminal.
        This keeps the table's five columns unchanged and is the only
        place the E4-T4 enrichment (`HistoryEntry.output`/`error_type`/
        `error_message`) surfaces in this screen. A key not present in
        :attr:`_displayed_entries` (e.g. a row selected in the narrow
        window between a refresh and this handler running) is silently
        ignored, mirroring ``runs.py``'s identical guard for its own
        row-selection handler.

        When the row also correlates to a checkpoint, the same
        notification names its provenance (``created_at``/``current_agent``)
        and that ``r`` resumes it -- a single call, not a second
        notification, since exactly one call is asserted in
        ``test_tui_history.py``.
        """
        key = event.row_key.value
        if key is None:
            return
        self._notify_replay_for(key)

    def action_show_replay_command(self) -> None:
        """Surface the replay command for the highlighted row -- the
        ``enter`` binding (E14-T3).

        This is a ``priority`` binding, so it runs *ahead* of ``DataTable``'s
        own hidden ``enter`` (``select_cursor``) and the keypress never
        becomes a ``RowSelected`` message. Mouse clicks still arrive that way
        and land in :meth:`on_data_table_row_selected`; both funnel through
        :meth:`_notify_replay_for`, so keyboard and mouse each take exactly
        one path and ``enter`` cannot notify twice.
        """
        key = self._selected_history_key()
        if key is None:
            return
        self._notify_replay_for(key)

    def _selected_history_key(self) -> str | None:
        """Return the DataTable row key behind the currently highlighted row.

        ``None`` when the table is empty or the cursor's row key can't be
        resolved -- mirrors ``runs.py``'s ``_selected_key``.
        """
        return highlighted_row_key(self.query_one(DataTable))

    def _notify_replay_for(self, key: str) -> None:
        """Notify the replay command for ``key``, if it still resolves.

        Depth is delegated, not re-implemented: this never opens a replay
        dashboard itself -- it surfaces the exact command via a
        notification so the operator can run it in a terminal (per the
        design's *Division of labor*, TUI = breadth).
        """
        entry = self._displayed_entries.get(key)
        if entry is None:
            return
        lines = [f"Replay with: conductor replay {entry.path}"]
        checkpoint = self._resumable.get(entry.path)
        if checkpoint is not None:
            lines[0] += (
                f"  ·  Press r to resume {checkpoint.workflow_path.name} from the "
                f"checkpoint saved {checkpoint.created_at} at {checkpoint.current_agent}."
            )
        if entry.outcome == "failed" and (entry.error_type or entry.error_message):
            reason = entry.error_message or "no message recorded"
            if entry.error_type:
                reason = f"{entry.error_type}: {reason}"
            lines.insert(0, f"Failed: {reason}")
        elif entry.outcome == "completed" and entry.output:
            lines.insert(0, f"Output: {entry.output}")

        self.notify("\n".join(lines), markup=False)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Refresh the footer as the cursor moves (issues #459, #460).

        Without this, ``check_action``'s ``show_replay_command``/``resume``
        gates keep reporting whichever row was selected when the footer was
        last built -- the exact reason ``runs.py::_refresh_row_bindings``
        exists for its own row-scoped bindings.
        """
        self.refresh_bindings()

    def _selected_entry(self) -> HistoryEntry | None:
        """Return the :class:`~conductor.fleet.history.HistoryEntry` behind
        the currently highlighted row, modelled on ``runs.py::_selected_key``
        / ``_selected_record``.

        ``None`` when the table is empty, the cursor position can't be
        resolved (e.g. a stale cursor mid-refresh), or the resolved key
        isn't (yet) in :attr:`_displayed_entries`.
        """
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_coordinate is None:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        if key is None:
            return None
        return self._displayed_entries.get(key)

    def _resume_target(self) -> ResumableCheckpoint | None:
        """Return the checkpoint that would be resumed by pressing ``r`` now,
        or ``None`` if the highlighted row has none."""
        entry = self._selected_entry()
        if entry is None:
            return None
        return self._resumable.get(entry.path)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide ``show_replay_command`` while no row is selected (e.g. an
        empty or still-loading table), and hide ``r``/Resume when the
        highlighted row has no checkpoint (issue #460), so the footer
        never advertises a key that does nothing.

        Mirrors ``runs.py::check_action``'s ``g``/Gate reasoning: a key that
        can't currently do anything is hidden from the footer outright
        rather than shown disabled. Resume gating is checkpoint-driven
        only -- never ``outcome`` -- so this returns ``True`` for a
        ``completed`` row with a correlated checkpoint exactly as it would
        for an ``unknown`` or ``failed`` one (see ``conductor.fleet.resume``'s
        module docstring for why).
        """
        if action == "show_replay_command":
            return self._selected_history_key() is not None
        if action == "resume":
            return self._resume_target() is not None
        return True

    @work
    async def action_resume(self) -> None:
        """Resume the highlighted row's checkpoint in the background --
        bound to ``r`` (issue #460).

        Mirrors ``NewRunScreen.action_launch``: run as an awaited worker
        (``launch_resume`` blocks on ``launch_background_resume()``'s own
        dashboard-reachability and D2 run-record-poll waits, each up to
        15s), guarded by :attr:`_resuming` against a second keystroke
        starting a duplicate, potentially billable run while one launch is
        already in flight. The guard is not cleared on success -- the
        screen is gone by then (unwound to Runs).
        """
        if self._resuming:
            return

        target = self._resume_target()
        if target is None:
            # Defensive: the key is hidden via `check_action` whenever
            # there is nothing to resume, so this should be unreachable in
            # practice.
            self.notify("No checkpoint found for this run.", severity="warning", markup=False)
            return

        self._resuming = True
        self.notify(
            f"Resuming {target.workflow_path.name} from checkpoint saved "
            f"{target.created_at} at {target.current_agent}…",
            markup=False,
        )

        cwd = cast("FleetApp", self.app).launch_dir

        try:
            launch = await asyncio.to_thread(
                partial(launch_resume, target.checkpoint_path, cwd=cwd)
            )
        except LaunchError as e:
            logger.warning("Failed to resume checkpoint %s", target.checkpoint_path, exc_info=True)
            self.notify(str(e), severity="error", markup=False)
            self._resuming = False
            return

        # Success: hand off to the Runs screen, whose own poll timer will
        # pick up the resumed run's record -- this screen never tracks the
        # launched run itself (viewer, not supervisor). Unwinds to Runs
        # rather than popping one level, mirroring `NewRunScreen`'s own
        # `return_to_runs()` call.
        cast("FleetApp", self.app).return_to_runs()
        # `still_running`/`workflow_started`/`run_record_written` are all
        # checked, in that order, by the shared helper (issue #410/#435) --
        # never report a URL for a process that has already exited.
        report_background_launch(self.app, launch, verb="Resumed")
