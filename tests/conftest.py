"""Pytest configuration and shared fixtures for Conductor tests.

This module contains fixtures used across multiple test modules. It also
defines a collection hook (``pytest_collection_modifyitems``) that auto-skips
``@pytest.mark.real_api`` and ``@pytest.mark.install_scripts`` tests unless
explicitly selected via ``-m`` — see its docstring and issues #326 / #331 for
the full rationale.
"""

import re
import tempfile
from pathlib import Path

import pytest

# Enables the `pytester` fixture used by tests/test_config/test_real_api_marker.py
# and tests/test_config/test_install_scripts_marker.py to exercise this file's
# collection hook via an inner pytest run.
pytest_plugins = ["pytester"]

# Marker names that are opt-in by default: unless the caller's `-m`
# expression explicitly references one of these (by name, as a whole word),
# tests carrying it are skipped rather than executed. See the function
# docstring below for the full rationale and per-invocation behavior.
_OPT_IN_MARKER_NAMES = ("real_api", "install_scripts")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Make opt-in markers (``real_api``, ``install_scripts``) skip by default.

    Without this hook, nothing deselects tests carrying one of
    ``_OPT_IN_MARKER_NAMES`` unless the caller's ``-m`` expression already
    references that marker name (as CI/release workflows do). Both markers
    gate tests that can reach out and disrupt the *host* environment:
    ``real_api`` spawns real Copilot/Claude subprocesses (issue #326);
    ``install_scripts`` drives the install scripts' host-wide
    process-killing ``--auto-stop`` path (issue #331). Either can collide
    with and kill a live ``conductor run --web-bg`` session.

    This hook is load-bearing to different degrees per marker and per
    invocation: a plain ``pytest`` or ``pytest -m "not performance"`` never
    mentions either marker, so both are only caught by this hook. ``make
    test``'s own ``-m "not install_scripts"`` (see ``Makefile``) already
    deselects ``install_scripts`` independently via pytest's native
    marker-expression evaluation — but it never mentions ``real_api``, so
    that marker still relies on this hook there too.

    For each marker name, if the caller's ``-m`` expression already
    references it (e.g. ``-m real_api`` / ``-m install_scripts`` to opt in,
    or CI's ``-m "not real_api and not performance"``), pytest's own
    marker-expression evaluation already produces the correct
    selection/deselection, so this hook steps aside for that marker.
    """
    marker_expr = config.getoption("markexpr")
    for mark_name in _OPT_IN_MARKER_NAMES:
        # Matches the marker name as a whole word (not merely a substring)
        # inside the `-m` expression, e.g. "install_scripts", "not
        # install_scripts", "not real_api and not performance" all match for
        # their respective marker; "install_scripts_other" does not.
        if re.search(rf"\b{re.escape(mark_name)}\b", marker_expr):
            continue  # explicitly referenced; pytest's own evaluation handles it

        skip = pytest.mark.skip(reason=f"{mark_name} test: opt in with -m {mark_name}")
        for item in items:
            if mark_name in item.keywords:
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolate_event_log_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect ``tempfile.gettempdir()`` to this test's own ``tmp_path`` for
    *every* test, not just the ones that already know to isolate it.

    ``conductor.fleet.retention.event_log_root()`` (and every other caller
    of ``tempfile.gettempdir()`` -- ``engine/event_log.py``,
    ``cli/bg_runner.py``) resolve ``$TMPDIR/conductor/`` from this same
    function. Since Fleet Manager E5, ``run_workflow_async`` /
    ``resume_workflow_async`` call ``maybe_prune_event_logs()`` on startup
    with retention *enabled by default* (``keep_last = 200``) -- without
    this guard, any test that drives those functions (directly, or via a
    CLI command) prunes the *developer's actual* ``$TMPDIR/conductor/``,
    permanently deleting real ``conductor replay`` material. This was
    reproduced empirically: planting sentinel logs in the real
    ``/tmp/conductor`` and running a single wiring test alone deleted two of
    them.

    Returns pytest's own ``tmp_path`` verbatim (not a nested subdirectory
    of it) so that any other code in the same test which independently
    builds a path under ``tmp_path`` and compares it against
    ``tempfile.gettempdir()`` (e.g. ``mcp/manager.py``'s spill-dir
    symlink policy, which checks ``spill_dir.is_relative_to(temp_root)``)
    still sees a consistent root -- a nested subdirectory would make such
    paths spuriously "outside" the patched temp root and change behavior
    unrelated to this guard's purpose.

    An autouse fixture here means no future test needs to remember to
    isolate this itself. Tests that need a specific isolated root (e.g.
    ``tests/test_fleet/test_retention.py``'s own ``temp_root`` fixture) can
    still patch ``tempfile.gettempdir`` again themselves -- the later
    ``monkeypatch.setattr`` simply wins, and both this fixture and theirs
    ultimately point at a location under pytest's own ``tmp_path``, never
    the real system temp directory.
    """
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the path to the test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_workflow_yaml() -> str:
    """Return a minimal valid workflow YAML for testing."""
    return """\
workflow:
  name: test-workflow
  description: A test workflow
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello, world!"
    routes:
      - to: $end
"""


@pytest.fixture
def tmp_workflow_file(tmp_path: Path, sample_workflow_yaml: str) -> Path:
    """Create a temporary workflow YAML file."""
    workflow_file = tmp_path / "test-workflow.yaml"
    workflow_file.write_text(sample_workflow_yaml)
    return workflow_file
