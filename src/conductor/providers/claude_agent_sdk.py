"""Claude Agent SDK provider — delegates agentic loop to the claude-agent-sdk package."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from conductor.exceptions import ProviderError
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef, OutputField

try:
    from claude_agent_sdk import ClaudeAgentOptions, query

    CLAUDE_AGENT_SDK_AVAILABLE = True
except ImportError:
    CLAUDE_AGENT_SDK_AVAILABLE = False
    query = None
    ClaudeAgentOptions = None

logger = logging.getLogger(__name__)


def _build_field_schema(field: OutputField, depth: int = 0) -> dict[str, Any]:
    if depth > 10:
        raise ProviderError("Output schema nesting exceeds 10 levels")

    schema: dict[str, Any] = {"type": field.type}
    if field.description:
        schema["description"] = field.description
    if field.type == "object" and field.properties:
        schema["properties"] = _build_properties(field.properties, depth + 1)
        schema["required"] = list(field.properties.keys())
    if field.type == "array" and field.items:
        schema["items"] = _build_field_schema(field.items, depth + 1)
    return schema


def _build_properties(fields: dict[str, OutputField], depth: int = 0) -> dict[str, Any]:
    return {name: _build_field_schema(field, depth) for name, field in fields.items()}


def _build_output_format(output: dict[str, OutputField]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": _build_properties(output),
            "required": list(output.keys()),
        },
    }


class ClaudeAgentSdkProvider(AgentProvider):
    """Claude Agent SDK provider.

    Uses the claude-agent-sdk package (async iterator API) to execute agents.
    The SDK manages the agentic loop, tool execution, and structured output
    extraction internally.
    """

    def __init__(
        self,
        model: str | None = None,
        max_turns: int | None = None,
        max_session_seconds: float | None = None,
    ) -> None:
        if not CLAUDE_AGENT_SDK_AVAILABLE:
            raise ProviderError(
                "Claude Agent SDK not installed",
                suggestion="Install with: uv add 'claude-agent-sdk>=0.1.0'",
            )

        self._default_model = model or "claude-sonnet-4-6"
        self._default_max_turns = max_turns if max_turns is not None else 50
        self._max_session_seconds = max_session_seconds

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
    ) -> AgentOutput:
        if query is None or ClaudeAgentOptions is None:
            raise ProviderError("Claude Agent SDK not available")

        from conductor.cli.app import is_full, is_verbose

        verbose_enabled = is_verbose()
        full_enabled = is_full()

        model = agent.model or self._default_model
        max_turns = (
            agent.max_agent_iterations
            if agent.max_agent_iterations is not None
            else self._default_max_turns
        )

        options = ClaudeAgentOptions(
            model=model,
            system_prompt=agent.system_prompt,
            output_format=_build_output_format(agent.output) if agent.output else None,
            max_turns=max_turns,
            permission_mode="bypassPermissions",
            tools={"type": "preset", "preset": "claude_code"},
        )

        content_parts: list[str] = []
        structured_output: Any = None
        total_input_tokens = 0
        total_output_tokens = 0
        result_model: str | None = model
        turn_count = 0
        # Track pending tool_use IDs so we can pair them with ToolResultBlocks
        pending_tools: dict[str, str] = {}

        try:
            async for message in query(prompt=rendered_prompt, options=options):
                if interrupt_signal is not None and interrupt_signal.is_set():
                    return self._build_output(
                        content_parts,
                        structured_output,
                        agent,
                        result_model,
                        total_input_tokens,
                        total_output_tokens,
                        partial=True,
                    )

                msg_type = type(message).__name__

                if msg_type == "AssistantMessage":
                    blocks = getattr(message, "content", None)
                    if blocks:
                        if event_callback:
                            _safe_callback(
                                event_callback,
                                "agent_turn_start",
                                {"turn": "awaiting_model"},
                            )
                        self._process_assistant_blocks(
                            blocks,
                            content_parts,
                            pending_tools,
                            event_callback,
                            verbose_enabled,
                            full_enabled,
                        )

                    if hasattr(message, "model") and message.model:
                        result_model = message.model
                    if hasattr(message, "usage") and message.usage:
                        total_input_tokens += message.usage.get("input_tokens", 0)
                        total_output_tokens += message.usage.get("output_tokens", 0)
                    turn_count += 1
                    if event_callback:
                        _safe_callback(
                            event_callback,
                            "agent_turn_start",
                            {"turn": turn_count},
                        )

                elif msg_type == "UserMessage":
                    msg_content = getattr(message, "content", None)
                    if msg_content:
                        self._process_tool_results(
                            msg_content,
                            pending_tools,
                            event_callback,
                            verbose_enabled,
                            full_enabled,
                        )

                elif msg_type == "ResultMessage":
                    if getattr(message, "structured_output", None) is not None:
                        structured_output = message.structured_output
                    elif getattr(message, "result", None) and not content_parts:
                        content_parts.append(message.result)
                    if hasattr(message, "usage") and message.usage:
                        total_input_tokens += message.usage.get("input_tokens", 0)
                        total_output_tokens += message.usage.get("output_tokens", 0)
                    if getattr(message, "is_error", False):
                        raise ProviderError(
                            self._build_error_message(message),
                            is_retryable=False,
                        )

        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(
                f"Claude Agent SDK execution error: {e}",
                suggestion="Check that the claude CLI is installed and accessible",
            ) from e

        return self._build_output(
            content_parts,
            structured_output,
            agent,
            result_model,
            total_input_tokens,
            total_output_tokens,
        )

    async def validate_connection(self) -> bool:
        return CLAUDE_AGENT_SDK_AVAILABLE

    async def close(self) -> None:
        pass

    @staticmethod
    def _process_assistant_blocks(
        blocks: list[Any],
        content_parts: list[str],
        pending_tools: dict[str, str],
        event_callback: EventCallback | None,
        verbose: bool = False,
        full_mode: bool = False,
    ) -> None:
        for block in blocks:
            block_type = getattr(block, "type", None) or type(block).__name__

            if block_type in ("text", "TextBlock"):
                text = getattr(block, "text", "")
                if text:
                    content_parts.append(text)
                    if event_callback:
                        _safe_callback(event_callback, "agent_message", {"content": text})

            elif block_type in ("thinking", "ThinkingBlock"):
                thinking = getattr(block, "thinking", "")
                if thinking:
                    if event_callback:
                        _safe_callback(
                            event_callback,
                            "agent_reasoning",
                            {"content": thinking},
                        )
                    if verbose:
                        _log_event_verbose("agent_reasoning", {"content": thinking}, full_mode)

            elif block_type in ("tool_use", "ToolUseBlock"):
                tool_name = getattr(block, "name", "unknown")
                tool_id = getattr(block, "id", "")
                tool_input = getattr(block, "input", {})
                pending_tools[tool_id] = tool_name
                data = {"tool_name": tool_name, "arguments": tool_input}
                if event_callback:
                    _safe_callback(event_callback, "agent_tool_start", data)
                if verbose:
                    _log_event_verbose("agent_tool_start", data, full_mode)

    @staticmethod
    def _process_tool_results(
        blocks: list[Any],
        pending_tools: dict[str, str],
        event_callback: EventCallback | None,
        verbose: bool = False,
        full_mode: bool = False,
    ) -> None:
        for block in blocks:
            block_type = getattr(block, "type", None) or type(block).__name__
            if block_type not in ("tool_result", "ToolResultBlock"):
                continue

            tool_use_id = getattr(block, "tool_use_id", "")
            tool_name = pending_tools.pop(tool_use_id, "unknown")
            content = getattr(block, "content", "")
            result_str = str(content)[:500] if content else None
            data = {"tool_name": tool_name, "result": result_str}

            if event_callback:
                _safe_callback(event_callback, "agent_tool_complete", data)
            if verbose:
                _log_event_verbose("agent_tool_complete", data, full_mode)

    @staticmethod
    def _build_error_message(message: Any) -> str:
        parts: list[str] = []

        errors = getattr(message, "errors", None)
        if errors:
            parts.append("; ".join(str(e) for e in errors))

        result = getattr(message, "result", None)
        if result:
            parts.append(str(result))

        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason:
            parts.append(f"stop_reason={stop_reason}")

        num_turns = getattr(message, "num_turns", None)
        if num_turns is not None:
            parts.append(f"after {num_turns} turns")

        if parts:
            return f"Claude Agent SDK execution failed: {', '.join(parts)}"
        return "Claude Agent SDK execution failed (no details available)"

    @staticmethod
    def _build_output(
        content_parts: list[str],
        structured_output: Any,
        agent: AgentDef,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        partial: bool = False,
    ) -> AgentOutput:
        if structured_output is not None:
            if isinstance(structured_output, dict):
                content = structured_output
            elif isinstance(structured_output, str):
                try:
                    content = json.loads(structured_output)
                except json.JSONDecodeError:
                    content = {"response": structured_output}
            else:
                content = {"response": str(structured_output)}
        elif agent.output:
            combined = "\n".join(content_parts)
            try:
                content = json.loads(combined)
            except json.JSONDecodeError:
                content = {"response": combined}
        else:
            content = {"response": "\n".join(content_parts)}

        total = input_tokens + output_tokens
        return AgentOutput(
            content=content,
            raw_response=structured_output or "\n".join(content_parts),
            tokens_used=total if total else None,
            input_tokens=input_tokens or None,
            output_tokens=output_tokens or None,
            model=model,
            partial=partial,
        )


def _log_event_verbose(event_type: str, data: dict[str, Any], full_mode: bool) -> None:
    from rich.console import Console
    from rich.text import Text

    from conductor.cli.run import _file_console

    console = Console(stderr=True, highlight=False)

    def _print(renderable: Any) -> None:
        console.print(renderable)
        if _file_console is not None:
            _file_console.print(renderable)

    if event_type == "agent_tool_start":
        tool_name = data.get("tool_name", "unknown")
        text = Text()
        text.append("    ├─ ", style="dim")
        text.append("🔧 ", style="")
        text.append(str(tool_name), style="cyan bold")
        _print(text)

        if full_mode:
            args = data.get("arguments")
            if args:
                args_str = str(args)
                args_preview = args_str[:200] + "..." if len(args_str) > 200 else args_str
                arg_text = Text()
                arg_text.append("    │     ", style="dim")
                arg_text.append("args: ", style="dim italic")
                arg_text.append(args_preview, style="dim")
                _print(arg_text)

    elif event_type == "agent_tool_complete":
        tool_name = data.get("tool_name")
        if tool_name:
            text = Text()
            text.append("    │  ", style="dim")
            text.append("✓ ", style="green")
            text.append(str(tool_name), style="dim")
            _print(text)

        if full_mode:
            result = data.get("result")
            if result:
                result_str = str(result)
                result_preview = result_str[:200] + "..." if len(result_str) > 200 else result_str
                result_text = Text()
                result_text.append("    │     ", style="dim")
                result_text.append("result: ", style="dim italic")
                result_text.append(result_preview, style="dim")
                _print(result_text)

    elif event_type == "agent_reasoning":
        if full_mode:
            reasoning = data.get("content", "")
            if reasoning:
                display = reasoning[:150] + "..." if len(reasoning) > 150 else reasoning
                text = Text()
                text.append("    │  ", style="dim")
                text.append("💭 ", style="")
                text.append(display.replace("\n", " "), style="italic dim")
                _print(text)


def _safe_callback(callback: EventCallback, event_type: str, data: dict[str, Any]) -> None:
    try:
        callback(event_type, data)
    except Exception:
        logger.debug("Error in event_callback for %s", event_type, exc_info=True)
