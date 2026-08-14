"""Tests for ``conductor.fleet.retention`` (Fleet Manager E5 — D3).

Covers:
- ``keep_last`` retains the newest N event logs by mtime, deleting the rest.
- ``keep_last`` of 0 or negative never deletes anything (mirrors
  ``CheckpointManager.rotate_periodic_checkpoints``'s negative-slice guard).
- The ``checkpoints/`` subdirectory is never touched.
- An event log referenced by a live run record always survives, regardless
  of its age.
- ``.bg.stderr.log`` / ``.bg.stdout.log`` companions are pruned or kept
  together with their events log.
- An unreadable/undeletable file does not abort the sweep.
- ``dry_run=True`` lists what would be deleted without deleting anything.
- ``maybe_prune_event_logs()`` (the settings-driven wrapper) sweeps nothing
  when ``[fleet.retention].enabled`` is false, and never raises on
  malformed settings.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from conductor.fleet.records import RunRecord, write_run_record
from conductor.fleet.retention import (
    PruneResult,
    event_log_root,
    maybe_prune_event_logs,
    prune_event_logs,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``event_log_root()`` to an isolated directory.

    Patches ``tempfile.gettempdir`` directly (rather than the ``TMPDIR``
    env var) so this is immune to ``tempfile``'s internal caching of the
    resolved temp directory across the test process's lifetime -- an env
    var change has no effect once ``tempfile`` has already resolved and
    cached a value earlier in the same interpreter (see the flakiness this
    caused in ``tests/test_engine/test_event_log.py``).
    """
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    root = event_log_root()
    return root


@pytest.fixture()
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the run-record directory (for liveness checks) to an isolated dir."""
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))

    legacy_dir = tmp_path / "legacy_runs"
    legacy_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: legacy_dir)

    return home


def _make_event_log(root: Path, name: str, *, age_seconds: float = 0.0) -> Path:
    """Create an event log file under ``root`` and backdate its mtime."""
    path = root / name
    path.write_text("{}\n")
    if age_seconds:
        now = time.time()
        os.utime(path, (now - age_seconds, now - age_seconds))
    return path


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


# ---------------------------------------------------------------------------
# keep_last semantics
# ---------------------------------------------------------------------------


class TestEventLogRootSymlinkGuard:
    def test_refuses_symlinked_conductor_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `$TMPDIR/conductor` that's actually a symlink must be refused
        rather than followed -- deleting files through it could reach
        outside the intended directory."""
        real_target = tmp_path / "elsewhere"
        real_target.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        (tmp_path / "conductor").symlink_to(real_target, target_is_directory=True)

        with pytest.raises(RuntimeError, match="symlink"):
            event_log_root()


class TestKeepLastSemantics:
    def test_retains_newest_n(self, temp_root: Path) -> None:
        oldest = _make_event_log(temp_root, "conductor-a-old.events.jsonl", age_seconds=300)
        middle = _make_event_log(temp_root, "conductor-b-mid.events.jsonl", age_seconds=150)
        newest = _make_event_log(temp_root, "conductor-c-new.events.jsonl", age_seconds=0)

        result = prune_event_logs(keep_last=2)

        assert newest.exists()
        assert middle.exists()
        assert not oldest.exists()
        assert oldest in result.deleted

    def test_keep_last_zero_deletes_nothing(self, temp_root: Path) -> None:
        """Mirrors rotate_periodic_checkpoints: keep_last < 1 means 'prune
        nothing', not 'delete everything' -- without the guard, a slice like
        candidates[0:] would still delete everything, so this specifically
        exercises that keep_last=0 is treated as a no-op."""
        a = _make_event_log(temp_root, "conductor-a.events.jsonl", age_seconds=300)
        b = _make_event_log(temp_root, "conductor-b.events.jsonl", age_seconds=0)

        result = prune_event_logs(keep_last=0)

        assert a.exists()
        assert b.exists()
        assert result.deleted == []

    def test_negative_keep_last_deletes_nothing(self, temp_root: Path) -> None:
        """A negative keep_last must not produce a negative-slice bug that
        would retain exactly the files meant to be deleted (or worse,
        delete the wrong set)."""
        a = _make_event_log(temp_root, "conductor-a.events.jsonl", age_seconds=300)
        b = _make_event_log(temp_root, "conductor-b.events.jsonl", age_seconds=0)

        result = prune_event_logs(keep_last=-5)

        assert a.exists()
        assert b.exists()
        assert result.deleted == []

    def test_keep_last_exceeds_file_count(self, temp_root: Path) -> None:
        a = _make_event_log(temp_root, "conductor-a.events.jsonl")
        result = prune_event_logs(keep_last=100)
        assert a.exists()
        assert result.deleted == []


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


class TestExclusions:
    def test_checkpoints_subdirectory_survives(self, temp_root: Path) -> None:
        checkpoints_dir = temp_root / "checkpoints"
        checkpoints_dir.mkdir()
        checkpoint_file = checkpoints_dir / "workflow-20260101-000000.json"
        checkpoint_file.write_text("{}")

        for i in range(5):
            _make_event_log(temp_root, f"conductor-run{i}.events.jsonl", age_seconds=i * 10)

        prune_event_logs(keep_last=1)

        assert checkpoint_file.exists()

    def test_live_run_log_survives(self, temp_root: Path, fleet_env: Path) -> None:
        live_log = _make_event_log(temp_root, "conductor-live-run.events.jsonl", age_seconds=1000)
        newest = _make_event_log(temp_root, "conductor-newest.events.jsonl", age_seconds=0)

        write_run_record(
            _make_record(run_id="liverun", pid=os.getpid(), event_log_path=str(live_log))
        )

        result = prune_event_logs(keep_last=1)

        assert live_log.exists()
        assert newest.exists()
        assert live_log in result.skipped_live

    def test_dead_run_log_is_pruned(self, temp_root: Path, fleet_env: Path) -> None:
        """A run record for a dead PID is not "live" -- read_run_records()
        itself filters to alive PIDs, so its event log is prunable like any
        other old file."""
        old_log = _make_event_log(temp_root, "conductor-dead-run.events.jsonl", age_seconds=1000)
        newest = _make_event_log(temp_root, "conductor-newest.events.jsonl", age_seconds=0)

        # An astronomically unlikely-to-be-alive PID.
        write_run_record(_make_record(run_id="deadrun", pid=2**30 - 1, event_log_path=str(old_log)))

        result = prune_event_logs(keep_last=1)

        assert not old_log.exists()
        assert newest.exists()
        assert old_log in result.deleted

    def test_liveness_read_failure_fails_closed(
        self, temp_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If liveness can't be determined, nothing is pruned -- an empty
        set of "live" paths must never be mistaken for "nothing is live"."""
        old_log = _make_event_log(temp_root, "conductor-old.events.jsonl", age_seconds=1000)
        newest = _make_event_log(temp_root, "conductor-newest.events.jsonl", age_seconds=0)

        def _boom() -> list[Any]:
            raise RuntimeError("boom")

        monkeypatch.setattr("conductor.fleet.records.read_run_records", _boom)

        result = prune_event_logs(keep_last=1)

        assert old_log.exists()
        assert newest.exists()
        assert result == PruneResult()

    def test_bg_companions_pruned_with_events_log(self, temp_root: Path) -> None:
        old_log = _make_event_log(
            temp_root, "conductor-bgrun-20260101-000000-abcd1234.events.jsonl", age_seconds=1000
        )
        stderr_log = temp_root / "conductor-bgrun-20260101-000000-abcd1234.bg.stderr.log"
        stdout_log = temp_root / "conductor-bgrun-20260101-000000-abcd1234.bg.stdout.log"
        stderr_log.write_text("")
        stdout_log.write_text("")
        newest = _make_event_log(temp_root, "conductor-newest.events.jsonl", age_seconds=0)

        result = prune_event_logs(keep_last=1)

        assert not old_log.exists()
        assert not stderr_log.exists()
        assert not stdout_log.exists()
        assert newest.exists()
        assert set(result.deleted) == {old_log, stderr_log, stdout_log}

    def test_bg_companions_pruned_with_hyphenated_run_id(self, temp_root: Path) -> None:
        """A run id containing ``-``/``_`` (issue #435's broadened
        ``conductor.run_id`` contract) still matches its companions --
        without the timestamp anchor in ``_RUN_ID_FROM_EVENT_LOG``, a
        hyphenated run id could backtrack across the timestamp segment and
        fail to match its own companion files."""
        old_log = _make_event_log(
            temp_root,
            "conductor-bgrun-20260101-000000-nightly-run_7.events.jsonl",
            age_seconds=1000,
        )
        stderr_log = temp_root / "conductor-bgrun-20260101-000000-nightly-run_7.bg.stderr.log"
        stdout_log = temp_root / "conductor-bgrun-20260101-000000-nightly-run_7.bg.stdout.log"
        stderr_log.write_text("")
        stdout_log.write_text("")
        newest = _make_event_log(temp_root, "conductor-newest.events.jsonl", age_seconds=0)

        result = prune_event_logs(keep_last=1)

        assert not old_log.exists()
        assert not stderr_log.exists()
        assert not stdout_log.exists()
        assert newest.exists()
        assert set(result.deleted) == {old_log, stderr_log, stdout_log}

    def test_bg_companions_of_retained_log_survive(self, temp_root: Path) -> None:
        """A retained (not-yet-pruned) events log's companions are simply
        never inspected/deleted -- they were never candidates."""
        newest_log = _make_event_log(
            temp_root,
            "conductor-bgrun-20260101-000000-abcd1234.events.jsonl",
            age_seconds=0,
        )
        stderr_log = temp_root / "conductor-bgrun-20260101-000000-abcd1234.bg.stderr.log"
        stdout_log = temp_root / "conductor-bgrun-20260101-000000-abcd1234.bg.stdout.log"
        stderr_log.write_text("")
        stdout_log.write_text("")

        prune_event_logs(keep_last=1)

        assert newest_log.exists()
        assert stderr_log.exists()
        assert stdout_log.exists()


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_unreadable_file_does_not_abort_sweep(self, temp_root: Path) -> None:
        """A directory colliding with the events-log glob pattern is
        filtered out before it can consume a `keep_last` slot or be
        unlinked; the sweep still prunes the genuinely old file rather
        than aborting entirely."""
        bogus_dir = temp_root / "conductor-bogus.events.jsonl"
        bogus_dir.mkdir()
        old_log = _make_event_log(temp_root, "conductor-old.events.jsonl", age_seconds=1000)
        newest = _make_event_log(temp_root, "conductor-newest.events.jsonl", age_seconds=0)

        result = prune_event_logs(keep_last=1)

        # The bogus directory survives (unlink failed), but the sweep still
        # pruned the genuinely old file rather than aborting entirely.
        assert bogus_dir.exists()
        assert not old_log.exists()
        assert newest.exists()
        assert old_log in result.deleted

    def test_never_raises_on_unexpected_error(
        self, temp_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """prune_event_logs() is documented never to raise; force an
        unexpected exception deep in the implementation and confirm it is
        swallowed, reported via ``error``, and deletes nothing.

        ``error`` is what stops the CLI rendering a failed sweep as
        "Nothing to prune." with exit 0 -- an empty result alone is
        indistinguishable from having had nothing to do.
        """
        _make_event_log(temp_root, "conductor-a.events.jsonl")

        def _boom() -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr("conductor.fleet.retention.event_log_root", _boom)

        result = prune_event_logs(keep_last=1)
        assert result.deleted == []
        assert result.skipped_live == []
        assert result.failed == []
        assert result.error is not None
        assert "boom" in result.error


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_lists_without_deleting(self, temp_root: Path) -> None:
        old_log = _make_event_log(temp_root, "conductor-old.events.jsonl", age_seconds=1000)
        newest = _make_event_log(temp_root, "conductor-newest.events.jsonl", age_seconds=0)

        result = prune_event_logs(keep_last=1, dry_run=True)

        assert old_log.exists()
        assert newest.exists()
        assert old_log in result.deleted

    def test_dry_run_includes_bg_companions(self, temp_root: Path) -> None:
        old_log = _make_event_log(
            temp_root, "conductor-bgrun-20260101-000000-abcd1234.events.jsonl", age_seconds=1000
        )
        stderr_log = temp_root / "conductor-bgrun-20260101-000000-abcd1234.bg.stderr.log"
        stderr_log.write_text("")
        _make_event_log(temp_root, "conductor-newest.events.jsonl", age_seconds=0)

        result = prune_event_logs(keep_last=1, dry_run=True)

        assert old_log.exists()
        assert stderr_log.exists()
        assert old_log in result.deleted
        assert stderr_log in result.deleted


# ---------------------------------------------------------------------------
# maybe_prune_event_logs (settings-driven wrapper)
# ---------------------------------------------------------------------------


class TestMaybePruneEventLogs:
    def test_enabled_by_default_sweeps(
        self, temp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No config.toml at all -> defaults -> enabled=True, keep_last=200.

        A single old log doesn't exceed the default keep_last, so nothing
        is actually deleted, but the sweep does run (result is not None).
        """
        home = tmp_path / "settings_home"
        home.mkdir()
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))

        old_log = _make_event_log(temp_root, "conductor-old.events.jsonl", age_seconds=1000)

        result = maybe_prune_event_logs()

        assert result is not None
        assert old_log.exists()

    def test_enabled_false_explicit_sweeps_nothing(
        self, temp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "settings_home"
        home.mkdir()
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))
        (home / "config.toml").write_text("[fleet.retention]\nenabled = false\nkeep_last = 1\n")

        old_log = _make_event_log(temp_root, "conductor-old.events.jsonl", age_seconds=1000)

        result = maybe_prune_event_logs()

        assert result is None
        assert old_log.exists()

    def test_enabled_true_sweeps(
        self, temp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "settings_home"
        home.mkdir()
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))
        (home / "config.toml").write_text("[fleet.retention]\nenabled = true\nkeep_last = 1\n")

        old_log = _make_event_log(temp_root, "conductor-old.events.jsonl", age_seconds=1000)
        newest = _make_event_log(temp_root, "conductor-newest.events.jsonl", age_seconds=0)

        result = maybe_prune_event_logs()

        assert result is not None
        assert not old_log.exists()
        assert newest.exists()

    def test_malformed_settings_never_raises(
        self, temp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed config.toml must be swallowed by the startup-sweep
        wrapper (contrast with settings.load_settings() itself, and with the
        explicit `conductor fleet prune` CLI path, which both surface the
        error to an interactive caller)."""
        home = tmp_path / "settings_home"
        home.mkdir()
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))
        (home / "config.toml").write_text("not valid [[[toml")

        old_log = _make_event_log(temp_root, "conductor-old.events.jsonl", age_seconds=1000)

        result = maybe_prune_event_logs()  # must not raise

        assert result is None
        assert old_log.exists()
