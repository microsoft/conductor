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
        SchemaDepthError: When the depth limit is exceeded.
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
        SchemaDepthError: When the depth limit is exceeded.
    """
    _check_depth(depth, max_depth)

    return {
        name: build_json_schema_field(field, depth=depth, max_depth=max_depth)
        for name, field in fields.items()
    }


def build_prompt_schema_field(
    field: OutputField,
    *,
    depth: int = 0,
    max_depth: int = 10,
) -> dict[str, Any]:
    """Build a prompt-facing schema fragment for a single ``OutputField``.

    The fragment contains ``type`` and optionally ``description``,
    ``properties`` + ``required`` (for objects), or ``items`` (for arrays).

    Args:
        field: The output field definition.
        depth: Current nesting depth.
        max_depth: Maximum allowed nesting depth.

    Returns:
        A prompt-facing schema fragment dictionary.

    Raises:
        SchemaDepthError: When the depth limit is exceeded.
    """
    _check_depth(depth, max_depth)

    schema: dict[str, Any] = {"type": field.type}
    if field.description:
        schema["description"] = field.description

    if field.type == "object" and field.properties:
        schema["properties"] = build_prompt_schema_properties(
            field.properties, depth=depth + 1, max_depth=max_depth
        )
        schema["required"] = list(field.properties.keys())

    if field.type == "array" and field.items:
        schema["items"] = build_prompt_schema_field(
            field.items, depth=depth + 1, max_depth=max_depth
        )

    return schema


def build_prompt_schema_properties(
    fields: dict[str, OutputField],
    *,
    depth: int = 0,
    max_depth: int = 10,
) -> dict[str, Any]:
    """Build a prompt-facing schema mapping from named ``OutputField`` definitions.

    Args:
        fields: Mapping from field name to output field definition.
        depth: Current nesting depth.
        max_depth: Maximum allowed nesting depth.

    Returns:
        A prompt-facing schema mapping.

    Raises:
        SchemaDepthError: When the depth limit is exceeded.
    """
    _check_depth(depth, max_depth)

    return {
        name: build_prompt_schema_field(field, depth=depth, max_depth=max_depth)
        for name, field in fields.items()
    }
