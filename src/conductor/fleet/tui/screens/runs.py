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

That scan runs in a worker thread (:func:`asyncio.to_thread`), not on the
event loop, so a large fleet's I/O never blocks keypresses or the footer
repaint (issue #437). A tick arriving while the previous scan is still
running is dropped rather than started alongside it -- the in-flight scan
wins and the newer tick is skipped, matching the
``_resolving_gate``/``_opening_dashboard`` guards' "first one wins, a
second is not started" convention. An *explicit* refresh request (after a
kill, or after a gate is resolved) is coalesced rather than dropped: see
:attr:`RunsScreen._refresh_pending`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import DataTable, Header, Static

from conductor.console import styled
from conductor.fleet.records import RunRecord, read_run_records
from conductor.fleet.summary import (
    GateInfo,
    RunSummary,
    derive_run_summary,
)
from conductor.fleet.tui.actions import (
    GateResolveOutcome,
    dashboard_disabled_reason,
    dashboard_url,
    gate_resolve_disabled_reason,
    kill_runs,
    open_dashboard,
    resolve_gate,
)
from conductor.fleet.tui.anim import (
    FRAME_INTERVAL,
    animations_enabled,
    breath_style,
    progress_bar,
    sparkline,
    spinner,
)
from conductor.fleet.tui.art import EMPTY_STAGE
from conductor.fleet.tui.dag import render_score, step_statuses
from conductor.fleet.tui.notify import TransitionNotifier, emit_terminal_notification
from conductor.fleet.tui.theme import (
    EMPTY,
    empty_cell,
    loading_text,
    mode_label,
    muted,
    status_badge,
    status_style,
)
from conductor.fleet.tui.widgets import BlockFooter, highlighted_row_key

if TYPE_CHECKING:
    # Guarded to avoid a runtime circular import: app.py imports RunsScreen
    # from this module, so a top-level import of FleetApp here would cycle.
    from conductor.fleet.tui.app import FleetApp

logger = logging.getLogger(__name__)

# Status badges/colours live in `tui/theme.py` -- one vocabulary shared with
# the History and run-detail screens, rather than the three overlapping
# glyph maps these screens each used to define.

#: Statuses whose badge moves. Everything else is repainted only by the
#: data poll, so a fleet of finished runs costs nothing to display.
_ANIMATED_STATUSES = frozenset({"running", "at-gate"})

#: Cells in the Burn sparkline. Sized to be readable in a table column
#: without crowding the numeric columns either side of it.
_BURN_WIDTH = 10

#: How many lines the flowed progress view may occupy in the preview before
#: the remaining steps are summarised. Run-detail renders all of them.
_SCORE_MAX_LINES = 3

#: Lines the gate section spends on things that are not prompt text: its
#: heading, the truncation marker, the options line, and the "press g" hint.
#: Subtracted from the pane's height to leave the prompt everything else.
_GATE_CHROME_LINES = 4

#: Horizontal padding the preview pane's CSS costs the content's usable width.
_PREVIEW_PADDING = 4

#: Vertical padding (top + bottom) the preview pane's CSS costs it.
_PREVIEW_VERTICAL_PADDING = 2

_NEXT_GATE_POLL_SECONDS = 0.4
"""How often to re-read a run's log while waiting for its next question."""

# Built here rather than at the call site so ``Text.from_markup`` receives a
# string literal: the markup guard (rule C) cannot prove a *name* holds one,
# and the point of that rule is that a non-literal may carry runtime data.
# The art is composed as `Text` rather than concatenated into the markup
# string: `Text.from_markup` takes a literal only (markup guard rule C), and
# a bracket in the art would otherwise be parsed as a style tag.
_EMPTY_STATE_TEXT = Text(EMPTY_STAGE, style="dim cyan") + Text.from_markup(
    "\nLaunch one with  [cyan]conductor run <workflow.yaml> --web-bg[/cyan]\n"
    "or press  [cyan]n[/cyan]  to start one from here.\n\n"
    "[dim]p providers · r registries · h history · q quit[/dim]"
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


def _started_cell(started_at: str | None) -> Text:
    """Render a run's launch time as local ``HH:MM`` (or ``MM-DD HH:MM``).

    Times are stored UTC and shown local: a fleet is read by the person
    sitting at the machine, for whom "was this the run I kicked off before
    lunch" is the actual question.
    """
    if not started_at:
        return empty_cell()
    try:
        moment = datetime.fromisoformat(started_at).astimezone()
    except (TypeError, ValueError):
        return empty_cell()
    now = datetime.now().astimezone()
    if moment.date() == now.date():
        return Text(moment.strftime("%H:%M"), style="dim")
    return Text(moment.strftime("%m-%d %H:%M"), style="dim")


def _directory_cell(cwd: str | None) -> Text:
    """Render the directory conductor was launched from, home-shortened.

    Two runs of the same workflow are otherwise indistinguishable in the
    table; in practice they are usually the *same* workflow against
    different checkouts, which is exactly what this column separates.
    """
    if not cwd:
        return empty_cell()
    home = str(Path.home())
    shown = f"~{cwd[len(home) :]}" if cwd.startswith(home) else cwd
    return Text(shown, style="dim")


def _dim_if_empty(value: str) -> Text:
    """Render a formatted cell, dimming it when it is only a placeholder.

    The formatters return ``"—"`` for "nothing to show", and a table where
    every such cell rendered at full brightness read as noise -- on a fresh
    run four of seven columns are placeholders.
    """
    if value == EMPTY:
        return empty_cell()
    return Text(value)


def _animated_badge(status: str, frame: int) -> Text:
    """Render a status badge that *moves* while the run is live.

    A fleet table refreshed every couple of seconds is otherwise
    indistinguishable from a screenshot of itself, which is the specific
    thing that makes a monitoring UI feel dead: the reader cannot tell
    "three runs are working" from "three runs are wedged". Motion is spent
    only on the two statuses where it means something -- a spinner for work
    in progress, a slower pulse for a gate that is waiting on the reader --
    and every other status keeps its static badge.
    """
    if not animations_enabled():
        return status_badge(status)

    style = status_style(status)
    if status == "running":
        return Text(spinner(frame), style=style.color or "")
    if status == "at-gate":
        return Text(style.badge, style=breath_style(frame, style.color))
    return status_badge(status)


def _workflow_cell(summary: RunSummary, record: RunRecord, frame: int = 0) -> Text:
    """Render the Workflow column's badge + name (D4, E13-T3).

    A ``mode == "fg"`` run at a gate (no HTTP channel to resolve it
    remotely -- see ``conductor.fleet.tui.actions.gate_resolve_disabled_reason``)
    is marked ``(terminal · PID <pid>)`` so it reads as display-only at a
    glance, distinct from an ``fg-web``/``bg`` gate the ``g`` action can
    actually resolve.

    Returns a ``Text`` so the badge carries its status colour and the name
    is emphasised over the row's secondary metrics -- previously the whole
    row rendered at one weight, leaving the workflow name (the thing being
    looked for) no more prominent than its own placeholder dashes.
    """
    cell = _animated_badge(summary.status, frame)
    cell.append(" ")
    cell.append(summary.workflow_name, style="bold")
    if summary.status == "at-gate" and not summary.gate_resolvable:
        cell.append(f"  terminal · PID {record.pid}", style="dim")
    return cell


#: Dollar thresholds at which a run's cost cell changes colour. A number
#: that is merely *printed* does not register -- these runs reach tens of
#: dollars, and the point of the column is to notice before it does.
_COST_HEAT: tuple[tuple[float, str], ...] = (
    (25.0, "bold red"),
    (10.0, "red"),
    (2.0, "yellow"),
)


def _cost_style(cost: float | None) -> str:
    """Return the style for a run's cost, warming as it climbs."""
    if cost is None:
        return ""
    for threshold, style in _COST_HEAT:
        if cost >= threshold:
            return style
    return ""


def _cost_cell(summary: RunSummary) -> Text:
    """Render the cost cell, warmed by magnitude."""
    text = _format_cost(summary)
    if text == EMPTY:
        return empty_cell()
    return Text(text, style=_cost_style(summary.total_cost_usd))


def _summary_bar_text(summaries: list[RunSummary]) -> Text:
    """One line of fleet-wide context: counts by status, then totals.

    Exists because the Runs screen previously answered "what is running"
    only by making the reader count table rows themselves -- and because a
    three-row table left most of the screen empty, so there was room for
    the answer without displacing anything.

    Statuses are listed in a fixed order (not by count) so the line does
    not reshuffle itself between polls, and a status with no runs is
    omitted rather than shown as ``0``.
    """
    parts: list[Text] = []
    for status in ("running", "at-gate", "paused", "failed", "completed"):
        count = sum(1 for s in summaries if s.status == status)
        if not count:
            continue
        style = status_style(status)
        parts.append(Text(f"{count} {style.label}", style=style.color))

    if not parts:
        parts.append(muted("no runs"))

    total_tokens = sum(s.total_tokens for s in summaries)
    if total_tokens > 0:
        parts.append(muted(_format_tokens(total_tokens)))

    priced = [s.total_cost_usd for s in summaries if s.total_cost_usd is not None]
    if priced:
        unpriced = any(s.has_unpriced for s in summaries)
        total = sum(priced)
        parts.append(muted(f"~${total:.2f}{' (partial)' if unpriced else ''}"))

    line = Text()
    for index, part in enumerate(parts):
        if index:
            line.append("  ·  ", style="dim")
        line.append_text(part)
    return line


def _gate_section(gate: GateInfo, resolvable: bool, width: int, max_prompt_lines: int) -> Text:
    """Render an open gate as a *summary plus a call to action*.

    Deliberately clipped rather than scrollable. A gate prompt is routinely
    hundreds of lines of markdown, and a scrollable preview answered the
    wrong question twice over: it invited the reader to read the whole
    prompt in a pane too small for it, and it put a second focusable
    scroller under a table whose own arrow keys the reader was already
    using. The pane's job is to say *a decision is waiting here and this is
    the key that takes it* -- the full prompt belongs in the ``g`` modal,
    which is built for it.

    Clipped to a *measured* budget rather than a fixed line count: the pane
    takes whatever height the table leaves it, so a fixed cap wasted a tall
    terminal and overflowed a short one.

    Args:
        gate: The open gate.
        resolvable: Whether ``g`` can answer it from here.
        width: Available width in cells; longer lines are clipped.
        max_prompt_lines: How many lines of the prompt to show.
    """
    out = Text()
    out.append("Gate", style="bold yellow")
    if gate.agent_name:
        out.append(f"  {gate.agent_name}", style="dim")
    out.append("\n")

    lines = [line for line in gate.prompt.splitlines() if line.strip()]
    shown = lines[: max(1, max_prompt_lines)]
    for index, line in enumerate(shown):
        if index:
            out.append("\n")
        clipped = line if len(line) <= width else line[: max(1, width - 1)] + "…"
        out.append(clipped)
    if len(lines) > len(shown):
        out.append("\n")
        out.append(f"… +{len(lines) - len(shown)} more lines", style="dim")

    if gate.options:
        out.append("\n")
        out.append("Options: ", style="dim")
        out.append(", ".join(gate.options))

    out.append("\n")
    if resolvable:
        out.append("Press ", style="dim")
        out.append("g", style="bold cyan")
        out.append(" to respond", style="dim")
    else:
        out.append(
            "This run has no dashboard — answer it in its own terminal.",
            style="dim italic",
        )
    return out


@dataclass(frozen=True)
class PreviewParts:
    """The preview pane's two halves, rebuilt at different rates.

    ``main`` (the gate section, plus the ``Progress N/M`` header and bar)
    changes only when the selected run or the data behind it changes -- a
    poll tick or a cursor move. ``score`` (the flowed step chips) is the
    one part of the pane that moves on its own, so it is kept separate to
    let :meth:`RunsScreen._animate_preview` repaint it alone at the
    animation clock's own rate without touching anything else (issue #462).
    """

    main: Text
    score: Text


def _preview_text(
    summary: RunSummary,
    *,
    width: int = 100,
    height: int = 12,
    frame: int = 0,
) -> PreviewParts:
    """Render the selected run's detail for the preview pane.

    The Runs table is deliberately one line per run, so an open gate's
    prompt and options, and the shape of the workflow, had nowhere to
    appear short of drilling in. This pane puts them in the space the
    table was leaving empty, which is what lets the home screen answer
    "what is this run doing" without a screen change.
    """
    # Deliberately *not* a restatement of the row above: every column the
    # table already carries (step, elapsed, tokens, cost, mode, PID and port)
    # is left out. What remains is the two things a one-line row cannot hold:
    # an open gate's full prompt, and the shape of the workflow.
    main = Text()

    # Built first so its height is known: the progress view is bounded
    # (`_SCORE_MAX_LINES`) while the gate prompt is not, so the fixed
    # section is measured and the variable one is given what remains. Both
    # halves are still built here (rather than passing the budget down to
    # a later, separate render) because the budget for the gate section
    # depends on the combined height of both.
    header = _progress_header(summary, width=width)
    score = _score_text(summary, width=width, frame=frame)
    header_lines = len(header.plain.splitlines()) if header.plain else 0
    score_lines = len(score.plain.splitlines()) if score.plain else 0
    progress_lines = header_lines + score_lines

    gate = summary.gate
    if gate is not None:
        budget = height - progress_lines - _GATE_CHROME_LINES
        if progress_lines:
            budget -= 1  # the blank line separating the two sections
        main.append_text(_gate_section(gate, summary.gate_resolvable, width, max(1, budget)))

    if header.plain:
        if gate is not None:
            main.append("\n\n")
        main.append_text(header)
        # No trailing newline here: `main` and `score` render as two
        # separately-stacked widgets now, so a newline after the header
        # would open a blank line between them that the single-widget
        # version never had.

    return PreviewParts(main=main, score=score)


def _progress_header(summary: RunSummary, *, width: int) -> Text:
    """Render the run's ``Progress N/M <bar>`` line, or empty if unknown.

    Split from :func:`_score_text` (issue #462) so the preview pane can
    rebuild this on data/selection changes only, while the animated step
    chips repaint separately at the frame clock's own rate.
    """
    del width  # unused: kept for signature symmetry with `_score_text`
    out = Text()
    topology = summary.topology
    if topology is not None and topology.agents:
        # The steps a run will take are already in its `workflow_started`
        # event, and the pane has the room -- so showing the shape of the
        # workflow (and where in it this run has got to) is what turns the
        # preview from a restatement of the row above it into a reason not
        # to drill in at all.
        statuses = step_statuses([a.name for a in topology.agents], summary.current_step)
        done = sum(1 for v in statuses.values() if v == "completed")

        out.append("Progress", style="bold")
        out.append(f"  {done}/{len(topology.agents)}  ", style="dim")
        out.append_text(progress_bar(done, len(topology.agents)))
    return out


def _score_text(summary: RunSummary, *, width: int, frame: int) -> Text:
    """Render the flowed step chips -- the one part of the preview pane
    that animates. See :func:`_progress_header` for the rest of the split
    (issue #462)."""
    out = Text()
    topology = summary.topology
    if topology is not None and topology.agents:
        statuses = step_statuses([a.name for a in topology.agents], summary.current_step)
        out.append_text(
            render_score(
                topology,
                statuses,
                width=width,
                frame=frame,
                animate=animations_enabled(),
                max_lines=_SCORE_MAX_LINES,
            )
        )
    return out


def _notification_message(summary: RunSummary) -> str:
    """Build the terminal-bell/OSC 9 notification text for a fresh
    transition into ``at-gate`` or ``failed`` (E13-T4)."""
    if summary.status == "at-gate":
        gate = summary.gate
        if gate is not None and gate.agent_name:
            return f"{summary.workflow_name}: waiting at gate ({gate.agent_name})"
        return f"{summary.workflow_name}: waiting at gate"
    return f"{summary.workflow_name}: run failed"


@dataclass(frozen=True, slots=True)
class RunScan:
    """One completed scan of the run-record directory.

    Carries the *seen* run ids alongside the successfully-derived rows
    because the two are not the same set, and conflating them re-fires
    notifications: :meth:`RunsScreen._render_runs` prunes
    :attr:`RunsScreen._notifier` against every id this scan **read**, so a
    run whose summary happened to fail on one tick keeps its notification
    history instead of looking brand-new on the next successful tick
    (issue #446 review; the once-per-transition contract from E13 review
    round 1).

    :attr:`failed` exists for the same reason in the other direction: an
    empty :attr:`collected` means "no runs" only when nothing failed, and
    the two render very differently -- see :meth:`RunsScreen._render_runs`.
    """

    collected: list[tuple[RunRecord, RunSummary]]
    """Rows to display: records whose summary derived, most recent first."""

    seen_run_ids: set[str] = field(default_factory=set)
    """Every non-empty ``run_id`` read this scan, derived or not."""

    failed: int = 0
    """How many records were read but could not have a summary derived."""


def _collect_runs() -> RunScan | None:
    """Read every run record and derive its summary, off the event loop.

    The I/O half of :meth:`RunsScreen.refresh_runs` (issue #437) -- touches
    no widget, so it is safe to run in a worker thread via
    :func:`asyncio.to_thread` and directly unit-testable on its own.

    Returns ``None`` when the directory scan itself failed (the caller's
    "skip this tick, do not reset the notifier" path), and otherwise a
    :class:`RunScan` whose ``seen_run_ids``/``failed`` preserve what a
    per-record derivation failure would otherwise erase -- see
    :class:`RunScan`. Records are returned sorted by recency
    (most-recently-started first) -- deliberately NOT grouped by workflow
    definition (Prefect lesson, E7-T4). ISO 8601 timestamps sort correctly
    as plain strings.
    """
    try:
        records = read_run_records()
    except Exception:
        logger.warning("Failed to read run records during TUI refresh", exc_info=True)
        return None

    records = sorted(records, key=lambda r: r.started_at or "", reverse=True)

    collected: list[tuple[RunRecord, RunSummary]] = []
    failed = 0
    for record in records:
        try:
            summary = derive_run_summary(record)
        except Exception:
            logger.warning(
                "Failed to derive run summary for run_id=%s", record.run_id, exc_info=True
            )
            failed += 1
            continue
        collected.append((record, summary))
    return RunScan(
        collected=collected,
        seen_run_ids={r.run_id for r in records if r.run_id},
        failed=failed,
    )


class RunsScreen(Screen):
    """Home screen: every live run, sorted by recency, polled refresh."""

    DEFAULT_CSS = """
    RunsScreen #summary-bar {
        padding: 0 2;
        height: 1;
    }

    RunsScreen #runs-table {
        /* auto (capped), not 1fr: a three-run fleet in a 1fr table left a
           dozen blank striped rows between the last run and the pane below
           it. The cap keeps a long fleet from squeezing the preview -- past
           it the table scrolls, and the preview keeps its share. */
        height: auto;
        max-height: 60%;
        padding: 0 1;
    }

    RunsScreen #preview-pane {
        /* Takes the remainder rather than hugging its content. With both
           this and the table sized to their content, a short fleet in a
           tall terminal left everything crammed against the top above a
           screen of dead space -- and the thing worth spending that space
           on is right here: the selected run's topology. The table is
           capped rather than 1fr so a long fleet cannot squeeze this to
           nothing, and `min-height` holds a floor when it is. */
        height: 1fr;
        min-height: 6;
        border-top: solid $primary 40%;
        padding: 1 2;
        /* Not scrollable: every section here is line-bounded on purpose
           (see `_gate_section`), and a focusable scroller under the table
           would compete with it for the arrow keys. */
        overflow: hidden hidden;
    }
    """

    POLL_INTERVAL_SECONDS: ClassVar[float] = 2.0
    """~2s poll per the design's *Refresh model*. Class attribute (rather
    than a hardcoded literal in :meth:`on_mount`) so tests can shrink it and
    observe a real poll tick pick up a newly-written record without waiting
    out the full interval."""

    # Descriptions are terse because they all share one footer line: the
    # longer wording ("Dashboard", "Resolve Gate") overflowed it, and an
    # overflowing footer does not wrap -- it truncates mid-word and drops
    # whatever came after, which is how `h History` disappeared entirely.
    # The full set currently needs ~91 columns; `test_footer_fits_without_
    # truncation` pins that, so adding a binding fails a test rather than
    # silently dropping the last one off the edge again.
    #
    # Ordered in two blocks, because these keys are not one flat set: the
    # first acts on whichever run the cursor is on, the second navigates the
    # app or commands the whole fleet. Textual renders the footer in this
    # order, so the ordering is what puts each block together -- and
    # `BlockFooter` draws the rule between them, because ordering alone is
    # invisible when every key has identical styling and spacing. `K` (kill
    # *all*) sits in the fleet block despite pairing visually with `k`: it is
    # fleet-scoped, and leaving the two adjacent put "kill everything" one
    # stray Shift away from "kill this one".
    BINDINGS = [
        # Row-scoped -- operate on the highlighted run.
        #
        # `enter` must be `priority` to be seen at all: `DataTable` binds it
        # itself (to `select_cursor`, `show=False`) and, as the focused
        # widget, sits ahead of the screen in the binding chain -- so without
        # priority its hidden binding shadows this one and the drill-down,
        # the most-used action here, never appears in the footer.
        Binding("enter", "open_detail", "Detail", priority=True),
        ("w", "open_dashboard", "Dash"),
        ("k", "kill", "Kill"),
        ("g", "resolve_gate", "Gate"),
        # Fleet-scoped -- navigation and whole-fleet commands.
        ("n", "open_new_run", "New"),
        ("p", "open_providers", "Providers"),
        ("r", "open_registries", "Registries"),
        ("h", "open_history", "History"),
        ("K", "kill_all", "Kill all"),
        ("q", "quit", "Quit"),
    ]

    _ROW_SCOPED_ACTIONS = frozenset({"open_detail", "open_dashboard", "kill"})
    """Actions that need a highlighted run to mean anything. Hidden by
    :meth:`check_action` while the table is empty. ``resolve_gate`` is
    row-scoped too but has a stricter condition of its own, so it is
    checked separately rather than listed here."""

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
        self._frame = 0
        """Animation clock, advanced by :meth:`_tick`. Every moving glyph on
        this screen derives its appearance from this one number so they stay
        in phase (see :mod:`conductor.fleet.tui.anim`)."""
        self._burn_history: dict[str, deque[float]] = {}
        """Per-row token-burn samples (tokens gained between polls), oldest
        first, feeding the Burn sparkline."""
        self._burn_totals: dict[str, float] = {}
        """Last-seen cumulative token total per row, so the next poll can
        take a delta from it."""
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
        self._anim_timer: Timer | None = None
        """The ~10fps animation timer, held so it can be paused while this
        screen is not on top -- see :meth:`on_screen_suspend`. ``None``
        when animations are disabled (``CONDUCTOR_FLEET_NO_ANIM``), so the
        suspend/resume hooks no-op."""
        self._refreshing = False
        """Guards against a poll tick (or an explicit ``refresh_runs()``
        call) starting a second, concurrent ``_refresh_worker`` while one
        is already awaiting its ``asyncio.to_thread`` scan (issue #437) --
        an overrunning refresh drops the next tick rather than running two
        scans at once, matching this screen's other re-entrancy guards."""
        self._refresh_pending = False
        """Set when an *explicit* refresh (after a kill, or after a gate is
        resolved) arrived while a scan was in flight. Those two callers
        promise the table updates immediately rather than a tick later, and
        on the slow-fleet case issue #437 targets a scan is in flight most
        of the time -- so an explicit request is coalesced and re-dispatched
        from :meth:`_refresh_worker`'s ``finally`` rather than dropped like
        a redundant poll tick (issue #446 review)."""
        self._opening_dashboard = False
        """Guards against a second, concurrent ``w`` press opening another
        browser tab while a dashboard-open (also off the event loop, issue
        #437) is still in flight."""
        self._scan_trouble_notified = False
        """Debounces the "could not read run records" notification to once
        per run of consecutive failures, so a persistently unreadable
        directory doesn't emit one toast per ~2s tick."""

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
        yield Static(id="summary-bar", classes="summary-bar")
        yield Static(loading_text(), id="runs-loading", classes="notice")
        yield DataTable(id="runs-table")
        yield Static(_EMPTY_STATE_TEXT, id="empty-state", classes="empty-state")
        # Two `Static`s rather than one (issue #462): `#run-preview` (the
        # gate section and the progress header/bar) is rebuilt only on
        # data/selection changes, while `#run-preview-score` (the flowed
        # step chips) is the one thing the ~10fps animation clock repaints
        # on its own -- see `_animate_preview`. No extra CSS rule is
        # needed: `#preview-pane` is a `Vertical` with auto-height
        # children, and two `Static`s stack inside it exactly as one did.
        yield Vertical(
            Static(id="run-preview"),
            Static(id="run-preview-score"),
            id="preview-pane",
        )
        yield BlockFooter(first_block_actions=self._ROW_SCOPED_ACTIONS | {"resolve_gate"})

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        # The first key is kept because `_tick` repaints that column in
        # place: `update_cell` addresses a column by key, not by index.
        self._workflow_column, *_rest = table.add_columns(
            "Workflow",
            "Step",
            "Elapsed",
            "On Step",
            "Tokens",
            "Cost",
            "Burn",
            "Mode",
            "PID",
            "Port",
            "Started",
            "Directory",
        )
        table.cursor_type = "row"
        table.zebra_stripes = True
        # Hidden until the first collector result lands, so the pre-load
        # frame shows one dim "Loading…" line instead of an empty table,
        # the "no runs" empty state, and an empty bordered preview pane all
        # at once (issue #437).
        table.display = False
        self.query_one("#empty-state", Static).display = False
        self.query_one("#preview-pane", Vertical).display = False
        self.refresh_runs()
        self.set_interval(self.POLL_INTERVAL_SECONDS, self.refresh_runs)
        if animations_enabled():
            # A second, much faster timer driving *only* repaints of the
            # cells that move. Deliberately not folded into the data poll:
            # animation wants ~10fps and rescanning the run-record
            # directory that often would be gratuitous I/O for the sake of
            # a spinner.
            self._anim_timer = self.set_interval(FRAME_INTERVAL, self._tick)

    def on_screen_suspend(self) -> None:
        """Pause the animation while another screen is on top.

        A pushed screen does not stop this one's timers, and a screen that
        is no longer on top is still *composited*: the compositor keeps
        painting whatever shows through the screen above it, and for a
        translucent ``ModalScreen`` -- the gate options dialog (``g``) and
        the kill confirmation (``k``/``K``) -- that is the whole visible
        area. So every animated repaint down here re-blends the screen
        above it, at ~10fps, for a screen nobody is reading.

        The user-visible cost is throughput, not frames: on one 160x45
        terminal sitting at an open gate this was roughly 2.5x the escape
        sequences and ~40% more CPU with the modal up than without it.
        Absolute figures are machine- and emulator-specific and nothing
        pins them, but the direction is structural -- and on a terminal
        that cannot absorb the stream (over SSH, in a multiplexer, on a
        slow emulator) keystrokes queue behind the redraw and the modal
        appears frozen.

        Note :attr:`~textual.screen.Screen.is_current` does **not** express
        "on top" and cannot be used to gate this: it means *still being
        composited*, which is exactly the state being paused here. Textual
        relies on that meaning -- ``Screen._on_timer_update`` gates the
        whole update cycle on it, which is why a covered screen keeps
        repainting at all. Opacity is irrelevant: ``App._background_screens``
        appends the screen below the top *before* testing the background
        alpha, so ``is_current`` is ``True`` under an opaque screen too.
        :attr:`~textual.screen.Screen.is_active` is the top-of-stack test,
        and is what the guards in :meth:`_tick` and
        :meth:`_update_gate_detail` use.
        """
        if self._anim_timer is not None:
            self._anim_timer.pause()

    def on_screen_resume(self) -> None:
        """Restart the animation and repaint whatever went stale.

        Resuming is deliberately done *before* the repaint: a raise out of
        :meth:`_update_gate_detail` (``query_one`` can fail if the pane is
        gone mid-teardown) would otherwise strand the timer paused, which
        is a worse freeze than the one this fixes.

        The repaint relies on ``App.pop_screen`` popping the stack *before*
        posting ``ScreenResume``, since :meth:`_update_gate_detail` no-ops
        unless this screen is already back on top. If that order ever
        inverted, the pane would silently keep showing what it had under
        the modal until the next ~2s poll.

        This also fires once at startup, before anything has been
        suspended -- and, because the splash is pushed over this screen
        immediately, it can arrive while this screen is already covered.
        That is why :meth:`_tick`'s guard is load-bearing rather than
        decorative.
        """
        if self._anim_timer is not None:
            self._anim_timer.resume()
        self._update_gate_detail()

    def _tick(self) -> None:
        """Advance the animation clock and repaint only what moves.

        Rebuilding the whole table at frame rate would fight the cursor and
        the reader's scroll position, so this updates the animated cell of
        each live row in place and, via :meth:`_animate_preview`, the one
        other moving thing on screen: the preview's live step.

        This is the frame clock's *entire* jurisdiction (issue #462): the
        preview pane's gate section and progress header, and the footer's
        bindings, belong to the data poll (:meth:`_update_gate_detail`) and
        to selection changes, not to this ~10fps timer. Before this fix,
        `_tick` ended by calling `_update_gate_detail()` directly, which
        rebuilt the whole pane and called `refresh_bindings()` ten times a
        second for the sake of one spinner glyph -- do not re-add that
        call here; it is the exact regression this method now guards
        against by construction.
        """
        # This guard, not `on_screen_suspend`'s pause, is what actually
        # stops frames landing on a covered screen. `App.push_screen`
        # appends to the screen stack synchronously but *posts*
        # `ScreenSuspend` as a message, and this callback is invoked
        # straight from the timer's own asyncio task rather than queued
        # behind that message -- so frames keep arriving until the pump
        # drains, which is longest precisely when the pump is backed up,
        # the failure this fix is about. The pause is hygiene on top: it
        # stops a 10fps task waking for a screen nobody is reading.
        if not self.is_active:
            return
        self._frame += 1
        table = self.query_one(DataTable)
        if not table.display:
            return

        for key, summary in self._displayed_summaries.items():
            if summary.status not in _ANIMATED_STATUSES:
                continue
            record = self._displayed_records.get(key)
            if record is None:
                continue
            try:
                table.update_cell(
                    key, self._workflow_column, _workflow_cell(summary, record, self._frame)
                )
            except Exception:  # noqa: BLE001 - a row can vanish mid-tick
                logger.debug("Skipped animating row %s", key, exc_info=True)

        self._animate_preview()

    def _animate_preview(self) -> None:
        """Repaint only the preview's live step (``#run-preview-score``).

        Called from :meth:`_tick` at ~10fps in place of
        :meth:`_update_gate_detail`, which used to be called there and
        rebuilt the *whole* pane's ``Text`` plus the footer's bindings for
        the sake of one spinner glyph inside it (issue #462). Everything
        else the preview shows -- the gate section, the progress header,
        the footer -- is driven by the data poll and by selection changes,
        not by this timer; see :meth:`_update_gate_detail`.

        A no-op when there is nothing that could actually move: the pane
        isn't displayed, no run is selected, the selected run's status
        isn't one of :data:`_ANIMATED_STATUSES`, or the selected run has no
        topology or no current step. :func:`~conductor.fleet.tui.dag.
        step_statuses` only ever marks the single current step
        ``"running"`` -- everything else is ``"completed"``/``"pending"``,
        neither of which animates -- so a run with no ``current_step`` has
        nothing here for a frame to change.
        """
        pane = self.query_one("#preview-pane", Vertical)
        if not pane.display:
            return

        summary = self._selected_summary()
        if summary is None or summary.status not in _ANIMATED_STATUSES:
            return

        topology = summary.topology
        if topology is None or not topology.agents or summary.current_step is None:
            return

        # Same width derivation as `_update_gate_detail`, so the animated
        # frame flows identically to the poll-rendered one instead of
        # re-wrapping every couple of seconds when the two disagree.
        pane_size = pane.size
        if not pane_size.width or not pane_size.height:
            pane_size = self.size

        try:
            score_widget = self.query_one("#run-preview-score", Static)
            score = _score_text(
                summary,
                width=max(20, pane_size.width - _PREVIEW_PADDING),
                frame=self._frame,
            )
            # `layout=False`: every glyph in `SPINNER_FRAMES` is exactly one
            # cell wide and `_score_text` only swaps that glyph per frame, so
            # the widget's size provably cannot change -- the default
            # `layout=True` would force a full screen layout pass every
            # frame for no reason.
            score_widget.update(score, layout=False)
            score_widget.display = bool(score.plain)
        except Exception:  # noqa: BLE001 - a frame can land mid-teardown
            logger.debug("Skipped animating preview score", exc_info=True)

    def _burn_cell(self, key: str) -> Text:
        """Render this run's recent token-burn sparkline.

        The Tokens column answers "how much has this cost so far"; it cannot
        answer "is this run still doing anything", which on a long run is the
        more urgent question. Sampling the delta between polls does -- a
        stalled agent flatlines visibly while a busy one stays ragged.
        """
        history = self._burn_history.get(key)
        if not history:
            return empty_cell()
        return Text(sparkline(list(history), width=_BURN_WIDTH), style="cyan")

    def _sample_burn(self, key: str, summary: RunSummary) -> None:
        """Record one token-burn sample for ``key``."""
        total = float(summary.total_tokens or 0)
        previous = self._burn_totals.get(key)
        self._burn_totals[key] = total
        if previous is None:
            # The first sample of a run that has been going for an hour is
            # its entire history, which would dwarf every later delta and
            # flatten the rest of the sparkline to nothing.
            return
        self._burn_history.setdefault(key, deque(maxlen=_BURN_WIDTH)).append(
            max(0.0, total - previous)
        )

    def refresh_runs(self, *, explicit: bool = False) -> None:
        """Rescan run records and repopulate the table (or show the empty state).

        A **synchronous dispatcher** (issue #437) -- it stays this shape
        deliberately because it is called from ``on_mount``, from
        ``set_interval``, and from two action paths (``_kill_and_refresh``,
        ``action_resolve_gate``'s ``finally``); only the body lives in a
        worker. Guarded by :attr:`_refreshing` so an overrunning refresh
        drops the next tick rather than starting a second, overlapping one
        -- deliberately not ``@work(exclusive=True)``, which would cancel
        the in-flight worker and start a new one ("newest wins") rather
        than the "skip this tick" behaviour wanted here. The flag is set
        here, synchronously, rather than as the first line of
        ``_refresh_worker`` -- a ``@work`` method's body doesn't start
        running the instant it is called (Textual schedules it), so setting
        the flag inside the worker would leave a window where a poll tick
        landing before that first scheduling turn sees ``_refreshing``
        still ``False`` and starts a second, overlapping worker.

        Args:
            explicit: ``True`` for a caller that needs the table to reflect
                something it just did (a kill, a resolved gate) rather than
                merely being a periodic poll. Such a request is remembered
                and re-dispatched when the in-flight scan finishes, since
                that scan started *before* the change and cannot show it.
        """
        if self._refreshing:
            self._refresh_pending = self._refresh_pending or explicit
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
        """Do the scan off the event loop, then render on it.

        Best-effort: a failure reading records or deriving one run's
        summary is logged and surfaced rather than crashing the whole
        refresh -- ``read_run_records()`` is already tolerant of individual
        bad files, but this is an extra backstop specifically so a poll
        loop can never take the TUI down. See :func:`_collect_runs` for the
        failure contract itself.

        A failure reading the run-record directory *itself* skips this tick
        entirely, leaving the previously displayed table, selection, and
        :attr:`_notifier` history untouched, rather than treating the
        failure as "zero records" and pruning all notifier history -- which
        would make every gated/failed run look brand-new again on the next
        successful scan and re-fire its notification, violating the
        once-per-transition contract (E13 review round 1). Skipping the
        tick's *data* is right; skipping it *silently* is not, so the
        failure is surfaced by :meth:`_render_scan_failure`.
        """
        try:
            scan = await asyncio.to_thread(_collect_runs)
            if scan is None:
                self._render_scan_failure()
                return
            try:
                self._render_runs(scan)
            except Exception:  # noqa: BLE001 - a render bug must not exit the app
                logger.warning("Failed to render runs table", exc_info=True)
        finally:
            self._refreshing = False
            if self._refresh_pending:
                self._refresh_pending = False
                self.refresh_runs(explicit=True)

    def _render_scan_failure(self) -> None:
        """Show that the scan failed, instead of leaving the screen silent.

        Two states this replaces, both of which look like a working app
        (issue #446 review): on first load, a dim "Loading…" line that
        never resolves, because only :meth:`_render_runs` hides it; and
        mid-session, a table frozen at its last good contents that the
        operator reads as current -- and may press ``k`` against a run that
        already exited.
        """
        table = self.query_one(DataTable)
        loading = self.query_one("#runs-loading", Static)
        loading.update(
            styled(
                "[red]Could not read run records[/red] -- {}",
                "showing the last successful scan" if table.display else "see the log",
            )
        )
        loading.display = True
        self._notify_scan_trouble("Could not read run records; the table may be stale.")

    def _notify_scan_trouble(self, message: str) -> None:
        """Notify once per run of consecutive scan failures.

        A persistently unreadable run-record directory fails on every ~2s
        tick; one toast per tick would bury the screen it is warning about.
        Reset by the next successful render.
        """
        if self._scan_trouble_notified:
            return
        self._scan_trouble_notified = True
        self.notify(message, severity="error", timeout=15, markup=False)

    def _render_runs(self, scan: RunScan) -> None:
        """Repopulate the table (or the empty state) from a completed scan.

        The render half of :meth:`refresh_runs` (issue #437) -- runs on the
        event loop, after the collector's ``asyncio.to_thread`` hop.

        Distinguishes an empty fleet from a fleet none of whose summaries
        could be derived. Both arrive with no rows, but the first is the
        launch affordance and the second is an error: showing "no runs" to
        an operator whose runs are all still burning tokens invites them to
        launch a duplicate (issue #446 review).
        """
        table = self.query_one(DataTable)
        empty_state = self.query_one("#empty-state", Static)
        loading = self.query_one("#runs-loading", Static)

        if not scan.collected and scan.failed:
            # Records exist; not one of them could be read. Deliberately
            # leaves the notifier, the selection and `_displayed_records`
            # alone -- emptying the latter would make `k`/`K`/`enter`
            # silent no-ops on a fleet that is still running.
            loading.update(
                styled(
                    "[red]Could not read {} of {} run(s)[/red] "
                    "-- the fleet is still running; see the log.",
                    str(scan.failed),
                    str(len(scan.seen_run_ids) or scan.failed),
                )
            )
            loading.display = True
            self._notify_scan_trouble(f"Could not read {scan.failed} run(s); the table is stale.")
            return

        loading.display = False
        self._scan_trouble_notified = False

        if not scan.collected:
            # First-class empty state (E7-T5): the launch affordance, not
            # an empty table.
            table.display = False
            empty_state.display = True
            table.clear()
            self._displayed_records = {}
            self._displayed_summaries = {}
            self._notifier.prune(scan.seen_run_ids)
            self._update_summary_bar([])
            # Hidden unconditionally rather than via the visibility-guarded
            # repaint below: this branch already forces a relayout, and
            # leaving the pane up would pair the "no runs" empty state with
            # a preview still offering `g` for a run that is gone.
            self.query_one("#preview-pane", Vertical).display = False
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
        for index, (record, summary) in enumerate(scan.collected):
            # Legacy .pid-derived records may carry an empty run_id, which
            # would collide on this row key across two such records and
            # raise DuplicateKey -- fall back to a per-refresh unique key.
            key = summary.run_id or f"_no-run-id-{index}"
            self._sample_burn(key, summary)
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
        # Pruned against every run_id this scan READ, not just the rows that
        # rendered: a record whose summary failed to derive must keep its
        # notification history, or it looks brand-new on the next
        # successful tick and re-fires (issue #446 review).
        self._notifier.prune(scan.seen_run_ids)

        if previous_key is not None:
            with contextlib.suppress(Exception):
                table.move_cursor(row=table.get_row_index(previous_key))

        self._update_summary_bar(list(summaries.values()))
        self._update_gate_detail()

    def _add_row(self, table: DataTable, summary: RunSummary, record: RunRecord, key: str) -> None:
        """Add one run's row, formatted per the design's mockup columns.

        Placeholders go through ``theme.empty_cell()`` so a row with little
        data yet reads as a workflow name with some blanks after it, rather
        than as a line of full-brightness dashes competing with the name for
        attention. The trailing column is the run's *mode*, not its port: a
        blank port was the only way a foreground run announced itself, and a
        blank is indistinguishable from "no data yet" -- which is precisely
        the ambiguity this column now removes (the port is in the preview
        pane, where it has room to be labelled).
        """
        table.add_row(
            _workflow_cell(summary, record),
            Text(summary.current_step) if summary.current_step else empty_cell(),
            _dim_if_empty(_format_duration(summary.total_elapsed_seconds())),
            _dim_if_empty(_format_duration(summary.elapsed_on_step_seconds())),
            _dim_if_empty(_format_tokens(summary.total_tokens)),
            _cost_cell(summary),
            self._burn_cell(key),
            mode_label(summary.mode),
            # PID and port live in the table rather than only in the preview:
            # they are per-run identity, so putting them here is what let the
            # preview stop restating the row it was sitting under.
            Text(str(record.pid), style="dim"),
            Text(str(summary.port), style="dim") if summary.port is not None else empty_cell(),
            _started_cell(summary.started_at),
            _directory_cell(summary.cwd),
            key=key,
        )

    def _selected_key(self) -> str | None:
        """Return the DataTable row key behind the currently highlighted row.

        ``None`` when the table is empty (the empty state is showing) or
        the cursor's row key can't be resolved (e.g. a stale cursor
        position mid-refresh). Shared by :meth:`_selected_record` and
        :meth:`_selected_summary` so both look up the same row.
        """
        return highlighted_row_key(self.query_one(DataTable))

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
        """(Re)render the preview pane for the currently selected row.

        Supersedes the gate-only panel this used to be (E13-T1): an open
        gate is now one section of a preview that is always populated,
        rather than the only thing that could ever appear below the table.
        The name is kept because it is the hook every refresh path already
        calls.

        Called after every table rebuild (a poll tick may open or close the
        selected run's gate) and on every cursor move
        (:meth:`on_data_table_row_highlighted`), since the selected row can
        change independently of a poll tick. Owns the *data and selection*
        half of the preview -- the gate section, the progress header, and
        the footer's bindings. It is never called from the ~10fps
        :meth:`_tick` (issue #462): the one thing in the pane that moves on
        its own, the score's live step, is repainted separately by
        :meth:`_animate_preview`, so this method rebuilding both widgets
        stays a data/selection-rate operation rather than a per-frame one.

        A no-op while another screen is on top -- repainting for a reader
        who is looking at a modal costs a re-blend of that modal for
        nothing (see :meth:`on_screen_suspend`). Note this defers the
        footer's :meth:`_refresh_row_bindings` too, not just the pane, so
        a gate closing under a modal can leave ``g`` advertised in the
        footer showing through it. Both are recomputed from scratch on
        every call, so the ~2s poll heals any staleness even if
        :meth:`on_screen_resume` never fires; resume is the latency
        optimisation on top of that.
        """
        if not self.is_active:
            return
        pane = self.query_one("#preview-pane", Vertical)
        panel = self.query_one("#run-preview", Static)
        score_widget = self.query_one("#run-preview-score", Static)

        summary = self._selected_summary()
        record = self._selected_record()
        if summary is None or record is None:
            # Hidden rather than shown empty: with no rows there is nothing
            # to preview, and an empty bordered pane would just crowd the
            # empty state's own launch hint.
            pane.display = False
            panel.update("")
            score_widget.update("")
            score_widget.display = False
            self._refresh_row_bindings()
            return

        pane.display = True
        # Fall back to the screen's own size when the pane hasn't been
        # laid out yet -- it starts hidden (`display = False`) until the
        # first load lands (issue #437), and Textual doesn't compute a
        # hidden widget's size until the *next* layout pass, which hasn't
        # happened yet at the point this first render runs. Without the
        # fallback, the very first gate preview on a fresh mount would be
        # clipped to a near-zero width.
        pane_size = pane.size
        if not pane_size.width or not pane_size.height:
            pane_size = self.size
        parts = _preview_text(
            summary,
            width=max(20, pane_size.width - _PREVIEW_PADDING),
            height=max(4, pane_size.height - _PREVIEW_VERTICAL_PADDING),
            frame=self._frame,
        )
        panel.update(parts.main)
        score_widget.update(parts.score)
        # Hidden rather than shown blank: a run with no topology yet (or
        # too narrow a pane to render one) would otherwise gain a stray
        # blank line under the gate/progress section.
        score_widget.display = bool(parts.score.plain)
        self._refresh_row_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide row-scoped bindings that the highlighted run can't support.

        Textual consults this for every binding when it builds the footer.
        Returning ``False`` hides a key outright rather than greying it out,
        which keeps the single, non-wrapping footer line short enough to
        survive (see the truncation note on :attr:`BINDINGS`).

        Two conditions, both row-scoped:

        - Every key in :attr:`_ROW_SCOPED_ACTIONS` needs a highlighted run,
          so all of them disappear while the fleet is empty — the footer then
          advertises only what an empty table can actually do.
        - ``g`` additionally needs that run to be *at* a gate. A gate is the
          exception rather than the norm, so a permanently-visible "Gate" key
          would be noise even when a row is selected.

        Deliberately *not* conditional: ``w`` on a portless (``mode == "fg"``)
        run. A dashboard is the norm, so hiding it per-row would make the key
        flicker as the cursor moves; ``action_open_dashboard`` explains the
        specific reason in a notification instead.

        Fleet-scoped actions (navigation, ``K``, ``q``) return ``True``
        unchanged — they never depend on a selection.
        """
        if action == "resolve_gate":
            summary = self._selected_summary()
            return summary is not None and summary.gate is not None
        if action in self._ROW_SCOPED_ACTIONS:
            return self._selected_key() is not None
        return True

    def _refresh_row_bindings(self) -> None:
        """Ask Textual to re-evaluate the footer after the selection moves.

        ``check_action`` is only consulted when bindings are refreshed, so a
        gate opening or closing between polls — or the last run in the fleet
        exiting, which retires every row-scoped key at once — would otherwise
        leave the footer showing yesterday's answer until some other event
        forced a redraw. Called only from :meth:`_update_gate_detail` --
        i.e. at poll/selection/resume rate, never from the ~10fps
        :meth:`_tick` (issue #462).
        """
        self.refresh_bindings()

    def _update_summary_bar(self, summaries: list[RunSummary]) -> None:
        """Refresh the fleet-wide counts/totals line above the table."""
        self.query_one("#summary-bar", Static).update(_summary_bar_text(summaries))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Refresh the preview pane when the cursor moves to a different
        row -- independent of the next poll tick."""
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
        self._push_detail_for(key)

    def action_open_detail(self) -> None:
        """Open the highlighted run's detail screen -- the ``enter`` binding.

        This is a ``priority`` binding, so it runs *ahead* of ``DataTable``'s
        own hidden ``enter`` (``select_cursor``) and the keypress never
        becomes a ``RowSelected`` message. Mouse clicks still arrive that way
        and land in :meth:`on_data_table_row_selected`; both funnel through
        :meth:`_push_detail_for`, and since keyboard and mouse take exactly
        one path each, ``enter`` cannot push two screens.
        """
        key = self._selected_key()
        if key is None:
            return
        self._push_detail_for(key)

    def _push_detail_for(self, key: str) -> None:
        """Push the run-detail screen for ``key``, if it still resolves."""
        record = self._displayed_records.get(key)
        if record is None:
            return
        cast("FleetApp", self.app).push_run_detail(record)

    # -----------------------------------------------------------------
    # Dashboard action (E8-T2)
    # -----------------------------------------------------------------

    def action_open_dashboard(self) -> None:
        """Open the selected run's dashboard in a browser -- bound to ``w``.

        A synchronous dispatcher (matching :meth:`refresh_runs`'s shape):
        the guard check and the disabled-reason notification stay here, on
        the event loop, and only the actual open -- which can block for up
        to 15s under WSL (``_wsl_open``'s ``subprocess.run(..., timeout=15)``,
        issue #437) -- moves into :meth:`_open_dashboard_worker`. The
        :attr:`_opening_dashboard` flag is set here, synchronously, rather
        than as the first line of the worker -- a ``@work`` method's body
        doesn't start running the instant it is called, so setting the
        flag inside the worker would leave a window where a rapid second
        ``w`` press sees it still ``False`` and opens a second tab.
        """
        record = self._selected_record()
        if record is None:
            return
        reason = dashboard_disabled_reason(record)
        if reason is not None:
            self.notify(f"Dashboard unavailable: {reason}", severity="warning", markup=False)
            return
        if self._opening_dashboard:
            return
        self._opening_dashboard = True
        try:
            self._open_dashboard_worker(record)
        except Exception:
            # The flag is set before the worker exists, so a failure to
            # dispatch would otherwise latch it True and make ``w`` a
            # silent no-op for the rest of the session.
            self._opening_dashboard = False
            raise

    @work
    async def _open_dashboard_worker(self, record: RunRecord) -> None:
        """Open ``record``'s dashboard off the event loop.

        Guarded by :attr:`_opening_dashboard` so a double ``w`` press
        cannot open two browser tabs while the first open is still in
        flight.
        """
        url = dashboard_url(record)
        try:
            opened = await asyncio.to_thread(open_dashboard, record)
        except Exception:  # noqa: BLE001 - surfaced, not crashed
            # ``open_dashboard`` catches broadly today, so this is a
            # backstop: a ``@work`` method defaults to ``exit_on_error``,
            # so anything escaping here would take the whole TUI down over
            # a failed browser launch. The URL is what the user actually
            # needs, so it is surfaced either way.
            logger.warning("Failed to open dashboard for run_id=%s", record.run_id, exc_info=True)
            self.notify(
                f"Could not open a browser. Dashboard: {url}",
                severity="error",
                timeout=15,
                markup=False,
            )
            return
        finally:
            self._opening_dashboard = False

        if opened:
            self.notify(f"Opened dashboard: {url}", markup=False)
            return
        # Reporting success unconditionally is how a failed open (a WSL host
        # with no working handler, a headless box) still told the user the
        # dashboard had been opened. The URL is included so it stays usable
        # by hand -- it is the only thing the user actually needs.
        self.notify(
            f"Could not open a browser. Dashboard: {url}",
            severity="warning",
            timeout=15,
            markup=False,
        )

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
        waiting out the next ~2s poll tick.

        Reports failures explicitly. ``stop_records`` writes its per-record
        diagnostics to the silent console this screen hands it, so they are
        discarded -- if a kill is refused (an identity mismatch, which is a
        safety stop the user needs to act on) or the process survives, this
        notification is the only place the user can learn it. Announcing
        only ``stopped`` would report a success that did not happen, which
        is the same defect the dashboard action above avoids.
        """
        outcome = await kill_runs(self.app, targets)
        if outcome.declined:
            self.notify("Kill cancelled.", severity="warning")
            return
        if outcome.stopped:
            self.notify(f"Killed {len(outcome.stopped)} run(s).", markup=False)
        if outcome.failed:
            detail = ", ".join(
                f"{record.workflow_name or record.pid} ({why})" for record, why in outcome.failed
            )
            self.notify(
                f"Could not kill {len(outcome.failed)} run(s): {detail}",
                severity="error",
                timeout=15,
                markup=False,
            )
        elif not outcome.stopped:
            self.notify("Nothing was killed.", severity="warning", markup=False)
        self.refresh_runs(explicit=True)

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
            self.notify(f"Cannot resolve gate here: {reason}", severity="warning", markup=False)
            return

        self._resolving_gate = True
        try:
            gate = summary.gate
            while True:
                outcome: GateResolveOutcome | None = await resolve_gate(self.app, record, gate)
                if outcome is None:
                    self.notify("Gate resolution cancelled.", severity="warning")
                    return
                if not outcome.success:
                    self.notify(
                        f"Gate resolution failed: {outcome.message}",
                        severity="error",
                        markup=False,
                    )
                    return

                self.notify(outcome.message, markup=False)

                # A `questions` node asks one question per gate, so answering
                # one immediately opens the next. Dismissing back to the table
                # and making the user press `g` again for every question turned
                # a four-question node into four round trips. Re-present as
                # long as the run keeps asking; anything else (the run moves
                # on, the gate closes) falls out of the loop.
                gate = await self._await_next_gate(record, after=gate)
                if gate is None:
                    return
        finally:
            self._resolving_gate = False
            self.refresh_runs(explicit=True)

    async def _await_next_gate(
        self, record: RunRecord, *, after: GateInfo, timeout: float = 8.0
    ) -> GateInfo | None:
        """Wait briefly for the run to present a *different* gate.

        Returns the new gate, or ``None`` if the run stopped asking (or the
        wait timed out). Bounded because a run that has genuinely moved on
        never presents another gate, and an unbounded wait would hold the
        resolve guard -- and the user -- indefinitely.

        Compared on prompt rather than agent name: a questions node presents
        every question under the same name, so the name alone cannot tell a
        new question from the one just answered.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(_NEXT_GATE_POLL_SECONDS)
            try:
                summary = await asyncio.to_thread(derive_run_summary, record)
            except Exception:
                logger.warning("Failed to re-read run summary after a gate", exc_info=True)
                return None
            gate = summary.gate
            if gate is None:
                return None
            if gate.prompt != after.prompt or gate.options != after.options:
                return gate
        return None
