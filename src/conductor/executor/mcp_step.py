"""Execution for ``type: mcp`` workflow steps.

An ``mcp`` step calls a tool on an MCP server configured in
``workflow.runtime.mcp_servers`` and stores the JSON-safe result envelope in
the workflow context. There is no LLM call — the step is a typed bridge
between the workflow engine and an MCP server.

Argument rendering:

- ``arguments`` values are Jinja2-rendered recursively: dicts and lists are
  walked, string leaves are rendered against the workflow context, and each
  FULLY RENDERED string is then YAML-parsed (the set-step ``auto`` rule).
  Whatever the rendered text parses as is the value: ``"105"`` -> ``int``,
  ``"true"`` -> ``bool``, ``"[1, 2]"`` -> ``list`` — including renders built
  from embedded templates, so ``"1{{ x }}"`` with ``x=2`` renders ``"12"``
  and becomes the integer ``12``, and ``"label: {{ x }}"`` becomes a mapping.
  A render that does not parse as YAML stays the raw string, and YAML-native
  scalars (int / float / bool / None) pass through untouched.
- ``FileString`` values (from the ``!file`` tag) are ``str`` subclasses and
  render like normal templates.

The result envelope (produced by
:meth:`conductor.mcp.manager.MCPManager.call_tool_structured`) has the shape
``{"content": [...], "structured": {...}|null, "is_error": bool}``. When
``structured`` is a dict, its keys are merged on top of the envelope so routes
and templates can address individual result fields directly — except the
reserved keys (see :data:`_RESERVED_ENVELOPE_KEYS`), which are never
overridden (collisions are dropped with a debug-level log, mirroring the
script-step JSON shadow precedent in ``engine/workflow.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from ruamel.yaml.error import YAMLError

from conductor.exceptions import ExecutionError
from conductor.executor.set_step import _to_json_safe, _yaml_load
from conductor.executor.template import TemplateRenderer

if TYPE_CHECKING:
    from conductor.config.schema import MCPStepDef
    from conductor.mcp.manager import MCPManager

logger = logging.getLogger(__name__)

# Envelope keys owned by the MCP result envelope itself. A structured result
# carrying same-named keys must never override them — the merge drops these
# collisions rather than corrupting the envelope contract.
#
# ``outputs`` / ``errors`` are envelope-external but equally reserved:
# ``WorkflowContext`` duck-types parallel/for-each group outputs by exactly
# those two top-level keys, so a structured result flattening them onto the
# envelope would make an ordinary step output misclassify as a group output
# (losing its normal ``.output`` wrapper in all three context modes) and would
# confuse for-each source resolution. They stay reachable under
# ``output.structured.outputs`` / ``output.structured.errors``.
_RESERVED_ENVELOPE_KEYS = frozenset({"content", "structured", "is_error", "outputs", "errors"})

# Explicit null markers recognised by the auto-coercion rule; a render that
# parses to None through any other string keeps its raw form (mirrors the
# set-step auto rule).
_NULL_MARKERS = frozenset({"null", "~", "Null", "NULL"})


def mcp_result_bytes(content: Any, structured: Any) -> int:
    """Compute the UTF-8 byte size of an MCP result envelope.

    This is the single result-size contract shared by the live engine events
    and the web server's synthetic replay path — both must measure the same
    payload the same way. ``ensure_ascii=False`` keeps multibyte text as-is so
    the byte count reflects the actual UTF-8 encoding, and the compact
    separators make the measurement independent of formatting.

    Args:
        content: The envelope's ``content`` block list.
        structured: The envelope's ``structured`` mapping (or ``None``).

    Returns:
        The byte length of the JSON-encoded ``{"content", "structured"}``
        payload.
    """
    return len(
        json.dumps(
            {"content": content, "structured": structured},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def mcp_truncation_metadata(content: Any) -> tuple[bool, str | None]:
    """Extract the trusted truncation markers from a LIVE result envelope.

    ``truncated`` and ``spill_path`` on a content block are Conductor-local
    metadata: :meth:`conductor.mcp.manager.MCPManager.call_tool_structured`
    strips any server-supplied fields of those names at ingestion and only
    its own truncation pass sets them. This helper is therefore called ONLY
    on a freshly returned envelope — the engine's live ``mcp_completed``
    event — never on a checkpoint-restored one: a checkpoint may have been
    written before the ingestion stripping existed, so a stored
    ``spill_path`` can be a server-supplied string. The web server's
    synthetic replay path does not republish stored markers at all (see
    ``WebDashboard._synth_mcp_pair``). The defensive type checks remain
    because the live contract is only ever a list of dict blocks.

    Args:
        content: The envelope's ``content`` value (expected list of dicts).

    Returns:
        ``(truncated, spill_path)`` — ``truncated`` is True when any block was
        locally truncated; ``spill_path`` is the first local spill path (a
        non-empty string) or ``None``.
    """
    blocks = content if isinstance(content, list) else []
    truncated = any(isinstance(b, dict) and b.get("truncated") is True for b in blocks)
    spill_path = next(
        (
            b["spill_path"]
            for b in blocks
            if isinstance(b, dict) and isinstance(b.get("spill_path"), str) and b["spill_path"]
        ),
        None,
    )
    return truncated, spill_path


class McpStepTimeoutError(ExecutionError):
    """A ``type: mcp`` step's per-call ``timeout`` elapsed.

    A distinct, value-free category: the message (``timed out after Ns``) is
    authored at the raise site and safe to surface verbatim, so the engine
    propagates it unchanged (unlike transport/render/validation failures,
    whose raw text can embed argument or result values and is redacted).
    """


class _McpToolTransportFailure(RuntimeError):
    """Private signal for a transport failure from the tool await alone."""


class McpStepExecutor:
    """Executes ``type: mcp`` workflow steps.

    Renders the step's ``arguments`` recursively against the workflow context,
    invokes the tool through the supplied :class:`MCPManager`, and returns the
    JSON-safe result envelope with ``structured`` keys merged on top.

    The renderer instance is reused across invocations to avoid Jinja2
    environment churn.
    """

    def __init__(self) -> None:
        """Initialize the executor with a shared template renderer."""
        self.renderer = TemplateRenderer()

    async def execute(
        self,
        agent: MCPStepDef,
        agent_context: dict[str, Any],
        manager: MCPManager,
    ) -> dict[str, Any]:
        """Render arguments, call the MCP tool, and merge the result envelope.

        Args:
            agent: Agent definition with ``type == "mcp"``.
            agent_context: Workflow context for template rendering.
            manager: Connected MCP manager owning the target server's session.

        Returns:
            The JSON-safe envelope ``{"content": [...], "structured": dict |
            None, "is_error": bool}`` with ``structured`` keys merged on top
            (reserved keys are never overridden — see
            :data:`_RESERVED_ENVELOPE_KEYS`).

        Raises:
            McpStepTimeoutError: If the call exceeds ``agent.timeout`` seconds.
            ValueError: Unknown server / tool — propagated from the manager.
            RuntimeError: Call failure or malformed structured content —
                propagated from the manager.
        """
        # Guaranteed by MCPStepDef.validate_agent_type (config/schema.py) for
        # type == "mcp": both fields are required and non-empty.
        assert agent.server is not None
        assert agent.tool is not None
        label = f"mcp step '{agent.name}'"
        rendered = _render_arguments(self.renderer, agent.arguments or {}, agent_context, label)
        result = _to_json_safe(rendered, label)

        coro = manager.call_tool_structured(agent.server, agent.tool, result)
        from conductor.mcp.manager import McpTransportError

        timeout = agent.timeout
        try:
            if timeout is not None:
                envelope = await asyncio.wait_for(coro, timeout=timeout)
            else:
                envelope = await coro
        except TimeoutError:
            raise McpStepTimeoutError(
                f"MCP step '{agent.name}' timed out after {timeout}s",
                agent_name=agent.name,
            ) from None
        except McpTransportError as error:
            raise _McpToolTransportFailure("MCP tool transport failed") from error

        structured = envelope.get("structured")
        if isinstance(structured, dict):
            shadowed = set(structured) & _RESERVED_ENVELOPE_KEYS
            if shadowed:
                logger.debug(
                    "MCP step '%s' structured content shadows envelope fields: %s",
                    agent.name,
                    ", ".join(sorted(shadowed)),
                )
            envelope.update(
                {key: value for key, value in structured.items() if key not in shadowed}
            )
        return envelope


def _render_arguments(
    renderer: TemplateRenderer,
    value: Any,
    context: dict[str, Any],
    label: str,
) -> Any:
    """Recursively render string leaves of an ``arguments`` mapping.

    Dicts and lists are walked recursively; string leaves are Jinja2-rendered
    against the workflow context and coerced with the set-step ``auto`` rule.
    ``FileString`` values (from the ``!file`` tag) are ``str`` subclasses and
    render like normal templates, yielding plain strings. All other YAML-native
    scalars (int / float / bool / None) pass through unchanged.

    Args:
        renderer: Template renderer instance.
        value: The value to render (dict / list / scalar).
        context: Workflow context for template rendering.
        label: Human-readable label for error messages (grows with nesting).

    Returns:
        The rendered value with the same container shape.
    """
    if isinstance(value, dict):
        return {
            key: _render_arguments(renderer, sub, context, f"{label}.{key}")
            for key, sub in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_render_arguments(renderer, item, context, label) for item in value]
    if isinstance(value, str):
        rendered = renderer.render(value, context)
        return _coerce_auto(rendered, label)
    return value


def _coerce_auto(rendered: str, label: str) -> Any:
    """Coerce a rendered template string using the set-step ``auto`` rule.

    The WHOLE rendered string is YAML-parsed and whatever it parses as is the
    value: a scalar (``"105"`` -> ``int``, ``"true"`` -> ``bool``, ``"null"``
    -> ``None``) or a collection (``"[1, 2]"`` -> ``list``, ``"a: 1"`` ->
    ``dict``). This applies to embedded templates too — ``"1{{ x }}"`` with
    ``x=2`` renders ``"12"`` and becomes the integer ``12``; only renders
    whose text parses as a plain string (e.g. ``"pre-{{ x }}"`` ->
    ``"pre-2"``, multi-word prose) stay strings. Empty and whitespace-only
    renders bind ``""`` rather than ``None``. A render that parses to ``None``
    through any string other than an explicit null marker keeps its raw form,
    so users don't get a surprise null argument.

    Args:
        rendered: The template's rendered string output.
        label: Human-readable label for debug messages.

    Returns:
        The coerced, JSON-safe-by-construction value.
    """
    stripped = rendered.strip()
    if not stripped:
        return ""
    try:
        parsed = _yaml_load(rendered)
    except YAMLError:
        # Best-effort fallback: a malformed render passes the raw string so
        # the MCP tool (or its schema validation) surfaces the issue. Logged
        # at debug level, mirroring the set-step auto rule.
        logger.debug(
            "%s: yaml.safe_load failed for auto-detect; using raw string",
            label,
            exc_info=True,
        )
        return rendered
    if parsed is None and stripped not in _NULL_MARKERS:
        return rendered
    return parsed
