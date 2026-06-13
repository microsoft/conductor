"""OpenAI Codex SDK provider implementation."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from conductor.exceptions import ProviderError, ValidationError
from conductor.executor.output import parse_json_output, validate_output
from conductor.providers._event_format import extract_tool_result_text, format_tool_arguments
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback, match_model_id
from conductor.providers.capabilities import ProviderCapabilities
from conductor.providers.reasoning import ReasoningEffort, resolve_reasoning_effort

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef, OutputField, ProviderSettings

ApprovalMode: Any = None
AsyncCodex: Any = None
CodexConfig: Any = None
CodexReasoningEffort: Any = None
Sandbox: Any = None
_codex_is_retryable_error: Any = None

try:
    _openai_codex = importlib.import_module("openai_codex")
    _openai_codex_errors = importlib.import_module("openai_codex.errors")
    _openai_codex_types = importlib.import_module("openai_codex.types")
    ApprovalMode = _openai_codex.ApprovalMode
    AsyncCodex = _openai_codex.AsyncCodex
    CodexConfig = _openai_codex.CodexConfig
    CodexReasoningEffort = _openai_codex_types.ReasoningEffort
    Sandbox = _openai_codex.Sandbox
    _codex_is_retryable_error = _openai_codex_errors.is_retryable_error
    CODEX_SDK_AVAILABLE = True
except ImportError:
    CODEX_SDK_AVAILABLE = False

logger = logging.getLogger(__name__)

_DEFAULT_MODEL: Final[str] = "gpt-5.4"
_TOOL_RESULT_PREVIEW_LEN: Final[int] = 500
_REASONING_DELTA_METHODS: Final[frozenset[str]] = frozenset(
    {"item/reasoning/textDelta", "item/reasoning/summaryTextDelta"}
)
_ReasoningKey = tuple[str, Any, Any]


@dataclass(slots=True)
class _CodexRunResult:
    """Collected Codex turn result in Conductor-friendly form."""

    turn_id: str
    final_response: str
    streamed_response: str
    items: list[Any]
    usage: Any | None
    raw_turn: Any | None
    partial: bool = False


def _build_field_schema(field: OutputField, depth: int = 0) -> dict[str, Any]:
    if depth > 10:
        raise ProviderError("Output schema nesting exceeds 10 levels")

    schema: dict[str, Any] = {"type": field.type}
    if field.description:
        schema["description"] = field.description
    if field.type == "object" and field.properties:
        schema["properties"] = _build_properties(field.properties, depth + 1)
        schema["required"] = list(field.properties.keys())
        schema["additionalProperties"] = False
    if field.type == "array" and field.items:
        schema["items"] = _build_field_schema(field.items, depth + 1)
    return schema


def _build_properties(fields: dict[str, OutputField], depth: int = 0) -> dict[str, Any]:
    return {name: _build_field_schema(field, depth) for name, field in fields.items()}


def _build_output_schema(output: dict[str, OutputField]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": _build_properties(output),
        "required": list(output.keys()),
        "additionalProperties": False,
    }


def _safe_callback(
    event_callback: EventCallback | None,
    event_type: str,
    data: dict[str, Any],
) -> None:
    if event_callback is None:
        return
    try:
        event_callback(event_type, data)
    except Exception:
        logger.debug("Error in event_callback for %s", event_type, exc_info=True)


def _dump_model(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_dump_model(item) for item in value]
    if isinstance(value, tuple):
        return [_dump_model(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _dump_model(v) for k, v in value.items()}
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True, mode="json")
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _thread_item(item: Any) -> Any:
    return getattr(item, "root", item)


def _item_type(item: Any) -> str | None:
    return getattr(_thread_item(item), "type", None)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _codex_effort(effort: ReasoningEffort | None) -> Any:
    if effort is None or CodexReasoningEffort is None:
        return effort
    return {
        "low": CodexReasoningEffort.low,
        "medium": CodexReasoningEffort.medium,
        "high": CodexReasoningEffort.high,
        "xhigh": CodexReasoningEffort.xhigh,
    }[effort]


def _reasoning_effort_value(effort_option: Any) -> str:
    return str(_enum_value(getattr(effort_option, "reasoning_effort", effort_option)))


def _reasoning_delta_key(method: str, payload: Any) -> _ReasoningKey:
    """Group Codex reasoning deltas that belong to the same text stream."""
    item_id = getattr(payload, "item_id", None)
    if method == "item/reasoning/summaryTextDelta":
        return (method, item_id, getattr(payload, "summary_index", None))
    return (method, item_id, getattr(payload, "content_index", None))


def _tool_name_from_item(item: Any) -> str | None:
    root = _thread_item(item)
    item_type = _item_type(root)
    if item_type in {"mcpToolCall", "dynamicToolCall"}:
        return getattr(root, "tool", None)
    if item_type == "commandExecution":
        command = getattr(root, "command", None)
        return command or "shell"
    if item_type == "collabAgentToolCall":
        tool = getattr(root, "tool", None)
        return _enum_value(tool) if tool is not None else "collab_agent"
    return None


def _final_response_from_items(items: list[Any], fallback: str) -> str:
    last_unknown_phase: str | None = None
    for item in reversed(items):
        root = _thread_item(item)
        if _item_type(root) != "agentMessage":
            continue
        text = getattr(root, "text", None)
        if not isinstance(text, str):
            continue
        phase = _enum_value(getattr(root, "phase", None))
        if phase == "final_answer":
            if text.strip():
                return text
            continue
        if phase is None and text.strip() and last_unknown_phase is None:
            last_unknown_phase = text
    return last_unknown_phase or fallback


class CodexProvider(AgentProvider):
    """OpenAI Codex provider backed by ``openai-codex`` and app-server."""

    CAPABILITIES = ProviderCapabilities(
        tier="experimental",
        mcp_tools=True,
        workflow_tools_passthrough=True,
        streaming_events=True,
        agent_reasoning_events=True,
        reasoning_effort=("low", "medium", "high", "xhigh"),
        structured_output="native",
        interrupt=True,
        max_session_seconds=True,
        checkpoint_resume=True,
        usage_tracking=True,
        concurrent_safe=True,
        upstream_pin="openai-codex==0.1.0b3",
        maintainer="@microsoft/conductor",
    )

    def __init__(
        self,
        model: str | None = None,
        mcp_servers: dict[str, Any] | None = None,
        max_session_seconds: float | None = None,
        default_reasoning_effort: ReasoningEffort | None = None,
        provider_settings: ProviderSettings | None = None,
    ) -> None:
        if not CODEX_SDK_AVAILABLE:
            raise ProviderError(
                "OpenAI Codex SDK not installed",
                suggestion="Install with: uv add --prerelease allow 'openai-codex==0.1.0b3'",
            )

        codex_options = (
            provider_settings.codex
            if provider_settings is not None and provider_settings.name == "codex"
            else None
        )
        self._default_model = model or _DEFAULT_MODEL
        self._mcp_servers = mcp_servers or {}
        self._max_session_seconds = max_session_seconds
        self._default_reasoning_effort = default_reasoning_effort
        self._codex_bin = codex_options.codex_bin if codex_options else None
        self._sandbox_name = codex_options.sandbox if codex_options else None
        self._approval_mode_name = codex_options.approval_mode if codex_options else None
        self._model_provider = codex_options.model_provider if codex_options else None
        self._service_tier = codex_options.service_tier if codex_options else None
        self._session_ids: dict[str, str] = {}
        self._resume_session_ids: dict[str, str] = {}
        self._model_effort_cache: dict[str, tuple[str, ...] | None] = {}

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
    ) -> AgentOutput:
        del context
        model = agent.model or self._default_model
        effort = resolve_reasoning_effort(agent, self._default_reasoning_effort)
        output_schema = _build_output_schema(agent.output) if agent.output else None
        max_session_seconds = (
            agent.max_session_seconds
            if agent.max_session_seconds is not None
            else self._max_session_seconds
        )

        try:
            async with self._new_codex() as codex:
                if effort is not None:
                    await self._validate_reasoning_effort_for_model(codex, model, effort)
                thread = await self._start_or_resume_thread(codex, agent, model, tools)
                result = await self._run_turn(
                    thread=thread,
                    agent=agent,
                    rendered_prompt=rendered_prompt,
                    model=model,
                    effort=effort,
                    output_schema=output_schema,
                    max_session_seconds=max_session_seconds,
                    interrupt_signal=interrupt_signal,
                    event_callback=event_callback,
                )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Codex execution failed for agent '{agent.name}': {exc}",
                provider_name="codex",
                is_retryable=self._is_retryable_error(exc),
            ) from exc

        return self._build_agent_output(result, agent, model)

    async def execute_dialog_turn(
        self,
        system_prompt: str,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        model: str | None = None,
    ) -> str:
        prompt_parts: list[str] = []
        for entry in history or []:
            role = entry.get("role", "user")
            content = entry.get("content", "")
            prompt_parts.append(f"{role}: {content}")
        prompt_parts.append(f"user: {user_message}")
        prompt = "\n\n".join(prompt_parts)
        selected_model = model or self._default_model

        try:
            async with self._new_codex() as codex:
                thread = await codex.thread_start(
                    developer_instructions=system_prompt or None,
                    model=selected_model,
                    model_provider=self._model_provider,
                    cwd=os.getcwd(),
                    sandbox=self._sandbox(),
                    service_tier=self._service_tier,
                    **self._approval_kwargs(),
                )
                result = await thread.run(
                    prompt,
                    model=selected_model,
                    service_tier=self._service_tier,
                )
                return result.final_response or ""
        except Exception as exc:
            raise ProviderError(
                f"Codex dialog turn failed: {exc}",
                provider_name="codex",
                is_retryable=self._is_retryable_error(exc),
            ) from exc

    async def validate_connection(self) -> bool:
        try:
            async with self._new_codex() as codex:
                account_state = await codex.account(refresh_token=True)
                return (
                    getattr(account_state, "account", None) is not None
                    or not bool(getattr(account_state, "requires_openai_auth", False))
                )
        except Exception as exc:
            logger.debug("Codex validate_connection failed: %s", exc, exc_info=True)
            return False

    async def close(self) -> None:
        """Release provider resources.

        Codex clients are scoped to individual executions, so there is no
        persistent app-server process to close here.
        """

    def get_session_ids(self) -> dict[str, str]:
        """Return tracked Codex thread IDs by agent name."""
        return self._session_ids.copy()

    def set_resume_session_ids(self, ids: dict[str, str]) -> None:
        """Set Codex thread IDs to attempt on future executions."""
        self._resume_session_ids = dict(ids)

    def _new_codex(self) -> Any:
        if AsyncCodex is None or CodexConfig is None:
            raise ProviderError(
                "OpenAI Codex SDK not installed",
                suggestion="Install with: uv add --prerelease allow 'openai-codex==0.1.0b3'",
            )
        config = CodexConfig(
            codex_bin=self._codex_bin,
            cwd=os.getcwd(),
            client_name="conductor",
            client_title="Conductor",
        )
        return AsyncCodex(config)

    async def _start_or_resume_thread(
        self,
        codex: Any,
        agent: AgentDef,
        model: str,
        tools: list[str] | None,
    ) -> Any:
        kwargs = self._thread_kwargs(agent, model, tools)
        resume_thread_id = self._resume_session_ids.get(agent.name)
        if resume_thread_id:
            try:
                thread = await codex.thread_resume(resume_thread_id, **kwargs)
                logger.info("Resumed Codex thread %s for agent '%s'", resume_thread_id, agent.name)
                self._session_ids[agent.name] = thread.id
                return thread
            except Exception as exc:
                logger.warning(
                    "Could not resume Codex thread %s for agent '%s': %s. "
                    "Falling back to a new thread.",
                    resume_thread_id,
                    agent.name,
                    exc,
                )

        thread = await codex.thread_start(ephemeral=False, **kwargs)
        self._session_ids[agent.name] = thread.id
        return thread

    def _thread_kwargs(
        self,
        agent: AgentDef,
        model: str,
        tools: list[str] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "developer_instructions": agent.system_prompt,
            "model": model,
            "model_provider": self._model_provider,
            "cwd": os.getcwd(),
            "sandbox": self._sandbox(),
            "service_tier": self._service_tier,
            "config": self._codex_config_for_agent(agent, tools),
        }
        kwargs.update(self._approval_kwargs())
        return {key: value for key, value in kwargs.items() if value is not None}

    def _approval_kwargs(self) -> dict[str, Any]:
        if self._approval_mode_name is None or ApprovalMode is None:
            return {}
        return {
            "approval_mode": {
                "deny_all": ApprovalMode.deny_all,
                "auto_review": ApprovalMode.auto_review,
            }[self._approval_mode_name]
        }

    def _sandbox(self) -> Any:
        if self._sandbox_name is None or Sandbox is None:
            return None
        return {
            "read-only": Sandbox.read_only,
            "workspace-write": Sandbox.workspace_write,
            "full-access": Sandbox.full_access,
        }[self._sandbox_name]

    def _codex_config_for_agent(
        self,
        agent: AgentDef,
        tools: list[str] | None,
    ) -> dict[str, Any] | None:
        if not self._mcp_servers:
            return None

        mcp_servers: dict[str, Any] = {}
        for server_name, server_config in self._mcp_servers.items():
            codex_server = self._codex_mcp_server_config(server_config)
            enabled_tools = self._enabled_tools_for_server(server_name, server_config, agent, tools)
            if enabled_tools is not None:
                codex_server["enabled_tools"] = enabled_tools
            if agent.tools == []:
                codex_server["enabled"] = False
            mcp_servers[server_name] = codex_server

        return {"mcp_servers": mcp_servers}

    def _codex_mcp_server_config(self, server_config: dict[str, Any]) -> dict[str, Any]:
        server_type = server_config.get("type", "stdio")
        if server_type == "stdio":
            codex_server: dict[str, Any] = {
                "command": server_config.get("command"),
                "args": server_config.get("args", []),
            }
            if server_config.get("env"):
                codex_server["env"] = server_config["env"]
        else:
            codex_server = {"url": server_config.get("url")}
            if server_config.get("headers"):
                codex_server["http_headers"] = server_config["headers"]

        timeout_ms = server_config.get("timeout")
        if timeout_ms:
            timeout_sec = max(1, int(round(float(timeout_ms) / 1000.0)))
            codex_server["startup_timeout_sec"] = timeout_sec
            codex_server["tool_timeout_sec"] = timeout_sec
        return {key: value for key, value in codex_server.items() if value is not None}

    def _enabled_tools_for_server(
        self,
        server_name: str,
        server_config: dict[str, Any],
        agent: AgentDef,
        tools: list[str] | None,
    ) -> list[str] | None:
        server_tools = server_config.get("tools") or ["*"]
        server_allows_all = "*" in server_tools

        if agent.tools == []:
            return []
        if agent.tools is None:
            return None if server_allows_all else list(server_tools)

        requested = tools or []
        if "*" in requested:
            return None if server_allows_all else list(server_tools)

        enabled: list[str] = []
        prefix = f"{server_name}__"
        for tool_name in requested:
            if tool_name.startswith(prefix):
                enabled.append(tool_name[len(prefix) :])
            elif "__" not in tool_name:
                enabled.append(tool_name)

        if not server_allows_all:
            allowed = set(server_tools)
            enabled = [name for name in enabled if name in allowed]

        return sorted(set(enabled))

    async def _run_turn(
        self,
        *,
        thread: Any,
        agent: AgentDef,
        rendered_prompt: str,
        model: str,
        effort: ReasoningEffort | None,
        output_schema: dict[str, Any] | None,
        max_session_seconds: float | None,
        interrupt_signal: asyncio.Event | None,
        event_callback: EventCallback | None,
    ) -> _CodexRunResult:
        _safe_callback(event_callback, "agent_turn_start", {"turn": "awaiting_model"})

        turn = await thread.turn(
            rendered_prompt,
            model=model,
            effort=_codex_effort(effort),
            output_schema=output_schema,
            sandbox=self._sandbox(),
            service_tier=self._service_tier,
            **self._approval_kwargs(),
        )
        stream = turn.stream()
        items: list[Any] = []
        usage: Any | None = None
        completed_turn: Any | None = None
        message_deltas: list[str] = []
        reasoning_deltas: list[str] = []
        reasoning_key: _ReasoningKey | None = None
        started_turn_emitted = False
        start_time = time.monotonic()
        next_event_task: asyncio.Task[Any] | None = asyncio.create_task(anext(stream))

        def flush_reasoning() -> None:
            nonlocal reasoning_key
            if not reasoning_deltas:
                reasoning_key = None
                return
            _safe_callback(
                event_callback,
                "agent_reasoning",
                {"content": "".join(reasoning_deltas)},
            )
            reasoning_deltas.clear()
            reasoning_key = None

        try:
            while True:
                if interrupt_signal is not None and interrupt_signal.is_set():
                    if next_event_task is not None:
                        next_event_task.cancel()
                    await turn.interrupt()
                    flush_reasoning()
                    final_response = _final_response_from_items(items, "".join(message_deltas))
                    streamed_response = "".join(message_deltas)
                    return _CodexRunResult(
                        turn_id=turn.id,
                        final_response=final_response,
                        streamed_response=streamed_response,
                        items=items,
                        usage=usage,
                        raw_turn=completed_turn,
                        partial=True,
                    )

                if (
                    max_session_seconds is not None
                    and time.monotonic() - start_time > max_session_seconds
                ):
                    if next_event_task is not None:
                        next_event_task.cancel()
                    await turn.interrupt()
                    flush_reasoning()
                    raise ProviderError(
                        f"Agent '{agent.name}' exceeded maximum session duration "
                        f"of {max_session_seconds:.0f}s",
                        provider_name="codex",
                        is_retryable=False,
                    )

                if next_event_task is None:
                    next_event_task = asyncio.create_task(anext(stream))
                done, _pending = await asyncio.wait({next_event_task}, timeout=0.25)
                if not done:
                    continue

                try:
                    event = next_event_task.result()
                except StopAsyncIteration:
                    break
                finally:
                    next_event_task = None

                payload = event.payload
                method = getattr(event, "method", "")
                is_reasoning_delta = method in _REASONING_DELTA_METHODS

                if method == "turn/started" and not started_turn_emitted:
                    started_turn_emitted = True
                    _safe_callback(event_callback, "agent_turn_start", {"turn": 1})
                elif method == "item/agentMessage/delta":
                    flush_reasoning()
                    delta = getattr(payload, "delta", "")
                    if delta:
                        message_deltas.append(delta)
                        _safe_callback(event_callback, "agent_message", {"content": delta})
                elif is_reasoning_delta:
                    delta = getattr(payload, "delta", "")
                    if delta:
                        next_reasoning_key = _reasoning_delta_key(method, payload)
                        if reasoning_key is not None and reasoning_key != next_reasoning_key:
                            flush_reasoning()
                        reasoning_key = next_reasoning_key
                        reasoning_deltas.append(delta)
                elif method == "item/started":
                    flush_reasoning()
                    self._handle_item_started(payload, event_callback)
                elif method == "item/completed":
                    if getattr(payload, "turn_id", None) == turn.id:
                        items.append(payload.item)
                    if _tool_name_from_item(getattr(payload, "item", None)):
                        flush_reasoning()
                    self._handle_item_completed(payload, event_callback)
                elif method == "thread/tokenUsage/updated":
                    if getattr(payload, "turn_id", None) == turn.id:
                        usage = getattr(payload, "token_usage", None)
                elif method == "turn/completed":
                    flush_reasoning()
                    completed_turn = getattr(payload, "turn", None)
                    break
            flush_reasoning()
        finally:
            if next_event_task is not None:
                next_event_task.cancel()
                try:
                    await next_event_task
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
                except Exception:
                    logger.debug("Ignored Codex stream task error during cleanup", exc_info=True)
            await stream.aclose()

        if completed_turn is None:
            raise ProviderError(
                f"Codex turn for agent '{agent.name}' ended without a completion event",
                provider_name="codex",
                is_retryable=True,
            )

        status = _enum_value(getattr(completed_turn, "status", None))
        if status == "failed":
            error = getattr(completed_turn, "error", None)
            message = getattr(error, "message", None) or "Codex turn failed"
            raise ProviderError(message, provider_name="codex", is_retryable=False)

        streamed_response = "".join(message_deltas)
        final_response = _final_response_from_items(items, streamed_response)
        return _CodexRunResult(
            turn_id=turn.id,
            final_response=final_response,
            streamed_response=streamed_response,
            items=items,
            usage=usage,
            raw_turn=completed_turn,
        )

    def _handle_item_started(self, payload: Any, event_callback: EventCallback | None) -> None:
        item = getattr(payload, "item", None)
        tool_name = _tool_name_from_item(item)
        if not tool_name:
            return
        arguments = getattr(_thread_item(item), "arguments", None)
        try:
            formatted_arguments = format_tool_arguments(arguments) if arguments else None
        except Exception:
            formatted_arguments = _dump_model(arguments)
        _safe_callback(
            event_callback,
            "agent_tool_start",
            {"tool_name": tool_name, "arguments": formatted_arguments},
        )

    def _handle_item_completed(self, payload: Any, event_callback: EventCallback | None) -> None:
        item = getattr(payload, "item", None)
        root = _thread_item(item)
        tool_name = _tool_name_from_item(root)
        if not tool_name:
            return
        result = getattr(root, "result", None) or getattr(root, "output", None)
        error = getattr(root, "error", None)
        if error is not None:
            result_text = f"Error: {_dump_model(error)}"
        else:
            result_text = extract_tool_result_text(_dump_model(result)) or ""
        _safe_callback(
            event_callback,
            "agent_tool_complete",
            {
                "tool_name": tool_name,
                "result": result_text[:_TOOL_RESULT_PREVIEW_LEN],
            },
        )

    def _build_agent_output(
        self,
        result: _CodexRunResult,
        agent: AgentDef,
        model: str,
    ) -> AgentOutput:
        content: dict[str, Any]
        selected_response = result.final_response
        if agent.output:
            try:
                content = parse_json_output(selected_response)
            except ValidationError as exc:
                if (
                    result.streamed_response
                    and result.streamed_response != selected_response
                ):
                    try:
                        selected_response = result.streamed_response
                        content = parse_json_output(selected_response)
                    except ValidationError as fallback_exc:
                        if not result.partial:
                            raise exc from fallback_exc
                        content = {"result": result.final_response}
                elif not result.partial:
                    raise
                else:
                    content = {"result": result.final_response}
            if not result.partial:
                validate_output(content, agent.output)
        else:
            content = {"result": result.final_response}

        total_usage = getattr(result.usage, "total", None)
        input_tokens = getattr(total_usage, "input_tokens", None)
        output_tokens = getattr(total_usage, "output_tokens", None)
        total_tokens = getattr(total_usage, "total_tokens", None)
        cache_read_tokens = getattr(total_usage, "cached_input_tokens", None)
        raw_response = {
            "turn_id": result.turn_id,
            "turn": _dump_model(result.raw_turn),
            "items": _dump_model(result.items),
            "usage": _dump_model(result.usage),
            "final_response": result.final_response,
            "streamed_response": result.streamed_response,
            "selected_response": selected_response,
        }
        return AgentOutput(
            content=content,
            raw_response=raw_response,
            tokens_used=total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            model=model,
            partial=result.partial,
        )

    async def _validate_reasoning_effort_for_model(
        self,
        codex: Any,
        model: str,
        effort: ReasoningEffort,
    ) -> None:
        supported = self._model_effort_cache.get(model)
        if supported is None and model not in self._model_effort_cache:
            try:
                response = await codex.models()
                model_entries = getattr(response, "models", None) or getattr(response, "data", [])
                known_ids = [
                    str(getattr(entry, "model", None) or getattr(entry, "id", ""))
                    for entry in model_entries
                ]
                matched = match_model_id(model, [known_id for known_id in known_ids if known_id])
                selected = next(
                    (
                        entry
                        for entry in model_entries
                        if matched in {getattr(entry, "model", None), getattr(entry, "id", None)}
                    ),
                    None,
                )
                if selected is None:
                    self._model_effort_cache[model] = None
                    return
                efforts = getattr(selected, "supported_reasoning_efforts", None)
                supported = (
                    tuple(_reasoning_effort_value(effort) for effort in efforts)
                    if efforts
                    else None
                )
                self._model_effort_cache[model] = supported
            except Exception:
                logger.debug("Could not fetch Codex model capabilities", exc_info=True)
                self._model_effort_cache[model] = None
                return

        if supported is not None and effort not in supported:
            raise ValidationError(
                f"Model {model!r} does not support reasoning.effort={effort!r}; "
                f"supported values: {sorted(supported)}",
                suggestion="Choose an effort listed in the model's capabilities.",
            )

    def _is_retryable_error(self, exc: BaseException) -> bool:
        if _codex_is_retryable_error is None:
            return False
        try:
            return bool(_codex_is_retryable_error(exc))
        except Exception:
            return False
