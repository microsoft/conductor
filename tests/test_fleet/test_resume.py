"""Tests for ``conductor.fleet.resume`` (issue #460).

Covers :func:`correlate_checkpoints`'s join semantics in isolation, without
driving any Textual UI:

- Primary join on ``event_log_path`` (normalized via ``os.path.realpath``).
- Primary join wins over a *conflicting* ``run_id``.
- ``run_id`` fallback used when ``event_log_path`` is empty (a pre-#411
  checkpoint, or one whose log was unavailable at save time).
- Fallback refused when two entries share the run id (a nested
  ``conductor`` invocation inherits ``CONDUCTOR_RUN_ID``).
- Newest ``created_at`` wins among several matching checkpoints.
- A checkpoint whose ``workflow_path`` no longer exists is excluded.
- A checkpoint file that has since been deleted is excluded.
- One checkpoint is never returned for two entries.
- An entry with neither match returns nothing.
- A ``periodic``-trigger checkpoint correlates exactly like a ``failure``
  one -- ``outcome`` (which lives on the ``HistoryEntry``, not the
  checkpoint) is never consulted here either.
- No entries / no checkpoints -> ``{}``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from conductor.engine.checkpoint import CheckpointManager
from conductor.fleet.history import HistoryEntry
from conductor.fleet.records import RunRecord
from conductor.fleet.resume import ResumableCheckpoint, correlate_checkpoints

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def checkpoints_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect both the event-log root and the checkpoints directory to an
    isolated location -- both derive from ``tempfile.gettempdir()``
    (``engine/event_log.py`` and ``engine/checkpoint.py``), so patching that
    one seam moves both."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return CheckpointManager.get_checkpoints_dir()


def _entry(
    tmp_path: Path,
    *,
    name: str = "wf",
    run_id: str | None = "aaaa0001",
    log_filename: str | None = None,
) -> HistoryEntry:
    """Build a minimal :class:`HistoryEntry` for correlation tests.

    The event log file is actually created on disk (empty) so a
    ``event_log_path`` join can realpath-resolve it the same way a genuine
    checkpoint's recorded path would.
    """
    filename = log_filename or f"conductor-{name}-20260101-120000-{run_id}.events.jsonl"
    log_path = tmp_path / filename
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("")
    return HistoryEntry(
        path=log_path,
        workflow_name=name,
        run_id=run_id,
        outcome="unknown",
        started_at=None,
        ended_at=None,
        duration_seconds=None,
        total_tokens=0,
        total_cost_usd=None,
        unpriced_agent_count=0,
    )


def _write_checkpoint(
    checkpoints_dir: Path,
    workflow_path: Path,
    *,
    filename: str | None = None,
    created_at: str | None = None,
    run_id: str = "",
    event_log_path: str = "",
    current_agent: str = "step-1",
    trigger: str = "failure",
) -> Path:
    """Write a schema-valid checkpoint JSON file directly to disk.

    Mirrors ``CheckpointManager.load_checkpoint``'s ``required_fields``
    list so ``list_checkpoints`` accepts it without raising
    ``CheckpointError`` and silently skipping the file.
    """
    if created_at is None:
        created_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    data = {
        "version": CheckpointManager.CHECKPOINT_VERSION,
        "workflow_path": str(workflow_path),
        "workflow_hash": "sha256:deadbeef",
        "created_at": created_at,
        "failure": {"error_type": None, "message": None, "agent": current_agent, "iteration": 0},
        "inputs": {},
        "current_agent": current_agent,
        "context": {},
        "limits": {},
        "run_id": run_id,
        "event_log_path": event_log_path,
        "trigger": trigger,
    }
    name = filename or f"{workflow_path.stem}-{os.urandom(4).hex()}.json"
    path = checkpoints_dir / name
    path.write_text(json.dumps(data))
    return path


@pytest.fixture()
def workflow_file(tmp_path: Path) -> Path:
    path = tmp_path / "workflow.yaml"
    path.write_text("workflow:\n  name: test\n  entry_point: a\nagents:\n  - name: a\n")
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoInput:
    def test_no_entries_returns_empty(self, checkpoints_dir: Path, workflow_file: Path) -> None:
        _write_checkpoint(checkpoints_dir, workflow_file, run_id="run1")
        assert correlate_checkpoints([]) == {}

    def test_no_checkpoints_returns_empty(self, checkpoints_dir: Path, tmp_path: Path) -> None:
        entry = _entry(tmp_path)
        assert correlate_checkpoints([entry]) == {}


class TestEventLogPathJoin:
    def test_primary_join_on_event_log_path(
        self, checkpoints_dir: Path, workflow_file: Path, tmp_path: Path
    ) -> None:
        entry = _entry(tmp_path, run_id="aaaa0001")
        _write_checkpoint(
            checkpoints_dir,
            workflow_file,
            run_id="aaaa0001",
            event_log_path=str(entry.path),
        )

        result = correlate_checkpoints([entry])

        assert entry.path in result
        assert result[entry.path].matched_by == "event_log_path"
        assert result[entry.path].workflow_path == workflow_file

    def test_event_log_path_join_normalizes_via_realpath(
        self, checkpoints_dir: Path, workflow_file: Path, tmp_path: Path
    ) -> None:
        """A checkpoint's recorded path need not be byte-identical to the
        entry's -- both sides normalize with ``os.path.realpath`` so a
        ``./``-relative or symlinked path still joins."""
        entry = _entry(tmp_path, run_id="aaaa0002")
        # Build the redundant "./" segment as a raw string -- `Path`
        # collapses "." at construction time (`Path("/x") / "." / "y" ==
        # Path("/x/y")`), which would make `recorded` byte-identical to
        # `str(entry.path)` and the realpath normalization untested.
        recorded = f"{entry.path.parent}{os.sep}.{os.sep}{entry.path.name}"
        assert recorded != str(entry.path)
        _write_checkpoint(
            checkpoints_dir, workflow_file, run_id="aaaa0002", event_log_path=recorded
        )

        result = correlate_checkpoints([entry])

        assert entry.path in result
        assert result[entry.path].matched_by == "event_log_path"

    def test_event_log_path_join_normalizes_via_symlinked_parent(
        self, checkpoints_dir: Path, workflow_file: Path, tmp_path: Path
    ) -> None:
        """The module docstring sells a symlinked ``$TMPDIR`` as the actual
        motivation for ``os.path.realpath`` normalization -- exercise that
        directly rather than only the ``./`` collapsing case above."""
        real_dir = tmp_path / "real_logs"
        real_dir.mkdir()
        link_dir = tmp_path / "linked_logs"
        link_dir.symlink_to(real_dir)

        log_path = real_dir / "conductor-wf-20260101-120000-aaaa0009.events.jsonl"
        log_path.write_text("")
        # The entry references the log through the symlinked directory,
        # while the checkpoint records it through the *real* one -- only
        # realpath normalization joins the two.
        entry = HistoryEntry(
            path=link_dir / log_path.name,
            workflow_name="wf",
            run_id="aaaa0009",
            outcome="unknown",
            started_at=None,
            ended_at=None,
            duration_seconds=None,
            total_tokens=0,
            total_cost_usd=None,
            unpriced_agent_count=0,
        )

        _write_checkpoint(
            checkpoints_dir,
            workflow_file,
            run_id="aaaa0009",
            event_log_path=str(log_path),
        )

        result = correlate_checkpoints([entry])

        assert entry.path in result
        assert result[entry.path].matched_by == "event_log_path"

    def test_primary_join_wins_over_conflicting_run_id(
        self, checkpoints_dir: Path, workflow_file: Path, tmp_path: Path
    ) -> None:
        """A checkpoint whose ``event_log_path`` matches entry A but whose
        ``run_id`` happens to equal entry B's must join to A, not B."""
        entry_a = _entry(tmp_path, name="wf-a", run_id="aaaa0003")
        entry_b = _entry(tmp_path, name="wf-b", run_id="bbbb0003")
        _write_checkpoint(
            checkpoints_dir,
            workflow_file,
            run_id="bbbb0003",  # matches entry_b's run_id
            event_log_path=str(entry_a.path),  # matches entry_a's log path
        )

        result = correlate_checkpoints([entry_a, entry_b])

        assert entry_a.path in result
        assert result[entry_a.path].matched_by == "event_log_path"
        assert entry_b.path not in result


class TestRunIdFallback:
    def test_run_id_fallback_used_when_event_log_path_empty(
        self, checkpoints_dir: Path, workflow_file: Path, tmp_path: Path
    ) -> None:
        entry = _entry(tmp_path, run_id="cccc0001")
        _write_checkpoint(checkpoints_dir, workflow_file, run_id="cccc0001", event_log_path="")

        result = correlate_checkpoints([entry])

        assert entry.path in result
        assert result[entry.path].matched_by == "run_id"

    def test_run_id_fallback_used_when_recorded_path_no_longer_exists(
        self, checkpoints_dir: Path, workflow_file: Path, tmp_path: Path
    ) -> None:
        """The second justified fallback case: ``event_log_path`` was
        recorded but the file it names has since been deleted (e.g. by
        retention)."""
        entry = _entry(tmp_path, run_id="cccc0002")
        gone_log_path = tmp_path / "conductor-gone-20260101-120000-cccc0002.events.jsonl"
        # Deliberately never written to disk -- this is the "recorded path
        # no longer exists" case, not the "never recorded" (empty) one.
        _write_checkpoint(
            checkpoints_dir,
            workflow_file,
            run_id="cccc0002",
            event_log_path=str(gone_log_path),
        )

        result = correlate_checkpoints([entry])

        assert entry.path in result
        assert result[entry.path].matched_by == "run_id"

    def test_run_id_fallback_refused_when_recorded_path_still_exists_elsewhere(
        self, checkpoints_dir: Path, workflow_file: Path, tmp_path: Path
    ) -> None:
        """Issue #460 review, blocking finding 3: the fallback must not fire
        just because the primary lookup found no *current* entry for
        ``event_log_path`` -- only when that path was never recorded or has
        genuinely vanished. Here the checkpoint's recorded path is still a
        real file (simulating one outside this scan, e.g. filtered out
        elsewhere) that merely isn't among the entries being correlated;
        falling back on ``run_id`` in that case previously let a checkpoint
        for a completely different workflow join to an unrelated row that
        happens to share the same (inherited) ``run_id``."""
        entry = _entry(tmp_path, run_id="cccc0003")
        other_real_log_path = tmp_path / "conductor-other-20260101-120000-cccc0003.events.jsonl"
        other_real_log_path.write_text("")
        _write_checkpoint(
            checkpoints_dir,
            workflow_file,
            run_id="cccc0003",
            event_log_path=str(other_real_log_path),
        )

        result = correlate_checkpoints([entry])

        assert entry.path not in result
        assert result == {}

    def test_run_id_fallback_refused_when_ambiguous(
        self, checkpoints_dir: Path, workflow_file: Path, tmp_path: Path
    ) -> None:
        """Two entries sharing one ``run_id`` (a nested ``conductor``
        invocation inheriting ``CONDUCTOR_RUN_ID``) must not let the
        fallback guess which one a checkpoint belongs to."""
        entry_a = _entry(tmp_path, name="wf-a", run_id="dddd0001")
        entry_b = _entry(tmp_path, name="wf-b", run_id="dddd0001")
        _write_checkpoint(checkpoints_dir, workflow_file, run_id="dddd0001", event_log_path="")

        result = correlate_checkpoints([entry_a, entry_b])

        assert result == {}

    def test_no_match_returns_nothing_for_that_entry(
        self, checkpoints_dir: Path, workflow_file: Path, tmp_path: Path
    ) -> None:
        entry = _entry(tmp_path, run_id="eeee0001")
        _write_checkpoint(
            checkpoints_dir,
            workflow_file,
            run_id="ffff9999",
            event_log_path=str(tmp_path / "unrelated.events.jsonl"),
        )

        assert correlate_checkpoints([entry]) == {}


class TestNewestWins:
    def test_newest_created_at_wins_among_several_matches(
        self, checkpoints_dir: Path, workflow_file: Path, tmp_path: Path
    ) -> None:
        entry = _entry(tmp_path, run_id="ffff0001")
        _write_checkpoint(
            checkpoints_dir,
            workflow_file,
            run_id="ffff0001",
            event_log_path=str(entry.path),
            created_at="2026-01-01T00:00:00+00:00",
            current_agent="older-step",
        )
        _write_checkpoint(
            checkpoints_dir,
            workflow_file,
            run_id="ffff0001",
            event_log_path=str(entry.path),
            created_at="2026-01-02T00:00:00+00:00",
            current_agent="newer-step",
        )

        result = correlate_checkpoints([entry])

        assert result[entry.path].current_agent == "newer-step"
        assert result[entry.path].created_at == "2026-01-02T00:00:00+00:00"


class TestValidityFilter:
    def test_excludes_checkpoint_whose_workflow_path_no_longer_exists(
        self, checkpoints_dir: Path, tmp_path: Path
    ) -> None:
        entry = _entry(tmp_path, run_id="00010001")
        missing_workflow = tmp_path / "gone.yaml"
        _write_checkpoint(
            checkpoints_dir,
            missing_workflow,
            run_id="00010001",
            event_log_path=str(entry.path),
        )

        assert correlate_checkpoints([entry]) == {}

    def test_excludes_a_deleted_checkpoint_file(
        self,
        checkpoints_dir: Path,
        workflow_file: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The checkpoint file must vanish *between* ``list_checkpoints``'s
        own scan and this module's re-check -- unlinking it before calling
        ``correlate_checkpoints`` (as this test previously did) means
        ``list_checkpoints``'s own glob never yields it at all, so
        ``resume.py``'s own existence re-check is never exercised. Patch
        ``list_checkpoints`` to return a hand-built ``CheckpointData``
        pointing at a path that was never written, simulating the race
        instead."""
        from conductor.engine.checkpoint import CheckpointData

        entry = _entry(tmp_path, run_id="00020001")
        missing_checkpoint_path = checkpoints_dir / "gone-00020001.json"
        cp = CheckpointData(
            version=CheckpointManager.CHECKPOINT_VERSION,
            workflow_path=str(workflow_file),
            workflow_hash="sha256:deadbeef",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            failure={"error_type": None, "message": None, "agent": "step-1", "iteration": 0},
            inputs={},
            current_agent="step-1",
            context={},
            limits={},
            file_path=missing_checkpoint_path,
            run_id="00020001",
            event_log_path=str(entry.path),
        )
        monkeypatch.setattr(CheckpointManager, "list_checkpoints", staticmethod(lambda _: [cp]))

        assert correlate_checkpoints([entry]) == {}


class TestOneCheckpointOneEntry:
    def test_a_checkpoint_is_never_returned_for_two_entries(
        self, checkpoints_dir: Path, workflow_file: Path, tmp_path: Path
    ) -> None:
        entry_a = _entry(tmp_path, name="wf-a", run_id="00030001")
        entry_b = _entry(tmp_path, name="wf-b", run_id="00030002")
        # Only entry_a's log path matches; entry_b has no matching key at all.
        _write_checkpoint(
            checkpoints_dir,
            workflow_file,
            run_id="00030001",
            event_log_path=str(entry_a.path),
        )

        result = correlate_checkpoints([entry_a, entry_b])

        assert entry_a.path in result
        assert entry_b.path not in result
        checkpoint_paths = {cp.checkpoint_path for cp in result.values()}
        assert len(checkpoint_paths) == len(result)


class TestTriggerAgnostic:
    def test_periodic_trigger_correlates_like_failure(
        self, checkpoints_dir: Path, workflow_file: Path, tmp_path: Path
    ) -> None:
        entry = _entry(tmp_path, run_id="00040001")
        _write_checkpoint(
            checkpoints_dir,
            workflow_file,
            run_id="00040001",
            event_log_path=str(entry.path),
            trigger="periodic",
        )

        result = correlate_checkpoints([entry])

        assert entry.path in result
        assert result[entry.path].trigger == "periodic"


def _run_record(*, event_log_path: str, pid: int = 12345) -> RunRecord:
    """Build a minimal :class:`RunRecord` for liveness-exclusion tests."""
    return RunRecord(
        run_id="live-run",
        pid=pid,
        workflow_path="workflow.yaml",
        workflow_name="workflow",
        started_at="2026-01-01T00:00:00+00:00",
        event_log_path=event_log_path,
        port=8080,
        mode="bg",
        checkpoint_dir=None,
    )


class TestLiveRunExclusion:
    """Blocking finding 1 (issue #460 review): a currently-live run must
    never be offered Resume, regardless of what checkpoint would otherwise
    correlate to it -- pressing ``r`` on it corrupts the live run (adopted
    ``run_id``, overwritten run record, interleaved event log, the same
    workflow executing twice)."""

    def test_live_run_excluded_even_with_a_matching_checkpoint(
        self,
        checkpoints_dir: Path,
        workflow_file: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = _entry(tmp_path, run_id="live0001")
        _write_checkpoint(
            checkpoints_dir,
            workflow_file,
            run_id="live0001",
            event_log_path=str(entry.path),
        )
        monkeypatch.setattr(
            "conductor.fleet.records.read_run_records",
            lambda: [_run_record(event_log_path=str(entry.path))],
        )

        assert correlate_checkpoints([entry]) == {}

    def test_a_non_live_entry_still_correlates_alongside_a_live_one(
        self,
        checkpoints_dir: Path,
        workflow_file: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        live_entry = _entry(tmp_path, name="wf-live", run_id="live0002")
        finished_entry = _entry(tmp_path, name="wf-finished", run_id="live0003")
        _write_checkpoint(
            checkpoints_dir,
            workflow_file,
            run_id="live0002",
            event_log_path=str(live_entry.path),
        )
        _write_checkpoint(
            checkpoints_dir,
            workflow_file,
            run_id="live0003",
            event_log_path=str(finished_entry.path),
        )
        monkeypatch.setattr(
            "conductor.fleet.records.read_run_records",
            lambda: [_run_record(event_log_path=str(live_entry.path))],
        )

        result = correlate_checkpoints([live_entry, finished_entry])

        assert live_entry.path not in result
        assert finished_entry.path in result

    def test_fails_closed_when_liveness_cannot_be_determined(
        self,
        checkpoints_dir: Path,
        workflow_file: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``read_run_records`` itself fails, ``_live_event_log_paths``
        returns ``None`` and this module must offer no Resume at all for the
        scan -- never assume nothing is live."""
        entry = _entry(tmp_path, run_id="live0004")
        _write_checkpoint(
            checkpoints_dir,
            workflow_file,
            run_id="live0004",
            event_log_path=str(entry.path),
        )

        def _raise() -> list[RunRecord]:
            raise OSError("cannot read run records")

        monkeypatch.setattr("conductor.fleet.records.read_run_records", _raise)

        assert correlate_checkpoints([entry]) == {}


def test_resumable_checkpoint_is_frozen() -> None:
    """Basic shape check: the dataclass is frozen/slotted per the plan."""
    cp = ResumableCheckpoint(
        checkpoint_path=Path("/tmp/x.json"),
        workflow_path=Path("/tmp/wf.yaml"),
        created_at="2026-01-01T00:00:00+00:00",
        run_id="abc",
        current_agent="step",
        trigger="failure",
        matched_by="run_id",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        cp.run_id = "other"  # type: ignore[misc]
