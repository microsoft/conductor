"""Tests for the ``conductor fleet list`` CLI command (Fleet Manager E4).

Covers:
- Zero, one, and several run records rendering as a Rich table.
- Portless records (a foreground run with no dashboard) render ``—`` for
  Port rather than crashing.
- The empty case prints a dim "no runs" line and exits 0 (a normal state,
  not an error).
- ``conductor fleet`` bare invocation (launches the TUI as of Fleet Manager
  E7) and ``fleet list --help`` both work.

As of the MCP server plan's E4, this command also lists recently-completed
runs (from ``read_terminal_records``, bounded by ``[fleet.retention]
.keep_last``) as additional rows carrying their real terminal status
(``completed``/``failed``) rather than the hard-coded ``"running"`` every
live row still shows — a user-facing contract change accepted at
stakeholder review (R1). ``--live`` restores the pre-E4 scope exactly.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.fleet.records import RunRecord, TerminalRunRecord, write_run_record

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


def _write_terminal(
    run_id: str,
    *,
    status: str = "success",
    workflow: str = "/tmp/completed-workflow.yaml",
    error_type: str | None = None,
) -> TerminalRunRecord:
    """Write a completed (terminal) run record for list rendering."""
    from conductor.fleet.records import write_terminal_record

    record = TerminalRunRecord(
        run_id=run_id,
        workflow_path=workflow,
        workflow_name=Path(workflow).stem,
        started_at="2026-03-03T00:00:00+00:00",
        ended_at="2026-03-03T00:05:00+00:00",
        status=status,
        output={},
        error_type=error_type,
        error_message=None,
        total_tokens=None,
        total_cost_usd=None,
        unpriced_agent_count=0,
        event_log_path=f"/tmp/conductor/{run_id}.events.jsonl",
        bg_stderr_log=None,
        bg_stdout_log=None,
    )
    write_terminal_record(record)
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


class TestFleetListCompletedRuns:
    """E4-T3: completed rows carry their real terminal status, bounded by
    ``[fleet.retention].keep_last``; ``--live`` restores the pre-E4 scope."""

    def test_completed_run_shows_real_terminal_status(self, fleet_env: Path) -> None:
        _write_terminal("run-done", status="success", workflow="/tmp/donerun.yaml")

        result = runner.invoke(app, ["fleet", "list"])

        assert result.exit_code == 0
        assert "donerun" in result.output
        assert "completed" in result.output.lower()

    def test_failed_completed_run_shows_failed_status(self, fleet_env: Path) -> None:
        _write_terminal("run-failed", status="failed", workflow="/tmp/failedrun.yaml")

        result = runner.invoke(app, ["fleet", "list"])

        assert result.exit_code == 0
        assert "failedrun" in result.output
        assert "failed" in result.output.lower()

    def test_live_row_still_shows_the_coarse_running_status(self, fleet_env: Path) -> None:
        """A live row is unaffected by the addition of completed rows -- it
        keeps the same coarse ``"running"`` status as before."""
        _write_record("run-live", workflow="/tmp/liverun.yaml")
        _write_terminal("run-done", status="success", workflow="/tmp/donerun.yaml")

        result = runner.invoke(app, ["fleet", "list"])

        assert result.exit_code == 0
        assert "liverun" in result.output
        assert "running" in result.output.lower()

    def test_live_flag_restores_the_old_scope(self, fleet_env: Path) -> None:
        """``--live`` must reproduce the pre-completed-runs output exactly:
        only live rows, no completed rows at all."""
        _write_record("run-live", workflow="/tmp/liverun.yaml")
        _write_terminal("run-done", status="success", workflow="/tmp/donerun.yaml")

        live_result = runner.invoke(app, ["fleet", "list", "--live"])
        default_result = runner.invoke(app, ["fleet", "list"])

        assert live_result.exit_code == 0
        assert "liverun" in live_result.output
        assert "donerun" not in live_result.output
        assert "donerun" in default_result.output

    def test_live_flag_with_only_completed_runs_reports_no_runs(self, fleet_env: Path) -> None:
        """With ``--live`` and nothing currently running, the empty-fleet
        message renders even though a completed run exists."""
        _write_terminal("run-done", status="success", workflow="/tmp/donerun.yaml")

        result = runner.invoke(app, ["fleet", "list", "--live"])

        assert result.exit_code == 0
        assert "No runs found" in result.output

    def test_both_empty_renders_without_error(self, fleet_env: Path) -> None:
        result = runner.invoke(app, ["fleet", "list"])

        assert result.exit_code == 0
        assert "No runs found" in result.output

    def test_one_empty_one_populated_renders_without_error(self, fleet_env: Path) -> None:
        """Live empty, completed populated -- and vice versa -- both render
        cleanly without treating either half as required."""
        _write_terminal("run-done", status="success", workflow="/tmp/donerun.yaml")
        result = runner.invoke(app, ["fleet", "list"])
        assert result.exit_code == 0
        assert "donerun" in result.output

        _write_record("run-live", workflow="/tmp/liverun.yaml")
        result2 = runner.invoke(app, ["fleet", "list", "--live"])
        assert result2.exit_code == 0
        assert "liverun" in result2.output
        assert "donerun" not in result2.output

    def test_completed_set_honours_keep_last(self, fleet_env: Path) -> None:
        """The completed rows are bounded by ``[fleet.retention].keep_last``,
        not shown unbounded."""
        for i in range(5):
            _write_terminal(f"run-{i}", status="success", workflow=f"/tmp/wf{i}.yaml")

        with patch("conductor.cli.fleet._resolve_completed_keep_last", return_value=2):
            result = runner.invoke(app, ["fleet", "list"])

        assert result.exit_code == 0
        shown = sum(1 for i in range(5) if f"wf{i}" in result.output)
        assert shown == 2

    def test_malformed_settings_falls_back_rather_than_crashing(self, fleet_env: Path) -> None:
        """A broken ``~/.conductor/config.toml`` must not break `fleet list`."""
        _write_terminal("run-done", status="success", workflow="/tmp/donerun.yaml")

        with patch("conductor.settings.load_settings", side_effect=Exception("boom")):
            result = runner.invoke(app, ["fleet", "list"])

        assert result.exit_code == 0
        assert "donerun" in result.output


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
