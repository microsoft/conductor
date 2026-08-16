"""Tests for ``conductor.cli.bg_runner``.

Covers three issues that landed together:

- **#195 / Windows job breakaway**: ``_detachment_kwargs`` returns the right
  kwargs for POSIX vs Windows; ``_spawn_detached`` happy path requests
  breakaway on Windows, falls back to plain ``CREATE_NEW_PROCESS_GROUP`` and
  prints a stderr warning when the parent's Windows job forbids breakaway
  (``OSError`` with ``winerror == 5``); non-breakaway ``OSError`` propagates
  without retry; POSIX paths never retry on ``OSError``; both
  ``launch_background`` and ``launch_background_resume`` route their Popen
  call through ``_spawn_detached``.
- **#116 / bg diagnostics**: parent-side bookkeeping for the captured
  stderr/stdout log files — log-file creation, env-var wiring,
  error-message threading, handle cleanup, and the ``_sanitize_name`` helper
  used to build the log filename.
- **#410 / two-stage readiness contract**: ``_wait_for_server``'s early-exit
  detection via ``proc.poll()``, the ``StartProbe`` enum and
  ``_wait_for_workflow_start``'s polling loop, ``_resolve_start_timeout``'s
  env-var parsing, ``_tail_log``'s bounded tail, and
  ``_finalize_background_launch``'s reworked control flow — see the
  "Issue #410" section below.

Neither group of tests actually spawns a child process, with one
deliberate exception: ``TestWaitForWorkflowStart
.test_trampoline_child_pid_differs_from_proc_pid_is_not_foreign`` (issue
#444) builds a real two-level process tree, because the bug it guards
against — a spawned process's pid legitimately differing from the pid of
the process that ends up running the workflow, as happens under a
trampoline ``sys.executable`` on Windows — cannot be reproduced with a
mocked pid pair on this (non-trampoline) test host.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from conductor.cli import bg_runner


def _make_breakaway_denied_error() -> OSError:
    """Build an OSError shaped like the Windows ERROR_ACCESS_DENIED case.

    On non-Windows hosts, ``OSError(...)`` does not automatically populate
    ``.winerror``, so we set it explicitly to simulate what Popen would raise
    on Windows when ``CREATE_BREAKAWAY_FROM_JOB`` is denied by the parent
    job's ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` flag.
    """
    err = OSError(13, "Access is denied")
    err.winerror = 5  # type: ignore[attr-defined]
    return err


def _make_file_not_found_error() -> OSError:
    """Build an OSError with a non-breakaway Windows error code."""
    err = FileNotFoundError(2, "The system cannot find the file specified")
    err.winerror = 2  # type: ignore[attr-defined]
    return err


# ---------------------------------------------------------------------------
# _detachment_kwargs
# ---------------------------------------------------------------------------


class TestDetachmentKwargs:
    """Platform-specific Popen kwargs returned by ``_detachment_kwargs``."""

    def test_posix_returns_start_new_session(self) -> None:
        with patch.object(bg_runner.sys, "platform", "linux"):
            kwargs = bg_runner._detachment_kwargs()

        assert kwargs == {"start_new_session": True}

    def test_macos_returns_start_new_session(self) -> None:
        with patch.object(bg_runner.sys, "platform", "darwin"):
            kwargs = bg_runner._detachment_kwargs()

        assert kwargs == {"start_new_session": True}

    def test_windows_sets_breakaway_and_new_process_group(self) -> None:
        with patch.object(bg_runner.sys, "platform", "win32"):
            kwargs = bg_runner._detachment_kwargs()

        assert "start_new_session" not in kwargs
        assert "creationflags" in kwargs
        flags = kwargs["creationflags"]
        assert flags & bg_runner._CREATE_NEW_PROCESS_GROUP
        assert flags & bg_runner._CREATE_BREAKAWAY_FROM_JOB
        # Exactly the OR of the two — no stray bits.
        assert flags == (bg_runner._CREATE_NEW_PROCESS_GROUP | bg_runner._CREATE_BREAKAWAY_FROM_JOB)


# ---------------------------------------------------------------------------
# _is_breakaway_denied
# ---------------------------------------------------------------------------


class TestIsBreakawayDenied:
    """Narrow OSError classification for the breakaway-denied case."""

    def test_winerror_5_is_denied(self) -> None:
        assert bg_runner._is_breakaway_denied(_make_breakaway_denied_error()) is True

    def test_winerror_other_is_not_denied(self) -> None:
        assert bg_runner._is_breakaway_denied(_make_file_not_found_error()) is False

    def test_missing_winerror_is_not_denied(self) -> None:
        """Plain POSIX OSError (no ``winerror`` attribute) must not be misclassified."""
        assert bg_runner._is_breakaway_denied(OSError(13, "Permission denied")) is False


# ---------------------------------------------------------------------------
# _spawn_detached
# ---------------------------------------------------------------------------


def _make_windows_mocks(*, create_process_side_effect: Any = None) -> tuple[MagicMock, MagicMock]:
    """Build MagicMocks standing in for ``_winapi``/``_kernel32`` on Windows.

    Sufficient to drive ``_spawn_detached_windows``/``_terminate_child``'s
    Windows path without a real Windows host: constants, ``CreateProcess``
    returning a ``(hp, ht, pid, tid)`` tuple, and the job-object primitives
    all succeeding by default.
    """
    winapi = MagicMock()
    winapi.STARTF_USESTDHANDLES = 0x100
    winapi.DUPLICATE_SAME_ACCESS = 0x2
    winapi.GetCurrentProcess.return_value = -1
    _handle_counter = iter(range(1000, 2000))
    winapi.DuplicateHandle.side_effect = lambda *a, **k: next(_handle_counter)
    if create_process_side_effect is not None:
        winapi.CreateProcess.side_effect = create_process_side_effect
    else:
        winapi.CreateProcess.return_value = (5001, 5002, 4321, 1)

    kernel32 = MagicMock()
    kernel32.CreateJobObjectW.return_value = 6001
    kernel32.AssignProcessToJobObject.return_value = True
    kernel32.SetInformationJobObject.return_value = True
    kernel32.ResumeThread.return_value = 1
    kernel32.TerminateJobObject.return_value = True
    kernel32.CloseHandle.return_value = True
    return winapi, kernel32


@contextlib.contextmanager
def _patched_windows_platform(winapi: MagicMock, kernel32: MagicMock) -> Any:
    """Patch ``sys.platform``, ``_winapi``, ``_kernel32``, and ``msvcrt``.

    ``msvcrt`` is patched with a trivial ``get_osfhandle`` that returns the
    fd itself (any int is fine — ``DuplicateHandle`` is what produces the
    "real" inheritable handle in these tests).
    """
    msvcrt_mock = MagicMock()
    msvcrt_mock.get_osfhandle.side_effect = lambda fd: fd
    with (
        patch.object(bg_runner.sys, "platform", "win32"),
        patch.object(bg_runner, "_winapi", winapi),
        patch.object(bg_runner, "_kernel32", kernel32),
        patch.object(bg_runner, "msvcrt", msvcrt_mock),
    ):
        yield


class TestSpawnDetached:
    """Behavior of ``_spawn_detached`` across platforms and failure modes."""

    def test_posix_happy_path_uses_start_new_session(self) -> None:
        captured: dict[str, Any] = {}

        def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return MagicMock(pid=1234)

        with (
            patch.object(bg_runner.sys, "platform", "linux"),
            patch.object(bg_runner.subprocess, "Popen", side_effect=_fake_popen) as mock_popen,
        ):
            proc = bg_runner._spawn_detached(["python", "-c", "pass"], {"X": "1"})

        assert proc.pid == 1234
        mock_popen.assert_called_once()
        kwargs = captured["kwargs"]
        assert kwargs["start_new_session"] is True
        assert "creationflags" not in kwargs
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["env"] == {"X": "1"}
        assert proc.pid in bg_runner._SPAWNED_GROUP_LEADERS

    def test_windows_happy_path_includes_breakaway_and_suspended(self) -> None:
        winapi, kernel32 = _make_windows_mocks()

        with _patched_windows_platform(winapi, kernel32):
            proc = bg_runner._spawn_detached(["python", "-c", "pass"], {"X": "1"})

        assert proc.pid == 4321
        winapi.CreateProcess.assert_called_once()
        call = winapi.CreateProcess.call_args
        creationflags = call.args[5]
        assert creationflags & bg_runner._CREATE_NEW_PROCESS_GROUP
        assert creationflags & bg_runner._CREATE_BREAKAWAY_FROM_JOB
        assert creationflags & bg_runner._CREATE_SUSPENDED

    def test_windows_happy_path_assigns_job_and_resumes_in_order(self) -> None:
        winapi, kernel32 = _make_windows_mocks()
        order: list[str] = []
        kernel32.CreateJobObjectW.side_effect = lambda *a: (order.append("create_job"), 6001)[1]
        kernel32.AssignProcessToJobObject.side_effect = lambda *a: (
            order.append("assign"),
            True,
        )[1]
        kernel32.ResumeThread.side_effect = lambda *a: (order.append("resume"), 1)[1]

        with _patched_windows_platform(winapi, kernel32):
            proc = bg_runner._spawn_detached(["python", "-c", "pass"], {"X": "1"})

        assert order == ["create_job", "assign", "resume"]
        kernel32.AssignProcessToJobObject.assert_called_once_with(6001, 5001)
        assert proc.pid == 4321

    def test_windows_resume_thread_still_called_when_job_creation_fails(self) -> None:
        winapi, kernel32 = _make_windows_mocks()
        kernel32.CreateJobObjectW.return_value = 0  # failure sentinel

        with _patched_windows_platform(winapi, kernel32):
            bg_runner._spawn_detached(["python", "-c", "pass"], {"X": "1"})

        kernel32.ResumeThread.assert_called_once()
        kernel32.AssignProcessToJobObject.assert_not_called()

    def test_windows_resume_thread_still_called_when_assignment_fails(self) -> None:
        winapi, kernel32 = _make_windows_mocks()
        kernel32.AssignProcessToJobObject.return_value = False

        with _patched_windows_platform(winapi, kernel32):
            bg_runner._spawn_detached(["python", "-c", "pass"], {"X": "1"})

        kernel32.ResumeThread.assert_called_once()

    def test_windows_breakaway_denied_falls_back_and_warns(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When the parent's job forbids breakaway, retry without the flag.

        - First CreateProcess call requests breakaway and raises
          OSError(winerror=5).
        - Second call must NOT include CREATE_BREAKAWAY_FROM_JOB (but must
          keep CREATE_SUSPENDED, since the job-assignment sequence still
          applies on the fallback path).
        - A user-visible warning must be written to stderr.
        """
        calls: list[Any] = []

        def _create_process(*args: Any) -> tuple[int, int, int, int]:
            calls.append(args)
            if len(calls) == 1:
                raise _make_breakaway_denied_error()
            return (5001, 5002, 4321, 1)

        winapi, kernel32 = _make_windows_mocks(create_process_side_effect=_create_process)

        with _patched_windows_platform(winapi, kernel32):
            proc = bg_runner._spawn_detached(["python", "-c", "pass"], {"X": "1"})

        assert proc.pid == 4321
        assert len(calls) == 2
        first_flags = calls[0][5]
        second_flags = calls[1][5]
        assert first_flags & bg_runner._CREATE_BREAKAWAY_FROM_JOB
        assert not (second_flags & bg_runner._CREATE_BREAKAWAY_FROM_JOB)
        assert second_flags & bg_runner._CREATE_NEW_PROCESS_GROUP
        assert second_flags & bg_runner._CREATE_SUSPENDED

        captured = capsys.readouterr()
        assert "warning" in captured.err.lower()
        assert "breakaway" in captured.err.lower()
        # Must not pollute stdout (caller prints "Dashboard: ..." there).
        assert captured.out == ""

    def test_windows_non_breakaway_oserror_propagates(self) -> None:
        """OSErrors other than ERROR_ACCESS_DENIED must propagate without retry."""
        not_found = _make_file_not_found_error()
        winapi, kernel32 = _make_windows_mocks(create_process_side_effect=not_found)

        with (
            _patched_windows_platform(winapi, kernel32),
            pytest.raises(FileNotFoundError),
        ):
            bg_runner._spawn_detached(["nonexistent.exe"], {})

        # Exactly one attempt — no fallback retry.
        winapi.CreateProcess.assert_called_once()

    def test_posix_oserror_propagates_without_retry(self) -> None:
        """POSIX never has a breakaway concept; OSErrors must propagate."""
        err = OSError(13, "Permission denied")
        with (
            patch.object(bg_runner.sys, "platform", "linux"),
            patch.object(bg_runner.subprocess, "Popen", side_effect=err) as mock_popen,
            pytest.raises(OSError, match="Permission denied"),
        ):
            bg_runner._spawn_detached(["python", "-c", "pass"], {})

        mock_popen.assert_called_once()


class TestSpawnDetachedWindowsTerminateTree:
    """``_WindowsDetachedProcess.terminate_tree`` calls ``TerminateJobObject``."""

    def test_terminate_tree_calls_terminate_job_object(self) -> None:
        winapi, kernel32 = _make_windows_mocks()

        with _patched_windows_platform(winapi, kernel32):
            proc = bg_runner._spawn_detached(["python", "-c", "pass"], {"X": "1"})
            proc.terminate_tree()

        kernel32.TerminateJobObject.assert_called_once_with(6001, 1)

    def test_terminate_tree_is_noop_when_job_was_never_created(self) -> None:
        winapi, kernel32 = _make_windows_mocks()
        kernel32.CreateJobObjectW.return_value = 0

        with _patched_windows_platform(winapi, kernel32):
            proc = bg_runner._spawn_detached(["python", "-c", "pass"], {"X": "1"})
            proc.terminate_tree()

        kernel32.TerminateJobObject.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: launch_background / launch_background_resume route through
# _spawn_detached so the breakaway fix applies in both run and resume paths.
# ---------------------------------------------------------------------------


class TestLaunchBackgroundRoutesThroughSpawnDetached:
    """End-to-end: ensure both launch helpers actually call ``_spawn_detached``."""

    def test_launch_background_calls_spawn_detached(self, tmp_path: Path) -> None:
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc) as mock_spawn,
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=1, mode="bg", port=9301),
            ),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            launch = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={"q": "hello"},
                web_port=9301,
            )

        assert launch.url == "http://127.0.0.1:9301"
        mock_spawn.assert_called_once()
        # _spawn_detached is called positionally: (cmd, env).
        cmd = mock_spawn.call_args.args[0]
        env = mock_spawn.call_args.args[1]
        assert "--web" in cmd
        assert "--web-port" in cmd
        assert "9301" in cmd
        assert env["CONDUCTOR_WEB_BG"] == "1"
        assert env["CONDUCTOR_WEB_PORT"] == "9301"

    def test_launch_background_resume_calls_spawn_detached(self, tmp_path: Path) -> None:
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=2)
        fake_proc.poll.return_value = None

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc) as mock_spawn,
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=2, mode="bg", port=9302),
            ),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            launch = bg_runner.launch_background_resume(
                workflow_path=wf_path,
                checkpoint_path=None,
                web_port=9302,
            )

        assert launch.url == "http://127.0.0.1:9302"
        mock_spawn.assert_called_once()
        cmd = mock_spawn.call_args.args[0]
        env = mock_spawn.call_args.args[1]
        assert "resume" in cmd
        assert "--web" in cmd
        assert "9302" in cmd
        assert env["CONDUCTOR_WEB_BG"] == "1"
        assert env["CONDUCTOR_WEB_PORT"] == "9302"

    def test_launch_background_resume_forwards_guidance(self, tmp_path: Path) -> None:
        """--guidance texts are forwarded as repeated --guidance argv entries."""
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=3)
        fake_proc.poll.return_value = None

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc) as mock_spawn,
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            # The launch gate only accepts a record matching the child's
            # pid/mode/port, so the stub must agree with this launch.
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=3, mode="bg", port=9303),
            ),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background_resume(
                workflow_path=wf_path,
                checkpoint_path=None,
                web_port=9303,
                guidance=["Skip the benchmark step", "Prefer Python 3.12"],
            )

        cmd = mock_spawn.call_args.args[0]
        guidance_indices = [i for i, arg in enumerate(cmd) if arg == "--guidance"]
        assert len(guidance_indices) == 2
        assert cmd[guidance_indices[0] + 1] == "Skip the benchmark step"
        assert cmd[guidance_indices[1] + 1] == "Prefer Python 3.12"

    def test_launch_background_wraps_spawn_failure_in_runtimeerror(self, tmp_path: Path) -> None:
        """Spawn failures are wrapped so the CLI surfaces a clean error."""
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        with (
            patch.object(
                bg_runner,
                "_spawn_detached",
                side_effect=OSError("simulated spawn failure"),
            ),
            pytest.raises(RuntimeError, match="Failed to start background process"),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={"q": "hello"},
                web_port=9303,
            )

    def test_launch_background_resume_wraps_spawn_failure_in_runtimeerror(
        self, tmp_path: Path
    ) -> None:
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        with (
            patch.object(
                bg_runner,
                "_spawn_detached",
                side_effect=OSError("simulated spawn failure"),
            ),
            pytest.raises(RuntimeError, match="Failed to start background process"),
        ):
            bg_runner.launch_background_resume(
                workflow_path=wf_path,
                checkpoint_path=None,
                web_port=9304,
            )


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestCreationFlagConstants:
    """Module constants must be importable on any platform.

    The real ``subprocess.CREATE_BREAKAWAY_FROM_JOB`` constant only exists on
    Windows; defaulting via ``getattr`` keeps the module importable on POSIX
    (where ``bg_runner`` is still imported by tests and by the launch flow's
    code path that just returns ``start_new_session=True``).
    """

    def test_constants_are_ints(self) -> None:
        assert isinstance(bg_runner._CREATE_NEW_PROCESS_GROUP, int)
        assert isinstance(bg_runner._CREATE_BREAKAWAY_FROM_JOB, int)

    def test_constants_match_subprocess_on_windows(self) -> None:
        if sys.platform != "win32":
            pytest.skip("Windows-only constants check")

        assert bg_runner._CREATE_NEW_PROCESS_GROUP == subprocess.CREATE_NEW_PROCESS_GROUP
        assert bg_runner._CREATE_BREAKAWAY_FROM_JOB == subprocess.CREATE_BREAKAWAY_FROM_JOB


# ---------------------------------------------------------------------------
# _terminate_child (issue #447): terminating the process *tree*, not just
# proc.pid, plus honestly reporting when termination could not be confirmed.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-tree kill only")
class TestTerminateChild:
    """``_terminate_child``'s POSIX process-tree kill and confirmation sweep."""

    def test_kills_real_process_tree_spawned_via_spawn_detached(self) -> None:
        """Regression test for issue #447: a real two-level process tree,
        spawned through the actual ``_spawn_detached`` production seam (so
        ``_SPAWNED_GROUP_LEADERS`` is populated the same way a real launch
        would populate it), must have BOTH levels dead afterwards -- the
        previous behavior (``proc.terminate()`` on the outer handle alone)
        would leave the inner descendant running.
        """
        script = (
            "import subprocess, sys, time; "
            "p = subprocess.Popen([sys.executable, '-c', "
            "'import os,sys,time; print(os.getpid(), flush=True); time.sleep(30)'], "
            "stdout=subprocess.PIPE, text=True); "
            "line = p.stdout.readline(); "
            "sys.stdout.write(line); sys.stdout.flush(); "
            "time.sleep(30)"
        )
        proc = bg_runner._spawn_detached(
            [sys.executable, "-c", script], dict(os.environ), stdout=subprocess.PIPE
        )
        assert isinstance(proc, subprocess.Popen)
        inner_pid: int | None = None
        try:
            assert proc.stdout is not None
            sel = selectors.DefaultSelector()
            sel.register(proc.stdout, selectors.EVENT_READ)
            try:
                if not sel.select(timeout=10.0):
                    pytest.fail("nested interpreter did not report its pid within 10s")
            finally:
                sel.close()
            line = proc.stdout.readline()
            if not line:
                pytest.fail("nested interpreter closed its stdout without reporting a pid")
            inner_pid = int(line.strip())

            outcome = bg_runner._terminate_child(proc)

            assert outcome.confirmed is True
            assert outcome.surviving_pids == ()
            # The real regression check: the tree kill (os.killpg) must have
            # reached the *inner* descendant too, not just the outer proc.
            deadline = time.monotonic() + 5.0
            inner_alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(inner_pid, 0)
                except ProcessLookupError:
                    inner_alive = False
                    break
                time.sleep(0.1)
            assert not inner_alive, "inner descendant survived _terminate_child"
        finally:
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()
                    proc.wait(timeout=5.0)
            if inner_pid is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.kill(inner_pid, signal.SIGKILL)

    def test_confirmed_child_pid_is_killed_when_not_a_descendant(self) -> None:
        """A ``confirmed_child_pid`` that is a genuinely separate process
        (the trampoline case, where the outer ``proc`` is only a shim) must
        be terminated too, even though it is not in the outer process's own
        tree."""
        proc = MagicMock()
        proc.pid = 999999999  # not a real pid; poll() reports it as dead
        proc.poll.return_value = 0

        other = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        # Reap ``other`` as soon as it dies so a SIGKILL delivered inside
        # ``_terminate_child``'s sweep doesn't leave a zombie that a plain
        # ``os.kill(pid, 0)`` liveness probe would still see as "alive"
        # (we are ``other``'s direct parent here, unlike the real
        # trampoline case this is standing in for).
        reaper = threading.Thread(target=other.wait, daemon=True)
        reaper.start()
        try:
            outcome = bg_runner._terminate_child(proc, confirmed_child_pid=other.pid)

            assert outcome.confirmed is True
            assert other.pid not in outcome.surviving_pids
            deadline = time.monotonic() + 5.0
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(other.pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.1)
            assert not alive
        finally:
            if other.poll() is None:
                with contextlib.suppress(Exception):
                    other.kill()
                    other.wait(timeout=5.0)

    def test_reports_unconfirmed_when_liveness_check_keeps_reporting_alive(self) -> None:
        """When the final sweep cannot confirm death, the outcome must name
        the surviving pid rather than silently claiming success."""
        proc = MagicMock()
        proc.pid = 4242
        proc.poll.return_value = None
        proc.terminate.side_effect = Exception("boom")

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.pid.terminate_process") as mock_terminate_process,
        ):
            outcome = bg_runner._terminate_child(proc)

        assert outcome.confirmed is False
        assert outcome.surviving_pids == (4242,)
        mock_terminate_process.assert_called_once_with(4242, timeout=2.0)

    def test_does_not_killpg_a_pid_this_module_never_spawned(self) -> None:
        """The ``os.killpg`` footgun guard: a pid this module did not itself
        register as a spawned group leader must never be passed to
        ``os.killpg``, even if it happens to still look "alive" (e.g. a
        stale ``MagicMock(pid=1)`` from an unrelated test)."""
        proc = MagicMock()
        proc.pid = 1  # deliberately NOT in _SPAWNED_GROUP_LEADERS
        proc.poll.return_value = None

        assert 1 not in bg_runner._SPAWNED_GROUP_LEADERS

        with (
            patch("os.killpg") as mock_killpg,
            patch("conductor.cli.pid.is_process_alive", return_value=False),
        ):
            bg_runner._terminate_child(proc)

        mock_killpg.assert_not_called()


class TestTerminationNote:
    """``_termination_note`` renders the confirmed/unconfirmed message halves."""

    def test_confirmed_renders_terminated_message(self) -> None:
        outcome = bg_runner._TerminationOutcome(confirmed=True, surviving_pids=())
        note = bg_runner._termination_note(outcome, web_port=9000)
        assert "terminated" in note.lower()

    def test_unconfirmed_names_surviving_pids_and_stop_command(self) -> None:
        outcome = bg_runner._TerminationOutcome(confirmed=False, surviving_pids=(111, 222))
        note = bg_runner._termination_note(outcome, web_port=9001)
        assert "111" in note
        assert "222" in note
        assert "conductor stop --port 9001" in note
        assert "could not confirm termination" in note.lower()


class TestCleanupRecordAfterTermination:
    """``_cleanup_record_after_termination`` only removes a confirmed-dead pid's record."""

    def test_removes_record_when_candidate_pid_is_dead(self) -> None:
        outcome = bg_runner._TerminationOutcome(confirmed=True, surviving_pids=())
        with patch.object(bg_runner, "_remove_dead_child_record") as mock_remove:
            bg_runner._cleanup_record_after_termination("deadbeef", outcome, 111)
        mock_remove.assert_called_once_with("deadbeef", 111)

    def test_preserves_record_when_candidate_pid_survived(self) -> None:
        outcome = bg_runner._TerminationOutcome(confirmed=False, surviving_pids=(111,))
        with patch.object(bg_runner, "_remove_dead_child_record") as mock_remove:
            bg_runner._cleanup_record_after_termination("deadbeef", outcome, 111)
        mock_remove.assert_not_called()

    def test_noop_when_candidate_pid_is_none(self) -> None:
        outcome = bg_runner._TerminationOutcome(confirmed=True, surviving_pids=())
        with patch.object(bg_runner, "_remove_dead_child_record") as mock_remove:
            bg_runner._cleanup_record_after_termination("deadbeef", outcome, None)
        mock_remove.assert_not_called()


# ---------------------------------------------------------------------------
# D2 parent-side gate: poll the child's run record instead of writing a PID
# file (Fleet Manager E2-T4/T7). The child owns the write in every mode; the
# parent only reads ``read_run_record(run_id)`` to confirm the child actually
# reported in before declaring the launch a success.
# ---------------------------------------------------------------------------


class TestRunRecordPollGate:
    """``_finalize_background_launch`` polls the child's run record (D2)."""

    def test_run_id_passed_to_read_run_record_matches_launch_run_id(self, tmp_path: Path) -> None:
        """The parent looks up the same ``run_id`` it hands to the child.

        ``read_run_record`` is keyed by ``run_id``; if the parent polled
        under a different key than the one it wired into ``CONDUCTOR_RUN_ID``
        (and hence the one the child's record is filed under), the gate
        would never see the child's record even on a successful launch.
        """
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=1, mode="bg", port=9310),
            ) as mock_read,
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            launch = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9310,
            )

        mock_read.assert_called_once_with(launch.run_id)

    def test_parent_never_writes_a_run_record_itself(self, tmp_path: Path) -> None:
        """Per D2, the child is the sole writer -- the parent only reads.

        Exactly one record must exist per bg run; if the parent also wrote
        one, there would be two.
        """
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=1, mode="bg", port=9311),
            ),
            patch("conductor.fleet.records.write_run_record") as mock_write,
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9311,
            )

        mock_write.assert_not_called()

    def test_run_record_never_appears_but_child_stays_reachable_downgrades_to_warning(
        self, tmp_path: Path
    ) -> None:
        """Issue #435: a bookkeeping failure must not kill a healthy workflow.

        When the run-record poll's own deadline passes but the child is
        still alive and its dashboard is still reachable (a fresh 1s
        reachability re-probe succeeds), the launch must **not** be
        terminated or raise -- the workflow itself is fine, only the
        discovery record write failed. The launch succeeds with
        ``run_record_written=False`` so callers can warn without treating
        this as a launch failure.
        """
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None  # still running throughout

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc),
            # First call is the initial 15s dashboard-reachability wait;
            # the second is the deadline branch's 1s re-probe. Both must
            # succeed for this to be the non-fatal path.
            patch.object(bg_runner, "_wait_for_server", side_effect=[True, True]),
            patch("conductor.fleet.records.read_run_record", return_value=None),
            patch.object(bg_runner.time, "sleep"),
            patch.object(bg_runner.time, "monotonic", side_effect=[0.0, 0.0, 20.0]),
            patch.object(bg_runner, "_terminate_child") as mock_terminate,
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            launch = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9319,
            )

        mock_terminate.assert_not_called()
        assert launch.run_record_written is False
        assert launch.still_running is True

    def test_raises_and_terminates_child_when_run_record_never_appears(
        self, tmp_path: Path
    ) -> None:
        """A fatal ``RuntimeError`` (naming the stderr log) when the child
        never reports in *and* its dashboard has gone unreachable (the
        deadline branch's 1s re-probe fails), and the still-running child
        is terminated so it doesn't leak an orphaned process holding the
        dashboard port. This is the one case issue #435 keeps fatal --
        see ``test_run_record_never_appears_but_child_stays_reachable_downgrades_to_warning``
        for the non-fatal counterpart."""
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None  # still running throughout

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc),
            # First call is the initial 15s dashboard-reachability wait
            # (succeeds); the second is the deadline branch's 1s re-probe,
            # which fails -- this is what keeps this path fatal.
            patch.object(bg_runner, "_wait_for_server", side_effect=[True, False]),
            patch("conductor.fleet.records.read_run_record", return_value=None),
            patch.object(bg_runner.time, "sleep"),
            # Two calls before the loop body runs once (deadline computation,
            # then the while-condition check that lets the loop execute
            # once), then a third call that lands past the deadline so the
            # loop exits on its second condition check -- without this the
            # test would really wait out the 15s timeout.
            patch.object(bg_runner.time, "monotonic", side_effect=[0.0, 0.0, 20.0]),
            patch.object(bg_runner, "_terminate_child") as mock_terminate,
            pytest.raises(RuntimeError) as exc_info,
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9312,
            )

        mock_terminate.assert_called_once_with(fake_proc)
        msg = str(exc_info.value)
        assert "run record" in msg
        assert ".bg.stderr.log" in msg

    def test_raises_fast_when_child_dies_during_record_poll(self, tmp_path: Path) -> None:
        """A child that dies while the parent is still polling for its run
        record is reported immediately (its exit code surfaced), rather than
        the parent waiting out the full poll timeout.

        This is the regression the epic calls out: a child that starts its
        dashboard (so ``_wait_for_server`` already succeeded) and then dies
        -- e.g. on an invalid workflow discovered after the dashboard came
        up -- must be reported as a failed launch instead of silently
        appearing to succeed.
        """
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=1)
        # Still running for the dashboard-reachability check, then dies
        # partway through the run-record poll loop.
        fake_proc.poll.side_effect = [None, 9]

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.fleet.records.read_run_record", return_value=None),
            patch.object(bg_runner.time, "sleep"),
            patch.object(bg_runner, "_terminate_child") as mock_terminate,
            pytest.raises(RuntimeError, match="exited immediately with code 9"),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9313,
            )

        # The child was already dead -- nothing to terminate.
        mock_terminate.assert_not_called()

    def test_rejects_record_with_mismatched_pid_and_times_out(self, tmp_path: Path) -> None:
        """A run record found under the right ``run_id`` but the wrong
        ``pid`` must not be accepted as readiness.

        Without this check, a stale record left behind under the same
        ``run_id`` key by an unrelated process (or, in the forced-resume-
        run-id case, a leftover record from the *original*, now-dead run
        that the resumed child hasn't yet overwritten) would be mistaken
        for proof that *this* launch's child is up.
        """
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None  # still running throughout

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc),
            # Second element is the deadline branch's 1s reachability
            # re-probe, which fails here -- keeping this test on the fatal
            # path (issue #435 only downgrades to a warning when the child
            # is alive *and* still reachable at the deadline).
            patch.object(bg_runner, "_wait_for_server", side_effect=[True, False]),
            # A record exists under the polled run_id, but its pid belongs
            # to some other process entirely -- must be treated the same
            # as "no record yet".
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=999999),
            ),
            patch.object(bg_runner.time, "sleep"),
            patch.object(bg_runner.time, "monotonic", side_effect=[0.0, 0.0, 20.0]),
            patch.object(bg_runner, "_terminate_child") as mock_terminate,
            pytest.raises(RuntimeError, match="did not report a run record"),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9314,
            )

        mock_terminate.assert_called_once_with(fake_proc)

    def test_rejects_record_with_mismatched_mode_and_times_out(self, tmp_path: Path) -> None:
        """A run record found under the right ``run_id``/``pid`` but a
        non-``"bg"`` ``mode`` must not be accepted as readiness.

        Without this check, a record some other, differently-launched
        process happens to have written under a colliding ``run_id`` key
        could be mistaken for proof that *this* bg launch's child is up.
        """
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None  # still running throughout

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc),
            # Second element is the deadline branch's 1s reachability
            # re-probe, which fails here -- keeping this test on the fatal
            # path (issue #435 only downgrades to a warning when the child
            # is alive *and* still reachable at the deadline).
            patch.object(bg_runner, "_wait_for_server", side_effect=[True, False]),
            # Right run_id and pid, but the record is for a foreground
            # (non-bg) run -- must not satisfy the bg readiness gate.
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=1, mode="fg", port=None),
            ),
            patch.object(bg_runner.time, "sleep"),
            patch.object(bg_runner.time, "monotonic", side_effect=[0.0, 0.0, 20.0]),
            patch.object(bg_runner, "_terminate_child") as mock_terminate,
            pytest.raises(RuntimeError, match="did not report a run record"),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9316,
            )

        mock_terminate.assert_called_once_with(fake_proc)

    def test_rejects_record_with_mismatched_port_and_times_out(self, tmp_path: Path) -> None:
        """A run record found under the right ``run_id``/``pid``/``mode`` but
        listing a *different* port must not be accepted as readiness.

        This guards against advertising an unrelated service's record: if
        the requested port were occupied and the child fell back to a
        different (or no) port, a bare ``pid``/``mode`` match would wrongly
        declare the launch ready on the *originally requested* port.
        """
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None  # still running throughout

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc),
            # Second element is the deadline branch's 1s reachability
            # re-probe, which fails here -- keeping this test on the fatal
            # path (issue #435 only downgrades to a warning when the child
            # is alive *and* still reachable at the deadline).
            patch.object(bg_runner, "_wait_for_server", side_effect=[True, False]),
            # Right run_id, pid, and mode, but the record's port doesn't
            # match the port this launch requested.
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=1, mode="bg", port=9999),
            ),
            patch.object(bg_runner.time, "sleep"),
            patch.object(bg_runner.time, "monotonic", side_effect=[0.0, 0.0, 20.0]),
            patch.object(bg_runner, "_terminate_child") as mock_terminate,
            pytest.raises(RuntimeError, match="did not report a run record"),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9317,
            )

        mock_terminate.assert_called_once_with(fake_proc)

    def test_terminates_and_raises_when_read_run_record_raises(self, tmp_path: Path) -> None:
        """An exception from ``read_run_record`` itself must not escape uncontained.

        A failure while *reading* the record (e.g. a permission error
        creating/reading ``run_records_dir()``) must terminate the
        still-running child and raise a contextual ``RuntimeError`` naming
        the stderr log -- the same fatal contract as every other failure
        mode in this gate -- rather than propagating the raw exception and
        leaking an orphaned background process.
        """
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None  # still running throughout

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                side_effect=OSError("permission denied"),
            ),
            patch.object(bg_runner.time, "sleep"),
            patch.object(bg_runner, "_terminate_child") as mock_terminate,
            pytest.raises(RuntimeError) as exc_info,
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9318,
            )

        mock_terminate.assert_called_once_with(fake_proc)
        msg = str(exc_info.value)
        assert "run record" in msg
        assert ".bg.stderr.log" in msg

    def test_accepts_record_once_pid_matches(self, tmp_path: Path) -> None:
        """A mismatched-pid record followed by a matching one still succeeds.

        Simulates a genuine race: a stale record under the polled run_id
        (wrong pid) is visible on the first poll, then the real child's
        record (matching pid) replaces it on the very next poll.
        """
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                side_effect=[MagicMock(pid=999999), MagicMock(pid=1, mode="bg", port=9315)],
            ),
            patch.object(bg_runner.time, "sleep"),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            launch = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9315,
            )

        assert launch.url == "http://127.0.0.1:9315"


class TestRecordIsFresh:
    """Direct unit coverage for ``_record_is_fresh``, including the realistic
    boundary the end-to-end fixtures previously skipped over (issue #444
    follow-up): the child writes ``started_at`` milliseconds *after*
    ``launched_at``, so the case that actually matters is near-equal
    instants, not an hour of slack in either direction.
    """

    def test_equal_instants_are_fresh(self) -> None:
        """The realistic boundary: a record stamped at exactly ``launched_at``
        (the child writing its record in the same instant the parent
        captured the spawn time) must be accepted. A freshness check that
        requires strictly-after (or any positive margin) would reject every
        real record, since the child always writes second -- but the clock
        resolution of ``datetime.now(UTC)`` can coincide."""
        t = datetime.now(UTC)
        record = MagicMock(started_at=t.isoformat())
        assert bg_runner._record_is_fresh(record, t) is True

    def test_slightly_after_launched_at_is_fresh(self) -> None:
        """The realistic case: the child's ``started_at`` is a few
        milliseconds after ``launched_at``, not an hour."""
        launched_at = datetime.now(UTC)
        record = MagicMock(started_at=(launched_at + timedelta(milliseconds=5)).isoformat())
        assert bg_runner._record_is_fresh(record, launched_at) is True

    def test_slightly_before_launched_at_is_stale(self) -> None:
        """The realistic stale case: a leftover record stamped a few
        milliseconds before this launch spawned its child."""
        launched_at = datetime.now(UTC)
        record = MagicMock(started_at=(launched_at - timedelta(milliseconds=5)).isoformat())
        assert bg_runner._record_is_fresh(record, launched_at) is False

    def test_naive_timestamp_is_not_fresh(self) -> None:
        launched_at = datetime.now(UTC)
        record = MagicMock(started_at=datetime.now().isoformat())  # noqa: DTZ005 - deliberately naive
        assert bg_runner._record_is_fresh(record, launched_at) is False

    def test_non_string_started_at_is_not_fresh(self) -> None:
        launched_at = datetime.now(UTC)
        record = MagicMock(started_at=12345)
        assert bg_runner._record_is_fresh(record, launched_at) is False

    def test_unparseable_started_at_is_not_fresh(self) -> None:
        launched_at = datetime.now(UTC)
        record = MagicMock(started_at="not-a-timestamp")
        assert bg_runner._record_is_fresh(record, launched_at) is False

    def test_real_run_record_written_by_the_production_writer_is_fresh(
        self, tmp_path: Path
    ) -> None:
        """Producer/consumer contract test: pins that the ``started_at``
        format ``cli/run.py`` actually writes (``datetime.now(UTC)
        .isoformat()``, see ``_write_run_record_for_current_process``) is
        the format ``_record_is_fresh`` accepts. Uses a real ``RunRecord``
        round-tripped through ``write_run_record``/``read_run_record``
        rather than a ``MagicMock``, so nothing here passes merely because
        the mock auto-supplies a plausible-looking attribute; if the
        production writer ever switched to a naive ``datetime.now()``, this
        test -- not just the mocked ones -- would fail.
        """
        from conductor.fleet.records import RunRecord, read_run_record, write_run_record

        launched_at = datetime.now(UTC)
        write_run_record(
            RunRecord(
                run_id="deadbeef",
                pid=99999,
                workflow_path=str(tmp_path / "wf.yaml"),
                workflow_name="wf",
                started_at=datetime.now(UTC).isoformat(),
                event_log_path=str(tmp_path / "run.events.jsonl"),
                port=9470,
                mode="bg",
                checkpoint_dir=None,
            )
        )
        record = read_run_record("deadbeef")
        assert record is not None
        assert bg_runner._record_is_fresh(record, launched_at) is True


class TestTrampolinePidMismatchEndToEnd:
    """Issue #444, end-to-end via ``launch_background``.

    A trampoline ``sys.executable`` (e.g. a Windows ``uv tool install``)
    re-execs, so ``subprocess.Popen``'s pid and the real workflow process's
    pid legitimately differ even on a perfectly healthy launch. These tests
    exercise the full ``launch_background`` path (not just the unit-level
    helpers) to prove the identity model actually wires together: a fresh
    record with a mismatched pid is accepted, a stale one is not, an
    unconfirmed identity mismatch never kills a healthy run, and a
    confirmed identity mismatch still catches a real conflict.
    """

    def _wf(self, tmp_path: Path) -> Path:
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")
        return wf_path

    def test_accepts_record_from_trampoline_child_via_freshness(self, tmp_path: Path) -> None:
        """A record whose pid differs from ``Popen.pid`` but is fresh (written
        after this launch spawned its child) is accepted as this launch's own,
        and the *record's* pid -- not ``Popen.pid`` -- is what's carried
        forward as the confirmed identity for stage two.

        The record's ``started_at`` is stamped from inside the
        ``_spawn_detached`` side effect -- i.e. at the real moment of the
        Popen call, right after ``launched_at`` is captured in
        ``_spawn_bg_child`` -- rather than an artificial hour of slack, so
        this exercises the realistic near-equal boundary a real child
        actually writes at (issue #444 review).
        """
        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None
        captured: dict[str, str] = {}

        def _spawn(*_args: Any, **_kwargs: Any) -> MagicMock:
            captured["started_at"] = datetime.now(UTC).isoformat()
            return fake_proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_spawn),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                side_effect=lambda run_id: MagicMock(
                    pid=98765, mode="bg", port=9460, started_at=captured["started_at"]
                ),
            ),
            patch.object(bg_runner, "_terminate_child") as mock_terminate,
            patch.object(bg_runner, "_resolve_start_timeout", return_value=5.0),
            patch.object(
                bg_runner, "_wait_for_workflow_start", return_value=bg_runner.StartProbe.STARTED
            ) as mock_wait,
        ):
            launch = bg_runner.launch_background(
                workflow_path=self._wf(tmp_path),
                inputs={},
                web_port=9460,
            )

        mock_terminate.assert_not_called()
        assert launch.run_record_written is True
        assert launch.url == "http://127.0.0.1:9460"
        _, kwargs = mock_wait.call_args
        assert kwargs["confirmed_child_pid"] == 98765

    def test_stale_record_with_mismatched_pid_is_still_rejected(self, tmp_path: Path) -> None:
        """Freshness must not dissolve the stale-record guard it replaces: a
        record whose ``started_at`` predates this launch is rejected exactly
        as a plain pid mismatch always was."""
        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None  # still running throughout

        stale_started_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        stale_record = MagicMock(pid=98765, mode="bg", port=9461, started_at=stale_started_at)

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc),
            # Second element is the deadline branch's 1s reachability
            # re-probe, which fails here to keep this on the fatal path.
            patch.object(bg_runner, "_wait_for_server", side_effect=[True, False]),
            patch("conductor.fleet.records.read_run_record", return_value=stale_record),
            patch.object(bg_runner.time, "sleep"),
            patch.object(bg_runner.time, "monotonic", side_effect=[0.0, 0.0, 20.0]),
            patch.object(bg_runner, "_terminate_child") as mock_terminate,
            pytest.raises(RuntimeError, match="did not report a run record"),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background(
                workflow_path=self._wf(tmp_path),
                inputs={},
                web_port=9461,
            )

        mock_terminate.assert_called_once_with(fake_proc)

    def test_unconfirmed_identity_mismatch_never_kills_end_to_end(self, tmp_path: Path) -> None:
        """Q2 end-to-end: once the run-record poll has downgraded (issue #435 --
        no confirmed identity), stage two must be invoked with
        ``confirmed_child_pid=None``, so a genuinely mismatching payload keeps
        waiting instead of raising and terminating a possibly healthy run."""
        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None  # still running throughout

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc),
            # Both dashboard-reachability probes succeed -- the non-fatal,
            # degraded (#435) path.
            patch.object(bg_runner, "_wait_for_server", side_effect=[True, True]),
            patch("conductor.fleet.records.read_run_record", return_value=None),
            patch.object(bg_runner.time, "sleep"),
            patch.object(bg_runner.time, "monotonic", side_effect=[0.0, 0.0, 20.0]),
            patch.object(bg_runner, "_terminate_child") as mock_terminate,
            patch.object(bg_runner, "_resolve_start_timeout", return_value=30.0),
            patch.object(
                bg_runner, "_wait_for_workflow_start", return_value=bg_runner.StartProbe.TIMED_OUT
            ) as mock_wait,
        ):
            launch = bg_runner.launch_background(
                workflow_path=self._wf(tmp_path),
                inputs={},
                web_port=9462,
            )

        mock_terminate.assert_not_called()
        assert launch.run_record_written is False
        assert launch.workflow_started is False
        assert launch.still_running is True
        _, kwargs = mock_wait.call_args
        assert kwargs["confirmed_child_pid"] is None

    def test_confirmed_identity_still_detects_a_real_conflict_end_to_end(
        self, tmp_path: Path
    ) -> None:
        """A genuinely different process holding the port is still fatal once
        this launch's own identity has been confirmed by the run-record poll
        -- the fix narrows false positives, it doesn't remove the check."""
        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None

        matching_record = MagicMock(pid=1, mode="bg", port=9463)

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.fleet.records.read_run_record", return_value=matching_record),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=5.0),
            patch.object(bg_runner, "_terminate_child") as mock_terminate,
            patch.object(bg_runner, "_probe_workflow_info", return_value={"pid": 42424}),
            pytest.raises(RuntimeError, match="already in use") as exc_info,
        ):
            bg_runner.launch_background(
                workflow_path=self._wf(tmp_path),
                inputs={},
                web_port=9463,
            )

        assert "42424" in str(exc_info.value)
        mock_terminate.assert_called_once_with(fake_proc, confirmed_child_pid=1)


# ---------------------------------------------------------------------------
# Resume run_id consistency (D2 regression): the parent must poll for the
# *same* run_id the resumed child will actually write its run record under.
# ``resume_workflow_async`` reuses a checkpoint's original run_id whenever
# the checkpoint's event_log_path still points at a real file -- see
# ``EventLogSubscriber``'s ``existing_path``/``existing_run_id`` branch. A
# parent that always generated (and polled for) a fresh run_id would time
# out and kill a perfectly healthy resumed run in that case.
# ---------------------------------------------------------------------------


def _write_checkpoint_json(
    checkpoint_path: Path,
    *,
    run_id: str,
    event_log_path: str,
    workflow_path: Path,
) -> None:
    """Write a minimal, valid checkpoint JSON file for peek/resume tests."""
    from conductor.engine.checkpoint import CheckpointManager

    checkpoint_path.write_text(
        json.dumps(
            {
                "version": CheckpointManager.CHECKPOINT_VERSION,
                "workflow_path": str(workflow_path),
                "workflow_hash": "sha256:deadbeef",
                "created_at": "2026-01-01T00:00:00+00:00",
                "failure": {
                    "error_type": None,
                    "message": None,
                    "agent": "a",
                    "iteration": 1,
                },
                "current_agent": "a",
                "context": {"outputs": {}, "mode": "accumulate"},
                "limits": {"max_iterations": 50, "current_iteration": 1},
                "run_id": run_id,
                "event_log_path": event_log_path,
            }
        )
    )


class TestPeekResumeRunId:
    """``_peek_resume_run_id`` mirrors ``EventLogSubscriber``'s reuse decision."""

    def test_returns_checkpoint_run_id_when_event_log_survives(self, tmp_path: Path) -> None:
        from conductor.engine.checkpoint import CheckpointManager

        wf_path = _write_workflow(tmp_path)
        event_log = tmp_path / "original.events.jsonl"
        event_log.write_text("")
        cp_path = tmp_path / "test-wf-20260101-000000-abcd1234.json"
        _write_checkpoint_json(
            cp_path, run_id="deadbeef", event_log_path=str(event_log), workflow_path=wf_path
        )

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            result = bg_runner._peek_resume_run_id(wf_path, cp_path)

        assert result == "deadbeef"

    def test_accepts_non_hex_but_fleet_safe_checkpoint_run_id(self, tmp_path: Path) -> None:
        """A checkpoint ``run_id`` need not be hex to be reused by the peek.

        ``EventLogSubscriber``'s ``existing_path``/``existing_run_id``
        reuse branch (what the child actually goes through when resuming)
        performs no format check of its own on the checkpoint's ``run_id``
        -- it accepts it verbatim. The only real gate is whether the
        child's own ``write_run_record`` call will later accept it as a
        filename component, i.e. ``conductor.fleet.records.is_valid_run_id``.
        A hex-only check here would reject a checkpoint ``run_id`` the
        child would happily reuse, causing the parent to poll under a
        freshly generated id the resumed child never writes its record
        under -- killing a perfectly healthy resumed run.
        """
        from conductor.engine.checkpoint import CheckpointManager

        wf_path = _write_workflow(tmp_path)
        event_log = tmp_path / "original.events.jsonl"
        event_log.write_text("")
        cp_path = tmp_path / "test-wf-20260101-000000-abcd1234.json"
        # Not a hex string, but path-safe (alphanumeric + '-'/'_') -- exactly
        # what conductor.fleet.records.is_valid_run_id accepts.
        non_hex_run_id = "custom-run_ID-42"
        _write_checkpoint_json(
            cp_path,
            run_id=non_hex_run_id,
            event_log_path=str(event_log),
            workflow_path=wf_path,
        )

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            result = bg_runner._peek_resume_run_id(wf_path, cp_path)

        assert result == non_hex_run_id

    def test_rejects_checkpoint_run_id_that_is_not_fleet_safe(self, tmp_path: Path) -> None:
        """A checkpoint ``run_id`` containing path-unsafe characters (e.g.
        ``.``) is rejected -- it would never round-trip through
        ``write_run_record``/``read_run_record`` either, so polling for it
        would always time out."""
        from conductor.engine.checkpoint import CheckpointManager

        wf_path = _write_workflow(tmp_path)
        event_log = tmp_path / "original.events.jsonl"
        event_log.write_text("")
        cp_path = tmp_path / "test-wf-20260101-000000-abcd1234.json"
        _write_checkpoint_json(
            cp_path,
            run_id="../escape",
            event_log_path=str(event_log),
            workflow_path=wf_path,
        )

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            result = bg_runner._peek_resume_run_id(wf_path, cp_path)

        assert result is None

    def test_returns_none_when_event_log_file_is_gone(self, tmp_path: Path) -> None:
        from conductor.engine.checkpoint import CheckpointManager

        wf_path = _write_workflow(tmp_path)
        missing_log = tmp_path / "vanished.events.jsonl"
        cp_path = tmp_path / "test-wf-20260101-000000-abcd1234.json"
        _write_checkpoint_json(
            cp_path, run_id="deadbeef", event_log_path=str(missing_log), workflow_path=wf_path
        )

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            result = bg_runner._peek_resume_run_id(wf_path, cp_path)

        assert result is None

    def test_returns_none_when_checkpoint_has_no_run_id(self, tmp_path: Path) -> None:
        from conductor.engine.checkpoint import CheckpointManager

        wf_path = _write_workflow(tmp_path)
        event_log = tmp_path / "original.events.jsonl"
        event_log.write_text("")
        cp_path = tmp_path / "test-wf-20260101-000000-abcd1234.json"
        _write_checkpoint_json(
            cp_path, run_id="", event_log_path=str(event_log), workflow_path=wf_path
        )

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            result = bg_runner._peek_resume_run_id(wf_path, cp_path)

        assert result is None

    def test_returns_none_when_no_checkpoint_found(self, tmp_path: Path) -> None:
        from conductor.engine.checkpoint import CheckpointManager

        wf_path = _write_workflow(tmp_path)

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            result = bg_runner._peek_resume_run_id(wf_path, None)

        assert result is None

    def test_finds_latest_checkpoint_when_only_workflow_path_given(self, tmp_path: Path) -> None:
        from conductor.engine.checkpoint import CheckpointManager

        wf_path = _write_workflow(tmp_path)
        event_log = tmp_path / "original.events.jsonl"
        event_log.write_text("")
        cp_path = tmp_path / "test-wf-20260101-000000-abcd1234.json"
        _write_checkpoint_json(
            cp_path, run_id="deadbeef", event_log_path=str(event_log), workflow_path=wf_path
        )

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            result = bg_runner._peek_resume_run_id(wf_path, None)

        assert result == "deadbeef"


class TestResumeLaunchPollsCheckpointRunId:
    """Integration: ``launch_background_resume`` uses the checkpoint's run_id.

    Regression test for the reported bug: a resumed launch must poll
    ``read_run_record`` under the same run_id the resumed child will
    actually write its record under (the checkpoint's original run_id),
    not a freshly generated parent-side id -- otherwise a healthy resume
    would time out and be killed.
    """

    def test_env_and_poll_key_use_checkpoint_run_id(self, tmp_path: Path) -> None:
        from conductor.engine.checkpoint import CheckpointManager

        wf_path = _write_workflow(tmp_path)
        event_log = tmp_path / "original.events.jsonl"
        event_log.write_text("")
        cp_path = tmp_path / "test-wf-20260101-000000-abcd1234.json"
        _write_checkpoint_json(
            cp_path, run_id="cafef00d", event_log_path=str(event_log), workflow_path=wf_path
        )

        fake_proc = MagicMock(pid=3)
        fake_proc.poll.return_value = None

        with (
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc) as mock_spawn,
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=3, mode="bg", port=9320),
            ) as mock_read,
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            launch = bg_runner.launch_background_resume(
                workflow_path=wf_path,
                checkpoint_path=cp_path,
                web_port=9320,
            )

        # The launch's own run_id, the env var handed to the child, and the
        # key polled via read_run_record must all agree with each other AND
        # with the checkpoint's original run_id.
        assert launch.run_id == "cafef00d"
        env = mock_spawn.call_args.args[1]
        assert env["CONDUCTOR_RUN_ID"] == "cafef00d"
        mock_read.assert_called_once_with("cafef00d")

    def test_falls_back_to_fresh_run_id_when_no_checkpoint_reuse(self, tmp_path: Path) -> None:
        """No checkpoint (or one whose event log vanished) -- fresh id, as before."""
        from conductor.engine.checkpoint import CheckpointManager

        wf_path = _write_workflow(tmp_path)

        fake_proc = MagicMock(pid=4)
        fake_proc.poll.return_value = None

        with (
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=4, mode="bg", port=9321),
            ) as mock_read,
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            launch = bg_runner.launch_background_resume(
                workflow_path=wf_path,
                checkpoint_path=None,
                web_port=9321,
            )

        assert re.fullmatch(r"[0-9a-f]{8}", launch.run_id)
        mock_read.assert_called_once_with(launch.run_id)


def _write_workflow(tmp_path: Path) -> Path:
    wf_path = tmp_path / "test-wf.yaml"
    wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")
    return wf_path


@pytest.fixture(autouse=True)
def _clean_bg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no parent CONDUCTOR_* bg env vars leak between tests."""
    for key in (
        "CONDUCTOR_RUN_ID",
        "CONDUCTOR_BG_STDERR_LOG",
        "CONDUCTOR_BG_STDOUT_LOG",
        "CONDUCTOR_WEB_BG",
        "CONDUCTOR_WEB_PORT",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# launch_background diagnostics
# ---------------------------------------------------------------------------


class TestLaunchBackgroundDiagnostics:
    """Diagnostics-side behaviour of ``launch_background`` (issue #116)."""

    def test_returns_structured_launch_with_log_paths(self, tmp_path: Path) -> None:
        """``launch_background`` returns a ``BackgroundLaunch`` with both log paths."""
        from conductor.cli import bg_runner

        wf_path = _write_workflow(tmp_path)

        def _fake_popen(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: Any
        ) -> MagicMock:
            proc = MagicMock()
            proc.pid = 1234
            proc.poll.return_value = None
            return proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=1234, mode="bg", port=9300),
            ),
            patch("conductor.cli.bg_runner._resolve_start_timeout", return_value=0.0),
        ):
            launch = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9300,
            )

        assert launch.url == "http://127.0.0.1:9300"
        assert launch.stderr_log.name.endswith(".bg.stderr.log")
        assert launch.stdout_log.name.endswith(".bg.stdout.log")
        # 8-hex-character run id, suitable for filename embedding.
        assert re.fullmatch(r"[0-9a-f]{8}", launch.run_id), launch.run_id
        # Filenames embed the same run id, so events JSONL (which honours
        # CONDUCTOR_RUN_ID) can be correlated with the bg log files.
        assert launch.run_id in launch.stderr_log.name
        assert launch.run_id in launch.stdout_log.name
        # The log files are siblings of the events JSONL under TMPDIR/conductor/
        assert launch.stderr_log.parent.name == "conductor"
        assert launch.stderr_log.parent.parent == Path(tempfile.gettempdir())

    def test_popen_receives_file_handles_not_devnull(self, tmp_path: Path) -> None:
        """stderr/stdout must NOT be ``DEVNULL`` — that's what causes #116's silent crash."""
        from conductor.cli import bg_runner

        wf_path = _write_workflow(tmp_path)

        captured: dict[str, Any] = {}

        def _fake_popen(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: Any
        ) -> MagicMock:
            captured.update(kwargs)
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None
            return proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=1, mode="bg", port=9301),
            ),
            patch("conductor.cli.bg_runner._resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9301,
            )

        assert (
            captured.get("stdin", subprocess.DEVNULL) is subprocess.DEVNULL
        )  # no interactive input
        for stream_name in ("stdout", "stderr"):
            stream = captured[stream_name]
            assert stream is not subprocess.DEVNULL
            assert hasattr(stream, "write")
            assert stream.name.endswith(".log")

    def test_parent_handles_closed_after_popen(self, tmp_path: Path) -> None:
        """Parent-side stdout/stderr file handles are closed after Popen returns.

        The child has its own duplicated OS handles, so the parent's Python
        file objects can — and should — be released immediately. Leaving
        them open would leak file descriptors in the parent.
        """
        from conductor.cli import bg_runner

        wf_path = _write_workflow(tmp_path)

        captured: dict[str, Any] = {}

        def _fake_popen(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: Any
        ) -> MagicMock:
            captured["stdout"] = kwargs["stdout"]
            captured["stderr"] = kwargs["stderr"]
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None
            return proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=1, mode="bg", port=9302),
            ),
            patch("conductor.cli.bg_runner._resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9302,
            )

        assert captured["stdout"].closed
        assert captured["stderr"].closed

    def test_parent_handles_closed_on_finalize_failure(self, tmp_path: Path) -> None:
        """Parent handles are closed even when ``_finalize_background_launch`` raises."""
        from conductor.cli import bg_runner

        wf_path = _write_workflow(tmp_path)

        captured: dict[str, Any] = {}

        def _fake_popen(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: Any
        ) -> MagicMock:
            captured["stdout"] = kwargs["stdout"]
            captured["stderr"] = kwargs["stderr"]
            proc = MagicMock()
            proc.pid = 1
            # Simulate child exited immediately — _finalize raises RuntimeError.
            proc.poll.return_value = 7
            return proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=False),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=1, mode="bg", port=9303),
            ),
            pytest.raises(RuntimeError, match="exited immediately with code 7"),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9303,
            )

        assert captured["stdout"].closed
        assert captured["stderr"].closed

    def test_env_wires_correlation_vars(self, tmp_path: Path) -> None:
        """``CONDUCTOR_RUN_ID`` / log paths are passed to the child via env.

        Also asserts the PID-file ``run_id`` equals ``env["CONDUCTOR_RUN_ID"]``
        — the property that makes the events JSONL findable by glob from the
        PID file alone (issue #404).
        """
        from conductor.cli import bg_runner

        wf_path = _write_workflow(tmp_path)

        captured: dict[str, Any] = {}

        def _fake_popen(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: Any
        ) -> MagicMock:
            captured["env"] = env
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None
            return proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=1, mode="bg", port=9304),
            ),
            patch("conductor.cli.bg_runner._resolve_start_timeout", return_value=0.0),
        ):
            launch = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9304,
            )

        env = captured["env"]
        assert env["CONDUCTOR_WEB_BG"] == "1"
        assert env["CONDUCTOR_WEB_PORT"] == "9304"
        assert env["CONDUCTOR_RUN_ID"] == launch.run_id
        assert env["CONDUCTOR_BG_STDERR_LOG"] == str(launch.stderr_log)
        assert env["CONDUCTOR_BG_STDOUT_LOG"] == str(launch.stdout_log)

    def test_launch_correlates_run_id_and_capture_logs(self, tmp_path: Path) -> None:
        """Regression guard for issue #404: a launch must not ship dead fields.

        Before that fix, the launcher recorded an empty ``run_id`` and a
        nonexistent ``log_file``, so a bg run could not be correlated to its
        events JSONL or its captured stderr/stdout at all.

        The parent no longer writes a PID file (Fleet Manager D2) -- the child
        writes its own run record and the parent polls for it -- so the
        guarantee is now asserted where it lives: the launch gate polls the
        same ``run_id`` the returned ``BackgroundLaunch`` reports, and that id
        appears in both capture-log filenames.
        """
        from conductor.cli import bg_runner

        wf_path = _write_workflow(tmp_path)

        def _fake_popen(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: Any
        ) -> MagicMock:
            proc = MagicMock()
            proc.pid = 4321
            proc.poll.return_value = None
            return proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=4321, mode="bg", port=9305),
            ) as mock_read,
            patch("conductor.cli.bg_runner._resolve_start_timeout", return_value=0.0),
        ):
            launch = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9305,
            )

        mock_read.assert_called_once_with(launch.run_id)
        assert launch.run_id  # non-empty
        # The id is what ties the three artefacts of one run together, so it
        # must appear in the capture-log names the launch reports.
        assert launch.run_id in str(launch.stderr_log)
        assert launch.run_id in str(launch.stdout_log)

    def test_early_exit_error_mentions_stderr_log_path(self, tmp_path: Path) -> None:
        """RuntimeError raised on early child exit includes the stderr log path."""
        from conductor.cli import bg_runner

        wf_path = _write_workflow(tmp_path)

        def _fake_popen(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: Any
        ) -> MagicMock:
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = 42  # immediate exit code 42
            return proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=False),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=1, mode="bg", port=9305),
            ),
            pytest.raises(RuntimeError) as exc_info,
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9305,
            )

        # The message must point users at the captured log so they can find
        # the traceback that previously vanished into DEVNULL.
        msg = str(exc_info.value)
        assert "exited immediately with code 42" in msg
        assert "stderr log" in msg
        assert ".bg.stderr.log" in msg

    def test_dashboard_timeout_error_mentions_stderr_log_path(self, tmp_path: Path) -> None:
        """RuntimeError raised on dashboard timeout includes the stderr log path."""
        from conductor.cli import bg_runner

        wf_path = _write_workflow(tmp_path)

        def _fake_popen(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: Any
        ) -> MagicMock:
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None  # still running
            return proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=False),
            patch("conductor.cli.bg_runner._terminate_child"),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=1, mode="bg", port=9306),
            ),
            pytest.raises(RuntimeError) as exc_info,
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9306,
            )

        msg = str(exc_info.value)
        assert "Dashboard did not start" in msg
        assert "stderr log" in msg
        assert ".bg.stderr.log" in msg


# ---------------------------------------------------------------------------
# Filename sanitisation
# ---------------------------------------------------------------------------


class TestSanitizeName:
    """The bg log filename is derived from the workflow stem and must be safe."""

    def test_strips_path_separators_and_specials(self) -> None:
        from conductor.cli.bg_runner import _sanitize_name

        assert _sanitize_name("my/weird:name") == "my-weird-name"
        # In practice the caller passes ``Path(...).stem`` (e.g. "passwd"),
        # so leading dots are uncommon — but the sanitizer must still
        # leave the rest of the name intact if such a stem ever arrives.
        assert _sanitize_name("../etc/passwd") == "..-etc-passwd"
        assert _sanitize_name("normal-name.v1") == "normal-name.v1"

    def test_empty_falls_back_to_workflow(self) -> None:
        from conductor.cli.bg_runner import _sanitize_name

        assert _sanitize_name("") == "workflow"
        assert _sanitize_name("///") == "workflow"


def _declared_option_strings(subcommand: str) -> set[str]:
    """Every ``--flag`` the real CLI declares for ``subcommand``.

    Reads the actual Click command Typer builds, so this reflects what a
    spawned child would genuinely accept rather than a restatement of it.
    """
    import typer.main

    from conductor.cli.app import app

    command = typer.main.get_command(app).commands[subcommand]  # ty: ignore[possibly-unbound-attribute]
    return {opt for param in command.params for opt in param.opts if opt.startswith("--")}


def _flags_in(cmd: list[str]) -> set[str]:
    """The ``--flag`` tokens of a built argv (values are never flag-shaped here)."""
    return {token for token in cmd if token.startswith("--")}


class TestLaunchedArgvIsAcceptedByTheChildCLI:
    """Every flag the launcher emits must exist on the command it spawns.

    These tests never spawn a process -- ``subprocess.Popen`` is patched out
    across this module -- so asserting on the argv the launcher *builds*
    proves only that it built what the test expected, not that anything
    could run it. That gap shipped a real bug: ``bg_runner`` forwarded
    inputs via ``--input-json``, a flag ``conductor run`` never declared, so
    every ``--web-bg`` launch carrying an input died instantly with exit
    code 2 -- including every launch from the Fleet Manager's New Run
    screen, whose form is built from the workflow's declared inputs and
    therefore essentially always passes some.

    Checking the built argv against the child command's real, declared
    options closes that gap without paying for a subprocess.
    """

    def _build_run_cmd(self, tmp_path: Path, inputs: dict[str, Any]) -> list[str]:
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=1)
        fake_proc.poll.return_value = None

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc) as mock_spawn,
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=1, mode="bg", port=9401),
            ),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs=inputs,
                web_port=9401,
                metadata={"tracker": "ado"},
                provider_override="copilot",
                skip_gates=True,
            )

        return mock_spawn.call_args.args[0]

    def test_run_launcher_emits_only_declared_flags(self, tmp_path: Path) -> None:
        cmd = self._build_run_cmd(
            tmp_path,
            # One of every shape ``_serialize_input_value`` round-trips, since
            # the flag carrying them is the one that was missing.
            {"topic": "tidal power", "count": 3, "deep": True, "tags": ["a", "b"]},
        )

        undeclared = _flags_in(cmd) - _declared_option_strings("run")
        assert not undeclared, f"launcher emits flags `conductor run` rejects: {sorted(undeclared)}"

    def test_run_launcher_forwards_inputs_at_all(self, tmp_path: Path) -> None:
        """Guards the other direction: a launcher that silently dropped
        inputs would trivially satisfy the check above."""
        cmd = self._build_run_cmd(tmp_path, {"topic": "tidal power"})

        assert "--input-json" in cmd
        assert 'topic="tidal power"' in cmd

    def test_resume_launcher_emits_only_declared_flags(self, tmp_path: Path) -> None:
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        fake_proc = MagicMock(pid=2)
        fake_proc.poll.return_value = None

        with (
            patch.object(bg_runner, "_spawn_detached", return_value=fake_proc) as mock_spawn,
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=MagicMock(pid=2, mode="bg", port=9402),
            ),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            bg_runner.launch_background_resume(
                workflow_path=wf_path,
                checkpoint_path=None,
                web_port=9402,
                metadata={"tracker": "ado"},
                provider_override="copilot",
                skip_gates=True,
                guidance=["Skip the benchmark step"],
            )

        cmd = mock_spawn.call_args.args[0]
        undeclared = _flags_in(cmd) - _declared_option_strings("resume")
        assert not undeclared, (
            f"launcher emits flags `conductor resume` rejects: {sorted(undeclared)}"
        )


# ---------------------------------------------------------------------------
# Issue #410 — two-stage readiness contract
# ---------------------------------------------------------------------------


class TestWaitForServerEarlyExit:
    """``_wait_for_server`` must notice a dead child well before its timeout."""

    def test_returns_false_immediately_when_child_already_exited(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = 1

        start = time.monotonic()
        result = bg_runner._wait_for_server(9400, timeout=15.0, proc=proc)
        elapsed = time.monotonic() - start

        assert result is False
        assert elapsed < 2.0

    def test_ignores_proc_when_not_given(self) -> None:
        """Without ``proc``, behavior is unchanged: only the timeout applies."""
        with patch.object(bg_runner.socket, "create_connection", side_effect=OSError("no")):
            result = bg_runner._wait_for_server(9401, timeout=0.3)
        assert result is False


class TestResolveStartTimeout:
    """Env parsing for ``CONDUCTOR_WEB_BG_START_TIMEOUT``."""

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(bg_runner._START_TIMEOUT_ENV, raising=False)
        assert bg_runner._resolve_start_timeout() == bg_runner._START_TIMEOUT_DEFAULT

    def test_valid_value_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(bg_runner._START_TIMEOUT_ENV, "5")
        assert bg_runner._resolve_start_timeout() == 5.0

    def test_zero_disables_the_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(bg_runner._START_TIMEOUT_ENV, "0")
        assert bg_runner._resolve_start_timeout() == 0.0

    def test_garbage_value_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(bg_runner._START_TIMEOUT_ENV, "not-a-number")
        assert bg_runner._resolve_start_timeout() == bg_runner._START_TIMEOUT_DEFAULT

    def test_negative_value_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(bg_runner._START_TIMEOUT_ENV, "-5")
        assert bg_runner._resolve_start_timeout() == bg_runner._START_TIMEOUT_DEFAULT


class TestTailLog:
    """``_tail_log`` bounds and never raises."""

    def test_missing_file_returns_empty_string(self, tmp_path: Path) -> None:
        assert bg_runner._tail_log(tmp_path / "does-not-exist.log") == ""

    def test_returns_last_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "out.log"
        log.write_text("\n".join(f"line {i}" for i in range(50)))

        tail = bg_runner._tail_log(log, max_lines=5)

        assert "line 49" in tail
        assert "line 0" not in tail
        assert str(log) in tail

    def test_bounds_total_chars(self, tmp_path: Path) -> None:
        log = tmp_path / "huge.log"
        log.write_text("x" * 10_000)

        tail = bg_runner._tail_log(log, max_lines=1, max_chars=100)

        assert len(tail) <= 100 + len(f"\n--- last 1 line(s) of {log} ---\n")

    def test_empty_file_returns_empty_string(self, tmp_path: Path) -> None:
        log = tmp_path / "empty.log"
        log.write_text("")
        assert bg_runner._tail_log(log) == ""


class TestProbeWorkflowInfo:
    """``_probe_workflow_info``'s narrowed exception handling (issue #410 follow-up).

    Only connection/timeout/HTTP-status errors and JSON-decode failures are
    "not ready yet" — an unexpected exception type must not be silently
    folded into the same bucket, or a persistent bug in this function would
    be indistinguishable from normal startup latency for the caller's full
    wait window.
    """

    def test_connect_error_returns_none(self) -> None:
        import httpx

        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            assert bg_runner._probe_workflow_info(9440) is None

    def test_http_status_error_returns_none(self) -> None:
        import httpx

        with patch("httpx.get") as get:
            get.return_value.raise_for_status.side_effect = httpx.HTTPStatusError(
                "not found", request=MagicMock(), response=MagicMock(status_code=404)
            )
            assert bg_runner._probe_workflow_info(9441) is None

    def test_non_json_body_returns_none(self) -> None:
        with patch("httpx.get") as get:
            get.return_value.raise_for_status.return_value = None
            get.return_value.json.side_effect = ValueError("not json")
            assert bg_runner._probe_workflow_info(9442) is None

    def test_non_dict_body_returns_none(self) -> None:
        with patch("httpx.get") as get:
            get.return_value.raise_for_status.return_value = None
            get.return_value.json.return_value = [1, 2, 3]
            assert bg_runner._probe_workflow_info(9443) is None

    def test_unexpected_exception_logged_at_warning_not_debug(self, caplog: Any) -> None:
        """A bug in this function must not masquerade as 'still starting'."""
        import logging

        with (
            patch("httpx.get", side_effect=TypeError("boom")),
            caplog.at_level(logging.WARNING, logger="conductor.cli.bg_runner"),
        ):
            result = bg_runner._probe_workflow_info(9444)

        assert result is None
        assert any("unexpected TypeError" in record.getMessage() for record in caplog.records)

    def test_valid_dict_body_passed_through(self) -> None:
        with patch("httpx.get") as get:
            get.return_value.raise_for_status.return_value = None
            get.return_value.json.return_value = {"pid": 1, "started_at": 5.0}
            assert bg_runner._probe_workflow_info(9445) == {"pid": 1, "started_at": 5.0}


class TestClassifyDashboardIdentity:
    """Direct unit coverage for ``_classify_dashboard_identity``'s five branches.

    Issue #444 follow-up: the pid arm only fires when *this launch's own*
    ``child_pid`` has been confirmed. A reported int ``pid`` with no
    confirmed ``child_pid`` must be ``UNKNOWN``, not fall through to a
    ``run_id`` comparison -- ``run_id`` legitimately differs on a resume
    (the child adopts the checkpoint's original run id), so comparing it
    would misjudge a resumed run's own healthy dashboard as FOREIGN.
    """

    def test_pid_ours(self) -> None:
        result = bg_runner._classify_dashboard_identity(
            {"pid": 123}, expected_run_id="deadbeef", child_pid=123
        )
        assert result is bg_runner._DashboardIdentity.OURS

    def test_pid_foreign(self) -> None:
        result = bg_runner._classify_dashboard_identity(
            {"pid": 999}, expected_run_id="deadbeef", child_pid=123
        )
        assert result is bg_runner._DashboardIdentity.FOREIGN

    def test_run_id_ours_when_no_usable_pid_in_payload(self) -> None:
        result = bg_runner._classify_dashboard_identity(
            {"run_id": "deadbeef"}, expected_run_id="deadbeef", child_pid=None
        )
        assert result is bg_runner._DashboardIdentity.OURS

    def test_run_id_foreign_when_no_usable_pid_in_payload(self) -> None:
        result = bg_runner._classify_dashboard_identity(
            {"run_id": "someone-elses-run"}, expected_run_id="deadbeef", child_pid=None
        )
        assert result is bg_runner._DashboardIdentity.FOREIGN

    def test_unknown_when_no_identity_signal_at_all(self) -> None:
        result = bg_runner._classify_dashboard_identity(
            {}, expected_run_id="deadbeef", child_pid=None
        )
        assert result is bg_runner._DashboardIdentity.UNKNOWN

    def test_unknown_when_pid_reported_but_our_own_identity_unconfirmed(self) -> None:
        """The issue #444 regression this fix closes: a payload reporting a
        usable pid (which ``/api/info`` always does) must not be compared
        against ``run_id`` just because this launch's own ``child_pid`` is
        still unconfirmed -- that would misjudge this launch's own healthy
        dashboard as FOREIGN whenever a resume's ``run_id`` legitimately
        differs from what this launch expected."""
        result = bg_runner._classify_dashboard_identity(
            {"pid": 123, "run_id": "someone-elses-run"},
            expected_run_id="deadbeef",
            child_pid=None,
        )
        assert result is bg_runner._DashboardIdentity.UNKNOWN


class TestWaitForWorkflowStart:
    """``_wait_for_workflow_start`` outcome coverage via ``_probe_workflow_info``."""

    def test_started_when_started_at_key_present(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        # Deliberately different from ``confirmed_child_pid`` below: the
        # function must compare against the confirmed pid, not ``proc.pid``
        # (issue #444) -- a review recommendation to make sure this test
        # can actually distinguish the two.
        proc.pid = 1

        with patch.object(
            bg_runner, "_probe_workflow_info", return_value={"pid": 123, "started_at": 0}
        ):
            result = bg_runner._wait_for_workflow_start(
                9410, proc, timeout=5.0, expected_run_id="deadbeef", confirmed_child_pid=123
            )

        assert result is bg_runner.StartProbe.STARTED

    def test_started_at_zero_still_counts_as_started(self) -> None:
        """``started_at`` can legitimately be falsy 0 — key presence is what matters."""
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 1

        with patch.object(
            bg_runner, "_probe_workflow_info", return_value={"pid": 123, "started_at": 0}
        ):
            result = bg_runner._wait_for_workflow_start(
                9411, proc, timeout=5.0, expected_run_id="deadbeef", confirmed_child_pid=123
            )

        assert result is bg_runner.StartProbe.STARTED

    def test_child_exited_takes_priority_over_probe(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.pid = 1

        with patch.object(bg_runner, "_probe_workflow_info") as mock_probe:
            result = bg_runner._wait_for_workflow_start(
                9412, proc, timeout=5.0, expected_run_id="deadbeef", confirmed_child_pid=123
            )

        assert result is bg_runner.StartProbe.CHILD_EXITED
        mock_probe.assert_not_called()

    def test_port_conflict_when_identity_confirmed_and_pid_differs(self) -> None:
        """A conflict is only fatal when this launch confirmed its own identity (issue #444)."""
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 1

        with patch.object(bg_runner, "_probe_workflow_info", return_value={"pid": 999}):
            result = bg_runner._wait_for_workflow_start(
                9413, proc, timeout=5.0, expected_run_id="deadbeef", confirmed_child_pid=123
            )

        assert result is bg_runner.StartProbe.PORT_CONFLICT

    def test_timed_out_when_never_started(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 1

        with patch.object(bg_runner, "_probe_workflow_info", return_value=None):
            result = bg_runner._wait_for_workflow_start(
                9414, proc, timeout=0.5, expected_run_id="deadbeef", confirmed_child_pid=123
            )

        assert result is bg_runner.StartProbe.TIMED_OUT

    def test_none_probe_result_keeps_waiting(self) -> None:
        """A probe returning ``None`` (e.g. connection refused) doesn't end the wait early."""
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 1

        responses = [None, None, {"pid": 123, "started_at": 1.0}]

        with patch.object(bg_runner, "_probe_workflow_info", side_effect=responses):
            result = bg_runner._wait_for_workflow_start(
                9415, proc, timeout=5.0, expected_run_id="deadbeef", confirmed_child_pid=123
            )

        assert result is bg_runner.StartProbe.STARTED

    def test_trampoline_child_pid_differs_from_proc_pid_is_not_foreign(self) -> None:
        """The issue #444 regression: a real two-level process tree where the
        spawned ``Popen`` pid and the confirmed child pid legitimately
        differ, mirroring a trampoline ``sys.executable`` (a Windows
        ``uv tool install``, the documented install path) that re-execs
        into a different process. Anchoring this to real OS-spawned
        processes (rather than two arbitrary integers) is what makes it a
        regression test for the actual bug rather than for a fabricated
        pair of numbers.
        """
        outer = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys, time; "
                "p = subprocess.Popen([sys.executable, '-c', "
                "'import os,sys,time; print(os.getpid(), flush=True); time.sleep(30)'], "
                "stdout=subprocess.PIPE, text=True); "
                "line = p.stdout.readline(); "
                "sys.stdout.write(line); sys.stdout.flush(); "
                "time.sleep(30)",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        inner_pid: int | None = None
        try:
            assert outer.stdout is not None
            # Bound the handshake read: a nested interpreter that hangs
            # without crashing (and so never closes the pipe) would
            # otherwise block this ``readline()`` -- and the whole suite,
            # since no pytest-timeout is configured -- indefinitely.
            # ``selectors`` is POSIX-only for pipes, matching this test's
            # own framing of the bug as reproducible only on a real OS
            # process tree rather than something to generalize to Windows.
            if sys.platform != "win32":
                sel = selectors.DefaultSelector()
                sel.register(outer.stdout, selectors.EVENT_READ)
                try:
                    if not sel.select(timeout=10.0):
                        pytest.fail("nested interpreter did not report its pid within 10s")
                finally:
                    sel.close()
            line = outer.stdout.readline()
            if not line:
                pytest.fail("nested interpreter closed its stdout without reporting a pid")
            inner_pid = int(line.strip())
            if inner_pid == outer.pid:
                pytest.skip("inner and outer pids coincided; cannot exercise the mismatch")

            with patch.object(
                bg_runner,
                "_probe_workflow_info",
                return_value={"pid": inner_pid, "started_at": 0},
            ):
                result = bg_runner._wait_for_workflow_start(
                    9450,
                    outer,
                    timeout=5.0,
                    expected_run_id="deadbeef",
                    confirmed_child_pid=inner_pid,
                )

            assert result is bg_runner.StartProbe.STARTED
        finally:
            outer.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                outer.wait(timeout=5.0)
            if outer.poll() is None:
                outer.kill()
                outer.wait(timeout=5.0)
            # ``outer.terminate()``/``.kill()`` only reaches the outer
            # process; the inner one it spawned is reparented and survives
            # unless killed directly.
            if inner_pid is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    sig = signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL
                    os.kill(inner_pid, sig)

    def test_unconfirmed_identity_mismatch_never_kills(self) -> None:
        """Q2: a mismatch with no confirmed identity keeps waiting instead of
        being reported as a port conflict -- there is no trustworthy basis
        for concluding the port is genuinely held by someone else."""
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 123

        with patch.object(
            bg_runner, "_probe_workflow_info", return_value={"run_id": "someone-elses-run"}
        ):
            result = bg_runner._wait_for_workflow_start(
                9451,
                proc,
                timeout=0.5,
                expected_run_id="deadbeef",
                confirmed_child_pid=None,
            )

        assert result is bg_runner.StartProbe.TIMED_OUT

    def test_unconfirmed_foreign_mismatch_does_not_accept_a_reported_start(self) -> None:
        """Pins the deliberate skip at the ``elif "started_at" in info`` branch:
        an unconfirmed-FOREIGN payload (no usable pid, mismatched run_id)
        must not be accepted as proof *our* workflow started even when it
        also reports a ``started_at`` key -- mutating that ``elif`` back to
        a plain ``if`` would let an unverified foreign dashboard's own start
        be reported as this launch's."""
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 123

        with patch.object(
            bg_runner,
            "_probe_workflow_info",
            return_value={"run_id": "someone-elses-run", "started_at": 0},
        ):
            result = bg_runner._wait_for_workflow_start(
                9453,
                proc,
                timeout=0.5,
                expected_run_id="deadbeef",
                confirmed_child_pid=None,
            )

        assert result is bg_runner.StartProbe.TIMED_OUT

    def test_last_seen_info_is_populated_on_port_conflict(self) -> None:
        """The caller-supplied ``last_seen_info`` carries the foreign payload
        back so the caller can name the real PID without a second, racy
        probe after the child has already been terminated."""
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 123
        last_seen_info: dict[str, Any] = {}

        with patch.object(bg_runner, "_probe_workflow_info", return_value={"pid": 999}):
            result = bg_runner._wait_for_workflow_start(
                9452,
                proc,
                timeout=5.0,
                expected_run_id="deadbeef",
                confirmed_child_pid=123,
                last_seen_info=last_seen_info,
            )

        assert result is bg_runner.StartProbe.PORT_CONFLICT
        assert last_seen_info == {"pid": 999}


class TestFinalizeBackgroundLaunchStageTwo:
    """``_finalize_background_launch``'s post-record-poll stage-two behavior.

    The gate that precedes stage two is the *child's own run record* (Fleet
    Manager D2), not the parent-side PID write these tests originally drove:
    ``write_pid_file`` was removed with that change, and the cleanup on a
    failed stage two is ``_remove_dead_child_record`` rather than
    ``remove_pid_file_at``. The stage-two contract itself (issue #410) is
    unchanged, which is what these tests still pin.
    """

    def _make_proc(self, pid: int = 111) -> MagicMock:
        proc = MagicMock()
        proc.pid = pid
        proc.poll.return_value = None
        return proc

    def _record_for(self, proc: MagicMock, port: int) -> MagicMock:
        """A run record the gate will accept as this launch's own."""
        return MagicMock(pid=proc.pid, mode="bg", port=port)

    def _launched_at(self) -> datetime:
        """A fixed spawn timestamp for tests that don't exercise freshness directly."""
        return datetime(2026, 1, 1, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("probe", "expected_workflow_started"),
        [
            (bg_runner.StartProbe.STARTED, True),
            (bg_runner.StartProbe.TIMED_OUT, False),
            (bg_runner.StartProbe.CHILD_EXITED, True),
        ],
    )
    def test_run_record_written_is_false_through_stage_two_when_record_never_appears(
        self,
        tmp_path: Path,
        probe: bg_runner.StartProbe,
        expected_workflow_started: bool,
    ) -> None:
        """Issue #435's downgrade must survive into every stage-two outcome,
        not just the ``start_timeout == 0`` early return -- mutating the
        ``StartProbe.STARTED`` arm to hard-code ``run_record_written=True``
        must fail this test (and its sibling below)."""
        proc = self._make_proc()
        if probe is bg_runner.StartProbe.CHILD_EXITED:
            # Alive through the record-poll deadline (including its
            # leniency re-check), exited cleanly by the time stage two
            # reports CHILD_EXITED.
            _poll = iter([None, None, None])
            proc.poll.side_effect = lambda: next(_poll, 0)

        with (
            # First call is the initial 15s dashboard-reachability wait;
            # the second is the deadline branch's 1s re-probe -- both must
            # succeed to reach the non-fatal, degraded path.
            patch.object(bg_runner, "_wait_for_server", side_effect=[True, True]),
            patch("conductor.fleet.records.read_run_record", return_value=None),
            patch.object(bg_runner.time, "sleep"),
            patch.object(bg_runner.time, "monotonic", side_effect=[0.0, 0.0, 20.0]),
            patch.object(bg_runner, "_terminate_child") as mock_terminate,
            patch.object(bg_runner, "_remove_dead_child_record") as mock_remove,
            patch.object(bg_runner, "_resolve_start_timeout", return_value=30.0),
            patch.object(bg_runner, "_wait_for_workflow_start", return_value=probe),
        ):
            result = bg_runner._finalize_background_launch(
                proc, 9430, "deadbeef", tmp_path / "err.log", launched_at=self._launched_at()
            )

        assert result.run_record_written is False
        assert result.workflow_started is expected_workflow_started
        mock_terminate.assert_not_called()
        mock_remove.assert_not_called()

    def test_run_record_written_is_true_through_stage_two_when_record_matches(
        self, tmp_path: Path
    ) -> None:
        """The matched-record counterpart to the test above -- guards against
        the flag simply being hard-coded ``False``."""
        proc = self._make_proc()

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=self._record_for(proc, 9431),
            ),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=30.0),
            patch.object(
                bg_runner, "_wait_for_workflow_start", return_value=bg_runner.StartProbe.STARTED
            ),
        ):
            result = bg_runner._finalize_background_launch(
                proc, 9431, "deadbeef", tmp_path / "err.log", launched_at=self._launched_at()
            )

        assert result.run_record_written is True
        assert result.workflow_started is True

    def test_record_polled_before_stage_two_wait(self, tmp_path: Path) -> None:
        """The child's record must be confirmed before stage two begins."""
        proc = self._make_proc()
        order: list[str] = []

        def _fake_read(run_id: str) -> MagicMock:
            order.append("read_run_record")
            return self._record_for(proc, 9420)

        def _fake_wait_for_start(*args: Any, **kwargs: Any) -> bg_runner.StartProbe:
            order.append("wait_for_workflow_start")
            return bg_runner.StartProbe.STARTED

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.fleet.records.read_run_record", side_effect=_fake_read),
            patch.object(bg_runner, "_wait_for_workflow_start", side_effect=_fake_wait_for_start),
        ):
            result = bg_runner._finalize_background_launch(
                proc, 9420, "deadbeef", tmp_path / "err.log", launched_at=self._launched_at()
            )

        assert result.workflow_started is True
        assert order == ["read_run_record", "wait_for_workflow_start"]

    def test_timed_out_returns_false_and_leaves_record(self, tmp_path: Path) -> None:
        proc = self._make_proc()

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=self._record_for(proc, 9421),
            ),
            patch.object(bg_runner, "_remove_dead_child_record") as mock_remove,
            patch.object(
                bg_runner, "_wait_for_workflow_start", return_value=bg_runner.StartProbe.TIMED_OUT
            ),
        ):
            result = bg_runner._finalize_background_launch(
                proc, 9421, "deadbeef", tmp_path / "err.log", launched_at=self._launched_at()
            )

        assert result.workflow_started is False
        mock_remove.assert_not_called()

    def test_start_timeout_zero_skips_stage_two(self, tmp_path: Path) -> None:
        proc = self._make_proc()

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=self._record_for(proc, 9422),
            ),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
            patch.object(bg_runner, "_wait_for_workflow_start") as mock_wait,
        ):
            result = bg_runner._finalize_background_launch(
                proc, 9422, "deadbeef", tmp_path / "err.log", launched_at=self._launched_at()
            )

        assert result.workflow_started is True
        mock_wait.assert_not_called()

    def test_child_exited_zero_after_record_returns_true(self, tmp_path: Path) -> None:
        """A workflow that completes within the stage-two window is a success."""
        proc = self._make_proc()

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=self._record_for(proc, 9423),
            ),
            patch.object(bg_runner, "_remove_dead_child_record") as mock_remove,
            patch.object(
                bg_runner,
                "_wait_for_workflow_start",
                return_value=bg_runner.StartProbe.CHILD_EXITED,
            ),
        ):
            # Alive for the record poll's liveness check, exited cleanly by
            # the time stage two reports CHILD_EXITED.
            _poll = iter([None])
            proc.poll.side_effect = lambda: next(_poll, 0)
            result = bg_runner._finalize_background_launch(
                proc, 9423, "deadbeef", tmp_path / "err.log", launched_at=self._launched_at()
            )

        assert result.workflow_started is True
        mock_remove.assert_not_called()

    def test_child_exited_nonzero_after_record_removes_it_and_raises(self, tmp_path: Path) -> None:
        proc = self._make_proc(pid=111)
        stderr_log = tmp_path / "err.log"
        stderr_log.write_text("boom: traceback here\n")

        # A record pid distinct from ``proc.pid`` (confirmed via freshness,
        # not pid equality) so the assertion below actually pins "removes
        # the *confirmed* pid" rather than passing merely because the two
        # happen to be equal (review recommendation).
        fresh_started_at = (self._launched_at() + timedelta(seconds=1)).isoformat()
        record = MagicMock(pid=222, mode="bg", port=9424, started_at=fresh_started_at)

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.fleet.records.read_run_record", return_value=record),
            patch.object(bg_runner, "_remove_dead_child_record") as mock_remove,
            patch("conductor.cli.pid.is_process_alive", return_value=False),
            patch.object(
                bg_runner,
                "_wait_for_workflow_start",
                return_value=bg_runner.StartProbe.CHILD_EXITED,
            ),
        ):
            _poll = iter([None, None, 3, 3, 3])
            proc.poll.side_effect = lambda: next(_poll, 3)
            with pytest.raises(RuntimeError, match="exited before the workflow started"):
                bg_runner._finalize_background_launch(
                    proc, 9424, "deadbeef", stderr_log, launched_at=self._launched_at()
                )

        mock_remove.assert_called_once_with("deadbeef", 222)

    def test_port_conflict_removes_confirmed_pid_record_when_dead(self, tmp_path: Path) -> None:
        proc = self._make_proc(pid=111)

        # A record pid distinct from ``proc.pid`` (confirmed via freshness,
        # not pid equality) -- #447 keys cleanup on the *confirmed* pid,
        # the one actually running the workflow under a trampoline, rather
        # than the deliberately-preserved ``proc.pid`` used before #447
        # (when termination reached only ``proc.pid`` and could not
        # confirm anything about the confirmed pid at all).
        fresh_started_at = (self._launched_at() + timedelta(seconds=1)).isoformat()
        record = MagicMock(pid=222, mode="bg", port=9425, started_at=fresh_started_at)

        def _fake_wait_for_start(
            *args: Any, last_seen_info: dict[str, Any] | None = None, **kwargs: Any
        ) -> bg_runner.StartProbe:
            # Mirrors what the real function does before returning
            # PORT_CONFLICT: populate the caller's ``last_seen_info`` with
            # the foreign payload so the caller can name the real PID.
            if last_seen_info is not None:
                last_seen_info.update({"pid": 999})
            return bg_runner.StartProbe.PORT_CONFLICT

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.fleet.records.read_run_record", return_value=record),
            patch.object(bg_runner, "_remove_dead_child_record") as mock_remove,
            patch.object(
                bg_runner,
                "_terminate_child",
                return_value=bg_runner._TerminationOutcome(confirmed=True, surviving_pids=()),
            ) as mock_terminate,
            patch.object(
                bg_runner,
                "_wait_for_workflow_start",
                side_effect=_fake_wait_for_start,
            ),
            pytest.raises(RuntimeError, match="already in use") as exc_info,
        ):
            bg_runner._finalize_background_launch(
                proc, 9425, "deadbeef", tmp_path / "err.log", launched_at=self._launched_at()
            )

        assert "9425" in str(exc_info.value)
        assert "999" in str(exc_info.value)
        assert "--web-port" in str(exc_info.value)
        mock_terminate.assert_called_once_with(proc, confirmed_child_pid=222)
        mock_remove.assert_called_once_with("deadbeef", 222)

    def test_port_conflict_preserves_record_when_confirmed_pid_survives(
        self, tmp_path: Path
    ) -> None:
        """A survivor keeps its run record -- ``conductor stop``'s only handle on it."""
        proc = self._make_proc(pid=111)
        fresh_started_at = (self._launched_at() + timedelta(seconds=1)).isoformat()
        record = MagicMock(pid=222, mode="bg", port=9440, started_at=fresh_started_at)

        def _fake_wait_for_start(
            *args: Any, last_seen_info: dict[str, Any] | None = None, **kwargs: Any
        ) -> bg_runner.StartProbe:
            if last_seen_info is not None:
                last_seen_info.update({"pid": 999})
            return bg_runner.StartProbe.PORT_CONFLICT

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.fleet.records.read_run_record", return_value=record),
            patch.object(bg_runner, "_remove_dead_child_record") as mock_remove,
            patch.object(
                bg_runner,
                "_terminate_child",
                return_value=bg_runner._TerminationOutcome(confirmed=False, surviving_pids=(222,)),
            ),
            patch.object(
                bg_runner,
                "_wait_for_workflow_start",
                side_effect=_fake_wait_for_start,
            ),
            pytest.raises(RuntimeError, match="already in use") as exc_info,
        ):
            bg_runner._finalize_background_launch(
                proc, 9440, "deadbeef", tmp_path / "err.log", launched_at=self._launched_at()
            )

        assert "could not confirm termination" in str(exc_info.value).lower()
        assert "222" in str(exc_info.value)
        mock_remove.assert_not_called()

    def test_stderr_tail_included_in_child_exited_error(self, tmp_path: Path) -> None:
        proc = self._make_proc()
        stderr_log = tmp_path / "err.log"
        stderr_log.write_text("Traceback (most recent call last):\nConfigurationError: bad yaml\n")

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                return_value=self._record_for(proc, 9426),
            ),
            patch.object(bg_runner, "_remove_dead_child_record"),
            patch.object(
                bg_runner,
                "_wait_for_workflow_start",
                return_value=bg_runner.StartProbe.CHILD_EXITED,
            ),
        ):
            _poll = iter([None, None, 1, 1, 1])
            proc.poll.side_effect = lambda: next(_poll, 1)
            with pytest.raises(RuntimeError) as exc_info:
                bg_runner._finalize_background_launch(
                    proc, 9426, "deadbeef", stderr_log, launched_at=self._launched_at()
                )

        assert "ConfigurationError: bad yaml" in str(exc_info.value)

    def test_early_exit_zero_returns_true_without_polling_for_a_record(
        self, tmp_path: Path
    ) -> None:
        """A sub-second clean exit is a success; the child removed its own record."""
        proc = self._make_proc()
        proc.poll.return_value = 0

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=False),
            patch("conductor.fleet.records.read_run_record") as mock_read,
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            result = bg_runner._finalize_background_launch(
                proc, 9427, "deadbeef", tmp_path / "err.log", launched_at=self._launched_at()
            )

        assert result.workflow_started is True
        mock_read.assert_not_called()


class TestSpawnBgChildPropagatesWorkflowStarted:
    """``_spawn_bg_child`` threads the stage-two result into ``BackgroundLaunch``."""

    def test_workflow_started_false_reaches_background_launch(self, tmp_path: Path) -> None:
        wf_path = _write_workflow(tmp_path)

        def _fake_popen(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: Any
        ) -> MagicMock:
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None
            return proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_fake_popen),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                side_effect=lambda _run_id: MagicMock(pid=1, mode="bg", port=9430),
            ),
            patch.object(
                bg_runner, "_wait_for_workflow_start", return_value=bg_runner.StartProbe.TIMED_OUT
            ),
        ):
            launch = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9430,
            )

        assert launch.workflow_started is False

    def test_workflow_started_true_by_default(self, tmp_path: Path) -> None:
        wf_path = _write_workflow(tmp_path)

        def _fake_popen(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: Any
        ) -> MagicMock:
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None
            return proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_fake_popen),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                side_effect=lambda _run_id: MagicMock(pid=1, mode="bg", port=9431),
            ),
            patch.object(
                bg_runner, "_wait_for_workflow_start", return_value=bg_runner.StartProbe.STARTED
            ),
        ):
            launch = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9431,
            )

        assert launch.workflow_started is True


class TestSpawnBgChildPropagatesStillRunning:
    """``_spawn_bg_child`` re-checks ``proc.poll()`` for ``BackgroundLaunch.still_running``.

    ``_finalize_background_launch`` returns bare ``True`` for both a
    genuinely-still-running child and one that already exited cleanly (issue
    #410) — see its docstring. ``still_running`` is what lets a caller (e.g.
    ``cli/app.py``) tell those two apart without printing a live dashboard
    URL for an already-exited process.
    """

    def test_still_running_true_when_child_alive(self, tmp_path: Path) -> None:
        wf_path = _write_workflow(tmp_path)

        def _fake_popen(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: Any
        ) -> MagicMock:
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None
            return proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_fake_popen),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                side_effect=lambda _run_id: MagicMock(pid=1, mode="bg", port=9432),
            ),
            patch.object(
                bg_runner, "_wait_for_workflow_start", return_value=bg_runner.StartProbe.STARTED
            ),
        ):
            launch = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9432,
            )

        assert launch.still_running is True
        assert launch.workflow_started is True

    def test_still_running_false_when_child_exited_during_stage_two(self, tmp_path: Path) -> None:
        """A workflow that completes during stage-two must not look 'running'."""
        wf_path = _write_workflow(tmp_path)
        fake_proc = MagicMock()
        fake_proc.pid = 1

        def _fake_popen(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: Any
        ) -> MagicMock:
            return fake_proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_fake_popen),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                side_effect=lambda _run_id: MagicMock(pid=1, mode="bg", port=9433),
            ),
            patch.object(
                bg_runner,
                "_wait_for_workflow_start",
                return_value=bg_runner.StartProbe.CHILD_EXITED,
            ),
        ):
            # The child is alive when write_pid_file runs, then exits cleanly
            # by the time _finalize_background_launch re-checks proc.poll().
            fake_proc.poll.return_value = 0
            launch = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9433,
            )

        assert launch.still_running is False
        # A clean exit within the wait window is still a success.
        assert launch.workflow_started is True

    def test_still_running_false_on_early_clean_exit(self, tmp_path: Path) -> None:
        """A sub-second workflow that exits before the port ever opens."""
        wf_path = _write_workflow(tmp_path)
        fake_proc = MagicMock()
        fake_proc.pid = 1
        fake_proc.poll.return_value = 0

        def _fake_popen(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: Any
        ) -> MagicMock:
            return fake_proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_fake_popen),
            patch.object(bg_runner, "_wait_for_server", return_value=False),
            patch("conductor.fleet.records.read_run_record") as mock_read,
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
        ):
            launch = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9434,
            )

        assert launch.still_running is False
        assert launch.workflow_started is True
        mock_read.assert_not_called()

    def test_crash_raced_between_started_and_final_repoll_raises(self, tmp_path: Path) -> None:
        """A crash right after STARTED but before the final re-poll must not

        be reported as a clean "Workflow completed" success.

        ``_wait_for_workflow_start`` returning ``StartProbe.STARTED`` makes
        ``_finalize_background_launch`` return ``True`` without re-checking
        the exit code (the child was confirmed alive moments before, by
        ``_wait_for_workflow_start``'s own last ``proc.poll()``). If the
        child then crashes with a non-zero code before ``_spawn_bg_child``'s
        own final re-poll, that must surface as a failure rather than
        silently becoming ``still_running=False`` with no distinction from
        a genuine clean completion (issue #410).
        """
        wf_path = _write_workflow(tmp_path)
        fake_proc = MagicMock()
        fake_proc.pid = 1
        # Alive while the run-record gate and _wait_for_workflow_start run;
        # crashed (non-zero) by the time _spawn_bg_child does its final
        # re-poll. Sequenced rather than a flat return value so the crash
        # lands in the window this test is about, not in the gate before it.
        _poll = iter([None])
        fake_proc.poll.side_effect = lambda: next(_poll, 1)

        def _fake_popen(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: Any
        ) -> MagicMock:
            return fake_proc

        with (
            patch.object(bg_runner, "_spawn_detached", side_effect=_fake_popen),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch(
                "conductor.fleet.records.read_run_record",
                side_effect=lambda _run_id: MagicMock(pid=1, mode="bg", port=9435),
            ),
            patch.object(
                bg_runner, "_wait_for_workflow_start", return_value=bg_runner.StartProbe.STARTED
            ),
            pytest.raises(RuntimeError, match="exited unexpectedly"),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9435,
            )
