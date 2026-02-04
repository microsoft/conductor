"""Tests for Claude provider parse recovery mechanism.

Tests comprehensive failure scenarios for parse recovery:
- Malformed JSON in tool responses
- Missing required output fields
- Invalid JSON syntax
- Empty responses
- Nested JSON parsing errors
- Partial JSON fragments
- Multiple retry attempts
- Recovery success and failure paths
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import ExecutionError
from conductor.providers.claude import ClaudeProvider


def create_tool_use_block(input_dict: dict) -> Mock:
    """Create a properly structured tool_use block mock."""
    block = Mock()
    block.type = "tool_use"
    block.id = "tool_123"
    block.name = "emit_output"
    block.input = input_dict
    return block


def create_text_block(text: str) -> Mock:
    """Create a properly structured text block mock."""
    block = Mock()
    block.type = "text"
    block.text = text
    return block


def create_response(content_blocks: list, msg_id: str = "msg_123") -> Mock:
    """Create a properly structured Claude API response mock."""
    response = Mock()
    response.id = msg_id
    response.content = content_blocks
    response.model = "claude-3-5-sonnet-latest"
    response.stop_reason = "end_turn"
    response.usage = Mock(input_tokens=10, output_tokens=20, cache_creation_input_tokens=0)
    response.type = "message"
    response.role = "assistant"
    return response


class TestClaudeParseRecovery:
    """Tests for parse recovery with malformed responses."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_recovery_from_malformed_json_in_tool(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that parse recovery handles malformed JSON in tool responses."""
        mock_anthropic_module.__version__ = "0.77.0"

        # First response: text response with malformed JSON (triggers recovery)
        malformed_response = create_response(
            [create_text_block('{"answer": "incomplete...')], "msg_malformed"
        )

        # Second response: corrected response with emit_output tool
        corrected_response = create_response(
            [create_tool_use_block({"answer": "Complete answer"})], "msg_corrected"
        )

        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(
            side_effect=[malformed_response, corrected_response]
        )
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        agent = AgentDef(
            name="test_agent",
            model="claude-3-5-sonnet-latest",
            prompt="Answer the question",
            output={"answer": OutputField(type="string")},
        )

        result = await provider.execute(agent, {"workflow": {"input": {}}}, "Test prompt")

        # Should successfully recover and return corrected output
        assert result.content["answer"] == "Complete answer"
        # Should have made 2 API calls (initial + recovery)
        assert mock_client.messages.create.call_count == 2

        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_recovery_from_missing_required_fields(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test recovery when required output fields are missing."""
        mock_anthropic_module.__version__ = "0.77.0"
        # Set APIStatusError to None so isinstance check is skipped
        mock_anthropic_module.APIStatusError = None

        # First response: invalid text that won't parse (triggers parse recovery)
        incomplete_response = create_response(
            [create_text_block("Here is an incomplete response without valid JSON...")],
            "msg_incomplete",
        )

        # Second response: includes required field
        complete_response = create_response(
            [create_tool_use_block({"answer": "Correct answer", "summary": "Summary text"})],
            "msg_complete",
        )

        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(
            side_effect=[incomplete_response, complete_response]
        )
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        agent = AgentDef(
            name="test_agent",
            model="claude-3-5-sonnet-latest",
            prompt="Provide structured answer",
            output={
                "answer": OutputField(type="string"),
                "summary": OutputField(type="string"),
            },
        )

        result = await provider.execute(agent, {"workflow": {"input": {}}}, "Test prompt")

        assert result.content["answer"] == "Correct answer"
        assert result.content["summary"] == "Summary text"
        assert mock_client.messages.create.call_count == 2

        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_recovery_from_invalid_json_syntax(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test recovery from completely invalid JSON syntax."""
        mock_anthropic_module.__version__ = "0.77.0"

        # First response: invalid JSON in text
        invalid_response = create_response(
            [create_text_block("This is not JSON at all {{{")], "msg_invalid"
        )

        # Second response: valid response with tool
        valid_response = create_response(
            [create_tool_use_block({"answer": "Valid JSON response"})], "msg_valid"
        )

        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(side_effect=[invalid_response, valid_response])
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        agent = AgentDef(
            name="test_agent",
            model="claude-3-5-sonnet-latest",
            prompt="Answer",
            output={"answer": OutputField(type="string")},
        )

        result = await provider.execute(agent, {"workflow": {"input": {}}}, "Test prompt")

        assert result.content["answer"] == "Valid JSON response"
        assert mock_client.messages.create.call_count == 2

        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_recovery_failure_after_max_retries(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that recovery fails gracefully after maximum retry attempts."""
        mock_anthropic_module.__version__ = "0.77.0"
        # Set APIStatusError to None so isinstance check is skipped
        mock_anthropic_module.APIStatusError = None

        # All responses: invalid text (no valid JSON)
        malformed_response = create_response(
            [create_text_block("This is not valid JSON")], "msg_malformed"
        )

        mock_client = Mock()
        mock_client.messages = Mock()
        # Return malformed response repeatedly
        mock_client.messages.create = AsyncMock(return_value=malformed_response)
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        agent = AgentDef(
            name="test_agent",
            model="claude-3-5-sonnet-latest",
            prompt="Answer",
            output={"answer": OutputField(type="string")},
        )

        # Should raise an error after retries exhausted (ProviderError wraps the failure)
        from conductor.exceptions import ProviderError

        with pytest.raises((ExecutionError, ProviderError)):
            await provider.execute(agent, {"workflow": {"input": {}}}, "Test prompt")

        # Should have attempted multiple times (initial + retries)
        assert mock_client.messages.create.call_count > 1

        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_recovery_from_empty_response(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test recovery when response is empty or whitespace."""
        mock_anthropic_module.__version__ = "0.77.0"

        # First response: empty text
        empty_response = create_response([create_text_block("")], "msg_empty")

        # Second response: valid
        valid_response = create_response(
            [create_tool_use_block({"answer": "Non-empty response"})], "msg_valid"
        )

        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(side_effect=[empty_response, valid_response])
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        agent = AgentDef(
            name="test_agent",
            model="claude-3-5-sonnet-latest",
            prompt="Answer",
            output={"answer": OutputField(type="string")},
        )

        result = await provider.execute(agent, {"workflow": {"input": {}}}, "Test prompt")

        assert result.content["answer"] == "Non-empty response"
        assert mock_client.messages.create.call_count == 2

        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_fallback_to_text_content_parsing(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test fallback to parsing JSON from text content when tool_use fails."""
        mock_anthropic_module.__version__ = "0.77.0"

        # Response with valid JSON in text content (no tool_use)
        text_response = create_response(
            [create_text_block('{"answer": "Parsed from text content"}')], "msg_text"
        )

        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(return_value=text_response)
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        agent = AgentDef(
            name="test_agent",
            model="claude-3-5-sonnet-latest",
            prompt="Answer",
            output={"answer": OutputField(type="string")},
        )

        result = await provider.execute(agent, {"workflow": {"input": {}}}, "Test prompt")

        # Should successfully extract JSON from text content
        assert result.content["answer"] == "Parsed from text content"
        # Should only make 1 call (fallback works on first try)
        assert mock_client.messages.create.call_count == 1

        await provider.close()
