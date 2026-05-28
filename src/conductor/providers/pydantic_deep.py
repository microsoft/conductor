"""Pydantic-Deep provider for Conductor.

Implements PydanticDeepProvider — an AgentProvider that drives agent execution
through pydantic-deepagents (create_deep_agent + agent.run / agent.iter), in the
same pattern as CopilotProvider and ClaudeProvider.

All pydantic-deep built-in features (filesystem, memory, subagents, todos,
context-manager, skills) are DISABLED at the execute level. Conductor manages
those concerns at the workflow level. Per-agent overrides can be added via an
``agent_options:`` extension field in a future iteration.

Canonical event vocabulary emitted via ``event_callback``:
  agent_turn_start  {"turn": "awaiting_model"}  — before each model request
  agent_turn_start  {"turn": N}                  — after each response (N starts at 1)
  agent_message     {"content": "..."}           — text response parts
  agent_reasoning   {"content": "..."}           — thinking / reasoning parts
  agent_tool_start  {"tool_name": ..., "arguments": ...}
  agent_tool_complete  {"tool_name": ..., "result": ...}
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, create_model

from conductor.exceptions import ProviderError
from conductor.providers._event_format import extract_tool_result_text, format_tool_arguments
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback
from conductor.providers.reasoning import ReasoningEffort

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef, MCPServerDef, OutputField

logger = logging.getLogger(__name__)

# Optional dependency guard — pydantic-deep is not in conductor's core deps.
try:
    from pydantic_deep import create_deep_agent
    from pydantic_deep.deps import DeepAgentDeps
    from pydantic_ai_backends import StateBackend

    PYDANTIC_DEEP_AVAILABLE = True
except ImportError:
    PYDANTIC_DEEP_AVAILABLE = False
    create_deep_agent = None  # type: ignore[assignment]
    DeepAgentDeps = None  # type: ignore[assignment]
    StateBackend = None  # type: ignore[assignment]

_DEFAULT_MODEL = "anthropic:claude-sonnet-4-6"
_DEFAULT_TIMEOUT = 600.0
_MAX_RETRY_ATTEMPTS = 3
_BASE_RETRY_DELAY = 1.0
_MAX_RETRY_DELAY = 30.0
_RETRY_JITTER = 0.25


# ---------------------------------------------------------------------------
# Schema conversion: Conductor OutputField -> Pydantic BaseModel
# ---------------------------------------------------------------------------

def _conductor_type_to_python(field: OutputField) -> Any:
    """Recursively map a Conductor OutputField to a Python type annotation."""
    if field.type == "string":
        return str
    if field.type == "number":
        return float
    if field.type == "boolean":
        return bool
    if field.type == "array":
        if field.items is not None:
            inner = _conductor_type_to_python(field.items)
            return list[inner]  # type: ignore[valid-type]
        return list[Any]
    if field.type == "object":
        if field.properties:
            return _build_output_model(field.properties)
        return dict[str, Any]
    return Any


def _build_output_model(output_schema: dict[str, OutputField]) -> type[BaseModel]:
    """Convert a Conductor output schema into a dynamic Pydantic BaseModel class.

    This replaces the prompt-injection + parse-recovery loop used by
    CopilotProvider: pydantic-ai enforces the schema at the API level via a
    formal output tool definition, so no recovery loop is needed.

    Args:
        output_schema: Conductor ``dict[str, OutputField]`` from ``AgentDef.output``.

    Returns:
        A freshly created Pydantic BaseModel class with the corresponding fields.
    """
    fields: dict[str, Any] = {}
    for name, field in output_schema.items():
        python_type = _conductor_type_to_python(field)
        fields[name] = (python_type, ...)
    model: type[BaseModel] = create_model("AgentOutput", **fields)  # type: ignore[call-overload]
    return model


# ---------------------------------------------------------------------------
# MCP capability builder
# ---------------------------------------------------------------------------

def _build_mcp_capabilities(mcp_servers: dict[str, MCPServerDef]) -> list[Any]:
    """Convert Conductor MCPServerDef entries to pydantic-ai MCP capabilities.

    Supports all three types (stdio, http, sse) — a superset of ClaudeProvider
    which handles stdio only.

    Args:
        mcp_servers: Keyed mapping of MCP server definitions.

    Returns:
        List of pydantic-ai MCP capability instances.
    """
    try:
        from pydantic_ai.mcp import MCPServerHTTP, MCPServerStdio
    except ImportError:
        logger.warning("pydantic-ai MCP support not available; skipping MCP servers")
        return []

    caps: list[Any] = []
    for name, srv in mcp_servers.items():
        try:
            if srv.type == "stdio":
                if srv.command:
                    cmd_parts = [srv.command, *(srv.args or [])]
                    caps.append(MCPServerStdio(cmd_parts, env=srv.env or {}))
            elif srv.type in ("http", "sse"):
                if srv.url:
                    caps.append(MCPServerHTTP(srv.url))
            else:
                logger.warning(
                    "Unknown MCP server type %r for server %r; skipping", srv.type, name
                )
        except Exception:
            logger.warning(
                "Failed to build MCP capability for server %r", name, exc_info=True
            )
    return caps


def _resolve_model_obj(model: str) -> Any:
    """Resolve a model string to a pydantic-ai Model instance.

    Handles the ``litellm:<provider/model>`` prefix used by pydantic-deep's
    LiteLLM adapter (GitHub Copilot OAuth flow, OpenRouter, etc.).
    Plain pydantic-ai model strings (``"anthropic:..."``, ``"openai:..."``)
    are returned as-is and resolved by pydantic-ai natively.
    """
    if model.startswith("litellm:"):
        from pydantic_deep.litellm import infer_litellm_model

        return infer_litellm_model(model)
    return model


def _safe_callback(callback: EventCallback, event_type: str, data: dict[str, Any]) -> None:
    """Call event_callback, swallowing exceptions to avoid disrupting agent execution."""
    try:
        callback(event_type, data)
    except Exception:
        logger.debug("Error in event_callback for %s", event_type, exc_info=True)


# ---------------------------------------------------------------------------
# Provider implementation
# ---------------------------------------------------------------------------

class PydanticDeepProvider(AgentProvider):
    """AgentProvider that executes agents via pydantic-deepagents.

    Drives ``create_deep_agent() + agent.run()`` in the same way CopilotProvider
    drives the github-copilot-sdk and ClaudeProvider drives the Anthropic SDK.

    All built-in pydantic-deep features (filesystem tools, subagents, memory,
    todos, skills, context-manager) are disabled by default.  Conductor owns
    those concerns at the workflow level.

    Supports:
    - Any pydantic-ai model string (``"anthropic:claude-sonnet-4-6"``,
      ``"openai:gpt-4o"``, ``"github_copilot/gpt-4o"`` via LiteLLM, etc.)
    - Structured output via dynamic Pydantic model (no prompt-injection hacks)
    - Canonical Conductor event vocabulary (agent_turn_start, agent_message, ...)
    - Mid-run interrupt via ``asyncio.wait()`` race
    - Exponential-backoff retry mirroring ClaudeProvider
    - MCP connectivity via pydantic-ai native MCP capabilities
    - Reasoning effort mapping to ``thinking=`` setting

    Example:
        >>> provider = PydanticDeepProvider(model="anthropic:claude-sonnet-4-6")
        >>> output = await provider.execute(agent_def, context, "Hello")
        >>> print(output.content)
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        mcp_servers: dict[str, MCPServerDef] | None = None,
        max_agent_iterations: int | None = None,
        default_reasoning_effort: ReasoningEffort | None = None,
    ) -> None:
        """Initialize the pydantic-deep provider.

        Args:
            model: Default model in pydantic-ai format
                (e.g. ``"anthropic:claude-sonnet-4-6"``, ``"openai:gpt-4o"``,
                ``"github_copilot/gpt-4o"``). Defaults to
                ``"anthropic:claude-sonnet-4-6"``.
            temperature: Temperature for generation (0.0-1.0).
            max_tokens: Maximum output tokens.
            timeout: Per-execution timeout in seconds. Defaults to 600s.
            mcp_servers: MCP server configurations from the workflow runtime.
            max_agent_iterations: Maximum tool-use iterations (``max_turns``).
            default_reasoning_effort: Workflow-wide reasoning effort
                (maps to pydantic-deep ``thinking=`` setting).
        """
        if not PYDANTIC_DEEP_AVAILABLE:
            raise ProviderError(
                "pydantic-deep provider requires the pydantic-deep package",
                suggestion="Install with: uv add 'pydantic-deep>=0.3.14'",
            )

        self._model = model or _DEFAULT_MODEL
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._mcp_servers: dict[str, MCPServerDef] = mcp_servers or {}
        self._max_agent_iterations = max_agent_iterations
        self._default_reasoning_effort = default_reasoning_effort

    # ------------------------------------------------------------------
    # AgentProvider contract
    # ------------------------------------------------------------------

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
    ) -> AgentOutput:
        """Execute an agent via pydantic-deepagents and return normalized output.

        Args:
            agent: Agent definition from workflow config.
            context: Accumulated workflow context.
            rendered_prompt: Jinja2-rendered user prompt.
            tools: Ignored — pydantic-deep exposes its own tools via MCP.
            interrupt_signal: Optional event that triggers partial output when set.
            event_callback: Optional callback for canonical Conductor events.

        Returns:
            Normalized AgentOutput.

        Raises:
            ProviderError: If execution fails after all retry attempts.
        """
        return await self._execute_with_retry(
            agent=agent,
            context=context,
            rendered_prompt=rendered_prompt,
            interrupt_signal=interrupt_signal,
            event_callback=event_callback,
        )

    async def execute_dialog_turn(
        self,
        system_prompt: str,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        model: str | None = None,
    ) -> str:
        """Execute a single lightweight dialog turn (no toolsets, no capabilities).

        Used by the dialog evaluator and handler for conversational exchanges.
        Creates a minimal agent — no filesystem, no subagents, no capabilities.

        Args:
            system_prompt: System prompt providing dialog context.
            user_message: The latest user message.
            history: Optional prior conversation as list of
                ``{"role": "user"|"assistant", "content": "..."}`` dicts.
            model: Optional model override.

        Returns:
            The agent's text response.

        Raises:
            ProviderError: If the dialog turn fails.
        """
        resolved_model = model or self._model
        try:
            dialog_agent = create_deep_agent(
                model=_resolve_model_obj(resolved_model),
                include_filesystem=False,
                include_subagents=False,
                include_skills=False,
                include_todo=False,
                include_plan=False,
                include_memory=False,
                web_search=False,
                web_fetch=False,
                context_manager=False,
                thinking=False,
            )
            deps = DeepAgentDeps(backend=StateBackend())

            # Weave prior history into a single prompt string
            prompt = user_message
            if history:
                lines = []
                for msg in history:
                    role = msg.get("role", "user").capitalize()
                    content = msg.get("content", "")
                    lines.append(f"{role}: {content}")
                lines.append(f"User: {user_message}")
                prompt = "\n".join(lines)

            result = await asyncio.wait_for(
                dialog_agent.run(prompt, deps=deps),
                timeout=self._timeout,
            )
            output = result.output
            return output if isinstance(output, str) else str(output)
        except asyncio.TimeoutError as e:
            raise ProviderError(
                f"Dialog turn timed out after {self._timeout}s",
                suggestion="Increase timeout or simplify the dialog",
            ) from e
        except Exception as e:
            raise ProviderError(
                f"Dialog turn failed: {e}",
                suggestion="Check model credentials and network connectivity",
            ) from e

    async def validate_connection(self) -> bool:
        """Verify pydantic-deep is functional by attempting a minimal model call.

        Returns:
            True if the default model responds successfully, False otherwise.
        """
        try:
            from pydantic_ai.usage import UsageLimits

            agent = create_deep_agent(
                model=_resolve_model_obj(self._model),
                include_filesystem=False,
                include_subagents=False,
                include_skills=False,
                include_todo=False,
                include_plan=False,
                include_memory=False,
                web_search=False,
                web_fetch=False,
                context_manager=False,
                thinking=False,
            )
            deps = DeepAgentDeps(backend=StateBackend())
            await agent.run(
                "ping",
                deps=deps,
                usage_limits=UsageLimits(request_limit=1),
            )
            return True
        except Exception:
            logger.debug("PydanticDeepProvider.validate_connection failed", exc_info=True)
            return False

    async def close(self) -> None:
        """No-op — pydantic-ai agents are stateless across runs."""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_model(self, agent: AgentDef) -> str:
        """Return per-agent model override or the provider default."""
        if hasattr(agent, "model") and agent.model:
            return agent.model
        return self._model

    def _resolve_thinking(self, agent: AgentDef) -> str | bool:
        """Map Conductor reasoning effort to pydantic-deep thinking setting.

        Priority: agent.reasoning.effort > provider default_reasoning_effort > "high".
        """
        effort: ReasoningEffort | None = None
        if hasattr(agent, "reasoning") and agent.reasoning is not None:
            effort = agent.reasoning.effort
        if effort is None:
            effort = self._default_reasoning_effort
        if effort is None:
            return "high"
        return str(effort)

    def _build_agent(
        self,
        model: str,
        thinking: str | bool,
        output_type: type[BaseModel] | None = None,
    ) -> Any:
        """Create a configured deep agent with all conductor-level features off."""
        kwargs: dict[str, Any] = {
            "model": _resolve_model_obj(model),
            "include_filesystem": False,
            "include_subagents": False,
            "include_skills": False,
            "include_todo": False,
            "include_plan": False,
            "include_memory": False,
            "web_search": False,
            "web_fetch": False,
            "context_manager": False,
            "thinking": thinking,
        }
        if output_type is not None:
            kwargs["output_type"] = output_type

        model_settings: dict[str, Any] = {}
        if self._temperature is not None:
            model_settings["temperature"] = self._temperature
        if self._max_tokens is not None:
            model_settings["max_tokens"] = self._max_tokens
        if model_settings:
            kwargs["model_settings"] = model_settings

        mcp_caps = _build_mcp_capabilities(self._mcp_servers)
        if mcp_caps:
            kwargs["capabilities"] = mcp_caps

        return create_deep_agent(**kwargs)

    def _resolve_output_type(self, agent: AgentDef) -> type[BaseModel] | None:
        """Build a Pydantic output model from the agent's output schema, or None."""
        if not (hasattr(agent, "output") and agent.output):
            return None
        return _build_output_model(agent.output)

    async def _execute_with_retry(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
    ) -> AgentOutput:
        """Wrap _execute_once with exponential-backoff retry, mirroring ClaudeProvider."""
        last_error: Exception | None = None

        for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
            try:
                return await self._execute_once(
                    agent=agent,
                    context=context,
                    rendered_prompt=rendered_prompt,
                    interrupt_signal=interrupt_signal,
                    event_callback=event_callback,
                )
            except ProviderError as exc:
                if not exc.is_retryable or attempt == _MAX_RETRY_ATTEMPTS:
                    raise
                last_error = exc
            except asyncio.TimeoutError as exc:
                if attempt == _MAX_RETRY_ATTEMPTS:
                    raise ProviderError(
                        f"Agent execution timed out after {self._timeout}s "
                        f"(attempt {attempt}/{_MAX_RETRY_ATTEMPTS})",
                        suggestion="Increase timeout or simplify the task",
                        is_retryable=False,
                    ) from exc
                last_error = exc

            delay = min(_BASE_RETRY_DELAY * (2 ** (attempt - 1)), _MAX_RETRY_DELAY)
            jitter = random.uniform(0, _RETRY_JITTER * delay)
            wait = delay + jitter
            logger.warning(
                "PydanticDeepProvider attempt %d/%d failed: %s — retrying in %.1fs",
                attempt,
                _MAX_RETRY_ATTEMPTS,
                last_error,
                wait,
            )
            await asyncio.sleep(wait)

        raise ProviderError(  # pragma: no cover
            f"Agent execution failed after {_MAX_RETRY_ATTEMPTS} attempts",
            suggestion="Check model credentials and network connectivity",
        )

    async def _execute_once(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
    ) -> AgentOutput:
        """Single execution attempt (no retry logic)."""
        model = self._resolve_model(agent)
        thinking = self._resolve_thinking(agent)
        output_type = self._resolve_output_type(agent)

        max_turns = self._max_agent_iterations
        if hasattr(agent, "max_agent_iterations") and agent.max_agent_iterations is not None:
            max_turns = agent.max_agent_iterations

        pydantic_agent = self._build_agent(model, thinking, output_type)
        deps = DeepAgentDeps(backend=StateBackend())

        run_kwargs: dict[str, Any] = {}
        if max_turns is not None:
            run_kwargs["max_turns"] = max_turns

        try:
            if event_callback is not None:
                result = await self._run_with_events(
                    pydantic_agent,
                    rendered_prompt,
                    deps,
                    run_kwargs,
                    event_callback,
                    interrupt_signal,
                )
            else:
                result = await self._run_with_interrupt(
                    pydantic_agent, rendered_prompt, deps, run_kwargs, interrupt_signal
                )

            if result is None:
                # Interrupted before any result
                return AgentOutput(
                    content={"result": ""},
                    raw_response=None,
                    model=model,
                    partial=True,
                )

            usage = result.usage()
            raw_output = result.output

            # Build output content from structured result or plain string
            if output_type is not None and isinstance(raw_output, BaseModel):
                content: dict[str, Any] = raw_output.model_dump()
            elif isinstance(raw_output, dict):
                content = raw_output
            else:
                content = {"result": raw_output}

            return AgentOutput(
                content=content,
                raw_response=result,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                tokens_used=(
                    (usage.input_tokens or 0) + (usage.output_tokens or 0) if usage else None
                ),
                model=model,
            )

        except asyncio.TimeoutError:
            raise
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(
                f"Agent execution failed: {e}",
                suggestion="Check model credentials, prompt, and network connectivity",
                is_retryable=True,
            ) from e

    async def _run_with_interrupt(
        self,
        pydantic_agent: Any,
        prompt: str,
        deps: Any,
        run_kwargs: dict[str, Any],
        interrupt_signal: asyncio.Event | None,
    ) -> Any:
        """Run agent.run(), racing against the interrupt signal if provided."""
        if interrupt_signal is None:
            if self._timeout is not None:
                return await asyncio.wait_for(
                    pydantic_agent.run(prompt, deps=deps, **run_kwargs),
                    timeout=self._timeout,
                )
            return await pydantic_agent.run(prompt, deps=deps, **run_kwargs)

        coro = pydantic_agent.run(prompt, deps=deps, **run_kwargs)
        run_coro: Any = (
            asyncio.wait_for(coro, timeout=self._timeout)
            if self._timeout is not None
            else coro
        )
        run_task = asyncio.create_task(run_coro)
        interrupt_task = asyncio.create_task(interrupt_signal.wait())
        done, pending = await asyncio.wait(
            [run_task, interrupt_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        if interrupt_task in done:
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):
                pass
            return None  # caller maps to partial=True

        return run_task.result()

    async def _run_with_events(
        self,
        pydantic_agent: Any,
        prompt: str,
        deps: Any,
        run_kwargs: dict[str, Any],
        event_callback: EventCallback,
        interrupt_signal: asyncio.Event | None,
    ) -> Any:
        """Run agent.iter() streaming, emitting canonical Conductor events.

        Emits the same event vocabulary as CopilotProvider and ClaudeProvider
        so the console renderer, JSONL logger, and web dashboard work unchanged.
        """
        from pydantic_ai import Agent
        from pydantic_ai._agent_graph import End, UserPromptNode  # noqa: PLC2701
        from pydantic_ai.messages import (
            FunctionToolCallEvent,
            FunctionToolResultEvent,
            PartDeltaEvent,
            TextPartDelta,
            ThinkingPartDelta,
        )

        result_holder: list[Any] = []
        turn_counter: list[int] = [0]

        async def _run_iter() -> None:
            async with pydantic_agent.iter(prompt, deps=deps, **run_kwargs) as run:
                async for node in run:
                    if isinstance(node, UserPromptNode):
                        continue

                    elif Agent.is_model_request_node(node):
                        _safe_callback(
                            event_callback, "agent_turn_start", {"turn": "awaiting_model"}
                        )
                        async with node.stream(run.ctx) as stream:
                            thinking_buf = ""
                            text_buf = ""
                            async for event in stream:
                                if isinstance(event, PartDeltaEvent):
                                    if isinstance(event.delta, ThinkingPartDelta):
                                        thinking_buf += event.delta.content_delta or ""
                                    elif isinstance(event.delta, TextPartDelta):
                                        text_buf += event.delta.content_delta or ""
                        turn_counter[0] += 1
                        _safe_callback(
                            event_callback, "agent_turn_start", {"turn": turn_counter[0]}
                        )
                        if thinking_buf:
                            _safe_callback(
                                event_callback, "agent_reasoning", {"content": thinking_buf}
                            )
                        if text_buf:
                            _safe_callback(
                                event_callback, "agent_message", {"content": text_buf}
                            )

                    elif Agent.is_call_tools_node(node):
                        async with node.stream(run.ctx) as handle:
                            async for event in handle:
                                if isinstance(event, FunctionToolCallEvent):
                                    args = (
                                        event.part.args
                                        if isinstance(event.part.args, dict)
                                        else {"args": str(event.part.args)}
                                    )
                                    _safe_callback(
                                        event_callback,
                                        "agent_tool_start",
                                        {
                                            "tool_name": event.part.tool_name,
                                            "arguments": format_tool_arguments(args),
                                        },
                                    )
                                elif isinstance(event, FunctionToolResultEvent):
                                    raw = (
                                        event.result.content
                                        if hasattr(event.result, "content")
                                        else event.result
                                    )
                                    _safe_callback(
                                        event_callback,
                                        "agent_tool_complete",
                                        {
                                            "tool_name": getattr(
                                                event.result, "tool_name", "unknown"
                                            ),
                                            "result": extract_tool_result_text(raw),
                                        },
                                    )

                    elif isinstance(node, End):
                        pass

            assert run.result is not None
            result_holder.append(run.result)

        if interrupt_signal is None:
            if self._timeout is not None:
                await asyncio.wait_for(_run_iter(), timeout=self._timeout)
            else:
                await _run_iter()
            return result_holder[0] if result_holder else None

        run_task = asyncio.create_task(_run_iter())
        interrupt_task = asyncio.create_task(interrupt_signal.wait())
        done, pending = await asyncio.wait(
            [run_task, interrupt_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        if interrupt_task in done:
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):
                pass
            return None  # caller maps to partial=True

        run_task.result()  # propagate any exception
        return result_holder[0] if result_holder else None
