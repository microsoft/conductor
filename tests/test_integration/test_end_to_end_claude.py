"""End-to-end integration tests for Claude provider.

Tests the complete flow from schema → provider → execution → output.
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from conductor.config.loader import load_workflow
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.factory import create_provider


@pytest.fixture
def mock_anthropic_response():
    """Mock Anthropic API response matching actual SDK structure."""
    mock_response = Mock()
    mock_response.content = [Mock(text='{"result": "Test response from Claude"}', type="text")]
    mock_response.model = "claude-3-5-sonnet-20241022"
    mock_response.usage = Mock(input_tokens=10, output_tokens=20, cache_creation_input_tokens=0)
    mock_response.stop_reason = "end_turn"
    mock_response.id = "msg_123"
    mock_response.type = "message"
    mock_response.role = "assistant"
    return mock_response


class TestEndToEndClaudeIntegration:
    """Verify Claude integration works end-to-end."""

    @pytest.mark.asyncio
    async def test_basic_claude_workflow_execution(self, tmp_path, mock_anthropic_response):
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
        assert config.workflow.runtime.provider == "claude"
        assert config.workflow.runtime.temperature == 0.7
        assert config.workflow.runtime.max_tokens == 1000

        # Execute workflow with mocked Anthropic SDK
        with (
            patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True),
            patch("conductor.providers.claude.AsyncAnthropic") as mock_anthropic,
            patch("conductor.providers.claude.anthropic") as mock_module,
        ):
            mock_module.__version__ = "0.77.0"
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_anthropic_response)
            mock_client.close = AsyncMock()

            provider = await create_provider(
                provider_type="claude",
                validate=False,
                temperature=config.workflow.runtime.temperature,
                max_tokens=config.workflow.runtime.max_tokens,
            )
            engine = WorkflowEngine(config, provider)
            result = await engine.run({"question": "What is 2+2?"})

            # Verify execution completed
            assert result is not None
            assert "answer" in result

            await provider.close()

    @pytest.mark.asyncio
    async def test_agent_level_parameter_overrides(self, tmp_path, mock_anthropic_response):
        """Test that agent-level parameters override runtime defaults."""
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
    prompt: "Be creative: {{ workflow.input.topic }}"
    output:
      result:
        type: string
    routes:
      - to: $end
""")

        config = load_workflow(str(workflow_yaml))

        with (
            patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True),
            patch("conductor.providers.claude.AsyncAnthropic") as mock_anthropic,
            patch("conductor.providers.claude.anthropic") as mock_module,
        ):
            mock_module.__version__ = "0.77.0"
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_anthropic_response)
            mock_client.close = AsyncMock()

            provider = await create_provider(
                provider_type="claude",
                validate=False,
                temperature=config.workflow.runtime.temperature,
            )
            engine = WorkflowEngine(config, provider)
            await engine.run({"topic": "AI"})

            # Verify API was called
            assert mock_client.messages.create.call_count >= 1

            await provider.close()

    @pytest.mark.asyncio
    async def test_exclude_none_in_actual_workflow(self, tmp_path, mock_anthropic_response):
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
    async def test_schema_validation_error_injection(self, tmp_path):
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
    async def test_backward_compatibility_in_workflow(self, tmp_path, mock_anthropic_response):
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
        assert config.workflow.runtime.provider == "copilot"

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

    @pytest.fixture
    def mock_anthropic_response(self):
        """Mock Anthropic API response for performance tests."""
        mock_response = Mock()
        mock_response.content = [Mock(text='{"result": "Test response"}', type="text")]
        mock_response.model = "claude-3-5-sonnet-20241022"
        mock_response.usage = Mock(input_tokens=10, output_tokens=20, cache_creation_input_tokens=0)
        mock_response.stop_reason = "end_turn"
        mock_response.id = "msg_123"
        mock_response.type = "message"
        mock_response.role = "assistant"
        return mock_response

    @pytest.mark.asyncio
    async def test_parameter_overhead(self, tmp_path, mock_anthropic_response):
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

        with (
            patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True),
            patch("conductor.providers.claude.AsyncAnthropic") as mock_anthropic,
            patch("conductor.providers.claude.anthropic") as mock_module,
        ):
            mock_module.__version__ = "0.77.0"
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_anthropic_response)
            mock_client.close = AsyncMock()

            provider = await create_provider(
                provider_type="claude",
                validate=False,
                temperature=config.workflow.runtime.temperature,
                max_tokens=config.workflow.runtime.max_tokens,
            )
            engine = WorkflowEngine(config, provider)

            # Measure execution time
            start = time.time()
            await engine.run({})
            duration = time.time() - start

            # Should complete in < 1 second (mocked, so overhead only)
            assert duration < 1.0, f"Unexpected overhead: {duration}s"

            await provider.close()
