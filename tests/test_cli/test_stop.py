"""Tests for ``conductor stop`` CLI command.

Covers:
- Stopping a workflow by port
- Stopping all workflows with ``--all``
- Auto-stop when exactly one workflow is running
- Listing when multiple workflows are running
- Error cases (no running workflows, invalid port)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()


@pytest.fixture()
def pid_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override ``pid_dir()`` to use a temporary directory."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: runs_dir)
    return runs_dir


def _write_pid(pid_dir: Path, pid: int, port: int, workflow: str = "/tmp/wf.yaml") -> Path:
    """Helper to write a PID file directly."""
    name = Path(workflow).stem
    filepath = pid_dir / f"{name}-{port}.pid"
    filepath.write_text(
        json.dumps(
            {"pid": pid, "port": port, "workflow": workflow, "started_at": "2026-03-03T00:00:00"}
        )
    )
    return filepath


class TestStopNoRunning:
    """Test behavior when no background workflows are running."""

    def test_no_workflows_message(self, pid_tmpdir: Path) -> None:
        result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0
        assert "No background workflows" in result.output


class TestStopByPort:
    """Test ``conductor stop --port <PORT>``."""

    def test_stops_specific_port(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_pid(pid_tmpdir, pid, 8080)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill"),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        assert result.exit_code == 0
        assert "Stopped" in result.output
        assert "8080" in result.output

    def test_error_on_unknown_port(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_pid(pid_tmpdir, pid, 8080)

        with patch("conductor.cli.pid._is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop", "--port", "9999"])

        assert result.exit_code == 1
        assert "No background workflow found on port 9999" in result.output


class TestStopAll:
    """Test ``conductor stop --all``."""

    def test_stops_all_workflows(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_pid(pid_tmpdir, pid, 8080, "/tmp/wf1.yaml")
        _write_pid(pid_tmpdir, pid, 9090, "/tmp/wf2.yaml")

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill") as mock_kill,
        ):
            result = runner.invoke(app, ["stop", "--all"])

        assert result.exit_code == 0
        assert "Stopped" in result.output
        # Both should be stopped
        assert mock_kill.call_count == 2


class TestStopAutoDetect:
    """Test ``conductor stop`` with no flags (auto-detect)."""

    def test_auto_stops_single_workflow(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_pid(pid_tmpdir, pid, 8080)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill"),
        ):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert "Stopped" in result.output

    def test_lists_multiple_workflows(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_pid(pid_tmpdir, pid, 8080, "/tmp/wf1.yaml")
        _write_pid(pid_tmpdir, pid, 9090, "/tmp/wf2.yaml")

        with patch("conductor.cli.pid._is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert "Multiple background workflows" in result.output
        assert "8080" in result.output
        assert "9090" in result.output


class TestStopProcessGone:
    """Test stopping a process that has already exited."""

    def test_process_already_exited(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, 99999999, 8080)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill", side_effect=ProcessLookupError),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        assert result.exit_code == 0
        assert "already exited" in result.output


class TestStopProcessUnexpectedOSError:
    """Companion regression for issue #166.

    The original bug crashed ``conductor stop`` when ``_is_process_alive``
    propagated an unexpected ``OSError`` (e.g. ``WinError 11``). That probe
    is now defensive — but ``_stop_process`` itself also calls ``os.kill``
    one frame deeper and must tolerate the same class of failure, especially
    because the "assume alive" fallback in ``_is_process_alive_windows`` lets
    probe-failing PIDs reach this code path.
    """

    def test_unexpected_oserror_does_not_crash(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, 99999999, 8080)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch(
                "conductor.cli.app.os.kill",
                side_effect=OSError(
                    11, "An attempt was made to load a program with an incorrect format"
                ),
            ),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        assert result.exit_code == 0
        assert "Could not signal" in result.output

    def test_pid_file_is_removed_after_oserror(self, pid_tmpdir: Path) -> None:
        # Even when os.kill raises an unexpected OSError, the PID file should
        # be cleaned up so the user's ``conductor stop`` listings don't
        # accumulate phantom entries.
        _write_pid(pid_tmpdir, 99999999, 8080)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill", side_effect=OSError(11, "boom")),
        ):
            runner.invoke(app, ["stop", "--port", "8080"])

        assert list(pid_tmpdir.glob("*.pid")) == []
