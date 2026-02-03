"""Tests for Claude provider error conditions and edge cases.

Note: Tests for top_p, top_k, stop_sequences, and metadata have been removed
as these were Claude-specific parameters not supported by both providers.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.providers.claude import ClaudeProvider


class TestClaudeErrorConditions:
    """Test error handling for various failure scenarios."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_malformed_output_triggers_parse_recovery(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that malformed JSON triggers parse recovery."""
        mock_anthropic_module.__version__ = "0.77.0"

        # First response: malformed (text without proper tool use)
        mock_bad_block = Mock()
        mock_bad_block.type = "text"
        mock_bad_block.text = "This is not JSON"

        mock_bad_response = Mock()
        mock_bad_response.content = [mock_bad_block]
        mock_bad_response.usage = Mock(input_tokens=10, output_tokens=5)

        # Second response: good (after recovery prompt)
        mock_good_block = Mock()
        mock_good_block.type = "tool_use"
        mock_good_block.name = "emit_output"
        mock_good_block.input = {"answer": "recovered"}

        mock_good_response = Mock()
        mock_good_response.content = [mock_good_block]
        mock_good_response.usage = Mock(input_tokens=15, output_tokens=10)

        mock_client = Mock()
        mock_client.messages.create = AsyncMock(side_effect=[mock_bad_response, mock_good_response])
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        agent = AgentDef(
            name="test",
            prompt="Test prompt",
            output={"answer": OutputField(type="string")},
        )

        result = await provider.execute(agent, {}, "Test")

        # Should succeed after recovery
        assert result.content["answer"] == "recovered"
        # Should have made 2 API calls (initial + recovery)
        assert mock_client.messages.create.call_count == 2

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_rate_limit_error_retryable(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that rate limit errors are retryable."""
        mock_anthropic_module.__version__ = "0.77.0"

        # Create mock RateLimitError class and instance
        MockRateLimitError = type("RateLimitError", (Exception,), {})
        rate_limit_error = MockRateLimitError("Rate limit exceeded")

        # Mock successful response
        mock_content_block = Mock()
        mock_content_block.type = "tool_use"
        mock_content_block.name = "emit_output"
        mock_content_block.input = {"answer": "success"}

        mock_response = Mock()
        mock_response.content = [mock_content_block]
        mock_response.usage = Mock(input_tokens=10, output_tokens=5)

        mock_client = Mock()
        # First call fails with rate limit, second succeeds
        mock_client.messages.create = AsyncMock(side_effect=[rate_limit_error, mock_response])
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        # Add RateLimitError to anthropic module
        mock_anthropic_module.RateLimitError = MockRateLimitError

        provider = ClaudeProvider()

        agent = AgentDef(
            name="test",
            prompt="Test prompt",
            output={"answer": OutputField(type="string")},
        )

        result = await provider.execute(agent, {}, "Test")

        # Should succeed after retry
        assert result.content["answer"] == "success"
        # Should have made 2 calls (failed + retry)
        assert mock_client.messages.create.call_count == 2


class TestClaudeEdgeCases:
    """Test edge cases and boundary conditions."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_empty_prompt(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test handling of empty prompt."""
        mock_anthropic_module.__version__ = "0.77.0"

        mock_content_block = Mock()
        mock_content_block.type = "tool_use"
        mock_content_block.name = "emit_output"
        mock_content_block.input = {"answer": "default"}

        mock_response = Mock()
        mock_response.content = [mock_content_block]
        mock_response.usage = Mock(input_tokens=5, output_tokens=5)

        mock_client = Mock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        agent = AgentDef(
            name="test",
            prompt="",  # Empty prompt
            output={"answer": OutputField(type="string")},
        )

        result = await provider.execute(agent, {}, "")

        assert result.content["answer"] == "default"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_special_characters_in_prompt(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test handling of special characters in prompts."""
        mock_anthropic_module.__version__ = "0.77.0"

        mock_content_block = Mock()
        mock_content_block.type = "tool_use"
        mock_content_block.name = "emit_output"
        mock_content_block.input = {"answer": "processed"}

        mock_response = Mock()
        mock_response.content = [mock_content_block]
        mock_response.usage = Mock(input_tokens=10, output_tokens=5)

        mock_client = Mock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Test with special characters including potential injection attempts
        special_prompt = "Test with special chars: \n\t\"'<>&{}[]\\u0000"

        agent = AgentDef(
            name="test",
            prompt=special_prompt,
            output={"answer": OutputField(type="string")},
        )

        result = await provider.execute(agent, {}, special_prompt)

        assert result.content["answer"] == "processed"

        # Verify prompt was passed correctly
        call_kwargs = mock_client.messages.create.call_args.kwargs
        messages = call_kwargs["messages"]
        assert len(messages) > 0
        # Check the prompt content starts with our special characters
        # The provider appends tool use instructions, so we check the beginning
        assert messages[0]["content"].startswith(special_prompt)

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_null_context(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test handling of null/empty context."""
        mock_anthropic_module.__version__ = "0.77.0"

        mock_content_block = Mock()
        mock_content_block.type = "tool_use"
        mock_content_block.name = "emit_output"
        mock_content_block.input = {"answer": "works"}

        mock_response = Mock()
        mock_response.content = [mock_content_block]
        mock_response.usage = Mock(input_tokens=10, output_tokens=5)

        mock_client = Mock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        agent = AgentDef(
            name="test",
            prompt="Test",
            output={"answer": OutputField(type="string")},
        )

        # Test with empty context
        result = await provider.execute(agent, {}, "Test")

        assert result.content["answer"] == "works"
