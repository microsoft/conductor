"""Tests for the ``conductor fleet list`` CLI command (Fleet Manager E4).

Covers:
- Zero, one, and several run records rendering as a Rich table.
- Portless records (a foreground run with no dashboard) render ``—`` for
  Port rather than crashing.
- The empty case prints a dim "no runs" line and exits 0 (a normal state,
  not an error).
- ``conductor fleet`` bare invocation (launches the TUI as of Fleet Manager
  E7) and ``fleet list --help`` both work.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.fleet.records import RunRecord, write_run_record

runner = CliRunner()


@pytest.fixture()
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect both the run-record directory and the legacy ``.pid`` directory.

    Mirrors the ``fleet_env`` fixture in ``tests/test_fleet/test_records.py``
    so these CLI tests never pick up real records under the developer's
    actual ``~/.conductor/``.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))

    legacy_dir = tmp_path / "legacy_runs"
    legacy_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: legacy_dir)

    return home


def _write_record(
    run_id: str,
    *,
    pid: int | None = None,
    port: int | None = 8080,
    workflow: str = "/tmp/my-workflow.yaml",
    mode: str = "bg",
    started_at: str = "2026-03-03T00:00:00",
) -> RunRecord:
    """Write a live (current-process-PID) run record for list rendering."""
    record = RunRecord(
        run_id=run_id,
        pid=pid if pid is not None else os.getpid(),
        workflow_path=workflow,
        workflow_name=Path(workflow).stem,
        started_at=started_at,
        event_log_path=f"/tmp/conductor/{run_id}.events.jsonl",
        port=port,
        mode=mode,
        checkpoint_dir="/tmp/conductor/checkpoints",
    )
    write_run_record(record)
    return record


class TestFleetListCommand:
    """Tests for the 'conductor fleet list' CLI command."""

    def test_help(self) -> None:
        """`fleet list --help` works."""
        result = runner.invoke(app, ["fleet", "list", "--help"])
        assert result.exit_code == 0
        assert "List every live Conductor run" in result.output

    def test_no_runs(self, fleet_env: Path) -> None:
        """Empty output when there are no runs -- a normal state, not an error."""
        result = runner.invoke(app, ["fleet", "list"])

        assert result.exit_code == 0
        assert "No runs found" in result.output

    def test_single_run(self, fleet_env: Path) -> None:
        """A single run record renders its workflow, mode, PID, port and start time."""
        _write_record("run-one", port=9090, workflow="/tmp/greeter.yaml", mode="fg-web")

        result = runner.invoke(app, ["fleet", "list"])

        assert result.exit_code == 0
        assert "greeter" in result.output
        assert "fg-web" in result.output
        assert "9090" in result.output
        assert str(os.getpid()) in result.output
        assert "2026-03-03T00:00:00" in result.output

    def test_several_runs(self, fleet_env: Path) -> None:
        """Several run records of different modes all appear."""
        _write_record("run-fg", port=None, workflow="/tmp/foreground.yaml", mode="fg")
        _write_record("run-web", port=7000, workflow="/tmp/webrun.yaml", mode="fg-web")
        _write_record("run-bg", port=7001, workflow="/tmp/bgrun.yaml", mode="bg")

        result = runner.invoke(app, ["fleet", "list"])

        assert result.exit_code == 0
        for name in ("foreground", "webrun", "bgrun"):
            assert name in result.output
        for mode in ("fg", "fg-web", "bg"):
            assert mode in result.output

    def test_portless_record_renders_em_dash(self, fleet_env: Path) -> None:
        """A foreground record with no dashboard port renders '—', not a crash."""
        _write_record("run-portless", port=None, workflow="/tmp/foreground.yaml", mode="fg")

        result = runner.invoke(app, ["fleet", "list"])

        assert result.exit_code == 0
        assert "—" in result.output

    def test_dead_pid_not_listed(self, fleet_env: Path) -> None:
        """A run record whose process is no longer alive is excluded (and pruned)."""
        # A PID astronomically unlikely to be alive.
        _write_record("run-dead", pid=2**30 - 1, workflow="/tmp/deadrun.yaml")

        result = runner.invoke(app, ["fleet", "list"])

        assert result.exit_code == 0
        assert "deadrun" not in result.output
        assert "No runs found" in result.output


class TestFleetBareInvocation:
    """Bare ``conductor fleet`` (no subcommand) launches the TUI (Fleet Manager E7)."""

    def test_bare_invocation_launches_tui(self) -> None:
        """With `textual` available, bare invocation constructs and runs the
        FleetApp. FleetApp itself is mocked so this test doesn't launch a
        real interactive terminal app (which would hang forever under
        CliRunner, which provides no real TTY to quit from)."""
        with (
            patch("conductor.cli.fleet.TEXTUAL_AVAILABLE", True),
            patch("conductor.fleet.tui.app.FleetApp") as mock_app_cls,
        ):
            result = runner.invoke(app, ["fleet"])

        assert result.exit_code == 0
        mock_app_cls.return_value.run.assert_called_once()

    def test_group_help(self) -> None:
        result = runner.invoke(app, ["fleet", "--help"])
        assert result.exit_code == 0
        assert "Monitor and manage running Conductor workflows." in result.output
        assert "list" in result.output
