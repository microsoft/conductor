"""End-to-end parameter passing tests for Claude provider.

Verifies that common parameters are properly passed through:
- temperature
- max_tokens

Note: top_p, top_k, stop_sequences, and metadata have been removed as they
were Claude-specific parameters not supported by both providers.

Tests the full chain: factory -> provider -> SDK
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from conductor.config.schema import AgentDef
from conductor.providers.factory import create_provider


class TestClaudeParameterPassing:
    """Tests for end-to-end parameter passing through factory."""

    @pytest.mark.asyncio
    @patch("conductor.providers.factory.ClaudeProvider")
    async def test_common_parameters_passed_from_factory(self, mock_claude_class: Mock) -> None:
        """Test that factory passes common parameters to provider."""
        mock_instance = Mock()
        mock_instance.validate_connection = AsyncMock(return_value=True)
        mock_claude_class.return_value = mock_instance

        # Create provider with common parameters
        await create_provider(
            provider_type="claude",
            default_model="claude-3-opus-20240229",
            temperature=0.7,
            max_tokens=4096,
            timeout=120.0,
        )

        # Verify parameters were passed to ClaudeProvider constructor
        mock_claude_class.assert_called_once_with(
            model="claude-3-opus-20240229",
            temperature=0.7,
            max_tokens=4096,
            timeout=120.0,
            mcp_servers=None,
        )

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_common_parameters_passed_to_sdk(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that provider passes common parameters to Claude SDK."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock successful response
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Test response")]
        mock_response.usage = Mock(input_tokens=10, output_tokens=20)
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        # Import after patching
        from conductor.providers.claude import ClaudeProvider

        # Create provider with common parameters
        provider = ClaudeProvider(
            api_key="test-key",
            model="claude-3-opus-20240229",
            temperature=0.7,
            max_tokens=4096,
        )

        # Execute agent
        agent = AgentDef(name="test_agent", prompt="Test prompt", model="claude-3-sonnet-20240229")
        context = {}
        rendered_prompt = "Test prompt"

        await provider.execute(agent, context, rendered_prompt)

        # Verify SDK was called with parameters
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-3-sonnet-20240229"  # Agent model overrides
        assert call_kwargs["max_tokens"] == 4096
        assert call_kwargs["temperature"] == 0.7
        assert call_kwargs["messages"] == [{"role": "user", "content": "Test prompt"}]

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_optional_parameters_not_passed_when_none(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that optional parameters are not passed to SDK when None."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock successful response
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Test response")]
        mock_response.usage = Mock(input_tokens=10, output_tokens=20)
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        # Import after patching
        from conductor.providers.claude import ClaudeProvider

        # Create provider with minimal parameters (all optional params are None)
        provider = ClaudeProvider()

        # Execute agent
        agent = AgentDef(name="test_agent", prompt="Test prompt")
        context = {}
        rendered_prompt = "Test prompt"

        await provider.execute(agent, context, rendered_prompt)

        # Verify SDK was called without optional parameters
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert "temperature" not in call_kwargs  # None, so not passed
        # Required parameters should still be present
        assert "model" in call_kwargs
        assert "max_tokens" in call_kwargs
        assert "messages" in call_kwargs

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_agent_model_overrides_provider_model(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that agent-level model overrides provider default."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock successful response
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Test response")]
        mock_response.usage = Mock(input_tokens=10, output_tokens=20)
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        # Import after patching
        from conductor.providers.claude import ClaudeProvider

        # Create provider with default model
        provider = ClaudeProvider(model="claude-3-5-sonnet-latest")

        # Execute agent with different model
        agent = AgentDef(name="test_agent", prompt="Test prompt", model="claude-3-opus-20240229")
        context = {}
        rendered_prompt = "Test prompt"

        await provider.execute(agent, context, rendered_prompt)

        # Verify agent model was used
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-3-opus-20240229"
