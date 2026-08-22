"""Tests for the terminal run-record primitives (MCP server plan E2 —
``conductor.fleet.records``'s ``TerminalRunRecord``).

Covers:
- ``TerminalRunRecord.to_dict`` / ``from_dict`` round-tripping, including
  tolerance of every field being absent from the parsed payload
- ``write_terminal_record`` / ``read_terminal_record`` / ``read_terminal_records``
  / ``remove_terminal_record`` round-tripping
- A record written under ``terminal/`` is invisible to ``read_run_records()``,
  ``scan_run_records()``, and ``remove_run_record_for_current_process()`` --
  all three glob ``run_records_dir()`` non-recursively (see
  ``docs/projects/mcp-server/conductor-mcp.design.md``'s *Why a subdirectory,
  not a sibling file*)
- A corrupt terminal record is skipped by ``read_terminal_records()``, not
  raised or pruned
- ``write_terminal_record`` never raises, even when the destination
  directory cannot be created/written to (e.g. read-only)
- ``read_terminal_records()`` honours its ``limit`` and returns newest-first
  by ``ended_at``
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from conductor.fleet.records import (
    RunRecord,
    TerminalRunRecord,
    read_run_records,
    read_terminal_record,
    read_terminal_records,
    remove_run_record_for_current_process,
    remove_terminal_record,
    run_records_dir,
    scan_run_records,
    terminal_records_dir,
    write_run_record,
    write_terminal_record,
)


@pytest.fixture()
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect both the run-record directory (via ``CONDUCTOR_HOME``) and
    the legacy ``.pid`` directory (via ``cli.pid.pid_dir``) to isolated
    temporary directories, mirroring ``tests/test_fleet/test_records.py``.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))

    legacy_dir = tmp_path / "legacy_runs"
    legacy_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: legacy_dir)

    return home


def _make_terminal_record(**overrides: object) -> TerminalRunRecord:
    defaults: dict[str, object] = {
        "run_id": "abc123",
        "workflow_path": "/tmp/workflow.yaml",
        "workflow_name": "workflow",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:05:00+00:00",
        "status": "success",
        "output": {"answer": "42"},
        "error_type": None,
        "error_message": None,
        "total_tokens": 1234,
        "total_cost_usd": 0.05,
        "unpriced_agent_count": 0,
        "event_log_path": "/tmp/conductor/workflow.events.jsonl",
        "bg_stderr_log": None,
        "bg_stdout_log": None,
    }
    defaults.update(overrides)
    return TerminalRunRecord(**defaults)  # type: ignore[arg-type]


class TestTerminalRunRecordRoundTrip:
    """``to_dict`` / ``from_dict`` round-tripping."""

    def test_to_dict_from_dict_round_trip(self) -> None:
        record = _make_terminal_record()
        assert TerminalRunRecord.from_dict(record.to_dict()) == record

    def test_from_dict_tolerates_every_field_absent(self) -> None:
        """A record written by a newer Conductor that dropped every field
        this version knows about must still parse, with sensible defaults
        rather than raising."""
        record = TerminalRunRecord.from_dict({})
        assert record.run_id == ""
        assert record.workflow_path == ""
        assert record.workflow_name == ""
        assert record.started_at == ""
        assert record.ended_at == ""
        assert record.status == "unknown"
        assert record.output == {}
        assert record.error_type is None
        assert record.error_message is None
        assert record.total_tokens is None
        assert record.total_cost_usd is None
        assert record.unpriced_agent_count == 0
        assert record.event_log_path == ""
        assert record.bg_stderr_log is None
        assert record.bg_stdout_log is None

    def test_from_dict_derives_workflow_name_from_path(self) -> None:
        record = TerminalRunRecord.from_dict({"workflow_path": "/some/dir/my-workflow.yaml"})
        assert record.workflow_name == "my-workflow"

    def test_from_dict_accepts_int_total_cost_usd(self) -> None:
        """JSON has no int/float distinction; a whole-dollar cost may
        round-trip as an int."""
        record = TerminalRunRecord.from_dict({"total_cost_usd": 3})
        assert record.total_cost_usd == 3.0

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("run_id", 123),
            ("workflow_path", 123),
            ("status", 123),
            ("output", "not-a-dict"),
            ("error_type", 123),
            ("total_tokens", "not-an-int"),
            ("total_tokens", True),
            ("total_cost_usd", "not-a-number"),
            ("total_cost_usd", True),
            ("unpriced_agent_count", "not-an-int"),
            ("unpriced_agent_count", True),
            ("bg_stderr_log", 123),
        ],
    )
    def test_from_dict_rejects_wrong_typed_present_fields(self, field: str, value: object) -> None:
        with pytest.raises(ValueError):
            TerminalRunRecord.from_dict({field: value})


class TestWriteReadTerminalRecord:
    """``write_terminal_record`` / ``read_terminal_record`` round-tripping."""

    def test_writes_atomically_and_reads_back(self, fleet_env: Path) -> None:
        record = _make_terminal_record()
        path = write_terminal_record(record)

        assert path == terminal_records_dir() / "abc123.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data == record.to_dict()
        # No leftover temp file.
        assert list(terminal_records_dir().glob("*.tmp")) == []

        read_back = read_terminal_record("abc123")
        assert read_back == record

    def test_read_terminal_record_missing_returns_none(self, fleet_env: Path) -> None:
        assert read_terminal_record("nonexistent") is None

    def test_read_terminal_record_rejects_unsafe_run_id(self, fleet_env: Path) -> None:
        assert read_terminal_record("../etc/passwd") is None
        assert read_terminal_record("a/b") is None

    def test_write_terminal_record_rejects_unsafe_run_id(self, fleet_env: Path) -> None:
        record = _make_terminal_record(run_id="../escape")
        assert write_terminal_record(record) is None
        # Nothing was ever written under a name derived from the payload.
        assert list(terminal_records_dir().glob("*")) == []

    def test_terminal_records_dir_is_a_subdirectory_of_run_records_dir(
        self, fleet_env: Path
    ) -> None:
        assert terminal_records_dir() == run_records_dir() / "terminal"
        assert terminal_records_dir().is_dir()

    def test_resume_replaces_rather_than_duplicates(self, fleet_env: Path) -> None:
        """Writing a second terminal record for the same ``run_id`` (e.g. a
        resumed run's own terminal write) replaces the file rather than
        creating a second one."""
        write_terminal_record(_make_terminal_record(status="failed"))
        write_terminal_record(_make_terminal_record(status="success"))

        assert len(list(terminal_records_dir().glob("*.json"))) == 1
        record = read_terminal_record("abc123")
        assert record is not None
        assert record.status == "success"


class TestTerminalRecordInvisibleToLiveRecordFunctions:
    """A terminal record must never be listed, pruned, or matched by any of
    the three functions that glob ``run_records_dir()`` non-recursively."""

    def test_invisible_to_read_run_records(self, fleet_env: Path) -> None:
        write_terminal_record(_make_terminal_record(run_id="deadbeef"))
        assert read_run_records() == []

    def test_invisible_to_scan_run_records(self, fleet_env: Path) -> None:
        write_terminal_record(_make_terminal_record(run_id="deadbeef"))
        assert scan_run_records() == []

    def test_invisible_to_remove_run_record_for_current_process(self, fleet_env: Path) -> None:
        """A live record for the *current* process coexists with a terminal
        record of the same ``run_id`` (e.g. written moments earlier by the
        same run in its own ``finally`` block); removing the live record
        must not also consume/clobber the terminal one, and the terminal
        record must survive untouched."""
        write_terminal_record(_make_terminal_record(run_id="deadbeef"))
        write_run_record(
            RunRecord(
                run_id="deadbeef",
                pid=os.getpid(),
                workflow_path="/tmp/workflow.yaml",
                workflow_name="workflow",
                started_at="2026-01-01T00:00:00+00:00",
                event_log_path="/tmp/conductor/workflow.events.jsonl",
                port=None,
                mode="fg",
                checkpoint_dir=None,
            )
        )

        removed = remove_run_record_for_current_process()

        assert removed is True
        assert read_run_records() == []
        # The terminal record is untouched -- a different directory that
        # `remove_run_record_for_current_process`'s own non-recursive glob
        # never lists.
        assert read_terminal_record("deadbeef") is not None


class TestReadTerminalRecordsTolerance:
    """``read_terminal_records()`` skips bad files rather than raising or pruning."""

    def test_corrupt_terminal_record_is_skipped_not_raised(self, fleet_env: Path) -> None:
        write_terminal_record(_make_terminal_record(run_id="good1"))
        bad_path = terminal_records_dir() / "corrupt.json"
        bad_path.write_text("{not valid json")

        records = read_terminal_records()

        assert [r.run_id for r in records] == ["good1"]
        # Unlike `read_run_records()`, this is a read-only query: the
        # corrupt file is left in place, not pruned.
        assert bad_path.exists()

    def test_record_with_mismatched_run_id_is_skipped(self, fleet_env: Path) -> None:
        record = _make_terminal_record(run_id="claimed-other-id")
        path = terminal_records_dir() / "actual-filename.json"
        path.write_text(json.dumps(record.to_dict()))

        assert read_terminal_records() == []
        assert read_terminal_record("actual-filename") is None


class TestReadTerminalRecordsOrderingAndLimit:
    def test_newest_first_by_ended_at(self, fleet_env: Path) -> None:
        write_terminal_record(
            _make_terminal_record(run_id="oldest", ended_at="2026-01-01T00:00:00+00:00")
        )
        write_terminal_record(
            _make_terminal_record(run_id="newest", ended_at="2026-01-03T00:00:00+00:00")
        )
        write_terminal_record(
            _make_terminal_record(run_id="middle", ended_at="2026-01-02T00:00:00+00:00")
        )

        records = read_terminal_records()

        assert [r.run_id for r in records] == ["newest", "middle", "oldest"]

    def test_limit_bounds_the_returned_list(self, fleet_env: Path) -> None:
        for i in range(5):
            write_terminal_record(
                _make_terminal_record(run_id=f"run{i}", ended_at=f"2026-01-0{i + 1}T00:00:00+00:00")
            )

        records = read_terminal_records(limit=2)

        assert len(records) == 2
        assert [r.run_id for r in records] == ["run4", "run3"]

    def test_no_limit_returns_everything(self, fleet_env: Path) -> None:
        for i in range(3):
            write_terminal_record(_make_terminal_record(run_id=f"run{i}"))

        assert len(read_terminal_records()) == 3


class TestRemoveTerminalRecord:
    def test_removes_existing_record(self, fleet_env: Path) -> None:
        write_terminal_record(_make_terminal_record(run_id="abc123"))
        assert remove_terminal_record("abc123") is True
        assert read_terminal_record("abc123") is None

    def test_returns_false_for_missing_record(self, fleet_env: Path) -> None:
        assert remove_terminal_record("nonexistent") is False

    def test_returns_false_for_unsafe_run_id(self, fleet_env: Path) -> None:
        assert remove_terminal_record("../escape") is False


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="Read-only directory permissions aren't meaningfully enforced "
    "for the current user on Windows, or when running as root.",
)
class TestWriteTerminalRecordNeverRaises:
    """``write_terminal_record`` never raises, even on I/O failure."""

    def test_never_raises_on_read_only_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        runs_dir = home / "runs"
        runs_dir.mkdir()
        # Make the parent read-only so `terminal_records_dir()`'s own
        # `mkdir` (creating the `terminal/` subdirectory) fails with
        # `PermissionError`.
        runs_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))

        try:
            result = write_terminal_record(_make_terminal_record())
        finally:
            # Restore write permission so tmp_path cleanup can proceed.
            runs_dir.chmod(stat.S_IRWXU)

        assert result is None
