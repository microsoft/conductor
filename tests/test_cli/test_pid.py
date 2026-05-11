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
from unittest.mock import patch

import pytest

from conductor.cli.pid import (
    _is_process_alive,
    _is_process_alive_posix,
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
    """Tests for ``_is_process_alive_posix``.

    These exercise the POSIX path directly so they can run on any platform —
    we mock ``os.kill`` rather than relying on real process state.
    """

    def test_returns_true_when_alive(self) -> None:
        with patch("conductor.cli.pid.os.kill", return_value=None):
            assert _is_process_alive_posix(12345) is True

    def test_returns_false_when_process_lookup_error(self) -> None:
        with patch("conductor.cli.pid.os.kill", side_effect=ProcessLookupError):
            assert _is_process_alive_posix(99999999) is False

    def test_returns_true_when_permission_error(self) -> None:
        # Process exists but signalling it is not permitted — still alive.
        with patch("conductor.cli.pid.os.kill", side_effect=PermissionError):
            assert _is_process_alive_posix(1) is True

    def test_unexpected_oserror_does_not_crash(self) -> None:
        # Regression for issue #166: a generic OSError (e.g. WinError 11 on
        # Windows or any other unexpected failure) must not propagate out and
        # crash ``conductor stop``.  We treat it as "assume alive" so the
        # corresponding PID file isn't silently dropped.
        with patch(
            "conductor.cli.pid.os.kill",
            side_effect=OSError(
                11, "An attempt was made to load a program with an incorrect format"
            ),
        ):
            assert _is_process_alive_posix(12345) is True


class TestIsProcessAliveDispatch:
    """Tests for the top-level ``_is_process_alive`` dispatcher."""

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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific implementation")
class TestIsProcessAliveWindows:
    """Tests for ``_is_process_alive_windows``.

    The implementation calls into ``kernel32.dll`` via ctypes so these tests
    only run on Windows where those APIs are actually available.
    """

    def test_current_process_is_alive(self) -> None:
        from conductor.cli.pid import _is_process_alive_windows

        assert _is_process_alive_windows(os.getpid()) is True

    def test_nonexistent_pid_is_not_alive(self) -> None:
        from conductor.cli.pid import _is_process_alive_windows

        # PID 0xFFFFFFFF (4294967295) is invalid and OpenProcess will reject
        # it with ERROR_INVALID_PARAMETER.
        assert _is_process_alive_windows(0xFFFFFFFF) is False


class TestReadPidFilesDoesNotCrashOnOsError:
    """Regression test for issue #166.

    ``read_pid_files`` must not propagate an unexpected ``OSError`` from the
    process-alive probe — otherwise ``conductor stop`` becomes unusable any
    time a stale PID file is present (e.g. ``WinError 11`` on Windows).
    """

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Patches os.kill, which the Windows path does not use.",
    )
    def test_unexpected_oserror_does_not_propagate(self, pid_tmpdir: Path) -> None:
        write_pid_file(99999999, 8080, "/tmp/wf.yaml")
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
