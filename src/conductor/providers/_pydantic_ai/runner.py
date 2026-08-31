"""Shared execution pipeline for Pydantic AI-based providers.

This module extracts the provider-agnostic execution loop from the Claude
provider into a reusable helper. It wires MCP toolsets into a caller-supplied
Pydantic AI agent factory, runs the interrupt-aware retry loop, and normalizes
the result into an :class:`~conductor.providers.base.AgentOutput`.

The underscore prefix signals that this module is an internal implementation
detail, not a public API of Conductor.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent

from conductor.config.schema import AgentDef, OutputField, ToolOutputConfig
from conductor.exceptions import ProviderError, ValidationError
from conductor.mcp.manager import MCPManager
from conductor.providers._pydantic_ai.compaction import CompactionConfig
from conductor.providers._pydantic_ai.retry import (
    RetryConfig as PydanticRetryConfig,
)
from conductor.providers.base import AgentOutput, EventCallback


def _model_name_from_pydantic_agent(
    pydantic_agent: Any,
    default_model: str,
) -> str:
    """Return a resolved model name from a Pydantic AI agent instance."""
    model = pydantic_agent.model
    if model is None:
        return default_model
    if hasattr(model, "model_name"):
        return model.model_name
    if hasattr(model, "name"):
        return model.name
    return str(model)


def _build_partial_content(
    partial_output: Any,
    output_schema: dict[str, OutputField] | None,
    agent_name: str,
) -> dict[str, Any]:
    """Build a content dict from a partial/interrupted output."""
    from conductor.executor.output import parse_json_output

    if isinstance(partial_output, BaseModel):
        return partial_output.model_dump()

    if isinstance(partial_output, str):
        if output_schema is not None:
            try:
                return parse_json_output(partial_output)
            except ValidationError:
                pass
        return {"result": partial_output}

    return {"result": partial_output}


async def run_agent_pipeline(
    *,
    agent: AgentDef,
    rendered_prompt: str,
    mcp_manager: MCPManager | None,
    tools: list[str] | None,
    tool_output_config: ToolOutputConfig,
    retry_config: PydanticRetryConfig,
    interrupt_signal: asyncio.Event | None,
    event_callback: EventCallback | None,
    max_agent_iterations: int,
    max_session_seconds: float | None,
    default_model: str,
    retry_history: list[dict[str, Any]],
    build_agent_fn: Callable[..., Agent[Any, Any]],
    compaction: CompactionConfig | None = None,
) -> AgentOutput:
    """Run the shared Pydantic AI execution pipeline.

    Constructs the MCP toolset, resolves the effective retry configuration,
    builds the Pydantic AI agent via ``build_agent_fn(toolsets)``, runs the
    interrupt-aware retry loop, and returns a normalized ``AgentOutput``.

    All Pydantic-AI seam helpers are imported inside this function so that tests
    can patch the module paths the provider historically used (e.g.
    ``conductor.providers._pydantic_ai.interrupt.run_with_interrupt``) without
    having to know the internal runner module.

    Args:
        agent: Conductor agent definition.
        rendered_prompt: Jinja2-rendered user prompt.
        mcp_manager: MCP manager for the resolved working directory, or ``None``
            if no MCP servers are configured.
        tools: Optional list of tool names available to this agent. ``None``
            grants all tools; ``[]`` grants none.
        tool_output_config: MCP tool result output-size configuration.
        retry_config: Provider-level retry defaults (merged with any per-agent
            ``retry`` policy).
        interrupt_signal: Optional event for mid-agent interrupt signaling.
        event_callback: Optional Conductor event callback for streaming events.
        max_agent_iterations: Maximum Pydantic AI requests for this run.
        max_session_seconds: Optional wall-clock cap for the session.
        default_model: Fallback model name when the agent's model cannot be
            resolved from the Pydantic AI agent instance.
        retry_history: Mutable list that receives ``agent_retry`` events.
        build_agent_fn: Callable that accepts ``toolsets`` and keyword args
            (``max_parse_recovery_attempts`` and ``compaction``) and returns a
            configured Pydantic AI ``Agent``.
        compaction: Optional resolved compaction config to pass through to
            ``build_agent_fn``.

    Returns:
        Normalized ``AgentOutput``.

    Raises:
        asyncio.CancelledError: When the run is hard-aborted via interrupt.
        ProviderError: When the agent produces no result or an unrecoverable
            provider failure occurs.
        ValidationError: When the output fails schema validation.
    """
    from pydantic_ai import UsageLimits

    from conductor.providers._pydantic_ai.interrupt import run_with_interrupt
    from conductor.providers._pydantic_ai.mcp_toolset import MCPManagerToolset
    from conductor.providers._pydantic_ai.retry import (
        _resolve_retry_config,
        execute_with_retry,
    )
    from conductor.providers._pydantic_ai.structured_output import extract_content
    from conductor.providers._pydantic_ai.usage import build_agent_output

    def intercepting_callback(event_type: str, data: dict[str, Any]) -> None:
        if event_type == "agent_retry":
            retry_history.append(data)
        if event_callback is not None:
            event_callback(event_type, data)

    toolsets: list[Any] = []
    if mcp_manager is not None:
        tool_names = None if agent.tools is None and not tools else tools
        toolsets.append(
            MCPManagerToolset(
                mcp_manager,
                tool_names,
                tool_output_config,
                event_callback=event_callback,
            )
        )

    retry_cfg = _resolve_retry_config(agent, retry_config)

    pydantic_agent = build_agent_fn(
        toolsets,
        max_parse_recovery_attempts=retry_cfg.max_parse_recovery_attempts,
        compaction=compaction,
    )

    outcome = await execute_with_retry(
        coro_factory=lambda: run_with_interrupt(
            agent=pydantic_agent,
            user_prompt=rendered_prompt,
            interrupt_signal=interrupt_signal,
            event_callback=intercepting_callback,
            has_output_schema=bool(agent.output),
            usage_limits=UsageLimits(request_limit=max_agent_iterations),
            max_session_seconds=max_session_seconds,
            max_parse_recovery_attempts=retry_cfg.max_parse_recovery_attempts,
        ),
        retry_config=retry_cfg,
        event_callback=intercepting_callback,
        agent_name=agent.name,
    )

    if outcome.is_cancelled:
        raise asyncio.CancelledError()

    model_name = _model_name_from_pydantic_agent(pydantic_agent, default_model)

    if outcome.is_partial:
        content = _build_partial_content(
            outcome.partial_output,
            agent.output,
            agent.name,
        )
        total_usage = outcome.total_usage or {}
        return AgentOutput(
            content=content,
            raw_response=outcome.partial_output,
            tokens_used=total_usage.get("total_tokens"),
            input_tokens=total_usage.get("request_tokens"),
            output_tokens=total_usage.get("response_tokens"),
            last_call_input_tokens=outcome.last_call_input_tokens,
            partial=True,
            model=model_name,
        )

    if outcome.result is None:
        raise ProviderError(
            f"Agent '{agent.name}' produced no result",
            suggestion="Check the model and prompt configuration.",
            is_retryable=False,
        )

    content = extract_content(outcome.result.output, agent.output, agent.name)
    return build_agent_output(
        content=content,
        raw_response=outcome.result,
        usage=outcome.result.usage,
        model=model_name,
        last_call_input_tokens=outcome.last_call_input_tokens,
    )
