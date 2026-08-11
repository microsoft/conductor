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
"""

from __future__ import annotations

import logging
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Static

from conductor.console import styled
from conductor.fleet.records import RunRecord
from conductor.fleet.summary import AgentDetail, RunSummary, derive_run_detail, derive_run_summary

logger = logging.getLogger(__name__)


_STATUS_LABELS: dict[str, str] = {
    "pending": "pending",
    "running": "▶ running",
    "completed": "✓ completed",
    "failed": "✗ failed",
}

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


def _gate_detail_text(summary: RunSummary) -> Text:
    """Render the gate-detail panel's text for a run with an open gate
    (E13-T1): its prompt and the available option labels/values -- the
    payload E6-T6 already carries on ``RunSummary.gate``.

    Duplicated from ``conductor.fleet.tui.screens.runs`` (rather than
    imported) to keep each screen module's rendering self-contained --
    matching that module's own precedent (see ``_format_duration``'s
    docstring above).

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


class RunDetailScreen(Screen):
    """Per-run detail: topology as discrete per-agent rows, current step
    highlighted. Not a DAG; no agent messages; no tool output (E9-T2)."""

    POLL_INTERVAL_SECONDS: ClassVar[float] = 2.0
    """Matches ``RunsScreen.POLL_INTERVAL_SECONDS`` -- a class attribute
    (rather than a hardcoded literal in :meth:`on_mount`) so tests can
    shrink it and observe a real poll tick pick up new events without
    waiting out the full interval."""

    BINDINGS = [("escape", "back", "Back")]

    def __init__(self, record: RunRecord) -> None:
        super().__init__()
        self._record = record
        self._poll_timer: Timer | None = None

    def action_back(self) -> None:
        """Pop back to the Runs screen -- bound to ``escape`` (E9-T1)."""
        self.app.pop_screen()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="detail-title")
        yield Static(id="gate-detail")
        yield DataTable(id="detail-table")
        yield Static(_PLACEHOLDER_TEXT, id="detail-placeholder")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Agent", "Type", "Status", "Elapsed", "Tokens", "Cost")
        table.cursor_type = "row"
        self.query_one("#gate-detail", Static).display = False
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
        the placeholder). Best-effort: a failure deriving detail is logged
        and treated the same as "no topology available" (E9-T5) rather
        than crashing the screen.
        """
        title = self.query_one("#detail-title", Static)
        gate_panel = self.query_one("#gate-detail", Static)
        table = self.query_one(DataTable)
        placeholder = self.query_one("#detail-placeholder", Static)

        title.update(f"[bold]{self._record.workflow_name}[/bold] ({self._record.run_id})")

        # The gate payload (agent_name/prompt/options, E6-T6) lives on
        # RunSummary, not RunDetail -- reuse the same bounded tail-read
        # derivation the Runs screen's row already relies on, rather than
        # threading gate fields through RunDetail as well (E13-T1).
        try:
            summary = derive_run_summary(self._record)
        except Exception:
            logger.warning(
                "Failed to derive run summary for run_id=%s", self._record.run_id, exc_info=True
            )
            summary = None

        if summary is not None and summary.gate is not None:
            gate_panel.display = True
            gate_panel.update(_gate_detail_text(summary))
        else:
            gate_panel.display = False
            gate_panel.update("")

        try:
            detail = derive_run_detail(self._record)
        except Exception:
            logger.warning(
                "Failed to derive run detail for run_id=%s", self._record.run_id, exc_info=True
            )
            detail = None

        if detail is None or detail.topology is None or not detail.agents:
            # A missing/unreadable event log, or one with no (yet-visible)
            # workflow_started event, degrades gracefully to a placeholder
            # (E9-T5) rather than an empty table or a crash.
            table.display = False
            placeholder.display = True
            table.clear()
            return

        table.display = True
        placeholder.display = False
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

    def _add_row(self, table: DataTable, agent: AgentDetail, key: str) -> None:
        """Add one agent's row: status, elapsed, tokens, cost -- the
        currently-running agent's row is visually highlighted (E9-T2)."""
        is_current = agent.status == "running"
        name_cell = f"▶ {agent.name}" if is_current else f"  {agent.name}"
        if is_current:
            name_cell = f"[bold]{name_cell}[/bold]"
        table.add_row(
            name_cell,
            agent.type,
            _STATUS_LABELS.get(agent.status, agent.status),
            _format_duration(agent.elapsed_seconds()),
            _format_agent_tokens(agent.tokens),
            _format_agent_cost(agent.cost_usd),
            key=key,
        )
