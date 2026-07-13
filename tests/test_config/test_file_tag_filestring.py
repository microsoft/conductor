"""Tests for the FileString metadata preservation and coercion for the !file tag."""

from __future__ import annotations

from pathlib import Path

from conductor.config.loader import ConfigLoader
from conductor.file_string import FileString


def test_file_tag_returns_file_string_for_raw_content(tmp_path: Path) -> None:
    # Requirement: !file loaded raw string content returns FileString
    # preserving path before validation.
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("Hello prompt content")

    loader = ConfigLoader()
    loader._constructor_cls._base_dir = tmp_path
    loader._constructor_cls._file_stack = []

    result = loader._yaml.load("!file prompt.md")
    # Clean up constructor state
    loader._constructor_cls._base_dir = Path(".")
    loader._constructor_cls._file_stack = []

    assert isinstance(result, FileString)
    assert result == "Hello prompt content"
    assert result.source_path == (tmp_path / "prompt.md").resolve()


def test_file_tag_non_prompt_fields_coerce_to_str(tmp_path: Path) -> None:
    # Requirement: non-prompt fields loaded via !file (command, stdin, reason, value)
    # coerce to plain str after Pydantic validation (wrap-validators are not yet registered).
    (tmp_path / "cmd.sh").write_text("echo 'hello'")
    (tmp_path / "payload.txt").write_text("stdin content")
    (tmp_path / "reason.md").write_text("termination completed")
    (tmp_path / "expr.j2").write_text("hello {{ value }}")

    yaml_content = """\
workflow:
  name: test-coercion
  entry_point: script_agent

agents:
  - name: script_agent
    type: script
    command: !file cmd.sh
    stdin: !file payload.txt
    routes:
      - to: set_step
  - name: set_step
    type: set
    value: !file expr.j2
    routes:
      - to: terminate_step
  - name: terminate_step
    type: terminate
    status: success
    reason: !file reason.md
"""

    loader = ConfigLoader()
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(yaml_content)

    config = loader.load(workflow_file)

    agents_map = {agent.name: agent for agent in config.agents}
    script_agent = agents_map["script_agent"]
    set_step = agents_map["set_step"]
    terminate_step = agents_map["terminate_step"]

    # Verify that command is coerced to plain str and is not FileString
    assert isinstance(script_agent.command, str)
    assert not isinstance(script_agent.command, FileString)
    assert script_agent.command == "echo 'hello'"

    # Verify that stdin is coerced to plain str and is not FileString
    assert isinstance(script_agent.stdin, str)
    assert not isinstance(script_agent.stdin, FileString)
    assert script_agent.stdin == "stdin content"

    # Verify that value is coerced to plain str and is not FileString
    assert isinstance(set_step.value, str)
    assert not isinstance(set_step.value, FileString)
    assert set_step.value == "hello {{ value }}"

    # Verify that reason is coerced to plain str and is not FileString
    assert isinstance(terminate_step.reason, str)
    assert not isinstance(terminate_step.reason, FileString)
    assert terminate_step.reason == "termination completed"


def test_file_tag_parsed_yaml_returns_plain_dict(tmp_path: Path) -> None:
    # Requirement: !file on a file containing YAML dict/list returns
    # a plain dict/list, not FileString.
    dict_file = tmp_path / "schema.yaml"
    dict_file.write_text("foo: bar\nkey: value")

    loader = ConfigLoader()
    loader._constructor_cls._base_dir = tmp_path
    loader._constructor_cls._file_stack = []

    result = loader._yaml.load("!file schema.yaml")
    # Clean up constructor state
    loader._constructor_cls._base_dir = Path(".")
    loader._constructor_cls._file_stack = []

    assert isinstance(result, dict)
    assert not isinstance(result, FileString)
    assert result == {"foo": "bar", "key": "value"}
