"""Execution for `type: set` workflow steps.

A `set` step renders one or more Jinja2 expressions and binds the rendered,
typed result into the workflow context. There is no LLM call, no subprocess
and no external I/O — these steps are pure context transformations.

Outputs:
- ``value:``  → ``<agent>.output`` is the typed result (scalar / list / dict
  depending on type detection or explicit ``output_type:``).
- ``values:`` → ``<agent>.output.<key>`` for each binding (always a dict).

Type detection (``output_type`` unset / ``auto``):
1. Render the template with Jinja2.
2. Parse the rendered string with ``yaml.safe_load``; fall back to the raw
   string on parse failure or non-JSON-safe types.
3. Empty / whitespace-only rendered strings become ``""`` (not ``None``).

Type detection results are passed through :func:`_to_json_safe`, which
converts ``datetime`` / ``date`` / ``time`` to their ISO-8601 string form and
rejects anything else. This guarantees that checkpoint round-trips and
event-payload serialisation never silently change the stored type.
"""

from __future__ import annotations

import datetime as _dt
import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from conductor.exceptions import ExecutionError
from conductor.executor.template import TemplateRenderer

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef


@dataclass
class SetOutput:
    """Result of a `set` step.

    Attributes:
        value: The final value to store in context. For single ``value:`` this
            is the typed scalar / list / dict; for multi ``values:`` this is a
            ``dict[str, Any]`` of every binding.
        is_multi: ``True`` for multi ``values:`` steps, ``False`` for single
            ``value:`` steps. Used by the engine to decide whether to apply
            ``output:`` schema validation (only meaningful for dict outputs).
        output_type: The effective type label used during coercion (e.g.
            ``"auto"``, ``"string"``, ``"boolean"``). ``"auto"`` for any
            binding that did not have an explicit ``output_type``.
    """

    value: Any
    is_multi: bool
    output_type: str


class SetExecutor:
    """Executes ``type: set`` workflow steps.

    Renders Jinja2 templates against the supplied agent context, coerces the
    rendered strings to typed values, and returns a :class:`SetOutput` for the
    engine to store.

    The renderer instance is reused across invocations to avoid Jinja2
    environment churn.

    Example::

        executor = SetExecutor()
        result = executor.execute(agent, agent_context)
        context.store(agent.name, result.value)
    """

    def __init__(self) -> None:
        """Initialize the SetExecutor with a template renderer."""
        self.renderer = TemplateRenderer()

    def execute(self, agent: AgentDef, context: dict[str, Any]) -> SetOutput:
        """Render and coerce the step's bindings.

        Args:
            agent: Agent definition with ``type == "set"``.
            context: Workflow context for template rendering.

        Returns:
            :class:`SetOutput` with the final value (scalar / list / dict) and
            metadata used by the engine for event payloads and validation.

        Raises:
            ExecutionError: If a template renders to a value that cannot be
                coerced to the requested ``output_type``, or to a non-JSON-safe
                Python object that we cannot normalize.
            TemplateError: If a template fails to render (undefined variable,
                syntax error, etc.) — propagated from the renderer.
        """
        # Both branches are guaranteed by the schema validator: exactly one of
        # value / values is non-None.
        if agent.values is not None:
            rendered_bindings: dict[str, Any] = {}
            for key, template in agent.values.items():
                # All bindings render against the *original* pre-step context;
                # later bindings can't reference earlier ones (per the issue's
                # resolution). Users who need ordered dependencies should chain
                # multiple set steps.
                rendered_bindings[key] = self._render_and_coerce(
                    template,
                    context,
                    output_type="auto",
                    label=f"set step '{agent.name}' values.{key}",
                )
            return SetOutput(value=rendered_bindings, is_multi=True, output_type="auto")

        assert agent.value is not None
        output_type = agent.output_type or "auto"
        result = self._render_and_coerce(
            agent.value,
            context,
            output_type=output_type,
            label=f"set step '{agent.name}' value",
        )
        return SetOutput(value=result, is_multi=False, output_type=output_type)

    def _render_and_coerce(
        self,
        template: str,
        context: dict[str, Any],
        *,
        output_type: str,
        label: str,
    ) -> Any:
        """Render a template, coerce to the requested type, and JSON-normalise.

        Args:
            template: Jinja2 template string.
            context: Workflow context for rendering.
            output_type: One of ``auto``, ``string``, ``number``, ``integer``,
                ``boolean``, ``list``, ``dict``.
            label: Human-readable label used in error messages.

        Returns:
            The coerced, JSON-safe value.

        Raises:
            ExecutionError: If coercion fails or yields a non-JSON-safe value.
        """
        rendered = self.renderer.render(template, context)
        coerced = _coerce(rendered, output_type, label)
        return _to_json_safe(coerced, label)


_TRUE_STRINGS = frozenset({"true", "1", "yes", "y", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "n", "off"})


# Shared ruamel.yaml loader. `safe` mode is the documented equivalent of
# `yaml.safe_load` from PyYAML — no Python-object construction, no arbitrary
# tag instantiation. We construct it once because YAML() instances are cheap
# but reusable.
_YAML_LOADER = YAML(typ="safe", pure=True)


def _yaml_load(text: str) -> Any:
    """Parse *text* with ruamel.yaml in safe mode.

    Mirrors PyYAML's ``yaml.safe_load`` semantics: returns Python-native
    types only and never instantiates arbitrary tagged objects. Used by the
    auto type detector and the explicit list/dict coercion path.
    """
    return _YAML_LOADER.load(io.StringIO(text))


def _coerce(rendered: str, output_type: str, label: str) -> Any:
    """Coerce a rendered template string to the requested type.

    See module docstring for the rules. ``label`` is woven into the error
    messages so users can find the offending step / binding quickly.

    Args:
        rendered: The template's rendered string output.
        output_type: One of ``auto``, ``string``, ``number``, ``integer``,
            ``boolean``, ``list``, ``dict``.
        label: Human-readable label for error messages.

    Returns:
        The coerced value. Type depends on ``output_type``.

    Raises:
        ExecutionError: If coercion fails for an explicit ``output_type``.
    """
    if output_type == "string":
        return rendered

    stripped = rendered.strip()

    if output_type == "auto":
        if not stripped:
            return ""
        try:
            parsed = _yaml_load(rendered)
        except YAMLError:
            return rendered
        # YAML interprets a bare URL-like string oddly in rare cases; if the
        # parse result is None and the rendered string isn't an explicit
        # null marker, keep it as a string (avoids surprise null binds).
        if parsed is None and stripped not in {"null", "~", "Null", "NULL"}:
            return rendered
        return parsed

    if output_type == "boolean":
        s = stripped.lower()
        if s in _TRUE_STRINGS:
            return True
        if s in _FALSE_STRINGS:
            return False
        raise ExecutionError(
            f"{label}: cannot coerce {rendered!r} to boolean",
            suggestion=(
                "Expected one of: true/false, 1/0, yes/no, y/n, on/off (case-insensitive)."
            ),
        )

    if output_type == "integer":
        try:
            return int(stripped)
        except (TypeError, ValueError) as exc:
            raise ExecutionError(
                f"{label}: cannot coerce {rendered!r} to integer",
                suggestion="Render an integer literal (e.g. '42') for output_type: integer.",
            ) from exc

    if output_type == "number":
        # Prefer int for integral renders, fall back to float.
        try:
            return int(stripped)
        except (TypeError, ValueError):
            pass
        try:
            return float(stripped)
        except (TypeError, ValueError) as exc:
            raise ExecutionError(
                f"{label}: cannot coerce {rendered!r} to number",
                suggestion=(
                    "Render a numeric literal (e.g. '42' or '3.14') for output_type: number."
                ),
            ) from exc

    if output_type in ("list", "dict"):
        try:
            parsed = _yaml_load(rendered)
        except YAMLError as exc:
            raise ExecutionError(
                f"{label}: cannot parse {rendered!r} for output_type: {output_type}",
                suggestion=(
                    "Render valid YAML/JSON (e.g. '[1, 2]' for a list, '{a: 1}' for a dict)."
                ),
            ) from exc
        expected = list if output_type == "list" else dict
        if not isinstance(parsed, expected):
            raise ExecutionError(
                f"{label}: expected output_type: {output_type} but got "
                f"{type(parsed).__name__}: {rendered!r}",
            )
        return parsed

    # Schema validator restricts output_type to the enumerated literals so we
    # only reach here if a new literal is added without updating this function.
    raise ExecutionError(  # pragma: no cover
        f"{label}: unknown output_type: {output_type!r}",
        suggestion=(
            "output_type must be one of: auto, string, number, integer, boolean, list, dict."
        ),
    )


_JSON_SCALAR_TYPES = (type(None), bool, int, float, str)


def _to_json_safe(value: Any, label: str) -> Any:
    """Recursively normalise a value to JSON-safe Python types.

    Set-step outputs flow through checkpoint serialisation, web-dashboard
    payloads, and JSONL event logs — all of which assume JSON-safe values.
    YAML's safe loader can produce ``datetime`` / ``date`` / ``time``
    objects from strings like ``"2024-01-01"``; we convert those to ISO 8601
    strings rather than allow the type to silently change on a resume.

    Args:
        value: A value from template coercion.
        label: Human-readable label for error messages.

    Returns:
        ``None``, ``bool``, ``int``, ``float``, ``str``, ``list`` or ``dict``
        (recursively). Container contents are recursively normalised.

    Raises:
        ExecutionError: If ``value`` contains a Python object that we cannot
            map to a JSON-safe form (e.g. a custom class).
    """
    # bool is a subclass of int, so handle it first for the right narrowing.
    if isinstance(value, bool):
        return value
    if isinstance(value, _JSON_SCALAR_TYPES):
        return value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(item, label) for item in value]
    if isinstance(value, dict):
        normalised: dict[str, Any] = {}
        for key, sub in value.items():
            if not isinstance(key, str):
                # JSON object keys must be strings — coerce to string and
                # surface the conversion so users notice unexpected key types.
                key = str(key)
            normalised[key] = _to_json_safe(sub, label)
        return normalised
    raise ExecutionError(
        f"{label}: rendered value of type {type(value).__name__} is not JSON-safe "
        "and cannot be stored in workflow context",
        suggestion=(
            "Render a JSON-safe value (string, number, boolean, list, dict, or null). "
            "If the value is a date/time, render it as an ISO 8601 string."
        ),
    )
