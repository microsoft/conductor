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
import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import _format_started_at, _print_running_list, app
from conductor.fleet.records import RunRecord

runner = CliRunner()

_RUN_ID = "a1b2c3d4"

_NARROW = {"COLUMNS": "80"}
"""The default terminal width — the inverse of ``test_help_panels.py``'s ``_WIDE``."""


def _squash(text: str) -> str:
    """Strip whitespace and box-drawing characters so a folded cell rejoins.

    ``Dashboard`` is the last table column, so a folded continuation line has
    only empty cells before it; squashing makes a wrapped URL contiguous
    again so it can be searched for as one string.
    """
    return re.sub(r"[\s\u2500-\u257f]", "", text)


@pytest.fixture()
def pid_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate both run records and legacy PID files.

    ``conductor status`` reads run records (``scan_run_records``) as well as
    legacy ``.pid`` files, so both locations must be redirected or these
    tests would pick up the developer's real ``~/.conductor/``.
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: runs_dir)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    return runs_dir


def _write_pid(
    pid_dir: Path,
    pid: int,
    port: int,
    workflow: str = "/tmp/wf.yaml",
    *,
    run_id: str | None = None,
    stderr_log: str = "/tmp/conductor/conductor-wf-20260303-000000-a1b2c3d4.bg.stderr.log",
    stdout_log: str = "/tmp/conductor/conductor-wf-20260303-000000-a1b2c3d4.bg.stdout.log",
) -> Path:
    """Write a run record via the real ``write_run_record``, not hand-built JSON.

    A hand-built fixture with a 19-character naive ``started_at`` (rather than
    production's 32-character microsecond-precision value) is what let issue
    #405 through: the table looked fine against a timestamp shorter than any
    real run ever produces. Delegating to the real writer means the widths
    under test are the widths production actually writes. Since the Fleet
    Manager that writer is ``conductor.fleet.records.write_run_record`` --
    ``cli.pid.write_pid_file`` was removed, and ``status`` reads run records
    now -- but the anti-hand-rolling reason for going through it is unchanged.
    """
    from conductor.fleet.records import RunRecord, write_run_record

    # Run records are keyed by run_id, so two runs sharing one id would
    # overwrite each other -- unlike the port-keyed .pid files this helper
    # replaced. Default to a per-port id so multi-run tests get two records.
    resolved_run_id = run_id if run_id is not None else f"{_RUN_ID[:4]}{port:04d}"

    # `status --json` locates a bg run's capture logs on disk (they are not
    # carried in the record), so they must actually exist for the lookup to
    # report them -- which also keeps this fixture honest about what a real
    # --web-bg launch leaves behind.
    log_dir = pid_dir.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    stem = f"conductor-wf-20260303-000000-{resolved_run_id}"
    (log_dir / f"{stem}.bg.stderr.log").write_text("")
    (log_dir / f"{stem}.bg.stdout.log").write_text("")
    events_log = log_dir / f"{stem}.events.jsonl"
    events_log.write_text("")

    filepath = write_run_record(
        RunRecord(
            run_id=resolved_run_id,
            pid=pid,
            workflow_path=workflow,
            workflow_name=Path(workflow).stem,
            started_at=datetime.now(UTC).isoformat(),
            event_log_path=str(events_log),
            port=port,
            mode="bg",
            checkpoint_dir=None,
        )
    )
    assert pid_dir.exists()
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
            patch("conductor.cli.pid.is_process_alive", return_value=True),
            patch("conductor.cli.app._stop_process") as stop_one,
            patch("conductor.cli.app.os.kill") as kill,
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "8080" in result.output
        stop_one.assert_not_called()
        kill.assert_not_called()

    def test_the_liveness_probe_only_ever_sends_the_null_signal(self, pid_tmpdir: Path) -> None:
        """The test above cannot fail, so this one carries the property.

        ``status`` never reaches ``_stop_process`` or ``app.os.kill``, and the
        one call that *can* signal a process — ``os.kill(pid, 0)`` in
        ``_is_process_alive_posix`` — is stubbed out there by the
        ``_is_process_alive`` patch. So every mutant that makes the probe send
        a real signal survives the whole suite.

        Asserting on the signal value instead pins "never terminates" at the
        only place it can actually be violated. ``sys.platform`` is forced (as
        in ``test_pid.py``) so the POSIX probe runs on any host rather than
        passing vacuously on Windows, where ``os.kill`` is never called.
        """
        _write_pid(pid_tmpdir, 4242, 8080)

        with (
            patch("conductor.cli.pid.sys.platform", "linux"),
            patch("conductor.cli.pid.os.kill") as kill,
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert kill.call_args_list, "the liveness probe never ran, so nothing was asserted"
        assert all(call.args == (4242, 0) for call in kill.call_args_list), (
            f"status signalled a process instead of probing it: {kill.call_args_list}"
        )

    def test_pid_files_are_left_in_place(self, pid_tmpdir: Path) -> None:
        record = _write_pid(pid_tmpdir, 4242, 8080)

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            runner.invoke(app, ["status"])

        assert record.exists()

    def test_a_pid_file_is_not_deleted_when_the_process_looks_dead(self, pid_tmpdir: Path) -> None:
        """Not stopping anything is only half of read-only.

        The test above only covers a *live* process, which the pruning reader
        would have kept anyway — so it passed while ``status`` was still
        deleting things. ``read_pid_files`` unlinks any entry whose process
        looks dead, and a liveness probe that says "dead" is not proof: an
        atomic-write window, a PID namespace, or a transient probe failure all
        reach here. Deleting on that evidence is how issue #344's orphan is
        reached through the command meant to be the safe one.
        """
        record = _write_pid(pid_tmpdir, 4242, 8080)

        with patch("conductor.cli.pid.is_process_alive", return_value=False):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert record.exists(), "status deleted a PID file — a read-only command must not prune"

    def test_an_unreadable_pid_file_is_not_deleted(self, pid_tmpdir: Path) -> None:
        """A half-written file is a live run mid-launch, not garbage."""
        corrupt = pid_tmpdir / "half-written-8080.pid"
        corrupt.write_text('{"pid": 4242, "por')

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert corrupt.exists(), "status deleted a file it merely could not parse"

    def test_one_malformed_file_does_not_hide_the_healthy_runs(self, pid_tmpdir: Path) -> None:
        """One bad file took down the whole listing with a traceback.

        The payload indexes ``e["port"]`` directly, and the reader only guarded
        ``pid``, so a single entry without a port raised ``KeyError`` and the
        command exited 1 having printed nothing — hiding every healthy run.
        """
        (pid_tmpdir / "noport-1234.pid").write_text(json.dumps({"pid": 999}))
        _write_pid(pid_tmpdir, 4242, 8080)

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "8080" in result.output, "a malformed neighbour hid a healthy run"

    def test_multiple_runs_are_all_listed(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, 1, 8080, "/tmp/wf1.yaml")
        _write_pid(pid_tmpdir, 2, 9090, "/tmp/wf2.yaml")

        with (
            patch("conductor.cli.pid.is_process_alive", return_value=True),
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

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["status"])

        assert "127.0.0.1:8080" in result.output.replace("\n", "")


class TestStatusJson:
    def test_json_lists_running_workflows(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, 4242, 8080, run_id=_RUN_ID)

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert len(payload["running"]) == 1
        entry = payload["running"][0]
        assert entry["pid"] == 4242
        assert entry["port"] == 8080
        assert entry["run_id"] == _RUN_ID
        assert entry["stderr_log"].endswith("bg.stderr.log")
        assert entry["stdout_log"].endswith("bg.stdout.log")
        assert entry["url"] == "http://127.0.0.1:8080"

    def test_json_empty_when_nothing_runs(self, pid_tmpdir: Path) -> None:
        result = runner.invoke(app, ["status", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"running": []}

    def test_json_missing_run_id_and_logs_report_null(self, pid_tmpdir: Path) -> None:
        """A PID file written before this field existed reports null, not "".

        Absent key, empty string, and "no run id" are three different facts
        (issue #404); collapsing them to ``""`` made all three indistinguishable
        in scripted output.
        """
        filepath = pid_tmpdir / "wf-8080.pid"
        filepath.write_text(
            json.dumps(
                {
                    "pid": 4242,
                    "port": 8080,
                    "workflow": "/tmp/wf.yaml",
                    "started_at": "2026-03-03T00:00:00",
                }
            )
        )

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        entry = json.loads(result.stdout)["running"][0]
        assert entry["run_id"] is None
        assert entry["stderr_log"] is None
        assert entry["stdout_log"] is None

    def test_json_empty_string_run_id_and_logs_report_null(self, pid_tmpdir: Path) -> None:
        """The actual pre-fix issue #404 shape (keys present, empty strings).

        Unlike the "field didn't exist yet" case above, every PID file
        actually affected by issue #404 has ``run_id``/``log_file`` *present*
        with empty-string values — ``write_pid_file``'s ``run_id``/
        ``stderr_log``/``stdout_log`` parameters already defaulted to ``""``
        before this fix; the bug was that the caller never passed real
        values, not that the keys were absent. This must report ``null`` too.
        ``write_pid_file`` was removed with the Fleet Manager, so the shape is
        built directly here -- a pre-upgrade ``.pid`` file is now the only
        thing that can still carry these empty strings, and
        ``read_run_records`` surfaces it as a record with an empty ``run_id``.
        """
        (pid_tmpdir / "wf-8080.pid").write_text(
            json.dumps(
                {
                    "pid": 4242,
                    "port": 8080,
                    "workflow": "/tmp/wf.yaml",
                    "started_at": "2026-03-03T00:00:00+00:00",
                    "run_id": "",
                    "stderr_log": "",
                    "stdout_log": "",
                }
            )
        )

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        entry = json.loads(result.stdout)["running"][0]
        assert entry["run_id"] is None
        assert entry["stderr_log"] is None
        assert entry["stdout_log"] is None

    def test_json_is_ascii_safe(self, pid_tmpdir: Path) -> None:
        """Workflow paths are user data and can contain non-ASCII.

        The JSON sink must stay encodable on a legacy stdout codec — see #342,
        where exactly this crashed a completed run after it had succeeded.
        """
        _write_pid(pid_tmpdir, 4242, 8080, "/tmp/wörkflow-→.yaml")

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        result.stdout.encode("ascii")  # must not raise
        assert json.loads(result.stdout)["running"][0]["port"] == 8080

    def test_json_keeps_the_full_recorded_timestamp(self, pid_tmpdir: Path) -> None:
        """The table's minute-precision ``Started`` must not leak into ``--json``.

        Reads the on-disk PID file directly so this pins the exact recorded
        value, not a re-derived one.
        """
        filepath = _write_pid(pid_tmpdir, 4242, 8080)
        on_disk = json.loads(filepath.read_text())["started_at"]

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        entry = json.loads(result.stdout)["running"][0]
        assert entry["started_at"] == on_disk


class TestStatusSurvivesMalformedFiles:
    """One unusable file must not cost the user every other run."""

    def test_json_still_emits_the_healthy_runs(self, pid_tmpdir: Path) -> None:
        (pid_tmpdir / "noport-1234.pid").write_text(json.dumps({"pid": 999}))
        (pid_tmpdir / "corrupt-5555.pid").write_text("{not json")
        _write_pid(pid_tmpdir, 4242, 8080)

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert [e["port"] for e in payload["running"]] == [8080]

    def test_malformed_files_are_still_on_disk_afterwards(self, pid_tmpdir: Path) -> None:
        bad = pid_tmpdir / "corrupt-5555.pid"
        bad.write_text("{not json")

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            runner.invoke(app, ["status", "--json"])

        assert bad.exists()


class TestStatusFitsAnEightyColumnTerminal:
    """Issue #405: the Dashboard column — the field the command exists to
    surface — was the one that got cropped at a default 80-column terminal.
    """

    def test_the_dashboard_url_survives_at_eighty_columns(self, pid_tmpdir: Path) -> None:
        """Production-shaped fixture: a realistic workflow stem and port.

        ``Dashboard`` is the last column, so a folded continuation line has
        only empty cells before it and squashing rejoins the URL
        contiguously. An ellipsis breaks contiguity, so this assertion would
        have failed against the pre-fix cropping behaviour.
        """
        _write_pid(pid_tmpdir, 4242, 53941, "/home/u/workflows/code-review-pipeline.yaml")

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["status"], env=_NARROW)

        assert result.exit_code == 0
        assert "http://127.0.0.1:53941" in _squash(result.output)

    def test_two_runs_both_keep_their_urls_at_eighty_columns(self, pid_tmpdir: Path) -> None:
        _write_pid(pid_tmpdir, 4242, 53941, "/home/u/workflows/code-review-pipeline.yaml")
        _write_pid(pid_tmpdir, 4243, 61200, "/home/u/workflows/another-long-workflow-name.yaml")

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["status"], env=_NARROW)

        assert result.exit_code == 0
        squashed = _squash(result.output)
        assert "http://127.0.0.1:53941" in squashed
        assert "http://127.0.0.1:61200" in squashed

    def test_started_is_rendered_to_minute_precision(self, pid_tmpdir: Path) -> None:
        filepath = _write_pid(
            pid_tmpdir, 4242, 53941, "/home/u/workflows/code-review-pipeline.yaml"
        )
        on_disk = json.loads(filepath.read_text())["started_at"]

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["status"], env=_NARROW)

        assert result.exit_code == 0
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}Z", result.output)
        assert on_disk not in result.output

    def test_one_malformed_started_at_does_not_hide_a_healthy_run(self, pid_tmpdir: Path) -> None:
        """A near-``datetime.max`` value raises ``OverflowError`` in
        ``.astimezone(UTC)``, not ``ValueError`` — a distinct failure mode
        from an unparseable string. One poisoned entry must not crash the
        whole listing and hide every other running workflow, matching the
        "one bad PID file can't cost you the rest" guarantee ``pid.py``'s
        own malformed-file handling already provides.
        """
        _write_pid(pid_tmpdir, 4242, 53941, "/home/u/workflows/code-review-pipeline.yaml")
        poisoned = pid_tmpdir / "poisoned-9999.pid"
        poisoned.write_text(
            json.dumps(
                {
                    "pid": 4243,
                    "port": 9999,
                    "workflow": "/tmp/poisoned.yaml",
                    "started_at": "9999-12-31T23:59:59-14:00",
                    "run_id": "deadbeef",
                    "stderr_log": "",
                    "stdout_log": "",
                }
            )
        )

        with patch("conductor.cli.pid.is_process_alive", return_value=True):
            result = runner.invoke(app, ["status"], env=_NARROW)

        assert result.exit_code == 0
        assert "http://127.0.0.1:53941" in _squash(result.output)

    def test_an_unfoldable_started_fallback_still_wraps_instead_of_cropping(self) -> None:
        """``_format_started_at``'s failure fallback returns the raw value
        unbounded (unlike its fixed-width happy path), which could
        reintroduce issue #405's cropping in the ``Started`` column instead
        of ``Dashboard``. ``Started`` must fold too, so a long fallback
        value wraps rather than gets cropped to an ellipsis.

        Checked directly against the constructed ``Table``, not via
        end-to-end squashing: with two folding columns on the same row,
        their wrapped continuation text interleaves and is no longer
        contiguous once whitespace is stripped, so squashing (which relies
        on ``Dashboard`` being the only folding, trailing column) cannot
        distinguish "folded" from "cropped" here. ``_print_running_list``
        only calls ``con.print(table)``, so a bare recorder standing in for
        ``con`` is enough to capture it without touching real ``Console``
        internals.
        """
        from rich.table import Table as RichTable

        class _Recorder:
            def __init__(self) -> None:
                self.printed: list[object] = []

            def print(self, *args: object, **kwargs: object) -> None:
                self.printed.extend(args)

        recorder = _Recorder()
        _print_running_list(
            [
                RunRecord(
                    run_id=_RUN_ID,
                    pid=4242,
                    workflow_path="wf.yaml",
                    workflow_name="wf",
                    started_at="bad",
                    event_log_path="",
                    port=53941,
                    mode="bg",
                    checkpoint_dir=None,
                )
            ],
            recorder,  # type: ignore[arg-type]
            show_url=True,
        )

        tables = [p for p in recorder.printed if isinstance(p, RichTable)]
        assert tables, "expected _print_running_list to print a Table"
        started_column = next(c for c in tables[0].columns if c.header == "Started")
        assert started_column.overflow == "fold"


class TestStartedColumnFormatting:
    """Direct unit tests for ``_format_started_at``'s five input shapes.

    The realistic fixture used above records ``now()`` and cannot assert a
    fixed timestamp string, so the exact-string assertions live here instead.
    """

    def test_microsecond_aware_utc(self) -> None:
        assert _format_started_at("2026-08-11T12:48:33.123456+00:00") == "2026-08-11 12:48Z"

    def test_legacy_naive_value_is_treated_as_utc(self) -> None:
        assert _format_started_at("2026-08-11T12:48:33") == "2026-08-11 12:48Z"

    def test_z_suffixed_value(self) -> None:
        assert _format_started_at("2026-08-11T12:48:33Z") == "2026-08-11 12:48Z"

    def test_non_utc_offset_is_converted_to_utc(self) -> None:
        assert _format_started_at("2026-08-11T12:48:33-04:00") == "2026-08-11 16:48Z"

    def test_unparseable_value_is_returned_verbatim(self) -> None:
        assert _format_started_at("not-a-timestamp") == "not-a-timestamp"

    def test_out_of_range_after_utc_conversion_is_returned_verbatim(self) -> None:
        """``fromisoformat`` parses this fine, but converting to UTC pushes
        it past ``datetime.max`` and ``.astimezone`` raises ``OverflowError``
        — a distinct failure mode from an unparseable string, and one the
        ``except ValueError`` alone would not catch.
        """
        value = "9999-12-31T23:59:59-14:00"
        assert _format_started_at(value) == value

    def test_none_becomes_a_question_mark(self) -> None:
        assert _format_started_at(None) == "?"

    def test_empty_string_becomes_a_question_mark(self) -> None:
        assert _format_started_at("") == "?"

    def test_non_string_becomes_a_question_mark(self) -> None:
        assert _format_started_at(12345) == "?"
