"""Tool generation: ``InputDef`` -> JSON Schema, and assembling the
generated ``mcp.types.Tool`` (FR3, FR5, DD5, E7-T4).

Known fidelity gap.

``InputDef`` (``config/schema.py``) has no ``enum``, no distinct
``integer`` type, and no ``items``/``properties`` for structured types, so
an ``array`` or ``object`` input publishes as an untyped
``{"type": "array"}`` / ``{"type": "object"}`` — valid JSON Schema, but one
that constrains nothing about the shape inside. This is an asymmetry
inside Conductor's own schema, not an MCP limitation: ``OutputField``
(``config/schema.py``) already carries ``items``, ``properties``,
``enum``, ``pattern``, ``minimum``/``maximum``, and more. This module does
not invent structure the workflow author did not declare; enriching
``InputDef`` toward ``OutputField``'s vocabulary is a natural follow-up
that would improve ``conductor show``, the Fleet Manager's New Run screen,
and this feature all at once (see the design's *Key Components -> 2. Tool
generator*).

No ``outputSchema`` is published (DD5): ``WorkflowConfig.output`` is
``dict[str, str]`` Jinja2 templates with no declared types, so an honest
schema cannot be derived from it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp.types import Tool, ToolAnnotations

from conductor.config.schema import InputDef, McpConfig
from conductor.config.validator import MCP_RESERVED_WAIT_SECONDS_INPUT
from conductor.mcp.serve.sanitize import sanitize_description

# The reserved parameter every generated tool accepts (FR5). Re-exported
# under this module's own name because callers of this module think in
# terms of "the tool generator's reserved parameter", not "the validator's
# constant" -- the two happen to be the same string so a workflow-declared
# collision is caught before it ever reaches tool generation (E6-T2,
# `config/validator.py::_validate_mcp_exposure`), and the catalogue builder
# repeats the same check defensively (E7-T6) since a third-party registry's
# workflow is never guaranteed to have been run through `conductor validate`.
WAIT_SECONDS_PARAM = MCP_RESERVED_WAIT_SECONDS_INPUT

_WAIT_SECONDS_DESCRIPTION = (
    "0 = return immediately; >0 = wait up to N seconds for a terminal run "
    "state (capped by the server's --max-wait-seconds ceiling regardless of "
    "the value requested); omitted defers to this workflow's declared "
    "mcp.mode."
)


def input_def_to_property(input_def: InputDef) -> dict[str, Any]:
    """Map one ``InputDef`` to a JSON Schema property object.

    Direct and total for scalars: ``type`` carries the identical
    vocabulary MCP/JSON Schema uses, ``default`` and ``description`` pass
    through unchanged when present. ``description`` is YAML-authored,
    third-party-registry-controlled text (same attack surface as the
    workflow's own top-level description, NFR4), so it is sanitized here
    too before it reaches a tool's schema. See the module docstring for
    the known ``array``/``object`` fidelity gap.
    """
    prop: dict[str, Any] = {"type": input_def.type}
    if input_def.description:
        sanitized = sanitize_description(input_def.description)
        if sanitized:
            prop["description"] = sanitized
    if input_def.default is not None:
        prop["default"] = input_def.default
    return prop


def build_input_schema(inputs: Mapping[str, InputDef]) -> dict[str, Any]:
    """Build a tool's ``inputSchema`` from a workflow's resolved ``input:``.

    Every declared input becomes a property; ``required: true`` entries
    populate the schema's ``required`` array (sorted for deterministic
    output). The reserved ``_wait_seconds`` parameter (FR5) is always
    injected, last.

    Raises:
        ValueError: if ``inputs`` already contains the reserved
            ``_wait_seconds`` key. Catalogue building (E7-T6) must reject
            such a workflow before it ever reaches tool generation; this
            is a defensive backstop, not the primary enforcement point.
    """
    if WAIT_SECONDS_PARAM in inputs:
        raise ValueError(
            f"Input {WAIT_SECONDS_PARAM!r} collides with the reserved parameter the "
            "tool generator injects (FR5). The catalogue builder must reject this "
            "workflow before calling build_input_schema."
        )

    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, input_def in inputs.items():
        properties[name] = input_def_to_property(input_def)
        if input_def.required:
            required.append(name)

    properties[WAIT_SECONDS_PARAM] = {
        "type": "number",
        "description": _WAIT_SECONDS_DESCRIPTION,
    }

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = sorted(required)
    return schema


def describe_with_mode(description: str, mcp: McpConfig) -> str:
    """Append the workflow's invocation mode (and estimate, if declared) to
    an already-sanitized description, per the design's API Contracts
    example: ``"<description> (async; ~8 min)"``.
    """
    suffix = mcp.mode
    if mcp.estimated_minutes:
        suffix = f"{suffix}; ~{mcp.estimated_minutes} min"
    if not description:
        return f"({suffix})"
    return f"{description} ({suffix})"


def build_tool(
    name: str,
    *,
    description: str,
    inputs: Mapping[str, InputDef],
    mcp: McpConfig,
) -> Tool:
    """Assemble one ``mcp.types.Tool`` for an exposed workflow (FR3, DD5).

    Args:
        name: The final, qualified tool name (``naming.py`` has already
            resolved collisions and applied ``--tool-prefix``).
        description: The workflow's description, already sanitized
            (``sanitize.py``) — this function only appends the mode/
            estimate suffix, it does not sanitize.
        inputs: The workflow's resolved ``input:`` definitions.
        mcp: The workflow's resolved ``mcp:`` block.

    Returns:
        A fully-formed ``Tool`` with ``inputSchema`` and ``annotations``
        set, and no ``outputSchema`` (DD5).
    """
    input_schema = build_input_schema(inputs)
    annotations = ToolAnnotations(
        readOnlyHint=mcp.read_only,
        destructiveHint=mcp.destructive,
    )
    return Tool(
        name=name,
        description=describe_with_mode(description, mcp),
        inputSchema=input_schema,
        annotations=annotations,
    )
