"""Pilot tests for the Fleet Manager TUI's step drill-down screen.

Reached with ``enter`` on a run-detail row. Covers the contract the screen
exists for: the prompt that went into a step and the output that came out
land in *separate, independently scrollable panes*, structured output is
pretty-printed rather than dumped on one line, and a step still in flight
shows its activity where its output will eventually go.

Uses Textual's ``App.run_test()`` against seeded run records, mirroring
``test_tui_run_detail.py``.
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Static

from conductor.fleet.records import RunRecord, write_run_record
from conductor.fleet.tui.app import FleetApp
from conductor.fleet.tui.screens.run_detail import RunDetailScreen
from conductor.fleet.tui.screens.step_detail import StepDetailScreen

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(etype: str, data: dict[str, Any] | None = None, *, ts: float | None = None) -> str:
    return json.dumps(
        {"type": etype, "timestamp": ts if ts is not None else time.time(), "data": data or {}}
    )


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


def _workflow_started_event(agent_names: list[str], name: str = "wf") -> str:
    return _event(
        "workflow_started",
        {
            "name": name,
            "entry_point": agent_names[0] if agent_names else None,
            "agents": [
                {"name": n, "type": "agent", "model": "gpt-5", "provider_name": "copilot"}
                for n in agent_names
            ],
        },
    )


@pytest.fixture()
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the run-record directory and the legacy ``.pid`` directory."""
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))

    legacy_dir = tmp_path / "legacy_runs"
    legacy_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: legacy_dir)

    return home


def _write_record(
    tmp_path: Path,
    run_id: str,
    *,
    workflow_name: str = "wf",
    event_log_path: str | None = None,
) -> RunRecord:
    log_path = event_log_path or str(tmp_path / f"{run_id}.events.jsonl")
    if not Path(log_path).exists():
        Path(log_path).write_text("")

    record = RunRecord(
        run_id=run_id,
        pid=os.getpid(),
        workflow_path=f"/tmp/{workflow_name}.yaml",
        workflow_name=workflow_name,
        started_at="2026-01-01T00:00:00+00:00",
        event_log_path=log_path,
        port=8080,
        mode="bg",
        checkpoint_dir=None,
    )
    write_run_record(record)
    return record


def _pane_text(screen: StepDetailScreen, widget_id: str) -> str:
    """Return a pane's visible text.

    ``Static.render()`` hands back a ``Text`` directly but wraps any other
    Rich renderable (the highlighted JSON, here) in a ``RichVisual``, whose
    ``str()`` is the wrapper's repr rather than its content -- so the inner
    renderable is unwrapped and printed to a recording console.
    """
    rendered = screen.query_one(f"#{widget_id}", Static).render()
    if isinstance(rendered, Text):
        return str(rendered)
    inner = getattr(rendered, "_renderable", rendered)
    # `file` keeps the render out of the test run's own stdout.
    console = Console(width=200, record=True, legacy_windows=False, file=io.StringIO())
    console.print(inner)
    return console.export_text()


async def _open_step(pilot: Any, app: FleetApp, row: int = 0) -> StepDetailScreen:
    """Drill Runs -> run detail -> step detail, returning the step screen."""
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, RunDetailScreen)

    app.screen.query_one(DataTable).move_cursor(row=row)
    await pilot.press("enter")
    # The screen reads the log in a worker thread.
    await app.workers.wait_for_complete()
    await pilot.pause()

    screen = app.screen
    assert isinstance(screen, StepDetailScreen)
    return screen


# ---------------------------------------------------------------------------
# Panes
# ---------------------------------------------------------------------------


class TestStepDetailPanes:
    async def test_prompt_and_output_land_in_separate_scrollable_panes(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "run-a.events.jsonl"
        _write_jsonl(
            log,
            [
                _workflow_started_event(["researcher"]),
                _event("agent_started", {"agent_name": "researcher"}),
                _event(
                    "agent_prompt_rendered",
                    {"agent_name": "researcher", "rendered_prompt": "PROMPT-MARKER"},
                ),
                _event(
                    "agent_completed",
                    {"agent_name": "researcher", "output": {"finding": "OUTPUT-MARKER"}},
                ),
            ],
        )
        _write_record(tmp_path, "run-a", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = await _open_step(pilot, app)

            # Each side is its own scroller: the whole point of the split is
            # that a long prompt does not push the output off the bottom.
            assert len(list(screen.query(".pane-scroll").results(VerticalScroll))) == 2

            assert "PROMPT-MARKER" in _pane_text(screen, "input-content")
            assert "OUTPUT-MARKER" in _pane_text(screen, "output-content")
            # ...and strictly on their own sides.
            assert "OUTPUT-MARKER" not in _pane_text(screen, "input-content")
            assert "PROMPT-MARKER" not in _pane_text(screen, "output-content")

    async def test_structured_output_is_pretty_printed(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """A dict output is rendered as indented JSON, not a single line."""
        log = tmp_path / "run-a.events.jsonl"
        _write_jsonl(
            log,
            [
                _workflow_started_event(["researcher"]),
                _event("agent_started", {"agent_name": "researcher"}),
                _event(
                    "agent_completed",
                    {
                        "agent_name": "researcher",
                        "output": {"found": True, "issue_number": 397},
                    },
                ),
            ],
        )
        _write_record(tmp_path, "run-a", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = await _open_step(pilot, app)

            rendered = _pane_text(screen, "output-content")
            assert '"issue_number": 397' in rendered
            # Indented across lines rather than `{'found': True, ...}`.
            assert rendered.count("\n") >= 3
            assert "True" not in rendered  # JSON `true`, not Python `True`

    async def test_tab_moves_focus_between_the_panes(self, fleet_env: Path, tmp_path: Path) -> None:
        """Only the focused pane scrolls, so swapping must not need a mouse."""
        log = tmp_path / "run-a.events.jsonl"
        _write_jsonl(
            log,
            [
                _workflow_started_event(["researcher"]),
                _event("agent_started", {"agent_name": "researcher"}),
                _event(
                    "agent_prompt_rendered",
                    {"agent_name": "researcher", "rendered_prompt": "p"},
                ),
                _event("agent_completed", {"agent_name": "researcher", "output": {"a": 1}}),
            ],
        )
        _write_record(tmp_path, "run-a", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = await _open_step(pilot, app)

            screen.action_focus_next_pane()
            await pilot.pause()
            first = screen.focused

            screen.action_focus_next_pane()
            await pilot.pause()
            second = screen.focused

            assert first is not None
            assert second is not None
            assert first is not second

    async def test_running_step_shows_activity_where_output_would_go(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """A step in flight has no output; a blank pane would read as broken."""
        log = tmp_path / "run-a.events.jsonl"
        _write_jsonl(
            log,
            [
                _workflow_started_event(["researcher"]),
                _event("agent_started", {"agent_name": "researcher"}),
                _event(
                    "agent_prompt_rendered",
                    {"agent_name": "researcher", "rendered_prompt": "p"},
                ),
                _event(
                    "agent_tool_start",
                    {"agent_name": "researcher", "tool_name": "grep_the_repo"},
                ),
            ],
        )
        _write_record(tmp_path, "run-a", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = await _open_step(pilot, app)

            assert "Activity" in _pane_text(screen, "output-heading")
            assert "grep_the_repo" in _pane_text(screen, "output-content")

    async def test_missing_prompt_is_stated_not_left_blank(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "run-a.events.jsonl"
        _write_jsonl(
            log,
            [
                _workflow_started_event(["researcher"]),
                _event("agent_started", {"agent_name": "researcher"}),
                _event("agent_completed", {"agent_name": "researcher", "output": {"a": 1}}),
            ],
        )
        _write_record(tmp_path, "run-a", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = await _open_step(pilot, app)
            assert "No rendered prompt" in _pane_text(screen, "input-content")


# ---------------------------------------------------------------------------
# Layout + title
# ---------------------------------------------------------------------------


class TestStepDetailChrome:
    async def test_title_prefers_the_declared_workflow_name(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """As on every other screen: the declared name, not the file stem."""
        log = tmp_path / "run-a.events.jsonl"
        _write_jsonl(
            log,
            [
                _workflow_started_event(["researcher"], name="ship"),
                _event("agent_started", {"agent_name": "researcher"}),
                _event("agent_completed", {"agent_name": "researcher", "output": {"a": 1}}),
            ],
        )
        _write_record(tmp_path, "run-a", workflow_name="workflow", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = await _open_step(pilot, app)

            title = _pane_text(screen, "step-title")
            assert "researcher" in title
            assert "ship" in title

    async def test_panes_stack_on_a_narrow_terminal(self, fleet_env: Path, tmp_path: Path) -> None:
        """Side-by-side halves of an 80-column terminal wrap into a ribbon."""
        log = tmp_path / "run-a.events.jsonl"
        _write_jsonl(
            log,
            [
                _workflow_started_event(["researcher"]),
                _event("agent_started", {"agent_name": "researcher"}),
                _event("agent_completed", {"agent_name": "researcher", "output": {"a": 1}}),
            ],
        )
        _write_record(tmp_path, "run-a", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            screen = await _open_step(pilot, app)
            assert screen.query_one("#step-panes").has_class("-stacked")

    async def test_panes_sit_side_by_side_on_a_wide_terminal(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "run-a.events.jsonl"
        _write_jsonl(
            log,
            [
                _workflow_started_event(["researcher"]),
                _event("agent_started", {"agent_name": "researcher"}),
                _event("agent_completed", {"agent_name": "researcher", "output": {"a": 1}}),
            ],
        )
        _write_record(tmp_path, "run-a", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            screen = await _open_step(pilot, app)
            assert not screen.query_one("#step-panes").has_class("-stacked")

    async def test_escape_pops_back_to_run_detail(self, fleet_env: Path, tmp_path: Path) -> None:
        log = tmp_path / "run-a.events.jsonl"
        _write_jsonl(
            log,
            [
                _workflow_started_event(["researcher"]),
                _event("agent_started", {"agent_name": "researcher"}),
                _event("agent_completed", {"agent_name": "researcher", "output": {"a": 1}}),
            ],
        )
        _write_record(tmp_path, "run-a", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_step(pilot, app)

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, RunDetailScreen)
