"""Unit tests for Pydantic AI -> Conductor event mapping.

These tests verify that ``map_pydantic_event`` and the helper emitters translate
Pydantic AI streaming events into the exact payload shapes that Conductor
subscribers (console, JSONL, web dashboard) expect.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    OutputToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.test import TestModel

from conductor.providers._pydantic_ai.events import (
    emit_agent_turn_start,
    emit_output_recovery_event,
    emit_pydantic_event,
    map_pydantic_event,
    maybe_emit_tool_truncation,
)


@pytest.fixture
def agent() -> Agent[Any, Any]:
    """Return a named Pydantic AI agent for event-mapping tests."""
    return Agent(TestModel(), name="mapper")


class TestMapPydanticEventText:
    """Requirement: text content maps to ``agent_message``."""

    def test_full_text_part(self, agent: Agent[Any, Any]) -> None:
        """A ``PartStartEvent`` with a full ``TextPart`` emits ``agent_message``."""
        event = PartStartEvent(index=0, part=TextPart(content="hello"))
        assert map_pydantic_event(agent, event) == (
            "agent_message",
            {"agent_name": "mapper", "content": "hello"},
        )

    def test_text_delta(self, agent: Agent[Any, Any]) -> None:
        """A ``PartDeltaEvent(TextPartDelta)`` emits an ``agent_message`` chunk."""
        event = PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="chunk"))
        assert map_pydantic_event(agent, event) == (
            "agent_message",
            {"agent_name": "mapper", "content": "chunk"},
        )

    def test_empty_text_ignored(self, agent: Agent[Any, Any]) -> None:
        """Empty text parts and deltas must not emit events."""
        assert map_pydantic_event(agent, PartStartEvent(index=0, part=TextPart(content=""))) is None
        assert (
            map_pydantic_event(
                agent, PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=""))
            )
            is None
        )


class TestMapPydanticEventReasoning:
    """Requirement: thinking content maps to ``agent_reasoning``."""

    def test_full_thinking_part(self, agent: Agent[Any, Any]) -> None:
        """A ``PartStartEvent`` with a full ``ThinkingPart`` emits ``agent_reasoning``."""
        event = PartStartEvent(index=0, part=ThinkingPart(content="thinking"))
        assert map_pydantic_event(agent, event) == (
            "agent_reasoning",
            {"agent_name": "mapper", "content": "thinking"},
        )

    def test_thinking_delta(self, agent: Agent[Any, Any]) -> None:
        """A ``PartDeltaEvent(ThinkingPartDelta)`` emits an ``agent_reasoning`` chunk."""
        event = PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta="thought"))
        assert map_pydantic_event(agent, event) == (
            "agent_reasoning",
            {"agent_name": "mapper", "content": "thought"},
        )

    def test_empty_thinking_ignored(self, agent: Agent[Any, Any]) -> None:
        """Empty thinking parts and deltas must not emit events."""
        assert (
            map_pydantic_event(agent, PartStartEvent(index=0, part=ThinkingPart(content="")))
            is None
        )
        assert (
            map_pydantic_event(
                agent, PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=""))
            )
            is None
        )


class TestMapPydanticEventToolCalls:
    """Requirement: tool calls and results map to ``agent_tool_start`` / ``agent_tool_complete``."""

    def test_function_tool_call_event(self, agent: Agent[Any, Any]) -> None:
        """``FunctionToolCallEvent`` emits ``agent_tool_start`` with name and arguments."""
        call = ToolCallPart(
            tool_name="fs__read",
            args={"path": "/tmp/test.txt"},
            tool_call_id="call-1",
        )
        event = FunctionToolCallEvent(part=call)
        mapped = map_pydantic_event(agent, event)

        assert mapped is not None
        event_type, data = mapped
        assert event_type == "agent_tool_start"
        assert data["tool_name"] == "fs__read"
        assert data["agent_name"] == "mapper"
        assert '"path": "/tmp/test.txt"' in data["arguments"]

    def test_part_start_tool_call_is_ignored(self, agent: Agent[Any, Any]) -> None:
        # Requirement: a generated tool call does not duplicate the execution-start event.
        call = ToolCallPart(
            tool_name="web__search",
            args={"query": "pydantic"},
            tool_call_id="call-2",
        )
        event = PartStartEvent(index=0, part=call)

        assert map_pydantic_event(agent, event) is None

    def test_function_tool_result_event(self, agent: Agent[Any, Any]) -> None:
        """``FunctionToolResultEvent`` emits ``agent_tool_complete`` with the result preview."""
        return_part = ToolReturnPart(
            tool_name="fs__read",
            content="file contents",
            tool_call_id="call-1",
        )
        event = FunctionToolResultEvent(part=return_part)
        mapped = map_pydantic_event(agent, event)

        assert mapped is not None
        event_type, data = mapped
        assert event_type == "agent_tool_complete"
        assert data["tool_name"] == "fs__read"
        assert data["result"] == "file contents"
        assert data["agent_name"] == "mapper"

    def test_tool_call_with_no_args(self, agent: Agent[Any, Any]) -> None:
        """Tool calls with no arguments should emit ``arguments: None``."""
        call = ToolCallPart(tool_name="noop", args=None, tool_call_id="call-3")
        event = FunctionToolCallEvent(part=call)
        mapped = map_pydantic_event(agent, event)

        assert mapped is not None
        assert mapped[0] == "agent_tool_start"
        assert mapped[1]["arguments"] is None


class TestUnknownEvents:
    """Requirement: unsupported events are ignored."""

    def test_unknown_event_kind_returns_none(self, agent: Agent[Any, Any]) -> None:
        """An arbitrary object must not crash or produce a phantom event."""
        assert map_pydantic_event(agent, object()) is None


class TestEmitPydanticEvent:
    """Requirement: events are forwarded through a callback and errors are swallowed."""

    def test_event_forwarded(self, agent: Agent[Any, Any]) -> None:
        """``emit_pydantic_event`` should call the callback with the mapped event."""
        recorded: list[tuple[str, dict[str, Any]]] = []
        event = PartStartEvent(index=0, part=TextPart(content="hi"))

        emit_pydantic_event(agent, event, lambda t, d: recorded.append((t, d)))

        assert recorded == [("agent_message", {"agent_name": "mapper", "content": "hi"})]

    def test_callback_error_swallowed(self, agent: Agent[Any, Any]) -> None:
        """A raising subscriber must not propagate through ``emit_pydantic_event``."""
        event = PartStartEvent(index=0, part=TextPart(content="hi"))

        emit_pydantic_event(
            agent,
            event,
            lambda _t, _d: (_ for _ in ()).throw(RuntimeError("boom")),
        )

    def test_no_callback_is_noop(self, agent: Agent[Any, Any]) -> None:
        """``emit_pydantic_event`` with ``None`` callback should return silently."""
        event = PartStartEvent(index=0, part=TextPart(content="hi"))
        emit_pydantic_event(agent, event, None)


class TestEmitAgentTurnStart:
    """Requirement: ``agent_turn_start`` is synthesized for iterations and API calls."""

    def test_iteration_turn(self) -> None:
        """``emit_agent_turn_start`` with an integer emits ``{"turn": N}``."""
        recorded: list[tuple[str, dict[str, Any]]] = []
        emit_agent_turn_start(lambda t, d: recorded.append((t, d)), 3)
        assert recorded == [("agent_turn_start", {"turn": 3})]

    def test_awaiting_model_turn(self) -> None:
        """``emit_agent_turn_start`` with ``"awaiting_model"`` emits the parity shape."""
        recorded: list[tuple[str, dict[str, Any]]] = []
        emit_agent_turn_start(lambda t, d: recorded.append((t, d)), "awaiting_model")
        assert recorded == [("agent_turn_start", {"turn": "awaiting_model"})]

    def test_callback_error_swallowed(self) -> None:
        """A raising subscriber must not propagate through ``emit_agent_turn_start``."""
        emit_agent_turn_start(
            lambda _t, _d: (_ for _ in ()).throw(RuntimeError("boom")),
            1,
        )

    def test_no_callback_is_noop(self) -> None:
        """``emit_agent_turn_start`` with ``None`` callback should return silently."""
        emit_agent_turn_start(None, 1)


class TestEmitOutputRecoveryEvent:
    def test_json_validation_error_emits_syntax_reason(self) -> None:
        # Requirement: invalid JSON is distinguished from schema validation failures.
        recorded: list[tuple[str, dict[str, Any]]] = []
        event = OutputToolResultEvent(
            part=RetryPromptPart(
                content=[
                    {
                        "type": "json_invalid",
                        "loc": (),
                        "msg": "Invalid JSON",
                        "input": "{",
                    }
                ],
                tool_name="final_result",
                tool_call_id="call-1",
            )
        )

        emitted = emit_output_recovery_event(
            event,
            lambda event_type, data: recorded.append((event_type, data)),
            attempt=1,
            max_attempts=2,
        )

        assert emitted is True
        assert recorded[0][0] == "agent_parse_recovery"
        assert recorded[0][1]["reason"] == "syntax"


class TestMaybeEmitToolTruncation:
    """Requirement: ``agent_tool_output_truncated`` is emitted for truncated results."""

    def test_truncated_result_emits_event(self, agent: Agent[Any, Any]) -> None:
        """A marker-bearing tool result should emit ``agent_tool_output_truncated``."""
        recorded: list[tuple[str, dict[str, Any]]] = []
        marker = (
            "\n\n[output truncated: 2000 chars -> 1000 kept; "
            "full output saved to: /tmp/spill.txt. "
            "The full output was truncated; refine the tool arguments to return less data.]"
        )
        result = "x" * 1000 + marker

        maybe_emit_tool_truncation(agent, "big__data", result, lambda t, d: recorded.append((t, d)))

        assert len(recorded) == 1
        event_type, data = recorded[0]
        assert event_type == "agent_tool_output_truncated"
        assert data["tool_name"] == "big__data"
        assert data["agent_name"] == "mapper"
        assert data["original_chars"] == 2000
        assert data["kept_chars"] == 1000
        assert data["spill_path"] == "/tmp/spill.txt"

    def test_untruncated_result_is_silent(self, agent: Agent[Any, Any]) -> None:
        """A plain tool result must not emit a truncation event."""
        recorded: list[tuple[str, dict[str, Any]]] = []
        maybe_emit_tool_truncation(
            agent, "small__data", "short result", lambda t, d: recorded.append((t, d))
        )
        assert recorded == []

    def test_truncation_callback_error_swallowed(self, agent: Agent[Any, Any]) -> None:
        """A raising subscriber must not propagate through ``maybe_emit_tool_truncation``."""
        marker = (
            "\n\n[output truncated: 100 chars -> 50 kept. "
            "The full output was truncated; refine the tool arguments to return less data.]"
        )
        maybe_emit_tool_truncation(
            agent,
            "big__data",
            "x" * 50 + marker,
            lambda _t, _d: (_ for _ in ()).throw(RuntimeError("boom")),
        )

    def test_no_callback_is_noop(self, agent: Agent[Any, Any]) -> None:
        """``maybe_emit_tool_truncation`` with ``None`` callback should return silently."""
        maybe_emit_tool_truncation(agent, "big__data", "anything", None)


class TestAgentNameFallback:
    """Requirement: events fall back to ``agent`` when the agent has no name."""

    def test_unnamed_agent(self) -> None:
        """An unnamed agent should still produce valid ``agent_name`` payloads."""
        unnamed = Agent(TestModel())
        event = PartStartEvent(index=0, part=TextPart(content="hi"))
        mapped = map_pydantic_event(unnamed, event)

        assert mapped is not None
        assert mapped[1]["agent_name"] == "agent"
