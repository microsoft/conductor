"""Pilot tests for the Fleet Manager TUI's run-detail screen (Fleet Manager E9).

Uses Textual's ``App.run_test()`` to drive a real (headless) instance of
:class:`~conductor.fleet.tui.app.FleetApp` against seeded run records,
covering E9-T5:

- ``enter`` on the Runs table pushes the detail screen for the selected run.
- ``escape`` pops back to the Runs screen via the real screen stack.
- The currently-running agent's row is visually highlighted.
- A run whose event log is missing renders a graceful placeholder rather
  than an empty table or a crash.
- The screen polls while mounted so live status/usage/highlighting update,
  and stops polling once popped.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import DataTable, Static

from conductor.fleet.records import RunRecord, write_run_record
from conductor.fleet.tui.app import FleetApp
from conductor.fleet.tui.screens.run_detail import RunDetailScreen
from conductor.fleet.tui.screens.runs import RunsScreen

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(etype: str, data: dict[str, Any] | None = None, *, ts: float | None = None) -> str:
    """Serialize a single JSONL event line matching WorkflowEvent.to_dict()'s shape."""
    return json.dumps(
        {"type": etype, "timestamp": ts if ts is not None else time.time(), "data": data or {}}
    )


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


def _workflow_started_event(agent_names: list[str]) -> str:
    return _event(
        "workflow_started",
        {
            "name": "wf",
            "entry_point": agent_names[0] if agent_names else None,
            "agents": [
                {"name": n, "type": "agent", "model": "gpt-5", "provider_name": "copilot"}
                for n in agent_names
            ],
        },
    )


@pytest.fixture()
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect both the run-record directory and the legacy ``.pid`` directory.

    Mirrors the ``fleet_env`` fixture used across
    ``tests/test_fleet/test_records.py``, ``tests/test_cli/test_fleet_list.py``,
    and ``tests/test_fleet/test_tui_runs.py``.
    """
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
    workflow_name: str | None = None,
    event_log_path: str | None = None,
    started_at: str = "2026-01-01T00:00:00+00:00",
    port: int | None = 8080,
    mode: str = "bg",
) -> RunRecord:
    """Write a live (current-process-PID) run record with a real (possibly
    empty) event log file backing it."""
    log_path = event_log_path or str(tmp_path / f"{run_id}.events.jsonl")
    if not Path(log_path).exists():
        Path(log_path).write_text("")

    record = RunRecord(
        run_id=run_id,
        pid=os.getpid(),
        workflow_path=f"/tmp/{workflow_name or run_id}.yaml",
        workflow_name=workflow_name or run_id,
        started_at=started_at,
        event_log_path=log_path,
        port=port,
        mode=mode,
        checkpoint_dir=None,
    )
    write_run_record(record)
    return record


# ---------------------------------------------------------------------------
# Navigation (E9-T1, E9-T4)
# ---------------------------------------------------------------------------


class TestRunDetailNavigation:
    async def test_enter_pushes_detail_for_selected_run(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "run-a.events.jsonl"
        _write_jsonl(log_path, [_workflow_started_event(["researcher"])])
        _write_record(tmp_path, "run-a", workflow_name="alpha", event_log_path=str(log_path))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, RunsScreen)

            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, RunDetailScreen)
            assert len(app.screen_stack) == 3  # default + RunsScreen + RunDetailScreen

    async def test_escape_returns_to_runs_screen(self, fleet_env: Path, tmp_path: Path) -> None:
        log_path = tmp_path / "run-a.events.jsonl"
        _write_jsonl(log_path, [_workflow_started_event(["researcher"])])
        _write_record(tmp_path, "run-a", workflow_name="alpha", event_log_path=str(log_path))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RunDetailScreen)

            await pilot.press("escape")
            await pilot.pause()

            assert isinstance(app.screen, RunsScreen)
            assert len(app.screen_stack) == 2  # back to default + RunsScreen

    async def test_detail_shows_correct_run(self, fleet_env: Path, tmp_path: Path) -> None:
        """Selecting a specific row pushes detail for *that* run, not just
        whichever run happens to be first."""
        log_a = tmp_path / "run-a.events.jsonl"
        log_b = tmp_path / "run-b.events.jsonl"
        _write_jsonl(log_a, [_workflow_started_event(["researcher"])])
        _write_jsonl(log_b, [_workflow_started_event(["writer"])])
        _write_record(
            tmp_path,
            "run-a",
            workflow_name="alpha",
            event_log_path=str(log_a),
            started_at="2026-01-01T00:00:00+00:00",
        )
        _write_record(
            tmp_path,
            "run-b",
            workflow_name="beta",
            event_log_path=str(log_b),
            started_at="2026-02-01T00:00:00+00:00",
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            # Runs are sorted by recency (newest first) -- "beta" (Feb) is
            # row 0, "alpha" (Jan) is row 1.
            table.move_cursor(row=1)
            await pilot.press("enter")
            await pilot.pause()

            detail_screen = app.screen
            assert isinstance(detail_screen, RunDetailScreen)
            title = detail_screen.query_one("#detail-title", Static)
            assert "alpha" in str(title.content)


# ---------------------------------------------------------------------------
# Topology + per-agent rows (E9-T1, E9-T2)
# ---------------------------------------------------------------------------


class TestRunDetailTopologyRows:
    async def test_renders_topology_as_discrete_rows(self, fleet_env: Path, tmp_path: Path) -> None:
        log_path = tmp_path / "run-a.events.jsonl"
        _write_jsonl(log_path, [_workflow_started_event(["researcher", "writer", "reviewer"])])
        _write_record(tmp_path, "run-a", event_log_path=str(log_path))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            table = app.screen.query_one(DataTable)
            assert table.row_count == 3
            names = [table.get_row_at(i)[0] for i in range(3)]
            assert any("researcher" in n for n in names)
            assert any("writer" in n for n in names)
            assert any("reviewer" in n for n in names)

    async def test_duplicate_agent_names_do_not_crash_the_screen(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """``conductor validate`` currently accepts a ``workflow_started``
        topology carrying two agents that share a name (e.g. two for-each
        iterations, or a loop-back reusing an agent id). Before the row-key
        fix, ``RunDetailScreen._add_row`` keyed rows by the bare
        ``agent.name`` and raised ``DuplicateKey`` out of the refresh; both
        rows must now render without crashing the screen."""
        log_path = tmp_path / "run-a.events.jsonl"
        _write_jsonl(log_path, [_workflow_started_event(["researcher", "researcher"])])
        _write_record(tmp_path, "run-a", event_log_path=str(log_path))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, RunDetailScreen)
            table = app.screen.query_one(DataTable)
            assert table.row_count == 2
            names = [table.get_row_at(i)[0] for i in range(2)]
            assert all("researcher" in n for n in names)

    async def test_current_step_is_highlighted(self, fleet_env: Path, tmp_path: Path) -> None:
        log_path = tmp_path / "run-a.events.jsonl"
        _write_jsonl(
            log_path,
            [
                _workflow_started_event(["researcher", "writer"]),
                _event("agent_started", {"agent_name": "researcher"}),
            ],
        )
        _write_record(tmp_path, "run-a", event_log_path=str(log_path))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            table = app.screen.query_one(DataTable)
            rows = [table.get_row_at(i) for i in range(table.row_count)]
            researcher_row = next(r for r in rows if "researcher" in r[0])
            writer_row = next(r for r in rows if "writer" in r[0])

            # The running agent's row is visually marked (E9-T2) -- distinct
            # from a not-yet-started agent's row.
            assert "▶" in researcher_row[0] or "bold" in researcher_row[0]
            assert "running" in researcher_row[2].lower()
            assert "pending" in writer_row[2].lower()

    async def test_completed_agent_shows_elapsed_tokens_cost(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "run-a.events.jsonl"
        _write_jsonl(
            log_path,
            [
                _workflow_started_event(["researcher"]),
                _event("agent_started", {"agent_name": "researcher"}),
                _event(
                    "agent_completed",
                    {
                        "agent_name": "researcher",
                        "elapsed": 12.0,
                        "tokens": 500,
                        "cost_usd": 0.05,
                    },
                ),
            ],
        )
        _write_record(tmp_path, "run-a", event_log_path=str(log_path))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            table = app.screen.query_one(DataTable)
            row = table.get_row_at(0)
            assert "completed" in row[2].lower()
            assert "12" in row[3]
            assert "500" in row[4] or "tok" in row[4]
            assert "0.05" in row[5]

    async def test_no_dag_agent_message_or_tool_output_widgets(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """Non-goals per the design: no DAG rendering, agent messages, or
        tool output. Only the documented columns are rendered."""
        log_path = tmp_path / "run-a.events.jsonl"
        _write_jsonl(log_path, [_workflow_started_event(["researcher"])])
        _write_record(tmp_path, "run-a", event_log_path=str(log_path))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            table = app.screen.query_one(DataTable)
            assert list(table.columns.values())[0].label.plain == "Agent"
            column_labels = [c.label.plain for c in table.columns.values()]
            assert column_labels == ["Agent", "Type", "Status", "Elapsed", "Tokens", "Cost"]


# ---------------------------------------------------------------------------
# Graceful degradation (E9-T5)
# ---------------------------------------------------------------------------


class TestRunDetailGracefulDegradation:
    async def test_missing_event_log_shows_placeholder(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """A run record whose event_log_path points nowhere still opens a
        detail screen -- with a graceful placeholder, not a crash or an
        empty table."""
        missing_log = tmp_path / "does-not-exist.events.jsonl"
        record = RunRecord(
            run_id="run-missing-log",
            pid=os.getpid(),
            workflow_path="/tmp/orphan.yaml",
            workflow_name="orphan",
            started_at="2026-01-01T00:00:00+00:00",
            event_log_path=str(missing_log),
            port=8080,
            mode="bg",
            checkpoint_dir=None,
        )
        write_run_record(record)

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, RunDetailScreen)
            table = app.screen.query_one(DataTable)
            placeholder = app.screen.query_one("#detail-placeholder", Static)
            assert table.display is False
            assert placeholder.display is True
            assert table.row_count == 0

    async def test_empty_event_log_shows_placeholder(self, fleet_env: Path, tmp_path: Path) -> None:
        """A log with no events yet (no workflow_started seen) also
        degrades to the placeholder rather than an empty table."""
        _write_record(tmp_path, "run-a")

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            table = app.screen.query_one(DataTable)
            placeholder = app.screen.query_one("#detail-placeholder", Static)
            assert table.display is False
            assert placeholder.display is True

    async def test_escape_from_placeholder_still_returns_to_runs(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        _write_record(tmp_path, "run-a")

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RunDetailScreen)

            await pilot.press("escape")
            await pilot.pause()

            assert isinstance(app.screen, RunsScreen)


# ---------------------------------------------------------------------------
# Live polling while mounted
# ---------------------------------------------------------------------------


class TestRunDetailPolling:
    async def test_poll_tick_picks_up_new_events(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Events appended *after* the screen has mounted (e.g. the agent
        completing) are picked up by the next poll tick -- confirms the
        screen polls the full log while mounted rather than only rendering
        once at mount time."""
        monkeypatch.setattr(RunDetailScreen, "POLL_INTERVAL_SECONDS", 0.05)

        log_path = tmp_path / "run-a.events.jsonl"
        _write_jsonl(
            log_path,
            [
                _workflow_started_event(["researcher"]),
                _event("agent_started", {"agent_name": "researcher"}),
            ],
        )
        _write_record(tmp_path, "run-a", event_log_path=str(log_path))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            table = app.screen.query_one(DataTable)
            row = table.get_row_at(0)
            assert "running" in row[2].lower()

            with log_path.open("a") as f:
                f.write(
                    _event(
                        "agent_completed",
                        {
                            "agent_name": "researcher",
                            "elapsed": 9.0,
                            "tokens": 300,
                            "cost_usd": 0.04,
                        },
                    )
                    + "\n"
                )

            # Wait out several poll intervals (real time -- set_interval is
            # a real asyncio timer, not something pilot.pause() advances).
            await asyncio.sleep(0.3)
            await pilot.pause()

            row = table.get_row_at(0)
            assert "completed" in row[2].lower()
            assert "300" in row[4] or "tok" in row[4]

    async def test_recovers_from_placeholder_once_workflow_started_arrives(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A log with no visible workflow_started event yet shows the
        placeholder at mount, then recovers to the topology table once a
        later poll tick sees it -- the placeholder must not be a permanent
        dead end for a run whose log was still being created."""
        monkeypatch.setattr(RunDetailScreen, "POLL_INTERVAL_SECONDS", 0.05)

        log_path = tmp_path / "run-a.events.jsonl"
        log_path.write_text("")
        _write_record(tmp_path, "run-a", event_log_path=str(log_path))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            table = app.screen.query_one(DataTable)
            placeholder = app.screen.query_one("#detail-placeholder", Static)
            assert table.display is False
            assert placeholder.display is True

            _write_jsonl(log_path, [_workflow_started_event(["researcher"])])

            await asyncio.sleep(0.3)
            await pilot.pause()

            assert table.display is True
            assert placeholder.display is False
            assert table.row_count == 1

    async def test_polling_stops_after_escape(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once popped back to the Runs screen, the detail screen's poll
        timer must not keep firing (it would otherwise query widgets torn
        down with the screen)."""
        monkeypatch.setattr(RunDetailScreen, "POLL_INTERVAL_SECONDS", 0.05)

        log_path = tmp_path / "run-a.events.jsonl"
        _write_jsonl(log_path, [_workflow_started_event(["researcher"])])
        _write_record(tmp_path, "run-a", event_log_path=str(log_path))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            detail_screen = app.screen
            assert isinstance(detail_screen, RunDetailScreen)

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, RunsScreen)

            # If the timer were still running, this would either raise
            # (querying a torn-down widget) or be silently swallowed by
            # Textual -- either way, letting several intervals elapse here
            # must not crash the app.
            await asyncio.sleep(0.3)
            await pilot.pause()
            assert isinstance(app.screen, RunsScreen)
