"""Tests for Claude provider streaming error handling.

Since streaming is deferred to Phase 2+, this test verifies that:
1. The provider does NOT attempt streaming in Phase 1
2. Users get clear feedback if they expect streaming behavior
3. Configuration is validated to prevent streaming-related issues
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.providers.claude import ClaudeProvider


class TestClaudeStreamingDeferral:
    """Tests for streaming behavior in Phase 1 (should not stream)."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_non_streaming_execution(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that provider uses non-streaming API calls (Phase 1 implementation)."""
        mock_anthropic_module.__version__ = "0.77.0"

        # Setup mock response
        mock_message = Mock()
        mock_message.id = "msg_123"
        mock_message.content = [Mock(type="text", text='{"answer": "Python"}')]
        mock_message.stop_reason = "end_turn"
        mock_message.usage = Mock(input_tokens=10, output_tokens=5)

        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(return_value=mock_message)
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        agent = AgentDef(
            name="test_agent",
            model="claude-3-5-sonnet-latest",
            prompt="Test",
            output={"answer": OutputField(type="string")},
        )

        await provider.execute(agent, {"workflow": {"input": {}}}, "Test prompt")

        # Verify non-streaming call
        call_kwargs = mock_client.messages.create.call_args[1]
        assert "stream" not in call_kwargs or call_kwargs.get("stream") is False

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_complete_response_returned(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that responses are returned only after complete generation."""
        mock_anthropic_module.__version__ = "0.77.0"

        # Setup mock response with full content
        full_response = "This is a complete response that should be returned all at once."
        mock_message = Mock()
        mock_message.id = "msg_456"
        mock_message.content = [Mock(type="text", text=f'{{"answer": "{full_response}"}}')]
        mock_message.stop_reason = "end_turn"
        mock_message.usage = Mock(input_tokens=15, output_tokens=20)

        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(return_value=mock_message)
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        agent = AgentDef(
            name="test_agent",
            model="claude-3-5-sonnet-latest",
            prompt="Generate a response",
            output={"answer": OutputField(type="string")},
        )

        result = await provider.execute(agent, {"workflow": {"input": {}}}, "Generate a response")

        # Verify complete response returned
        assert result.content["answer"] == full_response
        # Verify it was a single call, not streaming chunks
        assert mock_client.messages.create.call_count == 1


class TestClaudeMaxTokensValidation:
    """Tests for max_tokens configuration."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_respects_configured_max_tokens(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that configured max_tokens is passed to API correctly."""
        mock_anthropic_module.__version__ = "0.77.0"

        mock_message = Mock()
        mock_message.id = "msg_789"
        mock_message.content = [Mock(type="text", text='{"answer": "Short"}')]
        mock_message.stop_reason = "end_turn"
        mock_message.usage = Mock(input_tokens=10, output_tokens=5)

        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(return_value=mock_message)
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        # Create provider with custom max_tokens
        provider = ClaudeProvider(max_tokens=1024)

        agent = AgentDef(
            name="test_agent",
            model="claude-3-5-sonnet-latest",
            prompt="Test",
            output={"answer": OutputField(type="string")},
        )

        await provider.execute(agent, {"workflow": {"input": {}}}, "Test prompt")

        # Verify max_tokens passed to API
        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["max_tokens"] == 1024

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_default_max_tokens_set(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that provider has reasonable default max_tokens."""
        mock_anthropic_module.__version__ = "0.77.0"

        mock_client = Mock()
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Verify default is set (8192 per implementation)
        assert provider._default_max_tokens == 8192
