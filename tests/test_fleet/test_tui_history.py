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
  re-implemented).
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from textual.widgets import DataTable, Static

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
    caches its resolved directory per-process.
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
# `enter` footer advertisement (issue #459)
# ---------------------------------------------------------------------------


class TestReplayCommandFooter:
    """`enter` must both surface the replay command *and* actually appear
    in the footer -- `DataTable` binds `enter` itself (`show=False`), so
    without `priority=True` the screen's own binding is shadowed and the
    key silently vanishes from the footer while it still works (see
    `runs.py`'s identical `Detail` binding, which this mirrors)."""

    async def test_replay_cmd_binding_survives_datatables_own_enter(
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

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_history(pilot)

            shown = [
                ab.binding.description
                for ab in app.screen.active_bindings.values()
                if ab.binding.show and ab.enabled
            ]
            assert "Replay cmd" in shown

    async def test_enter_notifies_exactly_once(self, fleet_env: Path, event_log_dir: Path) -> None:
        """`enter` is bound twice over -- `DataTable`'s own hidden binding
        and the screen's visible one -- so prove the two paths stay
        mutually exclusive and don't double-notify."""
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

    async def test_mouse_click_still_notifies(self, fleet_env: Path, event_log_dir: Path) -> None:
        """A mouse click posts `RowSelected` directly (rather than going
        through the `priority` screen binding), so this exercises
        `on_data_table_row_selected` -- the other of the two paths that
        must both funnel into the same notification."""
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
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            with patch.object(HistoryScreen, "notify") as mock_notify:
                app.screen.post_message(DataTable.RowSelected(table, 0, row_key))
                await settle(pilot)

        mock_notify.assert_called_once()
        message = mock_notify.call_args.args[0]
        assert "conductor replay" in message
        assert str(log_path) in message
