"""Parameter flow verification tests.

These tests verify that parameters ACTUALLY reach the Pydantic AI model
boundary, addressing the reviewer concern: 'No verification that temperature,
max_tokens actually reach the Anthropic SDK API calls'.

With the Pydantic AI refactor, the SDK call is made by the pydantic-ai
AnthropicModel. The canonical seam to assert on is ``build_agent``: it
receives the resolved temperature/max_tokens and stores them in the agent's
``model_settings`` (an ``AnthropicModelSettings`` TypedDict). The tests run the
full WorkflowEngine path and inspect the constructed Pydantic AI agent.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from conductor.config.loader import load_workflow
from conductor.engine.workflow import WorkflowEngine
from conductor.providers._pydantic_ai import agent_builder as _ab
from conductor.providers.factory import create_provider


def _build_stub_agent(data: dict[str, Any]) -> Agent[Any, Any]:
    class DynamicModel(BaseModel):
        model_config = {"extra": "allow"}

    return Agent(model=TestModel(custom_output_args=data), output_type=DynamicModel)


def _capture_build_agent(
    captured: dict[str, Any],
    output_data: dict[str, Any],
) -> Any:
    """Return a patch that captures the real Agent's model_settings.

    The returned Agent is built by the real ``build_agent`` so its
    ``model_settings`` reflect the actual temperature/max_tokens flow, while a
    TestModel-backed replacement avoids network calls.
    """
    target = "conductor.providers._pydantic_ai.agent_builder.build_agent"
    real_build_agent = _ab.build_agent

    def make_agent(**kwargs: Any) -> Agent[Any, Any]:
        agent = real_build_agent(**kwargs)
        captured["model_settings"] = agent.model_settings
        captured["agent"] = kwargs.get("agent")

        return _build_stub_agent(output_data)

    return patch(target, side_effect=make_agent)


class TestParameterFlowToPydanticAiAgent:
    """Verify temperature and max_tokens reach the Pydantic AI Agent model_settings."""

    @pytest.mark.asyncio
    async def test_temperature_reaches_model_settings(self, tmp_path, monkeypatch) -> None:
        """Workflow runtime temperature is forwarded to Agent.model_settings."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        workflow_yaml = tmp_path / "test_temp.yaml"
        workflow_yaml.write_text("""
workflow:
  name: test-temperature
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: claude
    temperature: 0.42

agents:
  - name: agent1
    prompt: "test"
    output:
      result:
        type: string
    routes:
      - to: $end
""")

        config = load_workflow(str(workflow_yaml))
        provider = await create_provider(
            "claude",
            validate=False,
            temperature=config.workflow.runtime.temperature,
        )
        engine = WorkflowEngine(config, provider)
        captured: dict[str, Any] = {}

        with _capture_build_agent(captured, {"result": "Test response"}):
            await engine.run({})

        assert captured["model_settings"]["temperature"] == 0.42, (
            f"Expected temperature=0.42 in model_settings, got {captured['model_settings']}"
        )

        await provider.close()

    @pytest.mark.asyncio
    async def test_max_tokens_reaches_model_settings(self, tmp_path, monkeypatch) -> None:
        """Workflow runtime max_tokens is forwarded to Agent.model_settings."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        workflow_yaml = tmp_path / "test_max_tokens.yaml"
        workflow_yaml.write_text("""
workflow:
  name: test-max-tokens
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: claude
    max_tokens: 2048

agents:
  - name: agent1
    prompt: "test"
    output:
      result:
        type: string
    routes:
      - to: $end
""")

        config = load_workflow(str(workflow_yaml))
        provider = await create_provider(
            "claude",
            validate=False,
            max_tokens=config.workflow.runtime.max_tokens,
        )
        engine = WorkflowEngine(config, provider)
        captured: dict[str, Any] = {}

        with _capture_build_agent(captured, {"result": "Test response"}):
            await engine.run({})

        assert captured["model_settings"]["max_tokens"] == 2048, (
            f"Expected max_tokens=2048 in model_settings, got {captured['model_settings']}"
        )

        await provider.close()

    @pytest.mark.asyncio
    async def test_all_parameters_together_reach_model_settings(
        self, tmp_path, monkeypatch
    ) -> None:
        """Temperature and max_tokens both flow to Agent.model_settings simultaneously."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        workflow_yaml = tmp_path / "test_all_params.yaml"
        workflow_yaml.write_text("""
workflow:
  name: test-all-params
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: claude
    temperature: 0.75
    max_tokens: 4096

agents:
  - name: agent1
    prompt: "test"
    output:
      result:
        type: string
    routes:
      - to: $end
""")

        config = load_workflow(str(workflow_yaml))
        provider = await create_provider(
            "claude",
            validate=False,
            temperature=config.workflow.runtime.temperature,
            max_tokens=config.workflow.runtime.max_tokens,
        )
        engine = WorkflowEngine(config, provider)
        captured: dict[str, Any] = {}

        with _capture_build_agent(captured, {"result": "Test response"}):
            await engine.run({})

        assert captured["model_settings"]["temperature"] == 0.75
        assert captured["model_settings"]["max_tokens"] == 4096

        await provider.close()

    @pytest.mark.asyncio
    async def test_none_parameters_use_defaults(self, tmp_path, monkeypatch) -> None:
        """When parameters are omitted, provider defaults are used."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        workflow_yaml = tmp_path / "test_none_params.yaml"
        workflow_yaml.write_text("""
workflow:
  name: test-none-params
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: claude

agents:
  - name: agent1
    prompt: "test"
    output:
      result:
        type: string
    routes:
      - to: $end
""")

        config = load_workflow(str(workflow_yaml))
        provider = await create_provider("claude", validate=False)
        engine = WorkflowEngine(config, provider)
        captured: dict[str, Any] = {}

        with _capture_build_agent(captured, {"result": "Test response"}):
            await engine.run({})

        # ClaudeProvider defaults: temperature=None, max_tokens=8192.
        # _build_model_settings only includes temperature when it is not None.
        assert captured["model_settings"].get("temperature") is None
        assert captured["model_settings"]["max_tokens"] == 8192

        await provider.close()


class TestExcludeNoneInSerialization:
    """Verify exclude_none=True prevents Claude fields in serialized Copilot configs."""

    @pytest.mark.asyncio
    async def test_exclude_none_during_workflow_execution(self, tmp_path) -> None:
        """Test that exclude_none=True works during actual workflow execution."""
        workflow_yaml = tmp_path / "copilot_workflow.yaml"
        workflow_yaml.write_text("""
workflow:
  name: copilot-workflow
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: copilot

agents:
  - name: agent1
    prompt: "test"
    output:
      result:
        type: string
    routes:
      - to: $end
""")

        config = load_workflow(str(workflow_yaml))

        # Simulate config persistence/transmission during workflow execution
        serialized = config.model_dump(mode="json", exclude_none=True)

        # Verify Claude fields are completely absent
        runtime = serialized["workflow"]["runtime"]
        claude_fields = ["temperature", "max_tokens"]

        for field in claude_fields:
            assert field not in runtime, (
                f"Claude field '{field}' should not be in serialized Copilot config"
            )

        # Verify Copilot provider is present
        assert runtime["provider"] == "copilot"

    @pytest.mark.asyncio
    async def test_exclude_none_with_partial_claude_params(self, tmp_path) -> None:
        """Test exclude_none with some Claude params set, others None."""
        workflow_yaml = tmp_path / "partial_claude.yaml"
        workflow_yaml.write_text("""
workflow:
  name: partial-claude
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: claude
    temperature: 0.7

agents:
  - name: agent1
    prompt: "test"
    output:
      result:
        type: string
    routes:
      - to: $end
""")

        config = load_workflow(str(workflow_yaml))
        serialized = config.model_dump(mode="json", exclude_none=True)

        runtime = serialized["workflow"]["runtime"]

        # temperature is set, should be present
        assert "temperature" in runtime
        assert runtime["temperature"] == 0.7

        # Other Claude params are None, should be excluded
        assert "max_tokens" not in runtime
