"""Tests for workflows attempting to mix Copilot and Claude providers.

Verifies that mixing providers is properly documented as unsupported
and that the behavior is clear.

UPDATE: Multi-provider support has been added. Agents can now specify
a `provider` field to override the workflow default.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from conductor.config.loader import load_workflow
from conductor.config.schema import AgentDef, RuntimeConfig
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.base import AgentOutput, AgentProvider
from conductor.providers.registry import ProviderRegistry


class TestMixedProviderWorkflows:
    """Test workflows that attempt to use both providers."""

    def test_workflow_has_single_provider(self, tmp_path):
        """Verify workflow schema enforces single provider."""
        # Valid: Single provider
        workflow_yaml = tmp_path / "single_provider.yaml"
        workflow_yaml.write_text("""
workflow:
  name: single-provider
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
        assert config.workflow.runtime.provider == "claude"

    def test_can_override_provider_per_agent(self, tmp_path):
        """Verify agent-level provider override is now supported."""
        workflow_yaml = tmp_path / "workflow.yaml"
        workflow_yaml.write_text("""
workflow:
  name: test
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: copilot

agents:
  - name: agent1
    provider: claude
    prompt: "test"
    output:
      result:
        type: string
    routes:
      - to: $end
""")

        config = load_workflow(str(workflow_yaml))
        # Workflow default is copilot
        assert config.workflow.runtime.provider == "copilot"
        # Agent overrides to claude
        assert config.agents[0].provider == "claude"
        # Agent schema now has 'provider' field
        assert "provider" in AgentDef.model_fields

    def test_claude_fields_ignored_by_copilot_provider(self, tmp_path):
        """Verify Claude fields don't break Copilot provider."""
        # Create runtime config with provider=copilot
        runtime = RuntimeConfig(provider="copilot")

        # Verify Claude fields are None
        assert runtime.temperature is None
        assert runtime.max_tokens is None

        # Serialization excludes None values
        dumped = runtime.model_dump(exclude_none=True)
        assert dumped == {"provider": "copilot", "mcp_servers": {}}

    def test_provider_parameter_isolation(self, tmp_path):
        """Test that provider-specific parameters don't interfere.

        Addresses reviewer concern: No validation that mixing providers
        properly isolates parameters.

        Currently, workflows use a single provider, but this test documents
        the expected isolation behavior for parameter namespacing.
        """
        # Claude-specific parameters should only apply to Claude provider
        workflow_yaml = tmp_path / "claude_params.yaml"
        workflow_yaml.write_text("""
workflow:
  name: claude-with-params
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: claude
    temperature: 0.5
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

        # Verify Claude parameters are loaded
        assert config.workflow.runtime.temperature == 0.5
        assert config.workflow.runtime.max_tokens == 1000

        # If provider is later changed to Copilot, these parameters would be ignored
        # (Copilot doesn't support all the same parameters)
        # This is handled by factory.py using conditional parameter passing

    def test_parameter_exclusion_prevents_pollution(self, tmp_path):
        """Test that None parameters don't pollute provider instantiation.

        Ensures backward compatibility: Copilot workflows don't get
        claude_api_key=None, and Claude workflows don't get copilot_token=None.
        """
        # Copilot workflow shouldn't have Claude parameters
        copilot_yaml = tmp_path / "copilot.yaml"
        copilot_yaml.write_text("""
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

        config = load_workflow(str(copilot_yaml))

        # Schema should have Claude fields as None
        assert config.workflow.runtime.temperature is None
        assert config.workflow.runtime.max_tokens is None

        # But serialization with exclude_none=True won't include them
        serialized = config.model_dump(exclude_none=True)
        assert "temperature" not in serialized["workflow"]["runtime"]
        assert "max_tokens" not in serialized["workflow"]["runtime"]

        # This prevents provider factory from receiving irrelevant parameters


class MockProvider(AgentProvider):
    """Mock provider for testing."""

    def __init__(self, provider_type: str = "mock") -> None:
        self.provider_type = provider_type
        self.executed_agents: list[str] = []
        self.closed = False

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
    ) -> AgentOutput:
        self.executed_agents.append(agent.name)
        return AgentOutput(
            content={"result": f"executed by {self.provider_type}"},
            raw_response="mock",
        )

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


class TestMultiProviderExecution:
    """Tests for actual multi-provider workflow execution."""

    @patch("conductor.providers.registry.create_provider")
    @pytest.mark.asyncio
    async def test_workflow_with_mixed_providers_executes(
        self, mock_create: MagicMock, tmp_path
    ) -> None:
        """Test that workflow with different providers per agent executes correctly."""
        copilot_provider = MockProvider("copilot")
        claude_provider = MockProvider("claude")

        async def create_side_effect(**kwargs: Any) -> MockProvider:
            if kwargs.get("provider_type") == "copilot":
                return copilot_provider
            return claude_provider

        mock_create.side_effect = create_side_effect

        workflow_yaml = tmp_path / "multi_provider.yaml"
        workflow_yaml.write_text("""
workflow:
  name: multi-provider-test
  version: "1.0"
  entry_point: claude_agent
  runtime:
    provider: copilot

agents:
  - name: claude_agent
    provider: claude
    prompt: "Research topic"
    output:
      result:
        type: string
    routes:
      - to: copilot_agent

  - name: copilot_agent
    prompt: "Use copilot: {{ claude_agent.output.result }}"
    output:
      result:
        type: string
    routes:
      - to: $end

output:
  final: "{{ copilot_agent.output.result }}"
""")

        config = load_workflow(str(workflow_yaml))

        async with ProviderRegistry(config) as registry:
            engine = WorkflowEngine(config, registry=registry)
            result = await engine.run({})

        # Both providers should have been used
        assert "claude_agent" in claude_provider.executed_agents
        assert "copilot_agent" in copilot_provider.executed_agents

        # Verify output
        assert result == {"final": "executed by copilot"}

    @patch("conductor.providers.registry.create_provider")
    @pytest.mark.asyncio
    async def test_parallel_agents_with_different_providers(
        self, mock_create: MagicMock, tmp_path
    ) -> None:
        """Test parallel agents using different providers execute correctly."""
        copilot_provider = MockProvider("copilot")
        claude_provider = MockProvider("claude")

        async def create_side_effect(**kwargs: Any) -> MockProvider:
            if kwargs.get("provider_type") == "copilot":
                return copilot_provider
            return claude_provider

        mock_create.side_effect = create_side_effect

        workflow_yaml = tmp_path / "parallel_multi_provider.yaml"
        workflow_yaml.write_text("""
workflow:
  name: parallel-multi-provider
  version: "1.0"
  entry_point: parallel_group
  runtime:
    provider: copilot

agents:
  - name: claude_analyzer
    provider: claude
    prompt: "Analyze with Claude"
    output:
      result:
        type: string

  - name: copilot_analyzer
    prompt: "Analyze with Copilot"
    output:
      result:
        type: string

parallel:
  - name: parallel_group
    agents:
      - claude_analyzer
      - copilot_analyzer
    routes:
      - to: $end

output:
  claude_result: "{{ parallel_group.outputs.claude_analyzer.result }}"
  copilot_result: "{{ parallel_group.outputs.copilot_analyzer.result }}"
""")

        config = load_workflow(str(workflow_yaml))

        async with ProviderRegistry(config) as registry:
            engine = WorkflowEngine(config, registry=registry)
            result = await engine.run({})

        # Both providers should have executed their respective agents
        assert "claude_analyzer" in claude_provider.executed_agents
        assert "copilot_analyzer" in copilot_provider.executed_agents

        # Verify outputs
        assert result["claude_result"] == "executed by claude"
        assert result["copilot_result"] == "executed by copilot"

    @patch("conductor.providers.registry.create_provider")
    @pytest.mark.asyncio
    async def test_provider_only_created_when_needed(
        self, mock_create: MagicMock, tmp_path
    ) -> None:
        """Test that providers are only created for agents that need them."""
        copilot_provider = MockProvider("copilot")

        async def create_side_effect(**kwargs: Any) -> MockProvider:
            if kwargs.get("provider_type") == "copilot":
                return copilot_provider
            raise RuntimeError("Claude provider should not be created")

        mock_create.side_effect = create_side_effect

        workflow_yaml = tmp_path / "single_provider_used.yaml"
        workflow_yaml.write_text("""
workflow:
  name: single-provider-test
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: copilot

agents:
  - name: agent1
    prompt: "Use default copilot"
    output:
      result:
        type: string
    routes:
      - to: agent2

  - name: agent2
    prompt: "Also uses default copilot"
    output:
      result:
        type: string
    routes:
      - to: $end

output:
  result: "{{ agent2.output.result }}"
""")

        config = load_workflow(str(workflow_yaml))

        async with ProviderRegistry(config) as registry:
            engine = WorkflowEngine(config, registry=registry)
            await engine.run({})

        # Only copilot should have been created (no error from Claude creation)
        assert mock_create.call_count == 1
        mock_create.assert_called_once()
        assert mock_create.call_args[1]["provider_type"] == "copilot"


class TestMultiProviderSchemaValidation:
    """Tests for multi-provider YAML schema validation."""

    def test_valid_provider_values(self, tmp_path) -> None:
        """Test that valid provider values are accepted."""
        for provider in ["copilot", "claude"]:
            workflow_yaml = tmp_path / f"{provider}_agent.yaml"
            workflow_yaml.write_text(f"""
workflow:
  name: test
  version: "1.0"
  entry_point: agent1
  runtime:
    provider: copilot

agents:
  - name: agent1
    provider: {provider}
    prompt: "test"
    output:
      result:
        type: string
    routes:
      - to: $end
""")
            config = load_workflow(str(workflow_yaml))
            assert config.agents[0].provider == provider

    def test_null_provider_uses_default(self, tmp_path) -> None:
        """Test that null/missing provider uses workflow default."""
        workflow_yaml = tmp_path / "default_provider.yaml"
        workflow_yaml.write_text("""
workflow:
  name: test
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
        assert config.agents[0].provider is None
        assert config.workflow.runtime.provider == "claude"
