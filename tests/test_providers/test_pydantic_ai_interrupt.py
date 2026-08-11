"""Unit tests for interrupt-aware Pydantic AI agent execution.

These tests verify that ``run_with_interrupt`` mirrors the interrupt semantics
of ``ClaudeProvider``:

- An interrupt set before the run starts takes the partial path immediately.
- An interrupt set between tool/model iterations stops the loop and returns a
  partial result without schema validation.
- A run without interrupts completes normally.
- A hard abort (cancellation of the in-flight API call) returns a cancelled
  outcome.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from pydantic_ai import Agent, AgentRetries, UsageLimits
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.output import ToolOutput
from pydantic_ai.tools import Tool

from conductor.config.schema import OutputField
from conductor.exceptions import ProviderError
from conductor.providers._pydantic_ai.converters import output_schema_to_pydantic_model
from conductor.providers._pydantic_ai.interrupt import _make_interrupt_message, run_with_interrupt


@pytest.fixture(autouse=True)
def _ensure_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a dummy API key so AnthropicModel construction succeeds."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _make_text_agent() -> Agent[Any, Any]:
    """Build a text-output Pydantic AI agent using the scripted TestModel."""
    return Agent(TestModel(), output_type=str, retries=0)


def _make_structured_agent() -> Agent[Any, Any]:
    """Build a structured-output Pydantic AI agent using a text-answering model.

    ``FunctionModel`` is used instead of ``TestModel(custom_output_args=...)``
    because the interrupt partial path overrides ``output_type=str`` per call,
    and TestModel's ``custom_output_args`` mode requires the output tool to be
    present on the request.
    """

    async def _answer_with_partial(messages: list[Any], info: Any) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content='{"answer": "partial"}')])

    output_schema = {"answer": OutputField(type="string")}
    dynamic_model = output_schema_to_pydantic_model("FormatterOutput", output_schema)
    assert dynamic_model is not None
    return Agent(
        FunctionModel(_answer_with_partial),
        output_type=ToolOutput(dynamic_model),
        retries=0,
    )


def test_structured_interrupt_asks_for_plain_text_partial() -> None:
    # Requirement: structured interrupt recovery asks for a plain-text partial
    # result because the partial run replaces tools and the output schema.
    message = _make_interrupt_message(has_output_schema=True)

    assert "partial result" in message.content
    assert "do not call any tools" in message.content


class TestInterruptBeforeRun:
    """Requirement: an already-set interrupt takes the partial path immediately."""

    @pytest.mark.asyncio
    async def test_interrupt_set_before_run_returns_partial_text(self) -> None:
        """When the interrupt signal is set before ``run_with_interrupt`` starts,
        the helper must not run the original task and instead perform a single
        partial request, returning ``is_partial=True`` with best-effort text."""
        signal = asyncio.Event()
        signal.set()

        outcome = await run_with_interrupt(
            _make_text_agent(),
            "say hello",
            interrupt_signal=signal,
            event_callback=None,
            has_output_schema=False,
        )

        assert outcome.is_partial is True
        assert outcome.partial_output is not None
        assert outcome.result is None
        assert outcome.is_cancelled is False

    @pytest.mark.asyncio
    async def test_interrupt_before_run_skips_structured_validation(self) -> None:
        """A pre-run interrupt on a structured-output agent must return
        ``is_partial=True`` without enforcing the output schema, so a plain text
        partial result does not raise validation errors."""
        signal = asyncio.Event()
        signal.set()

        outcome = await run_with_interrupt(
            _make_structured_agent(),
            "format this",
            interrupt_signal=signal,
            event_callback=None,
            has_output_schema=True,
        )

        assert outcome.is_partial is True
        assert outcome.result is None


class TestInterruptMidRun:
    """Requirement: an interrupt between tool/model iterations returns partial output."""

    @pytest.mark.asyncio
    async def test_interrupt_between_iterations_returns_partial(self) -> None:
        """When the interrupt signal is set after a tool call but before the next
        model request, the helper must stop the loop and ask the model for a
        partial result.  The returned outcome must be marked partial and must not
        validate the output against a schema."""
        signal = asyncio.Event()

        def set_interrupt_signal() -> str:
            signal.set()
            return "interrupted"

        async def _call_tool_then_answer(messages: list[Any], info: Any) -> ModelResponse:
            # First turn: invoke the tool that sets the interrupt signal. The
            # partial run after it overrides output_type=str, so this function
            # only needs to answer with text.
            if len(messages) == 1:
                from pydantic_ai.messages import ToolCallPart

                return ModelResponse(
                    parts=[ToolCallPart(tool_name="set_interrupt_signal", args={})]
                )
            return ModelResponse(parts=[TextPart(content='{"answer": "partial"}')])

        output_schema = {"answer": OutputField(type="string")}
        dynamic_model = output_schema_to_pydantic_model("MultiTurnOutput", output_schema)
        assert dynamic_model is not None
        agent = Agent(
            FunctionModel(_call_tool_then_answer),
            tools=[Tool(set_interrupt_signal)],
            output_type=ToolOutput(dynamic_model),
            retries=0,
        )

        outcome = await run_with_interrupt(
            agent,
            "format this",
            interrupt_signal=signal,
            event_callback=None,
            has_output_schema=True,
        )

        assert outcome.is_partial is True
        assert outcome.result is None
        assert outcome.is_cancelled is False


class TestPartialRunOverrides:
    """Requirement: the partial run must disable tools and the output schema."""

    @pytest.mark.asyncio
    async def test_partial_run_blocks_tool_calls_and_returns_text(self) -> None:
        """On the partial-result call the model must not be able to invoke
        tools registered on the agent: a tool call attempt must fail instead
        of producing a side effect, and a text answer must come back as the
        partial output. This exercises real pydantic-ai semantics (the
        ``toolsets=`` run kwarg is additive and cannot clear construction-time
        toolsets), not just the kwargs we pass."""
        from pydantic_ai.messages import ToolCallPart

        side_effects: list[str] = []

        def dangerous_tool() -> str:
            side_effects.append("called")
            return "done"

        async def _try_tool_then_answer(messages: list[Any], info: Any) -> ModelResponse:
            tool_attempted = any(
                getattr(p, "part_kind", "") == "tool-call" and p.tool_name == "dangerous_tool"
                for m in messages
                for p in getattr(m, "parts", [])
            )
            tool_returned = any(
                getattr(p, "part_kind", "") == "tool-return"
                for m in messages
                for p in getattr(m, "parts", [])
            )
            if tool_returned or tool_attempted:
                return ModelResponse(parts=[TextPart(content="final partial text")])
            return ModelResponse(parts=[ToolCallPart(tool_name="dangerous_tool", args={})])

        output_schema = {"answer": OutputField(type="string")}
        dynamic_model = output_schema_to_pydantic_model("PartialOutput", output_schema)
        assert dynamic_model is not None
        agent = Agent(
            FunctionModel(_try_tool_then_answer),
            tools=[Tool(dangerous_tool)],
            output_type=ToolOutput(dynamic_model),
            # A small retry budget lets the model recover from the rejected
            # tool call and answer with text, exercising the real override
            # semantics instead of surfacing UnexpectedModelBehavior.
            retries=2,
        )

        signal = asyncio.Event()
        signal.set()

        outcome = await run_with_interrupt(
            agent,
            "format this",
            interrupt_signal=signal,
            event_callback=None,
            has_output_schema=True,
        )

        assert side_effects == []
        assert outcome.is_partial is True
        assert isinstance(outcome.partial_output, str)


class TestNormalCompletion:
    """Requirement: absence of an interrupt yields a normal completed run."""

    @pytest.mark.asyncio
    async def test_no_interrupt_returns_full_result(self) -> None:
        """Without an interrupt signal, ``run_with_interrupt`` must return a
        normal ``RunOutcome`` with ``result`` populated and ``is_partial=False``."""
        signal = asyncio.Event()

        outcome = await run_with_interrupt(
            _make_text_agent(),
            "say hello",
            interrupt_signal=signal,
            event_callback=None,
            has_output_schema=False,
        )

        assert outcome.is_partial is False
        assert outcome.is_cancelled is False
        assert outcome.result is not None
        assert outcome.partial_output is None

    @pytest.mark.asyncio
    async def test_tool_execution_emits_one_start_event(self) -> None:
        # Requirement: each tool execution emits exactly one lifecycle start event.
        agent = Agent(
            TestModel(call_tools=["echo"]),
            tools=[Tool(lambda value: value, name="echo")],
            retries=0,
        )
        recorded: list[tuple[str, dict[str, Any]]] = []

        await run_with_interrupt(
            agent,
            "echo a value",
            interrupt_signal=None,
            event_callback=lambda event_type, data: recorded.append((event_type, data)),
            has_output_schema=False,
        )

        starts = [data for event_type, data in recorded if event_type == "agent_tool_start"]
        assert len(starts) == 1
        assert starts[0]["tool_name"] == "echo"

    @pytest.mark.asyncio
    async def test_iter_emits_awaiting_model_before_each_request(self) -> None:
        # Requirement: every iter-path model request exposes the awaiting-model boundary.
        agent = Agent(
            TestModel(call_tools=["echo"]),
            tools=[Tool(lambda value: value, name="echo")],
            retries=0,
        )
        recorded: list[tuple[str, dict[str, Any]]] = []

        outcome = await run_with_interrupt(
            agent,
            "echo a value",
            interrupt_signal=asyncio.Event(),
            event_callback=lambda event_type, data: recorded.append((event_type, data)),
            has_output_schema=False,
        )

        awaiting = [
            data
            for event_type, data in recorded
            if event_type == "agent_turn_start" and data["turn"] == "awaiting_model"
        ]
        assert len(awaiting) == 2
        assert outcome.result is not None
        usage = outcome.result.usage
        assert outcome.total_usage == {
            "requests": usage.requests,
            "request_tokens": None,
            "response_tokens": None,
            "total_tokens": usage.total_tokens,
        }

    @pytest.mark.asyncio
    async def test_noninteractive_emits_awaiting_model_before_each_request(self) -> None:
        # Requirement: every non-interactive model request exposes the awaiting-model boundary.
        agent = Agent(
            TestModel(call_tools=["echo"]),
            tools=[Tool(lambda value: value, name="echo")],
            retries=0,
        )
        recorded: list[tuple[str, dict[str, Any]]] = []

        outcome = await run_with_interrupt(
            agent,
            "echo a value",
            interrupt_signal=None,
            event_callback=lambda event_type, data: recorded.append((event_type, data)),
            has_output_schema=False,
        )

        awaiting = [
            data
            for event_type, data in recorded
            if event_type == "agent_turn_start" and data["turn"] == "awaiting_model"
        ]
        assert len(awaiting) == 2
        assert outcome.result is not None
        assert outcome.result.usage.requests == 2

    @pytest.mark.asyncio
    async def test_iter_output_validation_retry_emits_parse_recovery(self) -> None:
        # Requirement: Agent.iter output correction attempts are visible to subscribers.
        output_model = output_schema_to_pydantic_model(
            "RecoveryOutput", {"answer": OutputField(type="string")}
        )
        assert output_model is not None
        agent = Agent(
            TestModel(custom_output_args={"answer": 42}),
            output_type=ToolOutput(output_model),
            retries=AgentRetries(tools=0, output=1),
        )
        recorded: list[tuple[str, dict[str, Any]]] = []

        with pytest.raises(UnexpectedModelBehavior):
            await run_with_interrupt(
                agent,
                "format this",
                interrupt_signal=asyncio.Event(),
                event_callback=lambda event_type, data: recorded.append((event_type, data)),
                has_output_schema=True,
                max_parse_recovery_attempts=1,
            )

        recovery_events = [
            data for event_type, data in recorded if event_type == "agent_parse_recovery"
        ]
        assert recovery_events == [
            {
                "attempt": 1,
                "max_attempts": 1,
                "reason": "schema",
                "error": recovery_events[0]["error"],
            }
        ]
        assert recovery_events[0]["error"]

    @pytest.mark.asyncio
    async def test_noninteractive_output_validation_retry_emits_parse_recovery(self) -> None:
        # Requirement: non-interactive output correction attempts are visible to subscribers.
        output_model = output_schema_to_pydantic_model(
            "RunRecoveryOutput", {"answer": OutputField(type="string")}
        )
        assert output_model is not None
        agent = Agent(
            TestModel(custom_output_args={"answer": 42}),
            output_type=ToolOutput(output_model),
            retries=AgentRetries(tools=0, output=1),
        )
        recorded: list[tuple[str, dict[str, Any]]] = []

        with pytest.raises(UnexpectedModelBehavior):
            await run_with_interrupt(
                agent,
                "format this",
                interrupt_signal=None,
                event_callback=lambda event_type, data: recorded.append((event_type, data)),
                has_output_schema=True,
                max_parse_recovery_attempts=1,
            )

        recovery_events = [
            data for event_type, data in recorded if event_type == "agent_parse_recovery"
        ]
        assert len(recovery_events) == 1
        assert recovery_events[0]["attempt"] == 1
        assert recovery_events[0]["max_attempts"] == 1
        assert recovery_events[0]["reason"] == "schema"
        assert recovery_events[0]["error"]


class TestHardAbort:
    """Requirement: a signal during an in-flight model call cancels the run."""

    @pytest.mark.asyncio
    async def test_interrupt_during_api_call_is_cancelled(self) -> None:
        """When the interrupt signal fires while a model request is in progress,
        the helper must cancel the in-flight model request task and return
        ``is_cancelled=True``.  This matches ``ClaudeProvider``'s hard-abort
        semantics for mid-API-call interrupts."""
        signal = asyncio.Event()
        started_event = asyncio.Event()

        class _SlowTestModel(TestModel):
            async def request(self, messages, *args, **kwargs):
                started_event.set()
                await asyncio.sleep(1)
                return await super().request(messages, *args, **kwargs)

        async def set_signal_after_model_start() -> None:
            await started_event.wait()
            signal.set()

        agent = Agent(_SlowTestModel(), output_type=str, retries=0)
        signal_task = asyncio.create_task(set_signal_after_model_start())
        try:
            outcome = await run_with_interrupt(
                agent,
                "say hello",
                interrupt_signal=signal,
                event_callback=None,
                has_output_schema=False,
            )
        finally:
            signal_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await signal_task

        assert outcome.is_cancelled is True
        assert outcome.is_partial is False
        assert outcome.result is None
        assert outcome.partial_output is None


class TestUsageLimits:
    """Requirement: usage_limits are forwarded and UsageLimitExceeded maps to a
    non-retryable ProviderError."""

    @pytest.mark.asyncio
    async def test_usage_limit_request_limit_maps_to_provider_error(self) -> None:
        """When pydantic-ai raises UsageLimitExceeded because request_limit=1 is
        exceeded by a two-request tool loop, the helper must raise a non-retryable
        ProviderError matching the legacy ClaudeProvider max-iterations message."""

        async def loop_tool() -> str:
            return "again"

        agent = Agent(
            TestModel(),
            tools=[Tool(loop_tool)],
            output_type=str,
            retries=0,
        )

        with pytest.raises(ProviderError) as exc_info:
            await run_with_interrupt(
                agent,
                "call loop",
                interrupt_signal=asyncio.Event(),
                event_callback=None,
                has_output_schema=False,
                usage_limits=UsageLimits(request_limit=1),
            )

        assert "exceeded maximum iterations (1)" in str(exc_info.value)
        assert exc_info.value.is_retryable is False


class TestMaxSessionSeconds:
    """Requirement: max_session_seconds is enforced at iteration boundaries and
    raises a non-retryable ProviderError."""

    @pytest.mark.asyncio
    async def test_max_session_seconds_expired_raises_provider_error(self) -> None:
        """When a model call takes longer than max_session_seconds, the helper must
        raise a non-retryable ProviderError at the next iteration boundary, matching
        the legacy ClaudeProvider session-timeout behavior."""

        class _SlowTestModel(TestModel):
            async def request(self, messages, *args, **kwargs):
                await asyncio.sleep(0.2)
                return await super().request(messages, *args, **kwargs)

        agent = Agent(_SlowTestModel(), output_type=str, retries=0)

        with pytest.raises(ProviderError) as exc_info:
            await run_with_interrupt(
                agent,
                "say hello",
                interrupt_signal=asyncio.Event(),
                event_callback=None,
                has_output_schema=False,
                max_session_seconds=0.05,
            )

        assert "exceeded maximum session duration" in str(exc_info.value).lower()
        assert exc_info.value.is_retryable is False


class TestLimitsDisabled:
    """Requirement: None limits preserve the existing no-limit interrupt behavior."""

    @pytest.mark.asyncio
    async def test_none_limits_completes_normally(self) -> None:
        """When usage_limits=None and max_session_seconds=None, the helper must
        complete normally without raising limit errors, matching the original
        default behavior."""
        signal = asyncio.Event()

        outcome = await run_with_interrupt(
            _make_text_agent(),
            "say hello",
            interrupt_signal=signal,
            event_callback=None,
            has_output_schema=False,
            usage_limits=None,
            max_session_seconds=None,
        )

        assert outcome.is_partial is False
        assert outcome.is_cancelled is False
        assert outcome.result is not None
        assert outcome.partial_output is None
