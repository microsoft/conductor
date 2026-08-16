"""Pilot tests for the Fleet Manager TUI's shared actions (Fleet Manager E8).

Uses Textual's ``App.run_test()`` to drive a real (headless) instance of
:class:`~conductor.fleet.tui.app.FleetApp` against seeded run records,
covering E8-T4:

- ``w`` on a portless run is disabled with a visible reason.
- ``w`` on a ``--web``/``--web-bg`` run invokes the browser opener with the
  right URL.
- ``k`` signals exactly the selected PID and does not report success until
  the process is confirmed gone (E3-T10's verify-then-report contract).
- ``K`` prompts exactly once and signals every displayed run.
- Declining the confirmation modal signals nothing.
- A kill on an already-dead PID does not raise.
- The TUI and CLI resolve to the same shared implementation
  (``conductor.cli.app.stop_records``).
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from conductor.cli.app import Identity
from conductor.cli.app import stop_records as cli_stop_records
from conductor.fleet.records import RunRecord, read_run_record, write_run_record
from conductor.fleet.tui import actions as tui_actions
from conductor.fleet.tui.app import FleetApp
from conductor.fleet.tui.screens.runs import RunsScreen
from tests.test_fleet.conftest import settle

# ---------------------------------------------------------------------------
# Helpers (mirroring tests/test_cli/test_stop.py's established patterns)
# ---------------------------------------------------------------------------


def _alive_then_dead():
    """``is_process_alive`` side_effect: alive on first probe per PID, dead after.

    Simulates a process that is running during the Runs screen's discovery
    (the first probe ``read_run_records()`` performs for that PID) and has
    actually terminated by the time ``_stop_process`` polls again after
    signalling it (E3-T10) -- without waiting out the real grace period.
    """
    seen: dict[int, int] = {}

    def _is_alive(pid: int) -> bool:
        seen[pid] = seen.get(pid, 0) + 1
        return seen[pid] == 1

    return _is_alive


@contextlib.contextmanager
def _fast_grace_period():
    """Confirm the target dead on the first rung so kill tests stay fast.

    The escalation ladder lives in ``cli/pid.py`` (``terminate_process`` /
    ``wait_for_exit``) rather than as grace-period constants on ``cli.app``,
    so its bounded waits are neutralised by stubbing those outcomes -- the
    same seam ``tests/test_cli/test_stop.py::_stops_cleanly`` uses.
    """
    from conductor.cli.pid import Liveness

    with (
        patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
        patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.DEAD),
        patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
        # False, not True: a successful graceful cancel ends the ladder at the
        # dashboard rung, and these tests assert on the *signal* rung below it.
        patch("conductor.cli.app._request_graceful_kill", return_value=False),
    ):
        yield


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
    pid: int | None = None,
    workflow_name: str | None = None,
    started_at: str = "2026-01-01T00:00:00+00:00",
    port: int | None = 8080,
    mode: str = "bg",
) -> RunRecord:
    """Write a live run record with a real (empty) event log file backing it."""
    log_path = tmp_path / f"{run_id}.events.jsonl"
    if not log_path.exists():
        log_path.write_text("")

    record = RunRecord(
        run_id=run_id,
        pid=pid if pid is not None else os.getpid(),
        workflow_path=f"/tmp/{workflow_name or run_id}.yaml",
        workflow_name=workflow_name or run_id,
        started_at=started_at,
        event_log_path=str(log_path),
        port=port,
        mode=mode,
        checkpoint_dir=None,
    )
    write_run_record(record)
    return record


# ---------------------------------------------------------------------------
# Dashboard action (E8-T2)
# ---------------------------------------------------------------------------


class TestDashboardAction:
    async def test_portless_run_disables_dashboard_with_reason(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """A foreground record (no port) shows a visible reason instead of
        opening a browser or failing silently."""
        _write_record(tmp_path, "run-fg", port=None, mode="fg")

        with patch.object(tui_actions.webbrowser, "open") as mock_open:
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                screen = app.screen
                assert isinstance(screen, RunsScreen)
                with patch.object(RunsScreen, "notify") as mock_notify:
                    await pilot.press("w")
                    await settle(pilot)

        mock_open.assert_not_called()
        mock_notify.assert_called_once()
        message = mock_notify.call_args.args[0]
        assert "unavailable" in message.lower()
        assert "no dashboard" in message.lower()

    async def test_web_run_opens_browser_with_correct_url(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """`w` on a run with a dashboard port invokes the browser opener
        with the right http://127.0.0.1:<port> URL.

        Patches `_is_wsl` off so this asserts the ordinary `webbrowser`
        path regardless of where the suite runs -- on a WSL host the real
        detection routes around `webbrowser` entirely (see
        `TestDashboardOnWsl`), which would otherwise make this pass or fail
        by accident of the developer's machine.
        """
        _write_record(tmp_path, "run-web", port=9123, mode="fg-web")

        with (
            patch.object(tui_actions, "_is_wsl", return_value=False),
            patch.object(tui_actions.webbrowser, "open", return_value=True) as mock_open,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("w")
                await settle(pilot)

        mock_open.assert_called_once_with("http://127.0.0.1:9123")

    async def test_no_selection_does_not_crash(self, fleet_env: Path) -> None:
        """Pressing `w` with no runs at all (empty state) is a no-op, not a crash."""
        with (
            patch.object(tui_actions, "_is_wsl", return_value=False),
            patch.object(tui_actions.webbrowser, "open") as mock_open,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("w")
                await settle(pilot)

        mock_open.assert_not_called()


# ---------------------------------------------------------------------------
# Kill (single) action (E8-T3)
# ---------------------------------------------------------------------------


class TestKillAction:
    async def test_kill_signals_exactly_the_selected_pid(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        pid = os.getpid()
        _write_record(tmp_path, "run-a", pid=pid, mode="bg")

        with (
            _fast_grace_period(),
            patch("conductor.cli.pid.is_process_alive", side_effect=_alive_then_dead()),
            patch("conductor.cli.app.os.kill") as mock_kill,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("k")
                # Not `settle`: `action_kill` is a `@work` method that
                # suspends on `push_screen_wait` until the modal below is
                # answered, so waiting for all workers here would deadlock.
                await pilot.pause()
                # Confirm the kill via the modal (D1: TUI always confirms).
                await pilot.press("y")
                await settle(pilot)

        mock_kill.assert_called_once()
        called_pid = mock_kill.call_args.args[0]
        assert called_pid == pid

    async def test_kill_works_with_no_dashboard_running(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """Kill works purely by signal via the run record -- a foreground
        run with no dashboard/port at all is still killable (independent of
        any dashboard or API being reachable, per *Patterns adopted from
        prior art*)."""
        pid = os.getpid()
        _write_record(tmp_path, "run-fg", pid=pid, port=None, mode="fg")

        with (
            _fast_grace_period(),
            patch("conductor.cli.pid.is_process_alive", side_effect=_alive_then_dead()),
            patch("conductor.cli.app.os.kill") as mock_kill,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("k")
                # Not `settle`: the kill worker suspends on the confirmation
                # modal below, so waiting for all workers here would deadlock.
                await pilot.pause()
                await pilot.press("y")
                await settle(pilot)

        mock_kill.assert_called_once()
        assert mock_kill.call_args.args[0] == pid
        assert read_run_record("run-fg") is None

    async def test_kill_does_not_report_success_until_process_confirmed_gone(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """E3-T10: `_stop_process` (via `stop_records`) verifies termination
        by polling `is_process_alive` before the record is removed -- a
        process that never actually dies must not have its record removed."""
        pid = os.getpid()
        _write_record(tmp_path, "run-a", pid=pid, mode="bg")

        # Deliberately not `_fast_grace_period()`: that helper stubs
        # `wait_for_exit` to DEAD, which is the opposite of what this test
        # needs. Here every rung reports the process still ALIVE.
        from conductor.cli.pid import Liveness

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
            patch("conductor.cli.app._request_graceful_kill", return_value=False),
            patch("conductor.cli.app.os.kill"),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("k")
                # Not `settle`: the kill worker suspends on the confirmation
                # modal below, so waiting for all workers here would deadlock.
                await pilot.pause()
                await pilot.press("y")
                await settle(pilot)

        # The process never actually died, even after SIGKILL escalation --
        # the record must survive.
        assert read_run_record("run-a") is not None

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "Patches `os.kill` to raise ProcessLookupError, which only the POSIX "
            "kill path calls -- `terminate_process` dispatches to the ctypes "
            "`TerminateProcess` implementation on Windows, so the simulated race "
            "never occurs there."
        ),
    )
    async def test_kill_already_dead_pid_does_not_raise(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """A PID that's already gone by the time we signal it raises
        ProcessLookupError from os.kill -- this must be handled gracefully,
        not propagate up through the TUI action.

        ``is_process_alive`` is mocked to always report alive so the
        record is still displayed (and thus selectable/killable) at the
        point ``k`` is pressed -- isolating this test to the "process
        vanished between listing and signalling" race that
        ``_stop_process`` itself is documented to handle, rather than the
        record simply having been pruned by discovery before ever
        reaching the table.
        """
        _write_record(tmp_path, "run-a", pid=os.getpid(), mode="bg")

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill", side_effect=ProcessLookupError),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("k")
                # Not `settle`: the kill worker suspends on the confirmation
                # modal below, so waiting for all workers here would deadlock.
                await pilot.pause()
                await pilot.press("y")
                await settle(pilot)

        # Reaching here without an unhandled exception is the assertion;
        # additionally confirm the record was cleaned up (already-gone
        # processes are treated as successfully stopped).
        assert read_run_record("run-a") is None

    async def test_declining_confirmation_signals_nothing(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        pid = os.getpid()
        _write_record(tmp_path, "run-a", pid=pid, mode="bg")

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill") as mock_kill,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("k")
                # Not `settle`: the kill worker suspends on the confirmation
                # modal below, so waiting for all workers here would deadlock.
                await pilot.pause()
                # Cancel the confirmation modal.
                await pilot.press("n")
                await settle(pilot)

        mock_kill.assert_not_called()
        assert read_run_record("run-a") is not None


# ---------------------------------------------------------------------------
# Kill-all action (E8-T3)
# ---------------------------------------------------------------------------


class TestKillAllAction:
    async def test_kill_all_confirms_once_and_signals_all(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        pid_a = os.getpid()
        pid_b = os.getpid() + 1
        _write_record(tmp_path, "run-a", pid=pid_a, mode="bg")
        _write_record(tmp_path, "run-b", pid=pid_b, mode="bg")

        with (
            _fast_grace_period(),
            patch("conductor.cli.pid.is_process_alive", side_effect=_alive_then_dead()),
            patch("conductor.cli.app.os.kill") as mock_kill,
            patch.object(
                tui_actions, "ConfirmKillModal", wraps=tui_actions.ConfirmKillModal
            ) as modal_spy,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("K")
                # Not `settle`: `action_kill_all` is a `@work` method that
                # suspends on `push_screen_wait` until the modal below is
                # answered, so waiting for all workers here would deadlock.
                await pilot.pause()
                await pilot.press("y")
                await settle(pilot)

        # Exactly one modal for the whole kill-all, not one per run.
        modal_spy.assert_called_once()
        assert mock_kill.call_count == 2
        signalled_pids = {call.args[0] for call in mock_kill.call_args_list}
        assert signalled_pids == {pid_a, pid_b}

    async def test_kill_all_names_foreground_runs_in_confirmation(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """D1: the TUI confirms always, but must specifically call out any
        foreground runs in scope (their progress is lossy)."""
        _write_record(tmp_path, "run-fg", pid=os.getpid(), port=None, mode="fg")
        _write_record(tmp_path, "run-bg", pid=os.getpid() + 1, port=9999, mode="bg")

        captured_message = None
        original_init = tui_actions.ConfirmKillModal.__init__

        def _capture_init(self, message: str) -> None:
            nonlocal captured_message
            captured_message = message
            original_init(self, message)

        with (
            patch.object(tui_actions.ConfirmKillModal, "__init__", _capture_init),
            patch("conductor.cli.app.os.kill"),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("K")
                # Not `settle`: the kill-all worker suspends on the
                # confirmation modal below, so waiting for all workers here
                # would deadlock.
                await pilot.pause()
                # Decline -- we only care about the confirmation message here.
                await pilot.press("n")
                await settle(pilot)

        assert captured_message is not None
        assert "run-fg" in captured_message
        assert "progress" in captured_message.lower()

    async def test_kill_all_declines_signals_nothing(self, fleet_env: Path, tmp_path: Path) -> None:
        _write_record(tmp_path, "run-a", pid=os.getpid(), mode="bg")
        _write_record(tmp_path, "run-b", pid=os.getpid() + 1, mode="bg")

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill") as mock_kill,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("K")
                # Not `settle`: the kill-all worker suspends on the
                # confirmation modal below, so waiting for all workers here
                # would deadlock.
                await pilot.pause()
                await pilot.press("escape")
                await settle(pilot)

        mock_kill.assert_not_called()

    async def test_kill_all_no_runs_is_a_noop(self, fleet_env: Path) -> None:
        with patch("conductor.cli.app.os.kill") as mock_kill:
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("K")
                await settle(pilot)

        mock_kill.assert_not_called()


# ---------------------------------------------------------------------------
# Shared implementation (E8-T1)
# ---------------------------------------------------------------------------


class TestSharedImplementation:
    def test_tui_and_cli_resolve_to_the_same_stop_records(self) -> None:
        """The TUI's kill actions and `conductor stop` must funnel through
        the exact same function object -- not two independently-maintained
        copies of "signal, verify, remove record" (E8-T1)."""
        assert tui_actions.stop_records is cli_stop_records

    async def test_kill_runs_delegates_to_stop_records(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """kill_runs's actual stopping mechanics are `stop_records`, not a
        re-implementation -- verified by patching `stop_records` itself and
        confirming it (not some parallel kill path) is what gets called."""
        record = _write_record(tmp_path, "run-a", pid=os.getpid(), mode="bg")

        fake_outcome = Mock()
        fake_outcome.declined = False
        fake_outcome.stopped = [record]
        fake_outcome.failed = []

        with patch.object(
            tui_actions, "stop_records", return_value=fake_outcome
        ) as mock_stop_records:
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await pilot.press("k")
                # Not `settle`: the kill worker suspends on the confirmation
                # modal below, so waiting for all workers here would deadlock.
                await pilot.pause()
                await pilot.press("y")
                await settle(pilot)

        mock_stop_records.assert_called_once()
        called_targets = mock_stop_records.call_args.args[0]
        assert [r.run_id for r in called_targets] == ["run-a"]

    async def test_stop_records_runs_off_the_main_thread(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """``kill_runs`` awaits ``stop_records`` via ``asyncio.to_thread``
        (issue #437) rather than calling it inline on the event loop --
        ``stop_records``'s underlying signal-and-poll escalation ladder can
        take seconds."""
        pid = os.getpid()
        _write_record(tmp_path, "run-a", pid=pid, mode="bg")

        seen_main_thread: list[bool] = []

        with (
            _fast_grace_period(),
            patch("conductor.cli.pid.is_process_alive", side_effect=_alive_then_dead()),
            patch("conductor.cli.app.os.kill"),
        ):
            real_stop_records = tui_actions.stop_records

            def _tracking_stop_records(*args: object, **kwargs: object):
                seen_main_thread.append(threading.current_thread() is threading.main_thread())
                return real_stop_records(*args, **kwargs)

            with patch.object(tui_actions, "stop_records", side_effect=_tracking_stop_records):
                app = FleetApp()
                async with app.run_test() as pilot:
                    await settle(pilot)
                    await pilot.press("k")
                    # Not `settle`: the kill worker suspends on the
                    # confirmation modal below, so waiting for all workers
                    # here would deadlock.
                    await pilot.pause()
                    await pilot.press("y")
                    await settle(pilot)

        assert seen_main_thread == [False]


# ---------------------------------------------------------------------------
# Gate resolution (Fleet Manager E13, D4) -- review round 1 regression tests
# ---------------------------------------------------------------------------


def _gate_info(
    *,
    agent_name: str = "reviewer",
    prompt: str = "Approve?",
    options: list[str] | None = None,
    option_details: list[dict] | None = None,
):
    from conductor.fleet.summary import GateInfo

    return GateInfo(
        agent_name=agent_name,
        prompt=prompt,
        options=options if options is not None else ["yes", "no"],
        option_details=option_details if option_details is not None else [],
    )


class TestGateOptionsModalMarkupSafety:
    async def test_markup_like_prompt_and_labels_do_not_crash(self, fleet_env: Path) -> None:
        """A workflow-controlled gate prompt/option label containing Rich
        markup syntax (e.g. ``[/red]``) must render as literal text, never
        raise ``rich.errors.MarkupError`` (E13 review round 1)."""
        from conductor.fleet.tui.actions import GateOptionsModal

        gate = _gate_info(
            agent_name="[/red]evil agent[/bold]",
            prompt="[/red]evil prompt[/bold]",
            options=["yes"],
            option_details=[{"value": "yes", "label": "[/red]evil label[/bold]"}],
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            result_holder: dict[str, object] = {}

            async def _push_modal() -> None:
                result_holder["result"] = await app.push_screen_wait(GateOptionsModal(gate))

            app.run_worker(_push_modal())
            # Not `settle`: `_push_modal`'s worker suspends on
            # `push_screen_wait` until `escape` dismisses it below, so
            # waiting for all workers here would deadlock.
            await pilot.pause()

            from textual.widgets import Static

            prompt_widget = app.screen.query_one("#gate-prompt", Static)
            text = str(prompt_widget.render())
            assert "[/red]evil prompt[/bold]" in text

            await pilot.press("escape")
            await settle(pilot)
            assert result_holder["result"] is None


class TestGateOptionsModalDuplicateValues:
    async def test_duplicate_option_values_do_not_raise_duplicate_id(self, fleet_env: Path) -> None:
        """Gate option values are schema-valid but not guaranteed unique --
        opening the modal must use opaque, unique widget ids internally
        rather than the raw (possibly duplicate) values, and the dismissed
        result must still be the correct underlying value (E13 review
        round 1)."""
        from textual.widgets import OptionList

        from conductor.fleet.tui.actions import GateOptionsModal

        gate = _gate_info(options=["retry", "retry", "abort"])

        app = FleetApp()
        async with app.run_test() as pilot:
            result_holder: dict[str, object] = {}

            async def _push_modal() -> None:
                result_holder["result"] = await app.push_screen_wait(GateOptionsModal(gate))

            app.run_worker(_push_modal())
            # Not `settle`: `_push_modal`'s worker suspends on
            # `push_screen_wait` until `enter` selects an option below, so
            # waiting for all workers here would deadlock.
            await pilot.pause()

            option_list = app.screen.query_one(OptionList)
            assert option_list.option_count == 3

            # Select the third option (the unique "abort") and confirm the
            # dismissed value is the real gate option value, not the
            # internal opaque id.
            option_list.highlighted = 2
            await pilot.press("enter")
            await settle(pilot)

            assert result_holder["result"] == "abort"


class TestResolveGateSyncErrorHandling:
    def test_unexpected_exception_surfaces_as_failed_outcome(self) -> None:
        """`_resolve_gate_sync` must translate *any* unexpected exception
        from the HTTP path (not just `typer.Exit`) into a failed
        `GateResolveOutcome` -- a malformed 409/422 body or a raw
        connection error must not escape the worker thread (E13 review
        round 1)."""
        import conductor.cli.gate as gate_module
        from conductor.fleet.tui.actions import _resolve_gate_sync

        with patch.object(
            gate_module, "_gate_respond_impl", side_effect=ValueError("malformed response body")
        ):
            outcome = _resolve_gate_sync(8080, "yes", "reviewer")

        assert outcome.success is False
        assert outcome.message

    def test_console_is_restored_after_unexpected_exception(self) -> None:
        """The module-level console global must be restored even when an
        unexpected (non-typer.Exit) exception is raised."""
        import conductor.cli.gate as gate_module
        from conductor.fleet.tui.actions import _resolve_gate_sync

        original = gate_module.console
        with patch.object(gate_module, "_gate_respond_impl", side_effect=RuntimeError("boom")):
            _resolve_gate_sync(8080, "yes", "reviewer")

        assert gate_module.console is original

    def test_markup_like_agent_and_choice_report_success_not_failure(self) -> None:
        """A workflow-controlled agent/choice value containing Rich markup
        syntax (e.g. ``[/cyan]``) must not make ``_gate_respond_impl``'s
        success-message print raise ``MarkupError`` after an accepted
        (HTTP 200) response -- which would otherwise make
        ``_resolve_gate_sync`` report a successfully accepted response as
        failed (review round 2)."""
        import json as json_module
        from unittest.mock import MagicMock

        import httpx

        from conductor.fleet.tui.actions import _resolve_gate_sync

        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.text = json_module.dumps({"status": "accepted"})
        resp.json.return_value = {"status": "accepted"}

        with patch("httpx.post", return_value=resp):
            outcome = _resolve_gate_sync(8080, "[/cyan]evil-choice", "[/cyan]evil-agent")

        assert outcome.success is True
        assert "Gate resolved" in outcome.message


class TestResolveGateSyncConcurrency:
    def test_concurrent_calls_do_not_leave_a_stale_global_console(self) -> None:
        """Two `_resolve_gate_sync` calls racing the module-level
        `gate_module.console` swap must not interleave -- each call's
        capture/restore region is serialized so the global console is
        never left pointed at a discarded buffer (E13 review round 1).

        Because the fix serializes the whole swap/call/restore region,
        the two calls cannot genuinely execute inside it at the same
        time -- that serialization *is* the property under test. A short
        sleep inside the faked ``_gate_respond_impl`` maximizes the
        chance a naive (unlocked) implementation would have interleaved,
        without requiring the two threads to rendezvous mid-call.
        """
        import threading
        import time

        import conductor.cli.gate as gate_module
        from conductor.fleet.tui.actions import _resolve_gate_sync

        original = gate_module.console

        def _slow_gate_respond(port, choice, agent, input_text, token) -> None:
            time.sleep(0.05)
            gate_module.console.print(f"resolved {choice}")

        results: list = []
        results_lock = threading.Lock()

        def _call(choice: str) -> None:
            outcome = _resolve_gate_sync(8080, choice, "reviewer")
            with results_lock:
                results.append(outcome)

        with patch.object(gate_module, "_gate_respond_impl", side_effect=_slow_gate_respond):
            t1 = threading.Thread(target=_call, args=("yes",))
            t2 = threading.Thread(target=_call, args=("no",))
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        assert len(results) == 2
        # Each call's captured message reflects its own choice, never the
        # other thread's -- proof the swap/call/restore region did not
        # interleave.
        messages = {r.message for r in results}
        assert "resolved yes" in messages
        assert "resolved no" in messages
        assert gate_module.console is original


class TestDashboardOnWsl:
    """WSL has no working Linux browser handler, so `webbrowser` must not be
    the mechanism there: on a stock WSL2 image it falls through to `gio`,
    which answers "Operation not supported" and opens nothing."""

    def test_wsl_uses_wslview_when_available(self) -> None:
        with (
            patch.object(tui_actions, "_is_wsl", return_value=True),
            patch.object(tui_actions.shutil, "which", return_value="/usr/bin/wslview"),
            patch.object(tui_actions.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = Mock(returncode=0, stderr=b"")
            assert tui_actions.open_url("http://127.0.0.1:9123") is True

        argv = mock_run.call_args.args[0]
        assert argv[0] == "wslview"
        assert argv[-1] == "http://127.0.0.1:9123"

    def test_wsl_falls_back_to_powershell(self) -> None:
        """`wslview` ships in `wslu`, which is not installed by default."""
        with (
            patch.object(tui_actions, "_is_wsl", return_value=True),
            patch.object(tui_actions.shutil, "which", return_value=None),
            patch.object(tui_actions.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = Mock(returncode=0, stderr=b"")
            assert tui_actions.open_url("http://127.0.0.1:9123") is True

        argv = mock_run.call_args.args[0]
        assert argv[0] == "powershell.exe"
        assert argv[-1] == "http://127.0.0.1:9123"

    def test_wsl_never_uses_a_shell(self) -> None:
        """List-form argv only -- a URL must never be concatenated into a
        shell command line."""
        with (
            patch.object(tui_actions, "_is_wsl", return_value=True),
            patch.object(tui_actions.shutil, "which", return_value=None),
            patch.object(tui_actions.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = Mock(returncode=0, stderr=b"")
            tui_actions.open_url("http://127.0.0.1:9123")

        assert isinstance(mock_run.call_args.args[0], list)
        assert mock_run.call_args.kwargs.get("shell") is not True

    def test_failed_open_reports_false(self) -> None:
        """The caller shows the URL for hand-copying when this is False, so
        a non-zero exit must not be reported as success."""
        with (
            patch.object(tui_actions, "_is_wsl", return_value=True),
            patch.object(tui_actions.shutil, "which", return_value=None),
            patch.object(tui_actions.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = Mock(returncode=1, stderr=b"nope")
            assert tui_actions.open_url("http://127.0.0.1:9123") is False

    def test_opener_raising_does_not_propagate(self) -> None:
        with (
            patch.object(tui_actions, "_is_wsl", return_value=True),
            patch.object(tui_actions.shutil, "which", return_value=None),
            patch.object(tui_actions.subprocess, "run", side_effect=OSError("boom")),
        ):
            assert tui_actions.open_url("http://127.0.0.1:9123") is False

    def test_non_wsl_uses_webbrowser(self) -> None:
        with (
            patch.object(tui_actions, "_is_wsl", return_value=False),
            patch.object(tui_actions.webbrowser, "open", return_value=True) as mock_open,
        ):
            assert tui_actions.open_url("http://x") is True
        mock_open.assert_called_once_with("http://x")


class TestGateModalScrolling:
    """A real gate prompt (a plan-approval gate carries the whole plan) is
    routinely longer than the terminal is tall. The modal used to be
    `height: auto`, so it grew past the screen and took its own option list
    off the bottom with it."""

    def _long_gate(self):
        from conductor.fleet.summary import GateInfo

        return GateInfo(
            agent_name="plan_approval",
            prompt="\n".join(f"plan line {i}" for i in range(400)),
            options=["approve", "abort"],
            option_details=[],
        )

    async def test_options_stay_on_screen_with_a_long_prompt(self, fleet_env: Path) -> None:
        from textual.widgets import OptionList

        from conductor.fleet.tui.actions import GateOptionsModal

        app = FleetApp()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(GateOptionsModal(self._long_gate()))
            await settle(pilot)

            option_list = app.screen.query_one(OptionList)
            screen_height = app.screen.size.height
            assert option_list.region.y + option_list.region.height <= screen_height, (
                "the option list must stay within the screen -- an auto-sized "
                "modal pushed it off the bottom"
            )

    async def test_option_list_holds_focus_not_the_scroller(self, fleet_env: Path) -> None:
        """A scroll container is focusable by default and would otherwise take
        arrows and Enter away from the choices."""
        from textual.widgets import OptionList

        from conductor.fleet.tui.actions import GateOptionsModal

        app = FleetApp()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(GateOptionsModal(self._long_gate()))
            await settle(pilot)
            assert isinstance(app.screen.focused, OptionList)

    async def test_ctrl_down_scrolls_the_prompt(self, fleet_env: Path) -> None:
        from textual.containers import VerticalScroll

        from conductor.fleet.tui.actions import GateOptionsModal

        app = FleetApp()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(GateOptionsModal(self._long_gate()))
            await settle(pilot)

            scroller = app.screen.query_one("#gate-prompt-scroll", VerticalScroll)
            assert scroller.scroll_offset.y == 0
            await pilot.press("ctrl+down")
            await settle(pilot)
            assert scroller.scroll_offset.y > 0, "ctrl+down must scroll the prompt"

    async def test_page_binding_scrolls_further_than_a_line(self, fleet_env: Path) -> None:
        """Not pageup/pagedown: the focused OptionList binds those for option
        navigation, so a screen-level binding for them never fires."""
        from textual.containers import VerticalScroll

        from conductor.fleet.tui.actions import GateOptionsModal

        app = FleetApp()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(GateOptionsModal(self._long_gate()))
            await settle(pilot)

            scroller = app.screen.query_one("#gate-prompt-scroll", VerticalScroll)
            await pilot.press("ctrl+shift+down")
            await settle(pilot)
            assert scroller.scroll_offset.y > 1

    async def test_a_refused_kill_is_reported_not_counted_as_zero(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        """A kill the shared implementation refused must reach the user.

        `stop_records` writes its per-record diagnostics -- including the
        identity-mismatch refusal, which is a safety stop the user has to
        act on -- to the silent console the TUI hands it, and that buffer is
        discarded. So `StopOutcome.failed` is the only channel left. Before
        this, the screen announced "Killed 0 run(s)." at *informational*
        severity and the reason was unreachable.
        """
        record = _write_record(tmp_path, "run-a", pid=os.getpid(), mode="bg")

        fake_outcome = Mock()
        fake_outcome.declined = False
        fake_outcome.stopped = []
        fake_outcome.failed = [(record, "survived")]

        notifications: list[tuple[str, str]] = []

        with patch.object(tui_actions, "stop_records", return_value=fake_outcome):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                screen = app.screen
                original_notify = screen.notify

                def _capture(message: str, **kwargs: object) -> None:
                    notifications.append((message, str(kwargs.get("severity", "information"))))
                    original_notify(message, **kwargs)  # type: ignore[arg-type]

                with patch.object(screen, "notify", _capture):
                    await pilot.press("k")
                    # Not `settle`: the kill worker suspends on the
                    # confirmation modal below, so waiting for all workers
                    # here would deadlock.
                    await pilot.pause()
                    await pilot.press("y")
                    await settle(pilot)

        assert notifications, "the kill produced no notification at all"
        assert not any("Killed 0 run(s)" in message for message, _ in notifications)
        failures = [m for m, sev in notifications if sev == "error"]
        assert failures, f"no error notification; got {notifications}"
        assert "survived" in failures[0]
        assert "run-a" in failures[0]
