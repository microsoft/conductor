"""Tests for the ``conductor stop`` termination ladder (issue #344).

``conductor stop`` used to send one best-effort signal, print "Stopped", and
delete the PID file unconditionally. On Windows that signal reliably failed
(``CTRL_BREAK_EVENT`` needs a shared console, which a separate ``stop``
invocation does not have), so the common outcome was: workflow still running,
user told it stopped, PID file gone — an untracked orphan burning tokens with
no supported way to find it again.

These tests pin the properties the fix requires:

1. **A PID file is only removed once its process is confirmed dead**, and the
   command reports failure when it isn't. This is the orphan bug itself, and
   it is asserted end-to-end through the CLI because the *decision* to unlink
   lives in the command, not in the ladder.
2. **Forceful termination is gated on confirming identity.** A recorded PID may
   since have been recycled onto an unrelated process, and terminating by PID
   with no identity check would be *worse* than the old failure mode: the old
   broken signal killed nothing, whereas ``TerminateProcess`` on a recycled PID
   kills a bystander.
3. **Removal is identity-checked too**, so stopping run A cannot deregister a
   newer run B that has taken over the same port.

The liveness primitives are patched at their definition site
(``conductor.cli.pid``) rather than at the call site, because the command
imports them lazily inside the function body — so the patched attribute is
what the import resolves to. Patching also keeps these tests free of real
sleeps.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from conductor.cli.app import _confirm_identity, _stop_process, app
from conductor.cli.pid import (
    Liveness,
    remove_pid_file_at,
    write_pid_file,
)

runner = CliRunner()

_RUN_ID = "a1b2c3d4"


@pytest.fixture()
def pid_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override ``pid_dir()`` to use a temporary directory."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: runs_dir)
    return runs_dir


def _entry(pid: int = 4242, port: int = 8080, run_id: str = _RUN_ID) -> dict:
    """Build a PID-file dict shaped like ``read_pid_files`` output."""
    return {
        "pid": pid,
        "port": port,
        "workflow": "/tmp/my-workflow.yaml",
        "started_at": "2026-01-01T00:00:00+00:00",
        "run_id": run_id,
        "file": f"/tmp/runs/my-workflow-{port}.pid",
    }


def _quiet() -> Console:
    """A console that renders nothing, so tests assert behaviour not prose."""
    return Console(quiet=True)


class TestSurvivingRunIsNotOrphaned:
    """The reported bug, asserted end-to-end through the CLI."""

    def test_pid_file_survives_when_the_process_does(self, pid_tmpdir: Path) -> None:
        write_pid_file(4242, 8080, "/tmp/my-workflow.yaml", run_id=_RUN_ID)

        # Every rung fails and the process refuses to die — the real Windows
        # behaviour that produced the bug report.
        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=True),
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
            patch("conductor.cli.app._signal_process"),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        assert result.exit_code == 2, "a surviving workflow must not report success"
        assert list(pid_tmpdir.glob("*.pid")), (
            "PID file was removed for a process that is still running — the run "
            "is now an untracked orphan (issue #344)"
        )

    def test_pid_file_is_removed_once_the_process_is_dead(self, pid_tmpdir: Path) -> None:
        write_pid_file(4242, 8080, "/tmp/my-workflow.yaml", run_id=_RUN_ID)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.DEAD),
            patch("conductor.cli.app._confirm_identity", return_value=True),
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        assert result.exit_code == 0
        assert list(pid_tmpdir.glob("*.pid")) == []

    def test_all_reports_failure_if_any_survives(self, pid_tmpdir: Path) -> None:
        write_pid_file(1, 8080, "/tmp/wf1.yaml", run_id=_RUN_ID)
        write_pid_file(2, 9090, "/tmp/wf2.yaml", run_id="ffffffff")

        # First target dies on the graceful rung; the second survives every
        # rung.  ``wait_for_exit`` is called once per rung attempted.
        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch(
                "conductor.cli.pid.wait_for_exit",
                side_effect=[Liveness.DEAD, Liveness.ALIVE, Liveness.ALIVE],
            ),
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=True),
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
            patch("conductor.cli.app._signal_process"),
        ):
            result = runner.invoke(app, ["stop", "--all"])

        assert result.exit_code == 2, "one survivor must fail the whole command"
        remaining = sorted(json.loads(p.read_text())["port"] for p in pid_tmpdir.glob("*.pid"))
        assert remaining == [9090], "only the confirmed-dead run should be deregistered"


class TestLadderOutcomes:
    """Each rung reports honestly which one actually worked."""

    def test_graceful_rung_reports_api_kill(self) -> None:
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.DEAD),
            patch("conductor.cli.app._confirm_identity", return_value=True),
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
        ):
            result = _stop_process(_entry(), _quiet())
        assert result["outcome"] == "stopped"
        assert result["rung"] == "api-kill"

    def test_signal_rung_used_when_graceful_fails(self) -> None:
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch(
                "conductor.cli.pid.wait_for_exit",
                side_effect=[Liveness.ALIVE, Liveness.DEAD],
            ),
            patch("conductor.cli.app._confirm_identity", return_value=True),
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
            patch("conductor.cli.app._signal_process") as sig,
        ):
            result = _stop_process(_entry(), _quiet())
        assert result["outcome"] == "stopped"
        assert result["rung"] == "signal"
        sig.assert_called_once_with(4242)

    def test_already_exited_is_not_an_error(self) -> None:
        with patch("conductor.cli.pid.process_liveness", return_value=Liveness.DEAD):
            result = _stop_process(_entry(), _quiet())
        assert result["outcome"] == "already-exited"
        assert result["rung"] == "none"

    def test_dead_process_is_never_signalled(self) -> None:
        """Don't signal a PID already known to be gone — by now it may belong
        to somebody else entirely."""
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.DEAD),
            patch("conductor.cli.app._signal_process") as sig,
            patch("conductor.cli.pid.terminate_process") as term,
        ):
            _stop_process(_entry(), _quiet())
        sig.assert_not_called()
        term.assert_not_called()


class TestForcefulTerminationRequiresIdentity:
    """Terminating an unidentified PID would be worse than not terminating."""

    def test_unconfirmed_identity_refuses_to_terminate(self) -> None:
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=False),
            patch("conductor.cli.app._signal_process"),
            patch("conductor.cli.pid.terminate_process") as terminate,
        ):
            result = _stop_process(_entry(), _quiet())

        assert result["outcome"] == "unconfirmed"
        terminate.assert_not_called()

    def test_force_overrides_the_identity_gate(self) -> None:
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=False),
            patch("conductor.cli.app._signal_process"),
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.DEAD) as terminate,
        ):
            result = _stop_process(_entry(), _quiet(), force=True)

        assert result["outcome"] == "stopped"
        assert result["rung"] == "terminate"
        terminate.assert_called_once()

    def test_confirmed_identity_escalates_to_terminate(self) -> None:
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=True),
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
            patch("conductor.cli.app._signal_process"),
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.DEAD) as terminate,
        ):
            result = _stop_process(_entry(), _quiet())

        assert result["outcome"] == "stopped"
        terminate.assert_called_once()

    def test_graceful_rung_is_skipped_when_identity_unconfirmed(self) -> None:
        """Never POST /api/kill to a dashboard we have not identified."""
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.DEAD),
            patch("conductor.cli.app._confirm_identity", return_value=False),
            patch("conductor.cli.app._signal_process"),
            patch("conductor.cli.app._request_graceful_kill") as graceful,
        ):
            _stop_process(_entry(), _quiet())

        graceful.assert_not_called()


class TestIdentityConfirmation:
    """``/api/info`` is the only party that can prove who owns a PID."""

    def test_matching_run_id_confirms(self) -> None:
        with patch("httpx.get") as get:
            get.return_value.json.return_value = {"run_id": _RUN_ID}
            get.return_value.raise_for_status.return_value = None
            assert _confirm_identity(_entry(), _quiet()) is True

    def test_mismatched_run_id_rejects(self) -> None:
        with patch("httpx.get") as get:
            get.return_value.json.return_value = {"run_id": "deadbeef"}
            get.return_value.raise_for_status.return_value = None
            assert _confirm_identity(_entry(), _quiet()) is False

    def test_empty_info_is_not_a_pass(self) -> None:
        """``/api/info`` returns {} before the first workflow_started event."""
        with patch("httpx.get") as get:
            get.return_value.json.return_value = {}
            get.return_value.raise_for_status.return_value = None
            assert _confirm_identity(_entry(), _quiet()) is False

    def test_unreachable_dashboard_is_not_a_pass(self) -> None:
        with patch("httpx.get", side_effect=OSError("connection refused")):
            assert _confirm_identity(_entry(), _quiet()) is False

    def test_missing_run_id_in_pid_file_is_not_a_pass(self) -> None:
        """Upgrade path: PID files written by older conductor have no run_id."""
        with patch("httpx.get") as get:
            assert _confirm_identity(_entry(run_id=""), _quiet()) is False
            get.assert_not_called()


class TestRemovePidFileAt:
    """Removal must be identity-checked, not port-keyed."""

    def test_removes_when_pid_matches(self, pid_tmpdir: Path) -> None:
        path = write_pid_file(4242, 8080, "/tmp/wf.yaml", run_id=_RUN_ID)
        assert remove_pid_file_at(path, 4242) is True
        assert not path.exists()

    def test_refuses_when_a_different_run_took_the_slot(self, pid_tmpdir: Path) -> None:
        """The race that makes port-keyed removal unsafe.

        Run A is being stopped. Mid-stop it exits, and run B binds the same
        port and writes its own PID file under the same name. Removing "the
        file for port 8080" would deregister B — a live workflow — leaving it
        untracked. Re-reading identity before unlinking prevents it.
        """
        path = write_pid_file(4242, 8080, "/tmp/wf.yaml", run_id=_RUN_ID)
        write_pid_file(9999, 8080, "/tmp/wf.yaml", run_id="ffffffff")  # run B

        assert remove_pid_file_at(path, 4242) is False
        assert path.exists(), "run B's PID file was deleted while B was still running"
        assert json.loads(path.read_text())["pid"] == 9999

    def test_missing_file_is_not_an_error(self, pid_tmpdir: Path) -> None:
        assert remove_pid_file_at(pid_tmpdir / "gone-8080.pid", 4242) is False


class TestJsonOutput:
    """``--json`` exists so automation stops having to parse prose."""

    def test_json_lists_stopped_runs(self, pid_tmpdir: Path) -> None:
        write_pid_file(4242, 8080, "/tmp/wf.yaml", run_id=_RUN_ID)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.DEAD),
            patch("conductor.cli.app._confirm_identity", return_value=True),
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["failed"] == []
        assert len(payload["stopped"]) == 1
        entry = payload["stopped"][0]
        # The fields automation needs to identify *which* run this was.
        assert entry["pid"] == 4242
        assert entry["port"] == 8080
        assert entry["run_id"] == _RUN_ID
        assert entry["rung"] == "api-kill"

    def test_json_reports_survivors(self, pid_tmpdir: Path) -> None:
        write_pid_file(4242, 8080, "/tmp/wf.yaml", run_id=_RUN_ID)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=True),
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
            patch("conductor.cli.app._signal_process"),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080", "--json"])

        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["stopped"] == []
        assert payload["failed"][0]["outcome"] == "survived"


class TestWritePidFileRecordsIdentity:
    """Identity can only be checked if it was recorded in the first place."""

    def test_run_id_round_trips(self, pid_tmpdir: Path) -> None:
        path = write_pid_file(1, 8080, "/tmp/wf.yaml", run_id=_RUN_ID, log_file="/tmp/e.jsonl")
        data = json.loads(path.read_text())
        assert data["run_id"] == _RUN_ID
        assert data["log_file"] == "/tmp/e.jsonl"

    def test_launcher_actually_forwards_run_id(self) -> None:
        """Guard the wiring, which is the part that was missing.

        ``run_id`` was in the PID-file schema and in ``/api/info`` from the
        start, but the launcher never populated it, so it was always ``""``
        and no identity check was possible. Asserting on the source keeps this
        honest without standing up a real background process: every unit test
        above *supplies* a populated ``run_id``, so nothing else here would
        notice if the wiring regressed.

        ``_spawn_bg_child`` is the single choke point — both ``conductor run
        --web-bg`` and ``conductor resume --web-bg`` reach the PID file
        through it — so guarding it covers both launch paths.
        """
        import inspect

        from conductor.cli import bg_runner

        finalize_src = inspect.getsource(bg_runner._finalize_background_launch)
        assert "run_id=run_id" in finalize_src, (
            "bg_runner must forward run_id to write_pid_file, or conductor stop "
            "can never confirm a run's identity (issue #344)"
        )
        spawn_src = inspect.getsource(bg_runner._spawn_bg_child)
        assert "run_id=run_id" in spawn_src, (
            "_spawn_bg_child must pass run_id to _finalize_background_launch"
        )
