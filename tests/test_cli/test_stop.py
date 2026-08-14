"""Tests for ``conductor stop`` CLI command.

Covers (Fleet Manager E3 — ``conductor stop`` over run records):

- Discovering every mode (``fg``, ``fg-web``, ``bg``) via
  ``conductor.fleet.records.read_run_records()``, not just background runs.
- ``--run-id`` and ``--port`` selectors; ``--all``; auto-stop-single; the
  multi-workflow listing branch.
- A missing ``port`` (portless foreground record) never crashes discovery,
  the listing, or ``--port`` filtering, and renders as ``—``.
- D1: the foreground-stop confirmation prompt, ``--yes``/``-y`` bypass, and
  the non-TTY refusal.
- E3-T5: the confirmation prompt's checkpoint-awareness text.
- E3-T6: run-id-keyed record removal, falling back to port-keyed removal
  for a legacy ``.pid`` record.
- E3-T10: ``_stop_process`` verifies termination (polling
  ``is_process_alive``) before reporting success or letting the caller
  remove the run record, escalating to ``SIGKILL`` if the grace period
  elapses.
- E3-T12: the Windows ``CTRL_BREAK_EVENT`` cross-console limitation.
- Legacy ``.pid`` file compatibility: still listable and stoppable, and
  never triggers the D1 prompt (mode is always ``"bg"``).
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import Identity, app
from conductor.cli.pid import Liveness

# ``conductor.cli`` re-exports the ``app`` Typer instance under the
# ``conductor.cli.app`` name, shadowing the submodule for attribute-based
# patching -- importlib resolves the module itself.
app_module = importlib.import_module("conductor.cli.app")

runner = CliRunner()


def _alive_then_dead():
    """``is_process_alive`` side_effect: alive on first probe per PID, dead after.

    Simulates a process that is running during discovery (the first probe
    ``read_run_records()`` performs for that PID) and has actually
    terminated by the time ``_stop_process`` polls again after signalling
    it (E3-T10) — without waiting out the real grace period in every test.
    """
    seen: dict[int, int] = {}

    def _is_alive(pid: int) -> bool:
        seen[pid] = seen.get(pid, 0) + 1
        return seen[pid] == 1

    return _is_alive


@contextlib.contextmanager
def _fast_grace_period():
    """Shrink the E3-T10 grace period/poll interval so escalation tests stay fast.

    A context manager (rather than ``monkeypatch.setattr``) because
    ``conductor.cli`` re-exports the ``app`` Typer instance under the
    ``conductor.cli.app`` name (see ``cli/__init__.py``), which shadows the
    submodule for pytest's own dotted-path attribute resolver; plain
    ``unittest.mock.patch`` (used everywhere else in this file) resolves it
    correctly via ``pkgutil.resolve_name``.
    """
    with (
        patch("conductor.cli.app._STOP_GRACE_PERIOD_SECONDS", 0.05),
        patch("conductor.cli.app._STOP_POLL_INTERVAL_SECONDS", 0.01),
    ):
        yield


@contextlib.contextmanager
def _stops_cleanly():
    """Patch the ladder so the target is confirmed dead on the first rung.

    These tests cover *routing* -- which records get targeted by ``--port`` /
    ``--run-id`` / ``--all`` / auto-detect -- not the escalation ladder
    itself, which has its own module (``test_stop_ladder.py``).

    Patching ``pid.process_liveness``/``pid.wait_for_exit`` rather than
    ``os.kill`` is load-bearing: ``_stop_process`` escalates through
    ``pid.terminate_process`` and asks ``process_liveness`` (not
    ``is_process_alive``) whether the target died, so a test that stubs only
    ``os.kill`` leaves the run looking like it survived and ``stop`` exits 2.
    """
    with (
        patch("conductor.cli.pid.is_process_alive", return_value=True),
        patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
        patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.DEAD),
        patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
        patch("conductor.cli.app._request_graceful_kill", return_value=True),
        # Stubbed even though the graceful rung above "succeeds": these tests
        # write records whose pid is the *test process's own*, and a portless
        # (foreground) record skips the dashboard rung entirely -- so an
        # unstubbed signal rung SIGTERMs the test runner mid-suite, which
        # presents as the whole session dying rather than as a failure.
        patch("conductor.cli.app._signal_process"),
        patch("conductor.cli.pid.terminate_process", return_value=Liveness.DEAD),
    ):
        yield


@contextlib.contextmanager
def _spy_stop_process():
    """Wrap the real ``_stop_process`` so calls are recorded without changing behaviour.

    Stop counts are asserted here rather than on ``app.os.kill``: the ladder
    escalates through ``pid.terminate_process``, so ``os.kill`` is no longer
    the seam ``stop`` acts on and counting it would silently measure nothing.
    """
    with patch.object(app_module, "_stop_process", wraps=app_module._stop_process) as spy:
        yield spy


@pytest.fixture(autouse=True)
def no_self_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep self-exclusion (issue #399) from perturbing the targeting tests.

    Clears the bg-launch identity env vars and stubs ``own_run_pids`` so no
    record is ever misidentified as "this run" by a coincidental ancestor
    PID. That is not hypothetical here: these tests write records whose
    ``pid`` is the *test process's own* PID so the liveness probe passes,
    which is exactly what ``partition_own_run``'s ancestry check flags.
    """
    monkeypatch.delenv("CONDUCTOR_RUN_ID", raising=False)
    monkeypatch.delenv("CONDUCTOR_WEB_BG", raising=False)
    monkeypatch.delenv("CONDUCTOR_WEB_PORT", raising=False)
    monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset())


@pytest.fixture()
def pid_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate both legacy PID files and Fleet Manager run records.

    ``conductor stop`` discovers workflows via
    ``conductor.fleet.records.read_run_records()`` (Fleet Manager E3),
    which reads *both* the ``CONDUCTOR_HOME``-aware run-record directory
    and the legacy, unredirected ``cli.pid.pid_dir()`` location. Without
    isolating both, these tests would pick up (and be polluted by) any
    real run records or PID files under the developer's actual
    ``~/.conductor/``, matching the ``fleet_env`` fixture already used by
    ``tests/test_fleet/test_records.py``.
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: runs_dir)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))

    return runs_dir


_LIVE_PID = 999001
"""A PID no live process owns, for tests that stub the liveness probe."""

_OTHER_PID = 999002
"""A second such PID, for tests that need two distinguishable runs."""


def _write_pid(
    pid_dir: Path,
    pid: int,
    port: int,
    workflow: str = "/tmp/wf.yaml",
    run_id: str = "",
) -> Path:
    """Helper to write a legacy PID file directly."""
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


def _write_run_record(
    run_id: str,
    pid: int,
    port: int | None,
    workflow: str = "/tmp/wf.yaml",
    mode: str = "bg",
    checkpoint_dir: str | None = "/tmp/conductor/checkpoints",
) -> Path:
    """Helper to write a Fleet Manager run record (the new, ``run_id``-keyed format)."""
    from conductor.fleet.records import RunRecord, write_run_record

    return write_run_record(
        RunRecord(
            run_id=run_id,
            pid=pid,
            workflow_path=workflow,
            workflow_name=Path(workflow).stem,
            started_at="2026-03-03T00:00:00",
            event_log_path=f"/tmp/conductor/{run_id}.events.jsonl",
            port=port,
            mode=mode,
            checkpoint_dir=checkpoint_dir,
        )
    )


def _write_checkpoint_file(
    checkpoint_dir: Path,
    workflow_name: str,
    run_id: str,
    *,
    trigger: str = "periodic",
    suffix: str = "20260303-000000",
) -> Path:
    """Write a minimal, valid checkpoint JSON file directly (for E3-T5 tests).

    Bypasses ``CheckpointManager.save_checkpoint`` (which requires a real
    ``WorkflowContext``/``LimitEnforcer``) and writes just the fields
    ``CheckpointManager.load_checkpoint`` requires, matching the on-disk
    shape documented in ``engine/checkpoint.py``.
    """
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    path = checkpoint_dir / f"{workflow_name}-{suffix}.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "workflow_path": f"/tmp/{workflow_name}.yaml",
                "workflow_hash": "sha256:deadbeef",
                "created_at": "2026-03-03T00:00:00",
                "failure": {"error_type": None, "message": None},
                "current_agent": "some_agent",
                "context": {},
                "limits": {},
                "run_id": run_id,
                "trigger": trigger,
            }
        )
    )
    return path


class TestStopNoRunning:
    """Test behavior when no workflows are running."""

    def test_no_workflows_message(self, pid_tmpdir: Path) -> None:
        result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0
        assert "No workflows are currently running" in result.output


class TestStopByPort:
    """Test ``conductor stop --port <PORT>``."""

    def test_stops_specific_port(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_pid(pid_tmpdir, pid, 8080)

        with (
            _stops_cleanly(),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        assert result.exit_code == 0
        assert "Stopped" in result.output
        assert "8080" in result.output

    def test_error_on_unknown_port(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_pid(pid_tmpdir, pid, 8080)

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop", "--port", "9999"])

        assert result.exit_code == 1
        assert "No running workflow found on port 9999" in result.output

    def test_port_never_matches_a_portless_foreground_record(self, pid_tmpdir: Path) -> None:
        """A foreground record has ``port=None`` -- ``--port <N>`` must never
        raise (E3-T2) and must simply report no match."""
        pid = os.getpid()
        _write_run_record("f0000001", pid, None, "/tmp/wf.yaml", mode="fg")

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        assert result.exit_code == 1
        assert "No running workflow found on port 8080" in result.output


class TestStopByRunId:
    """Test ``conductor stop --run-id <ID>`` (E3-T1)."""

    def test_stops_by_run_id(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_run_record("abcd1234", pid, 8080, "/tmp/wf.yaml", mode="bg")

        with (
            _stops_cleanly(),
        ):
            result = runner.invoke(app, ["stop", "--run-id", "abcd1234"])

        assert result.exit_code == 0
        assert "Stopped" in result.output

    def test_error_on_unknown_run_id(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_run_record("abcd1234", pid, 8080, "/tmp/wf.yaml", mode="bg")

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop", "--run-id", "nonexistent"])

        assert result.exit_code == 1
        assert "No running workflow found with run ID" in result.output

    def test_run_id_finds_a_portless_foreground_record(self, pid_tmpdir: Path) -> None:
        """``--run-id`` is the one selector that can target a foreground run
        with no dashboard/port at all."""
        pid = os.getpid()
        _write_run_record("f0000002", pid, None, "/tmp/wf.yaml", mode="fg")

        with (
            _stops_cleanly(),
        ):
            result = runner.invoke(app, ["stop", "--run-id", "f0000002", "--yes"])

        assert result.exit_code == 0
        assert "Stopped" in result.output


class TestStopAll:
    """Test ``conductor stop --all``."""

    def test_stops_all_workflows(self, pid_tmpdir: Path) -> None:
        # Distinct fake PIDs -- each real workflow run is a distinct OS
        # process, and the ``_alive_then_dead`` helper tracks liveness
        # per-PID, so two entries sharing one PID would collide.
        pid = os.getpid()
        _write_pid(pid_tmpdir, pid, 8080, "/tmp/wf1.yaml")
        _write_pid(pid_tmpdir, pid + 1, 9090, "/tmp/wf2.yaml")

        with (
            _stops_cleanly(),
            _spy_stop_process() as mock_kill,
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
            _stops_cleanly(),
        ):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert "Stopped" in result.output

    def test_lists_multiple_workflows(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_pid(pid_tmpdir, pid, 8080, "/tmp/wf1.yaml")
        _write_pid(pid_tmpdir, pid, 9090, "/tmp/wf2.yaml")

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop"])

        # Listing without stopping anything is a failure to act, so it exits
        # non-zero rather than reporting success to automation.
        assert result.exit_code == 1
        assert "Multiple workflows" in result.output
        assert "8080" in result.output
        assert "9090" in result.output

    def test_lists_mode_and_run_id_columns(self, pid_tmpdir: Path) -> None:
        """E3-T2: the listing table must show ``Mode`` and ``Run ID``."""
        pid = os.getpid()
        _write_run_record("aaaa0001", pid, 8080, "/tmp/wf1.yaml", mode="bg")
        _write_run_record("bbbb0002", pid, None, "/tmp/wf2.yaml", mode="fg")

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop"])

        # Listing without stopping anything is a failure to act, so it exits
        # non-zero rather than reporting success to automation.
        assert result.exit_code == 1
        assert "Mode" in result.output
        assert "Run ID" in result.output
        assert "aaaa0001" in result.output
        assert "bbbb0002" in result.output
        # The portless foreground record's Port column renders as "—", not
        # a crash.
        assert "—" in result.output


class TestStopProcessGone:
    """Test stopping a process that has already exited."""

    def test_process_already_exited(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, 99999999, 8080)

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill", side_effect=ProcessLookupError),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        assert result.exit_code == 0
        assert "already exited" in result.output


class TestStopProcessUnexpectedOSError:
    """Companion regression for issue #166.

    The original bug crashed ``conductor stop`` when ``is_process_alive``
    propagated an unexpected ``OSError`` (e.g. ``WinError 11``). That probe
    is now defensive — but ``_stop_process`` itself also calls ``os.kill``
    one frame deeper and must tolerate the same class of failure, especially
    because the "assume alive" fallback in ``_is_process_alive_windows`` lets
    probe-failing PIDs reach this code path.
    """

    def test_unexpected_oserror_does_not_crash(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, 99999999, 8080)

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            # process_liveness must be stubbed too: the ladder asks it (not
            # is_process_alive) whether the target is worth signalling, and
            # this record's PID is deliberately fake, so an unstubbed probe
            # reports DEAD and the run short-circuits as "already exited"
            # without ever reaching the failing call below.
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
            patch("conductor.cli.app._request_graceful_kill", return_value=False),
            # Patched inside pid.py rather than on `terminate_process`
            # itself: that function already classifies OSError internally,
            # so stubbing it to raise would assert against a path the real
            # implementation cannot take. os.kill is where the surprising
            # errno actually surfaces (issue: WinError 11 / ERROR_BAD_FORMAT).
            patch(
                "conductor.cli.pid.os.kill",
                side_effect=OSError(
                    11, "An attempt was made to load a program with an incorrect format"
                ),
            ),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        # Must not crash -- but must also not claim success for a process it
        # could not signal, so this is exit 2 (not stopped), never 0.
        assert result.exit_code == 2
        assert "Traceback" not in result.output

    def test_pid_file_is_retained_when_process_still_alive_after_oserror(
        self, pid_tmpdir: Path
    ) -> None:
        # An unexpected OSError from os.kill does not confirm the signal was
        # delivered. If the process is still alive, the PID file must be
        # retained -- removing it would let a live run silently disappear
        # from ``conductor stop``/``fleet list`` while still executing.
        _write_pid(pid_tmpdir, 99999999, 8080)

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill", side_effect=OSError(11, "boom")),
        ):
            runner.invoke(app, ["stop", "--port", "8080"])

        assert len(list(pid_tmpdir.glob("*.pid"))) == 1

    def test_pid_file_is_removed_when_process_confirmed_dead_after_oserror(
        self, pid_tmpdir: Path
    ) -> None:
        # If os.kill raises an unexpected OSError but the process is already
        # confirmed dead, the record should still be cleaned up.
        _write_pid(pid_tmpdir, 99999999, 8080)

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=False),
            patch("conductor.cli.app.os.kill", side_effect=OSError(11, "boom")),
        ):
            runner.invoke(app, ["stop", "--port", "8080"])

        assert list(pid_tmpdir.glob("*.pid")) == []


class TestStopDiscoversForegroundRecords:
    """Fleet Manager E3: ``conductor stop`` now discovers *every* mode, not
    just ``bg`` -- closing the design's stated blindness to foreground
    runs."""

    def test_foreground_run_is_discovered_and_listed(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_run_record("f0000010", pid, None, "/tmp/wf.yaml", mode="fg")
        # A second record forces the "multiple workflows" listing branch, so
        # this test observes discovery/listing only -- not stop behavior
        # (a lone foreground record would instead hit the D1 confirmation
        # gate, covered separately by TestStopForegroundConfirmation).
        _write_run_record("b0000099", pid + 1, 9099, "/tmp/wf2.yaml", mode="bg")

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop"])

        # Listing without stopping anything is a failure to act, so it exits
        # non-zero rather than reporting success to automation.
        assert result.exit_code == 1
        assert "fg" in result.output

    def test_foreground_web_run_is_discovered(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_run_record("f0000011", pid, 9000, "/tmp/wf.yaml", mode="fg-web")
        _write_run_record("b0000098", pid + 1, 9098, "/tmp/wf2.yaml", mode="bg")

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop"])

        # Listing without stopping anything is a failure to act, so it exits
        # non-zero rather than reporting success to automation.
        assert result.exit_code == 1
        assert "fg-web" in result.output
        assert "9000" in result.output

    def test_mixed_fg_and_bg_fleet_all_stops_with_confirmation(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_run_record("f0000012", pid, None, "/tmp/wf-fg.yaml", mode="fg")
        _write_run_record("b0000013", pid + 1, 8080, "/tmp/wf-bg.yaml", mode="bg")

        with (
            _stops_cleanly(),
            _spy_stop_process() as mock_kill,
        ):
            result = runner.invoke(app, ["stop", "--all", "--yes"])

        assert result.exit_code == 0
        assert mock_kill.call_count == 2


class TestStopDiscoversFleetRunRecords:
    """Fleet Manager E2/E3 compatibility: ``conductor stop`` discovers (and
    cleans up) the new ``run_id``-keyed run records written by every run,
    not just legacy port-keyed ``.pid`` files."""

    def test_stops_workflow_backed_by_a_run_record(self, pid_tmpdir: Path, tmp_path: Path) -> None:
        pid = os.getpid()
        _write_run_record("deadbeef", pid, 8080, "/tmp/wf.yaml", mode="bg")

        with (
            _stops_cleanly(),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        assert result.exit_code == 0
        assert "Stopped" in result.output
        assert "8080" in result.output

    def test_removes_the_run_record_not_a_pid_file(self, pid_tmpdir: Path, tmp_path: Path) -> None:
        """Stopping a run-record-backed entry must remove its ``.json``
        record via ``remove_run_record`` -- not attempt (and silently no-op)
        a legacy ``.pid`` removal."""
        from conductor.fleet.records import read_run_record

        pid = os.getpid()
        _write_run_record("cafef00d", pid, 8081, "/tmp/wf.yaml", mode="bg")

        with (
            _stops_cleanly(),
        ):
            result = runner.invoke(app, ["stop", "--port", "8081"])

        assert result.exit_code == 0
        assert read_run_record("cafef00d") is None

    def test_stops_all_mixes_legacy_pid_and_run_record_entries(
        self, pid_tmpdir: Path, tmp_path: Path
    ) -> None:
        """``--all`` stops both a legacy ``.pid``-backed run and a fleet
        run-record-backed run in the same invocation."""
        pid = os.getpid()
        _write_pid(pid_tmpdir, pid, 8080, "/tmp/wf1.yaml")
        _write_run_record("abc12345", pid + 1, 9090, "/tmp/wf2.yaml", mode="bg")

        with (
            _stops_cleanly(),
            _spy_stop_process() as mock_kill,
        ):
            result = runner.invoke(app, ["stop", "--all"])

        assert result.exit_code == 0
        assert mock_kill.call_count == 2

    def test_run_record_wins_liveness_check_like_pid_files(
        self, pid_tmpdir: Path, tmp_path: Path
    ) -> None:
        """A dead process's run record is pruned rather than surfaced,
        mirroring legacy ``.pid`` file liveness pruning."""
        _write_run_record("00000000", 99999999, 8083, "/tmp/wf.yaml", mode="bg")

        with patch("conductor.cli.pid.is_process_alive", return_value=False):
            result = runner.invoke(app, ["stop", "--port", "8083"])

        # The dead-pid record is pruned entirely, leaving no running
        # workflows at all -- the "no workflows" branch, not the "found
        # some, none match this port" branch.
        assert result.exit_code == 0
        assert "No workflows are currently running" in result.output

    def test_legacy_pid_record_classifies_as_bg_and_is_stoppable(
        self, pid_tmpdir: Path, tmp_path: Path
    ) -> None:
        """A pre-upgrade legacy ``.pid`` record is still listable and
        stoppable, and (per D1) never classifies as anything but ``bg`` --
        so it can never trigger the foreground-stop confirmation."""
        pid = os.getpid()
        _write_pid(pid_tmpdir, pid, 8084, "/tmp/legacy.yaml")

        with (
            _stops_cleanly(),
            # Even a non-TTY, --yes-less invocation must not be blocked --
            # a bg-only fleet (which a legacy record always is) never
            # prompts, so stdin's TTY-ness is irrelevant here.
            patch("conductor.cli.app._stdin_is_interactive", return_value=False),
        ):
            result = runner.invoke(app, ["stop", "--port", "8084"])

        assert result.exit_code == 0
        assert "Stopped" in result.output


class TestStopForegroundConfirmation:
    """D1: confirmation prompt gating a foreground (``mode in {"fg",
    "fg-web"}``) stop (E3-T3/E3-T4)."""

    def test_confirm_yes_stops_the_foreground_run(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_run_record("f1000001", pid, None, "/tmp/wf.yaml", mode="fg")

        with (
            _stops_cleanly(),
            patch("conductor.cli.app._stdin_is_interactive", return_value=True),
            patch("rich.prompt.Confirm.ask", return_value=True) as mock_confirm,
        ):
            result = runner.invoke(app, ["stop"], input="y\n")

        assert result.exit_code == 0
        assert "Stopped" in result.output
        mock_confirm.assert_called_once()

    def test_confirm_no_stops_nothing_and_exits_zero(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_run_record("f1000002", pid, None, "/tmp/wf.yaml", mode="fg")

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill") as mock_kill,
            patch("conductor.cli.app._stdin_is_interactive", return_value=True),
            patch("rich.prompt.Confirm.ask", return_value=False),
        ):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert "Aborted" in result.output
        mock_kill.assert_not_called()
        from conductor.fleet.records import read_run_record

        assert read_run_record("f1000002") is not None

    def test_all_over_mixed_fleet_prompts_exactly_once(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_run_record("f1000003", pid, None, "/tmp/wf-fg1.yaml", mode="fg")
        _write_run_record("f1000004", pid + 1, 9001, "/tmp/wf-fg2.yaml", mode="fg-web")
        _write_run_record("b1000005", pid + 2, 9002, "/tmp/wf-bg.yaml", mode="bg")

        with (
            _stops_cleanly(),
            _spy_stop_process() as mock_kill,
            patch("conductor.cli.app._stdin_is_interactive", return_value=True),
            patch("rich.prompt.Confirm.ask", return_value=True) as mock_confirm,
        ):
            result = runner.invoke(app, ["stop", "--all"])

        assert result.exit_code == 0
        mock_confirm.assert_called_once()
        assert mock_kill.call_count == 3
        # The prompt names both foreground runs.
        assert "wf-fg1" in result.output
        assert "wf-fg2" in result.output

    def test_all_over_bg_only_fleet_does_not_prompt(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_run_record("b1000006", pid, 9010, "/tmp/wf1.yaml", mode="bg")
        _write_run_record("b1000007", pid + 1, 9011, "/tmp/wf2.yaml", mode="bg")

        with (
            _stops_cleanly(),
            _spy_stop_process() as mock_kill,
            patch("rich.prompt.Confirm.ask") as mock_confirm,
        ):
            result = runner.invoke(app, ["stop", "--all"])

        assert result.exit_code == 0
        mock_confirm.assert_not_called()
        assert mock_kill.call_count == 2

    def test_yes_flag_bypasses_the_prompt(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_run_record("f1000008", pid, None, "/tmp/wf.yaml", mode="fg")

        with (
            _stops_cleanly(),
            patch("rich.prompt.Confirm.ask") as mock_confirm,
        ):
            result = runner.invoke(app, ["stop", "--run-id", "f1000008", "--yes"])

        assert result.exit_code == 0
        assert "Stopped" in result.output
        mock_confirm.assert_not_called()

    def test_short_y_flag_bypasses_the_prompt(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_run_record("f1000009", pid, None, "/tmp/wf.yaml", mode="fg")

        with (
            _stops_cleanly(),
            patch("rich.prompt.Confirm.ask") as mock_confirm,
        ):
            result = runner.invoke(app, ["stop", "--run-id", "f1000009", "-y"])

        assert result.exit_code == 0
        mock_confirm.assert_not_called()

    def test_non_tty_without_yes_signals_nothing_and_exits_nonzero(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_run_record("f100000a", pid, None, "/tmp/wf.yaml", mode="fg")

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill") as mock_kill,
            patch("conductor.cli.app._stdin_is_interactive", return_value=False),
        ):
            result = runner.invoke(app, ["stop", "--run-id", "f100000a"])

        assert result.exit_code != 0
        mock_kill.assert_not_called()
        from conductor.fleet.records import read_run_record

        assert read_run_record("f100000a") is not None

    def test_prompt_names_the_progress_loss_consequence(self, pid_tmpdir: Path) -> None:
        """E3-T5: the confirmation text states in-flight progress is lost
        unless periodic checkpoints are enabled."""
        pid = os.getpid()
        _write_run_record("f100000b", pid, None, "/tmp/wf.yaml", mode="fg", checkpoint_dir=None)

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.app._stdin_is_interactive", return_value=True),
            patch("rich.prompt.Confirm.ask", return_value=False),
        ):
            result = runner.invoke(app, ["stop"])

        assert "progress" in result.output.lower()
        assert "checkpoint" in result.output.lower()

    def test_prompt_reports_periodic_checkpoints_present(
        self, pid_tmpdir: Path, tmp_path: Path
    ) -> None:
        """E3-T5: when a periodic checkpoint file actually matches this
        run's ``run_id``, the prompt says so (not just that the directory
        exists)."""
        pid = os.getpid()
        checkpoint_dir = tmp_path / "checkpoints"
        _write_checkpoint_file(checkpoint_dir, "wf", "f100000c", trigger="periodic")
        _write_run_record(
            "f100000c", pid, None, "/tmp/wf.yaml", mode="fg", checkpoint_dir=str(checkpoint_dir)
        )

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.app._stdin_is_interactive", return_value=True),
            patch("rich.prompt.Confirm.ask", return_value=False),
        ):
            result = runner.invoke(app, ["stop"])

        assert "periodic checkpoints found" in result.output.lower()

    def test_prompt_reports_no_periodic_checkpoints(self, pid_tmpdir: Path, tmp_path: Path) -> None:
        """A checkpoint *directory* existing (shared globally by every run)
        is not, by itself, evidence that *this* run has any -- only a
        checkpoint file whose own ``run_id`` matches counts (E3-T5)."""
        pid = os.getpid()
        checkpoint_dir = tmp_path / "checkpoints"
        checkpoint_dir.mkdir()
        # A checkpoint file exists in the (shared) directory, but for a
        # *different* run_id -- must not be mistaken for this run's own.
        _write_checkpoint_file(checkpoint_dir, "wf", "some-other-run-id", trigger="periodic")
        _write_run_record(
            "f100000d", pid, None, "/tmp/wf.yaml", mode="fg", checkpoint_dir=str(checkpoint_dir)
        )

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.app._stdin_is_interactive", return_value=True),
            patch("rich.prompt.Confirm.ask", return_value=False),
        ):
            result = runner.invoke(app, ["stop"])

        assert "no periodic checkpoints found" in result.output.lower()

    def test_prompt_ignores_a_failure_only_checkpoint(
        self, pid_tmpdir: Path, tmp_path: Path
    ) -> None:
        """A ``trigger="failure"`` checkpoint for this run_id does not count
        as "periodic checkpoints enabled" -- it doesn't protect *future*
        in-flight progress the way an enabled periodic save would."""
        pid = os.getpid()
        checkpoint_dir = tmp_path / "checkpoints"
        _write_checkpoint_file(checkpoint_dir, "wf", "f100000e", trigger="failure")
        _write_run_record(
            "f100000e", pid, None, "/tmp/wf.yaml", mode="fg", checkpoint_dir=str(checkpoint_dir)
        )

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.app._stdin_is_interactive", return_value=True),
            patch("rich.prompt.Confirm.ask", return_value=False),
        ):
            result = runner.invoke(app, ["stop"])

        assert "no periodic checkpoints found" in result.output.lower()


class TestStopSelfExclusion:
    """Issue #399: ``conductor stop`` must never target the run it executes inside."""

    def test_run_id_match_refuses_no_flag(
        self, pid_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "abc123")
        _write_pid(pid_tmpdir, _LIVE_PID, 8080, run_id="abc123")

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
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
            patch("conductor.cli.pid.is_process_alive", return_value=True),
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
        assert [c.args[0].port for c in spy.call_args_list] == [9090]

    def test_all_with_only_self_sends_no_signal(
        self, pid_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "self-run")
        _write_pid(pid_tmpdir, _LIVE_PID, 8080, run_id="self-run")

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
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
            patch("conductor.cli.pid.is_process_alive", return_value=True),
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

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop", "--port", "9999"])

        assert result.exit_code == 1
        assert "No running workflow found on port 9999" in result.output
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
        assert sorted(c.args[0].port for c in spy.call_args_list) == [8080, 9090]

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
        assert [c.args[0].port for c in spy.call_args_list] == [9090]

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
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch.object(app_module, "_stop_process") as stop_spy,
        ):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 1
        assert "Multiple workflows running (2)" in result.output
        assert "9090" in result.output
        assert "9091" in result.output
        assert "Excluded" in result.output
        # The self entry's port must not leak into the "running" listing.
        assert "8080" not in result.output.split("Excluded")[0]
        stop_spy.assert_not_called()


class TestLegacyPidRemovalIsIdentityChecked:
    """The legacy ``.pid`` fallback must match on PID, not port alone.

    ``_remove_stopped_record`` falls back to a port-keyed removal whenever
    the run-record removal removed nothing -- which is the *normal* path,
    since a cooperating child removes its own record on exit. So the
    fallback fires routinely rather than only for pre-upgrade files, and a
    port-only match would delete whatever file currently holds the port.
    Between the caller's snapshot and this call the stopped run's port can
    already have been rebound by a different, live run (issue #344), and
    deleting *its* file leaves a live workflow burning tokens with nothing
    tracking it.
    """

    def test_a_different_live_runs_pid_file_survives(
        self, pid_tmpdir: Path, tmp_path: Path
    ) -> None:
        """Exercised directly rather than through the CLI: driving it via
        ``stop`` cannot isolate this, because ``read_run_records()`` prunes a
        dead-PID ``.pid`` file before ``stop`` ever reaches the removal, and
        making the usurper live enough to survive that would also make it a
        second target of the same ``--port`` selector.
        """
        from conductor.cli.app import _remove_stopped_record
        from conductor.fleet.records import RunRecord

        # A different run has since bound port 8080 and written its own file.
        usurper = _write_pid(pid_tmpdir, _OTHER_PID, 8080, "/tmp/other.yaml", run_id="otherrun")

        stopped = RunRecord(
            run_id="stopme",
            pid=_LIVE_PID,
            workflow_path="/tmp/wf.yaml",
            workflow_name="wf",
            started_at="2026-03-03T00:00:00",
            event_log_path="/tmp/conductor/stopme.events.jsonl",
            port=8080,
            mode="bg",
            checkpoint_dir=None,
        )

        _remove_stopped_record(stopped)

        assert usurper.exists(), "a live run's PID file was deleted by a port-only match"

    def test_the_matching_legacy_pid_file_is_still_removed(
        self, pid_tmpdir: Path, tmp_path: Path
    ) -> None:
        """The fallback must still work for the case it exists to serve."""
        pid = os.getpid()
        legacy = _write_pid(pid_tmpdir, pid, 8082, "/tmp/wf.yaml", run_id="legacyrun")

        with _stops_cleanly():
            result = runner.invoke(app, ["stop", "--port", "8082"])

        assert result.exit_code == 0
        assert not legacy.exists()


class TestJsonModeStillHonorsTheForegroundGate:
    """``--json`` must take D1's refusal branch, not skip the gate.

    JSON mode cannot prompt -- but "cannot ask" is precisely the condition
    the non-TTY branch treats as grounds to *refuse*, whose own docstring
    says defaulting to yes "would reinstate the exact hazard D1 closes".
    Skipping the gate instead let ``conductor stop --all --json`` kill a
    developer's foreground run with no prompt, no ``--yes``, and no refusal.
    """

    def test_json_without_yes_refuses_to_stop_a_foreground_run(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_run_record("j1000001", pid, None, "/tmp/wf.yaml", mode="fg")

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill") as mock_kill,
        ):
            result = runner.invoke(app, ["stop", "--all", "--json"])

        assert result.exit_code == 1
        mock_kill.assert_not_called()

    def test_json_with_yes_proceeds(self, pid_tmpdir: Path) -> None:
        pid = os.getpid()
        _write_run_record("j1000002", pid, None, "/tmp/wf.yaml", mode="fg")

        with _stops_cleanly():
            result = runner.invoke(app, ["stop", "--all", "--json", "--yes"])

        assert result.exit_code == 0

    def test_json_over_a_background_only_fleet_is_unaffected(self, pid_tmpdir: Path) -> None:
        """D1 never gated a bg run, and --json must not start."""
        pid = os.getpid()
        _write_run_record("j1000003", pid, 8090, "/tmp/wf.yaml", mode="bg")

        with _stops_cleanly():
            result = runner.invoke(app, ["stop", "--all", "--json"])

        assert result.exit_code == 0


class TestUnknownForegroundModeStillConfirms:
    """A newer Conductor's foreground mode must not disarm the D1 gate.

    An unrecognised ``mode`` is normalised rather than treated as corrupt
    (a corrupt record is deleted without a liveness check, which would
    orphan a live run). But the normalisation target matters: folding an
    unknown ``fg-*`` into ``bg`` keeps the record *and* silently removes
    the confirmation prompt guarding it, so ``stop --all`` would kill a
    foreground run with no prompt at all.
    """

    def test_an_unknown_fg_variant_is_still_gated(self, pid_tmpdir: Path) -> None:
        from conductor.fleet.records import run_records_dir

        pid = os.getpid()
        (run_records_dir() / "future01.json").write_text(
            json.dumps(
                {
                    "run_id": "future01",
                    "pid": pid,
                    "workflow_path": "/tmp/wf.yaml",
                    "workflow_name": "wf",
                    "started_at": "2026-03-03T00:00:00",
                    "event_log_path": "/tmp/conductor/future01.events.jsonl",
                    "port": None,
                    "mode": "fg-tui",
                    "checkpoint_dir": None,
                }
            )
        )

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.app.os.kill") as mock_kill,
        ):
            # Non-interactive and no --yes: the gate must refuse.
            result = runner.invoke(app, ["stop", "--all"])

        assert result.exit_code == 1
        mock_kill.assert_not_called()
