"""Convert Conductor ``OutputField`` schemas into Pydantic models.

This module provides helpers for recursively translating workflow
``output`` blocks into dynamic Pydantic models suitable for Pydantic AI
``output_type``.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, create_model

from conductor.config.schema import PATTERN_MATCH_TIMEOUT_SECONDS, OutputField


def _reject_bool(value: Any) -> Any:
    """Reject boolean values for Conductor ``number`` and ``integer`` fields.

    Python's ``bool`` is a subclass of ``int``, so plain ``int`` and
    ``int | float`` annotations would accept ``True``/``False``. Conductor's
    output validation treats booleans as incompatible with both ``number`` and
    ``integer``, so this validator mirrors that behaviour.
    """
    if isinstance(value, bool):
        raise ValueError("boolean values are not allowed for numeric type")
    return value


NumberType = Annotated[int | float, BeforeValidator(_reject_bool)]
"""Conductor ``number`` type: accepts integers and floats, rejects booleans."""

IntegerType = Annotated[int, BeforeValidator(_reject_bool)]
"""Conductor ``integer`` type: accepts integers, rejects booleans."""


class _OutputBaseModel(BaseModel):
    """Dynamic model base that tolerates extra output keys.

    ``extra="allow"`` matches Conductor's ``validate_output``, which ignores
    undeclared keys rather than rejecting them. Tool-schema sanitization is
    performed separately in :func:`agent_builder.build_agent` so that the
    schema attached to the Pydantic AI tool definition contains only the
    agreed structural keywords.
    """

    model_config = ConfigDict(extra="allow")


def _convert_anyof_nullable(node: dict[str, Any]) -> dict[str, Any]:
    """Convert a nullable ``anyOf`` union to a flat schema with ``type: [T, "null"]``.

    Pydantic emits ``{"anyOf": [{"type": "T", ...}, {"type": "null"}]}`` for
    ``T | None``. The output tool schema must not contain ``anyOf``, so this
    helper merges the non-null branch's constraints into a single schema while
    preserving siblings such as ``title`` or ``description``.

    The value branch may itself be a union (e.g. Conductor ``number`` is
    ``int | float``), in which case pydantic emits a nested ``anyOf`` with no
    top-level ``type`` key. In that case the inner union types are collected
    into ``type: [T1, T2, "null"]`` when every inner branch declares a type;
    otherwise the original ``anyOf`` node is returned unchanged so the null
    branch is never silently dropped.
    """
    any_of = node.get("anyOf", [])
    if len(any_of) != 2:
        return node

    null_branch: dict[str, Any] | None = None
    value_branch: dict[str, Any] | None = None
    for branch in any_of:
        if isinstance(branch, dict) and branch.get("type") == "null":
            null_branch = branch
        elif isinstance(branch, dict):
            value_branch = branch

    if null_branch is None or value_branch is None:
        return node

    merged = dict(value_branch)
    value_type = merged.pop("type", None)
    if value_type is not None:
        merged["type"] = [value_type, "null"]
    elif "anyOf" in merged:
        inner = merged.pop("anyOf")
        types = [b["type"] for b in inner if isinstance(b, dict) and "type" in b]
        if len(types) == len(inner):
            merged["type"] = [*types, "null"]
        else:
            merged["anyOf"] = [*inner, null_branch]
    else:
        return node

    for key, val in node.items():
        if key != "anyOf":
            merged[key] = val

    return merged


def _sanitize_json_schema(schema: dict[str, Any]) -> None:
    """Recursively sanitize a JSON schema in place.

    Removes ``default``, ``$ref``, and ``$defs`` keywords, strips the raw
    non-standard ``ge``/``le`` keys pydantic emits for union numeric ranges,
    and flattens nullable ``anyOf`` unions so the generated Pydantic-AI tool
    schema contains only the agreed structural keywords.
    """
    defs = schema.pop("$defs", {})
    _sanitize_schema_node(schema, defs)
    schema.pop("$defs", None)


def _sanitize_schema_node(node: Any, defs: dict[str, Any]) -> None:
    """Recursively sanitize a JSON schema node in place.

    ``$ref`` targets that cannot be resolved are left untouched defensively;
    the caller in ``agent_builder.py`` already has the complete ``$defs`` view
    at the time of sanitization, so unresolved references should not occur.
    """
    if isinstance(node, dict):
        nested_defs = node.pop("$defs", None)
        if nested_defs:
            defs = {**defs, **nested_defs}

        if "$ref" in node:
            ref_name = node["$ref"].split("/")[-1]
            if ref_name in defs:
                inlined = copy.deepcopy(defs[ref_name])
                node.clear()
                node.update(inlined)
                _sanitize_schema_node(node, defs)
            return

        if "anyOf" in node:
            for branch in node["anyOf"]:
                if isinstance(branch, dict) and "$ref" in branch:
                    _sanitize_schema_node(branch, defs)
            converted = _convert_anyof_nullable(node)
            if converted is not node:
                node.clear()
                node.update(converted)

        node.pop("default", None)
        node.pop("ge", None)
        node.pop("le", None)

        for value in node.values():
            _sanitize_schema_node(value, defs)
    elif isinstance(node, list):
        for item in node:
            _sanitize_schema_node(item, defs)


def _make_enum_validator(enum_values: list[Any]) -> Any:
    """Return an AfterValidator that enforces enum membership.

    Matches the shared semantics used by ``validate_output``: plain Python
    membership is used (so ``1.0`` satisfies a number enum ``[1]``), and
    booleans are rejected for non-boolean fields by the base-type validators
    before reaching this validator.
    """

    def _validate(value: Any) -> Any:
        if value in enum_values:
            return value
        raise ValueError(f"{value!r} is not one of {enum_values!r}")

    return _validate


def _make_pattern_validator(compiled: Any) -> Any:
    """Return an AfterValidator that runs ``regex.search`` with a timeout.

    Mirrors ``validate_output`` so Python-only regex constructs (lookarounds,
    backreferences) are evaluated by Python's regex engine, not pydantic's
    Rust-based default. A match that exceeds the wall-clock bound is raised as
    a ``ValueError`` so pydantic-ai retries the output instead of hanging on a
    pathological pattern.
    """

    def _validate(value: Any) -> Any:
        if not isinstance(value, str):
            raise ValueError("pattern can only be applied to strings")
        try:
            if compiled.search(value, timeout=PATTERN_MATCH_TIMEOUT_SECONDS) is None:
                raise ValueError(f"value does not match pattern {compiled.pattern!r}")
        except TimeoutError as exc:
            raise ValueError(
                f"pattern match exceeded the {PATTERN_MATCH_TIMEOUT_SECONDS}s time limit "
                "(catastrophic backtracking risk)"
            ) from exc
        return value

    return _validate


def _to_pascal(snake: str) -> str:
    """Convert a snake_case identifier to PascalCase.

    Used to build readable, unique nested model names from the parent
    model name and the field key.

    Args:
        snake: The snake_case identifier to convert.

    Returns:
        The PascalCase equivalent.
    """
    return "".join(part.capitalize() for part in snake.split("_"))


def _field_json_schema_extra(field: OutputField) -> dict[str, Any] | None:
    """Build ``json_schema_extra`` advertising Conductor constraints.

    Pydantic AI attaches this to the generated tool schema so the model sees
    the same ``enum``/``pattern``/range keywords as the shared JSON Schema
    builders. Length constraints are already emitted by pydantic from the
    ``min_length``/``max_length`` ``Field`` kwargs, so they are not duplicated
    here. Nullable fields advertise ``None`` in the enum so the schema matches
    the values accepted at validation time.
    """
    extra: dict[str, Any] = {}
    if field.enum is not None:
        extra["enum"] = [*field.enum, None] if field.nullable else field.enum
    if field.pattern is not None:
        extra["pattern"] = field.pattern
    if field.minimum is not None:
        extra["minimum"] = field.minimum
    if field.maximum is not None:
        extra["maximum"] = field.maximum
    return extra if extra else None


def _wrap_scalar_field_type(field: OutputField, base_type: Any) -> Any:
    """Wrap a scalar base type with validators and nullability.

    Adds enum/pattern validators via ``AfterValidator`` and unions with
    ``None`` for nullable fields. Optional fields are handled at the
    ``Field`` level (``default=None``); the type union only changes when the
    field is explicitly nullable.
    """
    annotated: Any = base_type
    if field.enum is not None:
        annotated = Annotated[
            annotated,
            AfterValidator(_make_enum_validator(field.enum)),
        ]
    if field.compiled_pattern is not None:
        annotated = Annotated[
            annotated,
            AfterValidator(_make_pattern_validator(field.compiled_pattern)),
        ]

    if field.nullable:
        annotated = annotated | None

    return annotated


def _build_field_info(field: OutputField) -> Any:
    """Build a Pydantic ``FieldInfo`` for a Conductor output field.

    Length/range constraints are enforced by ``Field`` itself (no regex
    involved). Optional fields get ``default=None``; required fields stay
    required with ``default=...``. The description is included when present,
    and constraint metadata is mirrored in ``json_schema_extra`` so the
    generated tool schema advertises them.
    """
    extra = _field_json_schema_extra(field)
    field_kwargs: dict[str, Any] = {}
    if field.description:
        field_kwargs["description"] = field.description
    if extra is not None:
        field_kwargs["json_schema_extra"] = extra

    if field.type == "string":
        if field.minLength is not None:
            field_kwargs["min_length"] = field.minLength
        if field.maxLength is not None:
            field_kwargs["max_length"] = field.maxLength
    elif field.type == "number":
        if field.minimum is not None:
            field_kwargs["ge"] = field.minimum
        if field.maximum is not None:
            field_kwargs["le"] = field.maximum

    field_kwargs["default"] = ... if field.required else None

    return Field(**field_kwargs)


def _build_array_item_type(
    field: OutputField,
    *,
    prefix: str = "Output",
    depth: int = 0,
    max_depth: int = 10,
) -> Any:
    """Build a Pydantic type for an array item, applying constraints.

    Array elements are validated individually, so the item's ``nullable`` flag
    and scalar constraints (enum, pattern, length, range) are applied exactly
    like top-level scalar fields. Object and array items are built recursively;
    their nullability is applied as a union with ``None`` when requested.
    """
    if depth > max_depth:
        raise ValueError(f"Maximum output schema nesting depth of {max_depth} exceeded")

    base_type = _map_output_field_type(
        field,
        prefix=prefix,
        depth=depth,
        max_depth=max_depth,
    )

    if field.type in ("string", "number", "boolean"):
        base_type = _wrap_scalar_field_type(field, base_type)
        field_info = _build_field_info(field)
        return Annotated[base_type, field_info]

    if field.nullable:
        base_type = base_type | None

    return base_type


def _map_output_field_type(
    field: OutputField,
    *,
    prefix: str = "Output",
    depth: int = 0,
    max_depth: int = 10,
) -> Any:
    """Return a Python type for a single Conductor output field.

    Recursively handles scalar types, arrays, and object properties.
    Object fields with declared properties become dynamically created
    Pydantic models named using ``prefix``. Free-form objects become
    ``dict[str, Any]``; arrays without items become ``list[Any]``.

    Args:
        field: The output field definition.
        prefix: Base name used for nested dynamic models created from object
            fields. The caller should pass a unique prefix to avoid name
            collisions.
        depth: Current nesting depth. The root schema starts at ``0``.
        max_depth: Maximum allowed nesting depth. Mirrors the depth limit in
            ``conductor.providers._schema`` so that schemas that fail the
            JSON-schema builder also fail here.

    Returns:
        A Python type suitable for a Pydantic model field annotation.

    Raises:
        ValueError: If the nesting depth exceeds ``max_depth``.
    """
    if depth > max_depth:
        raise ValueError(f"Maximum output schema nesting depth of {max_depth} exceeded")

    if field.type == "string":
        return str
    if field.type == "integer":
        return IntegerType
    if field.type == "number":
        return NumberType
    if field.type == "boolean":
        return bool
    if field.type == "array":
        if field.items:
            item_type = _build_array_item_type(
                field.items,
                prefix=f"{prefix}Item",
                depth=depth + 1,
                max_depth=max_depth,
            )
            return list[item_type]
        return list[Any]
    if field.type == "object":
        if field.properties:
            return _build_pydantic_model(
                prefix,
                field.properties,
                description=field.description,
                depth=depth + 1,
                max_depth=max_depth,
            )
        return dict[str, Any]
    return Any


def _build_pydantic_model(
    name: str,
    fields: dict[str, OutputField],
    *,
    description: str | None = None,
    depth: int = 0,
    max_depth: int = 10,
) -> type[BaseModel]:
    """Create a dynamic Pydantic model from a Conductor output schema.

    This is the internal recursive implementation used by
    :func:`output_schema_to_pydantic_model`.

    Args:
        name: The generated model name.
        fields: Mapping from output key to its Conductor ``OutputField``.
        description: Optional description applied to the generated model as
            ``__doc__`` so it appears in the JSON schema.
        depth: Current nesting depth.
        max_depth: Maximum allowed nesting depth.

    Returns:
        A new Pydantic ``BaseModel`` subclass matching the schema.

    Raises:
        ValueError: If the nesting depth exceeds ``max_depth``.
    """
    if depth > max_depth:
        raise ValueError(f"Maximum output schema nesting depth of {max_depth} exceeded")

    model_fields: dict[str, Any] = {}
    for field_name, field in fields.items():
        nested_prefix = f"{name}{_to_pascal(field_name)}"
        field_type = _map_output_field_type(
            field,
            prefix=nested_prefix,
            depth=depth,
            max_depth=max_depth,
        )
        field_type = _wrap_scalar_field_type(field, field_type)
        field_info = _build_field_info(field)
        model_fields[field_name] = (field_type, field_info)

    return create_model(
        name,
        **model_fields,
        __base__=_OutputBaseModel,
        __doc__=description,
    )


def output_schema_to_pydantic_model(
    name: str,
    fields: dict[str, OutputField] | None,
    *,
    max_depth: int = 10,
) -> type[BaseModel] | None:
    """Create a dynamic Pydantic model from a Conductor output schema.

    Args:
        name: The generated model name (e.g., the agent name + "Output").
        fields: Mapping from output key to its Conductor ``OutputField``.
            An empty or ``None`` mapping produces no model; the caller can
            treat that as "no structured output required".
        max_depth: Maximum allowed nesting depth for nested structures.

    Returns:
        A new Pydantic ``BaseModel`` subclass matching the schema, or
        ``None`` when ``fields`` is empty or ``None``.

    Raises:
        ValueError: If the nesting depth exceeds ``max_depth``.
    """
    if not fields:
        return None
    return _build_pydantic_model(name, fields, depth=0, max_depth=max_depth)
