"""Meta-test: the global test-suite isolation for ``rundir.runs_dir()`` holds.

The autouse ``_isolated_runs_dir`` fixture in ``tests/conftest.py`` points
``conductor.rundir.runs_dir()`` at a per-test temp directory so no test
reads or writes the developer's real ``~/.conductor/runs`` (issue #397's
PID/token registry). This test asserts that invariant directly rather than
relying on it only being exercised incidentally by other tests.
"""

from __future__ import annotations

from pathlib import Path

from conductor import rundir


def test_runs_dir_is_never_the_real_home() -> None:
    real_home_runs_dir = Path.home() / ".conductor" / "runs"
    assert rundir.runs_dir() != real_home_runs_dir
