"""Tests for the read-only ``conductor status`` command (issue #384).

Before this command existed, the only way to see which background workflows
were running was ``conductor stop`` — which stops one when exactly one is
running. So the natural "what's running?" reflex was destructive precisely when
there was a single run to lose. I killed a healthy 40-minute workflow that way.

The property under test is therefore simple and absolute: **`status` must never
terminate anything, in any configuration.**

It also surfaces the dashboard URL, which is otherwise unrecoverable once the
launching terminal is gone.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()

_RUN_ID = "a1b2c3d4"


@pytest.fixture()
def pid_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override ``pid_dir()`` to use a temporary directory."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: runs_dir)
    return runs_dir


def _write_pid(pid_dir: Path, pid: int, port: int, workflow: str = "/tmp/wf.yaml") -> Path:
    name = Path(workflow).stem
    filepath = pid_dir / f"{name}-{port}.pid"
    filepath.write_text(
        json.dumps(
            {
                "pid": pid,
                "port": port,
                "workflow": workflow,
                "started_at": "2026-03-03T00:00:00",
                "run_id": _RUN_ID,
            }
        )
    )
    return filepath


class TestStatusNeverStops:
    """The whole reason this command exists."""

    def test_single_run_is_listed_not_stopped(self, pid_tmpdir: Path) -> None:
        """``stop`` terminates when exactly one run exists. ``status`` must not.

        This is the exact scenario that cost me a workflow, so it is asserted
        directly rather than inferred from the absence of output.
        """
        _write_pid(pid_tmpdir, 4242, 8080)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.app._stop_process") as stop_one,
            patch("conductor.cli.app.os.kill") as kill,
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "8080" in result.output
        stop_one.assert_not_called()
        kill.assert_not_called()

    def test_pid_files_are_left_in_place(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, 4242, 8080)

        with patch("conductor.cli.pid._is_process_alive", return_value=True):
            runner.invoke(app, ["status"])

        assert len(list(pid_tmpdir.glob("*.pid"))) == 1

    def test_multiple_runs_are_all_listed(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, 1, 8080, "/tmp/wf1.yaml")
        _write_pid(pid_tmpdir, 2, 9090, "/tmp/wf2.yaml")

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.app._stop_process") as stop_one,
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "8080" in result.output
        assert "9090" in result.output
        stop_one.assert_not_called()


class TestStatusOutput:
    def test_nothing_running_is_not_an_error(self, pid_tmpdir: Path) -> None:
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "No background workflows" in result.output

    def test_dashboard_url_is_shown(self, pid_tmpdir: Path) -> None:
        """The URL is unrecoverable once the launching terminal is gone, so
        discovery is the main thing this command is for."""
        _write_pid(pid_tmpdir, 4242, 8080)

        with patch("conductor.cli.pid._is_process_alive", return_value=True):
            result = runner.invoke(app, ["status"])

        assert "127.0.0.1:8080" in result.output.replace("\n", "")


class TestStatusJson:
    def test_json_lists_running_workflows(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, 4242, 8080)

        with patch("conductor.cli.pid._is_process_alive", return_value=True):
            result = runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert len(payload["running"]) == 1
        entry = payload["running"][0]
        assert entry["pid"] == 4242
        assert entry["port"] == 8080
        assert entry["run_id"] == _RUN_ID
        assert entry["url"] == "http://127.0.0.1:8080"

    def test_json_empty_when_nothing_runs(self, pid_tmpdir: Path) -> None:
        result = runner.invoke(app, ["status", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"running": []}

    def test_json_is_ascii_safe(self, pid_tmpdir: Path) -> None:
        """Workflow paths are user data and can contain non-ASCII.

        The JSON sink must stay encodable on a legacy stdout codec — see #342,
        where exactly this crashed a completed run after it had succeeded.
        """
        _write_pid(pid_tmpdir, 4242, 8080, "/tmp/wörkflow-→.yaml")

        with patch("conductor.cli.pid._is_process_alive", return_value=True):
            result = runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        result.stdout.encode("ascii")  # must not raise
        assert json.loads(result.stdout)["running"][0]["port"] == 8080
