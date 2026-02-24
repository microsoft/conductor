"""Tests for the !file YAML tag functionality."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.config.loader import ConfigLoader
from conductor.exceptions import ConfigurationError

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "file_tag"


class TestFileTagStringContent:
    """Tests for !file loading raw string content."""

    def test_file_tag_loads_md_as_string(self) -> None:
        """!file loads a .md file as raw string into prompt field."""
        loader = ConfigLoader()
        config = loader.load(FIXTURES_DIR / "main.yaml")

        assert "You are a helpful assistant." in config.agents[0].prompt
        assert "provide a detailed response" in config.agents[0].prompt

    def test_file_tag_scalar_yaml_as_string(self) -> None:
        """YAML file containing only a scalar is returned as raw string."""
        loader = ConfigLoader()
        yaml_content = """\
workflow:
  name: scalar-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: !file scalar.yaml
    routes:
      - to: $end
"""
        config = loader.load_string(
            yaml_content,
            source_path=FIXTURES_DIR / "scalar_test.yaml",
        )
        assert "just a scalar value" in config.agents[0].prompt


class TestFileTagStructuredContent:
    """Tests for !file loading structured YAML content."""

    def test_file_tag_loads_yaml_as_dict(self) -> None:
        """!file loads a .yaml file as parsed dict into output field."""
        loader = ConfigLoader()
        config = loader.load(FIXTURES_DIR / "main.yaml")

        output = config.agents[0].output
        assert isinstance(output, dict)
        assert "summary" in output
        assert output["summary"].type == "string"
        assert "score" in output
        assert output["score"].type == "number"

    def test_file_tag_in_list(self) -> None:
        """!file works inside YAML list items for agent tools."""
        loader = ConfigLoader()
        yaml_content = """\
workflow:
  name: list-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    tools: !file list_items.yaml
    routes:
      - to: $end
"""
        config = loader.load_string(
            yaml_content,
            source_path=FIXTURES_DIR / "list_test.yaml",
        )
        assert "tool1" in config.agents[0].tools
        assert "tool2" in config.agents[0].tools


class TestFileTagRelativePath:
    """Tests for relative path resolution."""

    def test_paths_resolve_relative_to_parent_yaml(self, tmp_path: Path) -> None:
        """Paths resolve relative to parent YAML file, not CWD."""
        # Create a subdirectory with the prompt file
        subdir = tmp_path / "workflows"
        subdir.mkdir()
        prompts_dir = subdir / "prompts"
        prompts_dir.mkdir()

        prompt_file = prompts_dir / "hello.md"
        prompt_file.write_text("Hello from prompt file")

        workflow_file = subdir / "workflow.yaml"
        workflow_file.write_text("""\
workflow:
  name: relative-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: !file prompts/hello.md
    routes:
      - to: $end
""")
        # Load from a different CWD
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            loader = ConfigLoader()
            config = loader.load(workflow_file)
            assert config.agents[0].prompt == "Hello from prompt file"
        finally:
            os.chdir(original_cwd)


class TestFileTagNestedInclusion:
    """Tests for nested !file tag support."""

    def test_nested_file_tags_resolve(self) -> None:
        """Nested !file tags in included files work correctly."""
        loader = ConfigLoader()
        config = loader.load(FIXTURES_DIR / "nested_parent.yaml")

        output = config.agents[0].output
        assert isinstance(output, dict)
        assert "summary" in output
        # The nested_child.yaml has description: !file nested_leaf.md
        # which contains "This is the leaf content from a nested inclusion chain."
        assert "leaf content" in output["summary"].description


class TestFileTagCycleDetection:
    """Tests for circular reference detection."""

    def test_circular_reference_raises(self) -> None:
        """Circular !file references raise ConfigurationError."""
        loader = ConfigLoader()
        yaml_content = """\
workflow:
  name: cycle-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: !file cycle_a.yaml
    routes:
      - to: $end
"""
        with pytest.raises(ConfigurationError) as exc_info:
            loader.load_string(
                yaml_content,
                source_path=FIXTURES_DIR / "cycle_test.yaml",
            )
        assert "Circular file reference" in str(exc_info.value)


class TestFileTagMissingFile:
    """Tests for missing file error handling."""

    def test_missing_file_raises_configuration_error(self) -> None:
        """Missing file raises ConfigurationError with path info."""
        loader = ConfigLoader()
        yaml_content = """\
workflow:
  name: missing-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: !file nonexistent.md
    routes:
      - to: $end
"""
        with pytest.raises(ConfigurationError) as exc_info:
            loader.load_string(
                yaml_content,
                source_path=FIXTURES_DIR / "missing_test.yaml",
            )
        assert "File not found" in str(exc_info.value)
        assert "nonexistent.md" in str(exc_info.value)


class TestFileTagEnvVars:
    """Tests for environment variable resolution in included files."""

    def test_env_vars_in_included_file_resolved(self) -> None:
        """${VAR} in included file is resolved after inclusion."""
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


class TestFileTagNonUtf8:
    """Tests for non-UTF-8 file error handling."""

    def test_non_utf8_file_raises_configuration_error(self, tmp_path: Path) -> None:
        """Non-UTF-8 files produce ConfigurationError with encoding guidance."""
        bad_file = tmp_path / "bad.md"
        bad_file.write_bytes(b"caf\xe9")  # latin-1, not valid UTF-8

        workflow_file = tmp_path / "workflow.yaml"
        workflow_file.write_text("""\
workflow:
  name: encoding-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: !file bad.md
    routes:
      - to: $end
""")
        loader = ConfigLoader()
        with pytest.raises(ConfigurationError, match="not valid UTF-8"):
            loader.load(workflow_file)


class TestFileTagLoadString:
    """Tests for !file with load_string()."""

    def test_load_string_with_source_path(self) -> None:
        """load_string() resolves !file relative to source_path.parent."""
        loader = ConfigLoader()
        yaml_content = """\
workflow:
  name: source-path-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: !file prompt.md
    routes:
      - to: $end
"""
        config = loader.load_string(
            yaml_content,
            source_path=FIXTURES_DIR / "source_path_test.yaml",
        )
        assert "You are a helpful assistant." in config.agents[0].prompt

    def test_load_string_without_source_path_uses_cwd(self, tmp_path: Path) -> None:
        """load_string() without source_path resolves !file relative to CWD."""
        prompt_file = tmp_path / "cwd_prompt.md"
        prompt_file.write_text("CWD prompt content")

        yaml_content = """\
workflow:
  name: cwd-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: !file cwd_prompt.md
    routes:
      - to: $end
"""
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            loader = ConfigLoader()
            config = loader.load_string(yaml_content)
            assert config.agents[0].prompt == "CWD prompt content"
        finally:
            os.chdir(original_cwd)

    def test_load_string_state_reset_after_error(self) -> None:
        """Constructor state is properly reset even after errors."""
        loader = ConfigLoader()
        yaml_content = """\
workflow:
  name: error-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: !file nonexistent_file.md
    routes:
      - to: $end
"""
        with pytest.raises(ConfigurationError):
            loader.load_string(
                yaml_content,
                source_path=FIXTURES_DIR / "error_test.yaml",
            )

        # Verify state is reset - should be able to load a valid config
        valid_yaml = """\
workflow:
  name: after-error
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    routes:
      - to: $end
"""
        config = loader.load_string(valid_yaml)
        assert config.workflow.name == "after-error"
