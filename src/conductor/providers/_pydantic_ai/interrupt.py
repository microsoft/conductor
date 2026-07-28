"""Interrupt-aware runner for Pydantic AI agents.

This module provides a reusable helper that drives a Pydantic AI agent run
while honoring Conductor's ``interrupt_signal``.  It mirrors the interrupt
semantics of ``ClaudeProvider``:

1. Check the signal between agentic iterations (tool/model turns).  When it
   fires, stop the loop and request a short final model response that
   returns the best partial result.
2. Race in-flight API calls against the signal.  When the signal wins, the
   run task is cancelled (hard abort).
3. Return ``AgentOutput(partial=True)`` for partial results without running
   the output through Conductor schema validation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.run import AgentRunResult
from pydantic_graph.basenode import End

from conductor.exceptions import ProviderError
from conductor.providers._pydantic_ai.events import (
    emit_agent_turn_start,
    emit_pydantic_event,
)

logger = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], None]


_INTERRUPTION_USER_PROMPT = (
    "The user has interrupted execution. Please immediately provide "
    "your best partial result based on the work completed so far. "
    "Return whatever you have, even if incomplete."
)

_INTERRUPTION_TOOL_PROMPT = (
    "The user has interrupted execution. Please immediately call the "
    "'final_result' tool with your best partial result based on the work "
    "completed so far. Return whatever you have, even if incomplete."
)


def _session_expired_message(max_session_seconds: float) -> str:
    """Return a ProviderError message for a session timeout."""
    return f"Agent exceeded maximum session duration of {max_session_seconds:.0f}s"


def _iterations_exceeded_message(request_limit: int) -> str:
    """Return a ProviderError message for an exceeded iteration limit."""
    return f"Agentic loop exceeded maximum iterations ({request_limit})"


@dataclass
class RunOutcome:
    """Result of an interrupt-aware Pydantic AI agent run.

    Attributes:
        result: Final pydantic-ai run result when the run completed normally.
            ``None`` when a partial result was returned or the run was
            cancelled.
        partial_output: Best-effort partial content when the run was
            interrupted, or ``None`` for normal completion.
        is_partial: Whether the outcome represents a partial/interrupted
            result.
        is_cancelled: Whether the run was cancelled (hard abort).
        total_usage: Aggregated token usage across all model calls in this
            helper. May be ``None`` when no usage information is available.
    """

    result: AgentRunResult[Any] | None = None
    partial_output: Any = None
    is_partial: bool = False
    is_cancelled: bool = False
    total_usage: dict[str, Any] | None = None


def _make_interrupt_message(has_output_schema: bool) -> UserPromptPart:
    """Build the user message asking the model for a partial result."""
    content = _INTERRUPTION_TOOL_PROMPT if has_output_schema else _INTERRUPTION_USER_PROMPT
    return UserPromptPart(content=content)


def _usage_from_result(result: AgentRunResult[Any] | None) -> dict[str, Any] | None:
    """Return JSON-safe usage from a pydantic-ai result, or None."""
    if result is None or not hasattr(result, "usage"):
        return None
    usage = result.usage
    return {
        "requests": getattr(usage, "requests", None),
        "request_tokens": getattr(usage, "request_tokens", None),
        "response_tokens": getattr(usage, "response_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def _aggregate_usage(
    usages: list[dict[str, Any] | None],
) -> dict[str, int | None]:
    """Sum numeric usage fields across calls, preserving None for unknowns."""
    total: dict[str, int | None] = {
        "requests": 0,
        "request_tokens": 0,
        "response_tokens": 0,
        "total_tokens": 0,
    }
    had_any = False
    for usage in usages:
        if usage is None:
            continue
        had_any = True
        for key in total:
            value = usage.get(key)
            if value is not None:
                current = total.get(key) or 0
                total[key] = current + int(value)
            else:
                total[key] = None
    if not had_any:
        return {}
    return total


def _has_stream(node: Any) -> bool:
    """Return whether a graph node supports event streaming."""
    return hasattr(node, "stream")


async def _stream_node_events(
    agent: Agent[Any, Any],
    node: Any,
    run_context: Any,
    event_callback: EventCallback | None,
) -> None:
    """Stream a node's events through the Conductor callback."""
    if event_callback is None or not _has_stream(node):
        return
    async with node.stream(run_context) as stream:
        async for event in stream:
            emit_pydantic_event(agent, event, event_callback)


async def _execute_node(
    agent: Agent[Any, Any],
    run: Any,
    node: Any,
    event_callback: EventCallback | None,
) -> Any:
    """Execute a single graph node and emit its streaming events.

    Pydantic AI exposes streaming events on nodes that have a ``stream``
    method (``ModelRequestNode`` and ``CallToolsNode``).  We consume the
    stream to forward Conductor events, then advance the graph with
    ``run.next(node)`` to obtain the following node.
    """
    await _stream_node_events(agent, node, run.ctx, event_callback)
    return await run.next(node)


async def run_with_interrupt(
    agent: Agent[Any, Any],
    user_prompt: str,
    *,
    interrupt_signal: asyncio.Event | None,
    event_callback: EventCallback | None,
    has_output_schema: bool,
    usage_limits: UsageLimits | None = None,
    max_session_seconds: float | None = None,
) -> RunOutcome:
    """Run a Pydantic AI agent with Conductor interrupt support.

    The helper drives the agent node-by-node so it can check
    ``interrupt_signal`` between model/tool turns.  When the signal is set
    at a boundary, it exits the current run, appends a user message asking
    for the best partial result, and starts a fresh run with the captured
    message history.  When the signal wins a race against an in-flight
    node execution, the task is cancelled and ``is_cancelled=True`` is
    returned.

    Args:
        agent: The configured Pydantic AI agent.
        user_prompt: The rendered user prompt for this agent step.
        interrupt_signal: Optional asyncio event that requests a graceful
            interrupt.  When ``None``, the helper behaves like a normal run.
        event_callback: Optional Conductor event callback for streaming
            events.
        has_output_schema: Whether the agent was built with a structured
            output schema.  Affects the wording of the interrupt prompt.
        usage_limits: Optional pydantic-ai usage limits forwarded to
            ``agent.iter`` / ``agent.run``.  ``UsageLimits(request_limit=N)``
            caps model requests; when exceeded pydantic-ai raises
            ``UsageLimitExceeded``, which is mapped to a non-retryable
            ``ProviderError`` matching the legacy ``ClaudeProvider``
            max-iterations behavior.
        max_session_seconds: Optional wall-clock cap for the whole session.
            Enforced at the start of each agentic iteration, matching the
            legacy ``ClaudeProvider`` loop semantics.  When exceeded, a
            non-retryable ``ProviderError`` is raised.

    Returns:
        A ``RunOutcome`` describing normal completion, partial output, or
        cancellation.
    """
    if interrupt_signal is None:
        return await _run_to_completion(agent, user_prompt, event_callback, usage_limits)

    if interrupt_signal.is_set():
        logger.info("Pydantic AI agent interrupted before first iteration")
        interrupt_signal.clear()
        return await _request_partial_output(agent, [], event_callback, has_output_schema)

    usages: list[dict[str, Any] | None] = []
    iteration = 0
    session_start = time.monotonic()

    try:
        async with agent.iter(user_prompt, usage_limits=usage_limits) as run:
            next_node = run.next_node
            while not isinstance(next_node, End):
                iteration += 1

                if max_session_seconds is not None:
                    elapsed = time.monotonic() - session_start
                    if elapsed > max_session_seconds:
                        raise ProviderError(
                            _session_expired_message(max_session_seconds),
                            suggestion="The agent session exceeded its wall-clock limit.",
                            is_retryable=False,
                        )

                if interrupt_signal.is_set():
                    logger.info("Pydantic AI agent interrupted between iterations")
                    interrupt_signal.clear()
                    return await _request_partial_output(
                        agent, list(run.all_messages()), event_callback, has_output_schema
                    )

                emit_agent_turn_start(event_callback, iteration)

                if type(next_node).__name__ == "ModelRequestNode":
                    node_task = asyncio.create_task(
                        _execute_node(agent, run, next_node, event_callback)
                    )
                    signal_task = asyncio.create_task(interrupt_signal.wait())
                    finished: set[asyncio.Task[Any]]
                    try:
                        finished, _ = await asyncio.wait(
                            {node_task, signal_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    except BaseException:
                        node_task.cancel()
                        signal_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await node_task
                        with contextlib.suppress(asyncio.CancelledError):
                            await signal_task
                        raise

                    if signal_task in finished:
                        node_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await node_task
                        interrupt_signal.clear()
                        logger.info("Pydantic AI agent interrupted during model API call")
                        return RunOutcome(is_cancelled=True)

                    signal_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await signal_task

                    try:
                        next_node = await node_task
                    except asyncio.CancelledError:
                        raise
                else:
                    next_node = await _execute_node(agent, run, next_node, event_callback)

                usages.append(_usage_from_result(run.result))

    except UsageLimitExceeded as exc:
        request_limit = usage_limits.request_limit if usage_limits is not None else None
        raise ProviderError(
            _iterations_exceeded_message(request_limit)
            if request_limit is not None
            else f"Agent exceeded usage limits: {exc}",
            suggestion="The agent may be stuck in a tool-use loop. Check your MCP tools.",
            is_retryable=False,
        ) from exc

    final_result = run.result
    if final_result is None:
        raise RuntimeError("Pydantic AI run ended without a result")
    usages.append(_usage_from_result(final_result))
    return RunOutcome(result=final_result, total_usage=_aggregate_usage(usages))


async def _run_to_completion(
    agent: Agent[Any, Any],
    user_prompt: str,
    event_callback: EventCallback | None,
    usage_limits: UsageLimits | None = None,
) -> RunOutcome:
    """Run the agent normally without any interrupt handling."""
    emit_agent_turn_start(event_callback, "awaiting_model")
    try:
        result = await agent.run(user_prompt, usage_limits=usage_limits)
    except UsageLimitExceeded as exc:
        request_limit = usage_limits.request_limit if usage_limits is not None else None
        raise ProviderError(
            _iterations_exceeded_message(request_limit)
            if request_limit is not None
            else f"Agent exceeded usage limits: {exc}",
            suggestion="The agent may be stuck in a tool-use loop. Check your MCP tools.",
            is_retryable=False,
        ) from exc
    return RunOutcome(result=result, total_usage=_usage_from_result(result))


async def _request_partial_output(
    agent: Agent[Any, Any],
    history: list[ModelMessage],
    event_callback: EventCallback | None,
    has_output_schema: bool,
) -> RunOutcome:
    """Run one final model call asking for the best partial result.

    This performs a hard cut: tools are not available on this run so the model
    must answer directly.  The returned content is marked partial and is not
    validated against the Conductor output schema.
    """
    emit_agent_turn_start(event_callback, "awaiting_model")
    interrupt_message = _make_interrupt_message(has_output_schema)
    partial_history = list(history)
    partial_history.append(ModelRequest(parts=[interrupt_message]))
    result = await agent.run(user_prompt=None, message_history=partial_history)
    return RunOutcome(
        partial_output=result.output,
        is_partial=True,
        total_usage=_usage_from_result(result),
    )
