"""End-to-end integration tests for Claude provider.

Tests the complete flow from schema -> provider -> execution -> output using
the Pydantic AI TestModel seam so no real network calls are made.
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
from conductor.providers.factory import create_provider


def _build_text_agent(text: str) -> Agent[Any, str]:
    """Build a Pydantic AI text agent backed by TestModel."""
    return Agent(model=TestModel(custom_output_text=text), output_type=str)


def _build_structured_agent(data: dict[str, Any]) -> Agent[Any, Any]:
    """Build a Pydantic AI structured-output agent backed by TestModel."""

    class DynamicModel(BaseModel):
        model_config = {"extra": "allow"}

    return Agent(
        model=TestModel(custom_output_args=data),
        output_type=DynamicModel,
    )


class TestEndToEndClaudeIntegration:
    """Verify Claude integration works end-to-end."""

    @pytest.mark.asyncio
    async def test_basic_claude_workflow_execution(self, tmp_path) -> None:
        """Test basic Claude workflow execution with schema validation."""
        # Create workflow YAML with proper schema format
        workflow_yaml = tmp_path / "test_workflow.yaml"
        workflow_yaml.write_text("""
workflow:
  name: test-claude-integration
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: claude
    temperature: 0.7
    max_tokens: 1000
  input:
    question:
      type: string
      required: true

agents:
  - name: agent1
    prompt: "Answer: {{ workflow.input.question }}"
    output:
      result:
        type: string
    routes:
      - to: $end

output:
  answer: "{{ agent1.output.result }}"
""")

        # Load and validate workflow
        config = load_workflow(str(workflow_yaml))
        assert config.workflow.runtime.provider.name == "claude"
        assert config.workflow.runtime.temperature == 0.7
        assert config.workflow.runtime.max_tokens == 1000

        # Execute workflow with the Pydantic AI mock seam
        provider = await create_provider(
            provider_type="claude",
            validate=False,
            temperature=config.workflow.runtime.temperature,
            max_tokens=config.workflow.runtime.max_tokens,
        )
        engine = WorkflowEngine(config, provider)

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_structured_agent({"result": "Test response from Claude"}),
        ) as mock_build_agent:
            result = await engine.run({"question": "What is 2+2?"})

            # Verify execution completed
            assert result is not None
            assert "answer" in result

            # Verify the provider built a Pydantic AI agent with the right settings
            assert mock_build_agent.called
            assert (
                mock_build_agent.call_args.kwargs["default_temperature"] == 0.7
            )
            assert mock_build_agent.call_args.kwargs["default_max_tokens"] == 1000

        await provider.close()

    @pytest.mark.asyncio
    async def test_agent_level_parameter_overrides(self, tmp_path) -> None:
        """Test that agent-level model overrides runtime defaults."""
        workflow_yaml = tmp_path / "test_overrides.yaml"
        workflow_yaml.write_text("""
workflow:
  name: test-overrides
  version: "1.0"
  entry_point: creative
  runtime:
    provider: claude
    temperature: 0.5
  input:
    topic:
      type: string
      required: true

agents:
  - name: creative
    model: claude-3-opus-20240229
    prompt: "Be creative: {{ workflow.input.topic }}"
    output:
      result:
        type: string
    routes:
      - to: $end
""")

        config = load_workflow(str(workflow_yaml))

        provider = await create_provider(
            provider_type="claude",
            validate=False,
            temperature=config.workflow.runtime.temperature,
        )
        engine = WorkflowEngine(config, provider)

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_structured_agent({"result": "Creative response"}),
        ) as mock_build_agent:
            await engine.run({"topic": "AI"})

            # Verify API was called (provider built a Pydantic AI agent)
            assert mock_build_agent.call_count >= 1
            assert mock_build_agent.call_args.kwargs["agent"].model == "claude-3-opus-20240229"

        await provider.close()

    @pytest.mark.asyncio
    async def test_exclude_none_in_actual_workflow(self, tmp_path) -> None:
        """Verify exclude_none=True prevents Claude fields in Copilot workflows."""
        # Create workflow without Claude-specific fields
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
    prompt: "Test prompt"
    output:
      result:
        type: string
    routes:
      - to: $end
""")

        config = load_workflow(str(workflow_yaml))

        # Serialize to dict (simulates config persistence/transmission)
        config_dict = config.model_dump(mode="json", exclude_none=True)

        # Verify Claude-specific fields are not present
        runtime_dict = config_dict["workflow"]["runtime"]
        assert "temperature" not in runtime_dict
        assert "max_tokens" not in runtime_dict

    @pytest.mark.asyncio
    async def test_schema_validation_error_injection(self, tmp_path) -> None:
        """Test schema validation with invalid values."""

        # Test invalid temperature
        workflow_yaml = tmp_path / "invalid_temp.yaml"
        workflow_yaml.write_text("""
workflow:
  name: invalid
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: claude
    temperature: 2.5

agents:
  - name: agent1
    prompt: "test"
    output:
      result:
        type: string
    routes:
      - to: $end
""")

        with pytest.raises(Exception) as exc_info:
            load_workflow(str(workflow_yaml))
        assert "temperature" in str(exc_info.value).lower()

        # Test invalid max_tokens
        workflow_yaml.write_text("""
workflow:
  name: invalid
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: claude
    max_tokens: -1

agents:
  - name: agent1
    prompt: "test"
    output:
      result:
        type: string
    routes:
      - to: $end
""")

        with pytest.raises(Exception) as exc_info:
            load_workflow(str(workflow_yaml))
        error_str = str(exc_info.value).lower()
        assert "max_tokens" in error_str or "greater than" in error_str

    @pytest.mark.asyncio
    async def test_backward_compatibility_in_workflow(self, tmp_path) -> None:
        """Test that Copilot workflows still work after Claude addition."""
        from conductor.providers.copilot import CopilotProvider

        # Create pure Copilot workflow (no Claude fields)
        workflow_yaml = tmp_path / "copilot_only.yaml"
        workflow_yaml.write_text("""
workflow:
  name: copilot-only
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: copilot
  input:
    question:
      type: string
      required: true

agents:
  - name: agent1
    prompt: "Answer: {{ workflow.input.question }}"
    output:
      result:
        type: string
    routes:
      - to: $end

output:
  answer: "{{ agent1.output.result }}"
""")

        config = load_workflow(str(workflow_yaml))
        assert config.workflow.runtime.provider.name == "copilot"

        # Verify no Claude fields leaked
        assert config.workflow.runtime.temperature is None
        assert config.workflow.runtime.max_tokens is None

        # Create provider with mock handler
        def mock_handler(agent, prompt, context):
            return {"result": "Copilot response"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)
        result = await engine.run({"question": "test"})

        assert result is not None
        assert "answer" in result

        await provider.close()


@pytest.mark.performance
class TestClaudePerformanceIntegration:
    """Performance tests for Claude integration."""

    @pytest.mark.asyncio
    async def test_parameter_overhead(self, tmp_path) -> None:
        """Verify Claude parameter passing doesn't add significant overhead."""
        import time

        workflow_yaml = tmp_path / "perf_test.yaml"
        workflow_yaml.write_text("""
workflow:
  name: perf-test
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: claude
    temperature: 0.7
    max_tokens: 1000

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
            provider_type="claude",
            validate=False,
            temperature=config.workflow.runtime.temperature,
            max_tokens=config.workflow.runtime.max_tokens,
        )
        engine = WorkflowEngine(config, provider)

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_structured_agent({"result": "Test response"}),
        ) as mock_build_agent:
            # Measure execution time
            start = time.time()
            await engine.run({})
            duration = time.time() - start

            # Should complete in < 1 second (mocked, so overhead only)
            assert duration < 1.0, f"Unexpected overhead: {duration}s"

            # Verify sampling parameters reached the Pydantic AI seam
            assert mock_build_agent.call_args.kwargs["default_temperature"] == 0.7
            assert mock_build_agent.call_args.kwargs["default_max_tokens"] == 1000

        await provider.close()
