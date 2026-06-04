"""Hermes Agent provider implementation.

This module provides the HermesProvider class for executing agents
using the hermes-agent Python library (NousResearch/hermes-agent).

The library is an optional dependency — install with:
    pip install hermes-agent

Error Handling Strategy:
- ValidationError: Invalid inputs or output schema violations.
- ProviderError: Library failures, API errors, or unexpected result states.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from conductor.exceptions import ProviderError, ValidationError
from conductor.executor.output import parse_json_output, validate_output
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback
from conductor.providers.capabilities import ProviderCapabilities
from conductor.providers.reasoning import ReasoningEffort

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef

# Try to import the hermes-agent SDK
try:
    from run_agent import AIAgent

    HERMES_SDK_AVAILABLE = True
except ImportError:
    HERMES_SDK_AVAILABLE = False
    AIAgent = None  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

# JSON instruction appended to the prompt when a structured output schema is declared.
_JSON_INSTRUCTION = (
    "\n\nRespond ONLY with a valid JSON object. "
    "Do not include any explanation, markdown, or text outside the JSON object."
)


class HermesProvider(AgentProvider):
    """Hermes Agent SDK provider.

    Translates Conductor agent definitions into hermes-agent library calls and
    normalizes responses into AgentOutput format.

    Requires the hermes-agent package:
        pip install hermes-agent

    Example:
        >>> provider = HermesProvider()
        >>> await provider.validate_connection()
        True
        >>> await provider.close()
    """

    CAPABILITIES = ProviderCapabilities(
        tier="experimental",
        mcp_tools=False,
        workflow_tools_passthrough=False,
        streaming_events=False,
        agent_reasoning_events=False,
        reasoning_effort=None,
        structured_output="prompt_injection",
        interrupt=False,
        max_session_seconds=True,
        checkpoint_resume=False,
        usage_tracking=False,
        concurrent_safe=True,
        upstream_pin="hermes-agent",
        maintainer="(community contribution)",
    )


    def __init__(
        self,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        max_agent_iterations: int | None = None,
        max_session_seconds: float | None = None,
        default_reasoning_effort: ReasoningEffort | None = None,
    ) -> None:
        """Initialize the Hermes provider.

        Args:
            model: Default model in hermes/OpenRouter format, e.g.
                ``"anthropic/claude-sonnet-4"`` or ``"openai/gpt-4o"``.
                If None, uses whatever model hermes has configured.
            max_tokens: Maximum output tokens forwarded to ``AIAgent``.
            temperature: Sampling temperature forwarded to ``AIAgent``.
            base_url: Override endpoint base URL, e.g. OpenRouter.
                Forwarded to ``AIAgent`` when set.
            api_key: API key for the endpoint. Forwarded to ``AIAgent``
                when set. Use ``${ENV_VAR}`` interpolation in YAML so the
                literal value never appears in event logs.
            max_agent_iterations: Maximum tool-calling iterations per agent
                execution. Maps to hermes ``max_iterations``. Defaults to
                90 (hermes default) when None.
            max_session_seconds: Maximum wall-clock duration for agent sessions.
                Not directly supported by the hermes library — used only to
                impose an ``asyncio.wait_for`` timeout around each call.
            default_reasoning_effort: Workflow-wide default reasoning effort.
                Hermes controls reasoning internally per-model; this parameter
                is accepted for interface parity but has no effect.
        """
        if not HERMES_SDK_AVAILABLE:
            raise ProviderError(
                "Hermes provider requires the hermes-agent package",
                suggestion="Install with: pip install hermes-agent",
            )

        self._default_model = model
        self._default_max_tokens = max_tokens
        self._default_temperature = temperature
        self._base_url = base_url
        self._api_key = api_key
        self._default_max_agent_iterations = max_agent_iterations
        self._default_max_session_seconds = max_session_seconds

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
    ) -> AgentOutput:
        """Execute an agent via the hermes-agent library.

        Args:
            agent: Agent definition from workflow config.
            context: Accumulated workflow context (not passed to hermes directly;
                context is already rendered into ``rendered_prompt``).
            rendered_prompt: Jinja2-rendered user prompt.
            tools: Unused — hermes manages its own toolsets independently.
            interrupt_signal: Optional event that, when set, signals a
                mid-agent interrupt request. Monitored during execution;
                when fired the current library call is cancelled and a
                ProviderError is raised.
            event_callback: Optional callback for streaming events upstream.
                Emits ``agent_turn_start`` (before call) and ``agent_message``
                (after response) events.

        Returns:
            Normalized AgentOutput with structured content.

        Raises:
            ProviderError: If the hermes library call fails or returns an
                error state.
            ValidationError: If output doesn't match the declared schema.
        """
        # Resolve per-agent overrides
        resolved_model = agent.model or self._default_model
        resolved_max_iter = (
            agent.max_agent_iterations
            if agent.max_agent_iterations is not None
            else self._default_max_agent_iterations
        )
        resolved_timeout = (
            agent.max_session_seconds
            if agent.max_session_seconds is not None
            else self._default_max_session_seconds
        )

        # Append JSON instruction when agent declares a structured output schema
        prompt = rendered_prompt
        if agent.output:
            prompt = rendered_prompt + _JSON_INSTRUCTION

        _fire(event_callback, "agent_turn_start", {"turn": "awaiting_model"})

        # Build AIAgent kwargs — omit model when not set to use hermes default
        agent_kwargs: dict[str, Any] = {
            "quiet_mode": True,
            "skip_context_files": True,
            "skip_memory": True,
        }
        if resolved_model:
            agent_kwargs["model"] = resolved_model
        if resolved_max_iter is not None:
            agent_kwargs["max_iterations"] = resolved_max_iter
        if self._default_max_tokens is not None:
            agent_kwargs["max_tokens"] = self._default_max_tokens
        if self._default_temperature is not None:
            agent_kwargs["temperature"] = self._default_temperature
        if self._base_url:
            agent_kwargs["base_url"] = self._base_url
        if self._api_key:
            agent_kwargs["api_key"] = self._api_key
        # Conductor's per-agent tools: allowlist is NOT forwarded to hermes.
        # The two vocabularies are incompatible (Conductor MCP tool names vs
        # hermes-internal toolset names). workflow_tools_passthrough=False in
        # CAPABILITIES ensures the validator rejects any agent that sets
        # tools: against this provider.

        loop = asyncio.get_event_loop()

        def _run_sync() -> dict[str, Any]:
            hermes_agent = AIAgent(**agent_kwargs)
            return hermes_agent.run_conversation(
                prompt, system_message=agent.system_prompt or None
            )

        # Wrap the blocking call and optionally race against interrupt / timeout
        call_task = loop.run_in_executor(None, _run_sync)

        awaitables: list[Any] = [call_task]
        interrupt_task = None
        if interrupt_signal is not None:
            interrupt_task = asyncio.ensure_future(_wait_for_event(interrupt_signal))
            awaitables.append(interrupt_task)

        try:
            if resolved_timeout is not None:
                done, pending = await asyncio.wait(
                    awaitables,
                    timeout=resolved_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                done, pending = await asyncio.wait(
                    awaitables,
                    return_when=asyncio.FIRST_COMPLETED,
                )
        finally:
            # Clean up the interrupt watcher regardless of outcome
            if interrupt_task is not None and not interrupt_task.done():
                interrupt_task.cancel()

        if call_task not in done:
            # Either timeout or interrupt fired first.
            # Note: call_task.cancel() on a run_in_executor future cannot
            # actually stop the thread — the hermes call continues to natural
            # completion in the background.
            call_task.cancel()
            logger.warning(
                "Agent '%s' hermes executor thread cannot be stopped; "
                "it will continue running in the background until natural completion.",
                agent.name,
            )
            if interrupt_task is not None and interrupt_task in done:
                raise ProviderError(
                    f"Agent '{agent.name}' was interrupted; underlying hermes call "
                    f"may continue in the background until natural completion.",
                    is_retryable=False,
                )
            raise ProviderError(
                f"Agent '{agent.name}' exceeded maximum session duration "
                f"of {resolved_timeout:.0f}s",
                is_retryable=True,
            )

        try:
            result = call_task.result()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise ProviderError(
                f"Hermes agent execution failed (model='{resolved_model}'): {e}",
                suggestion="Check the hermes-agent logs and verify base_url/api_key.",
            ) from e

        # Surface library-level failures as ProviderError
        if result.get("failed"):
            error_msg = result.get("error") or "hermes agent run failed"
            raise ProviderError(
                f"Hermes agent execution failed (model='{resolved_model}'): {error_msg}",
            )

        final_response: str | None = result.get("final_response")
        if final_response is None:
            partial_error = result.get("error") or "no final response returned"
            raise ProviderError(
                f"Hermes agent returned no final response: {partial_error}",
            )

        _fire(event_callback, "agent_message", {"content": final_response})

        # Parse structured output or wrap as plain text
        if agent.output:
            content = parse_json_output(final_response)
            validate_output(content, agent.output)
        else:
            content = {"text": final_response}

        # Populate token counts from the result dict when available
        input_tokens: int | None = result.get("input_tokens") or result.get("prompt_tokens")
        output_tokens: int | None = result.get("output_tokens") or result.get("completion_tokens")
        tokens_used: int | None = result.get("total_tokens")
        if tokens_used is None and input_tokens is not None and output_tokens is not None:
            tokens_used = input_tokens + output_tokens

        # Use the actual model reported by hermes (may differ from requested)
        actual_model = result.get("model") or resolved_model

        return AgentOutput(
            content=content,
            raw_response={
                "final_response": final_response,
                "messages": result.get("messages", []),
                "api_calls": result.get("api_calls"),
                "completed": result.get("completed"),
                "partial": result.get("partial", False),
                "model": result.get("model"),
                "provider": result.get("provider"),
            },
            tokens_used=tokens_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=actual_model,
            partial=bool(result.get("partial", False)),
        )

    async def validate_connection(self) -> bool:
        """Verify the hermes-agent library is importable and functional.

        Performs a lightweight check by importing the library. Does not
        make any API calls — hermes uses the caller's ambient API keys.

        Returns:
            True if the hermes-agent library is available, False otherwise.
        """
        return HERMES_SDK_AVAILABLE

    async def close(self) -> None:
        """No-op — the hermes provider is stateless (no persistent sessions)."""


def _fire(callback: EventCallback | None, event: str, data: dict[str, Any]) -> None:
    """Call event_callback safely, swallowing any exception."""
    if callback is None:
        return
    try:
        callback(event, data)
    except Exception:
        logger.warning("Error in event_callback for %s", event, exc_info=True)


async def _wait_for_event(event: asyncio.Event) -> None:
    """Coroutine that resolves when the given asyncio.Event is set."""
    await event.wait()
