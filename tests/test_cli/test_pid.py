"""Tests for PID file utilities (``conductor.cli.pid``).

Covers:
- ``write_pid_file`` / ``read_pid_files`` / ``remove_pid_file``
- ``remove_pid_file_for_current_process``
- Stale PID cleanup (process no longer alive)
- ``_is_process_alive`` platform dispatch (POSIX + Windows)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conductor.cli.pid import (
    _is_process_alive,
    _is_process_alive_posix,
    _is_process_alive_windows,
    read_pid_files,
    remove_pid_file,
    remove_pid_file_for_current_process,
    write_pid_file,
)


@pytest.fixture()
def pid_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override ``pid_dir()`` to use a temporary directory."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: runs_dir)
    return runs_dir


class TestWritePidFile:
    """Tests for ``write_pid_file``."""

    def test_creates_pid_file(self, pid_tmpdir: Path) -> None:
        path = write_pid_file(12345, 8080, "/tmp/workflow.yaml")
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["pid"] == 12345
        assert data["port"] == 8080
        assert data["workflow"] == "/tmp/workflow.yaml"
        assert "started_at" in data

    def test_filename_uses_workflow_stem_and_port(self, pid_tmpdir: Path) -> None:
        path = write_pid_file(100, 9090, "/some/path/my-workflow.yaml")
        assert path.name == "my-workflow-9090.pid"

    def test_overwrites_existing_pid_file(self, pid_tmpdir: Path) -> None:
        write_pid_file(100, 8080, "/tmp/wf.yaml")
        write_pid_file(200, 8080, "/tmp/wf.yaml")
        data = json.loads((pid_tmpdir / "wf-8080.pid").read_text())
        assert data["pid"] == 200


class TestReadPidFiles:
    """Tests for ``read_pid_files``."""

    def test_returns_alive_processes(self, pid_tmpdir: Path) -> None:
        write_pid_file(os.getpid(), 8080, "/tmp/wf.yaml")
        results = read_pid_files()
        assert len(results) == 1
        assert results[0]["pid"] == os.getpid()
        assert results[0]["port"] == 8080

    def test_cleans_up_stale_pid_files(self, pid_tmpdir: Path) -> None:
        # Write a PID file for a non-existent process
        write_pid_file(99999999, 8080, "/tmp/wf.yaml")

        with patch("conductor.cli.pid._is_process_alive", return_value=False):
            results = read_pid_files()

        assert len(results) == 0
        # Verify the stale file was removed
        assert list(pid_tmpdir.glob("*.pid")) == []

    def test_removes_corrupted_files(self, pid_tmpdir: Path) -> None:
        (pid_tmpdir / "bad.pid").write_text("not json{{{")
        results = read_pid_files()
        assert len(results) == 0
        assert not (pid_tmpdir / "bad.pid").exists()

    def test_returns_multiple_processes(self, pid_tmpdir: Path) -> None:
        current = os.getpid()
        write_pid_file(current, 8080, "/tmp/wf1.yaml")
        write_pid_file(current, 9090, "/tmp/wf2.yaml")
        results = read_pid_files()
        assert len(results) == 2
        ports = {r["port"] for r in results}
        assert ports == {8080, 9090}


class TestRemovePidFile:
    """Tests for ``remove_pid_file``."""

    def test_removes_by_port(self, pid_tmpdir: Path) -> None:
        write_pid_file(12345, 8080, "/tmp/wf.yaml")
        assert remove_pid_file(8080) is True
        assert list(pid_tmpdir.glob("*.pid")) == []

    def test_returns_false_for_unknown_port(self, pid_tmpdir: Path) -> None:
        write_pid_file(12345, 8080, "/tmp/wf.yaml")
        assert remove_pid_file(9999) is False

    def test_returns_false_when_no_pid_files(self, pid_tmpdir: Path) -> None:
        assert remove_pid_file(8080) is False


class TestRemovePidFileForCurrentProcess:
    """Tests for ``remove_pid_file_for_current_process``."""

    def test_removes_own_pid_file(self, pid_tmpdir: Path) -> None:
        write_pid_file(os.getpid(), 8080, "/tmp/wf.yaml")
        assert remove_pid_file_for_current_process() is True
        assert list(pid_tmpdir.glob("*.pid")) == []

    def test_returns_false_when_no_match(self, pid_tmpdir: Path) -> None:
        write_pid_file(99999999, 8080, "/tmp/wf.yaml")
        assert remove_pid_file_for_current_process() is False

    def test_leaves_other_pid_files(self, pid_tmpdir: Path) -> None:
        write_pid_file(os.getpid(), 8080, "/tmp/wf.yaml")
        write_pid_file(99999999, 9090, "/tmp/wf2.yaml")
        remove_pid_file_for_current_process()
        remaining = list(pid_tmpdir.glob("*.pid"))
        assert len(remaining) == 1
        assert "9090" in remaining[0].name


class TestIsProcessAlivePosix:
    """The POSIX path is exercised on every platform — ``os.kill`` is mocked
    rather than relying on real process state."""

    def test_returns_true_when_alive(self) -> None:
        with patch("conductor.cli.pid.os.kill", return_value=None):
            assert _is_process_alive_posix(12345) is True

    def test_returns_false_when_process_lookup_error(self) -> None:
        with patch("conductor.cli.pid.os.kill", side_effect=ProcessLookupError):
            assert _is_process_alive_posix(99999999) is False

    def test_returns_true_when_permission_error(self) -> None:
        with patch("conductor.cli.pid.os.kill", side_effect=PermissionError):
            assert _is_process_alive_posix(1) is True

    def test_unexpected_oserror_does_not_crash(self) -> None:
        # Regression for issue #166: any unexpected OSError from ``os.kill``
        # (the original Windows ``WinError 11`` trigger, or any other novel
        # failure) must be absorbed rather than propagated, so that
        # ``conductor stop`` doesn't crash and live workflow PID files aren't
        # silently dropped.
        with patch(
            "conductor.cli.pid.os.kill",
            side_effect=OSError(
                11, "An attempt was made to load a program with an incorrect format"
            ),
        ):
            assert _is_process_alive_posix(12345) is True


class TestIsProcessAliveDispatch:
    """The top-level ``_is_process_alive`` selects the right implementation
    based on ``sys.platform``."""

    def test_dispatches_to_posix_on_non_windows(self) -> None:
        with (
            patch("conductor.cli.pid.sys.platform", "linux"),
            patch("conductor.cli.pid._is_process_alive_posix", return_value=True) as posix,
            patch("conductor.cli.pid._is_process_alive_windows") as win,
        ):
            assert _is_process_alive(42) is True
            posix.assert_called_once_with(42)
            win.assert_not_called()

    def test_dispatches_to_windows_on_win32(self) -> None:
        with (
            patch("conductor.cli.pid.sys.platform", "win32"),
            patch("conductor.cli.pid._is_process_alive_windows", return_value=False) as win,
            patch("conductor.cli.pid._is_process_alive_posix") as posix,
        ):
            assert _is_process_alive(42) is False
            win.assert_called_once_with(42)
            posix.assert_not_called()


def _make_kernel32_mock(
    *,
    open_handle: int = 0xDEADBEEF,
    get_exit_code_ok: bool = True,
    exit_code: int = 259,
) -> MagicMock:
    """Build a ``MagicMock`` standing in for the cached ``kernel32`` wrapper.

    ``GetExitCodeProcess`` is configured to write ``exit_code`` into the
    pointer it's given (mirroring the real Win32 behaviour) and return
    ``get_exit_code_ok``.
    """
    k = MagicMock(name="kernel32")
    k.OpenProcess.return_value = open_handle

    def _set_exit_code(handle, dword_ptr):  # type: ignore[no-untyped-def]
        dword_ptr._obj.value = exit_code
        return get_exit_code_ok

    k.GetExitCodeProcess.side_effect = _set_exit_code
    k.CloseHandle.return_value = True
    return k


class TestIsProcessAliveWindowsMocked:
    """Cross-platform unit tests for ``_is_process_alive_windows``.

    Mocks the cached ``_kernel32`` wrapper and ``ctypes.get_last_error`` so
    every branch of the Windows implementation is covered on POSIX runners,
    not only on Windows CI.  ``ctypes.get_last_error`` doesn't exist on
    non-Windows so it is patched with ``create=True``.
    """

    def test_running_process_returns_true(self) -> None:
        k = _make_kernel32_mock(open_handle=0x1000, exit_code=259)
        with patch("conductor.cli.pid._kernel32", k):
            assert _is_process_alive_windows(42) is True
        k.CloseHandle.assert_called_once_with(0x1000)

    def test_exited_process_returns_false(self) -> None:
        # The primary production case for stale-PID cleanup: GetExitCodeProcess
        # succeeds and reports a non-STILL_ACTIVE exit code.
        k = _make_kernel32_mock(open_handle=0x1000, exit_code=0)
        with patch("conductor.cli.pid._kernel32", k):
            assert _is_process_alive_windows(42) is False
        k.CloseHandle.assert_called_once_with(0x1000)

    def test_exited_with_nonzero_returns_false(self) -> None:
        # Any exit code other than STILL_ACTIVE (259) means the process exited.
        k = _make_kernel32_mock(open_handle=0x1000, exit_code=1)
        with patch("conductor.cli.pid._kernel32", k):
            assert _is_process_alive_windows(42) is False

    def test_open_process_access_denied_returns_true(self) -> None:
        # ERROR_ACCESS_DENIED = 5: process exists but we can't query it.
        k = _make_kernel32_mock(open_handle=0)
        with (
            patch("conductor.cli.pid._kernel32", k),
            patch("conductor.cli.pid.ctypes.get_last_error", create=True, return_value=5),
        ):
            assert _is_process_alive_windows(42) is True
        k.CloseHandle.assert_not_called()

    def test_open_process_invalid_parameter_returns_false(self) -> None:
        # ERROR_INVALID_PARAMETER = 87: no such process.
        k = _make_kernel32_mock(open_handle=0)
        with (
            patch("conductor.cli.pid._kernel32", k),
            patch("conductor.cli.pid.ctypes.get_last_error", create=True, return_value=87),
        ):
            assert _is_process_alive_windows(42) is False
        k.CloseHandle.assert_not_called()

    def test_open_process_unrecognized_error_assumes_alive(self) -> None:
        # Any other OpenProcess failure (e.g. WinError 11 from the original
        # bug, or 998 ERROR_NOACCESS) — assume alive to preserve PID file.
        k = _make_kernel32_mock(open_handle=0)
        with (
            patch("conductor.cli.pid._kernel32", k),
            patch("conductor.cli.pid.ctypes.get_last_error", create=True, return_value=11),
            patch("conductor.cli.pid.ctypes.FormatError", create=True, return_value="msg"),
        ):
            assert _is_process_alive_windows(42) is True
        k.CloseHandle.assert_not_called()

    def test_get_exit_code_failure_assumes_alive_and_closes_handle(self) -> None:
        # If GetExitCodeProcess fails after a successful OpenProcess we still
        # need to release the handle — the try/finally guarantees this.
        k = _make_kernel32_mock(open_handle=0x1000, get_exit_code_ok=False)
        with (
            patch("conductor.cli.pid._kernel32", k),
            patch("conductor.cli.pid.ctypes.get_last_error", create=True, return_value=998),
            patch("conductor.cli.pid.ctypes.FormatError", create=True, return_value="msg"),
        ):
            assert _is_process_alive_windows(42) is True
        # Critical: the handle from a successful OpenProcess must always be
        # closed, even when GetExitCodeProcess fails.
        k.CloseHandle.assert_called_once_with(0x1000)


@pytest.mark.skipif(sys.platform != "win32", reason="Real Win32 API smoke test")
class TestIsProcessAliveWindowsReal:
    """End-to-end smoke tests against the real ``kernel32.dll`` on Windows.

    These complement :class:`TestIsProcessAliveWindowsMocked` by verifying
    that the ctypes signatures and constants are correct against the actual
    Win32 API, not just our model of it.
    """

    def test_current_process_is_alive(self) -> None:
        assert _is_process_alive_windows(os.getpid()) is True

    def test_nonexistent_pid_is_not_alive(self) -> None:
        # In practice ``OpenProcess`` rejects ``0xFFFFFFFF`` with
        # ``ERROR_INVALID_PARAMETER`` (Windows PIDs are multiples of 4 and
        # this value is reserved as a pseudo-handle).  Even if a future
        # Windows release routed it differently, the fallback is "assume
        # alive" — this assertion would then fail and prompt re-evaluation.
        assert _is_process_alive_windows(0xFFFFFFFF) is False


class TestReadPidFilesDoesNotCrashOnOsError:
    """Regression test for issue #166.

    ``read_pid_files`` must not propagate an unexpected ``OSError`` from the
    process-alive probe — otherwise ``conductor stop`` becomes unusable any
    time a stale PID file is present (the original ``WinError 11`` symptom).
    """

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Patches os.kill, which the Windows path does not use.",
    )
    def test_unexpected_oserror_does_not_propagate(self, pid_tmpdir: Path) -> None:
        write_pid_file(99999999, 8080, str(pid_tmpdir / "wf.yaml"))
        with patch(
            "conductor.cli.pid.os.kill",
            side_effect=OSError(
                11, "An attempt was made to load a program with an incorrect format"
            ),
        ):
            # Should not raise. With the unexpected OSError caught and treated
            # as "assume alive", the PID file is preserved rather than
            # silently deleted.
            results = read_pid_files()

        assert len(results) == 1
        assert results[0]["port"] == 8080
        assert (pid_tmpdir / "wf-8080.pid").exists()
