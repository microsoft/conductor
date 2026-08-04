"""Convert Conductor ``OutputField`` schemas into Pydantic models.

This module provides helpers for recursively translating workflow
``output`` blocks into dynamic Pydantic models suitable for Pydantic AI
``output_type``.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, create_model

from conductor.config.schema import OutputField


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


class _NoDefaultBaseModel(BaseModel):
    """Dynamic model base that strips ``default`` keys from JSON schemas.

    Pydantic v2 emits ``"default": null`` for ``Field(default=None)``.
    Pydantic AI's tool schema must not include the JSON Schema ``default``
    keyword (it is not allowed on tool parameters), so this base removes it
    recursively from the generated schema, including nested ``$defs``.
    """

    model_config = ConfigDict(extra="allow")

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:
        schema = handler(core_schema)
        _strip_default_keys(schema)
        return schema


def _strip_default_keys(schema: Any) -> None:
    """Recursively remove ``default`` keys from a JSON schema dict in place."""
    if isinstance(schema, dict):
        schema.pop("default", None)
        for value in schema.values():
            _strip_default_keys(value)
    elif isinstance(schema, list):
        for item in schema:
            _strip_default_keys(item)


def _make_enum_validator(enum_values: list[Any], field_type: str) -> Any:
    """Return an AfterValidator that enforces enum membership.

    Matches the shared semantics used by ``validate_output``:

    - A boolean value only passes when the field type is ``boolean`` and the
      value is present in the enum.
    - For all other values, plain Python membership is used (so ``1.0``
      satisfies a number enum ``[1]``).
    """

    def _validate(value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, bool):
            if field_type == "boolean" and value in enum_values:
                return value
            raise ValueError(f"{value!r} is not a valid boolean enum value")
        if value in enum_values:
            return value
        raise ValueError(f"{value!r} is not one of {enum_values!r}")

    return _validate


def _make_pattern_validator(pattern: str) -> Any:
    """Return an AfterValidator that runs Python ``re.search``.

    Mirrors ``validate_output`` so Python-only regex constructs (lookarounds,
    backreferences) are evaluated by Python's regex engine, not pydantic's
    Rust-based default.
    """

    def _validate(value: Any) -> Any:
        if value is None:
            return value
        if not isinstance(value, str):
            raise ValueError("pattern can only be applied to strings")
        if re.search(pattern, value) is None:
            raise ValueError(f"value does not match pattern {pattern!r}")
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
    the same ``enum``/``pattern``/length/range keywords as the shared JSON
    Schema builders.
    """
    extra: dict[str, Any] = {}
    if field.enum is not None:
        extra["enum"] = field.enum
    if field.pattern is not None:
        extra["pattern"] = field.pattern
    if field.minLength is not None:
        extra["minLength"] = field.minLength
    if field.maxLength is not None:
        extra["maxLength"] = field.maxLength
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
    annotated = base_type
    if field.enum is not None:
        annotated = Annotated[
            annotated,
            AfterValidator(_make_enum_validator(field.enum, field.type)),
        ]
    if field.pattern is not None:
        annotated = Annotated[annotated, AfterValidator(_make_pattern_validator(field.pattern))]

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
    elif field.type in ("number", "integer"):
        if field.minimum is not None:
            field_kwargs["ge"] = field.minimum
        if field.maximum is not None:
            field_kwargs["le"] = field.maximum

    field_kwargs["default"] = ... if field.required else None

    return Field(**field_kwargs)


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
            item_type = _map_output_field_type(
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
        __base__=_NoDefaultBaseModel,
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
