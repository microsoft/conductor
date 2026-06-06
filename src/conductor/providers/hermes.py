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
from conductor.providers.reasoning import ReasoningEffort, resolve_reasoning_effort

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

# Maximum number of recovery attempts when JSON parsing fails.
_MAX_PARSE_RECOVERY_ATTEMPTS = 3

# Maximum schema nesting depth for prompt schema generation.
_MAX_SCHEMA_DEPTH = 10


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
        reasoning_effort=("low", "medium", "high", "xhigh"),
        structured_output="prompt_injection",
        interrupt=True,
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
        hermes_home: str | None = None,
        hermes_toolsets: list[str] | None = None,
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
            hermes_home: Path to a Hermes home directory (profile). When
                set, hermes loads config/soul/memory from this path instead
                of ``~/.hermes``. Thread-safe via ``ContextVar`` override.
            hermes_toolsets: Hermes toolset names to enable (e.g.
                ``["filesystem", "web"]``). None = hermes defaults (all
                available toolsets); empty list = no tools.
            max_agent_iterations: Maximum tool-calling iterations per agent
                execution. Maps to hermes ``max_iterations``. Defaults to
                90 (hermes default) when None.
            max_session_seconds: Maximum wall-clock duration for agent sessions.
                Not directly supported by the hermes library — used only to
                impose an ``asyncio.wait_for`` timeout around each call.
            default_reasoning_effort: Workflow-wide default reasoning effort.
                Forwarded to hermes via ``reasoning_config``.
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
        self._hermes_home = hermes_home
        self._hermes_toolsets = hermes_toolsets
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
            tools: Hermes toolset names to enable (e.g. ["filesystem", "web"]).
                None = hermes defaults; empty list = no tools.
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

        # Append schema instruction when agent declares a structured output schema
        prompt = rendered_prompt
        schema_for_prompt: dict[str, Any] | None = None
        if agent.output:
            schema_for_prompt = _build_prompt_schema(agent.output)
            schema_desc = json.dumps(schema_for_prompt, indent=2)
            prompt += (
                f"\n\n**IMPORTANT: You MUST respond with a JSON object matching this schema:**\n"
                f"```json\n{schema_desc}\n```\n"
                f"Return ONLY the JSON object, no other text."
            )

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
            # AIAgent doesn't accept temperature directly; route through
            # request_overrides which is applied at the transport layer.
            agent_kwargs.setdefault("request_overrides", {})["temperature"] = (
                self._default_temperature
            )
        if self._base_url:
            agent_kwargs["base_url"] = self._base_url
        if self._api_key:
            agent_kwargs["api_key"] = self._api_key

        # Resolve reasoning effort (per-agent override → workflow default)
        effort = resolve_reasoning_effort(agent, self._default_reasoning_effort)
        if effort is not None:
            agent_kwargs["reasoning_config"] = {"effort": effort}

        # Resolve enabled_toolsets. Conductor's per-agent tools: field
        # contains workflow tool names (not Hermes toolset names), so a
        # non-empty list cannot be forwarded. Provider-level hermes_toolsets
        # gives authors the knob to restrict which Hermes toolsets are active.
        if tools:
            raise ProviderError(
                f"Agent '{agent.name}' declares tools={tools!r}, but "
                "the Hermes provider does not support per-agent workflow tool "
                "allowlists (workflow tool names do not translate to Hermes "
                "toolset names).",
                suggestion=(
                    "Remove the 'tools:' field to use Hermes default toolsets, "
                    "set 'tools: []' to disable all tools, or configure "
                    "'hermes_toolsets' in the provider settings to restrict "
                    "which Hermes toolsets are available."
                ),
            )
        elif tools is not None:
            # Explicit empty list = no tools
            agent_kwargs["enabled_toolsets"] = []
        elif self._hermes_toolsets is not None:
            # Provider-level toolset restriction
            agent_kwargs["enabled_toolsets"] = self._hermes_toolsets

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

        # Keep a reference to the AIAgent so we can call interrupt() from
        # the async side — hermes interrupt() is thread-safe by design.
        hermes_agent_ref: list[Any] = []

        def _run_sync() -> dict[str, Any]:
            # Apply hermes_home profile override (thread-safe ContextVar)
            _home_token = None
            if self._hermes_home:
                from hermes_constants import set_hermes_home_override
                _home_token = set_hermes_home_override(self._hermes_home)
            try:
                hermes_agent = AIAgent(**agent_kwargs)
                hermes_agent_ref.append(hermes_agent)
                return hermes_agent.run_conversation(
                    prompt,
                    system_message=agent.system_prompt or None,
                    conversation_history=conversation_history,
                )
            finally:
                if _home_token is not None:
                    from hermes_constants import reset_hermes_home_override
                    reset_hermes_home_override(_home_token)

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
            # Either timeout or interrupt fired first. Signal hermes to stop
            # cooperatively — it checks _interrupt_requested between iterations.
            if hermes_agent_ref:
                hermes_agent_ref[0].interrupt()
            call_task.cancel()
            if interrupt_task is not None and interrupt_task in done:
                raise ProviderError(
                    f"Agent '{agent.name}' was interrupted by user request",
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

        # If no output schema, wrap as plain text and we're done
        if not agent.output:
            content: dict[str, Any] = {"text": final_response}
        else:
            # Try to parse as JSON with recovery loop (mirrors Copilot pattern)
            content = self._parse_with_recovery(
                final_response,
                result.get("messages", []),
                schema_for_prompt,  # type: ignore[arg-type]
                agent_kwargs,
                agent,
                conversation_history,
            )

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
    # Structured output: parse with recovery (mirrors Copilot pattern)
    # ------------------------------------------------------------------

    def _parse_with_recovery(
        self,
        response: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        agent_kwargs: dict[str, Any],
        agent: AgentDef,
        conversation_history: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Parse response as JSON, retrying via conversation if parsing fails."""
        last_error: str | None = None

        for attempt in range(_MAX_PARSE_RECOVERY_ATTEMPTS + 1):
            try:
                content = parse_json_output(response)
                validate_output(content, agent.output)  # type: ignore[arg-type]
                return content
            except (json.JSONDecodeError, ValueError, ValidationError) as e:
                last_error = str(e)
                if attempt >= _MAX_PARSE_RECOVERY_ATTEMPTS:
                    break

                logger.info(
                    "Agent '%s' parse recovery attempt %d/%d: %s",
                    agent.name, attempt + 1, _MAX_PARSE_RECOVERY_ATTEMPTS, last_error,
                )

                # Build recovery prompt and re-run with conversation history
                recovery_prompt = _build_recovery_prompt(last_error, response, schema)
                # Use the messages from the failed run as history
                history = messages if messages else conversation_history

                recovery_kwargs = {k: v for k, v in agent_kwargs.items()
                                   if k not in ("stream_delta_callback", "reasoning_callback")}
                hermes_agent = AIAgent(**recovery_kwargs)
                recovery_result = hermes_agent.run_conversation(
                    recovery_prompt,
                    system_message=agent.system_prompt or None,
                    conversation_history=history,
                )
                response = recovery_result.get("final_response") or ""
                messages = recovery_result.get("messages", [])

        expected_fields = list(agent.output.keys()) if agent.output else []  # type: ignore[union-attr]
        raise ProviderError(
            f"Failed to parse structured output after {_MAX_PARSE_RECOVERY_ATTEMPTS} "
            f"recovery attempts: {last_error}",
            suggestion=(
                f"Agent was expected to return JSON with fields: {expected_fields}. "
                "Consider simplifying the output schema or making the prompt more explicit."
            ),
        )

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


def _build_prompt_schema(
    schema: dict[str, Any], depth: int = 0
) -> dict[str, Any]:
    """Build a prompt-facing schema description from OutputField definitions."""
    if depth > _MAX_SCHEMA_DEPTH:
        raise ValidationError(
            f"Schema nesting depth exceeds maximum of {_MAX_SCHEMA_DEPTH} levels",
            suggestion="Simplify your output schema to reduce nesting depth",
        )
    result: dict[str, Any] = {}
    for field_name, field_def in schema.items():
        field_schema: dict[str, Any] = {"type": field_def.type}
        if field_def.description:
            field_schema["description"] = field_def.description
        else:
            field_schema["description"] = f"The {field_name} field"
        if field_def.type == "object" and field_def.properties:
            field_schema["properties"] = _build_prompt_schema(field_def.properties, depth + 1)
            field_schema["required"] = list(field_def.properties.keys())
        if field_def.type == "array" and field_def.items:
            item_schema: dict[str, Any] = {"type": field_def.items.type}
            if field_def.items.description:
                item_schema["description"] = field_def.items.description
            if field_def.items.type == "object" and field_def.items.properties:
                item_schema["properties"] = _build_prompt_schema(
                    field_def.items.properties, depth + 1
                )
            field_schema["items"] = item_schema
        result[field_name] = field_schema
    return result


def _build_recovery_prompt(
    parse_error: str, original_response: str, schema: dict[str, Any]
) -> str:
    """Build a prompt to recover from JSON parse failures."""
    truncated = original_response[:500]
    if len(original_response) > 500:
        truncated += "..."
    schema_desc = json.dumps(schema, indent=2)
    return (
        f"Your previous response could not be parsed as valid JSON.\n\n"
        f"**Parse Error:** {parse_error}\n\n"
        f"**Your response started with:**\n```\n{truncated}\n```\n\n"
        f"**Expected JSON schema:**\n```json\n{schema_desc}\n```\n\n"
        f"Please respond with ONLY a valid JSON object matching the schema above. "
        f"Do NOT include markdown code blocks, explanatory text, or anything other "
        f"than the raw JSON object."
    )
