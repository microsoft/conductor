"""Claude Agent SDK provider — delegates agentic loop to the claude-agent-sdk package."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Final, cast

from conductor.exceptions import ProviderError
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback
from conductor.providers.capabilities import ProviderCapabilities

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef, OutputField

try:
    from claude_agent_sdk import ClaudeAgentOptions, query  # ty: ignore[unresolved-import]

    CLAUDE_AGENT_SDK_AVAILABLE = True
except ImportError:
    CLAUDE_AGENT_SDK_AVAILABLE = False
    query: Any = None
    ClaudeAgentOptions: Any = None

logger = logging.getLogger(__name__)


def _build_field_schema(field: OutputField, depth: int = 0) -> dict[str, Any]:
    """Translate a single ``OutputField`` into a JSON-Schema fragment.

    Recursively descends into object properties and array items. The depth
    cap (10) protects against pathological YAML that would otherwise blow
    the Python recursion limit during schema construction.

    Args:
        field: The output field definition from the workflow YAML.
        depth: Current recursion depth (internal — do not pass).

    Returns:
        A JSON-Schema fragment matching the field's type and constraints.

    Raises:
        ProviderError: If recursion exceeds 10 nested levels.
    """
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
    """Translate a mapping of named ``OutputField`` definitions into JSON-Schema properties."""
    return {name: _build_field_schema(field, depth) for name, field in fields.items()}


def _build_output_format(output: dict[str, OutputField]) -> dict[str, Any]:
    """Build the ``output_format`` payload passed to ``ClaudeAgentOptions``.

    The SDK expects a wrapping ``{"type": "json_schema", "schema": ...}`` object
    around the actual JSON-Schema document. All declared fields are marked
    required in the schema sent to the SDK. Conductor does not currently
    validate the SDK's returned content against this schema — a missing
    key produces a dict with that key absent rather than a hard failure.
    If schema validation is added later, revisit this default.
    """
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": _build_properties(output),
            "required": list(output.keys()),
        },
    }


# Default tool preset granted when an agent omits the `tools:` list. This
# mirrors the SDK's `claude_code` preset (filesystem, bash, web, etc.) — i.e.
# the same behavior the user gets when running the `claude` CLI directly. It is
# selected from the RAW ``agent.tools is None`` signal, NOT from the executor's
# resolved list: for an agent that declares no `tools:`, the executor returns the
# workflow-tools copy, which is empty only when the workflow declares no `tools:`.
_DEFAULT_TOOL_PRESET: dict[str, str] = {"type": "preset", "preset": "claude_code"}

# Display-only previews for the verbose CLI pretty-printer (NOT surfaced
# in events — see ``_TOOL_RESULT_PREVIEW_LEN`` below for the on-the-wire
# truncation).
_VERBOSE_ARG_PREVIEW_LEN: Final[int] = 200
_VERBOSE_RESULT_PREVIEW_LEN: Final[int] = 200
_REASONING_PREVIEW_LEN: Final[int] = 150

# ``_TOOL_RESULT_PREVIEW_LEN`` is load-bearing: it is the upper limit the
# dashboard and JSONL stream observe for ``agent_tool_complete`` results.
# Changing it changes what every downstream consumer sees.
_TOOL_RESULT_PREVIEW_LEN: Final[int] = 500

# Default SDK-recognized model when neither the agent nor the workflow sets
# one. The string must match a model alias accepted by the upstream
# ``claude-agent-sdk`` package; revalidate when bumping the upstream pin
# in pyproject.toml.
_DEFAULT_MODEL: Final[str] = "claude-sonnet-4-5"


class ClaudeAgentSdkProvider(AgentProvider):
    """Claude Agent SDK provider.

    Uses the claude-agent-sdk package (async iterator API) to execute agents.
    The SDK manages the agentic loop, tool execution, and structured output
    extraction internally.
    """

    CAPABILITIES = ProviderCapabilities(
        tier="experimental",
        # MCP servers are rejected at the factory (no translation from
        # Conductor's MCP config to the SDK's MCP dict is implemented).
        mcp_tools=False,
        # Per-agent ``tools: []`` is honored (disables all tools). Per-agent
        # ``tools: [<names>]`` is refused loudly at execute time because
        # workflow tool names do not translate to Claude CLI tool IDs.
        # The capability records the strict end of that contract — when the
        # user declares a non-empty allowlist, the validator surfaces it as
        # an error before runtime hits the refusal.
        workflow_tools_passthrough=False,
        # The SDK yields messages incrementally via the async iterator —
        # ``agent_message`` / ``agent_tool_*`` events fire as they arrive.
        streaming_events=True,
        # ``ThinkingBlock`` content is forwarded as ``agent_reasoning``.
        agent_reasoning_events=True,
        # The SDK does expose an ``effort`` field on ClaudeAgentOptions,
        # but the provider does not currently wire ``agent.reasoning.effort``
        # through to it. Declare ``None`` until that plumbing exists.
        reasoning_effort=None,
        # The SDK's ``output_format={"type": "json_schema", ...}`` plus
        # follow-on JSON parsing approximates native schema enforcement,
        # but the model still occasionally returns prose. Mark as
        # prompt-injection to keep the validator honest.
        structured_output="prompt_injection",
        # ``interrupt_signal`` is checked between SDK messages and triggers
        # a partial-output return.
        interrupt=True,
        # ``max_session_seconds`` is enforced between messages via
        # ``time.monotonic()``.
        max_session_seconds=True,
        # The SDK manages its own session state inside the ``claude`` CLI;
        # Conductor does not persist or replay it through resume.
        checkpoint_resume=False,
        # Token counts come from ``ResultMessage.usage`` (cumulative
        # session total — see A4 fix).
        usage_tracking=True,
        # No global mutable state shared across calls — the SDK spawns
        # an independent subprocess per query() invocation.
        concurrent_safe=True,
        upstream_pin="claude-agent-sdk>=0.1.0",
        maintainer="@lesandiz (best-effort)",
    )

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

        self._default_model = model or _DEFAULT_MODEL
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

        # Verbose / full-mode flags drive optional diagnostic output. They
        # live in the CLI layer, so importing them couples this provider
        # to the CLI. Wrap defensively so library users (no CLI installed)
        # still get a working provider — just without the verbose pretty-printer.
        try:
            from conductor.cli.app import is_full, is_verbose

            verbose_enabled = is_verbose()
            full_enabled = is_full()
        except ImportError:
            verbose_enabled = False
            full_enabled = False

        model = agent.model or self._default_model
        max_turns = (
            agent.max_agent_iterations
            if agent.max_agent_iterations is not None
            else self._default_max_turns
        )

        # Per-agent ``max_session_seconds`` overrides the provider default,
        # matching Copilot / Claude semantics. ``None`` means "no timeout".
        max_session_seconds = (
            agent.max_session_seconds
            if agent.max_session_seconds is not None
            else self._max_session_seconds
        )

        sdk_tools, permission_mode = self._resolve_tool_config(tools, agent)

        options = ClaudeAgentOptions(
            model=model,
            system_prompt=agent.system_prompt,
            output_format=_build_output_format(agent.output) if agent.output else None,
            max_turns=max_turns,
            permission_mode=permission_mode,
            tools=sdk_tools,
        )

        content_parts: list[str] = []
        structured_output: Any = None
        total_input_tokens = 0
        total_output_tokens = 0
        result_model: str | None = model
        turn_count = 0
        # Track pending tool_use IDs so we can pair them with ToolResultBlocks
        pending_tools: dict[str, str] = {}
        session_start = time.monotonic()

        try:
            # Signal "awaiting model" before entering the SDK iterator: the
            # SDK is about to make the first model call. Dashboards use this
            # to show a "waiting for model" spinner.
            if event_callback:
                _safe_callback(
                    event_callback,
                    "agent_turn_start",
                    {"turn": "awaiting_model"},
                )

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

                # Wall-clock session timeout. The SDK does not expose a per-call
                # timeout, so enforce at each message boundary — the cheapest
                # cancellation point we have. The check is between messages
                # rather than around the full ``async for`` so we can return
                # a clean ProviderError rather than letting asyncio raise.
                if max_session_seconds is not None:
                    elapsed = time.monotonic() - session_start
                    if elapsed > max_session_seconds:
                        raise ProviderError(
                            f"Agent '{agent.name}' exceeded maximum session "
                            f"duration of {max_session_seconds:.0f}s "
                            f"after {turn_count} turn(s)",
                            is_retryable=False,
                        )

                msg_type = type(message).__name__

                if msg_type == "AssistantMessage":
                    msg = cast(Any, message)
                    # Iteration N begins when its assistant response arrives.
                    # Emit BEFORE processing blocks so per-parity rules the
                    # turn marker bounds the iteration's content events.
                    turn_count += 1
                    if event_callback:
                        _safe_callback(
                            event_callback,
                            "agent_turn_start",
                            {"turn": turn_count},
                        )

                    blocks = getattr(msg, "content", None)
                    has_tool_use = False
                    if blocks:
                        has_tool_use = any(
                            (getattr(b, "type", None) or type(b).__name__)
                            in ("tool_use", "ToolUseBlock")
                            for b in blocks
                        )
                        self._process_assistant_blocks(
                            blocks,
                            content_parts,
                            pending_tools,
                            event_callback,
                            verbose_enabled,
                            full_enabled,
                        )

                    if hasattr(msg, "model") and msg.model:
                        result_model = msg.model
                    if hasattr(msg, "usage") and msg.usage:
                        total_input_tokens += msg.usage.get("input_tokens", 0)
                        total_output_tokens += msg.usage.get("output_tokens", 0)

                    # If this turn requested tool calls, the SDK will run
                    # them and then make another model call. Signal
                    # "awaiting model" again so the spinner stays on
                    # through the tool roundtrip.
                    if has_tool_use and event_callback:
                        _safe_callback(
                            event_callback,
                            "agent_turn_start",
                            {"turn": "awaiting_model"},
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
                    msg = cast(Any, message)
                    if getattr(msg, "structured_output", None) is not None:
                        structured_output = msg.structured_output
                    elif getattr(msg, "result", None) and not content_parts:
                        content_parts.append(msg.result)
                    # ``ResultMessage.usage`` is the CUMULATIVE session total
                    # (per the SDK docstring on ApiUsage.apiUsage). Replace
                    # rather than add — the per-AssistantMessage running sum
                    # exists only as a fallback when no ResultMessage arrives
                    # (e.g. mid-stream interrupt).
                    if hasattr(msg, "usage") and msg.usage:
                        total_input_tokens = msg.usage.get("input_tokens", total_input_tokens)
                        total_output_tokens = msg.usage.get("output_tokens", total_output_tokens)
                    if getattr(msg, "is_error", False):
                        raise ProviderError(
                            self._build_error_message(msg),
                            is_retryable=_is_retryable_result(msg),
                        )

        except ProviderError:
            raise
        except asyncio.CancelledError:
            # Do NOT translate into ProviderError — upstream interrupt
            # handlers rely on CancelledError to unwind cleanly.
            raise
        except Exception as e:
            raise ProviderError(
                f"Claude Agent SDK execution error: {e}",
                suggestion=_classify_error_suggestion(e),
                is_retryable=_is_retryable_exception(e),
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
        """Check that the SDK is importable and the ``claude`` CLI is locatable.

        Mirrors the SDK's own CLI lookup logic (bundled binary first, then
        ``shutil.which``, then the SDK's hardcoded fallback locations). We
        avoid an actual API round-trip because that would require valid
        credentials and consume tokens — caller code can still surface auth
        failures at first ``execute()``.

        Returns:
            True when both the SDK import and CLI lookup succeed.
        """
        if not CLAUDE_AGENT_SDK_AVAILABLE:
            return False

        import shutil
        from pathlib import Path

        # Bundled CLI takes precedence (matches the SDK's own resolution).
        try:
            import claude_agent_sdk  # ty: ignore[unresolved-import]

            sdk_dir = Path(claude_agent_sdk.__file__).parent
            for candidate in (sdk_dir / "_bundled" / "claude",):
                if candidate.exists() and candidate.is_file():
                    return True
        except Exception:
            logger.debug("Bundled CLI probe failed", exc_info=True)

        if shutil.which("claude"):
            return True

        # SDK's hardcoded fallback locations — keep in sync with
        # claude_agent_sdk._internal.transport.subprocess_cli._find_cli.
        for path in (
            Path.home() / ".npm-global/bin/claude",
            Path("/usr/local/bin/claude"),
            Path.home() / ".local/bin/claude",
            Path.home() / "node_modules/.bin/claude",
            Path.home() / ".yarn/bin/claude",
            Path.home() / ".claude/local/claude",
        ):
            if path.exists() and path.is_file():
                return True

        logger.warning(
            "Claude CLI not found on PATH, in bundled package, or in any "
            "known fallback location. Install with `npm install -g "
            "@anthropic-ai/claude-code`."
        )
        return False

    async def close(self) -> None:
        pass

    @staticmethod
    def _resolve_tool_config(
        tools: list[str] | None,
        agent: AgentDef,
    ) -> tuple[Any, str | None]:
        """Resolve the SDK ``tools`` and ``permission_mode`` for an agent.

        Conductor's ``tools:`` allowlist contains workflow-tool names that
        resolve through ``runtime.tools`` — they are NOT Claude CLI tool
        identifiers. We therefore refuse to forward a non-empty allowlist
        to the SDK rather than silently grant the wrong native tools.

        The ``tools`` argument is the executor's *resolved* list from
        :func:`conductor.executor.agent.resolve_agent_tools`. That function
        erases the distinction between an omitted ``tools:`` and an explicit
        ``tools: []``: both arrive here as an empty list whenever the
        workflow declares no workflow-level ``tools:`` (``config.tools`` is
        empty; a non-empty list makes an omitted agent resolve non-empty). We
        therefore consult the RAW ``agent.tools`` field — the only place the
        omitted-vs-explicit signal survives — to pick the default.

        Semantics:

        * ``tools`` empty (``[]`` or ``None``) and ``agent.tools is None`` —
          the agent omitted ``tools:``. Fall back to the ``claude_code``
          preset (filesystem, bash, web) and bypass permissions, matching
          what the user gets from the bare ``claude`` CLI.
        * ``tools`` empty and ``agent.tools == []`` — explicit "no tools"
          request. Pass an empty list to the SDK so all tools are disabled.
          Drop the permission bypass because there are no tools to permit.
        * ``tools`` non-empty — raise ``ProviderError``. Workflow tool
          name → CLI tool ID translation is not implemented (tracked as
          a follow-up). Silently dropping the allowlist would be a
          security regression; silently passing it through could grant
          the wrong native tool. Refuse loudly.

        Args:
            tools: The executor-resolved ``tools:`` allowlist for this agent.
            agent: The agent definition. ``agent.tools`` carries the raw
                omitted-vs-explicit-empty signal; ``agent.name`` is used in
                the error message.

        Returns:
            A ``(sdk_tools, permission_mode)`` tuple suitable for
            ``ClaudeAgentOptions``.

        Raises:
            ProviderError: If ``tools`` is a non-empty list.
        """
        if not tools:
            # The executor passes [] for BOTH "omitted (no workflow tools to
            # inherit)" and explicit "tools: []". Disambiguate via the raw
            # per-agent field, which the executor's resolution erased.
            if agent.tools is None:
                # Omitted -> default claude_code preset (filesystem/bash/web).
                return _DEFAULT_TOOL_PRESET, "bypassPermissions"
            # Explicit `tools: []` -> no tools, no permission bypass.
            return [], None
        raise ProviderError(
            f"Agent '{agent.name}' resolves to tools={tools!r} (declared on "
            "the agent or inherited from the workflow-level 'tools:' list), "
            "but claude-agent-sdk does not support workflow tool allowlists "
            "(workflow tool names do not translate to Claude CLI tool IDs).",
            suggestion=(
                "Omit both the per-agent and workflow-level 'tools:' to grant "
                "the full claude_code preset, or set 'tools: []' to disable "
                "all tools."
            ),
        )

    @staticmethod
    def _process_assistant_blocks(
        blocks: list[Any],
        content_parts: list[str],
        pending_tools: dict[str, str],
        event_callback: EventCallback | None,
        verbose: bool = False,
        full_mode: bool = False,
    ) -> None:
        """Dispatch the content blocks of an ``AssistantMessage``.

        Appends text blocks to ``content_parts`` (the final-output buffer),
        forwards thinking blocks via ``agent_reasoning``, and registers
        tool_use blocks in ``pending_tools`` for later pairing with their
        results in :meth:`_process_tool_results`.

        Args:
            blocks: The ``AssistantMessage.content`` list.
            content_parts: Mutable list of text fragments accumulated so far.
            pending_tools: Mutable mapping of tool_use_id → tool_name.
            event_callback: Optional event forwarder.
            verbose: When True, also write to the verbose console.
            full_mode: When True, include argument / result previews.
        """
        for block in blocks:
            # Some SDK versions report block kind via a ``type`` string field
            # (snake_case), others rely on the dataclass class name (CamelCase).
            # Match both so we are robust to either packaging.
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
        """Pair ``ToolResultBlock`` entries with their pending tool_use IDs.

        Emits ``agent_tool_complete`` for every result, looking up the
        original tool_name from ``pending_tools`` by ``tool_use_id``. If
        the SDK ever delivers a result without a matching pending entry
        (recovered session, races, etc.), the tool_name falls back to
        ``"unknown"`` rather than dropping the event.

        Args:
            blocks: The ``UserMessage.content`` list (a mix of tool results
                and prose).
            pending_tools: Mapping of tool_use_id → tool_name; entries are
                consumed (popped) as their results arrive.
            event_callback: Optional event forwarder.
            verbose: When True, also write to the verbose console.
            full_mode: When True, include result preview.
        """
        for block in blocks:
            block_type = getattr(block, "type", None) or type(block).__name__
            if block_type not in ("tool_result", "ToolResultBlock"):
                continue

            tool_use_id = getattr(block, "tool_use_id", "")
            tool_name = pending_tools.pop(tool_use_id, "unknown")
            content = getattr(block, "content", "")
            result_str = str(content)[:_TOOL_RESULT_PREVIEW_LEN] if content else None
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
        """Assemble the final ``AgentOutput`` from accumulated execution state.

        Resolution order for ``content``:

        1. SDK-provided ``structured_output`` (preferred — already parsed by
           the SDK from a JSON-Schema response).
        2. JSON-parsed concatenation of text blocks (when ``agent.output`` is
           declared — fails loudly with ``ValidationError`` on parse error
           unless this is partial output, in which case the raw text is
           wrapped under ``{"response": ...}``).
        3. Bare ``{"response": ...}`` wrapper (when no schema declared).

        Args:
            content_parts: Text fragments captured from AssistantMessages.
            structured_output: SDK ``ResultMessage.structured_output`` value.
            agent: Agent definition (used for schema awareness and error msg).
            model: SDK-reported model identifier.
            input_tokens: Cumulative input tokens.
            output_tokens: Cumulative output tokens.
            partial: True when the output is from a mid-stream interrupt.
                Disables strict schema enforcement so partial best-effort
                output is preferred over hard failure.

        Returns:
            Populated ``AgentOutput`` ready to return from :meth:`execute`.

        Raises:
            ValidationError: If ``agent.output`` is declared, this is not
                a partial output, and the response cannot be parsed as JSON.
        """
        from conductor.exceptions import ValidationError

        if structured_output is not None:
            if isinstance(structured_output, dict):
                content = structured_output
            elif isinstance(structured_output, str):
                try:
                    content = json.loads(structured_output)
                except json.JSONDecodeError as e:
                    # If the agent declared a schema, a non-JSON
                    # structured_output value is a contract violation —
                    # downstream routes/templates assume the schema holds.
                    # Tolerate only on partial output (interrupt) where
                    # we'd rather surface what we have than nothing.
                    if agent.output and not partial:
                        raise ValidationError(
                            f"Agent '{agent.name}' declared an output schema "
                            f"but returned non-JSON structured_output: "
                            f"{structured_output[:200]!r}",
                            suggestion=(
                                "Ensure the prompt instructs the model to "
                                "emit JSON matching the declared `output:` "
                                "fields, or remove the `output:` schema."
                            ),
                        ) from e
                    content = {"response": structured_output}
            else:
                # The SDK returned ``structured_output`` of a shape the
                # provider does not understand (not a dict, not a str —
                # likely an SDK version drift). If the agent declared an
                # output schema, silently coercing to ``{"response": ...}``
                # would violate the schema contract; downstream routes /
                # templates that key off declared fields would then fail
                # with confusing KeyError / UndefinedError in unrelated
                # parts of the workflow.
                if agent.output and not partial:
                    raise ValidationError(
                        f"Agent '{agent.name}' declared an output schema but "
                        f"the SDK returned structured_output of unexpected "
                        f"type {type(structured_output).__name__}: "
                        f"{str(structured_output)[:200]!r}",
                        suggestion=(
                            "Pin or upgrade claude-agent-sdk to a compatible "
                            "version, or remove the `output:` schema."
                        ),
                    )
                content = {"response": str(structured_output)}
        elif agent.output:
            combined = "\n".join(content_parts)
            try:
                content = json.loads(combined)
            except json.JSONDecodeError as e:
                if not partial:
                    raise ValidationError(
                        f"Agent '{agent.name}' declared an output schema but "
                        f"returned non-JSON text: {combined[:200]!r}",
                        suggestion=(
                            "Ensure the prompt instructs the model to emit "
                            "JSON matching the declared `output:` fields, "
                            "or remove the `output:` schema."
                        ),
                    ) from e
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
    """Pretty-print an SDK event to the verbose console (stderr) and log file.

    ``execute()`` only calls this helper when its own CLI import succeeded,
    so the ``try/except ImportError`` around ``_file_console`` is belt-and-
    braces — kept in case a caller invokes the helper directly without
    going through ``execute()``.
    """
    from rich.console import Console
    from rich.text import Text

    try:
        from conductor.cli.run import _file_console
    except ImportError:
        _file_console = None

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
                args_preview = (
                    args_str[:_VERBOSE_ARG_PREVIEW_LEN] + "..."
                    if len(args_str) > _VERBOSE_ARG_PREVIEW_LEN
                    else args_str
                )
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
                result_preview = (
                    result_str[:_VERBOSE_RESULT_PREVIEW_LEN] + "..."
                    if len(result_str) > _VERBOSE_RESULT_PREVIEW_LEN
                    else result_str
                )
                result_text = Text()
                result_text.append("    │     ", style="dim")
                result_text.append("result: ", style="dim italic")
                result_text.append(result_preview, style="dim")
                _print(result_text)

    elif event_type == "agent_reasoning":
        if full_mode:
            reasoning = data.get("content", "")
            if reasoning:
                display = (
                    reasoning[:_REASONING_PREVIEW_LEN] + "..."
                    if len(reasoning) > _REASONING_PREVIEW_LEN
                    else reasoning
                )
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


def _classify_error_suggestion(exc: BaseException) -> str:
    """Build a remediation hint tailored to the kind of failure observed.

    Inspects the exception class hierarchy and message text to provide an
    actionable hint per failure mode (CLI missing, auth, rate limit,
    network, parse, generic). A single generic suggestion would be
    actively misleading for most failures.
    """
    cls = type(exc).__name__
    msg = str(exc).lower()

    if cls == "CLINotFoundError":
        return (
            "The `claude` CLI is not installed or not on PATH. Install it from "
            "https://docs.anthropic.com/claude/docs/claude-code and verify with `claude --version`."
        )
    if cls == "CLIConnectionError":
        return (
            "Could not connect to the `claude` CLI. Check that the binary is "
            "executable and that no firewall is blocking its spawned subprocess."
        )
    if cls in ("CLIJSONDecodeError", "MessageParseError"):
        return (
            "The Claude Agent SDK returned a malformed response. This usually "
            "indicates an SDK version mismatch — try upgrading "
            "`claude-agent-sdk` and the `claude` CLI to compatible versions."
        )
    if cls == "ProcessError":
        # Authentication and rate-limit failures surface as ProcessError with
        # a non-zero exit code; differentiate by stderr content where possible.
        if "auth" in msg or "api key" in msg or "unauthorized" in msg or "401" in msg:
            return (
                "Authentication failed. Verify `ANTHROPIC_API_KEY` is set and "
                "valid, or run `claude login` to refresh credentials."
            )
        if "rate" in msg or "429" in msg or "quota" in msg:
            return (
                "Rate-limited or quota exceeded. Retry after the cooldown, or "
                "lower the workflow's concurrency / iteration count."
            )
        if "network" in msg or "connection" in msg or "timeout" in msg:
            return (
                "Network connectivity issue reaching the Anthropic API. Check "
                "your internet connection and any proxy / firewall settings."
            )
        return (
            "The `claude` CLI subprocess failed. Inspect the error output "
            "above for the underlying cause."
        )

    # Generic fallback — only reached for non-SDK exception classes that
    # somehow propagated up. Keep the original advice as a last resort.
    return "Check that the `claude` CLI is installed and accessible."


def _is_retryable_exception(exc: BaseException) -> bool:
    """Classify an SDK exception as retryable based on type and message.

    Retryable conditions (transient, may succeed on a second attempt):
    network failures, rate limits, server-side 5xx, connection drops.

    Non-retryable: auth (401/403), bad request (400), malformed responses,
    missing CLI, unrecognized errors.
    """
    cls = type(exc).__name__
    msg = str(exc).lower()

    if cls in ("CLIJSONDecodeError", "MessageParseError", "CLINotFoundError"):
        return False

    if cls == "CLIConnectionError":
        # Connection drops to a local subprocess — often transient.
        return True

    if cls == "ProcessError":
        if "auth" in msg or "401" in msg or "403" in msg or "unauthorized" in msg:
            return False
        if "rate" in msg or "429" in msg or "quota" in msg or "overload" in msg:
            return True
        if "500" in msg or "502" in msg or "503" in msg or "504" in msg:
            return True
        return bool("network" in msg or "connection" in msg or "timeout" in msg)

    return False


def _is_retryable_result(message: Any) -> bool:
    """Classify a ResultMessage(is_error=True) as retryable.

    Inspects ``stop_reason``, ``api_error_status``, and the accumulated
    error text. Mirrors :func:`_is_retryable_exception` semantics:
    rate limits and 5xx are retryable; auth and bad requests are not.
    """
    status = getattr(message, "api_error_status", None)
    if isinstance(status, int):
        if status in (401, 403, 400):
            return False
        if status == 429 or 500 <= status < 600:
            return True

    stop_reason = getattr(message, "stop_reason", None)
    if isinstance(stop_reason, str):
        sr = stop_reason.lower()
        if sr in ("rate_limit", "overloaded", "overload", "server_error"):
            return True
        if sr in ("max_tokens", "max_turns", "stop_sequence", "tool_use", "end_turn"):
            # These are normal completion signals, not transient errors.
            # If is_error=True with one of these stop reasons, it's a logic
            # error in the agent — retry won't help.
            return False

    # Fall back to string inspection of the accumulated error text.
    text = " ".join(
        str(p)
        for p in (
            getattr(message, "errors", None) or [],
            getattr(message, "result", None) or "",
            stop_reason or "",
        )
        if p
    ).lower()
    if "rate" in text or "429" in text or "quota" in text or "overload" in text:
        return True
    if "500" in text or "502" in text or "503" in text or "504" in text:
        return True
    return bool("network" in text or "connection" in text or "timeout" in text)
