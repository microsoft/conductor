"""Notification step executor for Conductor workflows.

Renders a notification step's payload via Jinja2, validates each rendered
value against the declared :class:`OutputField` schema, and returns a
fully-built envelope ready to emit as a ``notification`` event.

Notifications are a fire-and-forget visibility primitive — there is no
provider call and no side effect beyond constructing the envelope. The
engine is responsible for actually emitting the event.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from conductor.exceptions import ValidationError
from conductor.executor.template import TemplateRenderer

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef, NotificationsConfig, OutputField


# Same regex used in schema.py for namespace validation. Kept local so the
# executor can slug a workflow name into a valid namespace at envelope-build
# time without importing the regex constant cross-module.
_NAMESPACE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$")


def slug_namespace(workflow_name: str) -> str:
    """Slugify *workflow_name* into a valid dotted-identifier namespace.

    Lowercases, replaces any character outside ``[a-z0-9_.]`` with ``_``,
    and prepends ``_`` if the result would otherwise start with a digit
    or a dot. Used to derive a default namespace when the workflow author
    did not set ``notifications.namespace`` explicitly.
    """
    s = re.sub(r"[^a-z0-9_.]", "_", workflow_name.lower())
    if not s or not re.match(r"^[a-z_]", s):
        s = "_" + s
    return s


def build_step_path(subworkflow_path: list[str], step_name: str) -> str:
    """Build the dotted step-path component of an ``emission_id``.

    Joins the engine's ``subworkflow_path`` slot keys with the step name
    using ``/`` (matches the dashboard's existing path convention).
    """
    if subworkflow_path:
        return "/".join([*subworkflow_path, step_name])
    return step_name


def build_emission_id(run_id: str, step_path: str, iteration: int) -> str:
    """Build a stable ``emission_id`` for a single notification emission.

    Format: ``<run_id>:<step_path>:<iteration>``. Deterministic across
    resume and replay so the first downstream consumer can dedupe.
    """
    return f"{run_id}:{step_path}:{iteration}"


def _validate_value(value: Any, field: OutputField, path: str) -> None:
    """Validate *value* against an :class:`OutputField` schema.

    Recurses into array items and object properties when the field
    declares them. Raises :class:`ValidationError` on mismatch with a
    dotted ``path`` identifying the offending location in the payload.
    """
    expected = field.type
    if expected == "string":
        if not isinstance(value, str):
            raise ValidationError(
                f"Notification payload field '{path}' must be a string, got {type(value).__name__}"
            )
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(
                f"Notification payload field '{path}' must be a number, got {type(value).__name__}"
            )
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise ValidationError(
                f"Notification payload field '{path}' must be a boolean, got {type(value).__name__}"
            )
    elif expected == "array":
        if not isinstance(value, list):
            raise ValidationError(
                f"Notification payload field '{path}' must be an array, got {type(value).__name__}"
            )
        if field.items is not None:
            for i, item in enumerate(value):
                _validate_value(item, field.items, f"{path}[{i}]")
    elif expected == "object":
        if not isinstance(value, dict):
            raise ValidationError(
                f"Notification payload field '{path}' must be an object, got {type(value).__name__}"
            )
        if field.properties is not None:
            for prop_name, prop_schema in field.properties.items():
                if prop_name in value:
                    _validate_value(value[prop_name], prop_schema, f"{path}.{prop_name}")


def _coerce_rendered(value: Any, field_type: str) -> Any:
    """Best-effort coerce a Jinja2-rendered string to the declared type.

    Jinja2 rendering produces a string. For non-string declared types we
    try ``json.loads`` so ``"42"`` becomes ``int(42)`` and ``"[1,2]"``
    becomes a list. Falls back to the raw value if parsing fails or the
    value is already non-string (e.g. a dict literal passed through).
    """
    if field_type == "string":
        return value
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        raise ValidationError(
            f"Failed to coerce rendered value {value!r} to declared type '{field_type}'. "
            "Check that the template produces a valid JSON-serialisable value for this field."
        ) from None


class NotificationExecutor:
    """Builds the envelope for a ``type=notification`` step.

    The executor is pure — it renders templates, validates types, and
    returns a dict ready to ship as the ``data`` of a ``notification``
    event. It does not call into the event emitter; the engine does that.
    """

    def __init__(self) -> None:
        self._renderer = TemplateRenderer()

    def build_envelope(
        self,
        agent: AgentDef,
        notifications_config: NotificationsConfig,
        context: dict[str, Any],
        *,
        workflow_name: str,
        run_id: str,
        subworkflow_path: list[str],
        iteration: int,
        correlation: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the full notification envelope for emission.

        Raises:
            ValidationError: If the referenced type is undeclared, if
                payload keys don't match the declared schema, or if any
                rendered value fails type validation.
        """
        if agent.emit is None or agent.payload is None:
            raise ValidationError(
                f"Notification step '{agent.name}' is missing 'emit' or 'payload' "
                "(this should have been caught by schema validation)"
            )

        type_name = agent.emit
        type_def = notifications_config.types.get(type_name)
        if type_def is None:
            available = ", ".join(sorted(notifications_config.types.keys())) or "(none)"
            raise ValidationError(
                f"Notification step '{agent.name}' references undeclared notification "
                f"type '{type_name}'. Declared types: {available}",
                suggestion=(
                    f"Add a '{type_name}' entry under workflow.notifications.types, "
                    "or change the step to reference an existing type."
                ),
            )

        declared_fields = set(type_def.payload.keys())
        provided_fields = set(agent.payload.keys())
        missing = declared_fields - provided_fields
        extra = provided_fields - declared_fields
        if missing or extra:
            parts = []
            if missing:
                parts.append(f"missing field(s): {', '.join(sorted(missing))}")
            if extra:
                parts.append(f"unexpected field(s): {', '.join(sorted(extra))}")
            raise ValidationError(
                f"Notification step '{agent.name}' payload for type '{type_name}' "
                f"does not match declared schema: {'; '.join(parts)}",
                suggestion=(f"Declared fields: {', '.join(sorted(declared_fields)) or '(none)'}"),
            )

        rendered_payload: dict[str, Any] = {}
        for field_name, template_value in agent.payload.items():
            field_schema = type_def.payload[field_name]
            if isinstance(template_value, str):
                try:
                    rendered = self._renderer.render(template_value, context)
                except Exception as e:
                    raise ValidationError(
                        f"Failed to render payload field '{field_name}' for "
                        f"notification step '{agent.name}': {e}"
                    ) from e
                try:
                    rendered = _coerce_rendered(rendered, field_schema.type)
                except ValidationError as e:
                    raise ValidationError(
                        f"Payload field '{field_name}' for notification step "
                        f"'{agent.name}': {e}"
                    ) from e
            else:
                rendered = template_value
            _validate_value(rendered, field_schema, field_name)
            rendered_payload[field_name] = rendered

        namespace = notifications_config.namespace or slug_namespace(workflow_name)
        if not _NAMESPACE_PATTERN.match(namespace):
            raise ValidationError(
                f"Resolved namespace '{namespace}' is not a valid dotted identifier",
                suggestion=(
                    "Set workflow.notifications.namespace explicitly to a valid value "
                    "(e.g. 'my_pkg.my_workflow')."
                ),
            )

        step_path = build_step_path(subworkflow_path, agent.name)
        emission_id = build_emission_id(run_id, step_path, iteration)
        schema_id = f"{namespace}.{type_name}@{type_def.version}"

        return {
            "emission_id": emission_id,
            "schema_id": schema_id,
            "notification_type": type_name,
            "namespace": namespace,
            "version": type_def.version,
            "run_id": run_id,
            "workflow": workflow_name,
            "source_agent": agent.name,
            "subworkflow_path": list(subworkflow_path),
            "correlation": dict(correlation),
            "payload": rendered_payload,
        }
