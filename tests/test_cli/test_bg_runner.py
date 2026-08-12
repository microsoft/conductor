"""Tests for ``conductor.cli.bg_runner``.

Covers two issues that landed together:

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

Neither group of tests actually spawns a child process. ``subprocess.Popen``
is patched in every test so nothing leaks into the test runner.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import time
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

    def test_windows_happy_path_includes_breakaway(self) -> None:
        captured: dict[str, Any] = {}

        def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return MagicMock(pid=4321)

        with (
            patch.object(bg_runner.sys, "platform", "win32"),
            patch.object(bg_runner.subprocess, "Popen", side_effect=_fake_popen) as mock_popen,
        ):
            proc = bg_runner._spawn_detached(["python", "-c", "pass"], {"X": "1"})

        assert proc.pid == 4321
        mock_popen.assert_called_once()
        flags = captured["kwargs"]["creationflags"]
        assert flags == (bg_runner._CREATE_NEW_PROCESS_GROUP | bg_runner._CREATE_BREAKAWAY_FROM_JOB)

    def test_windows_breakaway_denied_falls_back_and_warns(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When the parent's job forbids breakaway, retry without the flag.

        - First Popen call requests breakaway and raises OSError(winerror=5).
        - Second Popen call must NOT include CREATE_BREAKAWAY_FROM_JOB.
        - A user-visible warning must be written to stderr.
        """
        success_proc = MagicMock(pid=999)
        popen_kwargs: list[dict[str, Any]] = []

        def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
            popen_kwargs.append(kwargs)
            if len(popen_kwargs) == 1:
                raise _make_breakaway_denied_error()
            return success_proc

        with (
            patch.object(bg_runner.sys, "platform", "win32"),
            patch.object(bg_runner.subprocess, "Popen", side_effect=_fake_popen) as mock_popen,
        ):
            proc = bg_runner._spawn_detached(["python", "-c", "pass"], {"X": "1"})

        assert proc is success_proc
        assert mock_popen.call_count == 2

        # First attempt requested breakaway.
        first = popen_kwargs[0]
        assert first["creationflags"] & bg_runner._CREATE_BREAKAWAY_FROM_JOB
        # Second attempt is plain CREATE_NEW_PROCESS_GROUP, no breakaway.
        second = popen_kwargs[1]
        assert second["creationflags"] == bg_runner._CREATE_NEW_PROCESS_GROUP
        assert not (second["creationflags"] & bg_runner._CREATE_BREAKAWAY_FROM_JOB)
        # Stdio + env preserved across the retry.
        assert second["stdout"] is subprocess.DEVNULL
        assert second["stderr"] is subprocess.DEVNULL
        assert second["stdin"] is subprocess.DEVNULL
        assert second["env"] == {"X": "1"}

        captured = capsys.readouterr()
        assert "warning" in captured.err.lower()
        assert "breakaway" in captured.err.lower()
        # Must not pollute stdout (caller prints "Dashboard: ..." there).
        assert captured.out == ""

    def test_windows_non_breakaway_oserror_propagates(self) -> None:
        """OSErrors other than ERROR_ACCESS_DENIED must propagate without retry."""
        not_found = _make_file_not_found_error()
        with (
            patch.object(bg_runner.sys, "platform", "win32"),
            patch.object(bg_runner.subprocess, "Popen", side_effect=not_found) as mock_popen,
            pytest.raises(FileNotFoundError),
        ):
            bg_runner._spawn_detached(["nonexistent.exe"], {})

        # Exactly one attempt — no fallback retry.
        mock_popen.assert_called_once()

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
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
            patch("conductor.cli.pid.write_pid_file"),
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
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
            patch("conductor.cli.pid.write_pid_file"),
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

        def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
            proc = MagicMock()
            proc.pid = 1234
            proc.poll.return_value = None
            return proc

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch("conductor.cli.bg_runner._resolve_start_timeout", return_value=0.0),
            patch("conductor.cli.pid.write_pid_file"),
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

        def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None
            return proc

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch("conductor.cli.bg_runner._resolve_start_timeout", return_value=0.0),
            patch("conductor.cli.pid.write_pid_file"),
        ):
            bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9301,
            )

        assert captured["stdin"] is subprocess.DEVNULL  # no interactive input
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

        def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
            captured["stdout"] = kwargs["stdout"]
            captured["stderr"] = kwargs["stderr"]
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None
            return proc

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch("conductor.cli.bg_runner._resolve_start_timeout", return_value=0.0),
            patch("conductor.cli.pid.write_pid_file"),
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

        def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
            captured["stdout"] = kwargs["stdout"]
            captured["stderr"] = kwargs["stderr"]
            proc = MagicMock()
            proc.pid = 1
            # Simulate child exited immediately — _finalize raises RuntimeError.
            proc.poll.return_value = 7
            return proc

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=False),
            patch("conductor.cli.pid.write_pid_file"),
            pytest.raises(RuntimeError, match="exited immediately with code 7"),
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

        def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
            captured["env"] = kwargs["env"]
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None
            return proc

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch("conductor.cli.bg_runner._resolve_start_timeout", return_value=0.0),
            patch("conductor.cli.pid.write_pid_file") as mock_write,
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

        _, kwargs = mock_write.call_args
        assert kwargs["run_id"] == env["CONDUCTOR_RUN_ID"]

    def test_pid_file_records_run_id_and_capture_logs(self, tmp_path: Path) -> None:
        """Regression guard for issue #404: PID file must not ship dead fields.

        Before this fix, ``_finalize_background_launch`` called
        ``write_pid_file(proc.pid, web_port, pid_workflow_ref)`` with no
        ``run_id``/log kwargs, so every bg-launched PID file recorded an
        empty ``run_id`` and a nonexistent ``log_file``.
        """
        from conductor.cli import bg_runner

        wf_path = _write_workflow(tmp_path)

        def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
            proc = MagicMock()
            proc.pid = 4321
            proc.poll.return_value = None
            return proc

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch("conductor.cli.bg_runner._resolve_start_timeout", return_value=0.0),
            patch("conductor.cli.pid.write_pid_file") as mock_write,
        ):
            launch = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={},
                web_port=9305,
            )

        mock_write.assert_called_once()
        args, kwargs = mock_write.call_args
        assert args == (4321, 9305, wf_path)
        assert kwargs["run_id"] == launch.run_id
        assert kwargs["run_id"]  # non-empty
        assert kwargs["stderr_log"] == str(launch.stderr_log)
        assert kwargs["stdout_log"] == str(launch.stdout_log)

    def test_early_exit_error_mentions_stderr_log_path(self, tmp_path: Path) -> None:
        """RuntimeError raised on early child exit includes the stderr log path."""
        from conductor.cli import bg_runner

        wf_path = _write_workflow(tmp_path)

        def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = 42  # immediate exit code 42
            return proc

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=False),
            patch("conductor.cli.pid.write_pid_file"),
            pytest.raises(RuntimeError) as exc_info,
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

        def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None  # still running
            return proc

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=False),
            patch("conductor.cli.bg_runner._terminate_child"),
            patch("conductor.cli.pid.write_pid_file"),
            pytest.raises(RuntimeError) as exc_info,
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


class TestWaitForWorkflowStart:
    """``_wait_for_workflow_start`` outcome coverage via ``_probe_workflow_info``."""

    def test_started_when_started_at_key_present(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 123

        with patch.object(
            bg_runner, "_probe_workflow_info", return_value={"pid": 123, "started_at": 0}
        ):
            result = bg_runner._wait_for_workflow_start(9410, proc, timeout=5.0)

        assert result is bg_runner.StartProbe.STARTED

    def test_started_at_zero_still_counts_as_started(self) -> None:
        """``started_at`` can legitimately be falsy 0 — key presence is what matters."""
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 123

        with patch.object(
            bg_runner, "_probe_workflow_info", return_value={"pid": 123, "started_at": 0}
        ):
            result = bg_runner._wait_for_workflow_start(9411, proc, timeout=5.0)

        assert result is bg_runner.StartProbe.STARTED

    def test_child_exited_takes_priority_over_probe(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.pid = 123

        with patch.object(bg_runner, "_probe_workflow_info") as mock_probe:
            result = bg_runner._wait_for_workflow_start(9412, proc, timeout=5.0)

        assert result is bg_runner.StartProbe.CHILD_EXITED
        mock_probe.assert_not_called()

    def test_port_conflict_when_reported_pid_differs(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 123

        with patch.object(bg_runner, "_probe_workflow_info", return_value={"pid": 999}):
            result = bg_runner._wait_for_workflow_start(9413, proc, timeout=5.0)

        assert result is bg_runner.StartProbe.PORT_CONFLICT

    def test_timed_out_when_never_started(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 123

        with patch.object(bg_runner, "_probe_workflow_info", return_value=None):
            result = bg_runner._wait_for_workflow_start(9414, proc, timeout=0.5)

        assert result is bg_runner.StartProbe.TIMED_OUT

    def test_none_probe_result_keeps_waiting(self) -> None:
        """A probe returning ``None`` (e.g. connection refused) doesn't end the wait early."""
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 123

        responses = [None, None, {"pid": 123, "started_at": 1.0}]

        with patch.object(bg_runner, "_probe_workflow_info", side_effect=responses):
            result = bg_runner._wait_for_workflow_start(9415, proc, timeout=5.0)

        assert result is bg_runner.StartProbe.STARTED


class TestFinalizeBackgroundLaunchStageTwo:
    """``_finalize_background_launch``'s post-PID-write stage-two behavior."""

    def _make_proc(self, pid: int = 111) -> MagicMock:
        proc = MagicMock()
        proc.pid = pid
        proc.poll.return_value = None
        return proc

    def test_pid_file_written_before_stage_two_wait(self, tmp_path: Path) -> None:
        """The PID file must exist even while the stage-two wait is still pending."""
        wf_path = _write_workflow(tmp_path)
        proc = self._make_proc()
        order: list[str] = []

        def _fake_write_pid_file(*args: Any, **kwargs: Any) -> Path:
            order.append("write_pid_file")
            return tmp_path / "fake.pid"

        def _fake_wait_for_start(*args: Any, **kwargs: Any) -> bg_runner.StartProbe:
            order.append("wait_for_workflow_start")
            return bg_runner.StartProbe.STARTED

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file", side_effect=_fake_write_pid_file),
            patch.object(bg_runner, "_wait_for_workflow_start", side_effect=_fake_wait_for_start),
        ):
            result = bg_runner._finalize_background_launch(
                proc,
                9420,
                wf_path,
                tmp_path / "err.log",
                run_id="deadbeef",
                stdout_log=tmp_path / "out.log",
            )

        assert result is True
        assert order == ["write_pid_file", "wait_for_workflow_start"]

    def test_timed_out_returns_false_and_leaves_pid_file(self, tmp_path: Path) -> None:
        wf_path = _write_workflow(tmp_path)
        proc = self._make_proc()

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file", return_value=tmp_path / "fake.pid"),
            patch("conductor.cli.pid.remove_pid_file_at") as mock_remove,
            patch.object(
                bg_runner, "_wait_for_workflow_start", return_value=bg_runner.StartProbe.TIMED_OUT
            ),
        ):
            result = bg_runner._finalize_background_launch(
                proc,
                9421,
                wf_path,
                tmp_path / "err.log",
                run_id="deadbeef",
                stdout_log=tmp_path / "out.log",
            )

        assert result is False
        mock_remove.assert_not_called()

    def test_start_timeout_zero_skips_stage_two(self, tmp_path: Path) -> None:
        wf_path = _write_workflow(tmp_path)
        proc = self._make_proc()

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file", return_value=tmp_path / "fake.pid"),
            patch.object(bg_runner, "_resolve_start_timeout", return_value=0.0),
            patch.object(bg_runner, "_wait_for_workflow_start") as mock_wait,
        ):
            result = bg_runner._finalize_background_launch(
                proc,
                9422,
                wf_path,
                tmp_path / "err.log",
                run_id="deadbeef",
                stdout_log=tmp_path / "out.log",
            )

        assert result is True
        mock_wait.assert_not_called()

    def test_child_exited_zero_after_pid_write_returns_true(self, tmp_path: Path) -> None:
        """A workflow that completes within the stage-two window is a success."""
        wf_path = _write_workflow(tmp_path)
        proc = self._make_proc()

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file", return_value=tmp_path / "fake.pid"),
            patch("conductor.cli.pid.remove_pid_file_at") as mock_remove,
            patch.object(
                bg_runner,
                "_wait_for_workflow_start",
                return_value=bg_runner.StartProbe.CHILD_EXITED,
            ),
        ):
            proc.poll.return_value = 0
            result = bg_runner._finalize_background_launch(
                proc,
                9423,
                wf_path,
                tmp_path / "err.log",
                run_id="deadbeef",
                stdout_log=tmp_path / "out.log",
            )

        assert result is True
        mock_remove.assert_not_called()

    def test_child_exited_nonzero_after_pid_write_removes_pid_file_and_raises(
        self, tmp_path: Path
    ) -> None:
        wf_path = _write_workflow(tmp_path)
        proc = self._make_proc()
        stderr_log = tmp_path / "err.log"
        stderr_log.write_text("boom: traceback here\n")
        pid_path = tmp_path / "fake.pid"

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file", return_value=pid_path),
            patch("conductor.cli.pid.remove_pid_file_at") as mock_remove,
            patch.object(
                bg_runner,
                "_wait_for_workflow_start",
                return_value=bg_runner.StartProbe.CHILD_EXITED,
            ),
        ):
            proc.poll.return_value = 3
            with pytest.raises(RuntimeError, match="exited before the workflow started"):
                bg_runner._finalize_background_launch(
                    proc,
                    9424,
                    wf_path,
                    stderr_log,
                    run_id="deadbeef",
                    stdout_log=tmp_path / "out.log",
                )

        mock_remove.assert_called_once_with(pid_path, proc.pid)

    def test_port_conflict_removes_pid_file_and_raises_naming_port(self, tmp_path: Path) -> None:
        wf_path = _write_workflow(tmp_path)
        proc = self._make_proc(pid=111)
        pid_path = tmp_path / "fake.pid"

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file", return_value=pid_path),
            patch("conductor.cli.pid.remove_pid_file_at") as mock_remove,
            patch.object(bg_runner, "_terminate_child") as mock_terminate,
            patch.object(
                bg_runner,
                "_wait_for_workflow_start",
                return_value=bg_runner.StartProbe.PORT_CONFLICT,
            ),
            patch.object(bg_runner, "_probe_workflow_info", return_value={"pid": 999}),
            pytest.raises(RuntimeError, match="already in use") as exc_info,
        ):
            bg_runner._finalize_background_launch(
                proc,
                9425,
                wf_path,
                tmp_path / "err.log",
                run_id="deadbeef",
                stdout_log=tmp_path / "out.log",
            )

        assert "9425" in str(exc_info.value)
        assert "999" in str(exc_info.value)
        assert "--web-port" in str(exc_info.value)
        mock_terminate.assert_called_once_with(proc)
        mock_remove.assert_called_once_with(pid_path, proc.pid)

    def test_stderr_tail_included_in_child_exited_error(self, tmp_path: Path) -> None:
        wf_path = _write_workflow(tmp_path)
        proc = self._make_proc()
        stderr_log = tmp_path / "err.log"
        stderr_log.write_text("Traceback (most recent call last):\nConfigurationError: bad yaml\n")

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file", return_value=tmp_path / "fake.pid"),
            patch("conductor.cli.pid.remove_pid_file_at"),
            patch.object(
                bg_runner,
                "_wait_for_workflow_start",
                return_value=bg_runner.StartProbe.CHILD_EXITED,
            ),
        ):
            proc.poll.return_value = 1
            with pytest.raises(RuntimeError) as exc_info:
                bg_runner._finalize_background_launch(
                    proc,
                    9426,
                    wf_path,
                    stderr_log,
                    run_id="deadbeef",
                    stdout_log=tmp_path / "out.log",
                )

        assert "ConfigurationError: bad yaml" in str(exc_info.value)

    def test_early_exit_zero_returns_true_without_writing_pid_file(self, tmp_path: Path) -> None:
        """A sub-second clean exit is a success and has no PID file to write."""
        wf_path = _write_workflow(tmp_path)
        proc = self._make_proc()
        proc.poll.return_value = 0

        with (
            patch.object(bg_runner, "_wait_for_server", return_value=False),
            patch("conductor.cli.pid.write_pid_file") as mock_write,
        ):
            result = bg_runner._finalize_background_launch(
                proc,
                9427,
                wf_path,
                tmp_path / "err.log",
                run_id="deadbeef",
                stdout_log=tmp_path / "out.log",
            )

        assert result is True
        mock_write.assert_not_called()


class TestSpawnBgChildPropagatesWorkflowStarted:
    """``_spawn_bg_child`` threads the stage-two result into ``BackgroundLaunch``."""

    def test_workflow_started_false_reaches_background_launch(self, tmp_path: Path) -> None:
        wf_path = _write_workflow(tmp_path)

        def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None
            return proc

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", side_effect=_fake_popen),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file"),
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

        def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None
            return proc

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", side_effect=_fake_popen),
            patch.object(bg_runner, "_wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file"),
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
