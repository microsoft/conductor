"""End-to-end coverage for file-backed prompt includes in workflows."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conductor.config.loader import ConfigLoader
from conductor.config.schema import AgentDef, WorkflowConfig
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import ExecutionError, TemplateError
from conductor.providers.copilot import CopilotProvider

MockHandler = Callable[[AgentDef, str, dict[str, Any]], dict[str, Any]]


def _load_workflow(workflow_file: Path) -> WorkflowConfig:
    loader = ConfigLoader()
    return loader.load(workflow_file)


def _write_prompt_files(tmp_path: Path, *, main_text: str, partial_text: str) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "main.md.jinja").write_text(main_text)
    (prompts_dir / "_shared.md.jinja").write_text(partial_text)


@pytest.mark.asyncio
async def test_workflow_with_prompt_file_include(tmp_path: Path) -> None:
    """Pipeline renders an included partial from a file-backed agent prompt."""
    # Requirement: !file -> env resolution -> Pydantic -> AgentExecutor renders
    # prompt includes before the provider receives the prompt.
    _write_prompt_files(
        tmp_path,
        main_text="Main says {{ workflow.input.topic }}. {% include '_shared.md.jinja' %}",
        partial_text="Shared partial uses the include body.",
    )
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        """
workflow:
  name: prompt-file-include
  entry_point: answerer
agents:
  - name: answerer
    model: gpt-4
    prompt: !file prompts/main.md.jinja
    output:
      answer:
        type: string
    routes:
      - to: $end
output:
  answer: "{{ answerer.output.answer }}"
""".strip()
    )
    received_prompts: list[str] = []

    def mock_handler(agent: AgentDef, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        received_prompts.append(prompt)
        return {"answer": agent.name}

    config = _load_workflow(workflow_file)
    engine = WorkflowEngine(
        config,
        CopilotProvider(mock_handler=mock_handler),
        workflow_path=workflow_file,
    )

    result = await engine.run({"topic": "integration"})

    assert result == {"answer": "answerer"}
    assert received_prompts == ["Main says integration. Shared partial uses the include body."]


@pytest.mark.asyncio
async def test_workflow_with_system_prompt_file_include(tmp_path: Path) -> None:
    """Pipeline renders an included partial from a file-backed system prompt."""
    # Requirement: system_prompt: !file follows the same include pipeline and the
    # provider receives the rendered system_prompt on the copied AgentDef.
    _write_prompt_files(
        tmp_path,
        main_text="System root {{ workflow.input.role }}. {% include '_shared.md.jinja' %}",
        partial_text="Shared system policy from include.",
    )
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        """
workflow:
  name: system-prompt-file-include
  entry_point: answerer
agents:
  - name: answerer
    model: gpt-4
    system_prompt: !file prompts/main.md.jinja
    prompt: "User prompt"
    output:
      answer:
        type: string
    routes:
      - to: $end
output:
  answer: "{{ answerer.output.answer }}"
""".strip()
    )
    received_system_prompts: list[str | None] = []

    def mock_handler(agent: AgentDef, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        received_system_prompts.append(agent.system_prompt)
        return {"answer": prompt}

    config = _load_workflow(workflow_file)
    engine = WorkflowEngine(
        config,
        CopilotProvider(mock_handler=mock_handler),
        workflow_path=workflow_file,
    )

    result = await engine.run({"role": "reviewer"})

    assert result == {"answer": "User prompt"}
    assert received_system_prompts == ["System root reviewer. Shared system policy from include."]


@pytest.mark.asyncio
async def test_for_each_inline_agent_prompt_file_include(tmp_path: Path) -> None:
    """For-each inline agents keep FileString prompts through shallow copies."""
    # Requirement: inline for_each prompt: !file renders includes for every item
    # and still resolves loop variables after engine model_copy operations.
    _write_prompt_files(
        tmp_path,
        main_text="Item {{ item }} at {{ _index }}. {% include '_shared.md.jinja' %}",
        partial_text="Shared loop marker.",
    )
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        """
workflow:
  name: for-each-file-include
  entry_point: seed
agents:
  - name: seed
    type: set
    value: ready
    routes:
      - to: loop
for_each:
  - name: loop
    type: for_each
    source: workflow.input.items
    as: item
    max_concurrent: 1
    agent:
      name: worker
      model: gpt-4
      prompt: !file prompts/main.md.jinja
      output:
        result:
          type: string
    routes:
      - to: $end
output:
  count: "{{ loop.outputs | length }}"
""".strip()
    )
    received_prompts: list[str] = []

    def mock_handler(agent: AgentDef, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        received_prompts.append(prompt)
        return {"result": agent.name}

    config = _load_workflow(workflow_file)
    engine = WorkflowEngine(
        config,
        CopilotProvider(mock_handler=mock_handler),
        workflow_path=workflow_file,
    )

    result = await engine.run({"items": ["alpha", "beta"]})

    assert result == {"count": 2}
    assert received_prompts == [
        "Item alpha at 0. Shared loop marker.",
        "Item beta at 1. Shared loop marker.",
    ]


@pytest.mark.asyncio
async def test_human_gate_prompt_file_include(tmp_path: Path) -> None:
    """Human gates render file-backed prompts with includes before presentation."""
    # Requirement: human_gate uses the shared renderer, so prompt: !file includes
    # surface in the observable gate_presented event under skip_gates execution.
    _write_prompt_files(
        tmp_path,
        main_text="Gate asks {{ workflow.input.request }}. {% include '_shared.md.jinja' %}",
        partial_text="Shared gate copy from include.",
    )
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        """
workflow:
  name: human-gate-file-include
  entry_point: approval
agents:
  - name: approval
    type: human_gate
    prompt: !file prompts/main.md.jinja
    options:
      - label: Approve
        value: approved
        route: $end
output:
  selected: "{{ approval.output.selected }}"
""".strip()
    )
    events: list[WorkflowEvent] = []
    emitter = WorkflowEventEmitter()
    emitter.subscribe(events.append)

    config = _load_workflow(workflow_file)
    engine = WorkflowEngine(
        config,
        CopilotProvider(mock_handler=lambda agent, prompt, context: {}),
        skip_gates=True,
        workflow_path=workflow_file,
        event_emitter=emitter,
    )

    result = await engine.run({"request": "decision"})

    gate_prompts = [event.data["prompt"] for event in events if event.type == "gate_presented"]
    assert result == {"selected": "approved"}
    assert gate_prompts == ["Gate asks decision. Shared gate copy from include."]


@pytest.mark.asyncio
async def test_missing_include_raises_template_error(tmp_path: Path) -> None:
    """Missing file-backed includes fail with the missing template name."""
    # Requirement: malformed include references fail the integration path and
    # expose the missing include name regardless of wrapper exception wording.
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "main.md.jinja").write_text("Main {% include '_missing.md.jinja' %}")
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        """
workflow:
  name: missing-include
  entry_point: answerer
agents:
  - name: answerer
    model: gpt-4
    prompt: !file prompts/main.md.jinja
    output:
      answer:
        type: string
    routes:
      - to: $end
output:
  answer: "{{ answerer.output.answer }}"
""".strip()
    )

    def mock_handler(agent: AgentDef, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        return {"answer": "unreachable"}

    config = _load_workflow(workflow_file)
    engine = WorkflowEngine(
        config,
        CopilotProvider(mock_handler=mock_handler),
        workflow_path=workflow_file,
    )

    with pytest.raises((ExecutionError, TemplateError)) as exc_info:
        await engine.run({})

    assert "_missing.md.jinja" in str(exc_info.value)
