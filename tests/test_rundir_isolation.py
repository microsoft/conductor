"""Meta-test: the global test-suite isolation for ``rundir.runs_dir()`` holds.

The autouse ``_isolated_runs_dir`` fixture in ``tests/conftest.py`` points
``conductor.rundir.runs_dir()`` at a per-test temp directory so no test
reads or writes the developer's real ``~/.conductor/runs`` (issue #397's
PID/token registry). This test asserts that invariant directly rather than
relying on it only being exercised incidentally by other tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor import rundir

# Bound to the real implementation at import time, before the autouse
# _isolated_runs_dir fixture (tests/conftest.py) monkeypatches
# ``rundir.runs_dir`` for the duration of each test.
_real_runs_dir = rundir.runs_dir


def test_runs_dir_is_never_the_real_home() -> None:
    real_home_runs_dir = Path.home() / ".conductor" / "runs"
    assert rundir.runs_dir() != real_home_runs_dir


def test_runs_dir_resolves_under_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real implementation resolves to ``<home>/.conductor/runs``.

    This is the fact the documented Windows token-file ACL posture (issue
    #425) rests on: the token file's protection comes from inheriting its
    parent directory's ACL, and that parent is ``Path.home()``-relative,
    unlike the ``CONDUCTOR_HOME``-aware roots in ``registry/config.py``,
    ``registry/cache.py`` and ``plugins/fetch.py``. Do NOT assert the NTFS
    ACL itself here — the stdlib cannot express it, and the ACL is
    inherited rather than set by Conductor, so such an assertion would pass
    against a build with zero hardening.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _real_runs_dir() == tmp_path / ".conductor" / "runs"
