"""Output parsing and validation for agent responses.

This module provides functions for validating agent output against
declared output schemas.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from conductor.config.schema import PATTERN_MATCH_TIMEOUT_SECONDS, OutputField
from conductor.exceptions import ValidationError

logger = logging.getLogger(__name__)


def validate_output(
    content: dict[str, Any],
    schema: dict[str, OutputField],
    *,
    warn_undeclared_keys: bool = False,
) -> None:
    """Validate agent output against declared schema.

    Checks that all required fields are present and have the correct types.
    Nested object properties and array items are validated recursively.

    Args:
        content: Agent's output content as a dictionary.
        schema: Expected output schema with field definitions.
        warn_undeclared_keys: When ``True``, log a warning for any keys in
            ``content`` that are not declared in ``schema``. This is useful for
            provider-backed (LLM) agents, where a typo in an optional field
            name silently drops data. Engine step types such as ``script`` and
            ``set`` intentionally leave this off: scripts always carry the
            injected ``stdout``/``stderr``/``exit_code`` baseline keys, and both
            step types are deterministic/author-controlled rather than produced
            by a non-deterministic model.

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

        _validate_field(
            field_name, content[field_name], field_def, warn_undeclared_keys=warn_undeclared_keys
        )

    if warn_undeclared_keys:
        undeclared_keys = [k for k in content if k not in schema]
        if undeclared_keys:
            logger.warning(
                "Output contains undeclared fields not present in the output schema: %s — "
                "check for typos against the declared output fields",
                undeclared_keys,
            )


def _check_constraints(field_name: str, value: Any, field_def: OutputField) -> None:
    """Validate scalar constraints (enum, length, pattern, range) for a field.

    Called after the type check has passed, so ``value`` is known to match
    ``field_def.type``. Raises ``ValidationError`` with a suggestion on failure.
    """
    if field_def.enum is not None and value not in field_def.enum:
        raise ValidationError(
            f"Output field '{field_name}' must be one of {field_def.enum!r}, got {value!r}",
            suggestion=f"Ensure '{field_name}' is one of {field_def.enum!r}",
        )

    if field_def.type == "string":
        if field_def.minLength is not None and len(value) < field_def.minLength:
            raise ValidationError(
                f"Output field '{field_name}' is shorter than minLength {field_def.minLength} "
                f"(received: {_describe_value(value)})",
                suggestion=f"Ensure '{field_name}' has at least {field_def.minLength} characters",
            )
        if field_def.maxLength is not None and len(value) > field_def.maxLength:
            raise ValidationError(
                f"Output field '{field_name}' is longer than maxLength {field_def.maxLength} "
                f"(received: {_describe_value(value)})",
                suggestion=f"Ensure '{field_name}' has at most {field_def.maxLength} characters",
            )

    if field_def.pattern is not None:
        try:
            match = field_def.compiled_pattern.search(value, timeout=PATTERN_MATCH_TIMEOUT_SECONDS)
        except TimeoutError as e:
            raise ValidationError(
                f"Output field '{field_name}' pattern match exceeded the "
                f"{PATTERN_MATCH_TIMEOUT_SECONDS}s time limit",
                suggestion=(
                    f"Simplify the pattern for '{field_name}' "
                    "or check for catastrophic backtracking"
                ),
            ) from e
        if match is None:
            raise ValidationError(
                f"Output field '{field_name}' does not match pattern '{field_def.pattern}' "
                f"(received: {_describe_value(value)})",
                suggestion=f"Ensure '{field_name}' matches the pattern '{field_def.pattern}'",
            )

    if field_def.type == "number":
        if field_def.minimum is not None and value < field_def.minimum:
            raise ValidationError(
                f"Output field '{field_name}' is below minimum {field_def.minimum} "
                f"(received: {_describe_value(value)})",
                suggestion=f"Ensure '{field_name}' is at least {field_def.minimum}",
            )
        if field_def.maximum is not None and value > field_def.maximum:
            raise ValidationError(
                f"Output field '{field_name}' is above maximum {field_def.maximum} "
                f"(received: {_describe_value(value)})",
                suggestion=f"Ensure '{field_name}' is at most {field_def.maximum}",
            )


def _validate_field(
    field_name: str,
    value: Any,
    field_def: OutputField,
    *,
    warn_undeclared_keys: bool = False,
) -> None:
    """Validate a single value against its output field definition.

    Recursively validates nested object properties and array items so
    ``array<object>`` and deeper combinations are checked at every depth,
    matching the recursion the object branch has always had.

    Args:
        field_name: Field name used in error messages (array items keep the
            parent array's name, matching the existing message style).
        value: The value to validate.
        field_def: The field definition to validate against.
        warn_undeclared_keys: Forwarded to nested ``validate_output`` calls so
            undeclared-key warnings are threaded through object properties and
            array items consistently.

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
        validate_output(value, field_def.properties, warn_undeclared_keys=warn_undeclared_keys)

    if field_def.type == "array" and field_def.items and isinstance(value, list):
        for i, item in enumerate(value):
            if item is None and field_def.items.nullable:
                continue
            if not check_type(item, field_def.items.type):
                raise ValidationError(
                    f"Array item {i} in '{field_name}' has wrong type: "
                    f"expected {field_def.items.type}, got {type(item).__name__} "
                    f"(received: {_describe_value(item)})",
                    suggestion=f"Ensure all items in '{field_name}' have correct type",
                )
            _validate_field(
                f"array item {i} in '{field_name}'",
                item,
                field_def.items,
                warn_undeclared_keys=warn_undeclared_keys,
            )


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
