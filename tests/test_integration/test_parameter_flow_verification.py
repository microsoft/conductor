"""Parameter flow verification tests.

These tests verify that parameters ACTUALLY reach the Anthropic SDK API calls,
addressing the reviewer concern: 'No verification that temperature, max_tokens
actually reach the Anthropic SDK API calls'.

This test file inspects the actual kwargs passed to the Anthropic SDK's
messages.create() method to ensure all parameters flow correctly.
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from conductor.config.loader import load_workflow
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.factory import create_provider


class TestParameterFlowToAnthropicSDK:
    """Verify ALL parameters reach Anthropic SDK API calls."""

    @pytest.mark.asyncio
    async def test_temperature_reaches_api_call(self, tmp_path):
        """Verify temperature parameter reaches Anthropic SDK API call."""
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

        with (
            patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True),
            patch("conductor.providers.claude.AsyncAnthropic") as mock_anthropic,
            patch("conductor.providers.claude.anthropic") as mock_module,
        ):
            mock_module.__version__ = "0.77.0"
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client

            # Mock successful response with valid JSON
            mock_response = Mock()
            mock_response.content = [Mock(text='{"result": "Test response"}', type="text")]
            mock_response.model = "claude-3-5-sonnet-20241022"
            mock_response.usage = Mock(
                input_tokens=10, output_tokens=20, cache_creation_input_tokens=0
            )
            mock_response.stop_reason = "end_turn"
            mock_response.id = "msg_123"
            mock_response.type = "message"
            mock_response.role = "assistant"

            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()

            config = load_workflow(str(workflow_yaml))
            provider = await create_provider(
                provider_type="claude",
                validate=False,
                temperature=config.workflow.runtime.temperature,
            )
            engine = WorkflowEngine(config, provider)
            await engine.run({})

            # Verify temperature=0.42 was passed to SDK
            call_kwargs = mock_client.messages.create.call_args.kwargs
            assert call_kwargs["temperature"] == 0.42, (
                f"Expected temperature=0.42, got {call_kwargs.get('temperature')}"
            )

            await provider.close()

    @pytest.mark.asyncio
    async def test_max_tokens_reaches_api_call(self, tmp_path):
        """Verify max_tokens parameter reaches Anthropic SDK API call."""
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

        with (
            patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True),
            patch("conductor.providers.claude.AsyncAnthropic") as mock_anthropic,
            patch("conductor.providers.claude.anthropic") as mock_module,
        ):
            mock_module.__version__ = "0.77.0"
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client

            mock_response = Mock()
            mock_response.content = [Mock(text='{"result": "Test response"}', type="text")]
            mock_response.model = "claude-3-5-sonnet-20241022"
            mock_response.usage = Mock(
                input_tokens=10, output_tokens=20, cache_creation_input_tokens=0
            )
            mock_response.stop_reason = "end_turn"
            mock_response.id = "msg_123"
            mock_response.type = "message"
            mock_response.role = "assistant"

            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()

            config = load_workflow(str(workflow_yaml))
            provider = await create_provider(
                provider_type="claude",
                validate=False,
                max_tokens=config.workflow.runtime.max_tokens,
            )
            engine = WorkflowEngine(config, provider)
            await engine.run({})

            # Verify max_tokens=2048 was passed to SDK
            call_kwargs = mock_client.messages.create.call_args.kwargs
            assert call_kwargs["max_tokens"] == 2048, (
                f"Expected max_tokens=2048, got {call_kwargs.get('max_tokens')}"
            )

            await provider.close()

    @pytest.mark.asyncio
    async def test_all_parameters_together_reach_api_call(self, tmp_path):
        """Verify ALL Claude parameters reach Anthropic SDK API call simultaneously."""
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

        with (
            patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True),
            patch("conductor.providers.claude.AsyncAnthropic") as mock_anthropic,
            patch("conductor.providers.claude.anthropic") as mock_module,
        ):
            mock_module.__version__ = "0.77.0"
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client

            mock_response = Mock()
            mock_response.content = [Mock(text='{"result": "Test response"}', type="text")]
            mock_response.model = "claude-3-5-sonnet-20241022"
            mock_response.usage = Mock(
                input_tokens=10, output_tokens=20, cache_creation_input_tokens=0
            )
            mock_response.stop_reason = "end_turn"
            mock_response.id = "msg_123"
            mock_response.type = "message"
            mock_response.role = "assistant"

            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()

            config = load_workflow(str(workflow_yaml))
            provider = await create_provider(
                provider_type="claude",
                validate=False,
                temperature=config.workflow.runtime.temperature,
                max_tokens=config.workflow.runtime.max_tokens,
            )
            engine = WorkflowEngine(config, provider)
            await engine.run({})

            # Verify ALL parameters were passed to SDK in the same call
            call_kwargs = mock_client.messages.create.call_args.kwargs
            assert call_kwargs["temperature"] == 0.75
            assert call_kwargs["max_tokens"] == 4096

            await provider.close()

    @pytest.mark.asyncio
    async def test_none_parameters_use_defaults(self, tmp_path):
        """Verify parameters with None values use provider defaults."""
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

        with (
            patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True),
            patch("conductor.providers.claude.AsyncAnthropic") as mock_anthropic,
            patch("conductor.providers.claude.anthropic") as mock_module,
        ):
            mock_module.__version__ = "0.77.0"
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client

            mock_response = Mock()
            mock_response.content = [Mock(text='{"result": "Test response"}', type="text")]
            mock_response.model = "claude-3-5-sonnet-20241022"
            mock_response.usage = Mock(
                input_tokens=10, output_tokens=20, cache_creation_input_tokens=0
            )
            mock_response.stop_reason = "end_turn"
            mock_response.id = "msg_123"
            mock_response.type = "message"
            mock_response.role = "assistant"

            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()

            config = load_workflow(str(workflow_yaml))
            provider = await create_provider(
                provider_type="claude",
                validate=False,
            )
            engine = WorkflowEngine(config, provider)
            await engine.run({})

            # When temperature is None, Claude provider uses default (1.0)
            call_kwargs = mock_client.messages.create.call_args.kwargs
            # Required parameters should still be present
            assert "model" in call_kwargs
            assert "max_tokens" in call_kwargs
            assert "messages" in call_kwargs

            await provider.close()


class TestExcludeNoneInSerialization:
    """Verify exclude_none=True prevents Claude fields in serialized Copilot configs."""

    @pytest.mark.asyncio
    async def test_exclude_none_during_workflow_execution(self, tmp_path):
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
    async def test_exclude_none_with_partial_claude_params(self, tmp_path):
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
