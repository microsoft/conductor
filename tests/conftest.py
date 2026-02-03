"""Pytest configuration and shared fixtures for Conductor tests.

This module contains fixtures used across multiple test modules.
"""

from pathlib import Path

import pytest


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
