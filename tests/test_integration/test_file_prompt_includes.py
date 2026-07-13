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
from conductor.exceptions import ConfigurationError, ExecutionError, TemplateError
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


@pytest.mark.asyncio
async def test_partial_env_var_resolved_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """${VAR} inside an included partial resolves at render time through the pipeline."""
    # Requirement: partial files go through the same env resolver as the root
    # prompt. The env var is set only AFTER config load to prove resolution
    # happens at render time, not from a load-time snapshot.
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "main.md.jinja").write_text(
        "Main {{ workflow.input.topic }}. {% include '_shared.md.jinja' %}"
    )
    (prompts_dir / "_shared.md.jinja").write_text(
        "Partial env: ${CONDUCTOR_E2E_PARTIAL_VAR}; defaulted: ${CONDUCTOR_E2E_UNSET:-fallback}."
    )
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        """
workflow:
  name: partial-env-e2e
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

    monkeypatch.delenv("CONDUCTOR_E2E_PARTIAL_VAR", raising=False)
    monkeypatch.delenv("CONDUCTOR_E2E_UNSET", raising=False)

    config = _load_workflow(workflow_file)
    engine = WorkflowEngine(
        config,
        CopilotProvider(mock_handler=mock_handler),
        workflow_path=workflow_file,
    )

    monkeypatch.setenv("CONDUCTOR_E2E_PARTIAL_VAR", "resolved-value")
    result = await engine.run({"topic": "integration"})

    assert result == {"answer": "answerer"}
    assert received_prompts == [
        "Main integration. Partial env: resolved-value; defaulted: fallback."
    ]


@pytest.mark.asyncio
async def test_partial_unset_required_env_var_fails_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset required ${VAR} in a partial fails the run with the normal configuration error."""
    # Requirement: the error surfaces as ConfigurationError (not a generic
    # template failure) so users get the standard env-var guidance.
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "main.md.jinja").write_text("{% include '_shared.md.jinja' %}")
    (prompts_dir / "_shared.md.jinja").write_text("key=${CONDUCTOR_E2E_REQUIRED_VAR}")
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        """
workflow:
  name: partial-env-required
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

    monkeypatch.delenv("CONDUCTOR_E2E_REQUIRED_VAR", raising=False)

    config = _load_workflow(workflow_file)
    engine = WorkflowEngine(
        config,
        CopilotProvider(mock_handler=mock_handler),
        workflow_path=workflow_file,
    )

    with pytest.raises(ConfigurationError) as exc_info:
        await engine.run({})

    msg = str(exc_info.value)
    assert "Required environment variable 'CONDUCTOR_E2E_REQUIRED_VAR' is not set" in msg
    assert "Template rendering failed" not in msg


@pytest.mark.asyncio
async def test_deleted_prompt_source_fails_with_explicit_error(tmp_path: Path) -> None:
    """A prompt file deleted after config load fails with a direct source-path error."""
    # Requirement: no silent downgrade to inline rendering — the error names the
    # missing file and never suggests converting to !file (already in use).
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    main_prompt = prompts_dir / "main.md.jinja"
    main_prompt.write_text("Main {{ workflow.input.topic }}.")
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        """
workflow:
  name: deleted-source
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
    main_prompt.unlink()

    engine = WorkflowEngine(
        config,
        CopilotProvider(mock_handler=mock_handler),
        workflow_path=workflow_file,
    )

    with pytest.raises((ExecutionError, TemplateError)) as exc_info:
        await engine.run({"topic": "integration"})

    msg = str(exc_info.value)
    assert "main.md.jinja" in msg
    assert "no longer available" in msg
    assert "loader-dependent" not in msg
    # The error must not be double-wrapped by the renderer's generic handler.
    assert "Template rendering failed" not in msg
    assert "Check template and context for errors" not in msg
