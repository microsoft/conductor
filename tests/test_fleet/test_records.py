"""Tests for the fleet manager run-record primitives (``conductor.fleet.records``).

Covers:
- ``write_run_record`` / ``read_run_records`` / ``read_run_record`` round-tripping
- Stale-record pruning (dead PID) and ``kill -9``-style orphan self-pruning
- Atomic writes: concurrent writers never yield a torn read
- Legacy port-keyed ``.pid`` file tolerance, surfaced with ``mode="bg"``
- Tolerant handling of corrupt / vanished / unparseable files, including
  invalid UTF-8, unlink failures during pruning, oversized ``pid`` integers
  (both a plain out-of-range ``int`` and a JSON literal that exceeds
  CPython's integer-string conversion digit limit), and JSON payloads that
  falsely claim a different ``run_id`` than their own filename
- ``CONDUCTOR_HOME`` redirection of the run-record directory, while legacy
  ``.pid`` files are still read from the un-redirected default ``pid_dir()``
- Strict validation of ``pid`` / ``mode`` / path-safe ``run_id`` in
  ``RunRecord.from_dict`` and the keyed read/write/remove primitives,
  including distinguishing a genuinely missing field from an explicit but
  invalid one (e.g. ``run_id: []``, ``mode: null``)
- Pruning never deletes a record that was concurrently replaced under the
  same ``run_id``, even when the replacement lands at the last possible
  instant before the deleting rename
- ``remove_run_record`` / ``remove_run_record_for_current_process`` report
  success only when a removal actually occurred
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conductor.cli import pid as cli_pid
from conductor.fleet.records import (
    RunRecord,
    is_valid_run_id,
    read_run_record,
    read_run_records,
    remove_run_record,
    remove_run_record_for_current_process,
    run_records_dir,
    write_run_record,
)


def _write_legacy_pid_file(
    pid: int, port: int, workflow_path: str, *, run_id: str = "", log_file: str = ""
) -> Path:
    """Write a legacy port-keyed ``.pid`` file directly.

    Replicates the schema of the now-removed ``cli.pid.write_pid_file``
    (Fleet Manager E2-T4 deleted it — every run path writes a ``run_id``-
    keyed record via ``conductor.fleet.records.write_run_record`` instead).
    This module's ``TestLegacyPidFileTolerance`` still needs to construct a
    pre-upgrade-shaped ``.pid`` file to exercise ``read_run_records()``'s
    legacy-tolerance behavior, so it builds one directly rather than via
    the deleted function.

    Calls ``cli_pid.pid_dir()`` (module attribute lookup, not a bound
    import) so this picks up the ``fleet_env`` fixture's
    ``monkeypatch.setattr("conductor.cli.pid.pid_dir", ...)`` redirection —
    a plain ``from conductor.cli.pid import pid_dir`` at module import time
    would bind the pre-patch function object instead.
    """
    workflow_name = Path(workflow_path).stem
    filepath = cli_pid.pid_dir() / f"{workflow_name}-{port}.pid"
    data = {
        "pid": pid,
        "port": port,
        "workflow": str(workflow_path),
        "started_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "log_file": log_file,
    }
    filepath.write_text(json.dumps(data, indent=2))
    return filepath


@pytest.fixture()
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect both the new run-record directory (via ``CONDUCTOR_HOME``)
    and the legacy ``.pid`` directory (via ``cli.pid.pid_dir``) to isolated
    temporary directories."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))

    legacy_dir = tmp_path / "legacy_runs"
    legacy_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: legacy_dir)

    return home


def _make_record(**overrides: object) -> RunRecord:
    defaults: dict[str, object] = {
        "run_id": "abc123",
        "pid": os.getpid(),
        "workflow_path": "/tmp/workflow.yaml",
        "workflow_name": "workflow",
        "started_at": "2026-01-01T00:00:00+00:00",
        "event_log_path": "/tmp/conductor/workflow.events.jsonl",
        "port": 8080,
        "mode": "bg",
        "checkpoint_dir": "/tmp/conductor/checkpoints",
    }
    defaults.update(overrides)
    return RunRecord(**defaults)  # type: ignore[arg-type]


class TestWriteRunRecord:
    """Tests for ``write_run_record``."""

    def test_writes_atomically_via_temp_and_replace(self, fleet_env: Path) -> None:
        record = _make_record()
        path = write_run_record(record)

        assert path == run_records_dir() / "abc123.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data == record.to_dict()
        # No leftover temp file.
        assert list(run_records_dir().glob("*.tmp")) == []

    def test_honors_conductor_home(self, fleet_env: Path) -> None:
        write_run_record(_make_record())
        assert (fleet_env / "runs" / "abc123.json").exists()

    def test_overwrite_replaces_existing_record(self, fleet_env: Path) -> None:
        write_run_record(_make_record(pid=111))
        write_run_record(_make_record(pid=222))
        data = json.loads((run_records_dir() / "abc123.json").read_text())
        assert data["pid"] == 222


class TestRoundTrip:
    """Round-trip write/read for both the bulk and single-record readers."""

    def test_read_run_records_returns_written_record(self, fleet_env: Path) -> None:
        write_run_record(_make_record(pid=os.getpid()))
        records = read_run_records()
        assert len(records) == 1
        assert records[0].run_id == "abc123"
        assert records[0].pid == os.getpid()
        assert records[0].mode == "bg"
        assert records[0].port == 8080

    def test_read_run_record_returns_matching_record(self, fleet_env: Path) -> None:
        write_run_record(_make_record(pid=os.getpid(), run_id="abcdef01"))
        record = read_run_record("abcdef01")
        assert record is not None
        assert record.run_id == "abcdef01"
        assert record.pid == os.getpid()

    def test_read_run_record_returns_none_for_missing_run_id(self, fleet_env: Path) -> None:
        assert read_run_record("does-not-exist") is None

    def test_read_run_records_returns_multiple(self, fleet_env: Path) -> None:
        write_run_record(_make_record(run_id="00000001", pid=os.getpid()))
        write_run_record(_make_record(run_id="00000002", pid=os.getpid()))
        records = read_run_records()
        assert {r.run_id for r in records} == {"00000001", "00000002"}


class TestStalePruning:
    """Dead-PID records are pruned; live ones survive."""

    def test_stale_record_is_pruned(self, fleet_env: Path) -> None:
        # PID 99999999 is assumed not to be a live process on the test host.
        write_run_record(_make_record(pid=99999999))
        records = read_run_records()
        assert records == []
        assert not (run_records_dir() / "abc123.json").exists()

    def test_kill_minus_9_orphan_self_prunes(self, fleet_env: Path) -> None:
        """A record whose owning process was ``kill -9``'d (so nothing ever
        called ``remove_run_record_for_current_process``) must be cleaned up
        by the next reader rather than lingering forever."""
        write_run_record(_make_record(pid=99999999, run_id="deadbeef"))
        assert (run_records_dir() / "deadbeef.json").exists()

        records = read_run_records()

        assert records == []
        assert not (run_records_dir() / "deadbeef.json").exists()

    def test_live_record_is_not_pruned(self, fleet_env: Path) -> None:
        write_run_record(_make_record(pid=os.getpid()))
        records = read_run_records()
        assert len(records) == 1
        assert (run_records_dir() / "abc123.json").exists()


class TestConcurrentWrites:
    """Concurrent atomic writers must never produce a torn/partial read."""

    def test_concurrent_writers_never_yield_torn_read(self, fleet_env: Path) -> None:
        errors: list[Exception] = []
        stop = threading.Event()
        run_ids = [f"{i:08x}" for i in range(4)]
        expected_keys = set(_make_record().to_dict().keys())

        def _writer(run_id: str) -> None:
            try:
                for _ in range(50):
                    write_run_record(_make_record(run_id=run_id, pid=os.getpid()))
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def _reader() -> None:
            # Deliberately bypass the tolerant `read_run_records()` scanner
            # (which would swallow a torn read as a "corrupt" JSON decode
            # error) and instead parse each target file directly, so a torn
            # write actually fails this test via `json.loads` raising
            # `JSONDecodeError`. Only the file's *disappearance* — an
            # expected race with `os.replace` — is tolerated.
            try:
                while not stop.is_set():
                    for run_id in run_ids:
                        path = run_records_dir() / f"{run_id}.json"
                        try:
                            text = path.read_text()
                        except FileNotFoundError:
                            continue
                        data = json.loads(text)
                        assert isinstance(data, dict)
                        assert set(data.keys()) == expected_keys
                        assert data["run_id"] == run_id
                        assert isinstance(data["pid"], int)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        writers = [threading.Thread(target=_writer, args=(run_id,)) for run_id in run_ids]
        reader = threading.Thread(target=_reader)

        reader.start()
        for w in writers:
            w.start()
        for w in writers:
            w.join()
        stop.set()
        reader.join()

        assert errors == []


class TestLegacyPidFileTolerance:
    """Legacy port-keyed ``.pid`` files (pre-upgrade ``--web-bg`` runs) are
    surfaced as ``RunRecord`` instances classified ``mode="bg"``."""

    def test_legacy_pid_file_is_read_and_classified_bg(self, fleet_env: Path) -> None:
        _write_legacy_pid_file(os.getpid(), 9090, "/tmp/legacy-workflow.yaml", run_id="legacy-run")

        records = read_run_records()

        assert len(records) == 1
        record = records[0]
        assert record.mode == "bg"
        assert record.pid == os.getpid()
        assert record.port == 9090
        assert record.run_id == "legacy-run"
        assert record.workflow_path == "/tmp/legacy-workflow.yaml"

    def test_legacy_pid_file_missing_run_id_defaults_gracefully(self, fleet_env: Path) -> None:
        # _write_legacy_pid_file always writes a run_id key (possibly empty);
        # simulate a truly pre-``run_id`` legacy file by writing raw JSON
        # without it.
        legacy_path = cli_pid.pid_dir() / "old-workflow-9091.pid"
        legacy_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "port": 9091,
                    "workflow": "/tmp/old-workflow.yaml",
                    "started_at": "2025-01-01T00:00:00",
                }
            )
        )

        records = read_run_records()

        assert len(records) == 1
        record = records[0]
        assert record.mode == "bg"
        assert record.run_id == ""
        assert record.event_log_path == ""
        assert record.workflow_path == "/tmp/old-workflow.yaml"

    def test_legacy_dir_ignores_conductor_home(
        self, fleet_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legacy ``.pid`` files must be read from the real ``pid_dir()``
        (unredirected), not from the ``CONDUCTOR_HOME``-aware run-record
        directory — the two diverge when ``CONDUCTOR_HOME`` is set."""
        # Point CONDUCTOR_HOME somewhere new, but keep the legacy dir fixed
        # via the fleet_env fixture's monkeypatch on pid_dir().
        other_home = tmp_path / "other-home"
        other_home.mkdir()
        monkeypatch.setenv("CONDUCTOR_HOME", str(other_home))

        _write_legacy_pid_file(os.getpid(), 9092, "/tmp/legacy2.yaml", run_id="legacy-run-2")

        records = read_run_records()

        assert len(records) == 1
        assert records[0].run_id == "legacy-run-2"
        # The new run-record dir under the redirected CONDUCTOR_HOME is empty.
        assert list((other_home / "runs").glob("*.json")) == []


class TestCorruptAndVanishedFiles:
    """Corrupt, unreadable, or mid-scan-vanished files must never crash a
    reader."""

    def test_corrupt_json_is_removed_not_raised(self, fleet_env: Path) -> None:
        bad_path = run_records_dir() / "bad.json"
        bad_path.write_text("not json{{{")

        records = read_run_records()

        assert records == []
        assert not bad_path.exists()

    def test_record_missing_pid_is_removed_not_raised(self, fleet_env: Path) -> None:
        bad_path = run_records_dir() / "no-pid.json"
        bad_path.write_text(json.dumps({"run_id": "no-pid", "workflow_path": "/tmp/x.yaml"}))

        records = read_run_records()

        assert records == []
        assert not bad_path.exists()

    def test_invalid_utf8_is_removed_not_raised(self, fleet_env: Path) -> None:
        """A file containing bytes that aren't valid UTF-8 must be treated
        as corrupt (and pruned), not crash the reader with an unhandled
        ``UnicodeDecodeError``."""
        bad_path = run_records_dir() / "badbytes.json"
        bad_path.write_bytes(b'{"pid": 123, "run_id": "\xff\xfe"')

        records = read_run_records()

        assert records == []
        assert not bad_path.exists()

    def test_file_vanishing_mid_scan_does_not_raise(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_run_record(_make_record(pid=os.getpid()))

        real_read_text = Path.read_text

        def _vanish_then_raise(self: Path, *args: object, **kwargs: object) -> str:
            if self.name == "abc123.json":
                self.unlink(missing_ok=True)
                raise FileNotFoundError(self)
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", _vanish_then_raise)

        records = read_run_records()

        assert records == []

    def test_deeply_nested_malformed_json_is_pruned_not_raised(self, fleet_env: Path) -> None:
        """A ``RecursionError`` from CPython's recursive-descent JSON
        decoder (raised for a sufficiently deeply-nested payload before it
        ever gets a chance to raise its usual ``JSONDecodeError``) is a
        ``RuntimeError`` subclass, not a ``ValueError`` -- it must still be
        classified as corrupt content and pruned, not escape the tolerant
        bulk reader."""
        bad_path = run_records_dir() / "deepnest.json"
        bad_path.write_text("[" * 100_000)

        records = read_run_records()

        assert records == []
        assert not bad_path.exists()


class TestReadRunRecordTransientFailures:
    """``read_run_record`` must not delete a record it merely cannot parse
    yet, since a concurrent atomic write is the expected reason."""

    def test_transient_parse_failure_returns_none_without_deleting(self, fleet_env: Path) -> None:
        path = run_records_dir() / "12345678.json"
        path.write_text('{"run_id": "12345678", "pid": ')  # truncated JSON

        result = read_run_record("12345678")

        assert result is None
        # The file must still be present — a concurrent atomic writer is the
        # expected explanation, not corruption.
        assert path.exists()

    def test_unreadable_file_is_left_intact_not_deleted(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient permission/I-O error while reading is not evidence of
        corruption; the file must be left in place either by the bulk
        scanner or the single-key lookup."""
        write_run_record(_make_record(run_id="0badc0de", pid=os.getpid()))

        real_read_text = Path.read_text

        def _deny(self: Path, *args: object, **kwargs: object) -> str:
            if self.name == "0badc0de.json":
                raise PermissionError(self)
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", _deny)

        assert read_run_record("0badc0de") is None
        assert read_run_records() == []
        assert (run_records_dir() / "0badc0de.json").exists()


class TestBulkScanUnlinkFailureDoesNotRaise:
    """Pruning must not let a filesystem-level ``unlink`` failure escape
    ``read_run_records()`` (e.g. a read-only filesystem or permission
    error deleting a stale/corrupt record).

    The restoration path is deliberately non-clobbering: it recreates the
    original file via ``os.link`` (which never overwrites an existing
    destination) rather than an unconditional ``os.replace``, so a record
    concurrently written under the same name is never destroyed by a
    restore (see ``TestConcurrentReplacementDuringPruneRestore`` below). A
    genuine no-clobber move has no single-syscall equivalent in ``os`` --
    it takes a ``link`` (create the new name) followed by an ``unlink``
    (drop the quarantine name) -- so in the pathological scenario these
    tests simulate, where *every* unlink for the affected file is denied,
    that second cleanup unlink also fails and a harmless orphaned
    ``.prune-*`` hard link (same inode/content, zero data loss) can remain
    alongside the restored file. That is an acceptable trade-off for never
    clobbering a live record; the tests here assert the guarantees that
    matter: no crash, and the original content is never lost.
    """

    def test_unlink_failure_while_pruning_stale_record_does_not_raise(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_run_record(_make_record(pid=99999999))  # stale -> would be pruned

        real_unlink = Path.unlink

        def _deny_unlink(self: Path, *args: object, **kwargs: object) -> None:
            # The record is renamed into a private quarantine path (e.g.
            # ``.abc123.json.prune-<hex>``) before the final unlink, so
            # match on the original stem rather than the exact filename.
            if "abc123" in self.name:
                raise PermissionError(self)
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _deny_unlink)

        records = read_run_records()

        assert records == []
        # The file is still there (restored to its original name after the
        # failed deletion) because unlink failed — the call did not raise,
        # and the original content was never lost.
        restored = json.loads((run_records_dir() / "abc123.json").read_text())
        assert restored["pid"] == 99999999

    def test_unlink_failure_while_removing_corrupt_record_does_not_raise(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad_path = run_records_dir() / "bad.json"
        bad_path.write_text("not json{{{")

        real_unlink = Path.unlink

        def _deny_unlink(self: Path, *args: object, **kwargs: object) -> None:
            if "bad" in self.name:
                raise PermissionError(self)
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _deny_unlink)

        records = read_run_records()

        assert records == []
        assert bad_path.exists()
        assert bad_path.read_text() == "not json{{{"


class TestIsValidRunId:
    """``is_valid_run_id`` is the single, exported source of truth for what
    a run_id must look like to be safe as a run-record filename component
    -- shared with ``cli.bg_runner._peek_resume_run_id`` so the parent's
    resume-launch id prediction can't drift from what the child's own
    ``write_run_record`` call will actually accept (see Fleet Manager E2's
    resume-run-id centralization fix)."""

    def test_accepts_typical_hex_run_id(self) -> None:
        assert is_valid_run_id("deadbeef") is True

    def test_accepts_non_hex_alphanumeric_with_dash_and_underscore(self) -> None:
        assert is_valid_run_id("custom-run_ID-42") is True

    def test_rejects_traversal_sequence(self) -> None:
        assert is_valid_run_id("../escape") is False

    def test_rejects_path_separator(self) -> None:
        assert is_valid_run_id("foo/bar") is False

    def test_rejects_empty_string(self) -> None:
        assert is_valid_run_id("") is False

    def test_rejects_dot(self) -> None:
        assert is_valid_run_id("run.id") is False

    def test_write_run_record_and_is_valid_run_id_agree(self, fleet_env: Path) -> None:
        """The exported predicate must match what ``write_run_record`` itself
        enforces -- a run_id it accepts must round-trip through
        ``read_run_record`` too."""
        run_id = "custom-run_ID-42"
        assert is_valid_run_id(run_id) is True
        record = _make_record(run_id=run_id, pid=os.getpid())
        write_run_record(record)
        assert read_run_record(run_id) is not None


class TestPathTraversalRejected:
    """``run_id`` is interpolated directly into a filename; a value like
    ``"../../target"`` must never let a caller read, write, or delete a
    file outside :func:`run_records_dir`."""

    def test_write_run_record_rejects_traversal_run_id(self, fleet_env: Path) -> None:
        outside_target = run_records_dir().parent / "target.json"
        record = _make_record(run_id="../../target", pid=os.getpid())

        with pytest.raises(ValueError, match="Invalid run_id"):
            write_run_record(record)

        assert not outside_target.exists()

    def test_read_run_record_rejects_traversal_run_id(self, fleet_env: Path) -> None:
        # Plant a file at the location traversal would target, and confirm
        # the lookup never reaches it.
        outside_target = run_records_dir().parent / "target.json"
        outside_target.write_text(json.dumps(_make_record().to_dict()))

        assert read_run_record("../target") is None

    def test_remove_run_record_rejects_traversal_run_id(self, fleet_env: Path) -> None:
        outside_target = run_records_dir().parent / "target.json"
        outside_target.write_text(json.dumps(_make_record().to_dict()))

        assert remove_run_record("../target") is False
        assert outside_target.exists()


class TestRemoveRunRecord:
    """Tests for ``remove_run_record`` / ``remove_run_record_for_current_process``."""

    def test_remove_run_record_removes_existing(self, fleet_env: Path) -> None:
        write_run_record(_make_record())
        assert remove_run_record("abc123") is True
        assert not (run_records_dir() / "abc123.json").exists()

    def test_remove_run_record_returns_false_when_absent(self, fleet_env: Path) -> None:
        assert remove_run_record("nope") is False

    def test_remove_for_current_process_removes_own_record(self, fleet_env: Path) -> None:
        write_run_record(_make_record(pid=os.getpid()))
        assert remove_run_record_for_current_process() is True
        assert not (run_records_dir() / "abc123.json").exists()

    def test_remove_for_current_process_returns_false_when_no_match(self, fleet_env: Path) -> None:
        write_run_record(_make_record(pid=99999999))
        assert remove_run_record_for_current_process() is False

    def test_remove_for_current_process_leaves_other_records(self, fleet_env: Path) -> None:
        write_run_record(_make_record(run_id="000000aa", pid=os.getpid()))
        write_run_record(_make_record(run_id="000000bb", pid=99999999))
        remove_run_record_for_current_process()
        remaining = list(run_records_dir().glob("*.json"))
        assert len(remaining) == 1
        assert remaining[0].name == "000000bb.json"


class TestRemoveRunRecordPropagatesDeletionStatus:
    """``remove_run_record`` / ``remove_run_record_for_current_process`` must
    report success only when a removal actually occurred -- not merely when
    the record existed at the start of the call. A suppressed unlink error,
    or a concurrent replacement detected and restored by
    ``_delete_if_unchanged``, must surface as ``False``."""

    def test_remove_run_record_returns_false_when_unlink_fails(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_run_record(_make_record())
        filepath = run_records_dir() / "abc123.json"
        assert filepath.exists()

        real_unlink = Path.unlink

        def _deny_unlink(self: Path, *args: object, **kwargs: object) -> None:
            if self.name == "abc123.json":
                raise PermissionError(self)
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _deny_unlink)

        # The file existed, but the removal itself failed -- this must not
        # be reported as a successful removal.
        assert remove_run_record("abc123") is False
        assert filepath.exists()

    def test_remove_for_current_process_returns_false_when_unlink_fails(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_run_record(_make_record(pid=os.getpid()))
        filepath = run_records_dir() / "abc123.json"

        real_unlink = Path.unlink

        def _deny_unlink(self: Path, *args: object, **kwargs: object) -> None:
            if "abc123" in self.name:
                raise PermissionError(self)
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _deny_unlink)

        assert remove_run_record_for_current_process() is False
        # Restored to its original path after the failed deletion -- not
        # left behind as an orphaned quarantine file, and not silently lost.
        assert filepath.exists()

    def test_remove_for_current_process_returns_false_on_concurrent_replacement(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the matched record is concurrently replaced (by the same
        process re-writing its own record, e.g. a mid-shutdown periodic
        checkpoint write) between being read and being deleted, the
        replacement must survive and the call must report ``False`` rather
        than claiming a removal that didn't happen."""
        write_run_record(_make_record(pid=os.getpid()))

        real_read_text = Path.read_text

        def _replace_after_read(self: Path, *args: object, **kwargs: object) -> str:
            text = real_read_text(self, *args, **kwargs)
            if self.name == "abc123.json":
                write_run_record(_make_record(pid=os.getpid(), started_at="replaced"))
            return text

        monkeypatch.setattr(Path, "read_text", _replace_after_read)

        assert remove_run_record_for_current_process() is False
        surviving = json.loads((run_records_dir() / "abc123.json").read_text())
        assert surviving["started_at"] == "replaced"


class TestRunRecordToFromDict:
    """Direct unit coverage of ``RunRecord.to_dict`` / ``from_dict``."""

    def test_to_dict_has_exactly_nine_fields(self) -> None:
        record = _make_record()
        data = record.to_dict()
        assert set(data.keys()) == {
            "run_id",
            "pid",
            "workflow_path",
            "workflow_name",
            "started_at",
            "event_log_path",
            "port",
            "mode",
            "checkpoint_dir",
        }

    def test_from_dict_round_trips_full_record(self) -> None:
        record = _make_record()
        assert RunRecord.from_dict(record.to_dict()) == record

    def test_from_dict_defaults_missing_optional_fields(self) -> None:
        record = RunRecord.from_dict({"pid": 123, "workflow": "/tmp/x.yaml"})
        assert record.pid == 123
        assert record.run_id == ""
        assert record.mode == "bg"
        assert record.event_log_path == ""
        assert record.port is None
        assert record.workflow_path == "/tmp/x.yaml"

    def test_from_dict_raises_on_missing_pid(self) -> None:
        with pytest.raises((ValueError, KeyError, TypeError)):
            RunRecord.from_dict({"workflow_path": "/tmp/x.yaml"})


class TestFromDictUnsafePidRejected:
    """``pid`` values that could misdirect a signal or a foreground-stop
    confirmation must be rejected rather than silently coerced."""

    def test_rejects_zero_pid(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            RunRecord.from_dict({"pid": 0})

    def test_rejects_negative_pid(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            RunRecord.from_dict({"pid": -1})

    def test_rejects_boolean_pid(self) -> None:
        with pytest.raises(ValueError, match="boolean"):
            RunRecord.from_dict({"pid": True})

    def test_rejects_non_integral_float_pid(self) -> None:
        with pytest.raises(ValueError, match="integral"):
            RunRecord.from_dict({"pid": 123.5})

    def test_rejects_overflowing_float_pid_without_raising_overflow_error(self) -> None:
        """A pid value like ``1e10000`` overflows a plain ``int()`` call
        with ``OverflowError`` — this must surface as ``ValueError`` (the
        exception type every tolerant reader already catches), not escape
        uncaught."""
        with pytest.raises(ValueError):
            RunRecord.from_dict({"pid": 1e10000})

    def test_accepts_integral_float_pid(self) -> None:
        record = RunRecord.from_dict({"pid": 123.0})
        assert record.pid == 123
        assert isinstance(record.pid, int)

    def test_rejects_non_numeric_pid(self) -> None:
        with pytest.raises(ValueError, match="int"):
            RunRecord.from_dict({"pid": "123"})

    def test_overflowing_pid_in_a_record_file_is_pruned_not_raised(self, fleet_env: Path) -> None:
        """The same overflow must not escape the tolerant bulk reader
        either."""
        bad_path = run_records_dir() / "overflow.json"
        bad_path.write_text(json.dumps({"pid": 1e10000, "run_id": "overflow"}))

        records = read_run_records()

        assert records == []
        assert not bad_path.exists()

    def test_rejects_plain_int_pid_beyond_os_kill_range(self) -> None:
        """A JSON-parsed plain ``int`` (not a float) can still be too large
        for ``os.kill`` on POSIX, which parses its ``pid`` argument as a C
        ``int`` and raises an uncaught ``OverflowError`` -- well past any
        ``float``-specific overflow handling. This must be rejected at
        parse time rather than surfacing as an unhandled exception deep
        inside ``is_process_alive``."""
        with pytest.raises(ValueError, match="out of range"):
            RunRecord.from_dict({"pid": 2**33})

    def test_plain_int_overflowing_pid_in_a_record_file_is_pruned_not_raised(
        self, fleet_env: Path
    ) -> None:
        """Regression test: a plain (non-float) oversized ``pid`` integer
        must not crash the tolerant bulk reader with an uncaught
        ``OverflowError`` from ``os.kill``."""
        bad_path = run_records_dir() / "hugeplainpid.json"
        bad_path.write_text(json.dumps({"pid": 2**33, "run_id": "hugeplainpid"}))

        records = read_run_records()

        assert records == []
        assert not bad_path.exists()

    def test_json_literal_exceeding_int_string_conversion_limit_is_pruned_not_raised(
        self, fleet_env: Path
    ) -> None:
        """A ``pid`` value with thousands of digits triggers CPython's
        integer-string conversion length guard (hardening for
        CVE-2020-10735) *inside* ``json.loads`` itself, raising a plain
        ``ValueError`` distinct from ``json.JSONDecodeError``. This must be
        classified as corrupt content and pruned, not escape the tolerant
        bulk reader."""
        bad_path = run_records_dir() / "digitlimit.json"
        huge_digits = "9" * 5000
        bad_path.write_text(f'{{"pid": {huge_digits}, "run_id": "digitlimit"}}')

        records = read_run_records()

        assert records == []
        assert not bad_path.exists()


class TestPlatformSpecificPidBounds:
    """``_coerce_pid``'s upper bound must match the platform's actual PID
    range: POSIX ``os.kill`` parses ``pid`` as a signed 32-bit C ``int``
    (max ``2**31 - 1``), but Windows PIDs are an unsigned 32-bit ``DWORD``
    (max ``2**32 - 1``) -- a bound fixed at the POSIX ceiling would wrongly
    reject a real, if unlikely, high-numbered Windows PID."""

    def test_posix_rejects_pid_above_signed_32_bit_range(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("conductor.fleet.records.sys.platform", "linux")
        with pytest.raises(ValueError, match="out of range"):
            RunRecord.from_dict({"pid": 2**31})

    def test_posix_accepts_pid_at_signed_32_bit_max(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.fleet.records.sys.platform", "linux")
        record = RunRecord.from_dict({"pid": 2**31 - 1})
        assert record.pid == 2**31 - 1

    def test_windows_accepts_pid_above_signed_32_bit_range(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A PID between ``2**31`` and ``2**32 - 1`` is invalid on POSIX but
        is a legal (if unlikely) Windows ``DWORD`` PID and must not be
        rejected there."""
        monkeypatch.setattr("conductor.fleet.records.sys.platform", "win32")
        record = RunRecord.from_dict({"pid": 2**31})
        assert record.pid == 2**31

    def test_windows_accepts_pid_at_unsigned_32_bit_max(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("conductor.fleet.records.sys.platform", "win32")
        record = RunRecord.from_dict({"pid": 2**32 - 1})
        assert record.pid == 2**32 - 1

    def test_windows_rejects_pid_beyond_unsigned_32_bit_range(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("conductor.fleet.records.sys.platform", "win32")
        with pytest.raises(ValueError, match="out of range"):
            RunRecord.from_dict({"pid": 2**32})


class TestFromDictModeCoercion:
    """``mode`` drives D1's stop-confirmation gating, so a *malformed* value
    must be rejected — but an *unrecognised* one must not be, because a
    rejection routes into the corrupt-and-delete path that never consults
    liveness."""

    def test_unknown_mode_normalises_to_bg(self) -> None:
        record = RunRecord.from_dict({"pid": 123, "mode": "not-a-real-mode"})
        assert record.mode == "bg"

    def test_an_unknown_foreground_variant_fails_closed_to_fg(self) -> None:
        """``mode`` also arms D1's stop confirmation, so an unknown ``fg-*``
        must not fold into ``bg`` -- that would keep the record intact and
        silently drop the prompt guarding it."""
        record = RunRecord.from_dict({"pid": 123, "mode": "fg-tui"})
        assert record.mode == "fg"

    def test_rejects_non_string_mode(self) -> None:
        with pytest.raises(ValueError, match="invalid mode"):
            RunRecord.from_dict({"pid": 123, "mode": 1})

    def test_rejects_explicit_null_mode(self) -> None:
        with pytest.raises(ValueError, match="invalid mode"):
            RunRecord.from_dict({"pid": 123, "mode": None})

    def test_accepts_each_valid_mode(self) -> None:
        for mode in ("fg", "fg-web", "bg"):
            record = RunRecord.from_dict({"pid": 123, "mode": mode})
            assert record.mode == mode

    def test_missing_mode_defaults_to_bg(self) -> None:
        record = RunRecord.from_dict({"pid": 123})
        assert record.mode == "bg"

    def test_a_live_run_written_by_a_newer_conductor_survives(self, fleet_env: Path) -> None:
        """A mode this version has never heard of must not delete the record.

        ``_read_and_prune`` deletes a corrupt record *without* checking
        liveness, so treating an unrecognised mode as corruption would make
        an older Conductor silently orphan a live newer run from ``stop``,
        ``status`` and the fleet -- the exact bug the run record exists to
        fix. The record is kept, surfaced, and gated as ``bg`` (the value
        that never triggers a spurious foreground confirmation).
        """
        path = run_records_dir() / "newmode.json"
        path.write_text(
            json.dumps({"pid": os.getpid(), "run_id": "newmode", "mode": "fg-tui"}),
        )

        records = read_run_records()

        assert [r.run_id for r in records] == ["newmode"]
        # Normalised, not deleted -- and to `fg`, so D1 still confirms.
        assert records[0].mode == "fg"
        assert path.exists()

    def test_a_genuinely_malformed_record_is_still_pruned(self, fleet_env: Path) -> None:
        bad_path = run_records_dir() / "badmode.json"
        bad_path.write_text(json.dumps({"pid": 123, "run_id": "badmode", "mode": 17}))

        records = read_run_records()

        assert records == []
        assert not bad_path.exists()


class TestExplicitInvalidValuesDistinguishedFromMissing:
    """A key that is genuinely *absent* defaults gracefully (legacy-file
    tolerance), but an explicit, wrong-typed value for the same field must
    be rejected rather than silently collapsed to the same default -- a
    falsy-but-present value (``[]``, ``False``, ``null``) is a malformed
    payload, not an omission."""

    def test_explicit_empty_list_run_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="run_id must be a string"):
            RunRecord.from_dict({"pid": 123, "run_id": []})

    def test_explicit_false_workflow_path_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="workflow_path must be a string"):
            RunRecord.from_dict({"pid": 123, "workflow_path": False})

    def test_explicit_false_workflow_alias_is_rejected_not_hidden_by_fallback(self) -> None:
        """``workflow_path`` (primary key) present-but-invalid must not be
        silently masked by falling through to the legacy ``workflow``
        alias -- the invalid primary value must surface as an error."""
        with pytest.raises(ValueError, match="workflow_path must be a string"):
            RunRecord.from_dict({"pid": 123, "workflow_path": False, "workflow": "/tmp/x.yaml"})

    def test_explicit_zero_event_log_path_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="event_log_path must be a string"):
            RunRecord.from_dict({"pid": 123, "event_log_path": 0})

    def test_explicit_null_mode_is_rejected_not_defaulted_to_bg(self) -> None:
        """Unlike a genuinely *missing* ``mode`` key (which defaults to
        ``"bg"``), an explicit ``"mode": null`` is a malformed payload --
        ``write_run_record`` never emits ``null`` for this field -- and
        must be rejected the same way any other invalid value is."""
        with pytest.raises(ValueError, match="invalid mode"):
            RunRecord.from_dict({"pid": 123, "mode": None})

    def test_missing_run_id_still_defaults_to_empty_string(self) -> None:
        """A genuinely missing key (the legacy-file case) is unaffected by
        the stricter falsy-value handling."""
        record = RunRecord.from_dict({"pid": 123})
        assert record.run_id == ""

    def test_missing_workflow_path_falls_back_to_legacy_alias(self) -> None:
        """When ``workflow_path`` is absent entirely (not merely falsy),
        the legacy ``workflow`` alias is still honored."""
        record = RunRecord.from_dict({"pid": 123, "workflow": "/tmp/legacy.yaml"})
        assert record.workflow_path == "/tmp/legacy.yaml"

    def test_missing_mode_key_still_defaults_to_bg(self) -> None:
        record = RunRecord.from_dict({"pid": 123})
        assert record.mode == "bg"


class TestReadRunRecordIdentityMismatch:
    """A keyed lookup must not trust a payload that declares a different
    ``run_id`` than the filename it was found under."""

    def test_read_run_record_rejects_payload_with_mismatched_run_id(self, fleet_env: Path) -> None:
        # Write a record under "aaaaaaaa.json" whose own payload claims a
        # different run_id ("bbbbbbbb") -- e.g. copied/renamed by hand, or a
        # bug elsewhere in the writer.
        mismatched = _make_record(run_id="bbbbbbbb", pid=os.getpid())
        (run_records_dir() / "aaaaaaaa.json").write_text(json.dumps(mismatched.to_dict()))

        assert read_run_record("aaaaaaaa") is None

    def test_read_run_record_accepts_matching_run_id(self, fleet_env: Path) -> None:
        write_run_record(_make_record(run_id="cccccccc", pid=os.getpid()))
        record = read_run_record("cccccccc")
        assert record is not None
        assert record.run_id == "cccccccc"

    def test_bulk_reader_prunes_json_record_with_mismatched_filename(self, fleet_env: Path) -> None:
        """``read_run_records()`` must apply the same identity check as the
        single-key lookup: a ``*.json`` record whose own payload claims a
        different ``run_id`` than its filename stem must be treated as
        untrustworthy (pruned), not silently surfaced under the claimed
        identity."""
        mismatched = _make_record(run_id="bbbbbbbb", pid=os.getpid())
        filepath = run_records_dir() / "aaaaaaaa.json"
        filepath.write_text(json.dumps(mismatched.to_dict()))

        records = read_run_records()

        assert records == []
        assert not filepath.exists()

    def test_bulk_reader_prunes_json_record_with_empty_payload_run_id(
        self, fleet_env: Path
    ) -> None:
        """A ``*.json`` record (this module's own naming scheme, unlike a
        legacy ``.pid`` file) with a missing/empty payload ``run_id`` can
        never equal its own non-empty filename stem, so it is pruned rather
        than surfaced -- only a genuine legacy ``.pid`` file is allowed an
        empty ``run_id``."""
        filepath = run_records_dir() / "ddddddd1.json"
        filepath.write_text(json.dumps({"pid": os.getpid(), "mode": "bg"}))

        records = read_run_records()

        assert records == []
        assert not filepath.exists()


class TestConcurrentReplacementDuringPrune:
    """A reader that decides a record is stale/corrupt must not delete a
    concurrent replacement written under the same ``run_id`` (e.g. by a
    ``resume``) between the read and the deletion decision."""

    def test_stale_record_replaced_concurrently_is_not_deleted(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A dead-pid record is on disk -- read_run_records() would normally
        # prune it as stale.
        write_run_record(_make_record(run_id="racer", pid=99999999))

        real_read_text = Path.read_text

        def _replace_after_read(self: Path, *args: object, **kwargs: object) -> str:
            text = real_read_text(self, *args, **kwargs)
            if self.name == "racer.json":
                # Simulate a concurrent `resume` atomically replacing the
                # same run_id with a live record, landing *after* this
                # reader already loaded the stale content but *before* it
                # acts on that staleness.
                write_run_record(_make_record(run_id="racer", pid=os.getpid()))
            return text

        monkeypatch.setattr(Path, "read_text", _replace_after_read)

        records = read_run_records()

        # The stale content this reader saw is dead, so it isn't returned
        # from *this* call -- but the live replacement it raced against must
        # survive on disk rather than being torn out by the stale-pid unlink.
        assert records == []
        surviving = json.loads((run_records_dir() / "racer.json").read_text())
        assert surviving["pid"] == os.getpid()

        # The next scan picks up the live replacement normally.
        records_after = read_run_records()
        assert len(records_after) == 1
        assert records_after[0].run_id == "racer"
        assert records_after[0].pid == os.getpid()

    def test_corrupt_record_replaced_concurrently_is_not_deleted(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad_path = run_records_dir() / "racer2.json"
        bad_path.write_text("not json{{{")

        real_read_text = Path.read_text

        def _replace_after_read(self: Path, *args: object, **kwargs: object) -> str:
            text = real_read_text(self, *args, **kwargs)
            if self.name == "racer2.json":
                write_run_record(_make_record(run_id="racer2", pid=os.getpid()))
            return text

        monkeypatch.setattr(Path, "read_text", _replace_after_read)

        read_run_records()

        surviving = json.loads(bad_path.read_text())
        assert surviving["pid"] == os.getpid()

    def test_replacement_immediately_before_the_deleting_rename_is_not_lost(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test for the narrowest possible version of this race:
        a naive ``stat()``-then-``unlink()`` implementation leaves a gap
        between the identity-confirming ``stat()`` and the actual
        ``unlink()`` syscall where a concurrent replacement can still slip
        in and be destroyed. This drives the concurrent write in as late as
        possible -- immediately before the real ``os.rename`` call that
        ``_delete_if_unchanged`` uses to atomically move the record out of
        the way -- to prove that even landing at that last instant, the
        replacement survives (because the rename captures whatever is
        actually on disk *at the moment it runs*, not a stale ``stat()``
        snapshot taken earlier)."""
        write_run_record(_make_record(run_id="racer3", pid=99999999))  # stale

        real_rename = os.rename

        def _replace_then_rename(src: object, dst: object, *a: object, **kw: object) -> None:
            if Path(src).name == "racer3.json":
                write_run_record(_make_record(run_id="racer3", pid=os.getpid()))
            return real_rename(src, dst, *a, **kw)

        monkeypatch.setattr(os, "rename", _replace_then_rename)

        records = read_run_records()

        # The stale content this call saw is dead, so it isn't returned --
        # but the replacement that landed at the last possible instant
        # before the deleting rename must survive untouched.
        assert records == []
        surviving = json.loads((run_records_dir() / "racer3.json").read_text())
        assert surviving["pid"] == os.getpid()

        records_after = read_run_records()
        assert len(records_after) == 1
        assert records_after[0].run_id == "racer3"
        assert records_after[0].pid == os.getpid()


class TestRestoreNeverClobbersWriteLandingAfterQuarantine:
    """The restoration step inside ``_delete_if_unchanged`` (used both when
    a concurrent replacement is detected, and when the final deletion of a
    genuinely stale/corrupt file itself fails) must never clobber a fresh
    write that lands *after* the file has already been quarantined but
    *before* the restore attempt runs. An unconditional ``os.replace``
    restoration would lose that fresh write; the non-clobbering
    ``os.link``-based restore must not."""

    def test_write_between_mismatch_detection_and_restore_is_not_clobbered(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exercises the "mismatch" branch: a stale record is quarantined,
        but a concurrent writer (B) already replaced it by the time the
        deleting rename runs, so the rename actually captures B instead of
        the stale content and a restore-of-B is triggered. A *third*
        writer (C) then lands at the now-empty original path in the
        narrow window between that mismatch being detected and the
        restore's ``os.link`` call actually executing. C, being the
        newest, must survive untouched -- B must not clobber it."""
        write_run_record(_make_record(run_id="racemis", pid=99999999))  # stale

        real_read_text = Path.read_text

        def _replace_with_b_after_read(self: Path, *a: object, **kw: object) -> str:
            text = real_read_text(self, *a, **kw)
            if self.name == "racemis.json":
                # B replaces the stale content -- this is what the deleting
                # rename will actually capture, triggering the "mismatch"
                # branch (captured content != stat_before).
                write_run_record(_make_record(run_id="racemis", pid=os.getpid(), started_at="B"))
            return text

        monkeypatch.setattr(Path, "read_text", _replace_with_b_after_read)

        real_link = os.link
        landed_c = {"done": False}

        def _replace_with_c_before_link(src: object, dst: object, *a: object, **kw: object) -> None:
            if not landed_c["done"] and Path(dst).name == "racemis.json":
                landed_c["done"] = True
                # C lands in the gap between mismatch detection and the
                # restore's os.link call -- at this point the original path
                # is empty (renamed into quarantine), so this write
                # succeeds exactly like a legitimate concurrent writer's
                # would.
                write_run_record(_make_record(run_id="racemis", pid=os.getpid(), started_at="C"))
            return real_link(src, dst, *a, **kw)

        monkeypatch.setattr(os, "link", _replace_with_c_before_link)

        read_run_records()

        surviving = json.loads((run_records_dir() / "racemis.json").read_text())
        assert surviving["started_at"] == "C"

        # The next scan picks up C normally (it's alive).
        records_after = read_run_records()
        assert len(records_after) == 1
        assert records_after[0].run_id == "racemis"
        assert records_after[0].pid == os.getpid()

    def test_write_between_failed_deletion_and_restore_is_not_clobbered(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exercises the "final unlink failed" branch: a stale record is
        quarantined and confirmed unchanged, but deleting it fails (e.g.
        permission denied), so a restore is attempted. A concurrent writer
        lands at the now-empty original path in the gap between that
        failed deletion and the restore's ``os.link`` call. The fresh
        write must survive untouched -- the restore of the old, dead-pid
        content must not clobber it."""
        write_run_record(_make_record(run_id="raceuf", pid=99999999))  # stale

        real_unlink = Path.unlink

        def _deny_unlink(self: Path, *a: object, **kw: object) -> None:
            if "raceuf" in self.name:
                raise PermissionError(self)
            return real_unlink(self, *a, **kw)

        monkeypatch.setattr(Path, "unlink", _deny_unlink)

        real_link = os.link
        landed = {"done": False}

        def _replace_before_link(src: object, dst: object, *a: object, **kw: object) -> None:
            if not landed["done"] and Path(dst).name == "raceuf.json":
                landed["done"] = True
                write_run_record(_make_record(run_id="raceuf", pid=os.getpid(), started_at="fresh"))
            return real_link(src, dst, *a, **kw)

        monkeypatch.setattr(os, "link", _replace_before_link)

        read_run_records()

        surviving = json.loads((run_records_dir() / "raceuf.json").read_text())
        assert surviving["started_at"] == "fresh"
        assert surviving["pid"] == os.getpid()


class TestQuarantineStatFailureDoesNotOrphan:
    """If stat()-ing the freshly-quarantined file itself fails (but the
    file is still actually present on disk), the quarantine artifact must
    be restored rather than left behind forever as an orphaned
    ``.prune-*`` file."""

    def test_quarantine_stat_failure_restores_rather_than_orphans(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_run_record(_make_record(run_id="statfail", pid=99999999))  # stale

        real_stat = Path.stat

        def _deny_stat(self: Path, *a: object, **kw: object) -> os.stat_result:
            if ".prune-" in self.name:
                raise PermissionError(self)
            return real_stat(self, *a, **kw)

        monkeypatch.setattr(Path, "stat", _deny_stat)

        records = read_run_records()

        assert records == []
        # Restored to its original name (best-effort) rather than left
        # behind as an orphaned `.prune-*` artifact.
        restored = json.loads((run_records_dir() / "statfail.json").read_text())
        assert restored["pid"] == 99999999
        assert list(run_records_dir().glob(".statfail.json.prune-*")) == []

    def test_second_writer_landing_during_restoration_is_not_clobbered(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stacked-race regression: after the reader's quarantine rename
        already captured one concurrent writer's replacement (mismatched
        identity, so restoration is required), a *second* writer can still
        land at the same path in the gap between the quarantine rename and
        the restoration attempt itself. Restoration must be non-clobbering
        (``os.link``, which atomically fails if the destination already
        exists) so the second writer's fresher record always wins, rather
        than an unconditional ``os.replace`` stomping it with the (older)
        quarantined content."""
        write_run_record(_make_record(run_id="racer4", pid=99999999))  # stale

        real_rename = os.rename
        real_link = os.link

        def _replace_on_first_rename(src: object, dst: object, *a: object, **kw: object) -> None:
            if Path(src).name == "racer4.json":
                # Writer #1: replaces the file *after* the reader loaded the
                # stale content but *at* the moment of the deleting rename,
                # so the rename captures writer #1's replacement -- forcing
                # the mismatched-identity restoration path.
                write_run_record(_make_record(run_id="racer4", pid=88888888))  # also stale
            return real_rename(src, dst, *a, **kw)

        def _replace_before_link(src: object, dst: object, *a: object, **kw: object) -> None:
            # Writer #2: lands at the destination *immediately before* the
            # non-clobbering restore attempt links the quarantined copy
            # (writer #1's stale record) back into place.
            if Path(dst).name == "racer4.json":
                write_run_record(_make_record(run_id="racer4", pid=os.getpid()))  # live
            return real_link(src, dst, *a, **kw)

        monkeypatch.setattr(os, "rename", _replace_on_first_rename)
        monkeypatch.setattr(os, "link", _replace_before_link)

        records = read_run_records()

        # Neither the originally-stale record nor writer #1's replacement
        # (also stale) is returned by this call, but crucially writer #2's
        # live record must survive on disk -- not be clobbered by the
        # restoration of writer #1's stale content.
        assert records == []
        surviving = json.loads((run_records_dir() / "racer4.json").read_text())
        assert surviving["pid"] == os.getpid()

        records_after = read_run_records()
        assert len(records_after) == 1
        assert records_after[0].run_id == "racer4"
        assert records_after[0].pid == os.getpid()


class TestLegacyEventLogRecovery:
    """A pre-Fleet-Manager ``.pid`` file records no event-log path, so every
    derived detail (step, tokens, cost, topology) came out blank in the TUI
    even though the log was on disk beside it. The record does carry the run
    id, and the log's filename ends in it."""

    @pytest.fixture()
    def log_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        d = tmp_path / "tmp"
        d.mkdir()
        (d / "conductor").mkdir()
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(d))
        return d / "conductor"

    def test_unique_log_is_recovered(self, log_dir: Path) -> None:
        log = log_dir / "conductor-ship-20260812-180146-293d3f34.events.jsonl"
        log.write_text("")

        record = RunRecord.from_dict(
            {"pid": 1, "port": 8080, "workflow": "/tmp/wf.yaml", "run_id": "293d3f34"}
        )
        assert record.event_log_path == str(log)

    def test_ambiguous_match_without_started_at_is_not_adopted(self, log_dir: Path) -> None:
        """Run ids are 8 hex chars and a resumed run reuses its predecessor's,
        so one id can legitimately name several logs. With nothing to
        disambiguate on, showing one run's details against another's log is
        worse than showing none."""
        for stem in ("alpha-20260812-100000", "beta-20260812-110000"):
            (log_dir / f"conductor-{stem}-293d3f34.events.jsonl").write_text("")

        record = RunRecord.from_dict(
            {"pid": 1, "port": 8080, "workflow": "/tmp/wf.yaml", "run_id": "293d3f34"}
        )
        assert record.event_log_path == ""

    def test_started_at_disambiguates_a_crowded_id(self, log_dir: Path) -> None:
        """The stamp in the filename is the one the record already carries.

        This is not hypothetical: the test suite writes its own logs into the
        very directory real runs use and pins run ids, so a live run's id
        matched 21 files and the TUI blanked every derived column for it
        while the right log sat on disk.
        """
        wanted = log_dir / "conductor-ship-20260812-183544-1764e163.events.jsonl"
        wanted.write_text("")
        for stem in ("test-20260812-190115", "multi-20260812-190037"):
            (log_dir / f"conductor-{stem}-1764e163.events.jsonl").write_text("")

        record = RunRecord.from_dict(
            {
                "pid": 1,
                "port": 8080,
                "workflow": "/tmp/wf.yaml",
                "run_id": "1764e163",
                # The record stamps UTC; the filename stamps naive local
                # time, so this only resolves if the two are reconciled.
                "started_at": datetime(2026, 8, 12, 18, 35, 44).astimezone().isoformat(),
            }
        )
        assert record.event_log_path == str(wanted)

    def test_started_at_far_from_every_candidate_is_not_adopted(self, log_dir: Path) -> None:
        """Outside the tolerance window nothing is claimed: a same-id log from
        an unrelated run must not be adopted just for being closest."""
        for stem in ("a-20260812-100000", "b-20260812-110000"):
            (log_dir / f"conductor-{stem}-293d3f34.events.jsonl").write_text("")

        record = RunRecord.from_dict(
            {
                "pid": 1,
                "port": 8080,
                "workflow": "/tmp/wf.yaml",
                "run_id": "293d3f34",
                "started_at": datetime(2026, 8, 12, 18, 0, 0).astimezone().isoformat(),
            }
        )
        assert record.event_log_path == ""

    def test_equidistant_candidates_are_not_adopted(self, log_dir: Path) -> None:
        """A tie is still ambiguous -- picking either would be a coin flip."""
        # Exactly 60 seconds either side of the record's start time.
        for stem in ("a-20260812-175900", "b-20260812-180100"):
            (log_dir / f"conductor-{stem}-293d3f34.events.jsonl").write_text("")

        record = RunRecord.from_dict(
            {
                "pid": 1,
                "port": 8080,
                "workflow": "/tmp/wf.yaml",
                "run_id": "293d3f34",
                "started_at": datetime(2026, 8, 12, 18, 0, 0).astimezone().isoformat(),
            }
        )
        assert record.event_log_path == ""

    def test_no_match_leaves_it_empty(self, log_dir: Path) -> None:
        record = RunRecord.from_dict(
            {"pid": 1, "port": 8080, "workflow": "/tmp/wf.yaml", "run_id": "nomatch1"}
        )
        assert record.event_log_path == ""

    def test_explicit_path_is_never_overridden(self, log_dir: Path) -> None:
        """A modern record already knows its log; the search must not second-
        guess it even when a same-id file happens to exist."""
        (log_dir / "conductor-other-20260812-100000-293d3f34.events.jsonl").write_text("")

        record = RunRecord.from_dict(
            {
                "pid": 1,
                "port": 8080,
                "workflow": "/tmp/wf.yaml",
                "run_id": "293d3f34",
                "event_log_path": "/explicit/path.jsonl",
            }
        )
        assert record.event_log_path == "/explicit/path.jsonl"

    def test_blank_run_id_searches_for_nothing(self, log_dir: Path) -> None:
        """A record with no run id has nothing to match on -- the glob would
        otherwise be `conductor-*-.events.jsonl`."""
        (log_dir / "conductor-x-20260812-100000-abc.events.jsonl").write_text("")

        record = RunRecord.from_dict({"pid": 1, "port": 8080, "workflow": "/tmp/wf.yaml"})
        assert record.event_log_path == ""

    def test_missing_log_directory_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lookup is read-only and must not create the directory (or
        raise) just because a record was parsed."""
        empty = tmp_path / "no-tmp"
        empty.mkdir()
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(empty))

        record = RunRecord.from_dict(
            {"pid": 1, "port": 8080, "workflow": "/tmp/wf.yaml", "run_id": "293d3f34"}
        )
        assert record.event_log_path == ""
        assert not (empty / "conductor").exists()
