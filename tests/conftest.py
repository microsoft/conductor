"""Pytest configuration and shared fixtures for Conductor tests.

This module contains fixtures used across multiple test modules. It also
defines a collection hook (``pytest_collection_modifyitems``) that auto-skips
``@pytest.mark.real_api`` and ``@pytest.mark.install_scripts`` tests unless
explicitly selected via ``-m`` — see its docstring and issues #326 / #331 for
the full rationale.
"""

import re
from pathlib import Path

import pytest

# Enables the `pytester` fixture used by tests/test_config/test_real_api_marker.py
# and tests/test_config/test_install_scripts_marker.py to exercise this file's
# collection hook via an inner pytest run.
pytest_plugins = ["pytester"]

# Marker names that are opt-in by default: unless the caller's `-m`
# expression explicitly references one of these (by name, as a whole word),
# tests carrying it are skipped rather than executed. Both markers gate tests
# that can reach out and disrupt the *host* environment — real_api spawns
# real Copilot/Claude subprocesses (issue #326); install_scripts drives
# install.sh/install.ps1's `--auto-stop`, which SIGTERM-kills every live
# `conductor` process on the machine (issue #331) — so both must be excluded
# from a plain `pytest` / `pytest -m "not performance"` run, not just from
# `make test`.
_OPT_IN_MARKER_NAMES = ("real_api", "install_scripts")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Make opt-in markers (``real_api``, ``install_scripts``) skip by default.

    Without this hook, nothing deselects tests carrying one of
    ``_OPT_IN_MARKER_NAMES`` unless the caller passes an explicit ``-m``
    expression (as CI/release workflows do). A plain ``pytest``,
    ``pytest -m "not performance"``, or ``make test`` would otherwise run
    them — spawning real Copilot/Claude subprocesses (``real_api``, issue
    #326) or driving the install scripts' host-wide process-killing
    ``--auto-stop`` path (``install_scripts``, issue #331). Either can
    collide with and kill a live ``conductor run --web-bg`` session.

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
