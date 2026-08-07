"""Tests for ``conductor stop`` CLI command.

Covers:
- Stopping a workflow by port
- Stopping all workflows with ``--all``
- Auto-stop when exactly one workflow is running
- Listing when multiple workflows are running
- Error cases (no running workflows, invalid port)
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.cli.pid import Liveness

runner = CliRunner()


@pytest.fixture()
def pid_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override ``pid_dir()`` to use a temporary directory."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: runs_dir)
    return runs_dir


def _write_pid(
    pid_dir: Path,
    pid: int,
    port: int,
    workflow: str = "/tmp/wf.yaml",
    run_id: str = "a1b2c3d4",
) -> Path:
    """Helper to write a PID file directly."""
    name = Path(workflow).stem
    filepath = pid_dir / f"{name}-{port}.pid"
    filepath.write_text(
        json.dumps(
            {
                "pid": pid,
                "port": port,
                "workflow": workflow,
                "started_at": "2026-03-03T00:00:00",
                "run_id": run_id,
            }
        )
    )
    return filepath


@contextlib.contextmanager
def _stops_cleanly() -> Iterator[None]:
    """Patch the ladder so the target is confirmed dead on the first rung.

    These tests cover *routing* — which PID files get targeted by ``--port`` /
    ``--all`` / auto-detect — not the escalation ladder itself, which has its
    own module (``test_stop_ladder.py``). Patching the outcome also keeps them
    free of the ladder's real bounded waits.
    """
    with (
        patch("conductor.cli.pid._is_process_alive", return_value=True),
        patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
        patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.DEAD),
        patch("conductor.cli.app._confirm_identity", return_value=True),
        patch("conductor.cli.app._request_graceful_kill", return_value=True),
    ):
        yield


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

        with _stops_cleanly():
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

        with _stops_cleanly(), patch("conductor.cli.app._stop_process") as stop_one:
            stop_one.return_value = {
                "pid": pid,
                "port": 0,
                "workflow": "wf",
                "run_id": "",
                "outcome": "stopped",
                "rung": "api-kill",
            }
            result = runner.invoke(app, ["stop", "--all"])

        assert result.exit_code == 0
        # Both registered workflows must be targeted.
        assert stop_one.call_count == 2
        assert sorted(c.args[0]["port"] for c in stop_one.call_args_list) == [8080, 9090]


class TestStopAutoDetect:
    """Test ``conductor stop`` with no flags (auto-detect)."""

    def test_auto_stops_single_workflow(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_pid(pid_tmpdir, pid, 8080)

        with _stops_cleanly():
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
    is now defensive — but the stop ladder also calls ``os.kill`` one frame
    deeper and must tolerate the same class of failure, especially because
    the "unknown — assume alive" fallback in the Windows probe lets
    probe-failing PIDs reach this code path.
    """

    def test_unexpected_oserror_does_not_crash(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, 4242, 8080)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.DEAD),
            patch("conductor.cli.app._confirm_identity", return_value=True),
            patch("conductor.cli.app._request_graceful_kill", return_value=False),
            patch(
                "conductor.cli.app.os.kill",
                side_effect=OSError(
                    11, "An attempt was made to load a program with an incorrect format"
                ),
            ),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        # The signal rung swallowed the OSError and the ladder escalated
        # rather than crashing.
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert result.exit_code == 0
        assert "Stopped" in result.output

    def test_pid_file_is_removed_when_process_confirmed_gone(self, pid_tmpdir: Path) -> None:
        # A PID file for a process that no longer exists must be cleaned up so
        # ``conductor stop`` listings don't accumulate phantom entries — even
        # though the signal path would have raised.
        _write_pid(pid_tmpdir, 99999999, 8080)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill", side_effect=OSError(11, "boom")),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        assert result.exit_code == 0
        assert "already exited" in result.output
        assert list(pid_tmpdir.glob("*.pid")) == []
