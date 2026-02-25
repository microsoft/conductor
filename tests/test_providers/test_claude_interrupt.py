"""Tests for mid-agent interrupt support in the Claude provider.

Tests cover:
- Interrupt signal checked at the start of each agentic loop iteration
- Final emit_output request sent as a user message (not system)
- Partial output from emit_output tool_use parsed correctly
- Partial output from text response parsed correctly (no schema)
- Partial output is NOT schema-validated
- Interrupt signal is cleared after handling
- No interrupt when signal is not set
- Re-invocation with guidance starts a fresh conversation
- Token accounting includes the interrupt request
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.providers.claude import ClaudeProvider


def _make_provider() -> ClaudeProvider:
    """Create a ClaudeProvider with essential attributes for testing."""
    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider._client = MagicMock()
    provider._mcp_manager = None
    provider._mcp_servers_config = None
    provider._default_model = "claude-3-5-sonnet-latest"
    provider._default_temperature = None
    provider._default_max_tokens = 8192
    provider._retry_config = MagicMock()
    provider._retry_config.max_attempts = 1
    provider._retry_config.base_delay = 1.0
    provider._retry_config.max_delay = 30.0
    provider._retry_config.jitter = 0.0
    provider._retry_history = []
    provider._max_parse_recovery_attempts = 2
    provider._max_schema_depth = 10
    return provider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_text_block(text: str) -> MagicMock:
    """Create a mock text content block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_tool_use_block(name: str, input_data: dict[str, Any], tool_id: str = "t1") -> MagicMock:
    """Create a mock tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.id = tool_id
    block.input = input_data
    return block


def _make_response(
    content_blocks: list[MagicMock],
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> MagicMock:
    """Create a mock Claude API response."""
    response = MagicMock()
    response.content = content_blocks
    response.usage = MagicMock()
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    response.usage.cache_read_input_tokens = None
    response.usage.cache_creation_input_tokens = None
    return response


def _agent_with_output() -> AgentDef:
    """Agent definition with output schema."""
    return AgentDef(
        name="test_agent",
        model="claude-3-5-sonnet-latest",
        prompt="Test prompt",
        output={"result": OutputField(type="string", description="test result")},
    )


def _agent_no_output() -> AgentDef:
    """Agent definition without output schema."""
    return AgentDef(
        name="test_agent",
        model="claude-3-5-sonnet-latest",
        prompt="Test prompt",
    )


# ---------------------------------------------------------------------------
# Tests: Interrupt signal handling in _execute_agentic_loop
# ---------------------------------------------------------------------------


class TestAgenticLoopInterrupt:
    """Tests for interrupt handling within the agentic loop."""

    @pytest.mark.asyncio
    async def test_interrupt_on_first_iteration_with_emit_output(self) -> None:
        """Interrupt on the first iteration sends a user message and returns partial."""
        provider = _make_provider()

        # The interrupt response returns emit_output with partial data
        emit_response = _make_response(
            [_make_tool_use_block("emit_output", {"result": "partial data"})]
        )
        provider._execute_api_call = AsyncMock(return_value=emit_response)

        interrupt = asyncio.Event()
        interrupt.set()

        tools = [
            {
                "name": "emit_output",
                "description": "Emit output",
                "input_schema": {"type": "object", "properties": {"result": {"type": "string"}}},
            }
        ]

        response, tokens, is_partial = await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=tools,
            output_schema={"result": OutputField(type="string")},
            has_output_schema=True,
            interrupt_signal=interrupt,
        )

        assert is_partial is True
        assert tokens > 0
        assert not interrupt.is_set()  # Signal should be cleared

    @pytest.mark.asyncio
    async def test_interrupt_sends_user_message_not_system(self) -> None:
        """The interrupt prompt is sent as a user message, not a system message."""
        provider = _make_provider()

        captured_messages: list[list[dict[str, Any]]] = []

        async def capture_api_call(messages: Any, **kwargs: Any) -> MagicMock:
            captured_messages.append(list(messages))
            return _make_response([_make_tool_use_block("emit_output", {"result": "partial"})])

        provider._execute_api_call = AsyncMock(side_effect=capture_api_call)

        interrupt = asyncio.Event()
        interrupt.set()

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "original prompt"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=[{"name": "emit_output", "description": "d", "input_schema": {}}],
            output_schema={"result": OutputField(type="string")},
            has_output_schema=True,
            interrupt_signal=interrupt,
        )

        # The API was called with the interrupt user message appended
        assert len(captured_messages) == 1
        messages = captured_messages[0]
        last_message = messages[-1]
        assert last_message["role"] == "user"
        assert "emit_output" in last_message["content"]
        assert "interrupted" in last_message["content"].lower()

    @pytest.mark.asyncio
    async def test_interrupt_on_second_iteration(self) -> None:
        """Interrupt is detected after the first iteration's tool call completes."""
        provider = _make_provider()
        provider._mcp_manager = MagicMock()
        provider._mcp_manager.call_tool = AsyncMock(return_value="tool result")

        interrupt = asyncio.Event()

        # First call (parse_recovery): returns an MCP tool_use (not emit_output)
        mcp_response = _make_response(
            [_make_tool_use_block("search_web", {"query": "test"}, "mcp1")]
        )
        # Second call (interrupt prompt via _execute_api_call): returns emit_output
        emit_response = _make_response(
            [_make_tool_use_block("emit_output", {"result": "partial after tools"})]
        )

        async def mock_parse_recovery(messages: Any, **kwargs: Any) -> MagicMock:
            """First iteration goes through parse recovery and returns MCP tool."""
            # Set interrupt during first iteration so it's caught at top of second
            interrupt.set()
            return mcp_response

        provider._execute_with_parse_recovery = AsyncMock(side_effect=mock_parse_recovery)
        provider._execute_api_call = AsyncMock(return_value=emit_response)

        tools = [
            {"name": "emit_output", "description": "d", "input_schema": {}},
            {"name": "search_web", "description": "s", "input_schema": {}},
        ]

        response, tokens, is_partial = await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=tools,
            output_schema={"result": OutputField(type="string")},
            has_output_schema=True,
            interrupt_signal=interrupt,
        )

        assert is_partial is True
        # First call via parse_recovery, second call via _execute_api_call for interrupt
        provider._execute_with_parse_recovery.assert_awaited_once()
        provider._execute_api_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_interrupt_when_signal_not_set(self) -> None:
        """Normal completion when interrupt signal exists but is never set."""
        provider = _make_provider()

        emit_response = _make_response(
            [_make_tool_use_block("emit_output", {"result": "complete data"})]
        )
        provider._execute_api_call = AsyncMock(return_value=emit_response)
        provider._execute_with_parse_recovery = AsyncMock(return_value=emit_response)

        interrupt = asyncio.Event()  # Not set

        response, tokens, is_partial = await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=[{"name": "emit_output", "description": "d", "input_schema": {}}],
            output_schema={"result": OutputField(type="string")},
            has_output_schema=True,
            interrupt_signal=interrupt,
        )

        assert is_partial is False

    @pytest.mark.asyncio
    async def test_no_interrupt_when_signal_is_none(self) -> None:
        """Normal completion when no interrupt signal is provided."""
        provider = _make_provider()

        text_response = _make_response([_make_text_block("some result")])
        provider._execute_api_call = AsyncMock(return_value=text_response)

        response, tokens, is_partial = await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            interrupt_signal=None,
        )

        assert is_partial is False

    @pytest.mark.asyncio
    async def test_interrupt_without_output_schema(self) -> None:
        """Interrupt with no output schema asks for text summary."""
        provider = _make_provider()

        captured_messages: list[list[dict[str, Any]]] = []

        async def capture_api_call(messages: Any, **kwargs: Any) -> MagicMock:
            captured_messages.append(list(messages))
            return _make_response([_make_text_block("partial text result")])

        provider._execute_api_call = AsyncMock(side_effect=capture_api_call)

        interrupt = asyncio.Event()
        interrupt.set()

        response, tokens, is_partial = await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            interrupt_signal=interrupt,
        )

        assert is_partial is True
        # Verify the prompt asks for a text result (no emit_output mention)
        last_msg = captured_messages[0][-1]
        assert last_msg["role"] == "user"
        assert "emit_output" not in last_msg["content"]
        assert "interrupted" in last_msg["content"].lower()


# ---------------------------------------------------------------------------
# Tests: Full execute() flow with interrupt
# ---------------------------------------------------------------------------


class TestExecuteWithInterrupt:
    """Tests for the full execute() -> _execute_with_retry -> agentic_loop flow."""

    @pytest.mark.asyncio
    async def test_execute_returns_partial_output(self) -> None:
        """execute() returns AgentOutput with partial=True on interrupt."""
        provider = _make_provider()

        emit_response = _make_response(
            [_make_tool_use_block("emit_output", {"result": "partial data"})]
        )
        provider._execute_api_call = AsyncMock(return_value=emit_response)

        interrupt = asyncio.Event()
        interrupt.set()

        agent = _agent_with_output()
        output = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="test prompt",
            tools=None,
            interrupt_signal=interrupt,
        )

        assert output.partial is True
        assert output.content == {"result": "partial data"}
        assert output.model == "claude-3-5-sonnet-latest"

    @pytest.mark.asyncio
    async def test_partial_output_not_schema_validated(self) -> None:
        """Partial output is NOT validated against the agent's output schema."""
        provider = _make_provider()

        # Return a partial result that doesn't match the schema
        # (schema expects "result" as string, but we return "partial_data" as int)
        emit_response = _make_response([_make_tool_use_block("emit_output", {"wrong_field": 42})])
        provider._execute_api_call = AsyncMock(return_value=emit_response)

        interrupt = asyncio.Event()
        interrupt.set()

        agent = _agent_with_output()
        # This should NOT raise ValidationError because partial output skips validation
        output = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="test prompt",
            tools=None,
            interrupt_signal=interrupt,
        )

        assert output.partial is True
        assert output.content == {"wrong_field": 42}

    @pytest.mark.asyncio
    async def test_execute_without_interrupt_completes_normally(self) -> None:
        """execute() completes normally when interrupt signal is not set."""
        provider = _make_provider()

        emit_response = _make_response(
            [_make_tool_use_block("emit_output", {"result": "complete data"})]
        )
        provider._execute_with_parse_recovery = AsyncMock(return_value=emit_response)

        interrupt = asyncio.Event()  # Not set

        agent = _agent_with_output()
        output = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="test prompt",
            tools=None,
            interrupt_signal=interrupt,
        )

        assert output.partial is False
        assert output.content == {"result": "complete data"}

    @pytest.mark.asyncio
    async def test_partial_output_fallback_to_text(self) -> None:
        """When emit_output parsing fails on interrupt, falls back to text content."""
        provider = _make_provider()

        # Return text instead of emit_output tool use
        text_response = _make_response([_make_text_block("Here is my partial answer so far.")])
        provider._execute_api_call = AsyncMock(return_value=text_response)

        interrupt = asyncio.Event()
        interrupt.set()

        agent = _agent_with_output()
        output = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="test prompt",
            tools=None,
            interrupt_signal=interrupt,
        )

        assert output.partial is True
        # Fell back to text extraction since emit_output tool was not called
        assert "text" in output.content or "result" in output.content

    @pytest.mark.asyncio
    async def test_interrupt_signal_cleared_after_handling(self) -> None:
        """The interrupt signal is cleared in the agentic loop after handling."""
        provider = _make_provider()

        emit_response = _make_response([_make_tool_use_block("emit_output", {"result": "partial"})])
        provider._execute_api_call = AsyncMock(return_value=emit_response)

        interrupt = asyncio.Event()
        interrupt.set()

        agent = _agent_with_output()
        await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="test prompt",
            interrupt_signal=interrupt,
        )

        assert not interrupt.is_set()

    @pytest.mark.asyncio
    async def test_token_accounting_includes_interrupt_call(self) -> None:
        """Token usage includes tokens from the interrupt prompt call."""
        provider = _make_provider()

        emit_response = _make_response(
            [_make_tool_use_block("emit_output", {"result": "partial"})],
            input_tokens=200,
            output_tokens=100,
        )
        provider._execute_api_call = AsyncMock(return_value=emit_response)

        interrupt = asyncio.Event()
        interrupt.set()

        agent = _agent_with_output()
        output = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="test prompt",
            interrupt_signal=interrupt,
        )

        assert output.tokens_used == 300  # 200 input + 100 output


# ---------------------------------------------------------------------------
# Tests: Re-invocation with guidance
# ---------------------------------------------------------------------------


class TestReInvocationWithGuidance:
    """Tests for Claude re-invocation with guidance after interrupt."""

    @pytest.mark.asyncio
    async def test_guidance_appended_to_rendered_prompt(self) -> None:
        """When re-invoked with guidance, the prompt includes the guidance text."""
        provider = _make_provider()

        captured_messages: list[list[dict[str, Any]]] = []

        async def capture_api_call(messages: Any, **kwargs: Any) -> MagicMock:
            captured_messages.append(list(messages))
            return _make_response([_make_tool_use_block("emit_output", {"result": "final result"})])

        provider._execute_with_parse_recovery = AsyncMock(side_effect=capture_api_call)

        agent = _agent_with_output()

        # Simulate re-invocation with guidance appended to prompt
        guidance_section = (
            "\n\n[User Guidance]\n"
            "The following guidance was provided by the user during workflow execution. "
            "Incorporate this guidance into your response:\n"
            "- Focus on Python 3.12+ features"
        )
        rendered_prompt = "Original prompt" + guidance_section

        output = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt=rendered_prompt,
            interrupt_signal=None,  # Fresh conversation, no interrupt
        )

        assert output.partial is False
        # The messages should start fresh with the guidance in the prompt
        assert len(captured_messages) == 1
        user_message = captured_messages[0][0]
        assert user_message["role"] == "user"
        assert "Original prompt" in user_message["content"]
        assert "[User Guidance]" in user_message["content"]
        assert "Focus on Python 3.12+ features" in user_message["content"]

    @pytest.mark.asyncio
    async def test_fresh_conversation_on_re_invocation(self) -> None:
        """Re-invocation starts a fresh conversation (no prior message history)."""
        provider = _make_provider()

        captured_messages: list[list[dict[str, Any]]] = []

        async def capture_api_call(messages: Any, **kwargs: Any) -> MagicMock:
            captured_messages.append(list(messages))
            return _make_response([_make_tool_use_block("emit_output", {"result": "done"})])

        provider._execute_with_parse_recovery = AsyncMock(side_effect=capture_api_call)

        agent = _agent_with_output()

        # First call - simulating original execution (interrupt not relevant here)
        await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="First prompt",
            interrupt_signal=None,
        )

        # Second call - simulating re-invocation after interrupt
        await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Second prompt with guidance",
            interrupt_signal=None,
        )

        # Each call should have a fresh message list with a single user message
        assert len(captured_messages) == 2
        assert len(captured_messages[0]) == 1  # First: one user message
        assert len(captured_messages[1]) == 1  # Second: one user message (fresh)
        assert captured_messages[0][0]["content"].startswith("First prompt")
        assert captured_messages[1][0]["content"].startswith("Second prompt")


# ---------------------------------------------------------------------------
# Tests: _request_partial_output
# ---------------------------------------------------------------------------


class TestRequestPartialOutput:
    """Tests for the _request_partial_output helper."""

    @pytest.mark.asyncio
    async def test_with_output_schema_mentions_emit_output(self) -> None:
        """When agent has output schema, interrupt prompt mentions emit_output."""
        provider = _make_provider()

        captured_messages: list[list[dict[str, Any]]] = []

        async def capture(messages: Any, **kwargs: Any) -> MagicMock:
            captured_messages.append(list(messages))
            return _make_response([_make_tool_use_block("emit_output", {"result": "partial"})])

        provider._execute_api_call = AsyncMock(side_effect=capture)

        messages: list[dict[str, Any]] = [{"role": "user", "content": "original"}]
        response, tokens = await provider._request_partial_output(
            working_messages=messages,
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=[{"name": "emit_output"}],
            has_output_schema=True,
        )

        last_msg = captured_messages[0][-1]
        assert last_msg["role"] == "user"
        assert "emit_output" in last_msg["content"]

    @pytest.mark.asyncio
    async def test_without_output_schema_no_emit_output(self) -> None:
        """When agent has no output schema, interrupt prompt does not mention emit_output."""
        provider = _make_provider()

        captured_messages: list[list[dict[str, Any]]] = []

        async def capture(messages: Any, **kwargs: Any) -> MagicMock:
            captured_messages.append(list(messages))
            return _make_response([_make_text_block("partial text")])

        provider._execute_api_call = AsyncMock(side_effect=capture)

        messages: list[dict[str, Any]] = [{"role": "user", "content": "original"}]
        response, tokens = await provider._request_partial_output(
            working_messages=messages,
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            has_output_schema=False,
        )

        last_msg = captured_messages[0][-1]
        assert last_msg["role"] == "user"
        assert "emit_output" not in last_msg["content"]

    @pytest.mark.asyncio
    async def test_token_accounting(self) -> None:
        """Tokens from the interrupt call are returned."""
        provider = _make_provider()

        response = _make_response(
            [_make_text_block("partial")],
            input_tokens=150,
            output_tokens=75,
        )
        provider._execute_api_call = AsyncMock(return_value=response)

        messages: list[dict[str, Any]] = [{"role": "user", "content": "original"}]
        _, tokens = await provider._request_partial_output(
            working_messages=messages,
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            has_output_schema=False,
        )

        assert tokens == 225  # 150 + 75
