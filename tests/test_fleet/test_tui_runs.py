"""Pilot tests for the Fleet Manager TUI's Runs screen (Fleet Manager E7).

Uses Textual's ``App.run_test()`` to drive a real (headless) instance of
:class:`~conductor.fleet.tui.app.FleetApp` against seeded run records —
covering E7-T6: the table renders seeded records, the at-gate badge
appears, the empty state renders when no records exist, and a poll tick
picks up a newly-written record.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from textual.widgets import DataTable, Static

from conductor.cli import pid as cli_pid
from conductor.fleet.records import RunRecord, write_run_record
from conductor.fleet.tui.app import FleetApp
from conductor.fleet.tui.screens.runs import RunsScreen

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(etype: str, data: dict[str, Any] | None = None, *, ts: float | None = None) -> str:
    """Serialize a single JSONL event line matching WorkflowEvent.to_dict()'s shape."""
    return json.dumps(
        {"type": etype, "timestamp": ts if ts is not None else time.time(), "data": data or {}}
    )


def _write_events(path: Path, lines: list[str]) -> Path:
    """Write serialized JSONL event lines to ``path``."""
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture()
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect both the run-record directory and the legacy ``.pid`` directory.

    Mirrors the ``fleet_env`` fixture used by ``tests/test_fleet/test_records.py``
    and ``tests/test_cli/test_fleet_list.py`` so these pilot tests never pick
    up real records under the developer's actual ``~/.conductor/``.
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


def _write_legacy_pid_file(
    pid: int, port: int, workflow_path: str, *, run_id: str = "", log_file: str = ""
) -> Path:
    """Write a legacy port-keyed ``.pid`` file directly (pre-run_id-migration
    shape). Mirrors the identical helper in ``tests/test_fleet/test_records.py``
    -- these are the records that may carry an empty ``run_id``."""
    workflow_name = Path(workflow_path).stem
    filepath = cli_pid.pid_dir() / f"{workflow_name}-{port}.pid"
    data = {
        "pid": pid,
        "port": port,
        "workflow": str(workflow_path),
        "started_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "log_file": log_file,
    }
    filepath.write_text(json.dumps(data, indent=2))
    return filepath


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunsScreenTable:
    async def test_table_renders_seeded_records(self, fleet_env: Path, tmp_path: Path) -> None:
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)

            assert table.row_count == 1
            row = table.get_row_at(0)
            assert "alpha" in row[0]

    async def test_multiple_records_all_render(self, fleet_env: Path, tmp_path: Path) -> None:
        _write_record(
            tmp_path, "run-a", workflow_name="alpha", started_at="2026-01-01T00:00:00+00:00"
        )
        _write_record(
            tmp_path, "run-b", workflow_name="beta", started_at="2026-01-02T00:00:00+00:00"
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)

            assert table.row_count == 2
            workflows = {str(table.get_row_at(i)[0]) for i in range(table.row_count)}
            assert any("alpha" in w for w in workflows)
            assert any("beta" in w for w in workflows)

    async def test_sorted_by_recency_newest_first(self, fleet_env: Path, tmp_path: Path) -> None:
        """Flat list sorted by recency -- NOT grouped by workflow definition
        (E7-T4, the explicit Prefect lesson)."""
        _write_record(
            tmp_path, "run-old", workflow_name="oldest", started_at="2026-01-01T00:00:00+00:00"
        )
        _write_record(
            tmp_path, "run-new", workflow_name="newest", started_at="2026-03-01T00:00:00+00:00"
        )
        _write_record(
            tmp_path, "run-mid", workflow_name="middle", started_at="2026-02-01T00:00:00+00:00"
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)

            rows = [table.get_row_at(i)[0] for i in range(table.row_count)]
            assert "newest" in rows[0]
            assert "middle" in rows[1]
            assert "oldest" in rows[2]

    async def test_at_gate_badge_appears(self, fleet_env: Path, tmp_path: Path) -> None:
        log_path = tmp_path / "gate.events.jsonl"
        log_path.write_text(
            _event(
                "gate_presented",
                {
                    "agent_name": "review",
                    "prompt": "OK?",
                    "options": ["yes"],
                    "option_details": [],
                },
            )
            + "\n"
        )
        _write_record(tmp_path, "run-gate", workflow_name="gatewf", event_log_path=str(log_path))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)

            row = table.get_row_at(0)
            assert "▲" in row[0]
            assert "gatewf" in row[0]

    async def test_running_badge_appears(self, fleet_env: Path, tmp_path: Path) -> None:
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)

            row = table.get_row_at(0)
            assert "●" in row[0]


class TestRunsScreenEmptyState:
    async def test_empty_state_renders_when_no_records(self, fleet_env: Path) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            empty_state = app.screen.query_one("#empty-state", Static)
            table = app.screen.query_one(DataTable)

            assert empty_state.display is True
            assert table.display is False
            assert table.row_count == 0

    async def test_empty_state_shows_launch_affordance(self, fleet_env: Path) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            empty_state = app.screen.query_one("#empty-state", Static)

            rendered = str(empty_state.content)
            assert "conductor run" in rendered
            assert "--web-bg" in rendered

    async def test_table_shown_and_empty_state_hidden_once_records_exist(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            empty_state = app.screen.query_one("#empty-state", Static)
            table = app.screen.query_one(DataTable)

            assert table.display is True
            assert empty_state.display is False


class TestRunsScreenPolling:
    async def test_poll_tick_picks_up_new_record(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A record written *after* the screen has mounted is picked up by
        the next poll tick, without any manual refresh -- confirms the
        ~2s set_interval poll (shrunk here for a fast test) actually drives
        a rescan rather than only refreshing once at mount."""
        monkeypatch.setattr(RunsScreen, "POLL_INTERVAL_SECONDS", 0.05)

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.row_count == 0

            _write_record(tmp_path, "run-new", workflow_name="newwf")

            # Wait out several poll intervals (real time -- set_interval is
            # a real asyncio timer, not something pilot.pause() advances).
            await asyncio.sleep(0.3)
            await pilot.pause()

            assert table.row_count == 1
            row = table.get_row_at(0)
            assert "newwf" in row[0]

    async def test_poll_tick_removes_completed_run(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run whose record is removed (e.g. the process exited and
        cleaned up after itself) disappears from the table on the next
        poll tick."""
        from conductor.fleet.records import remove_run_record

        monkeypatch.setattr(RunsScreen, "POLL_INTERVAL_SECONDS", 0.05)
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.row_count == 1

            remove_run_record("run-a")

            await asyncio.sleep(0.3)
            await pilot.pause()

            assert table.row_count == 0


class TestRunsScreenDuplicateEmptyRunId:
    async def test_two_legacy_records_with_empty_run_id_do_not_crash(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """Two live legacy ``.pid`` records with empty ``run_id`` must not
        raise ``DuplicateKey`` and take the TUI down -- each gets a
        guaranteed-unique fallback row key instead."""
        log_a = tmp_path / "a.events.jsonl"
        log_a.write_text("")
        log_b = tmp_path / "b.events.jsonl"
        log_b.write_text("")
        _write_legacy_pid_file(
            os.getpid(), 9001, "/tmp/legacy-a.yaml", run_id="", log_file=str(log_a)
        )
        _write_legacy_pid_file(
            os.getpid(), 9002, "/tmp/legacy-b.yaml", run_id="", log_file=str(log_b)
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)

            assert table.row_count == 2


class TestRunsScreenQuitBinding:
    async def test_q_quits_the_app(self, fleet_env: Path) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()

            assert not app.is_running


class TestRunsScreenCursorPreservation:
    async def test_poll_tick_preserves_selected_row(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Moving the cursor onto a non-first row must survive the next
        poll tick's rebuild -- a poll must not reset the operator's
        selection back to row 0."""
        monkeypatch.setattr(RunsScreen, "POLL_INTERVAL_SECONDS", 0.05)
        _write_record(
            tmp_path, "run-a", workflow_name="alpha", started_at="2026-01-01T00:00:00+00:00"
        )
        _write_record(
            tmp_path, "run-b", workflow_name="beta", started_at="2026-01-02T00:00:00+00:00"
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.row_count == 2

            table.move_cursor(row=1)
            selected_row = table.get_row_at(table.cursor_row)

            await asyncio.sleep(0.3)
            await pilot.pause()

            assert table.get_row_at(table.cursor_row) == selected_row


# ---------------------------------------------------------------------------
# Gate visibility/resolution (Fleet Manager E13, D4) -- review round 1
# regression tests
# ---------------------------------------------------------------------------


class TestGateDetailMarkupSafety:
    async def test_markup_like_gate_fields_do_not_crash_the_panel(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """A workflow-controlled gate agent_name/prompt/option containing
        Rich markup syntax (e.g. ``[/red]``) must render as literal text
        in the gate-detail panel, never raise ``rich.errors.MarkupError``
        (E13 review round 1)."""
        log_path = tmp_path / "gate.events.jsonl"
        log_path.write_text(
            _event(
                "gate_presented",
                {
                    "agent_name": "[/red]evil agent[/bold]",
                    "prompt": "[/red]evil prompt[/bold]",
                    "options": ["[/red]evil option[/bold]"],
                    "option_details": [],
                },
            )
            + "\n"
        )
        _write_record(tmp_path, "run-gate", workflow_name="gatewf", event_log_path=str(log_path))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await pilot.pause()

            panel = app.screen.query_one("#run-preview", Static)
            assert panel.display is True
            text = str(panel.render())
            assert "[/red]evil agent[/bold]" in text
            assert "[/red]evil prompt[/bold]" in text
            assert "[/red]evil option[/bold]" in text


class TestRunsScreenScanFailurePreservesNotifierState:
    async def test_failed_scan_does_not_reset_notifier_or_displayed_state(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient failure reading run records must skip that refresh
        entirely -- neither the displayed table/records nor the
        ``TransitionNotifier`` history are reset -- so a subsequent
        successful scan does not treat an unchanged, already-notified
        gated run as a brand-new transition (E13 review round 1)."""
        _write_record(tmp_path, "run-gate", workflow_name="gatewf")

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, RunsScreen)

            # Seed the notifier as if this run had already been observed
            # at-gate once (so a second "fresh transition" would be a bug).
            screen._notifier.observe("run-gate", "at-gate")
            assert screen._notifier.observe("run-gate", "at-gate") is False

            table = app.screen.query_one(DataTable)
            assert table.row_count == 1

            with patch(
                "conductor.fleet.tui.screens.runs.read_run_records",
                side_effect=OSError("transient failure"),
            ):
                screen.refresh_runs()
                await pilot.pause()

            # The table/displayed state from before the failed scan is
            # left untouched -- not cleared to the empty state.
            assert table.row_count == 1
            assert screen._displayed_records

            # The notifier still remembers this run was already at-gate --
            # observing the same status again must not look like a fresh
            # transition.
            assert screen._notifier.observe("run-gate", "at-gate") is False


class TestGateResolveDuplicateGuard:
    async def test_second_g_press_while_resolving_is_a_noop(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """A second ``g`` press while a gate resolution is already in
        flight must not start a duplicate resolve worker (E13 review
        round 1) -- guarded by ``RunsScreen._resolving_gate``."""
        log_path = tmp_path / "gate.events.jsonl"
        log_path.write_text(
            _event(
                "gate_presented",
                {"agent_name": "reviewer", "prompt": "OK?", "options": ["yes"]},
            )
            + "\n"
        )
        _write_record(tmp_path, "run-gate", workflow_name="gatewf", event_log_path=str(log_path))

        call_count = 0

        async def _fake_resolve_gate(app, record, gate):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.2)
            return None

        with patch("conductor.fleet.tui.screens.runs.resolve_gate", _fake_resolve_gate):
            app = FleetApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)
                await pilot.pause()

                await pilot.press("g")
                await pilot.pause()
                await pilot.press("g")
                await pilot.pause(0.3)

        assert call_count == 1


# ---------------------------------------------------------------------------
# Summary bar and preview pane
# ---------------------------------------------------------------------------


class TestSummaryBar:
    """The fleet-wide line above the table: what previously could only be
    learned by counting rows by eye."""

    async def test_counts_are_grouped_by_status(self, fleet_env: Path, tmp_path: Path) -> None:
        _write_record(tmp_path, "aaaa0001", workflow_name="one")
        _write_record(tmp_path, "aaaa0002", workflow_name="two")

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            bar = str(app.screen.query_one("#summary-bar", Static).render())
            assert "2 running" in bar

    async def test_empty_fleet_says_so_rather_than_zero(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            bar = str(app.screen.query_one("#summary-bar", Static).render())
            assert "no runs" in bar
            assert "0 " not in bar

    async def test_totals_appear_once_a_run_has_usage(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "usage.events.jsonl"
        _write_events(
            log,
            [
                _event("workflow_started", {"workflow_name": "wf"}),
                _event("agent_started", {"agent_name": "a"}),
                _event(
                    "agent_completed",
                    {"agent_name": "a", "tokens": 124226, "cost_usd": 0.65},
                ),
            ],
        )
        _write_record(tmp_path, "aaaa0003", workflow_name="wf", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            bar = str(app.screen.query_one("#summary-bar", Static).render())
            assert "124k tok" in bar
            assert "$0.65" in bar


class TestPreviewPane:
    """The pane that reclaims the space a short table used to leave empty."""

    async def test_hidden_when_nothing_is_running(self, fleet_env: Path) -> None:
        """An empty bordered pane would only crowd the empty state's own
        launch hint."""
        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.query_one("#preview-pane").display is False

    async def test_shows_the_selected_run(self, fleet_env: Path, tmp_path: Path) -> None:
        _write_record(tmp_path, "aaaa0004", workflow_name="alpha", port=8410, mode="bg")

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            pane = app.screen.query_one("#preview-pane")
            assert pane.display is True
            text = str(app.screen.query_one("#run-preview", Static).render())
            assert "alpha" in text
            # Facts the one-line table row has no column for.
            assert str(os.getpid()) in text
            assert "8410" in text

    async def test_lists_the_workflow_topology(self, fleet_env: Path, tmp_path: Path) -> None:
        """Showing the run's steps is what makes the preview worth reading
        rather than a restatement of the row above it."""
        log = tmp_path / "topo.events.jsonl"
        _write_events(
            log,
            [
                _event(
                    "workflow_started",
                    {
                        "workflow_name": "wf",
                        "entry_point": "first",
                        "agents": [
                            {"name": "first", "type": "agent"},
                            {"name": "second", "type": "agent"},
                        ],
                    },
                ),
            ],
        )
        _write_record(tmp_path, "aaaa0005", workflow_name="wf", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            text = str(app.screen.query_one("#run-preview", Static).render())
            assert "first" in text
            assert "second" in text

    async def test_open_gate_is_surfaced_without_drilling_in(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "gate.events.jsonl"
        _write_events(
            log,
            [
                _event("workflow_started", {"workflow_name": "wf"}),
                _event("agent_started", {"agent_name": "planner"}),
                _event(
                    "gate_presented",
                    {
                        "agent_name": "planner",
                        "gate_id": "g1",
                        "prompt": "Approve the plan?",
                        "options": ["approve", "revise"],
                    },
                ),
            ],
        )
        _write_record(tmp_path, "aaaa0006", workflow_name="wf", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            text = str(app.screen.query_one("#run-preview", Static).render())
            assert "Approve the plan?" in text
            assert "approve" in text
