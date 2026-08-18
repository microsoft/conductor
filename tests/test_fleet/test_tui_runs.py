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
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Static

from conductor.cli import pid as cli_pid
from conductor.fleet.records import RunRecord, read_run_records, write_run_record
from conductor.fleet.summary import GateInfo, derive_run_summary
from conductor.fleet.tui.actions import GateOptionsModal
from conductor.fleet.tui.anim import FRAME_INTERVAL
from conductor.fleet.tui.app import FleetApp
from conductor.fleet.tui.screens.runs import RunScan, RunsScreen, _collect_runs
from tests.test_fleet.conftest import settle

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


async def _assert_guard_released(pilot: Any, screen: RunsScreen, *, timeout: float = 5.0) -> None:
    """Assert ``_refreshing`` *clears*, rather than sampling it once.

    The guard-recovery tests shrink ``POLL_INTERVAL_SECONDS`` so a later
    tick can be observed unattended, which means the flag legitimately
    flips back to ``True`` every interval -- including during ``settle()``'s
    trailing ``pilot.pause()``, where a tick can dispatch a fresh refresh
    between the last worker finishing and the assertion reading the flag.
    A single-instant ``assert screen._refreshing is False`` therefore fails
    at random (it did, on CI and ~20% of local runs) for a reason that has
    nothing to do with the ``finally`` it is meant to pin.

    What that ``finally`` actually guarantees is that the flag *clears*.
    Without it the flag latches ``True`` for the rest of the session and
    every subsequent tick is dropped, so this wait times out -- the
    regression is still caught, just not by a coin flip.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if screen._refreshing is False:
            return
        await pilot.pause()
        await asyncio.sleep(0.01)
    raise AssertionError(
        "_refreshing never cleared -- a transient failure wedged the screen, "
        "so no later refresh can run"
    )


_BLOCK_RULE = "▏"
"""The glyph Textual's ``vkey`` border paints -- the rule ``BlockFooter``
draws between the row-scoped and fleet-scoped key blocks (and the same one
Textual itself uses to fence off the docked command-palette key)."""


def _footer_line(app: FleetApp) -> str:
    """The compositor row carrying the footer bindings, or ``""``.

    Searches for the row rather than assuming the footer is the last strip.
    It is on Linux, but not reliably on Windows, where the trailing strip
    can be blank -- which made every footer assertion read an empty line and
    fail for a reason unrelated to the grouping these tests are about.
    """
    strips = list(app.screen._compositor.render_strips())
    for strip in reversed(strips):
        line = "".join(segment.text for segment in strip)
        if "New" in line:
            return line
    return ""


async def _rendered_footer(pilot: Any, app: FleetApp) -> str:
    """Return the footer as actually painted, for assertions about what a
    user can see.

    Reaches through the screen's compositor because that is the only place
    the border glyphs exist: ``Footer`` is a container, so rendering the
    widget alone yields nothing, and the marker class on a ``FooterKey``
    says only that styling was *requested*, not that it landed.

    Waits for the footer to actually paint instead of reading after a single
    ``pause()``. One pause is enough on Linux; on Windows the first frame can
    land before the footer is composited, so the assertion read a blank line.
    """
    for _ in range(20):
        line = _footer_line(app)
        if line:
            return line
        await settle(pilot)
    return _footer_line(app)


def _gate_info(prompt: str = "Approve?") -> GateInfo:
    """A minimal open gate, for pushing the options modal directly."""
    return GateInfo(agent_name="ask", prompt=prompt, options=["yes", "no"], option_details=[])


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
            await settle(pilot)
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
            await settle(pilot)
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
            await settle(pilot)
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
            await settle(pilot)
            table = app.screen.query_one(DataTable)

            row = table.get_row_at(0)
            assert "▲" in row[0]
            assert "gatewf" in row[0]

    async def test_running_badge_appears(self, fleet_env: Path, tmp_path: Path) -> None:
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            table = app.screen.query_one(DataTable)

            row = table.get_row_at(0)
            assert "●" in row[0]


class TestRunsScreenEmptyState:
    async def test_empty_state_renders_when_no_records(self, fleet_env: Path) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            empty_state = app.screen.query_one("#empty-state", Static)
            table = app.screen.query_one(DataTable)

            assert empty_state.display is True
            assert table.display is False
            assert table.row_count == 0

    async def test_empty_state_shows_launch_affordance(self, fleet_env: Path) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
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
            await settle(pilot)
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
            await settle(pilot)
            table = app.screen.query_one(DataTable)
            assert table.row_count == 0

            _write_record(tmp_path, "run-new", workflow_name="newwf")

            # Wait out several poll intervals (real time -- set_interval is
            # a real asyncio timer, not something pilot.pause() advances).
            await asyncio.sleep(0.3)
            await settle(pilot)

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
            await settle(pilot)
            table = app.screen.query_one(DataTable)
            assert table.row_count == 1

            remove_run_record("run-a")

            await asyncio.sleep(0.3)
            await settle(pilot)

            assert table.row_count == 0


class TestCollectRuns:
    """Unit tests for ``_collect_runs``, the collector half of the poll
    refresh (issue #437) -- pure enough to test without a running app."""

    def test_returns_pairs_sorted_newest_first(self, fleet_env: Path, tmp_path: Path) -> None:
        _write_record(
            tmp_path, "run-old", workflow_name="old", started_at="2020-01-01T00:00:00+00:00"
        )
        _write_record(
            tmp_path, "run-new", workflow_name="new", started_at="2026-01-01T00:00:00+00:00"
        )

        scan = _collect_runs()

        assert scan is not None
        assert [record.run_id for record, _ in scan.collected] == ["run-new", "run-old"]
        assert all(summary.run_id == record.run_id for record, summary in scan.collected)
        assert scan.seen_run_ids == {"run-new", "run-old"}
        assert scan.failed == 0

    def test_returns_none_when_read_run_records_raises(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        _write_record(tmp_path, "run-a")

        with patch(
            "conductor.fleet.tui.screens.runs.read_run_records",
            side_effect=OSError("transient failure"),
        ):
            assert _collect_runs() is None

    def test_skips_a_record_whose_summary_derivation_raises(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        _write_record(tmp_path, "run-good", workflow_name="good")
        _write_record(tmp_path, "run-bad", workflow_name="bad")

        def _flaky_derive(record: RunRecord):
            if record.run_id == "run-bad":
                raise RuntimeError("boom")
            return derive_run_summary(record)

        with patch(
            "conductor.fleet.tui.screens.runs.derive_run_summary", side_effect=_flaky_derive
        ):
            scan = _collect_runs()

        assert scan is not None
        assert [record.run_id for record, _ in scan.collected] == ["run-good"]
        assert scan.failed == 1

    def test_a_failed_derivation_still_reports_the_record_as_seen(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """The notifier is pruned against ``seen_run_ids``, so a record that
        failed to derive must still appear there -- otherwise it is forgotten
        and re-fires its gate/failure notification on the next successful
        tick (issue #446 review)."""
        _write_record(tmp_path, "run-good", workflow_name="good")
        _write_record(tmp_path, "run-bad", workflow_name="bad")

        def _flaky_derive(record: RunRecord):
            if record.run_id == "run-bad":
                raise RuntimeError("boom")
            return derive_run_summary(record)

        with patch(
            "conductor.fleet.tui.screens.runs.derive_run_summary", side_effect=_flaky_derive
        ):
            scan = _collect_runs()

        assert scan is not None
        assert scan.seen_run_ids == {"run-good", "run-bad"}


class TestRunsScreenWorkerThreading:
    """The poll refresh (and the dashboard-open action) run off the main
    thread (issue #437), so a large fleet's I/O never blocks the event
    loop -- verified by thread identity rather than timing, so nothing
    here is flaky."""

    async def test_collector_runs_off_the_main_thread(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        seen_main_thread: list[bool] = []

        def _tracking_read():
            seen_main_thread.append(threading.current_thread() is threading.main_thread())
            return read_run_records()

        with patch("conductor.fleet.tui.screens.runs.read_run_records", side_effect=_tracking_read):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)

        assert seen_main_thread, "the collector was never called"
        assert all(on_main is False for on_main in seen_main_thread)

    async def test_a_tick_is_skipped_while_a_refresh_is_in_flight(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An overrunning refresh causes the next poll tick to be dropped,
        not queued behind it (Q2) -- the collector is entered exactly once
        while blocked, however many ticks elapse in the meantime."""
        monkeypatch.setattr(RunsScreen, "POLL_INTERVAL_SECONDS", 0.05)
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        release = threading.Event()
        call_count = 0

        def _blocking_collect():
            nonlocal call_count
            call_count += 1
            release.wait(timeout=10)
            return []

        with patch("conductor.fleet.tui.screens.runs._collect_runs", side_effect=_blocking_collect):
            app = FleetApp()
            async with app.run_test() as pilot:
                # Several poll intervals elapse while the collector is
                # blocked -- none of them may start a second worker.
                await asyncio.sleep(0.3)
                assert call_count == 1

                # Release and let the app settle -- once unblocked, later
                # ticks are expected to run (and each completes almost
                # instantly), so nothing further is asserted about
                # `call_count` past this point.
                release.set()
                await settle(pilot)

    async def test_ui_stays_responsive_while_a_refresh_is_in_flight(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """A refresh blocked in its worker thread must not block the event
        loop -- the app can still process an unrelated keypress (pushing
        the History screen) while it is in flight."""
        from conductor.fleet.tui.screens.history import HistoryScreen

        _write_record(tmp_path, "run-a", workflow_name="alpha")

        # Starts released so the mount-time load returns immediately --
        # otherwise the opening `settle` sits through the full timeout,
        # which is ~9% of this suite's wall time and tests nothing.
        release = threading.Event()
        release.set()

        def _blocking_collect():
            release.wait(timeout=10)
            return RunScan(collected=[])

        with patch("conductor.fleet.tui.screens.runs._collect_runs", side_effect=_blocking_collect):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                screen = app.screen
                assert isinstance(screen, RunsScreen)

                release.clear()
                screen.refresh_runs()
                # The refresh worker is now blocked on `release` -- the
                # event loop must still be free to push a new screen.
                await asyncio.wait_for(pilot.press("h"), timeout=2.0)
                await pilot.pause()

                assert isinstance(app.screen, HistoryScreen)

                release.set()

    async def test_loading_indicator_hides_after_first_load(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """``#runs-loading`` is visible before the first collector result
        lands and hidden once the table (or empty state) takes over."""
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        release = threading.Event()

        def _blocking_collect():
            release.wait(timeout=10)
            return _collect_runs()

        with patch("conductor.fleet.tui.screens.runs._collect_runs", side_effect=_blocking_collect):
            app = FleetApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                # Assert the whole pre-load frame: a freshly-mounted Static
                # defaults to display=True, so checking only that would pass
                # against a screen that never set it (issue #446 review).
                loading = app.screen.query_one("#runs-loading", Static)
                assert loading.display is True
                assert "Loading" in str(loading.render())
                assert app.screen.query_one(DataTable).display is False
                assert app.screen.query_one("#empty-state", Static).display is False

                release.set()
                await settle(pilot)

                assert loading.display is False
                table = app.screen.query_one(DataTable)
                assert table.display is True

    async def test_dashboard_opens_off_the_main_thread(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        _write_record(tmp_path, "run-web", port=9123, mode="fg-web")

        seen_main_thread: list[bool] = []

        def _tracking_open(record: RunRecord) -> bool:
            seen_main_thread.append(threading.current_thread() is threading.main_thread())
            return True

        with patch("conductor.fleet.tui.screens.runs.open_dashboard", side_effect=_tracking_open):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("w")
                await settle(pilot)

        assert seen_main_thread == [False]

    async def test_second_w_press_while_opening_does_not_open_twice(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """A second ``w`` press while a dashboard open is already in flight
        must not open a second tab (mirroring the kill/gate re-entrancy
        guards)."""
        _write_record(tmp_path, "run-web", port=9123, mode="fg-web")

        release = threading.Event()
        call_count = 0

        def _blocking_open(record: RunRecord) -> bool:
            nonlocal call_count
            call_count += 1
            release.wait(timeout=10)
            return True

        with patch("conductor.fleet.tui.screens.runs.open_dashboard", side_effect=_blocking_open):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("w")
                # Not `settle`: the dashboard-open worker is genuinely
                # blocked on `release`, so waiting for all workers here
                # would deadlock.
                await pilot.pause()

                await pilot.press("w")
                await pilot.pause()

                release.set()
                await settle(pilot)

        assert call_count == 1

    async def test_the_open_guard_is_released_so_w_keeps_working(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """The guard is only safe because it is always released -- a stuck
        flag makes ``w`` a silent no-op for the rest of the session, and
        unlike the poll guard there is no timer to self-heal it (issue #446
        review)."""
        _write_record(tmp_path, "run-web", port=9123, mode="fg-web")

        with patch("conductor.fleet.tui.screens.runs.open_dashboard", return_value=True) as opener:
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("w")
                await settle(pilot)
                await pilot.press("w")
                await settle(pilot)

        assert opener.call_count == 2

    async def test_a_failing_open_does_not_exit_the_app(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """``open_dashboard`` catches broadly today, so this is a backstop:
        a ``@work`` method defaults to ``exit_on_error``, so anything
        escaping would take the TUI down over a failed browser launch."""
        _write_record(tmp_path, "run-web", port=9123, mode="fg-web")

        with patch(
            "conductor.fleet.tui.screens.runs.open_dashboard",
            side_effect=RuntimeError("no browser"),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("w")
                await settle(pilot)

                assert app.is_running
                screen = app.screen
                assert isinstance(screen, RunsScreen)
                assert screen._opening_dashboard is False


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
            await settle(pilot)
            table = app.screen.query_one(DataTable)

            assert table.row_count == 2


class TestRunsScreenQuitBinding:
    async def test_q_quits_the_app(self, fleet_env: Path) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("q")
            await settle(pilot)

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
            await settle(pilot)
            table = app.screen.query_one(DataTable)
            assert table.row_count == 2

            table.move_cursor(row=1)
            selected_row = table.get_row_at(table.cursor_row)

            await asyncio.sleep(0.3)
            await settle(pilot)

            # Compared on the workflow cell rather than the whole row: the
            # Burn sparkline legitimately changes between polls (that is
            # what it is for), so a whole-row comparison would assert the
            # run is *static* rather than that it is still *selected*.
            assert table.get_row_at(table.cursor_row)[0] == selected_row[0]


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
            await settle(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await settle(pilot)

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
            await settle(pilot)
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
                await settle(pilot)

            # The table/displayed state from before the failed scan is
            # left untouched -- not cleared to the empty state.
            assert table.row_count == 1
            assert screen._displayed_records

            # The notifier still remembers this run was already at-gate --
            # observing the same status again must not look like a fresh
            # transition.
            assert screen._notifier.observe("run-gate", "at-gate") is False

    async def test_a_failed_derivation_does_not_reset_that_run_s_notifier_history(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """The same once-per-transition contract, through the other door
        (issue #446 review). A *per-record* derivation failure used to drop
        that record from the prune set, so ``TransitionNotifier.prune``
        forgot it and the next successful tick re-fired its notification.

        Deliberately a **partial** failure: one run still derives, so the
        render reaches the normal row-building path and its prune call.
        A total failure takes the error branch and never prunes at all.
        """
        _write_record(tmp_path, "run-gate", workflow_name="gatewf")
        _write_record(tmp_path, "run-ok", workflow_name="okwf")

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            screen = app.screen
            assert isinstance(screen, RunsScreen)

            screen._notifier.observe("run-gate", "at-gate")
            assert screen._notifier.observe("run-gate", "at-gate") is False

            def _flaky_derive(record: RunRecord):
                if record.run_id == "run-gate":
                    raise RuntimeError("boom")
                return derive_run_summary(record)

            with patch(
                "conductor.fleet.tui.screens.runs.derive_run_summary",
                side_effect=_flaky_derive,
            ):
                screen.refresh_runs()
                await settle(pilot)

            # It was still *seen* this tick, so its history survives -- the
            # run has not silently become a brand-new transition.
            assert screen._notifier.observe("run-gate", "at-gate") is False

            # One tick where this run's summary cannot be derived at all.
            with patch(
                "conductor.fleet.tui.screens.runs.derive_run_summary",
                side_effect=RuntimeError("boom"),
            ):
                screen.refresh_runs()
                await settle(pilot)

            # It was still *seen*, so its history survives -- the run has
            # not silently become a brand-new transition.
            assert screen._notifier.observe("run-gate", "at-gate") is False

    async def test_a_total_derivation_failure_is_not_shown_as_an_empty_fleet(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """Records exist but none can be read: that is an error, not the
        "No runs -- press n to launch one" launch affordance. Showing the
        empty state to an operator whose runs are still burning tokens
        invites them to launch a duplicate (issue #446 review)."""
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            screen = app.screen
            assert isinstance(screen, RunsScreen)

            with patch(
                "conductor.fleet.tui.screens.runs.derive_run_summary",
                side_effect=RuntimeError("boom"),
            ):
                screen.refresh_runs()
                await settle(pilot)

            assert screen.query_one("#empty-state", Static).display is False
            loading = screen.query_one("#runs-loading", Static)
            assert loading.display is True
            assert "Could not read" in str(loading.render())
            # The row-scoped actions must keep working against the fleet
            # that is demonstrably still there.
            assert screen._displayed_records

    async def test_a_persistent_scan_failure_surfaces_instead_of_loading_forever(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """If the very first scan fails, only ``_render_runs`` would ever
        hide the loading line -- so the screen sat on a dim "Loading…" that
        never resolved, with no error and no timeout (issue #446 review)."""
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        with patch(
            "conductor.fleet.tui.screens.runs.read_run_records",
            side_effect=OSError("unreadable"),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                screen = app.screen
                assert isinstance(screen, RunsScreen)

                loading = screen.query_one("#runs-loading", Static)
                assert loading.display is True
                rendered = str(loading.render())
                assert "Could not read run records" in rendered
                assert "Loading" not in rendered


class TestRunsScreenGuardRecovery:
    """The ``_refreshing`` guard is only safe because it is always released.
    Without the ``finally``, one transient error stops the screen refreshing
    for the rest of the session -- silently, and indistinguishably from a
    calm fleet (issue #446 review)."""

    async def test_a_scan_failure_does_not_wedge_the_screen(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(RunsScreen, "POLL_INTERVAL_SECONDS", 0.05)
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            screen = app.screen
            assert isinstance(screen, RunsScreen)

            with patch(
                "conductor.fleet.tui.screens.runs.read_run_records",
                side_effect=OSError("transient failure"),
            ):
                screen.refresh_runs()
                await settle(pilot)

            await _assert_guard_released(pilot, screen)

            # And it genuinely recovers on a later tick.
            await asyncio.sleep(0.3)
            await settle(pilot)
            assert screen.query_one(DataTable).row_count == 1

    async def test_a_render_failure_does_not_wedge_the_screen_or_exit_the_app(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``@work`` defaults to ``exit_on_error``, so without the render
        guard the first exception in ``_render_runs`` takes the whole TUI
        down rather than logging a warning."""
        monkeypatch.setattr(RunsScreen, "POLL_INTERVAL_SECONDS", 0.05)
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            screen = app.screen
            assert isinstance(screen, RunsScreen)

            with patch.object(type(screen), "_render_runs", side_effect=RuntimeError("render bug")):
                screen.refresh_runs()
                await settle(pilot)
                await asyncio.sleep(0.2)
                await settle(pilot)

            assert app.is_running
            await _assert_guard_released(pilot, screen)

            await asyncio.sleep(0.2)
            await settle(pilot)
            assert screen.query_one(DataTable).row_count == 1

    async def test_two_dispatches_on_one_turn_start_only_one_worker(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pins the flag's placement: it is set in the synchronous
        dispatcher, not the worker body, because a ``@work`` method's body
        does not start until Textual schedules it."""
        monkeypatch.setattr(RunsScreen, "POLL_INTERVAL_SECONDS", 3600)
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            screen = app.screen
            assert isinstance(screen, RunsScreen)

            started = 0
            real_worker = type(screen)._refresh_worker

            def _counting(self_):
                nonlocal started
                started += 1
                return real_worker(self_)

            with patch.object(type(screen), "_refresh_worker", _counting):
                screen.refresh_runs()
                screen.refresh_runs()  # same event-loop turn, no await between
                assert started == 1
            await settle(pilot)

    async def test_an_explicit_refresh_is_coalesced_rather_than_dropped(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A poll tick may be dropped, but ``_kill_and_refresh`` and the
        gate-resolve ``finally`` promise the table reflects what they just
        did -- and the in-flight scan they collide with started *before* it
        (issue #446 review)."""
        monkeypatch.setattr(RunsScreen, "POLL_INTERVAL_SECONDS", 3600)
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            screen = app.screen
            assert isinstance(screen, RunsScreen)

            scans = 0
            real_collect = _collect_runs

            def _counting_collect():
                nonlocal scans
                scans += 1
                return real_collect()

            with patch(
                "conductor.fleet.tui.screens.runs._collect_runs", side_effect=_counting_collect
            ):
                screen._refreshing = True  # pretend a scan is already in flight
                screen.refresh_runs(explicit=True)
                assert scans == 0, "the explicit request must not start a second scan"
                assert screen._refresh_pending is True

                # When the in-flight scan finishes, the pending request runs.
                screen._refreshing = False
                screen._refresh_pending = False
                screen.refresh_runs(explicit=True)
                await settle(pilot)
                assert scans == 1

    async def test_a_dropped_poll_tick_is_not_remembered(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only *explicit* requests coalesce; a redundant poll tick is still
        dropped, which is the whole point of the guard."""
        monkeypatch.setattr(RunsScreen, "POLL_INTERVAL_SECONDS", 3600)
        _write_record(tmp_path, "run-a", workflow_name="alpha")

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            screen = app.screen
            assert isinstance(screen, RunsScreen)

            screen._refreshing = True
            screen.refresh_runs()
            assert screen._refresh_pending is False
            screen._refreshing = False


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
                await settle(pilot)
                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)
                await settle(pilot)

                await pilot.press("g")
                # Not `settle`: the fake resolve sleeps 0.2s to simulate an
                # in-flight resolution, and the point of this test is a
                # second `g` press *during* that window -- waiting for the
                # worker to complete here would let it finish first and
                # defeat the very race this test is checking.
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
            await settle(pilot)
            bar = str(app.screen.query_one("#summary-bar", Static).render())
            assert "2 running" in bar

    async def test_empty_fleet_says_so_rather_than_zero(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
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
            await settle(pilot)
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
            await settle(pilot)
            assert app.screen.query_one("#preview-pane").display is False

    async def test_pid_and_port_are_columns_not_preview_text(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """PID and port moved into the table, so the preview no longer
        restates them -- with one run the preview was near-identical to the
        row it sat under."""
        _write_record(tmp_path, "aaaa0004", workflow_name="alpha", port=8410, mode="bg")

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            row = " ".join(str(c) for c in app.screen.query_one(DataTable).get_row_at(0))
            assert "alpha" in row
            assert str(os.getpid()) in row
            assert "8410" in row

            text = str(app.screen.query_one("#run-preview", Static).render())
            assert str(os.getpid()) not in text
            assert "8410" not in text

    async def test_pane_is_shown_when_a_run_has_something_to_add(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """The pane still appears for a run whose log carries a topology --
        that (and an open gate) is all it now carries."""
        log = tmp_path / "topo2.events.jsonl"
        _write_events(
            log,
            [
                _event(
                    "workflow_started",
                    {"workflow_name": "wf", "agents": [{"name": "only", "type": "agent"}]},
                ),
            ],
        )
        _write_record(tmp_path, "aaaa0009", workflow_name="wf", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.screen.query_one("#preview-pane").display is True
            assert "only" in str(app.screen.query_one("#run-preview", Static).render())

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
            await settle(pilot)
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
            await settle(pilot)
            text = str(app.screen.query_one("#run-preview", Static).render())
            assert "Approve the plan?" in text
            assert "approve" in text

    async def test_gate_preview_offers_the_key_that_answers_it(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """The pane's job is "a decision is waiting and this is the key that
        takes it" -- not to be somewhere the whole prompt gets read."""
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
                        "options": ["approve"],
                    },
                ),
            ],
        )
        _write_record(tmp_path, "aaaa0016", workflow_name="wf", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            text = str(app.screen.query_one("#run-preview", Static).render())
            assert "to respond" in text

    async def test_a_long_gate_prompt_is_clipped_and_says_so(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """A gate prompt is routinely hundreds of lines of markdown. Clipped
        rather than scrollable -- a second focusable scroller under the table
        would compete with it for the arrow keys."""
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
                        "prompt": "\n".join(f"line {i}" for i in range(400)),
                        "options": ["approve"],
                    },
                ),
            ],
        )
        _write_record(tmp_path, "aaaa0017", workflow_name="wf", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test(size=(100, 26)) as pilot:
            await settle(pilot)
            text = str(app.screen.query_one("#run-preview", Static).render())
            assert "more lines" in text
            # The call to action survives the clipping -- it is the point.
            assert "to respond" in text
            assert len(text.splitlines()) < 40

    async def test_the_preview_pane_does_not_scroll(self, fleet_env: Path, tmp_path: Path) -> None:
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
                        "prompt": "\n".join(f"line {i}" for i in range(400)),
                        "options": ["approve"],
                    },
                ),
            ],
        )
        _write_record(tmp_path, "aaaa0018", workflow_name="wf", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test(size=(100, 26)) as pilot:
            await settle(pilot)
            pane = app.screen.query_one("#preview-pane")
            assert not pane.allow_vertical_scroll


class TestGateBindingVisibility:
    """`g` is hidden unless the selected run is actually at a gate: most rows
    never have one, and a permanently-visible key crowds a footer that
    truncates rather than wraps."""

    async def test_hidden_when_the_selected_run_has_no_gate(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        _write_record(tmp_path, "aaaa0010", workflow_name="plain")

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.screen.check_action("resolve_gate", ()) is False

    async def test_shown_when_the_selected_run_is_at_a_gate(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "gated.events.jsonl"
        _write_events(
            log,
            [
                _event("workflow_started", {"workflow_name": "wf"}),
                _event("agent_started", {"agent_name": "planner"}),
                _event("gate_presented", {"agent_name": "planner", "prompt": "Approve?"}),
            ],
        )
        _write_record(tmp_path, "aaaa0011", workflow_name="wf", event_log_path=str(log))

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.screen.check_action("resolve_gate", ()) is True

    async def test_hidden_with_no_runs_at_all(self, fleet_env: Path) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.screen.check_action("resolve_gate", ()) is False

    async def test_fleet_scoped_bindings_are_unaffected(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """Navigation and whole-fleet keys never depend on a selection --
        returning False for any of them would silently disable the rest of
        the screen."""
        _write_record(tmp_path, "aaaa0012", workflow_name="plain")

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            for action in ("quit", "kill_all", "open_history", "open_new_run"):
                assert app.screen.check_action(action, ()) is True

    async def test_row_scoped_bindings_need_a_selected_run(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """With a run on the table the row keys are offered; they are the
        keys that act on the highlighted run."""
        _write_record(tmp_path, "aaaa0013", workflow_name="plain")

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            for action in ("open_detail", "open_dashboard", "kill"):
                assert app.screen.check_action(action, ()) is True

    async def test_row_scoped_bindings_hide_on_an_empty_fleet(self, fleet_env: Path) -> None:
        """An empty table has no run to act on, so every row-scoped key
        retires -- the footer should not advertise `Kill` with nothing to
        kill, and the reclaimed width is what lets `Detail` fit."""
        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            for action in ("open_detail", "open_dashboard", "kill", "resolve_gate"):
                assert app.screen.check_action(action, ()) is False
            # Fleet-scoped keys survive an empty fleet.
            for action in ("quit", "open_history", "open_new_run"):
                assert app.screen.check_action(action, ()) is True

    async def test_kill_all_is_not_adjacent_to_kill(self) -> None:
        """`K` is fleet-scoped and must not sit next to the single-run `k`,
        which put "kill everything" one stray Shift away from "kill this
        one". Also pins the two-block ordering the footer relies on, since
        Textual renders BINDINGS in order and there is no separator widget.
        """
        keys = [b.key if isinstance(b, Binding) else b[0] for b in RunsScreen.BINDINGS]
        assert keys.index("K") > keys.index("k") + 1

        row_scoped = ["enter", "w", "k", "g"]
        fleet_scoped = ["n", "p", "r", "h", "K", "q"]
        assert keys == row_scoped + fleet_scoped

    async def test_a_rule_is_drawn_between_the_two_blocks(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """The block boundary must be *visible*, not merely ordered.

        Ordering the bindings put each block together but every key renders
        with identical styling and spacing, so the footer still read as one
        undifferentiated run of nine keys. Asserting on the rendered line
        rather than just the marker class is the point: the class was applied
        correctly while nothing was painted.
        """
        _write_record(tmp_path, "aaaa0017", workflow_name="plain")

        app = FleetApp()
        async with app.run_test(size=(140, 30)) as pilot:
            await settle(pilot)
            line = await _rendered_footer(pilot, app)

        assert "Kill  " in line and "n New" in line
        between = line[line.index("k Kill") : line.index("n New")]
        assert _BLOCK_RULE in between, f"no rule between the blocks: {line!r}"

    async def test_no_leading_rule_on_an_empty_fleet(self, fleet_env: Path) -> None:
        """With every row-scoped key hidden, `New` leads the footer -- a rule
        hanging off it with nothing to its left reads as a rendering fault
        rather than a grouping."""
        app = FleetApp()
        async with app.run_test(size=(140, 30)) as pilot:
            await settle(pilot)
            line = await _rendered_footer(pilot, app)

        assert line.lstrip().startswith("n New"), line
        assert _BLOCK_RULE not in line[: line.index("n New")], line

    async def test_footer_fits_without_truncation(self, fleet_env: Path, tmp_path: Path) -> None:
        """Every visible footer key must fit inside the footer.

        The footer is a single non-wrapping line: once the keys overrun it,
        Textual clips the tail rather than wrapping, which is how `h History`
        silently disappeared once already. Adding a binding (or lengthening a
        description) should fail here instead of dropping `q Quit` off the
        right-hand edge unnoticed.

        Checked at 100 columns -- comfortably under the width the 12-column
        runs table itself needs, so this is a floor on the footer, not an
        assertion about the terminal anyone actually uses.
        """
        _write_record(tmp_path, "aaaa0016", workflow_name="plain")

        app = FleetApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot)
            footer = app.screen.query_one(Footer)
            overflowing = [
                (child.region.x, child.region.right, getattr(child, "description", "?"))
                for child in footer.children
                if child.display and child.region.right > footer.region.width
            ]
            assert not overflowing, f"footer keys clipped at 100 cols: {overflowing}"

    async def test_detail_binding_survives_datatables_own_enter(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """`Detail` must actually reach the footer.

        `DataTable` binds `enter` itself with `show=False` and, as the
        focused widget, precedes the screen in the binding chain -- so
        without `priority` its hidden binding shadows ours and the
        drill-down silently vanishes from the footer while still working.
        """
        _write_record(tmp_path, "aaaa0015", workflow_name="plain")

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            shown = [
                ab.binding.description
                for ab in app.screen.active_bindings.values()
                if ab.binding.show and ab.enabled
            ]
            assert "Detail" in shown
            # And the two blocks stay contiguous in the rendered order.
            assert shown.index("Kill") < shown.index("New")
            assert shown.index("Kill all") > shown.index("History")

    async def test_enter_pushes_exactly_one_detail_screen(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """`enter` is bound twice over -- DataTable's own hidden binding and
        the screen's visible one -- so prove the two paths stay mutually
        exclusive and don't stack two identical screens."""
        _write_record(tmp_path, "aaaa0014", workflow_name="plain")

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            before = len(app.screen_stack)
            await pilot.press("enter")
            await settle(pilot)
            assert len(app.screen_stack) == before + 1


class TestChainedGateResolution:
    """A `questions` node asks one question per gate, so answering one opens
    the next. Dismissing to the table after each answer turned a four-question
    node into four separate `g` presses."""

    async def test_next_question_is_presented_automatically(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conductor.fleet.tui.actions import GateResolveOutcome

        log = tmp_path / "q.events.jsonl"
        _write_events(
            log,
            [
                _event("workflow_started", {"workflow_name": "wf"}),
                _event("agent_started", {"agent_name": "ask"}),
                _event("gate_presented", {"agent_name": "ask", "prompt": "Q1?", "options": ["a"]}),
            ],
        )
        _write_record(tmp_path, "aaaa0020", workflow_name="wf", event_log_path=str(log))

        presented: list[str] = []

        async def _fake_resolve(app, record, gate):
            presented.append(gate.prompt)
            return GateResolveOutcome(success=True, message="ok")

        # Second question, then the node finishes.
        follow_ups = [
            GateInfo(agent_name="ask", prompt="Q2?", options=["a"], option_details=[]),
            None,
        ]

        async def _fake_next(self, record, *, after, timeout=8.0):
            return follow_ups.pop(0) if follow_ups else None

        monkeypatch.setattr("conductor.fleet.tui.screens.runs.resolve_gate", _fake_resolve)
        monkeypatch.setattr(RunsScreen, "_await_next_gate", _fake_next)

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("g")
            for _ in range(40):
                await settle(pilot)
                if len(presented) >= 2:
                    break
                await asyncio.sleep(0.05)

        assert presented == ["Q1?", "Q2?"], (
            "answering the first question must re-present the next one "
            "instead of dropping back to the table"
        )

    async def test_cancelling_stops_the_chain(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Escape means "stop", not "ask me the next one"."""
        log = tmp_path / "q2.events.jsonl"
        _write_events(
            log,
            [
                _event("workflow_started", {"workflow_name": "wf"}),
                _event("agent_started", {"agent_name": "ask"}),
                _event("gate_presented", {"agent_name": "ask", "prompt": "Q1?", "options": ["a"]}),
            ],
        )
        _write_record(tmp_path, "aaaa0021", workflow_name="wf", event_log_path=str(log))

        calls: list[str] = []

        async def _fake_resolve(app, record, gate):
            calls.append(gate.prompt)
            return None  # cancelled

        async def _fail_next(self, record, *, after, timeout=8.0):
            raise AssertionError("must not wait for another gate after a cancel")

        monkeypatch.setattr("conductor.fleet.tui.screens.runs.resolve_gate", _fake_resolve)
        monkeypatch.setattr(RunsScreen, "_await_next_gate", _fail_next)

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("g")
            for _ in range(20):
                await settle(pilot)
                if calls:
                    break
                await asyncio.sleep(0.05)

        assert calls == ["Q1?"]


class TestRunsScreenPausesWhileNotOnTop:
    """The Runs screen must stop animating while another screen covers it.

    This is the gate freeze: a covered screen is still composited, so its
    ~10fps repaints keep re-blending whatever sits on top of it until the
    terminal cannot absorb the stream and keystrokes queue behind the
    redraw. The mechanism, the measurements, and why
    ``Screen.is_current`` cannot express the condition are recorded on
    :meth:`~conductor.fleet.tui.screens.runs.RunsScreen.on_screen_suspend`
    rather than repeated here.

    Three independent mechanisms, one test each: the guard in ``_tick``
    (which covers the window before the pause lands), the guard in
    ``_update_gate_detail`` (which covers the ~2s poll, deliberately left
    running), and the timer pause itself. Plus the two things that must
    keep working while covered: the poll's notifications, and the repaint
    on resume.
    """

    @staticmethod
    def _seed_gated_run(tmp_path: Path) -> None:
        """One live run sitting at an open gate -- what these tests start from."""
        log = _write_events(
            tmp_path / "gate.events.jsonl",
            [
                _event("workflow_started", {"workflow_name": "wf"}),
                _event("agent_started", {"agent_name": "ask"}),
                _event(
                    "gate_presented",
                    {"agent_name": "ask", "prompt": "Approve?", "options": ["yes", "no"]},
                ),
            ],
        )
        _write_record(tmp_path, "run-gate", workflow_name="wf", event_log_path=str(log))

    async def test_tick_is_a_noop_while_a_modal_is_on_top(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """``push_screen`` appends to the stack synchronously but *posts*
        ``ScreenSuspend``, and the timer invokes ``_tick`` from its own
        task rather than through the message pump -- so frames keep
        arriving until the pump drains. This guard, not the pause, is what
        stops them."""
        self._seed_gated_run(tmp_path)

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, RunsScreen)

            screen._tick()
            assert screen._frame == 1, "the animation clock advances while on top"

            app.push_screen(GateOptionsModal(_gate_info()))
            await pilot.pause()

            screen._tick()
            screen._tick()
            assert screen._frame == 1, "the animation clock must not advance under a modal"

            app.pop_screen()
            await pilot.pause()

            screen._tick()
            assert screen._frame == 2, "the animation clock resumes once the modal is dismissed"

    async def test_preview_is_not_repainted_while_a_modal_is_on_top(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """The ~2s data poll keeps running under a modal (see
        :meth:`test_the_poll_keeps_running_under_a_modal`), so the guard
        that stops it repainting has to live in ``_update_gate_detail``
        itself rather than in the poll."""
        self._seed_gated_run(tmp_path)

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, RunsScreen)
            panel = screen.query_one("#run-preview", Static)

            app.push_screen(GateOptionsModal(_gate_info()))
            await pilot.pause()

            with patch.object(panel, "update", wraps=panel.update) as painted:
                screen.refresh_runs()
                screen._update_gate_detail()
                # Drained inside the suspended window so any message the
                # rebuild queued (`table.clear()` emits `RowHighlighted`) is
                # dispatched here, while the guard still rejects it, rather
                # than landing after the pop and being mistaken for the
                # resume repaint.
                await pilot.pause()
                assert painted.call_count == 0

    async def test_resume_repaints_what_went_stale(self, fleet_env: Path, tmp_path: Path) -> None:
        """``on_screen_resume``'s repaint is what makes the guard above safe.

        Asserted against an **empty** fleet on purpose: with no rows there
        is no ``DataTable.RowHighlighted`` on resume, so ``on_screen_resume``
        is provably the only caller that can repaint. With a populated
        table the focus-regain message repaints too, and a call-count
        assertion passes even if this handler's repaint is deleted.
        """
        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, RunsScreen)
            panel = screen.query_one("#run-preview", Static)

            app.push_screen(GateOptionsModal(_gate_info()))
            await pilot.pause()

            with patch.object(panel, "update", wraps=panel.update) as painted:
                await pilot.pause()
                assert painted.call_count == 0, "nothing repaints while covered"

                app.pop_screen()
                await pilot.pause()
                assert painted.call_count >= 1, "resuming repaints whatever went stale"

    async def test_the_poll_keeps_running_under_a_modal(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the *render* is suppressed while covered, never the poll.

        The obvious next optimisation -- returning early from
        ``refresh_runs`` too -- would silently stop gate-entry and
        run-failure notifications at exactly the moment the operator is
        sitting inside a gate modal, with every other test still green.
        """
        notified: list[str] = []
        monkeypatch.setattr(
            "conductor.fleet.tui.screens.runs.emit_terminal_notification",
            lambda app, message: notified.append(message),
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, RunsScreen)

            app.push_screen(GateOptionsModal(_gate_info()))
            await pilot.pause()

            # The run reaches its gate *while* the modal is up.
            self._seed_gated_run(tmp_path)
            screen.refresh_runs()
            # The scan itself runs in a worker thread (issue #437), so the
            # notification lands after that hop rather than synchronously.
            # `settle` is safe here specifically because the modal was
            # pushed with `push_screen`, not `push_screen_wait` -- no worker
            # is suspended waiting on a keypress.
            await settle(pilot)

            assert notified == ["wf: waiting at gate (ask)"]

    async def test_the_animation_timer_is_paused_and_resumed(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The timer itself must stop, not merely its repaints.

        Asserted by counting *invocations* of ``_tick`` rather than the
        frames it produces. ``_tick``'s own guard already stops the frames,
        so a frame-counting assertion holds whether or not the timer was
        ever paused -- only an invocation count separates "paused" (never
        called) from "called, returned early", which is the difference
        between waking the event loop 10x a second for a covered screen
        and leaving it alone.
        """
        monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)
        self._seed_gated_run(tmp_path)

        ticks: list[int] = []
        original_tick = RunsScreen._tick

        def _counting_tick(screen: RunsScreen) -> None:
            ticks.append(1)
            original_tick(screen)

        monkeypatch.setattr(RunsScreen, "_tick", _counting_tick)

        app = FleetApp()
        async with app.run_test() as pilot:
            # Animation is on, so the splash is pushed over the Runs screen.
            await pilot.pause()
            # The timer is created by `on_mount` and must already be paused
            # under the splash -- the launch-time case of the same bug.
            runs = next(s for s in app.screen_stack if isinstance(s, RunsScreen))
            assert runs._anim_timer is not None
            ticks.clear()
            await asyncio.sleep(FRAME_INTERVAL * 4)
            assert ticks == [], "the timer must not fire while the splash covers the fleet"

            await pilot.press("x")
            for _ in range(40):
                await pilot.pause()
                if isinstance(app.screen, RunsScreen):
                    break
                await asyncio.sleep(0.05)
            screen = app.screen
            assert isinstance(screen, RunsScreen)

            ticks.clear()
            await asyncio.sleep(FRAME_INTERVAL * 4)
            await pilot.pause()
            assert ticks, "the timer fires while the Runs screen is on top"

            app.push_screen(GateOptionsModal(_gate_info()))
            await pilot.pause()
            ticks.clear()
            await asyncio.sleep(FRAME_INTERVAL * 6)
            await pilot.pause()
            assert ticks == [], "the timer must not fire at all under the gate modal"

            app.pop_screen()
            await pilot.pause()
            ticks.clear()
            await asyncio.sleep(FRAME_INTERVAL * 4)
            await pilot.pause()
            assert ticks, "the timer fires again once the gate is answered"


class TestAwaitNextGateThreading:
    """``_await_next_gate`` re-reads the log while a gate modal is up, so a
    synchronous derivation there is exactly the freeze issue #437 removes.
    Both chained-gate tests monkeypatch the method away entirely, so without
    this its thread hop is indistinguishable from unconverted code."""

    async def test_the_gate_re_read_runs_off_the_main_thread(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        _write_record(tmp_path, "aaaa0021", workflow_name="wf")

        seen_main_thread: list[bool] = []

        def _tracking_derive(record: RunRecord):
            seen_main_thread.append(threading.current_thread() is threading.main_thread())
            return derive_run_summary(record)

        app = FleetApp()
        async with app.run_test() as pilot:
            await settle(pilot)
            screen = app.screen
            assert isinstance(screen, RunsScreen)
            record = next(iter(screen._displayed_records.values()))

            with patch(
                "conductor.fleet.tui.screens.runs.derive_run_summary",
                side_effect=_tracking_derive,
            ):
                await screen._await_next_gate(
                    record,
                    after=GateInfo(agent_name="ask", prompt="Q1?", options=[], option_details=[]),
                    timeout=0.6,
                )

        assert seen_main_thread, "the gate re-read never ran"
        assert not any(seen_main_thread), "the gate re-read blocked the event loop"
