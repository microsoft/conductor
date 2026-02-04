"""Integration tests verifying schema fields are correctly passed to providers.

This module tests that all Claude-specific schema fields (temperature, max_tokens)
are correctly passed from the schema to the ClaudeProvider constructor and used
during execution.

These tests use real provider classes (not mocks) to verify actual integration.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from conductor.config.schema import (
    AgentDef,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.claude import ClaudeProvider
from conductor.providers.factory import create_provider


class TestSchemaToProviderIntegration:
    """Test that schema fields correctly integrate with provider implementations."""

    @pytest.mark.asyncio
    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    async def test_claude_runtime_config_fields_passed_to_provider(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ):
        """Test that all Claude runtime config fields are passed to ClaudeProvider.

        Verifies: temperature, max_tokens
        """
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock the messages.create method
        mock_message = Mock()
        mock_message.id = "msg_123"
        mock_message.type = "message"
        mock_message.role = "assistant"
        mock_message.model = "claude-3-5-sonnet-latest"
        mock_message.stop_reason = "end_turn"
        mock_message.usage = Mock(input_tokens=10, output_tokens=20, cache_creation_input_tokens=0)
        mock_message.content = [Mock(type="text", text='{"answer": "test"}')]

        mock_client.messages.create = AsyncMock(return_value=mock_message)
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client

        # Create workflow config with all Claude fields
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-claude-fields",
                description="Test Claude fields",
                version="1.0.0",
                entry_point="agent1",
                runtime=RuntimeConfig(
                    provider="claude",
                    temperature=0.8,
                    max_tokens=2048,
                ),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    description="Test agent",
                    model="claude-3-5-sonnet-latest",
                    prompt="Test prompt",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                )
            ],
        )

        # Create provider using factory (real instantiation)
        provider = await create_provider(
            provider_type="claude",
            validate=False,
            default_model="claude-3-5-sonnet-latest",
            temperature=config.workflow.runtime.temperature,
            max_tokens=config.workflow.runtime.max_tokens,
        )

        # Verify provider is ClaudeProvider
        assert isinstance(provider, ClaudeProvider)

        # Execute through engine to verify fields are used
        engine = WorkflowEngine(config, provider)
        await engine.run({})

        # Verify messages.create was called with correct parameters
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["temperature"] == 0.8
        assert call_kwargs["max_tokens"] == 2048

        await provider.close()

    @pytest.mark.asyncio
    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    async def test_claude_provider_with_none_fields(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ):
        """Test that ClaudeProvider handles None values for optional fields correctly."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock the messages.create method
        mock_message = Mock()
        mock_message.id = "msg_123"
        mock_message.type = "message"
        mock_message.role = "assistant"
        mock_message.model = "claude-3-5-sonnet-latest"
        mock_message.stop_reason = "end_turn"
        mock_message.usage = Mock(input_tokens=10, output_tokens=20, cache_creation_input_tokens=0)
        mock_message.content = [Mock(type="text", text='{"result": "ok"}')]

        mock_client.messages.create = AsyncMock(return_value=mock_message)
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client

        # Create workflow config with all Claude fields set to None
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-none-fields",
                description="Test None fields",
                version="1.0.0",
                entry_point="agent1",
                runtime=RuntimeConfig(
                    provider="claude",
                    temperature=None,
                    max_tokens=None,
                ),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    description="Test agent",
                    model="claude-3-5-sonnet-latest",
                    prompt="Test prompt",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                )
            ],
        )

        # Create provider using factory
        provider = await create_provider(
            provider_type="claude",
            validate=False,
            default_model="claude-3-5-sonnet-latest",
        )

        # Verify provider is ClaudeProvider
        assert isinstance(provider, ClaudeProvider)

        # Execute workflow
        engine = WorkflowEngine(config, provider)
        await engine.run({})

        # Verify messages.create was called with defaults
        call_kwargs = mock_client.messages.create.call_args.kwargs
        # When temperature is None, it should NOT be in call_kwargs
        # (provider correctly omits None values from API calls)
        assert "temperature" not in call_kwargs
        # max_tokens uses a default of 8192 when not specified
        assert "max_tokens" in call_kwargs

        await provider.close()

    @pytest.mark.asyncio
    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    async def test_agent_level_overrides_runtime_defaults(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ):
        """Test that agent-level config overrides runtime defaults."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock the messages.create method
        mock_message = Mock()
        mock_message.id = "msg_123"
        mock_message.type = "message"
        mock_message.role = "assistant"
        mock_message.model = "claude-3-5-sonnet-latest"
        mock_message.stop_reason = "end_turn"
        mock_message.usage = Mock(input_tokens=10, output_tokens=20, cache_creation_input_tokens=0)
        mock_message.content = [Mock(type="text", text='{"result": "ok"}')]

        mock_client.messages.create = AsyncMock(return_value=mock_message)
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client

        # Create workflow with runtime defaults
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-overrides",
                description="Test agent overrides",
                version="1.0.0",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="claude", temperature=0.5, max_tokens=1024),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    description="Test agent",
                    model="claude-3-5-sonnet-latest",
                    prompt="Test prompt",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                )
            ],
        )

        # Create provider
        provider = await create_provider(
            provider_type="claude",
            validate=False,
            default_model="claude-3-5-sonnet-latest",
            temperature=config.workflow.runtime.temperature,
            max_tokens=config.workflow.runtime.max_tokens,
        )

        # Execute workflow
        engine = WorkflowEngine(config, provider)
        await engine.run({})

        # Verify API was called
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["temperature"] == 0.5
        assert call_kwargs["max_tokens"] == 1024

        await provider.close()
