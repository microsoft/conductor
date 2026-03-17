"""Tests for event_callback wiring in the Claude provider.

Verifies that the Claude provider emits streaming events (agent_turn_start,
agent_message, agent_tool_start, agent_tool_complete) through its agentic
loop, matching the Copilot provider's event types and data shapes for web
dashboard compatibility.

Related issue: https://github.com/microsoft/conductor/issues/39
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from conductor.providers.claude import ClaudeProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_use_block(name: str, input_data: dict[str, Any] | None = None) -> MagicMock:
    """Create a mock tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.id = f"call_{name}"
    block.input = input_data or {}
    return block


def _make_text_block(text: str) -> MagicMock:
    """Create a mock text content block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_response(blocks: list[MagicMock]) -> MagicMock:
    """Create a mock Claude API response with the given content blocks."""
    resp = MagicMock()
    resp.content = blocks
    resp.usage = MagicMock()
    resp.usage.input_tokens = 100
    resp.usage.output_tokens = 50
    return resp


def _make_provider_with_mcp() -> ClaudeProvider:
    """Create a minimal ClaudeProvider with a mock MCP manager."""
    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider._client = MagicMock()
    provider._mcp_servers_config = None
    provider._default_model = "claude-3-5-sonnet-latest"
    provider._default_temperature = None
    provider._default_max_tokens = 8192
    provider._retry_config = MagicMock()
    provider._retry_config.max_attempts = 1
    provider._retry_history = []
    provider._max_parse_recovery_attempts = 2
    provider._max_schema_depth = 10
    provider._default_max_agent_iterations = 50
    provider._default_max_session_seconds = None

    mock_mcp_manager = MagicMock()
    mock_mcp_manager.has_servers.return_value = True
    mock_mcp_manager.get_all_tools.return_value = []
    mock_mcp_manager.call_tool = AsyncMock(return_value="tool result")
    provider._mcp_manager = mock_mcp_manager

    return provider


def _make_bare_provider() -> ClaudeProvider:
    """Create a minimal ClaudeProvider without MCP."""
    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider._client = MagicMock()
    provider._mcp_manager = None
    provider._mcp_servers_config = None
    provider._default_model = "claude-3-5-sonnet-latest"
    provider._default_temperature = None
    provider._default_max_tokens = 8192
    provider._retry_config = MagicMock()
    provider._retry_config.max_attempts = 1
    provider._retry_history = []
    provider._max_parse_recovery_attempts = 2
    provider._max_schema_depth = 10
    provider._default_max_agent_iterations = 50
    provider._default_max_session_seconds = None
    return provider


# ---------------------------------------------------------------------------
# Tests: agent_turn_start
# ---------------------------------------------------------------------------


class TestAgentTurnStartEvent:
    """Verify agent_turn_start is emitted at each iteration."""

    @pytest.mark.asyncio
    async def test_emitted_on_single_iteration(self) -> None:
        """A single-iteration loop should emit one agent_turn_start."""
        provider = _make_bare_provider()
        events: list[tuple[str, dict[str, Any]]] = []

        text_response = _make_response([_make_text_block("Hello")])
        provider._execute_api_call = AsyncMock(return_value=text_response)

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
        )

        turn_events = [(t, d) for t, d in events if t == "agent_turn_start"]
        assert len(turn_events) == 1
        assert turn_events[0][1] == {"turn": 1}

    @pytest.mark.asyncio
    async def test_emitted_on_multiple_iterations(self) -> None:
        """Multiple iterations should emit agent_turn_start for each."""
        provider = _make_provider_with_mcp()
        events: list[tuple[str, dict[str, Any]]] = []

        # First response: MCP tool call → triggers second iteration
        mcp_response = _make_response(
            [
                _make_tool_use_block("filesystem__read_file", {"path": "/tmp/test.txt"}),
            ]
        )
        # Second response: text → exits loop
        text_response = _make_response([_make_text_block("Done")])

        provider._execute_api_call = AsyncMock(side_effect=[mcp_response, text_response])

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
        )

        turn_events = [(t, d) for t, d in events if t == "agent_turn_start"]
        assert len(turn_events) == 2
        assert turn_events[0][1] == {"turn": 1}
        assert turn_events[1][1] == {"turn": 2}


# ---------------------------------------------------------------------------
# Tests: agent_message
# ---------------------------------------------------------------------------


class TestAgentMessageEvent:
    """Verify agent_message is emitted for text blocks in responses."""

    @pytest.mark.asyncio
    async def test_emitted_for_text_response(self) -> None:
        """Text content blocks should generate agent_message events."""
        provider = _make_bare_provider()
        events: list[tuple[str, dict[str, Any]]] = []

        text_response = _make_response([_make_text_block("Hello, world!")])
        provider._execute_api_call = AsyncMock(return_value=text_response)

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
        )

        msg_events = [(t, d) for t, d in events if t == "agent_message"]
        assert len(msg_events) == 1
        assert msg_events[0][1] == {"content": "Hello, world!"}

    @pytest.mark.asyncio
    async def test_multiple_text_blocks(self) -> None:
        """Multiple text blocks in one response should each emit an event."""
        provider = _make_bare_provider()
        events: list[tuple[str, dict[str, Any]]] = []

        text_response = _make_response(
            [
                _make_text_block("First"),
                _make_text_block("Second"),
            ]
        )
        provider._execute_api_call = AsyncMock(return_value=text_response)

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
        )

        msg_events = [(t, d) for t, d in events if t == "agent_message"]
        assert len(msg_events) == 2
        assert msg_events[0][1]["content"] == "First"
        assert msg_events[1][1]["content"] == "Second"

    @pytest.mark.asyncio
    async def test_empty_text_not_emitted(self) -> None:
        """Empty text blocks should not generate agent_message events."""
        provider = _make_bare_provider()
        events: list[tuple[str, dict[str, Any]]] = []

        text_response = _make_response([_make_text_block("")])
        provider._execute_api_call = AsyncMock(return_value=text_response)

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
        )

        msg_events = [(t, d) for t, d in events if t == "agent_message"]
        assert len(msg_events) == 0


# ---------------------------------------------------------------------------
# Tests: agent_tool_start / agent_tool_complete
# ---------------------------------------------------------------------------


class TestAgentToolEvents:
    """Verify agent_tool_start and agent_tool_complete are emitted for MCP tool calls."""

    @pytest.mark.asyncio
    async def test_tool_start_and_complete_on_success(self) -> None:
        """Successful MCP tool call should emit both start and complete events."""
        provider = _make_provider_with_mcp()
        events: list[tuple[str, dict[str, Any]]] = []

        mcp_response = _make_response(
            [
                _make_tool_use_block("filesystem__read_file", {"path": "/tmp/test.txt"}),
            ]
        )
        text_response = _make_response([_make_text_block("Done")])
        provider._execute_api_call = AsyncMock(side_effect=[mcp_response, text_response])
        provider._mcp_manager.call_tool = AsyncMock(return_value="file content here")

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
        )

        start_events = [(t, d) for t, d in events if t == "agent_tool_start"]
        assert len(start_events) == 1
        assert start_events[0][1]["tool_name"] == "filesystem__read_file"
        assert "path" in start_events[0][1]["arguments"]

        complete_events = [(t, d) for t, d in events if t == "agent_tool_complete"]
        assert len(complete_events) == 1
        assert complete_events[0][1]["tool_name"] == "filesystem__read_file"
        assert complete_events[0][1]["result"] == "file content here"

    @pytest.mark.asyncio
    async def test_tool_complete_on_failure(self) -> None:
        """Failed MCP tool call should emit start and complete with error."""
        provider = _make_provider_with_mcp()
        events: list[tuple[str, dict[str, Any]]] = []

        mcp_response = _make_response(
            [
                _make_tool_use_block("filesystem__read_file", {"path": "/nonexistent"}),
            ]
        )
        text_response = _make_response([_make_text_block("Error handled")])
        provider._execute_api_call = AsyncMock(side_effect=[mcp_response, text_response])
        provider._mcp_manager.call_tool = AsyncMock(side_effect=RuntimeError("File not found"))

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
        )

        start_events = [(t, d) for t, d in events if t == "agent_tool_start"]
        assert len(start_events) == 1
        assert start_events[0][1]["tool_name"] == "filesystem__read_file"

        complete_events = [(t, d) for t, d in events if t == "agent_tool_complete"]
        assert len(complete_events) == 1
        assert complete_events[0][1]["tool_name"] == "filesystem__read_file"
        assert "Error:" in complete_events[0][1]["result"]
        assert "File not found" in complete_events[0][1]["result"]

    @pytest.mark.asyncio
    async def test_multiple_tool_calls(self) -> None:
        """Multiple MCP tool calls in one response should each emit events."""
        provider = _make_provider_with_mcp()
        events: list[tuple[str, dict[str, Any]]] = []

        mcp_response = _make_response(
            [
                _make_tool_use_block("filesystem__read_file", {"path": "/a"}),
                _make_tool_use_block("web_search__search", {"query": "test"}),
            ]
        )
        text_response = _make_response([_make_text_block("Done")])
        provider._execute_api_call = AsyncMock(side_effect=[mcp_response, text_response])
        provider._mcp_manager.call_tool = AsyncMock(return_value="result")

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
        )

        start_events = [(t, d) for t, d in events if t == "agent_tool_start"]
        assert len(start_events) == 2
        assert start_events[0][1]["tool_name"] == "filesystem__read_file"
        assert start_events[1][1]["tool_name"] == "web_search__search"

        complete_events = [(t, d) for t, d in events if t == "agent_tool_complete"]
        assert len(complete_events) == 2

    @pytest.mark.asyncio
    async def test_tool_arguments_truncated(self) -> None:
        """Tool arguments longer than 500 chars should be truncated."""
        provider = _make_provider_with_mcp()
        events: list[tuple[str, dict[str, Any]]] = []

        long_input = {"data": "x" * 600}
        mcp_response = _make_response(
            [
                _make_tool_use_block("my_tool", long_input),
            ]
        )
        text_response = _make_response([_make_text_block("Done")])
        provider._execute_api_call = AsyncMock(side_effect=[mcp_response, text_response])

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
        )

        start_events = [(t, d) for t, d in events if t == "agent_tool_start"]
        assert len(start_events) == 1
        assert len(start_events[0][1]["arguments"]) <= 500

    @pytest.mark.asyncio
    async def test_tool_result_truncated(self) -> None:
        """Tool results longer than 500 chars should be truncated."""
        provider = _make_provider_with_mcp()
        events: list[tuple[str, dict[str, Any]]] = []

        mcp_response = _make_response(
            [
                _make_tool_use_block("my_tool", {"key": "val"}),
            ]
        )
        text_response = _make_response([_make_text_block("Done")])
        provider._execute_api_call = AsyncMock(side_effect=[mcp_response, text_response])
        provider._mcp_manager.call_tool = AsyncMock(return_value="y" * 600)

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
        )

        complete_events = [(t, d) for t, d in events if t == "agent_tool_complete"]
        assert len(complete_events) == 1
        assert len(complete_events[0][1]["result"]) <= 500


# ---------------------------------------------------------------------------
# Tests: no events when callback is None
# ---------------------------------------------------------------------------


class TestNoEventsWhenCallbackIsNone:
    """Verify no errors or events when event_callback is None."""

    @pytest.mark.asyncio
    async def test_text_response_no_callback(self) -> None:
        """Text response with no callback should not raise."""
        provider = _make_bare_provider()

        text_response = _make_response([_make_text_block("Hello")])
        provider._execute_api_call = AsyncMock(return_value=text_response)

        response, tokens, partial = await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=None,
        )

        assert response is text_response
        assert partial is False

    @pytest.mark.asyncio
    async def test_tool_calls_no_callback(self) -> None:
        """MCP tool calls with no callback should not raise."""
        provider = _make_provider_with_mcp()

        mcp_response = _make_response(
            [
                _make_tool_use_block("filesystem__read_file", {"path": "/tmp/test.txt"}),
            ]
        )
        text_response = _make_response([_make_text_block("Done")])
        provider._execute_api_call = AsyncMock(side_effect=[mcp_response, text_response])

        response, tokens, partial = await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=None,
        )

        assert response is text_response
        assert partial is False


# ---------------------------------------------------------------------------
# Tests: callback errors are swallowed
# ---------------------------------------------------------------------------


class TestCallbackErrorsSwallowed:
    """Verify that errors in event_callback don't break the agentic loop."""

    @pytest.mark.asyncio
    async def test_callback_error_does_not_break_loop(self) -> None:
        """If the callback raises, the loop should still complete."""
        provider = _make_provider_with_mcp()

        mcp_response = _make_response(
            [
                _make_tool_use_block("filesystem__read_file", {"path": "/tmp/test.txt"}),
            ]
        )
        text_response = _make_response([_make_text_block("Done")])
        provider._execute_api_call = AsyncMock(side_effect=[mcp_response, text_response])

        def bad_callback(event_type: str, data: dict[str, Any]) -> None:
            raise RuntimeError("callback exploded")

        response, tokens, partial = await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=bad_callback,
        )

        # Should still return normally despite callback errors
        assert response is text_response
        assert partial is False


# ---------------------------------------------------------------------------
# Tests: event_callback flows from execute() to _execute_agentic_loop()
# ---------------------------------------------------------------------------


class TestEventCallbackThreading:
    """Verify event_callback is threaded from execute() through the call chain."""

    @pytest.mark.asyncio
    async def test_callback_reaches_agentic_loop(self) -> None:
        """event_callback passed to execute() should reach _execute_agentic_loop."""
        provider = _make_bare_provider()
        events: list[tuple[str, dict[str, Any]]] = []

        text_response = _make_response([_make_text_block("Hello")])
        provider._execute_api_call = AsyncMock(return_value=text_response)

        # Mock _ensure_mcp_connected since we're calling _execute_with_retry directly
        provider._ensure_mcp_connected = AsyncMock()

        # Create a minimal agent mock
        agent = MagicMock()
        agent.output = None
        agent.model = None
        agent.max_agent_iterations = None
        agent.max_session_seconds = None

        await provider._execute_with_retry(
            agent=agent,
            context={},
            rendered_prompt="test prompt",
            tools=None,
            interrupt_signal=None,
            event_callback=lambda t, d: events.append((t, d)),
        )

        # Should have received at least an agent_turn_start event
        turn_events = [(t, d) for t, d in events if t == "agent_turn_start"]
        assert len(turn_events) == 1
        assert turn_events[0][1] == {"turn": 1}

        # Should have received an agent_message event
        msg_events = [(t, d) for t, d in events if t == "agent_message"]
        assert len(msg_events) == 1
        assert msg_events[0][1] == {"content": "Hello"}


# ---------------------------------------------------------------------------
# Tests: full event sequence
# ---------------------------------------------------------------------------


class TestFullEventSequence:
    """Verify the complete sequence of events for a multi-turn tool-use flow."""

    @pytest.mark.asyncio
    async def test_full_sequence(self) -> None:
        """A tool-use loop should emit events in the correct order."""
        provider = _make_provider_with_mcp()
        events: list[tuple[str, dict[str, Any]]] = []

        # Turn 1: text + tool call
        turn1_response = _make_response(
            [
                _make_text_block("Let me read that file."),
                _make_tool_use_block("filesystem__read_file", {"path": "/tmp/test.txt"}),
            ]
        )
        # Turn 2: final text
        turn2_response = _make_response(
            [
                _make_text_block("The file contains: hello"),
            ]
        )

        provider._execute_api_call = AsyncMock(side_effect=[turn1_response, turn2_response])
        provider._mcp_manager.call_tool = AsyncMock(return_value="hello")

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "read /tmp/test.txt"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
        )

        event_types = [t for t, _ in events]
        assert event_types == [
            "agent_turn_start",  # Turn 1 starts
            "agent_message",  # "Let me read that file."
            "agent_tool_start",  # filesystem__read_file starts
            "agent_tool_complete",  # filesystem__read_file completes
            "agent_turn_start",  # Turn 2 starts
            "agent_message",  # "The file contains: hello"
        ]
