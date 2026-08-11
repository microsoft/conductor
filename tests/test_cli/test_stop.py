"""Tests for ``conductor stop`` CLI command.

Covers:
- Stopping a workflow by port
- Stopping all workflows with ``--all``
- Auto-stop when exactly one workflow is running
- Listing when multiple workflows are running
- Error cases (no running workflows, invalid port)
- Self-exclusion (issue #399): ``stop`` must never target the run it
  executes inside
"""

from __future__ import annotations

import contextlib
import importlib
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import Identity, app
from conductor.cli.pid import Liveness

# ``conductor.cli.__init__`` does ``from conductor.cli.app import app``, which
# rebinds the *package's* ``app`` attribute to the Typer instance -- shadowing
# the submodule of the same name. ``import conductor.cli.app as x`` resolves
# through that shadowed attribute (via IMPORT_FROM) and would silently hand
# back the Typer app instead of the module, so ``importlib.import_module`` is
# used here instead to get the real module object to patch/wrap attributes on.
app_module = importlib.import_module("conductor.cli.app")

runner = CliRunner()

# ``os.getpid()`` used to be a convenient "definitely alive" PID for these
# fixtures, but the self-exclusion rule (issue #399) treats a PID-file entry
# naming *this* test process as the caller's own run -- which would flip all
# of these tests to the refusal path. A synthetic PID that will never match
# the real test process keeps them deterministic.
_LIVE_PID = 999001

# A second synthetic "definitely alive but distinct from _LIVE_PID" PID, used
# in self+other mixed-population tests so assertions can pin down *which*
# entry was actually signalled rather than merely counting calls (a swapped
# own/other classification would otherwise pass the same assertions).
_OTHER_PID = 999002


@pytest.fixture()
def pid_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override ``pid_dir()`` to use a temporary directory."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: runs_dir)
    return runs_dir


@pytest.fixture(autouse=True)
def no_self_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep self-exclusion (issue #399) from perturbing the pre-existing targeting tests.

    Clears the bg-launch identity env vars and stubs ``own_run_pids`` so no
    entry is ever misidentified as "this run" by a coincidental ancestor PID.
    ``TestStopSelfExclusion`` overrides this per-test to exercise the actual
    self-exclusion behaviour.
    """
    monkeypatch.delenv("CONDUCTOR_RUN_ID", raising=False)
    monkeypatch.delenv("CONDUCTOR_WEB_BG", raising=False)
    monkeypatch.delenv("CONDUCTOR_WEB_PORT", raising=False)
    monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset())


def _write_pid(
    pid_dir: Path, pid: int, port: int, workflow: str = "/tmp/wf.yaml", run_id: str = ""
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
    ``--all`` / auto-detect / self-exclusion — not the escalation ladder
    itself, which has its own module (``test_stop_ladder.py``). Patching the
    outcome also keeps them free of the ladder's real bounded waits.
    """
    with (
        patch("conductor.cli.pid._is_process_alive", return_value=True),
        patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
        patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.DEAD),
        patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
        patch("conductor.cli.app._request_graceful_kill", return_value=True),
    ):
        yield


@contextlib.contextmanager
def _spy_stop_process() -> Iterator[object]:
    """Wrap the real ``_stop_process`` so calls are recorded without changing behaviour.

    Used by the self-exclusion tests to pin down *which* PID-file entries
    were actually targeted, the same way ``TestStopAll`` above pins down
    call args by patching ``_stop_process`` directly -- except here the real
    ladder still runs (under ``_stops_cleanly()``), so the printed "Stopped"
    / "Excluded" / "Warning" text is genuine rather than asserted on faith.
    """
    with patch.object(app_module, "_stop_process", wraps=app_module._stop_process) as spy:
        yield spy


class TestStopNoRunning:
    """Test behavior when no background workflows are running."""

    def test_no_workflows_message(self, pid_tmpdir: Path) -> None:
        result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0
        assert "No background workflows" in result.output


class TestStopByPort:
    """Test ``conductor stop --port <PORT>``."""

    def test_stops_specific_port(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, _LIVE_PID, 8080)

        with _stops_cleanly():
            result = runner.invoke(app, ["stop", "--port", "8080"])

        assert result.exit_code == 0
        assert "Stopped" in result.output
        assert "8080" in result.output

    def test_error_on_unknown_port(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, _LIVE_PID, 8080)

        with patch("conductor.cli.pid._is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop", "--port", "9999"])

        assert result.exit_code == 1
        assert "No background workflow found on port 9999" in result.output


class TestStopAll:
    """Test ``conductor stop --all``."""

    def test_stops_all_workflows(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, _LIVE_PID, 8080, "/tmp/wf1.yaml")
        _write_pid(pid_tmpdir, _LIVE_PID, 9090, "/tmp/wf2.yaml")

        with _stops_cleanly(), patch("conductor.cli.app._stop_process") as stop_one:
            stop_one.return_value = {
                "pid": _LIVE_PID,
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
        _write_pid(pid_tmpdir, _LIVE_PID, 8080)

        with _stops_cleanly():
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert "Stopped" in result.output

    def test_lists_multiple_workflows(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, _LIVE_PID, 8080, "/tmp/wf1.yaml")
        _write_pid(pid_tmpdir, _LIVE_PID, 9090, "/tmp/wf2.yaml")

        with patch("conductor.cli.pid._is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop"])

        # Ambiguous target: nothing was stopped, so this must not report
        # success to a script that only checks the exit code.
        assert result.exit_code == 1
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
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
            patch("conductor.cli.app._request_graceful_kill", return_value=False),
            # Pin the platform so the signal rung is deterministic; otherwise
            # this exercises CTRL_BREAK_EVENT on Windows and SIGTERM on Linux,
            # and only one of them is what CI actually runs.
            patch("sys.platform", "linux"),
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
        # ``conductor stop`` listings don't accumulate phantom entries.
        #
        # ``process_liveness`` is patched explicitly rather than relying on a
        # bogus PID: ``patch("conductor.cli.app.os.kill")`` mutates the shared
        # ``os`` module object, so on POSIX it would also break ``pid.py``'s
        # own probe (turning DEAD into UNKNOWN) and this test would fail on
        # Linux CI while passing on Windows.
        _write_pid(pid_tmpdir, 99999999, 8080)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.DEAD),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        assert result.exit_code == 0
        assert "already exited" in result.output
        assert list(pid_tmpdir.glob("*.pid")) == []


class TestStopSelfExclusion:
    """Issue #399: ``conductor stop`` must never target the run it executes inside."""

    def test_run_id_match_refuses_no_flag(
        self, pid_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "abc123")
        _write_pid(pid_tmpdir, _LIVE_PID, 8080, run_id="abc123")

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch.object(app_module, "_stop_process") as stop_spy,
        ):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert "Refusing" in result.output
        assert "No other workflows are running." in result.output
        stop_spy.assert_not_called()

    def test_ancestry_match_refuses_no_flag(
        self, pid_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_pid(pid_tmpdir, _LIVE_PID, 8080)
        monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset({_LIVE_PID}))

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch.object(app_module, "_stop_process") as stop_spy,
        ):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert "Refusing" in result.output
        stop_spy.assert_not_called()

    def test_all_stops_others_and_reports_exclusion(
        self, pid_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "self-run")
        _write_pid(pid_tmpdir, _LIVE_PID, 8080, "/tmp/self.yaml", run_id="self-run")
        _write_pid(pid_tmpdir, _OTHER_PID, 9090, "/tmp/other.yaml", run_id="other-run")

        with _stops_cleanly(), _spy_stop_process() as spy:
            result = runner.invoke(app, ["stop", "--all"])

        assert result.exit_code == 0
        assert "Excluded" in result.output
        assert "Stopped" in result.output
        assert "9090" in result.output
        # Pins down *which* run was targeted: a classification that swapped
        # own/other would still print "Excluded"/"Stopped", just against the
        # wrong entry.
        assert [c.args[0]["port"] for c in spy.call_args_list] == [9090]

    def test_all_with_only_self_sends_no_signal(
        self, pid_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "self-run")
        _write_pid(pid_tmpdir, _LIVE_PID, 8080, run_id="self-run")

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch.object(app_module, "_stop_process") as stop_spy,
        ):
            result = runner.invoke(app, ["stop", "--all"])

        assert result.exit_code == 0
        assert "No other workflows are running." in result.output
        stop_spy.assert_not_called()

    def test_port_matching_own_run_exits_1(
        self, pid_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "self-run")
        _write_pid(pid_tmpdir, _LIVE_PID, 8080, run_id="self-run")

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch.object(app_module, "_stop_process") as stop_spy,
        ):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        assert result.exit_code == 1
        assert "Refusing" in result.output
        assert "--allow-self" in result.output
        stop_spy.assert_not_called()

    def test_port_unknown_with_only_self_shows_exclusion_not_empty_table(
        self, pid_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "self-run")
        _write_pid(pid_tmpdir, _LIVE_PID, 8080, run_id="self-run")

        with patch("conductor.cli.pid._is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop", "--port", "9999"])

        assert result.exit_code == 1
        assert "No background workflow found on port 9999" in result.output
        assert "Excluded" in result.output
        assert "Running workflows:" not in result.output

    def test_allow_self_restores_stop_and_warns(
        self, pid_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "self-run")
        _write_pid(pid_tmpdir, _LIVE_PID, 8080, run_id="self-run")

        with _stops_cleanly(), _spy_stop_process() as spy:
            result = runner.invoke(app, ["stop", "--allow-self"])

        assert result.exit_code == 0
        assert "Stopped" in result.output
        assert "Warning" in result.output
        assert spy.call_count == 1

    def test_allow_self_with_port_stops_own_run(
        self, pid_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "self-run")
        _write_pid(pid_tmpdir, _LIVE_PID, 8080, run_id="self-run")

        with _stops_cleanly(), _spy_stop_process() as spy:
            result = runner.invoke(app, ["stop", "--allow-self", "--port", "8080"])

        assert result.exit_code == 0
        assert "Stopped" in result.output
        assert "Warning" in result.output
        assert spy.call_count == 1

    def test_allow_self_all_stops_both_and_warns(
        self, pid_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "self-run")
        _write_pid(pid_tmpdir, _LIVE_PID, 8080, "/tmp/self.yaml", run_id="self-run")
        _write_pid(pid_tmpdir, _OTHER_PID, 9090, "/tmp/other.yaml", run_id="other-run")

        with _stops_cleanly(), _spy_stop_process() as spy:
            result = runner.invoke(app, ["stop", "--all", "--allow-self"])

        assert result.exit_code == 0
        assert "Warning" in result.output
        # Only the self entry should trigger the warning; a swapped
        # classification would warn about the *other* entry instead, silently.
        assert result.output.count("Warning") == 1
        assert spy.call_count == 2
        assert sorted(c.args[0]["port"] for c in spy.call_args_list) == [8080, 9090]

    def test_no_flag_mixed_auto_stops_sole_other_and_notes_exclusion(
        self, pid_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No flags, self-run + exactly one other run: auto-stops the other, excludes self.

        Covers `app.py`'s single-target auto-stop branch when the caller's
        own run is present alongside exactly one other -- the most common
        real trigger for issue #399 (an agent's own background workflow
        plus one unrelated run, invoking bare `conductor stop`).
        """
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "self-run")
        _write_pid(pid_tmpdir, _LIVE_PID, 8080, "/tmp/self.yaml", run_id="self-run")
        _write_pid(pid_tmpdir, _OTHER_PID, 9090, "/tmp/other.yaml", run_id="other-run")

        with _stops_cleanly(), _spy_stop_process() as spy:
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert "Stopped" in result.output
        assert "9090" in result.output
        assert "Excluded" in result.output
        assert [c.args[0]["port"] for c in spy.call_args_list] == [9090]

    def test_no_flag_mixed_lists_others_only_and_notes_exclusion(
        self, pid_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No flags, self-run + two other runs: lists the *other* runs only.

        Covers `app.py`'s multi-target listing branch, asserting the printed
        count reflects the post-exclusion `targetable` list (2), not the raw
        PID-file count (3), and that the self entry's port never appears
        under the running-workflows listing.
        """
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "self-run")
        _write_pid(pid_tmpdir, _LIVE_PID, 8080, "/tmp/self.yaml", run_id="self-run")
        _write_pid(pid_tmpdir, 999003, 9090, "/tmp/other1.yaml", run_id="other-run-1")
        _write_pid(pid_tmpdir, 999004, 9091, "/tmp/other2.yaml", run_id="other-run-2")

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch.object(app_module, "_stop_process") as stop_spy,
        ):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 1
        assert "Multiple background workflows running (2)" in result.output
        assert "9090" in result.output
        assert "9091" in result.output
        assert "Excluded" in result.output
        # The self entry's port must not leak into the "running" listing.
        assert "8080" not in result.output.split("Excluded")[0]
        stop_spy.assert_not_called()
