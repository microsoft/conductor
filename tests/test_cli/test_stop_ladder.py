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

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from conductor.cli.app import (
    Identity,
    _confirm_identity,
    _request_graceful_kill,
    _signal_process,
    _stop_process,
    app,
)
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


@pytest.fixture(autouse=True)
def no_self_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep self-exclusion (issue #399) from perturbing these ladder tests.

    These tests use small, arbitrary PIDs (e.g. ``1``, ``2``, ``4242``) as
    stand-ins for "some other process" -- they predate issue #399 and are not
    about self-exclusion at all. But ``conductor.cli.self_run.own_run_pids()``
    walks real process ancestry, and in a shallow-PID-namespace environment
    (e.g. a container) that walk can genuinely include low PIDs like ``1`` or
    ``2``, which would misclassify these entries as the caller's own run and
    silently exclude them from ``stop --all`` targeting. Clearing the env
    vars and stubbing ``own_run_pids`` keeps these pre-existing tests exactly
    as deterministic as they were before #399; self-exclusion itself is
    covered by ``test_stop.py::TestStopSelfExclusion``.
    """
    monkeypatch.delenv("CONDUCTOR_RUN_ID", raising=False)
    monkeypatch.delenv("CONDUCTOR_WEB_BG", raising=False)
    monkeypatch.delenv("CONDUCTOR_WEB_PORT", raising=False)
    monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset())


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


class TestWorkflowNamesArePrintedLiterally:
    """A workflow name is data, and the surrounding styling is not.

    Conductor consoles are built with ``markup=False`` and style through
    ``styled()`` (issue #406). An f-string written against the old
    markup-parsing console therefore fails in one of two ways depending on
    which side of #406 it runs on: the console parses ``deploy[prod]`` as a
    tag and drops it, or it stops parsing and the ``[green]`` scaffolding is
    printed at the user verbatim.

    That matters here more than in most output: after issue #344 the entire job
    of these messages is telling the operator *which* run survived so they can
    go and kill it by hand.

    Note a ``/`` cannot appear inside a filename, so the shapes worth testing
    are the lowercase-initial ones rich reads as an opening tag.
    """

    @pytest.mark.parametrize(
        "stem",
        ["deploy[prod]", "run[task1]", "x[dim]y", "q[not a tag]z"],
    )
    def test_bracketed_workflow_name_survives_every_rung(self, stem: str) -> None:
        from conductor.console import make_console

        rungs = [
            ({"process_liveness": Liveness.DEAD}, "already-exited"),
            ({"terminate": Liveness.ALIVE}, "survived"),
            ({"terminate": Liveness.UNKNOWN}, "unconfirmed"),
        ]
        for overrides, expected in rungs:
            buf = io.StringIO()
            con = make_console(file=buf, width=200)
            entry = _entry() | {"workflow": f"/tmp/{stem}.yaml"}
            with (
                patch(
                    "conductor.cli.pid.process_liveness",
                    return_value=overrides.get("process_liveness", Liveness.ALIVE),
                ),
                patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
                patch(
                    "conductor.cli.pid.terminate_process",
                    return_value=overrides.get("terminate", Liveness.DEAD),
                ),
                patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
                patch("conductor.cli.app._request_graceful_kill", return_value=False),
                patch("conductor.cli.app._signal_process"),
            ):
                result = _stop_process(entry, con)

            out = buf.getvalue()
            assert result["outcome"] == expected
            assert stem in out, f"{expected!r} rung mangled the workflow name: {out!r}"
            # No template of ours has a closing tag left in it, and none of the
            # names above contain ``[/`` — so this catches styling that leaked
            # through as literal text.
            assert "[/" not in out, f"{expected!r} rung leaked raw markup: {out!r}"

    def test_mismatch_warning_keeps_the_name_intact(self) -> None:
        """The mismatch path interpolates into the longest template, so it is
        the easiest one to regress when the wording is edited.

        A ``/`` cannot appear inside a filename, so the dangerous shapes here
        are the lowercase-initial ones rich reads as opening tags.
        """
        from conductor.console import make_console

        buf = io.StringIO()
        con = make_console(file=buf, width=200)
        entry = _entry() | {"workflow": "/tmp/a[bold]c.yaml"}
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.MISMATCHED),
            patch("conductor.cli.app._signal_process") as signal,
        ):
            result = _stop_process(entry, con)

        assert result["outcome"] == "mismatched"
        signal.assert_not_called()
        assert "a[bold]c" in buf.getvalue()
        assert "[/" not in buf.getvalue()


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
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
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
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080"])

        assert result.exit_code == 0
        assert list(pid_tmpdir.glob("*.pid")) == []

    def test_all_reports_failure_if_any_survives(self, pid_tmpdir: Path) -> None:
        write_pid_file(1, 8080, "/tmp/wf1.yaml", run_id=_RUN_ID)
        write_pid_file(2, 9090, "/tmp/wf2.yaml", run_id="ffffffff")

        # Port 8080's run dies on the graceful rung; 9090's survives every
        # rung. Keyed off the pid rather than call order, because
        # ``read_pid_files`` iterates ``Path.glob`` whose order is
        # filesystem-dependent.
        def _wait(pid: int, timeout: float, interval: float = 0.1) -> Liveness:
            return Liveness.DEAD if pid == 1 else Liveness.ALIVE

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", side_effect=_wait),
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
            patch("conductor.cli.app._signal_process"),
        ):
            result = runner.invoke(app, ["stop", "--all"])

        assert result.exit_code == 2, "one survivor must fail the whole command"
        remaining = sorted(json.loads(p.read_text())["port"] for p in pid_tmpdir.glob("*.pid"))
        assert remaining == [9090], "only the confirmed-dead run should be deregistered"

    def test_force_can_clear_an_entry_whose_liveness_cannot_be_probed(
        self, pid_tmpdir: Path
    ) -> None:
        """Otherwise an unprobeable entry wedges ``stop`` permanently (#166).

        When the liveness probe itself fails we cannot say whether the process
        died, so the file is kept — correctly. But nothing could then ever
        remove it: bare ``stop`` stays ambiguous and ``stop --all`` exits 2 for
        good, so a CI teardown never recovers. ``--force`` is the operator
        accepting that risk.
        """
        write_pid_file(4242, 8080, "/tmp/my-workflow.yaml", run_id=_RUN_ID)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.UNKNOWN),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
            patch("conductor.cli.app._signal_process"),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080", "--force"])

        assert list(pid_tmpdir.glob("*.pid")) == [], (
            "--force must be able to clear an entry the probe cannot resolve, or "
            "the entry is permanent"
        )
        # Still a failure: we cleared the record, we did not confirm a stop.
        assert result.exit_code == 2

    def test_force_does_not_clear_a_demonstrably_surviving_run(self, pid_tmpdir: Path) -> None:
        """The escape hatch is for *uncertainty*, not for a live process.

        ``survived`` means terminate ran and the process is still there.
        Removing its file would orphan it — the exact bug this PR fixes — so
        ``--force`` must not widen into that case.
        """
        write_pid_file(4242, 8080, "/tmp/my-workflow.yaml", run_id=_RUN_ID)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
            patch("conductor.cli.app._signal_process"),
        ):
            result = runner.invoke(app, ["stop", "--port", "8080", "--force"])

        assert result.exit_code == 2
        assert list(pid_tmpdir.glob("*.pid")), "a live process must stay tracked even under --force"


class TestLadderOutcomes:
    """Each rung reports honestly which one actually worked."""

    def test_graceful_rung_reports_api_kill(self) -> None:
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.DEAD),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
            patch("conductor.cli.app._request_graceful_kill", return_value=True) as graceful,
            patch("conductor.cli.app._signal_process") as sig,
        ):
            result = _stop_process(_entry(), _quiet())
        assert result["outcome"] == "stopped"
        assert result["rung"] == "api-kill"
        # The graceful rung must actually have been attempted, not merely
        # skipped past into a lucky liveness read.
        graceful.assert_called_once_with(8080)
        sig.assert_not_called()

    def test_signal_rung_used_when_graceful_fails(self) -> None:
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch(
                "conductor.cli.pid.wait_for_exit",
                side_effect=[Liveness.ALIVE, Liveness.DEAD],
            ),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
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
            patch("conductor.cli.app._confirm_identity") as ident,
            patch("conductor.cli.app._request_graceful_kill") as graceful,
        ):
            _stop_process(_entry(), _quiet())
        sig.assert_not_called()
        term.assert_not_called()
        # Nor should we bother the network for a process we know is gone.
        ident.assert_not_called()
        graceful.assert_not_called()

    def test_unknown_after_terminate_is_not_reported_as_survived(self) -> None:
        """A failed probe is not evidence the process lived.

        ``terminate_process`` returns UNKNOWN when the probe itself failed
        (e.g. ``WaitForSingleObject`` returned an unexpected value, or SIGKILL
        raised ``PermissionError``). Reporting ``survived`` there would assert
        more than is known; JSON consumers would read "definitely still alive".
        """
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.UNKNOWN),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
            patch("conductor.cli.app._signal_process"),
        ):
            result = _stop_process(_entry(), _quiet())
        assert result["outcome"] == "unconfirmed"
        assert result["rung"] == "terminate"

    def test_alive_after_terminate_is_reported_as_survived(self) -> None:
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
            patch("conductor.cli.app._signal_process"),
        ):
            result = _stop_process(_entry(), _quiet())
        assert result["outcome"] == "survived"


class TestForcefulTerminationRequiresIdentity:
    """Terminating an unidentified PID would be worse than not terminating."""

    def test_unconfirmed_identity_refuses_to_terminate(self) -> None:
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.UNCONFIRMED),
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
            patch("conductor.cli.app._confirm_identity", return_value=Identity.UNCONFIRMED),
            patch("conductor.cli.app._signal_process"),
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.DEAD) as terminate,
        ):
            result = _stop_process(_entry(), _quiet(), force=True)

        assert result["outcome"] == "stopped"
        assert result["rung"] == "terminate"
        terminate.assert_called_once()

    def test_force_does_not_override_a_positive_mismatch(self) -> None:
        """``--force`` overrides uncertainty, never evidence of the wrong PID.

        A MISMATCHED identity is not "we could not tell" — it is the dashboard
        on that port reporting a different PID than the file records, which is
        positive proof the PID was recycled onto an unrelated process. Killing
        it is the exact failure this PR exists to prevent, so ``--force`` must
        not reach it. Without this test the ladder signalled *and* force-killed
        that process while printing "Refusing to act on it", then reported
        ``stopped`` and exited 0.
        """
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.MISMATCHED),
            patch("conductor.cli.app._request_graceful_kill", return_value=True) as graceful,
            patch("conductor.cli.app._signal_process") as sig,
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.DEAD) as terminate,
        ):
            result = _stop_process(_entry(), _quiet(), force=True)

        assert result["outcome"] == "mismatched"
        assert result["rung"] == "refused"
        graceful.assert_not_called()
        sig.assert_not_called()
        terminate.assert_not_called()

    def test_force_reconfirms_identity_before_the_irreversible_rung(self) -> None:
        """The recycle window is not something ``--force`` may skip.

        Rung 3 is irreversible, and seconds of waiting elapse before it. If the
        target dies in that window its PID can land on an unrelated process, so
        the re-check must happen under ``--force`` too — previously it was
        skipped precisely when the consequences were worst.
        """
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch(
                "conductor.cli.app._confirm_identity",
                side_effect=[Identity.UNCONFIRMED, Identity.MISMATCHED],
            ) as ident,
            patch("conductor.cli.app._signal_process"),
            patch("conductor.cli.pid.terminate_process", return_value=Liveness.DEAD) as terminate,
        ):
            result = _stop_process(_entry(), _quiet(), force=True)

        assert ident.call_count == 2
        assert result["outcome"] == "mismatched"
        terminate.assert_not_called()

    def test_confirmed_identity_escalates_to_terminate(self) -> None:
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
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
            patch("conductor.cli.app._confirm_identity", return_value=Identity.UNCONFIRMED),
            patch("conductor.cli.app._signal_process"),
            patch("conductor.cli.app._request_graceful_kill") as graceful,
        ):
            _stop_process(_entry(), _quiet())

        graceful.assert_not_called()

    def test_positive_mismatch_signals_nothing_at_all(self) -> None:
        """A confirmed mismatch means this PID is somebody else's process.

        Refusing only the *forceful* rung would be incoherent: on POSIX the
        polite rung is ``SIGTERM``, which is perfectly capable of killing the
        bystander the identity gate exists to protect.
        """
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.MISMATCHED),
            patch("conductor.cli.app._signal_process") as sig,
            patch("conductor.cli.pid.terminate_process") as term,
        ):
            result = _stop_process(_entry(), _quiet())

        # ``mismatched`` rather than ``unconfirmed``: this outcome carries
        # positive evidence about the PID, and reporting it as "could not
        # confirm" understates what the identity probe actually established.
        assert result["outcome"] == "mismatched"
        sig.assert_not_called()
        term.assert_not_called()

    def test_unconfirmed_identity_still_sends_the_polite_signal(self) -> None:
        """ "Cannot confirm" is not evidence of anything.

        PID files written by older conductor have no ``run_id``, so identity is
        unconfirmable. Refusing to signal them would be a regression: a signal
        is all the previous implementation ever sent, and on POSIX it works.
        """
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.DEAD),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.UNCONFIRMED),
            patch("conductor.cli.app._signal_process") as sig,
        ):
            result = _stop_process(_entry(run_id=""), _quiet())

        assert result["outcome"] == "stopped"
        assert result["rung"] == "signal"
        sig.assert_called_once_with(4242)

    def test_identity_is_reconfirmed_before_forceful_termination(self) -> None:
        """Seconds elapse between the first check and the irreversible rung.

        If the target dies during those waits, its PID can be recycled onto an
        unrelated process — so the check is repeated immediately before
        terminating rather than trusting the stale result.
        """
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.terminate_process") as term,
            patch(
                "conductor.cli.app._confirm_identity",
                side_effect=[Identity.CONFIRMED, Identity.MISMATCHED],
            ) as ident,
            patch("conductor.cli.app._request_graceful_kill", return_value=True),
            patch("conductor.cli.app._signal_process"),
        ):
            result = _stop_process(_entry(), _quiet())

        assert ident.call_count == 2
        # ``mismatched`` rather than ``unconfirmed``: the second probe returned
        # positive evidence the PID belongs to someone else, which is knowledge,
        # not the absence of it.
        assert result["outcome"] == "mismatched"
        term.assert_not_called()


class TestIdentityConfirmation:
    """Only the running process itself can prove it owns a PID and a port."""

    def _info(self, payload: dict) -> object:
        resp = patch("httpx.get")
        return resp

    def test_matching_pid_confirms(self) -> None:
        with patch("httpx.get") as get:
            get.return_value.json.return_value = {"pid": 4242, "run_id": _RUN_ID}
            get.return_value.raise_for_status.return_value = None
            assert _confirm_identity(_entry(), _quiet()) is Identity.CONFIRMED

    def test_pid_wins_over_run_id(self) -> None:
        """A resumed run legitimately reports a *different* run_id than the
        launcher recorded, so a PID match must be decisive on its own.

        Regression guard for the resume path: ``bg_runner`` generates a fresh
        run id for the PID file, but a resumed child reuses the checkpoint's
        original run id (``event_log.py`` returns early on ``existing_run_id``
        before consulting ``CONDUCTOR_RUN_ID``). Comparing run ids alone marked
        every resumed ``--web-bg`` run as somebody else's, which on Windows
        blocked the forceful rung and left the run unstoppable.
        """
        with patch("httpx.get") as get:
            get.return_value.json.return_value = {"pid": 4242, "run_id": "different"}
            get.return_value.raise_for_status.return_value = None
            assert _confirm_identity(_entry(), _quiet()) is Identity.CONFIRMED

    def test_mismatched_pid_is_a_positive_mismatch(self) -> None:
        with patch("httpx.get") as get:
            get.return_value.json.return_value = {"pid": 9999, "run_id": _RUN_ID}
            get.return_value.raise_for_status.return_value = None
            assert _confirm_identity(_entry(), _quiet()) is Identity.MISMATCHED

    def test_older_dashboard_falls_back_to_run_id(self) -> None:
        """A dashboard from before this change reports no ``pid``."""
        with patch("httpx.get") as get:
            get.return_value.json.return_value = {"run_id": _RUN_ID}
            get.return_value.raise_for_status.return_value = None
            assert _confirm_identity(_entry(), _quiet()) is Identity.CONFIRMED

    def test_older_dashboard_mismatched_run_id(self) -> None:
        with patch("httpx.get") as get:
            get.return_value.json.return_value = {"run_id": "deadbeef"}
            get.return_value.raise_for_status.return_value = None
            assert _confirm_identity(_entry(), _quiet()) is Identity.MISMATCHED

    def test_empty_info_is_unconfirmed_not_mismatched(self) -> None:
        """An old dashboard returns ``{}`` until ``workflow_started`` fires."""
        with patch("httpx.get") as get:
            get.return_value.json.return_value = {}
            get.return_value.raise_for_status.return_value = None
            assert _confirm_identity(_entry(), _quiet()) is Identity.UNCONFIRMED

    def test_unreachable_dashboard_is_unconfirmed(self) -> None:
        with patch("httpx.get", side_effect=OSError("connection refused")):
            assert _confirm_identity(_entry(), _quiet()) is Identity.UNCONFIRMED

    def test_legacy_pid_file_without_run_id_is_unconfirmed(self) -> None:
        with patch("httpx.get") as get:
            get.return_value.json.return_value = {"run_id": _RUN_ID}
            get.return_value.raise_for_status.return_value = None
            assert _confirm_identity(_entry(run_id=""), _quiet()) is Identity.UNCONFIRMED

    def test_non_dict_response_is_unconfirmed(self) -> None:
        """A proxy or captive portal answering on the port must not be trusted."""
        with patch("httpx.get") as get:
            get.return_value.json.return_value = ["not", "a", "dict"]
            get.return_value.raise_for_status.return_value = None
            assert _confirm_identity(_entry(), _quiet()) is Identity.UNCONFIRMED

    def test_http_error_status_is_unconfirmed(self) -> None:
        with patch("httpx.get") as get:
            get.return_value.raise_for_status.side_effect = RuntimeError("404")
            assert _confirm_identity(_entry(), _quiet()) is Identity.UNCONFIRMED


class TestGracefulKillRequest:
    """Rung 1 talks to the run's own dashboard.

    Run-registry isolation (``rundir.runs_dir()`` -> a temp directory,
    needed since ``_request_graceful_kill`` resolves a token via
    ``conductor.web.auth.resolve_cli_token``) is provided globally by the
    autouse ``_isolated_runs_dir`` fixture in ``tests/conftest.py``.
    """

    def test_posts_to_api_kill_and_reports_acceptance(self) -> None:
        with patch("httpx.post") as post:
            post.return_value.raise_for_status.return_value = None
            assert _request_graceful_kill(8080) is True
        assert post.call_args.args[0] == "http://127.0.0.1:8080/api/kill"
        # POST /api/kill is a mutating route (issue #397): a JSON
        # Content-Type is required even though the body is empty.
        assert post.call_args.kwargs["headers"]["Content-Type"] == "application/json"

    def test_unreachable_dashboard_reports_failure(self) -> None:
        with patch("httpx.post", side_effect=OSError("connection refused")):
            assert _request_graceful_kill(8080) is False

    def test_error_status_reports_failure(self) -> None:
        with patch("httpx.post") as post:
            post.return_value.raise_for_status.side_effect = RuntimeError("500")
            assert _request_graceful_kill(8080) is False

    def test_sends_authorization_header_when_token_resolved(self) -> None:
        """A token file for the target port is picked up and presented."""
        from conductor.web.auth import write_token_file

        write_token_file(8080, "file-token")

        with patch("httpx.post") as post:
            post.return_value.raise_for_status.return_value = None
            assert _request_graceful_kill(8080) is True
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer file-token"


class TestSignalProcess:
    """Rung 2 is best-effort and must never raise."""

    def test_posix_sends_sigterm(self) -> None:
        import signal

        with (
            patch("sys.platform", "linux"),
            patch("conductor.cli.app.os.kill") as kill,
        ):
            _signal_process(4242)
        kill.assert_called_once_with(4242, signal.SIGTERM)

    def test_windows_sends_ctrl_break(self) -> None:
        import signal

        with (
            patch("sys.platform", "win32"),
            # CTRL_BREAK_EVENT is Windows-only; inject it so the branch is
            # verified on Linux CI too rather than silently skipped.
            patch.object(signal, "CTRL_BREAK_EVENT", 1, create=True),
            patch("conductor.cli.app.os.kill") as kill,
        ):
            _signal_process(4242)
            # Asserted inside the patch: the sentinel does not exist on Linux
            # once the patch is undone.
            kill.assert_called_once_with(4242, signal.CTRL_BREAK_EVENT)

    def test_oserror_is_swallowed(self) -> None:
        """On Windows this fails routinely (WinError 87, no shared console).
        It must not abort the ladder before the forceful rung."""
        with (
            patch("sys.platform", "linux"),
            patch("conductor.cli.app.os.kill", side_effect=OSError(87, "bad parameter")),
        ):
            _signal_process(4242)  # must not raise

    def test_valueerror_is_swallowed(self) -> None:
        with (
            patch("sys.platform", "linux"),
            patch("conductor.cli.app.os.kill", side_effect=ValueError("bad signal")),
        ):
            _signal_process(4242)  # must not raise


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

    def test_unreadable_file_is_left_in_place(self, pid_tmpdir: Path) -> None:
        """A corrupt PID file is not proof the run ended.

        Identity cannot be established, so the safe action is to leave the
        registration alone rather than deregister a possibly-live workflow.
        """
        corrupt = pid_tmpdir / "wf-8080.pid"
        corrupt.write_text("{ this is not json")

        assert remove_pid_file_at(corrupt, 4242) is False
        assert corrupt.exists()


class TestJsonOutput:
    """``--json`` exists so automation stops having to parse prose."""

    def test_json_when_nothing_is_running(self, pid_tmpdir: Path) -> None:
        result = runner.invoke(app, ["stop", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"stopped": [], "failed": []}

    def test_json_reports_unknown_port_as_an_error(self, pid_tmpdir: Path) -> None:
        write_pid_file(4242, 8080, "/tmp/wf.yaml", run_id=_RUN_ID)
        with patch("conductor.cli.pid._is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop", "--port", "9999", "--json"])
        assert result.exit_code == 1
        assert "error" in json.loads(result.stdout)

    def test_json_reports_ambiguous_target_as_an_error(self, pid_tmpdir: Path) -> None:
        write_pid_file(1, 8080, "/tmp/wf1.yaml", run_id=_RUN_ID)
        write_pid_file(2, 9090, "/tmp/wf2.yaml", run_id="ffffffff")
        with patch("conductor.cli.pid._is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop", "--json"])
        # Nothing was stopped, so this must not look like success.
        assert result.exit_code == 1
        assert "error" in json.loads(result.stdout)

    def test_json_lists_stopped_runs(self, pid_tmpdir: Path) -> None:
        write_pid_file(4242, 8080, "/tmp/wf.yaml", run_id=_RUN_ID)

        with (
            patch("conductor.cli.pid._is_process_alive", return_value=True),
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.DEAD),
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
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
            patch("conductor.cli.app._confirm_identity", return_value=Identity.CONFIRMED),
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
        path = write_pid_file(
            1,
            8080,
            "/tmp/wf.yaml",
            run_id=_RUN_ID,
            stderr_log="/tmp/e.stderr.log",
            stdout_log="/tmp/e.stdout.log",
        )
        data = json.loads(path.read_text())
        assert data["run_id"] == _RUN_ID
        assert data["stderr_log"] == "/tmp/e.stderr.log"
        assert data["stdout_log"] == "/tmp/e.stdout.log"

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
