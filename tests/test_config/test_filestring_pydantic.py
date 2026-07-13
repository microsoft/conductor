"""Tests for Pydantic validation behavior with FileString instances."""

from __future__ import annotations

from pathlib import Path

from conductor.config.schema import AgentDef, WorkflowConfig
from conductor.file_string import FileString


def test_prompt_filestring_preserved() -> None:
    # Requirement: A FileString prompt passed to WorkflowConfig.model_validate
    # must be preserved as a FileString instance in AgentDef.prompt, retaining source_path.
    fs_prompt = FileString("Hello prompt content", "/path/to/prompt.md")

    data = {
        "workflow": {
            "name": "test-workflow",
            "entry_point": "my_agent",
        },
        "agents": [
            {
                "name": "my_agent",
                "prompt": fs_prompt,
            }
        ],
    }

    config = WorkflowConfig.model_validate(data)
    agent = config.agents[0]

    assert isinstance(agent.prompt, FileString)
    assert agent.prompt == "Hello prompt content"
    assert agent.prompt.source_path == Path("/path/to/prompt.md")


def test_system_prompt_filestring_preserved() -> None:
    # Requirement: A FileString system_prompt passed to WorkflowConfig.model_validate
    # must be preserved as a FileString instance in AgentDef.system_prompt, retaining source_path.
    fs_sys = FileString("Hello system content", "/path/to/sys.md")

    data = {
        "workflow": {
            "name": "test-workflow",
            "entry_point": "my_agent",
        },
        "agents": [
            {
                "name": "my_agent",
                "prompt": "some prompt",
                "system_prompt": fs_sys,
            }
        ],
    }

    config = WorkflowConfig.model_validate(data)
    agent = config.agents[0]

    assert isinstance(agent.system_prompt, FileString)
    assert agent.system_prompt == "Hello system content"
    assert agent.system_prompt.source_path == Path("/path/to/sys.md")


def test_plain_string_prompt_stays_str() -> None:
    # Requirement: A plain string prompt must remain a plain string (exact type str)
    # after validation, without being converted to FileString or any other type.
    data = {
        "workflow": {
            "name": "test-workflow",
            "entry_point": "my_agent",
        },
        "agents": [
            {
                "name": "my_agent",
                "prompt": "plain prompt",
            }
        ],
    }

    config = WorkflowConfig.model_validate(data)
    agent = config.agents[0]

    assert type(agent.prompt) is str
    assert not isinstance(agent.prompt, FileString)


def test_model_dump_json_returns_plain_string() -> None:
    # Requirement: model_dump(mode="json") serializes FileString prompt as a plain string.
    fs_prompt = FileString("Hello prompt content", "/path/to/prompt.md")

    data = {
        "workflow": {
            "name": "test-workflow",
            "entry_point": "my_agent",
        },
        "agents": [
            {
                "name": "my_agent",
                "prompt": fs_prompt,
            }
        ],
    }

    config = WorkflowConfig.model_validate(data)
    dumped = config.model_dump(mode="json")

    prompt_val = dumped["agents"][0]["prompt"]
    assert type(prompt_val) is str
    assert prompt_val == "Hello prompt content"


def test_model_copy_shallow_preserves_filestring() -> None:
    # Requirement: Shallow copying of an AgentDef (e.g. model_copy())
    # preserves the FileString subclass.
    fs_prompt = FileString("Hello prompt content", "/path/to/prompt.md")
    fs_sys = FileString("Hello system content", "/path/to/sys.md")

    agent = AgentDef(
        name="my_agent",
        prompt=fs_prompt,
        system_prompt=fs_sys,
    )

    copied = agent.model_copy()

    assert isinstance(copied.prompt, FileString)
    assert copied.prompt.source_path == Path("/path/to/prompt.md")
    assert isinstance(copied.system_prompt, FileString)
    assert copied.system_prompt.source_path == Path("/path/to/sys.md")


def test_model_dump_validate_roundtrip_yields_plain_str() -> None:
    # Requirement: Running a round-trip of model_dump(mode="json") and then validating
    # the output yields plain strings (loss of FileString on JSON serialization).
    fs_prompt = FileString("Hello prompt content", "/path/to/prompt.md")

    data = {
        "workflow": {
            "name": "test-workflow",
            "entry_point": "my_agent",
        },
        "agents": [
            {
                "name": "my_agent",
                "prompt": fs_prompt,
            }
        ],
    }

    config = WorkflowConfig.model_validate(data)
    dumped = config.model_dump(mode="json")

    revalidated = WorkflowConfig.model_validate(dumped)
    agent = revalidated.agents[0]

    assert type(agent.prompt) is str
    assert not isinstance(agent.prompt, FileString)


def test_none_system_prompt_stays_none() -> None:
    # Requirement: An omitted or None system_prompt must validate to None and not trigger errors.
    data = {
        "workflow": {
            "name": "test-workflow",
            "entry_point": "my_agent",
        },
        "agents": [
            {
                "name": "my_agent",
                "prompt": "some prompt",
                "system_prompt": None,
            }
        ],
    }

    config = WorkflowConfig.model_validate(data)
    agent = config.agents[0]

    assert agent.system_prompt is None
