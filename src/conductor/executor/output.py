"""Output parsing and validation for agent responses.

This module provides functions for validating agent output against
declared output schemas.
"""

from __future__ import annotations

import re
from typing import Any

from conductor.config.schema import OutputField
from conductor.exceptions import ValidationError


def validate_output(
    content: dict[str, Any],
    schema: dict[str, OutputField],
) -> None:
    """Validate agent output against declared schema.

    Checks that all required fields are present and have the correct types.
    Nested object properties and array items are validated recursively.

    Args:
        content: Agent's output content as a dictionary.
        schema: Expected output schema with field definitions.

    Raises:
        ValidationError: If output doesn't match schema (missing fields or wrong types).

    Example:
        >>> from conductor.config.schema import OutputField
        >>> schema = {"answer": OutputField(type="string")}
        >>> validate_output({"answer": "Hello"}, schema)  # OK
        >>> validate_output({}, schema)  # Raises ValidationError
    """
    for field_name, field_def in schema.items():
        if field_name not in content:
            if not field_def.required:
                continue
            raise ValidationError(
                f"Missing required output field: {field_name}",
                suggestion=f"Ensure agent returns '{field_name}' in output",
            )

        _validate_field(field_name, content[field_name], field_def)


def _check_constraints(field_name: str, value: Any, field_def: OutputField) -> None:
    """Validate scalar constraints (enum, pattern, length, range) for a field.

    Called after the type check has passed, so ``value`` is known to match
    ``field_def.type``. Raises ``ValidationError`` with a suggestion on failure.
    """
    if field_def.enum is not None:
        if isinstance(value, bool):
            if not (field_def.type == "boolean" and value in field_def.enum):
                raise ValidationError(
                    f"Output field '{field_name}' must be one of {field_def.enum!r}, got {value!r}",
                    suggestion=f"Ensure '{field_name}' is one of {field_def.enum!r}",
                )
        elif value not in field_def.enum:
            raise ValidationError(
                f"Output field '{field_name}' must be one of {field_def.enum!r}, got {value!r}",
                suggestion=f"Ensure '{field_name}' is one of {field_def.enum!r}",
            )

    if field_def.pattern is not None and re.search(field_def.pattern, value) is None:
        raise ValidationError(
            f"Output field '{field_name}' does not match pattern '{field_def.pattern}'",
            suggestion=f"Ensure '{field_name}' matches the pattern '{field_def.pattern}'",
        )

    if field_def.type == "string":
        if field_def.minLength is not None and len(value) < field_def.minLength:
            raise ValidationError(
                f"Output field '{field_name}' is shorter than minLength {field_def.minLength}",
                suggestion=f"Ensure '{field_name}' has at least {field_def.minLength} characters",
            )
        if field_def.maxLength is not None and len(value) > field_def.maxLength:
            raise ValidationError(
                f"Output field '{field_name}' is longer than maxLength {field_def.maxLength}",
                suggestion=f"Ensure '{field_name}' has at most {field_def.maxLength} characters",
            )
    elif field_def.type == "number":
        if field_def.minimum is not None and value < field_def.minimum:
            raise ValidationError(
                f"Output field '{field_name}' is below minimum {field_def.minimum}",
                suggestion=f"Ensure '{field_name}' is at least {field_def.minimum}",
            )
        if field_def.maximum is not None and value > field_def.maximum:
            raise ValidationError(
                f"Output field '{field_name}' is above maximum {field_def.maximum}",
                suggestion=f"Ensure '{field_name}' is at most {field_def.maximum}",
            )


def _validate_field(field_name: str, value: Any, field_def: OutputField) -> None:
    """Validate a single value against its output field definition.

    Recursively validates nested object properties and array items so
    ``array<object>`` and deeper combinations are checked at every depth,
    matching the recursion the object branch has always had.

    Args:
        field_name: Field name used in error messages (array items keep the
            parent array's name, matching the existing message style).
        value: The value to validate.
        field_def: The field definition to validate against.

    Raises:
        ValidationError: If the value or any nested value doesn't match.
    """
    if value is None:
        if field_def.nullable:
            return
        raise ValidationError(
            f"Output field '{field_name}' must not be null",
            suggestion=f"Ensure '{field_name}' is not null or set nullable: true",
        )

    if not check_type(value, field_def.type):
        raise ValidationError(
            f"Output field '{field_name}' has wrong type: "
            f"expected {field_def.type}, got {type(value).__name__} "
            f"(received: {_describe_value(value)})",
            suggestion=f"Ensure agent returns correct type for '{field_name}'",
        )

    _check_constraints(field_name, value, field_def)

    if field_def.type == "object" and field_def.properties and isinstance(value, dict):
        validate_output(value, field_def.properties)

    if field_def.type == "array" and field_def.items and isinstance(value, list):
        for i, item in enumerate(value):
            if not check_type(item, field_def.items.type):
                raise ValidationError(
                    f"Array item {i} in '{field_name}' has wrong type: "
                    f"expected {field_def.items.type}, got {type(item).__name__} "
                    f"(received: {_describe_value(item)})",
                    suggestion=f"Ensure all items in '{field_name}' have correct type",
                )
            _validate_field(field_name, item, field_def.items)


def _describe_value(value: Any, max_chars: int = 200) -> str:
    """Render a value for an error message, describing containers by shape.

    Diagnosing a shape mismatch from logs alone is impractical when the
    message names only the two types. ``validate_output`` also runs on ``set``
    and ``script`` step output, so dict and list values are reduced to their
    keys or length rather than dumped. Scalars are still rendered via ``repr``
    and truncated, since a mismatched scalar is usually the whole diagnosis.

    Args:
        value: The value that failed validation.
        max_chars: Maximum length of the rendered value before truncation.

    Returns:
        A short description suitable for an error message.
    """
    if isinstance(value, dict):
        keys = sorted(str(k) for k in value)
        rendered = f"object with keys {keys}"
    elif isinstance(value, list):
        return f"array of {len(value)} item(s)"
    else:
        rendered = repr(value)

    if len(rendered) > max_chars:
        return rendered[:max_chars] + "..."
    return rendered


def check_type(value: Any, expected: str) -> bool:
    """Check if value matches expected type.

    Args:
        value: The value to check.
        expected: The expected type name (string, number, boolean, array, object).

    Returns:
        True if value matches expected type, False otherwise.
    """
    type_map: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    expected_types = type_map.get(expected)
    if expected_types is None:
        # Unknown type - accept any value
        return True

    # Special handling for number type to exclude booleans
    # (in Python, bool is a subclass of int)
    if expected == "number" and isinstance(value, bool):
        return False

    return isinstance(value, expected_types)


def parse_json_output(raw_response: str) -> dict[str, Any]:
    """Parse JSON from an agent's raw response.

    Attempts to extract JSON from the response, handling common cases
    like markdown code blocks.

    Args:
        raw_response: The raw text response from the agent.

    Returns:
        Parsed JSON as a dictionary.

    Raises:
        ValidationError: If JSON parsing fails.
    """
    import json
    import re

    text = raw_response.strip()

    # Try to extract JSON from markdown code blocks. Two-stage strategy:
    # 1. Non-greedy findall + try-parse each candidate (first valid wins).
    #    Handles the common case of multiple fenced blocks in one response
    #    (e.g. "initial answer ... revised answer") where the first complete
    #    JSON block is the authoritative one.
    # 2. Greedy single capture as fallback. Handles the case where the JSON
    #    contains literal ``` inside a string field, which breaks non-greedy
    #    matching at the inner fence but is recovered by closing at the LAST
    #    fence in the response.
    candidates = re.findall(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    for candidate in candidates:
        stripped = candidate.strip()
        try:
            result = json.loads(stripped)
            if isinstance(result, dict):
                return result
            return {"result": result}
        except json.JSONDecodeError:
            continue
    greedy = re.search(r"```(?:json)?\s*\n?(.*)\n?```", text, re.DOTALL)
    if greedy:
        text = greedy.group(1).strip()

    # Try to find JSON object or array
    if not text.startswith(("{", "[")):
        # Try to find first { or [
        obj_start = text.find("{")
        arr_start = text.find("[")

        if obj_start >= 0 and (arr_start < 0 or obj_start < arr_start):
            text = text[obj_start:]
        elif arr_start >= 0:
            text = text[arr_start:]

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
        # If result is not a dict, wrap it
        return {"result": result}
    except json.JSONDecodeError as e:
        raise ValidationError(
            f"Failed to parse JSON from agent response: {e}",
            suggestion="Ensure agent outputs valid JSON format",
        ) from e
