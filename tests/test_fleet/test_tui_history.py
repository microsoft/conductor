"""Pilot tests for the Fleet Manager TUI's History screen (Fleet Manager E14).

Uses Textual's ``App.run_test()`` to drive a real (headless) instance of
:class:`~conductor.fleet.tui.app.FleetApp` against a real event-log
directory (redirected via ``tempfile.gettempdir``, mirroring
``tests/test_fleet/test_history.py``'s own fixture), covering E14-T2/E14-T3:

- ``h`` from the Runs screen pushes the History screen; ``escape`` returns.
- The table renders workflow/outcome/duration/tokens/cost for completed,
  failed, and outcome-unknown logs.
- The empty state renders when there is no history yet.
- Selecting a row offers ``conductor replay <log>`` via a notification --
  it never opens a replay dashboard itself (depth is delegated, not
  re-implemented for viewing).
- ``TestHistoryResume`` (issue #460): pressing ``r`` on a row correlated to
  an on-disk checkpoint resumes it in the background via the exact same
  ``launch_background_resume`` path ``conductor resume --web-bg`` uses,
  gated purely on checkpoint existence -- never on the row's ``outcome``.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from textual.widgets import DataTable, Static

from conductor.cli.bg_runner import BackgroundLaunch
from conductor.engine.checkpoint import CheckpointManager
from conductor.fleet.history import build_history_entries
from conductor.fleet.retention import event_log_root
from conductor.fleet.tui.app import FleetApp
from conductor.fleet.tui.screens.history import HistoryScreen
from conductor.fleet.tui.screens.runs import RunsScreen
from tests.test_fleet.conftest import settle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect both the run-record directory and the legacy ``.pid`` directory.

    Mirrors the ``fleet_env`` fixture used across the other TUI pilot test
    modules -- the History screen itself never touches run records, but
    the app still mounts the Runs screen first, which does.
    """
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))

    legacy_dir = tmp_path / "legacy_runs"
    legacy_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: legacy_dir)

    return home


@pytest.fixture()
def event_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``event_log_root()`` to an isolated directory.

    Mirrors ``tests/test_fleet/test_history.py``'s own ``temp_root``
    fixture -- patches ``tempfile.gettempdir`` directly since ``tempfile``
    caches its resolved directory per-process. This is also the same seam
    ``CheckpointManager.get_checkpoints_dir()`` reads, so a checkpoint
    written under it and an event log written under it can genuinely join.
    """
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return event_log_root()


def _event(etype: str, data: dict[str, Any] | None = None, *, ts: float | None = None) -> str:
    """Serialize a single JSONL event line matching WorkflowEvent.to_dict()'s shape."""
    return json.dumps(
        {"type": etype, "timestamp": ts if ts is not None else time.time(), "data": data or {}}
    )


def _write_log(
    root: Path,
    *,
    name: str = "my-workflow",
    ts: str = "20260101-120000",
    run_id: str = "deadbeef",
    lines: list[str] | None = None,
) -> Path:
    """Write a ``conductor-<name>-<ts>-<run_id>.events.jsonl`` file under ``root``."""
    path = root / f"conductor-{name}-{ts}-{run_id}.events.jsonl"
    path.write_text("\n".join(lines or []) + ("\n" if lines else ""))
    return path


_RESUME_WORKFLOW_YAML = """\
workflow:
  name: resumable-workflow
  entry_point: helper
agents:
  - name: helper
    model: copilot
    prompt: Say hello
"""


@pytest.fixture()
def resume_workflow_file(tmp_path: Path) -> Path:
    """A real workflow YAML on disk -- ``correlate_checkpoints``'s validity
    filter requires the checkpoint's recorded ``workflow_path`` to exist."""
    path = tmp_path / "resumable-workflow.yaml"
    path.write_text(_RESUME_WORKFLOW_YAML)
    return path


def _write_checkpoint(
    workflow_path: Path,
    *,
    event_log_path: str,
    run_id: str = "",
    created_at: str | None = None,
    current_agent: str = "step-1",
    trigger: str = "failure",
) -> Path:
    """Write a schema-valid checkpoint JSON file to
    ``CheckpointManager.get_checkpoints_dir()`` -- which, under the
    ``event_log_dir`` fixture, resolves under the same ``tmp_path`` as the
    event logs it correlates to.
    """
    if created_at is None:
        created_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    data = {
        "version": CheckpointManager.CHECKPOINT_VERSION,
        "workflow_path": str(workflow_path),
        "workflow_hash": "sha256:deadbeef",
        "created_at": created_at,
        "failure": {"error_type": None, "message": None, "agent": current_agent, "iteration": 0},
        "inputs": {},
        "current_agent": current_agent,
        "context": {},
        "limits": {},
        "run_id": run_id,
        "event_log_path": event_log_path,
        "trigger": trigger,
    }
    checkpoints_dir = CheckpointManager.get_checkpoints_dir()
    path = checkpoints_dir / f"{workflow_path.stem}-{run_id or 'nocp'}.json"
    path.write_text(json.dumps(data))
    return path


async def _goto_history(pilot) -> None:
    """Navigate from the (already-mounted) Runs screen to History."""
    await settle(pilot)
    await pilot.press("h")
    await settle(pilot)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


class TestHistoryNavigation:
    async def test_h_pushes_history_screen(self, fleet_env: Path, event_log_dir: Path) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            assert isinstance(app.screen, RunsScreen)
            await _goto_history(pilot)

            assert isinstance(app.screen, HistoryScreen)

    async def test_escape_returns_to_runs(self, fleet_env: Path, event_log_dir: Path) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_history(pilot)
            assert isinstance(app.screen, HistoryScreen)

            await pilot.press("escape")
            await settle(pilot)

            assert isinstance(app.screen, RunsScreen)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestHistoryListing:
    async def test_markup_like_workflow_name_does_not_crash_or_misrender(
        self, fleet_env: Path, event_log_dir: Path
    ) -> None:
        """A workflow name recovered from the filename is data, not
        authored Rich markup -- a value containing markup-like bracket
        syntax (e.g. ``evil[bold]name``; a filename cannot itself contain
        a literal ``/``, so a full closing tag like ``[/red]`` cannot
        appear here) must render as literal text in the table, never be
        silently swallowed/misrendered as markup (E14 review round 1)."""
        _write_log(
            event_log_dir,
            name="evil[bold]workflow",
            run_id="00000009",
            lines=[
                _event("workflow_started", {"name": "evil"}, ts=1000.0),
                _event("workflow_completed", {"elapsed": 1.0}, ts=1001.0),
            ],
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_history(pilot)

            table = app.screen.query_one(DataTable)
            assert table.row_count == 1
            row = table.get_row_at(0)

            assert row[0].plain == "evil[bold]workflow"

    async def test_renders_completed_failed_and_unknown_logs(
        self, fleet_env: Path, event_log_dir: Path
    ) -> None:
        _write_log(
            event_log_dir,
            name="completed-wf",
            run_id="00000001",
            lines=[
                _event("workflow_started", {"name": "completed-wf"}, ts=1000.0),
                _event(
                    "agent_completed",
                    {"agent_name": "a", "tokens": 500, "cost_usd": 0.25},
                    ts=1010.0,
                ),
                _event("workflow_completed", {"elapsed": 42.0}, ts=1042.0),
            ],
        )
        _write_log(
            event_log_dir,
            name="failed-wf",
            run_id="00000002",
            lines=[
                _event("workflow_started", {"name": "failed-wf"}, ts=2000.0),
                _event("workflow_failed", {"error_type": "ValueError"}, ts=2010.0),
            ],
        )
        _write_log(
            event_log_dir,
            name="unknown-wf",
            run_id="00000003",
            lines=[_event("workflow_started", {"name": "unknown-wf"}, ts=3000.0)],
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_history(pilot)

            table = app.screen.query_one(DataTable)
            assert table.row_count == 3
            rows = [table.get_row_at(i) for i in range(3)]

            completed_row = next(r for r in rows if str(r[0]) == "completed-wf")
            assert "completed" in str(completed_row[1]).lower()
            assert "42s" in str(completed_row[2])
            assert "500" in str(completed_row[3]) or "tok" in str(completed_row[3])
            assert "0.25" in str(completed_row[4])

            failed_row = next(r for r in rows if str(r[0]) == "failed-wf")
            assert "failed" in str(failed_row[1]).lower()

            unknown_row = next(r for r in rows if str(r[0]) == "unknown-wf")
            assert "unknown" in str(unknown_row[1]).lower()
            assert "running" not in str(unknown_row[1]).lower()

    async def test_empty_state_renders_when_no_history(
        self, fleet_env: Path, event_log_dir: Path
    ) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_history(pilot)

            table = app.screen.query_one(DataTable)
            assert table.display is False
            empty_state = app.screen.query_one("#history-empty-state", Static)
            assert empty_state.display is True

    async def test_list_is_bounded_by_retention(self, fleet_env: Path, event_log_dir: Path) -> None:
        for i in range(5):
            _write_log(event_log_dir, name=f"wf{i}", run_id=f"{i:08x}", lines=[])

        with patch("conductor.fleet.history._resolve_keep_last", return_value=2):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_history(pilot)

                table = app.screen.query_one(DataTable)
                assert table.row_count == 2

    async def test_duplicate_run_id_does_not_crash_the_screen(
        self, fleet_env: Path, event_log_dir: Path
    ) -> None:
        """Two retained event logs can share a ``run_id`` in ordinary use:
        ``CONDUCTOR_RUN_ID`` is inherited by any nested ``conductor``
        invocation (exactly what ``bg_runner`` exports to its child), so a
        workflow that shells out to ``conductor`` from a ``type: script``
        step produces sibling logs under one id. Before the row-key fix,
        ``HistoryScreen.load_history`` keyed rows by the bare ``run_id`` and
        crashed with ``DuplicateKey`` out of ``on_mount``; both rows must
        now render without raising."""
        _write_log(
            event_log_dir,
            name="wf-a",
            run_id="aaaa1111",
            lines=[
                _event("workflow_started", {"name": "wf-a"}, ts=1000.0),
                _event("workflow_completed", {"elapsed": 1.0}, ts=1001.0),
            ],
        )
        _write_log(
            event_log_dir,
            name="wf-b",
            run_id="aaaa1111",
            ts="20260101-130000",
            lines=[
                _event("workflow_started", {"name": "wf-b"}, ts=2000.0),
                _event("workflow_failed", {"error_type": "ValueError"}, ts=2001.0),
            ],
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_history(pilot)

            table = app.screen.query_one(DataTable)
            assert table.row_count == 2
            names = {str(table.get_row_at(i)[0]) for i in range(2)}
            assert names == {"wf-a", "wf-b"}


# ---------------------------------------------------------------------------
# Depth delegated to `conductor replay` (E14-T3)
# ---------------------------------------------------------------------------


class TestHistoryReplayDelegation:
    async def test_selecting_a_row_offers_the_replay_command(
        self, fleet_env: Path, event_log_dir: Path
    ) -> None:
        log_path = _write_log(
            event_log_dir,
            name="completed-wf",
            run_id="00000001",
            lines=[
                _event("workflow_started", {"name": "completed-wf"}, ts=1000.0),
                _event("workflow_completed", {"elapsed": 42.0}, ts=1042.0),
            ],
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_history(pilot)

            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            with patch.object(HistoryScreen, "notify") as mock_notify:
                await pilot.press("enter")
                await settle(pilot)

        mock_notify.assert_called_once()
        message = mock_notify.call_args.args[0]
        assert "conductor replay" in message
        assert str(log_path) in message

    async def test_selecting_a_row_never_opens_a_dashboard_or_browser(
        self, fleet_env: Path, event_log_dir: Path
    ) -> None:
        """Depth is delegated, not re-implemented -- selecting a history
        row must never itself start a replay dashboard or open a
        browser."""
        _write_log(
            event_log_dir,
            name="completed-wf",
            run_id="00000001",
            lines=[
                _event("workflow_started", {"name": "completed-wf"}, ts=1000.0),
                _event("workflow_completed", {"elapsed": 42.0}, ts=1042.0),
            ],
        )

        with (
            patch("webbrowser.open") as mock_open,
            patch("conductor.web.replay.ReplayDashboard") as mock_dashboard,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_history(pilot)
                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)
                await pilot.press("enter")
                await settle(pilot)

        mock_open.assert_not_called()
        mock_dashboard.assert_not_called()


# ---------------------------------------------------------------------------
# Off-loop loading (issue #437)
# ---------------------------------------------------------------------------


class TestHistoryWorkerThreading:
    async def test_build_history_entries_runs_off_the_main_thread(
        self, fleet_env: Path, event_log_dir: Path
    ) -> None:
        _write_log(
            event_log_dir,
            name="completed-wf",
            run_id="00000001",
            lines=[
                _event("workflow_started", {"name": "completed-wf"}, ts=1000.0),
                _event("workflow_completed", {"elapsed": 42.0}, ts=1042.0),
            ],
        )

        seen_main_thread: list[bool] = []

        def _tracking_build():
            seen_main_thread.append(threading.current_thread() is threading.main_thread())
            return build_history_entries()

        with patch(
            "conductor.fleet.tui.screens.history.build_history_entries",
            side_effect=_tracking_build,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_history(pilot)

        assert seen_main_thread == [False]

    @pytest.mark.parametrize(
        ("write_log", "revealed_id"),
        [(True, "#history-table"), (False, "#history-empty-state")],
    )
    async def test_loading_indicator_shows_then_yields_to_the_result(
        self, fleet_env: Path, event_log_dir: Path, write_log: bool, revealed_id: str
    ) -> None:
        if write_log:
            _write_log(
                event_log_dir,
                name="completed-wf",
                run_id="00000001",
                lines=[
                    _event("workflow_started", {"name": "completed-wf"}, ts=1000.0),
                    _event("workflow_completed", {"elapsed": 42.0}, ts=1042.0),
                ],
            )

        release = threading.Event()

        def _blocking_build():
            release.wait(timeout=10)
            return build_history_entries()

        with patch(
            "conductor.fleet.tui.screens.history.build_history_entries",
            side_effect=_blocking_build,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("h")
                await pilot.pause()

                # Assert the whole pre-load frame, not just that a Static
                # exists: a freshly-mounted Static defaults to display=True,
                # so checking only that would pass against a screen which
                # never touched it (issue #446 review).
                loading = app.screen.query_one("#history-loading", Static)
                assert loading.display is True
                assert "Loading" in str(loading.render())
                assert app.screen.query_one(DataTable).display is False
                assert app.screen.query_one("#history-empty-state", Static).display is False

                release.set()
                await settle(pilot)

                assert loading.display is False
                assert app.screen.query_one(revealed_id, Static | DataTable).display is True

    async def test_a_failed_build_is_not_reported_as_no_history(
        self, fleet_env: Path, event_log_dir: Path
    ) -> None:
        """Degrading a read failure into the empty state makes a positive
        claim of absence -- and because this screen loads once, with no
        poll to self-heal, that claim is permanent and the operator stops
        looking (issue #446 review)."""
        with patch(
            "conductor.fleet.tui.screens.history.build_history_entries",
            side_effect=OSError("unreadable"),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("h")
                await settle(pilot)

                assert app.is_running
                assert app.screen.query_one("#history-empty-state", Static).display is False
                loading = app.screen.query_one("#history-loading", Static)
                assert loading.display is True
                assert "Could not read run history" in str(loading.render())

    async def test_a_render_failure_does_not_exit_the_app(
        self, fleet_env: Path, event_log_dir: Path
    ) -> None:
        """``@work`` defaults to ``exit_on_error``, so an unguarded render
        exception here would take the whole TUI down -- while the identical
        bug on the two polled screens is only a logged warning."""
        with patch.object(HistoryScreen, "_render_history", side_effect=RuntimeError("render bug")):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("h")
                await settle(pilot)

                assert app.is_running


# ---------------------------------------------------------------------------
# Resume from a correlated checkpoint (issue #460)
# ---------------------------------------------------------------------------


class TestHistoryResume:
    async def test_matched_row_offers_resume(
        self, fleet_env: Path, event_log_dir: Path, resume_workflow_file: Path
    ) -> None:
        log_path = _write_log(
            event_log_dir,
            name="resumable-workflow",
            run_id="aaaa1111",
            lines=[_event("workflow_started", {"name": "resumable-workflow"}, ts=1000.0)],
        )
        _write_checkpoint(resume_workflow_file, event_log_path=str(log_path), run_id="aaaa1111")

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_history(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)

            assert app.screen.check_action("resume", ()) is True

    async def test_unmatched_row_hides_resume(self, fleet_env: Path, event_log_dir: Path) -> None:
        _write_log(
            event_log_dir,
            name="no-checkpoint-workflow",
            run_id="bbbb1111",
            lines=[_event("workflow_started", {"name": "no-checkpoint-workflow"}, ts=1000.0)],
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_history(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)

            assert app.screen.check_action("resume", ()) is False

    async def test_explicit_terminate_failure_with_no_checkpoint_hides_resume(
        self, fleet_env: Path, event_log_dir: Path
    ) -> None:
        """A ``status: failed`` terminate step writes no checkpoint by
        design -- gating must fall out of "no checkpoint correlates",
        never a special-cased read of ``is_explicit``/``outcome``."""
        _write_log(
            event_log_dir,
            name="terminated-workflow",
            run_id="cccc1111",
            lines=[
                _event("workflow_started", {"name": "terminated-workflow"}, ts=1000.0),
                _event(
                    "workflow_failed",
                    {"error_type": "WorkflowTerminated", "is_explicit": True},
                    ts=1001.0,
                ),
            ],
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_history(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)

            assert app.screen.check_action("resume", ()) is False

    async def test_missing_workflow_path_hides_resume(
        self, fleet_env: Path, event_log_dir: Path, tmp_path: Path
    ) -> None:
        log_path = _write_log(
            event_log_dir,
            name="gone-workflow",
            run_id="dddd1111",
            lines=[_event("workflow_started", {"name": "gone-workflow"}, ts=1000.0)],
        )
        missing_workflow = tmp_path / "gone.yaml"  # never written
        _write_checkpoint(missing_workflow, event_log_path=str(log_path), run_id="dddd1111")

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_history(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)

            assert app.screen.check_action("resume", ()) is False

    async def test_completed_row_with_checkpoint_offers_resume(
        self, fleet_env: Path, event_log_dir: Path, resume_workflow_file: Path
    ) -> None:
        """Q2 regression guard: gating is checkpoint-driven only. A
        ``completed`` row must offer Resume exactly like any other outcome
        when a checkpoint correlates to it -- no outcome special-case."""
        log_path = _write_log(
            event_log_dir,
            name="resumable-workflow",
            run_id="eeee1111",
            lines=[
                _event("workflow_started", {"name": "resumable-workflow"}, ts=1000.0),
                _event("workflow_completed", {"elapsed": 1.0}, ts=1001.0),
            ],
        )
        _write_checkpoint(resume_workflow_file, event_log_path=str(log_path), run_id="eeee1111")

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_history(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)

            assert app.screen.check_action("resume", ()) is True

    async def test_unknown_row_with_periodic_checkpoint_offers_resume(
        self, fleet_env: Path, event_log_dir: Path, resume_workflow_file: Path
    ) -> None:
        log_path = _write_log(
            event_log_dir,
            name="resumable-workflow",
            run_id="ffff1111",
            lines=[_event("workflow_started", {"name": "resumable-workflow"}, ts=1000.0)],
        )
        _write_checkpoint(
            resume_workflow_file,
            event_log_path=str(log_path),
            run_id="ffff1111",
            trigger="periodic",
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_history(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)

            assert app.screen.check_action("resume", ()) is True

    async def test_pressing_r_launches_resume_and_returns_to_runs(
        self, fleet_env: Path, event_log_dir: Path, resume_workflow_file: Path
    ) -> None:
        log_path = _write_log(
            event_log_dir,
            name="resumable-workflow",
            run_id="00001111",
            lines=[_event("workflow_started", {"name": "resumable-workflow"}, ts=1000.0)],
        )
        cp_path = _write_checkpoint(
            resume_workflow_file, event_log_path=str(log_path), run_id="00001111"
        )

        launch = BackgroundLaunch(
            url="http://127.0.0.1:8080",
            stderr_log=event_log_dir / "00001111.bg.stderr.log",
            stdout_log=event_log_dir / "00001111.bg.stdout.log",
            run_id="00001111",
        )
        fake_launch = Mock(return_value=launch)

        app = FleetApp()
        async with app.run_test() as pilot:
            with patch("conductor.cli.bg_runner.launch_background_resume", fake_launch):
                await _goto_history(pilot)
                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)

                await pilot.press("r")
                await settle(pilot)

            assert isinstance(app.screen, RunsScreen)

        fake_launch.assert_called_once()
        _args, kwargs = fake_launch.call_args
        assert kwargs["workflow_path"] is None
        assert kwargs["checkpoint_path"] == cp_path

    async def test_pressing_r_resumes_the_highlighted_rows_checkpoint(
        self, fleet_env: Path, event_log_dir: Path, resume_workflow_file: Path
    ) -> None:
        """Blocking finding 4 (issue #460 review): every existing resume
        test wrote exactly one row, so ``move_cursor(row=0)`` was a no-op
        and nothing verified that ``r`` resumes the *highlighted* row
        rather than always the first one. Two mutation probes confirmed
        the gap: replacing ``_selected_entry`` with a version that ignores
        the cursor, and deleting ``on_data_table_row_highlighted``
        entirely, both left the full suite green."""

        log_path_a = _write_log(
            event_log_dir,
            name="wf-a",
            run_id="aaaa0001",
            lines=[_event("workflow_started", {"name": "wf-a"}, ts=1000.0)],
        )
        log_path_b = _write_log(
            event_log_dir,
            name="wf-b",
            run_id="bbbb0002",
            lines=[_event("workflow_started", {"name": "wf-b"}, ts=1000.0)],
        )
        # `build_history_entries` sorts newest-mtime-first -- pin the order
        # explicitly rather than relying on write-call timing, which is a
        # sub-millisecond race on a fast filesystem.
        now = time.time()
        os.utime(log_path_a, (now - 10, now - 10))
        os.utime(log_path_b, (now, now))
        # Row 0 is therefore wf-b (newer), row 1 is wf-a (older).

        cp_a = _write_checkpoint(
            resume_workflow_file, event_log_path=str(log_path_a), run_id="aaaa0001"
        )
        cp_b = _write_checkpoint(
            resume_workflow_file, event_log_path=str(log_path_b), run_id="bbbb0002"
        )

        launch = BackgroundLaunch(
            url="http://127.0.0.1:8080",
            stderr_log=event_log_dir / "resumed.bg.stderr.log",
            stdout_log=event_log_dir / "resumed.bg.stdout.log",
            run_id="resumed",
        )
        fake_launch = Mock(return_value=launch)

        app = FleetApp()
        async with app.run_test() as pilot:
            with patch("conductor.cli.bg_runner.launch_background_resume", fake_launch):
                await _goto_history(pilot)
                table = app.screen.query_one(DataTable)
                table.move_cursor(row=1)

                await pilot.press("r")
                await settle(pilot)

        fake_launch.assert_called_once()
        _args, kwargs = fake_launch.call_args
        assert kwargs["checkpoint_path"] == cp_a
        assert kwargs["checkpoint_path"] != cp_b

    async def test_pressing_r_resumes_row_zero_when_highlighted(
        self, fleet_env: Path, event_log_dir: Path, resume_workflow_file: Path
    ) -> None:
        """Mirror of the above with the cursor left on row 0, so a naive
        "always resume the first displayed entry" implementation can't pass
        both."""

        log_path_a = _write_log(
            event_log_dir,
            name="wf-a",
            run_id="aaaa0003",
            lines=[_event("workflow_started", {"name": "wf-a"}, ts=1000.0)],
        )
        log_path_b = _write_log(
            event_log_dir,
            name="wf-b",
            run_id="bbbb0004",
            lines=[_event("workflow_started", {"name": "wf-b"}, ts=1000.0)],
        )
        now = time.time()
        os.utime(log_path_a, (now - 10, now - 10))
        os.utime(log_path_b, (now, now))
        # Row 0 is wf-b (newer), row 1 is wf-a (older).

        cp_a = _write_checkpoint(
            resume_workflow_file, event_log_path=str(log_path_a), run_id="aaaa0003"
        )
        cp_b = _write_checkpoint(
            resume_workflow_file, event_log_path=str(log_path_b), run_id="bbbb0004"
        )

        launch = BackgroundLaunch(
            url="http://127.0.0.1:8080",
            stderr_log=event_log_dir / "resumed.bg.stderr.log",
            stdout_log=event_log_dir / "resumed.bg.stdout.log",
            run_id="resumed",
        )
        fake_launch = Mock(return_value=launch)

        app = FleetApp()
        async with app.run_test() as pilot:
            with patch("conductor.cli.bg_runner.launch_background_resume", fake_launch):
                await _goto_history(pilot)
                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)

                await pilot.press("r")
                await settle(pilot)

        fake_launch.assert_called_once()
        _args, kwargs = fake_launch.call_args
        assert kwargs["checkpoint_path"] == cp_b
        assert kwargs["checkpoint_path"] != cp_a

    async def test_pressing_r_on_a_row_without_a_checkpoint_launches_nothing(
        self, fleet_env: Path, event_log_dir: Path
    ) -> None:
        """The three existing "hides resume" tests only assert
        ``check_action``'s return value, never that the key is actually
        inert -- so they'd all pass even if the binding fired regardless.
        Drive the real key here and assert nothing was launched."""
        _write_log(
            event_log_dir,
            name="no-checkpoint-workflow",
            run_id="cccc0005",
            lines=[_event("workflow_started", {"name": "no-checkpoint-workflow"}, ts=1000.0)],
        )

        fake_launch = Mock()

        app = FleetApp()
        async with app.run_test() as pilot:
            with patch("conductor.cli.bg_runner.launch_background_resume", fake_launch):
                await _goto_history(pilot)
                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)

                await pilot.press("r")
                await settle(pilot)

            assert isinstance(app.screen, HistoryScreen)

        fake_launch.assert_not_called()

    async def test_cursor_movement_flips_the_resume_gate(
        self, fleet_env: Path, event_log_dir: Path, resume_workflow_file: Path
    ) -> None:
        """Drive real cursor movement (not ``move_cursor`` directly) between
        a resumable row and a non-resumable one, and assert the footer gate
        actually flips -- covering ``on_data_table_row_highlighted``, which
        a mutation probe showed was otherwise dead code as far as the suite
        was concerned."""

        log_path_resumable = _write_log(
            event_log_dir,
            name="resumable-workflow",
            run_id="dddd0006",
            lines=[_event("workflow_started", {"name": "resumable-workflow"}, ts=1000.0)],
        )
        log_path_bare = _write_log(
            event_log_dir,
            name="no-checkpoint-workflow",
            run_id="eeee0007",
            lines=[_event("workflow_started", {"name": "no-checkpoint-workflow"}, ts=1000.0)],
        )
        now = time.time()
        os.utime(log_path_bare, (now, now))
        os.utime(log_path_resumable, (now - 10, now - 10))
        # Row 0 is the bare (non-resumable) log, row 1 is the resumable one.
        _write_checkpoint(
            resume_workflow_file, event_log_path=str(log_path_resumable), run_id="dddd0006"
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_history(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await settle(pilot)

            assert app.screen.check_action("resume", ()) is False

            await pilot.press("down")
            await settle(pilot)

            assert app.screen.check_action("resume", ()) is True

    async def test_run_record_not_written_shows_warning_notification(
        self, fleet_env: Path, event_log_dir: Path, resume_workflow_file: Path
    ) -> None:
        """Issue #435: a resume that succeeded but could not confirm its own
        discovery record must warn -- a real ``BackgroundLaunch`` is
        required since a ``Mock(...)`` would leave ``run_record_written``
        an auto-created truthy attribute (mirrors
        ``test_tui_new_run.py``'s identical case)."""
        log_path = _write_log(
            event_log_dir,
            name="resumable-workflow",
            run_id="00002222",
            lines=[_event("workflow_started", {"name": "resumable-workflow"}, ts=1000.0)],
        )
        _write_checkpoint(resume_workflow_file, event_log_path=str(log_path), run_id="00002222")

        launch = BackgroundLaunch(
            url="http://127.0.0.1:8080",
            stderr_log=event_log_dir / "00002222.bg.stderr.log",
            stdout_log=event_log_dir / "00002222.bg.stdout.log",
            run_id="00002222",
            run_record_written=False,
        )

        notifications: list[tuple[str, str]] = []

        app = FleetApp()
        async with app.run_test() as pilot:
            original_notify = app.notify

            def _capture(message, **kwargs):
                notifications.append((message, str(kwargs.get("severity", "information"))))
                original_notify(message, **kwargs)

            with (
                patch(
                    "conductor.cli.bg_runner.launch_background_resume",
                    Mock(return_value=launch),
                ),
                patch.object(app, "notify", _capture),
            ):
                await _goto_history(pilot)
                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)

                await pilot.press("r")
                await settle(pilot)

            assert isinstance(app.screen, RunsScreen)

        warnings = [message for message, severity in notifications if severity == "warning"]
        assert any("could not register itself for discovery" in m for m in warnings), notifications

    async def test_resume_that_already_completed_reports_no_url(
        self, fleet_env: Path, event_log_dir: Path, resume_workflow_file: Path
    ) -> None:
        """Blocking finding 2 (issue #460 review): this action previously
        reported ``Resumed: {launch.url}`` unconditionally, in direct
        violation of ``BackgroundLaunch.still_running``'s documented
        contract (issue #410) -- a real ``BackgroundLaunch`` is required
        since a ``Mock(...)`` would leave ``still_running`` an
        auto-created truthy attribute and this branch unreachable."""
        log_path = _write_log(
            event_log_dir,
            name="resumable-workflow",
            run_id="00007777",
            lines=[_event("workflow_started", {"name": "resumable-workflow"}, ts=1000.0)],
        )
        _write_checkpoint(resume_workflow_file, event_log_path=str(log_path), run_id="00007777")

        launch = BackgroundLaunch(
            url="http://127.0.0.1:8080",
            stderr_log=event_log_dir / "00007777.bg.stderr.log",
            stdout_log=event_log_dir / "00007777.bg.stdout.log",
            run_id="00007777",
            still_running=False,
        )

        notifications: list[tuple[str, str]] = []

        app = FleetApp()
        async with app.run_test() as pilot:
            original_notify = app.notify

            def _capture(message, **kwargs):
                notifications.append((message, str(kwargs.get("severity", "information"))))
                original_notify(message, **kwargs)

            with (
                patch(
                    "conductor.cli.bg_runner.launch_background_resume",
                    Mock(return_value=launch),
                ),
                patch.object(app, "notify", _capture),
            ):
                await _goto_history(pilot)
                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)

                await pilot.press("r")
                await settle(pilot)

            assert isinstance(app.screen, RunsScreen)

        messages = [message for message, _severity in notifications]
        assert not any(launch.url in m for m in messages), notifications
        assert any("completed" in m.lower() for m in messages), notifications

    async def test_resume_not_yet_started_shows_initializing_warning(
        self, fleet_env: Path, event_log_dir: Path, resume_workflow_file: Path
    ) -> None:
        """The other half of the CLI's three-way branch: ``workflow_started
        =False`` (process still alive) must warn about initialization,
        distinct from the ``run_record_written`` warning above."""
        log_path = _write_log(
            event_log_dir,
            name="resumable-workflow",
            run_id="00008888",
            lines=[_event("workflow_started", {"name": "resumable-workflow"}, ts=1000.0)],
        )
        _write_checkpoint(resume_workflow_file, event_log_path=str(log_path), run_id="00008888")

        launch = BackgroundLaunch(
            url="http://127.0.0.1:8080",
            stderr_log=event_log_dir / "00008888.bg.stderr.log",
            stdout_log=event_log_dir / "00008888.bg.stdout.log",
            run_id="00008888",
            workflow_started=False,
        )

        notifications: list[tuple[str, str]] = []

        app = FleetApp()
        async with app.run_test() as pilot:
            original_notify = app.notify

            def _capture(message, **kwargs):
                notifications.append((message, str(kwargs.get("severity", "information"))))
                original_notify(message, **kwargs)

            with (
                patch(
                    "conductor.cli.bg_runner.launch_background_resume",
                    Mock(return_value=launch),
                ),
                patch.object(app, "notify", _capture),
            ):
                await _goto_history(pilot)
                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)

                await pilot.press("r")
                await settle(pilot)

            assert isinstance(app.screen, RunsScreen)

        warnings = [message for message, severity in notifications if severity == "warning"]
        assert any("initializ" in m.lower() for m in warnings), notifications

    async def test_launch_error_surfaces_as_notification_without_navigating(
        self, fleet_env: Path, event_log_dir: Path, resume_workflow_file: Path
    ) -> None:
        log_path = _write_log(
            event_log_dir,
            name="resumable-workflow",
            run_id="00003333",
            lines=[_event("workflow_started", {"name": "resumable-workflow"}, ts=1000.0)],
        )
        _write_checkpoint(resume_workflow_file, event_log_path=str(log_path), run_id="00003333")

        app = FleetApp()
        async with app.run_test() as pilot:
            with patch(
                "conductor.cli.bg_runner.launch_background_resume",
                side_effect=RuntimeError("boom"),
            ):
                await _goto_history(pilot)
                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)

                with patch.object(HistoryScreen, "notify") as mock_notify:
                    await pilot.press("r")
                    await settle(pilot)

            assert app.is_running
            assert isinstance(app.screen, HistoryScreen)

        severities = [call.kwargs.get("severity") for call in mock_notify.call_args_list]
        assert "error" in severities

    async def test_correlate_checkpoints_runs_off_the_main_thread(
        self, fleet_env: Path, event_log_dir: Path, resume_workflow_file: Path
    ) -> None:
        log_path = _write_log(
            event_log_dir,
            name="resumable-workflow",
            run_id="00004444",
            lines=[_event("workflow_started", {"name": "resumable-workflow"}, ts=1000.0)],
        )
        _write_checkpoint(resume_workflow_file, event_log_path=str(log_path), run_id="00004444")

        seen_main_thread: list[bool] = []

        from conductor.fleet.resume import correlate_checkpoints

        def _tracking_correlate(entries):
            seen_main_thread.append(threading.current_thread() is threading.main_thread())
            return correlate_checkpoints(entries)

        with patch(
            "conductor.fleet.tui.screens.history.correlate_checkpoints",
            side_effect=_tracking_correlate,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_history(pilot)

        assert seen_main_thread == [False]

    async def test_correlate_checkpoints_failure_still_renders_rows_and_warns(
        self, fleet_env: Path, event_log_dir: Path
    ) -> None:
        _write_log(
            event_log_dir,
            name="resumable-workflow",
            run_id="00005555",
            lines=[_event("workflow_started", {"name": "resumable-workflow"}, ts=1000.0)],
        )

        with patch(
            "conductor.fleet.tui.screens.history.correlate_checkpoints",
            side_effect=OSError("checkpoints unreadable"),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_history(pilot)

                table = app.screen.query_one(DataTable)
                assert table.display is True
                assert table.row_count == 1
                assert app.screen.check_action("resume", ()) is False

    async def test_second_r_while_resuming_does_not_launch_twice(
        self, fleet_env: Path, event_log_dir: Path, resume_workflow_file: Path
    ) -> None:
        log_path = _write_log(
            event_log_dir,
            name="resumable-workflow",
            run_id="00006666",
            lines=[_event("workflow_started", {"name": "resumable-workflow"}, ts=1000.0)],
        )
        _write_checkpoint(resume_workflow_file, event_log_path=str(log_path), run_id="00006666")

        call_count = 0
        release_launch = threading.Event()

        def _blocking_launch_background_resume(**kwargs):
            nonlocal call_count
            call_count += 1
            release_launch.wait(timeout=2)
            return BackgroundLaunch(
                url="http://127.0.0.1:8080",
                stderr_log=event_log_dir / "00006666.bg.stderr.log",
                stdout_log=event_log_dir / "00006666.bg.stdout.log",
                run_id="00006666",
            )

        app = FleetApp()
        async with app.run_test() as pilot:
            with patch(
                "conductor.cli.bg_runner.launch_background_resume",
                _blocking_launch_background_resume,
            ):
                await _goto_history(pilot)
                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)

                await pilot.press("r")
                # Not `settle`: the launch worker is genuinely blocked on
                # `release_launch`, so this must observe the in-flight
                # state rather than wait it out (see AGENTS.md's test
                # caution about `settle` deadlocking on a suspended worker).
                await pilot.pause()

                assert app.screen._resuming is True

                await pilot.press("r")
                await pilot.pause()

                release_launch.set()
                await pilot.pause(0.3)

        assert call_count == 1
