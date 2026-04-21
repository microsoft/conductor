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
                            blocks, content_parts, pending_tools, event_callback
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
                    if msg_content and event_callback:
                        self._process_tool_results(msg_content, pending_tools, event_callback)

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
                            f"Claude Agent SDK execution failed: "
                            f"{getattr(message, 'result', 'Unknown error')}"
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
                if thinking and event_callback:
                    _safe_callback(
                        event_callback,
                        "agent_reasoning",
                        {"content": thinking},
                    )

            elif block_type in ("tool_use", "ToolUseBlock"):
                tool_name = getattr(block, "name", "unknown")
                tool_id = getattr(block, "id", "")
                tool_input = getattr(block, "input", {})
                pending_tools[tool_id] = tool_name
                if event_callback:
                    _safe_callback(
                        event_callback,
                        "agent_tool_start",
                        {"tool_name": tool_name, "arguments": tool_input},
                    )

    @staticmethod
    def _process_tool_results(
        blocks: list[Any],
        pending_tools: dict[str, str],
        event_callback: EventCallback,
    ) -> None:
        for block in blocks:
            block_type = getattr(block, "type", None) or type(block).__name__
            if block_type not in ("tool_result", "ToolResultBlock"):
                continue

            tool_use_id = getattr(block, "tool_use_id", "")
            tool_name = pending_tools.pop(tool_use_id, "unknown")
            content = getattr(block, "content", "")
            result_str = str(content)[:500] if content else None

            _safe_callback(
                event_callback,
                "agent_tool_complete",
                {"tool_name": tool_name, "result": result_str},
            )

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


def _safe_callback(callback: EventCallback, event_type: str, data: dict[str, Any]) -> None:
    try:
        callback(event_type, data)
    except Exception:
        logger.debug("Error in event_callback for %s", event_type, exc_info=True)
