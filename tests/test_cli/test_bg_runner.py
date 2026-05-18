"""Tests for ``bg_runner`` helpers: detachment kwargs and detached spawn.

Covers the Windows job-breakaway fix from issue #195:

- ``_detachment_kwargs`` returns the right kwargs for POSIX vs Windows.
- ``_spawn_detached`` happy path requests breakaway on Windows.
- ``_spawn_detached`` falls back to plain ``CREATE_NEW_PROCESS_GROUP`` and
  prints a stderr warning when the parent's Windows job forbids breakaway
  (``OSError`` with ``winerror == 5``).
- Non-breakaway ``OSError`` (e.g. ``winerror == 2``, "file not found")
  propagates from the first ``Popen`` call without a retry.
- POSIX paths never retry on ``OSError``.
- Both ``launch_background`` and ``launch_background_resume`` route their
  Popen call through ``_spawn_detached`` (i.e., the Windows breakaway flag
  is set in both run and resume paths).
"""

from __future__ import annotations

import subprocess
import sys
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
            patch("conductor.cli.pid.write_pid_file"),
        ):
            url = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={"q": "hello"},
                web_port=9301,
            )

        assert url == "http://127.0.0.1:9301"
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
            patch("conductor.cli.pid.write_pid_file"),
        ):
            url = bg_runner.launch_background_resume(
                workflow_path=wf_path,
                checkpoint_path=None,
                web_port=9302,
            )

        assert url == "http://127.0.0.1:9302"
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
