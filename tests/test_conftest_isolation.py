"""Regression test for the autouse ``_isolate_event_log_root`` fixture
(``tests/conftest.py``).

Guards against the test suite pruning a developer's real
``$TMPDIR/conductor/`` event logs -- reproduced empirically by planting
sentinel logs in the real ``/tmp/conductor``, running a single Fleet
Manager wiring test alone, and observing two of them deleted by the
startup retention sweep (``maybe_prune_event_logs``, enabled by default).
This test asserts the guard itself is in effect for every test in the
suite, not just the ones that already knew to isolate it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from conductor.fleet.retention import event_log_root


def test_gettempdir_is_isolated_from_the_real_system_tempdir(tmp_path: Path) -> None:
    """``tempfile.gettempdir()`` must never resolve to the real system temp
    directory during a test run -- every test gets its own isolated root
    (pytest's own ``tmp_path``, applied automatically by the autouse
    fixture in ``tests/conftest.py``)."""
    resolved = Path(tempfile.gettempdir())
    assert resolved != Path("/tmp")
    assert resolved == tmp_path


def test_event_log_root_is_isolated(tmp_path: Path) -> None:
    """``conductor.fleet.retention.event_log_root()`` -- the directory the
    startup pruning sweep operates on -- must resolve under the isolated
    root, never the developer's real ``$TMPDIR/conductor/``."""
    root = event_log_root()
    assert root.is_relative_to(tmp_path)
    assert root != Path("/tmp/conductor")


def test_run_record_dirs_are_isolated_from_the_real_home(tmp_path: Path) -> None:
    """Both run-record directories must resolve away from the real ``~``.

    ``read_run_records()`` is a *pruning* reader -- it deletes any record it
    judges corrupt, identity-mismatched, or dead-PID -- so anything reaching
    it (``conductor stop``, ``fleet list``, the TUI poll, and the retention
    sweep via ``_live_event_log_paths``) destroys the developer's *live* run
    records unless both are redirected.

    Both are checked because they resolve differently by design:
    ``run_records_dir()`` honors ``CONDUCTOR_HOME`` while ``pid_dir()``
    deliberately does not, so isolating one still leaves the other pointed
    at the real home.
    """
    from conductor.cli.pid import pid_dir
    from conductor.fleet.records import run_records_dir

    real_runs = Path.home() / ".conductor" / "runs"

    records_dir = run_records_dir()
    assert records_dir.is_relative_to(tmp_path)
    assert records_dir != real_runs

    legacy_dir = pid_dir()
    assert legacy_dir.is_relative_to(tmp_path)
    assert legacy_dir != real_runs
