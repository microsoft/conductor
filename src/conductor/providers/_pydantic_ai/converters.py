"""Convert Conductor ``OutputField`` schemas into Pydantic models.

This module provides helpers for recursively translating workflow
``output`` blocks into dynamic Pydantic models suitable for Pydantic AI
``output_type``.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, create_model

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
        field_info = (
            Field(description=field.description) if field.description else Field(default=...)
        )
        model_fields[field_name] = (field_type, field_info)  # type: ignore[assignment]

    return create_model(
        name,
        **model_fields,
        __base__=BaseModel,
        __doc__=description,
        __config__=ConfigDict(extra="allow"),
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
