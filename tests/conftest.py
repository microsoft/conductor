"""Pytest configuration and shared fixtures for Conductor tests.

This module contains fixtures used across multiple test modules.
"""

import re
from pathlib import Path

import pytest

# Enables the `pytester` fixture used by tests/test_config/test_real_api_marker.py
# to exercise this file's collection hook via an inner pytest run.
pytest_plugins = ["pytester"]

# Matches "real_api" as a whole marker name (not merely a substring) inside a
# `-m` expression, e.g. "real_api", "not real_api", "not real_api and not
# performance" all match; "real_api_other" does not.
_REAL_API_IN_MARKEXPR = re.compile(r"\breal_api\b")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Make ``@pytest.mark.real_api`` tests opt-in by default.

    Without this hook, nothing deselects ``real_api``-marked tests unless the
    caller passes an explicit ``-m`` expression (as CI/release workflows do).
    A plain ``pytest``, ``pytest -m "not performance"``, or ``make test`` would
    otherwise spawn real Copilot/Claude subprocesses (see issue #326) — which
    can collide with and kill a live ``conductor run --web-bg`` session.

    If the caller's ``-m`` expression already references ``real_api`` (e.g.
    ``-m real_api`` to opt in, or CI's ``-m "not real_api and not
    performance"``), pytest's own marker-expression evaluation already
    produces the correct selection/deselection, so this hook steps aside.
    """
    marker_expr = config.getoption("-m") or ""
    if _REAL_API_IN_MARKEXPR.search(marker_expr):
        return

    skip_real_api = pytest.mark.skip(reason="real_api test: opt in with -m real_api")
    for item in items:
        if "real_api" in item.keywords:
            item.add_marker(skip_real_api)


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
