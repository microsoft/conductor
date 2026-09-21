"""Tests for the !file and !yamlfile YAML tag functionality."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.config.loader import ConfigLoader
from conductor.exceptions import ConfigurationError

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "file_tag"


class TestFileTagStringContent:
    """Tests for !file always loading raw string content, never YAML-parsed."""

    def test_file_tag_loads_md_as_string(self) -> None:
        """!file loads a .md file as raw string into prompt field."""
        loader = ConfigLoader()
        config = loader.load(FIXTURES_DIR / "main.yaml")

        assert "You are a helpful assistant." in config.agents[0].prompt
        assert "provide a detailed response" in config.agents[0].prompt

    def test_file_tag_scalar_yaml_as_string(self) -> None:
        """A file containing only a YAML scalar is returned as raw string."""
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

    def test_file_tag_never_sniffs_yaml_shaped_markdown(self) -> None:
        """!file on prose that happens to parse as YAML still stays a string.

        yaml_shaped.md's prose is a valid YAML mapping (a line ending in ':'
        followed by a '- ' bullet list). Under !rawfile-era !file this used to
        be sniffed and returned as a dict, rejecting a str-typed field like
        system_prompt. !file no longer sniffs at all, so this loads cleanly.
        """
        loader = ConfigLoader()
        yaml_content = """\
workflow:
  name: file-tag-no-trap
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    system_prompt: !file yaml_shaped.md
    prompt: "Hello"
    routes:
      - to: $end
"""
        config = loader.load_string(
            yaml_content,
            source_path=FIXTURES_DIR / "file_tag_no_trap.yaml",
        )
        assert isinstance(config.agents[0].system_prompt, str)
        assert "Summarize the changelog" in config.agents[0].system_prompt


class TestYamlfileTagStructuredContent:
    """Tests for !yamlfile explicitly parsing structured YAML content."""

    def test_yamlfile_tag_loads_yaml_as_dict(self) -> None:
        """!yamlfile loads a .yaml file as parsed dict into output field."""
        loader = ConfigLoader()
        config = loader.load(FIXTURES_DIR / "main.yaml")

        output = config.agents[0].output
        assert isinstance(output, dict)
        assert "summary" in output
        assert output["summary"].type == "string"
        assert "score" in output
        assert output["score"].type == "number"

    def test_yamlfile_tag_in_list(self) -> None:
        """!yamlfile works inside YAML list items for agent tools."""
        loader = ConfigLoader()
        yaml_content = """\
workflow:
  name: list-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    tools: !yamlfile list_items.yaml
    routes:
      - to: $end
"""
        config = loader.load_string(
            yaml_content,
            source_path=FIXTURES_DIR / "list_test.yaml",
        )
        assert "tool1" in config.agents[0].tools
        assert "tool2" in config.agents[0].tools

    def test_yamlfile_tag_parses_yaml_shaped_markdown_as_dict(self) -> None:
        """!yamlfile is the explicit opt-in to parse prose that happens to be YAML-shaped."""
        loader = ConfigLoader()
        loader._constructor_cls._base_dir = FIXTURES_DIR
        loader._constructor_cls._file_stack = []

        result = loader._yaml.load("!yamlfile yaml_shaped.md")

        loader._constructor_cls._base_dir = Path(".")
        loader._constructor_cls._file_stack = []

        assert isinstance(result, dict)
        assert any("Summarize the changelog" in k for k in result)

    def test_yamlfile_tag_parses_scalar_documents(self) -> None:
        """!yamlfile parses a scalar document instead of returning it as text.

        jrob5756's blocking review comment: !yamlfile only honored its
        "always parse as YAML" contract for mappings and sequences, so a bare
        true/42/quoted string came back as the raw file text instead of the
        parsed bool/int/str.
        """
        loader = ConfigLoader()
        loader._constructor_cls._base_dir = FIXTURES_DIR
        loader._constructor_cls._file_stack = []

        bool_result = loader._yaml.load("!yamlfile scalar_bool.yaml")
        number_result = loader._yaml.load("!yamlfile scalar_number.yaml")

        loader._constructor_cls._base_dir = Path(".")
        loader._constructor_cls._file_stack = []

        assert bool_result is True
        assert number_result == 42

    def test_yamlfile_missing_file_raises_configuration_error(self) -> None:
        """!yamlfile shares !file's missing-file error handling."""
        loader = ConfigLoader()
        yaml_content = """\
workflow:
  name: yamlfile-missing-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    output: !yamlfile nonexistent.yaml
    routes:
      - to: $end
"""
        with pytest.raises(ConfigurationError, match="File not found"):
            loader.load_string(
                yaml_content,
                source_path=FIXTURES_DIR / "yamlfile_missing_test.yaml",
            )

    def test_yamlfile_reports_malformed_yaml_instead_of_falling_back(self) -> None:
        """!yamlfile raises on malformed YAML rather than silently returning a string.

        This is the behavior change jrob5756 asked for: the old !file heuristic
        caught any YAMLError and fell back to raw text, which meant a typo in a
        structured include failed at the point of use (a type error against
        whatever field it landed in, or worse, silently) rather than at the
        point of the actual syntax mistake.
        """
        loader = ConfigLoader()
        yaml_content = """\
workflow:
  name: yamlfile-malformed-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    output: !yamlfile malformed.yaml
    routes:
      - to: $end
"""
        with pytest.raises(ConfigurationError, match="not valid YAML"):
            loader.load_string(
                yaml_content,
                source_path=FIXTURES_DIR / "yamlfile_malformed_test.yaml",
            )


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
    """Tests for nested !file/!yamlfile tag support."""

    def test_nested_file_tags_resolve(self) -> None:
        """A !file tag nested inside a !yamlfile include works correctly."""
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
    prompt: !yamlfile cycle_a.yaml
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
