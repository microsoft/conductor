"""Tests for PID file utilities (``conductor.cli.pid``).

Covers:
- ``write_pid_file`` / ``read_pid_files`` / ``remove_pid_file``
- ``remove_pid_file_for_current_process``
- Stale PID cleanup (process no longer alive)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.cli.pid import (
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
