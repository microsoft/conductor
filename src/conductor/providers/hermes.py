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
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from conductor.exceptions import ProviderError, ValidationError
from conductor.executor.output import parse_json_output, validate_output
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback
from conductor.providers.capabilities import ProviderCapabilities
from conductor.providers.reasoning import ReasoningEffort

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef

# The hermes-agent package ships its public API under the top-level module
# name "run_agent" (not "hermes_agent"). Catch only ModuleNotFoundError so
# that real dependency failures inside the package (e.g. missing openai)
# propagate instead of producing a misleading "install hermes-agent" hint.
try:
    from run_agent import AIAgent

    HERMES_SDK_AVAILABLE = True
except ModuleNotFoundError as _e:
    if _e.name is not None and _e.name.split(".")[0] == "run_agent":
        HERMES_SDK_AVAILABLE = False
        AIAgent = None  # type: ignore[misc, assignment]
    else:
        raise

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
        streaming_events=True,
        agent_reasoning_events=True,
        reasoning_effort=None,
        structured_output="prompt_injection",
        interrupt=False,
        max_session_seconds=True,
        checkpoint_resume=True,
        usage_tracking=True,
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
        self._default_reasoning_effort = default_reasoning_effort

        # Session state for checkpoint resume. Maps agent name → path to a
        # JSON file containing the conversation history from the last run.
        self._session_ids: dict[str, str] = {}
        self._resume_session_ids: dict[str, str] = {}
        self._session_dir = Path(tempfile.gettempdir()) / "conductor" / "hermes-sessions"

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

        # Wire streaming callbacks so events fire incrementally from the
        # executor thread. The _fire helper is thread-safe (swallows errors).
        if event_callback is not None:
            agent_kwargs["stream_delta_callback"] = lambda text: _fire(
                event_callback, "agent_message", {"content": text}
            )
            agent_kwargs["reasoning_callback"] = lambda text: _fire(
                event_callback, "agent_reasoning", {"content": text}
            )

        # Load conversation history from a prior checkpoint if available
        conversation_history: list[dict[str, Any]] | None = None
        resume_path = self._resume_session_ids.get(agent.name)
        if resume_path:
            try:
                conversation_history = json.loads(Path(resume_path).read_text())
                logger.info(
                    "Resuming agent '%s' with %d prior messages",
                    agent.name, len(conversation_history),
                )
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(
                    "Could not load conversation history for '%s' from %s: %s",
                    agent.name, resume_path, e,
                )

        loop = asyncio.get_running_loop()

        def _run_sync() -> dict[str, Any]:
            hermes_agent = AIAgent(**agent_kwargs)
            return hermes_agent.run_conversation(
                prompt,
                system_message=agent.system_prompt or None,
                conversation_history=conversation_history,
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

        # Parse structured output or wrap as plain text
        if agent.output:
            content = parse_json_output(final_response)
            validate_output(content, agent.output)
        else:
            content = {"text": final_response}

        # Populate token counts from the result dict when available.
        # Use explicit None-check (not `or`) so legitimate zero is preserved.
        input_tokens: int | None = result.get("input_tokens")
        if input_tokens is None:
            input_tokens = result.get("prompt_tokens")
        output_tokens: int | None = result.get("output_tokens")
        if output_tokens is None:
            output_tokens = result.get("completion_tokens")
        tokens_used: int | None = result.get("total_tokens")
        if tokens_used is None and input_tokens is not None and output_tokens is not None:
            tokens_used = input_tokens + output_tokens

        # Use the actual model reported by hermes (may differ from requested)
        actual_model = result.get("model") or resolved_model

        # Persist conversation history for checkpoint resume
        messages = result.get("messages")
        if messages:
            self._save_session(agent.name, messages)

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
        """Confirms the hermes-agent SDK is importable.

        The import is performed at module load and reflected in the
        module-level ``HERMES_SDK_AVAILABLE`` constant; this method
        simply returns that value. Does NOT verify the configured
        ``base_url``/``api_key`` are reachable — credential and endpoint
        failures surface only at first agent execution.
        """
        return HERMES_SDK_AVAILABLE

    async def close(self) -> None:
        """No-op — the hermes provider is stateless (no persistent sessions)."""

    # ------------------------------------------------------------------
    # Session state for checkpoint resume
    # ------------------------------------------------------------------

    def _save_session(self, agent_name: str, messages: list[dict[str, Any]]) -> None:
        """Persist conversation history to a temp file for checkpoint resume."""
        self._session_dir.mkdir(parents=True, exist_ok=True)
        session_file = self._session_dir / f"{agent_name}.json"
        try:
            session_file.write_text(json.dumps(messages, ensure_ascii=False))
            self._session_ids[agent_name] = str(session_file)
        except OSError as e:
            logger.warning("Failed to save hermes session for '%s': %s", agent_name, e)

    def get_session_ids(self) -> dict[str, str]:
        """Return mapping of agent names to session file paths."""
        return self._session_ids.copy()

    def set_resume_session_ids(self, ids: dict[str, str]) -> None:
        """Set session file paths for resuming conversations on next execution."""
        self._resume_session_ids = dict(ids)

    def cleanup_sessions(self) -> None:
        """Remove all saved session files."""
        for path_str in self._session_ids.values():
            try:
                Path(path_str).unlink(missing_ok=True)
            except OSError:
                pass
        self._session_ids.clear()
        if self._session_dir.exists():
            try:
                # Remove dir only if empty (don't nuke other providers' files)
                self._session_dir.rmdir()
            except OSError:
                pass


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
