"""Tests for environment variable resolution preserving FileString metadata."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from conductor.config.loader import ConfigLoader, _resolve_env_vars_recursive
from conductor.file_string import FileString

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "file_tag"


def test_env_vars_preserve_file_string_origin() -> None:
    # Requirement: _resolve_env_vars_recursive must preserve FileString type and
    # source_path when resolving env vars.
    # Assertion for malformed input / no env vars: must return unchanged but still FileString.
    fs_no_vars = FileString("Hello World!", Path("/tmp/p.md"))
    res_no_vars = _resolve_env_vars_recursive(fs_no_vars)
    assert isinstance(res_no_vars, FileString)
    assert res_no_vars == "Hello World!"
    assert res_no_vars.source_path == Path("/tmp/p.md")

    # With env vars:
    fs_with_vars = FileString("Hello ${TEST_VAR_XYZ}!", Path("/tmp/p.md"))
    with patch.dict(os.environ, {"TEST_VAR_XYZ": "Universe"}):
        res = _resolve_env_vars_recursive(fs_with_vars)
    assert isinstance(res, FileString)
    assert res == "Hello Universe!"
    assert res.source_path == Path("/tmp/p.md")


def test_env_vars_plain_string_stays_plain_str() -> None:
    # Requirement: _resolve_env_vars_recursive must resolve plain str to a
    # plain str (not FileString).
    plain_str = "Hello ${TEST_VAR_XYZ}!"
    with patch.dict(os.environ, {"TEST_VAR_XYZ": "Universe"}):
        res = _resolve_env_vars_recursive(plain_str)
    assert type(res) is str
    assert res == "Hello Universe!"


def test_env_vars_in_included_file_still_resolved_e2e() -> None:
    # Requirement: E2E check that environment variables inside files loaded
    # via !file tag are resolved correctly through ConfigLoader.load_string.
    loader = ConfigLoader()
    yaml_content = """\
workflow:
  name: env-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: !file env_vars.md
    routes:
      - to: $end
"""
    with patch.dict(os.environ, {"TEST_FILE_TAG_VAR": "World"}):
        config = loader.load_string(
            yaml_content,
            source_path=FIXTURES_DIR / "env_test.yaml",
        )
    assert "Hello World" in config.agents[0].prompt
