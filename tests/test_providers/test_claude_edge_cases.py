"""Edge case tests for ClaudeProvider.

Tests cover:
- Temperature validation edge cases
- Empty/unusual responses
- Retry history exposure

Note: Tests for stop_sequences, metadata, top_p, and top_k have been removed
as these were Claude-specific parameters not supported by both providers.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from conductor.config.schema import AgentDef
from conductor.exceptions import ValidationError
from conductor.providers.claude import ClaudeProvider


class TestClaudeEdgeCases:
    """Tests for edge cases in Claude provider."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_temperature_validation_edge_cases(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test temperature validation at boundaries."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        # Valid boundaries
        provider = ClaudeProvider(temperature=0.0)
        assert provider._default_temperature == 0.0

        provider = ClaudeProvider(temperature=1.0)
        assert provider._default_temperature == 1.0

        # Invalid - below range
        with pytest.raises(ValidationError, match="Temperature must be between 0.0 and 1.0"):
            ClaudeProvider(temperature=-0.1)

        # Invalid - above range
        with pytest.raises(ValidationError, match="Temperature must be between 0.0 and 1.0"):
            ClaudeProvider(temperature=1.1)

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_empty_response_handling(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test handling of empty response content."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock response with empty content
        mock_response = Mock()
        mock_response.content = []
        mock_response.usage = Mock(input_tokens=10, output_tokens=0)
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Agent without output schema - should handle empty content
        agent = AgentDef(name="test_agent", prompt="Test prompt")
        context = {}
        rendered_prompt = "Test prompt"

        result = await provider.execute(agent, context, rendered_prompt)
        assert result.content == {"text": ""}

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_retry_history_exposure(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that retry history can be accessed for debugging."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Initially empty
        history = provider.get_retry_history()
        assert history == []
        assert isinstance(history, list)

        # Ensure it returns a copy (not the internal list)
        history.append({"test": "data"})
        assert provider.get_retry_history() == []
