"""Shared, provider-neutral output-schema builders.

This private module contains the core logic for turning
:class:`~conductor.config.schema.OutputField` definitions into
JSON-Schema fragments and prompt-facing schema fragments. Each provider
wraps these helpers with its own error type and message formatting.
"""

from __future__ import annotations

from typing import Any

from conductor.config.schema import OutputField


class SchemaDepthError(Exception):
    """Raised when output schema nesting exceeds the configured maximum depth."""

    def __init__(self, depth: int, max_depth: int) -> None:
        """Initialize with the depth that was exceeded.

        Args:
            depth: The current nesting depth that triggered the limit.
            max_depth: The maximum allowed nesting depth.
        """
        super().__init__(f"Schema nesting depth {depth} exceeds maximum of {max_depth} levels")
        self.depth = depth
        self.max_depth = max_depth


def _check_depth(depth: int, max_depth: int) -> None:
    """Raise :class:`SchemaDepthError` if ``depth > max_depth``.

    Args:
        depth: Current nesting depth.
        max_depth: Maximum allowed nesting depth.

    Raises:
        SchemaDepthError: When the depth limit is exceeded.
    """
    if depth > max_depth:
        raise SchemaDepthError(depth, max_depth)


def build_json_schema_field(
    field: OutputField, *, depth: int = 0, max_depth: int = 10
) -> dict[str, Any]:
    """Build a JSON-Schema fragment for a single ``OutputField``.

    The fragment contains ``type`` and optionally ``description``,
    ``properties`` + ``required`` (for objects), or ``items`` (for arrays).

    Args:
        field: The output field definition.
        depth: Current nesting depth.
        max_depth: Maximum allowed nesting depth.

    Returns:
        A JSON-Schema fragment dictionary.

    Raises:
        _SchemaDepthError: When the depth limit is exceeded.
    """
    _check_depth(depth, max_depth)

    schema: dict[str, Any] = {"type": field.type}

    if field.description:
        schema["description"] = field.description

    if field.type == "object" and field.properties:
        schema["properties"] = build_json_schema_properties(
            field.properties, depth=depth + 1, max_depth=max_depth
        )
        schema["required"] = list(field.properties.keys())

    if field.type == "array" and field.items:
        schema["items"] = build_json_schema_field(field.items, depth=depth + 1, max_depth=max_depth)

    return schema


def build_json_schema_properties(
    fields: dict[str, OutputField], *, depth: int = 0, max_depth: int = 10
) -> dict[str, Any]:
    """Build a JSON-Schema ``properties`` mapping from named ``OutputField`` definitions.

    Args:
        fields: Mapping from field name to output field definition.
        depth: Current nesting depth.
        max_depth: Maximum allowed nesting depth.

    Returns:
        A JSON-Schema ``properties`` object.

    Raises:
        _SchemaDepthError: When the depth limit is exceeded.
    """
    _check_depth(depth, max_depth)

    return {
        name: build_json_schema_field(field, depth=depth, max_depth=max_depth)
        for name, field in fields.items()
    }


def build_prompt_schema_field(
    field: OutputField,
    *,
    field_name: str | None,
    depth: int = 0,
    max_depth: int = 10,
    description_fallback: bool = False,
) -> dict[str, Any]:
    """Build a prompt-facing schema fragment for a single ``OutputField``.

    When ``description_fallback`` is true and the field has no explicit
    description and ``field_name`` is provided, the description is filled
    with ``"The {field_name} field"``. Array items are built with
    ``field_name=None`` so they do not gain a fallback description.

    Args:
        field: The output field definition.
        field_name: The field name used for the fallback description, or
            ``None`` to suppress the fallback.
        depth: Current nesting depth.
        max_depth: Maximum allowed nesting depth.
        description_fallback: Whether to synthesize a description when one
            is not explicitly set.

    Returns:
        A prompt-facing schema fragment dictionary.

    Raises:
        _SchemaDepthError: When the depth limit is exceeded.
    """
    _check_depth(depth, max_depth)

    description = field.description
    if description_fallback and description is None and field_name is not None:
        description = f"The {field_name} field"

    schema: dict[str, Any] = {"type": field.type}
    if description:
        schema["description"] = description

    if field.type == "object" and field.properties:
        schema["properties"] = build_prompt_schema_properties(
            field.properties,
            depth=depth + 1,
            max_depth=max_depth,
            description_fallback=description_fallback,
        )
        schema["required"] = list(field.properties.keys())

    if field.type == "array" and field.items:
        schema["items"] = build_prompt_schema_field(
            field.items,
            field_name=None,
            depth=depth + 1,
            max_depth=max_depth,
            description_fallback=description_fallback,
        )

    return schema


def build_prompt_schema_properties(
    fields: dict[str, OutputField],
    *,
    depth: int = 0,
    max_depth: int = 10,
    description_fallback: bool = False,
) -> dict[str, Any]:
    """Build a prompt-facing schema mapping from named ``OutputField`` definitions.

    Args:
        fields: Mapping from field name to output field definition.
        depth: Current nesting depth.
        max_depth: Maximum allowed nesting depth.
        description_fallback: Whether to synthesize a description when one
            is not explicitly set.

    Returns:
        A prompt-facing schema mapping.

    Raises:
        _SchemaDepthError: When the depth limit is exceeded.
    """
    _check_depth(depth, max_depth)

    return {
        name: build_prompt_schema_field(
            field,
            field_name=name,
            depth=depth,
            max_depth=max_depth,
            description_fallback=description_fallback,
        )
        for name, field in fields.items()
    }


def build_hermes_legacy_prompt_schema(
    fields: dict[str, OutputField], *, depth: int = 0, max_depth: int = 10
) -> dict[str, Any]:
    """Build the Hermes legacy prompt-facing schema mapping.

    This matches the legacy Hermes provider behavior: descriptions fall back
    to ``"The {field_name} field"`` at the top level, but array items do not
    receive a fallback description. Unlike the generic prompt builder, object
    items inside arrays are emitted with ``properties`` but no ``required``
    key, and array-of-array items collapse to ``{"type": "array"}`` without
    further recursion or description.

    Args:
        fields: Mapping from field name to output field definition.
        depth: Current nesting depth.
        max_depth: Maximum allowed nesting depth.

    Returns:
        The Hermes legacy prompt-facing schema mapping.

    Raises:
        _SchemaDepthError: When the depth limit is exceeded.
    """
    _check_depth(depth, max_depth)

    result: dict[str, Any] = {}
    for field_name, field_def in fields.items():
        field_schema: dict[str, Any] = {"type": field_def.type}

        if field_def.description:
            field_schema["description"] = field_def.description
        else:
            field_schema["description"] = f"The {field_name} field"

        if field_def.type == "object" and field_def.properties:
            field_schema["properties"] = build_hermes_legacy_prompt_schema(
                field_def.properties, depth=depth + 1, max_depth=max_depth
            )
            field_schema["required"] = list(field_def.properties.keys())

        if field_def.type == "array" and field_def.items:
            field_schema["items"] = _build_hermes_legacy_item_schema(
                field_def.items, depth=depth + 1, max_depth=max_depth
            )

        result[field_name] = field_schema

    return result


def _build_hermes_legacy_item_schema(
    field: OutputField, *, depth: int, max_depth: int
) -> dict[str, Any]:
    """Build the Hermes legacy schema fragment for an array item.

    Object items include ``properties`` but no ``required``. Array items of
    any kind collapse to the bare ``{"type": "array"}`` shape with no
    inner recursion or description.

    Args:
        field: The array item output field definition.
        depth: Current nesting depth.
        max_depth: Maximum allowed nesting depth.

    Returns:
        The Hermes legacy item schema fragment.

    Raises:
        SchemaDepthError: When the depth limit is exceeded.
    """
    _check_depth(depth, max_depth)

    item_schema: dict[str, Any] = {"type": field.type}

    if field.description:
        item_schema["description"] = field.description

    if field.type == "object" and field.properties:
        item_schema["properties"] = build_hermes_legacy_prompt_schema(
            field.properties, depth=depth + 1, max_depth=max_depth
        )

    return item_schema
