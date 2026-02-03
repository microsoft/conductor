"""Output parsing and validation for agent responses.

This module provides functions for validating agent output against
declared output schemas.
"""

from __future__ import annotations

from typing import Any

from conductor.config.schema import OutputField
from conductor.exceptions import ValidationError


def validate_output(
    content: dict[str, Any],
    schema: dict[str, OutputField],
) -> None:
    """Validate agent output against declared schema.

    Checks that all required fields are present and have the correct types.

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
            raise ValidationError(
                f"Missing required output field: {field_name}",
                suggestion=f"Ensure agent returns '{field_name}' in output",
            )

        value = content[field_name]
        expected_type = field_def.type

        # Type checking
        if not _check_type(value, expected_type):
            raise ValidationError(
                f"Output field '{field_name}' has wrong type: "
                f"expected {expected_type}, got {type(value).__name__}",
                suggestion=f"Ensure agent returns correct type for '{field_name}'",
            )

        # Recursively validate nested structures
        if expected_type == "object" and field_def.properties and isinstance(value, dict):
            validate_output(value, field_def.properties)

        if expected_type == "array" and field_def.items and isinstance(value, list):
            for i, item in enumerate(value):
                if not _check_type(item, field_def.items.type):
                    raise ValidationError(
                        f"Array item {i} in '{field_name}' has wrong type: "
                        f"expected {field_def.items.type}, got {type(item).__name__}",
                        suggestion=f"Ensure all items in '{field_name}' have correct type",
                    )


def _check_type(value: Any, expected: str) -> bool:
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

    # Try to extract JSON from markdown code blocks
    json_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if json_block_match:
        text = json_block_match.group(1).strip()

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
