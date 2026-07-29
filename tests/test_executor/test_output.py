"""Unit tests for output validation.

Tests cover:
- Type validation for all supported types
- Missing field detection
- Nested object validation
- Array item validation
- JSON parsing from raw responses
"""

from typing import Any

import pytest

from conductor.config.schema import OutputField
from conductor.exceptions import ValidationError
from conductor.executor.output import (
    _check_type,
    parse_json_output,
    validate_output,
)


class TestCheckType:
    """Tests for _check_type helper function."""

    def test_string_type(self) -> None:
        """Test string type checking."""
        assert _check_type("hello", "string") is True
        assert _check_type("", "string") is True
        assert _check_type(123, "string") is False
        assert _check_type(None, "string") is False

    def test_number_type(self) -> None:
        """Test number type checking."""
        assert _check_type(42, "number") is True
        assert _check_type(3.14, "number") is True
        assert _check_type(0, "number") is True
        assert _check_type(-1, "number") is True
        assert _check_type("42", "number") is False
        # Booleans should not count as numbers
        assert _check_type(True, "number") is False
        assert _check_type(False, "number") is False

    def test_boolean_type(self) -> None:
        """Test boolean type checking."""
        assert _check_type(True, "boolean") is True
        assert _check_type(False, "boolean") is True
        assert _check_type(1, "boolean") is False
        assert _check_type("true", "boolean") is False

    def test_array_type(self) -> None:
        """Test array type checking."""
        assert _check_type([], "array") is True
        assert _check_type([1, 2, 3], "array") is True
        assert _check_type(["a", "b"], "array") is True
        assert _check_type({}, "array") is False
        assert _check_type("[]", "array") is False

    def test_object_type(self) -> None:
        """Test object type checking."""
        assert _check_type({}, "object") is True
        assert _check_type({"key": "value"}, "object") is True
        assert _check_type([], "object") is False
        assert _check_type("{}", "object") is False

    def test_unknown_type(self) -> None:
        """Test unknown type accepts anything."""
        assert _check_type("anything", "unknown_type") is True
        assert _check_type(123, "unknown_type") is True


class TestValidateOutput:
    """Tests for validate_output function."""

    def test_valid_string_field(self) -> None:
        """Test validation of valid string field."""
        schema = {"answer": OutputField(type="string")}
        content = {"answer": "Hello, world!"}

        # Should not raise
        validate_output(content, schema)

    def test_valid_number_field(self) -> None:
        """Test validation of valid number field."""
        schema = {"count": OutputField(type="number")}
        content = {"count": 42}

        validate_output(content, schema)

    def test_valid_boolean_field(self) -> None:
        """Test validation of valid boolean field."""
        schema = {"is_valid": OutputField(type="boolean")}
        content = {"is_valid": True}

        validate_output(content, schema)

    def test_valid_array_field(self) -> None:
        """Test validation of valid array field."""
        schema = {"items": OutputField(type="array")}
        content = {"items": [1, 2, 3]}

        validate_output(content, schema)

    def test_valid_object_field(self) -> None:
        """Test validation of valid object field."""
        schema = {"data": OutputField(type="object")}
        content = {"data": {"key": "value"}}

        validate_output(content, schema)

    def test_missing_field_raises(self) -> None:
        """Test that missing field raises ValidationError."""
        schema = {"answer": OutputField(type="string")}
        content = {}

        with pytest.raises(ValidationError, match="Missing required output field: answer"):
            validate_output(content, schema)

    def test_wrong_type_raises(self) -> None:
        """Test that wrong type raises ValidationError."""
        schema = {"count": OutputField(type="number")}
        content = {"count": "not a number"}

        with pytest.raises(ValidationError, match="wrong type.*expected number.*got str"):
            validate_output(content, schema)

    def test_multiple_fields(self) -> None:
        """Test validation of multiple fields."""
        schema = {
            "name": OutputField(type="string"),
            "age": OutputField(type="number"),
            "active": OutputField(type="boolean"),
        }
        content = {"name": "Alice", "age": 30, "active": True}

        validate_output(content, schema)

    def test_extra_fields_allowed(self) -> None:
        """Test that extra fields in content are allowed."""
        schema = {"required_field": OutputField(type="string")}
        content = {"required_field": "value", "extra_field": "ignored"}

        validate_output(content, schema)


class TestValidateOutputNested:
    """Tests for nested structure validation."""

    def test_nested_object_validation(self) -> None:
        """Test validation of nested object."""
        schema = {
            "person": OutputField(
                type="object",
                properties={
                    "name": OutputField(type="string"),
                    "age": OutputField(type="number"),
                },
            )
        }
        content = {"person": {"name": "Alice", "age": 30}}

        validate_output(content, schema)

    def test_nested_object_missing_field(self) -> None:
        """Test that missing field in nested object raises."""
        schema = {
            "person": OutputField(
                type="object",
                properties={
                    "name": OutputField(type="string"),
                    "age": OutputField(type="number"),
                },
            )
        }
        content = {"person": {"name": "Alice"}}

        with pytest.raises(ValidationError, match="Missing required output field: age"):
            validate_output(content, schema)

    def test_array_items_validation(self) -> None:
        """Test validation of array items."""
        schema = {
            "numbers": OutputField(
                type="array",
                items=OutputField(type="number"),
            )
        }
        content = {"numbers": [1, 2, 3, 4, 5]}

        validate_output(content, schema)

    def test_array_items_wrong_type(self) -> None:
        """Test that wrong type in array raises."""
        schema = {
            "numbers": OutputField(
                type="array",
                items=OutputField(type="number"),
            )
        }
        content = {"numbers": [1, 2, "three", 4]}

        with pytest.raises(ValidationError, match="Array item 2.*wrong type"):
            validate_output(content, schema)


class TestValidateOutputArrayRecursion:
    """Tests for recursive validation of array item schemas (issue regression)."""

    def test_array_of_objects_valid_passes(self) -> None:
        """Valid array<object> content must pass unchanged (no false positives)."""
        schema = {
            "findings": OutputField(
                type="array",
                items=OutputField(
                    type="object",
                    properties={
                        "title": OutputField(type="string"),
                        "score": OutputField(type="number"),
                    },
                ),
            )
        }
        content = {
            "findings": [
                {"title": "first", "score": 1.0},
                {"title": "second", "score": 2.0},
            ]
        }

        validate_output(content, schema)

    def test_array_of_objects_missing_nested_field_raises(self) -> None:
        """Missing required field inside an array item must raise (previously silently accepted)."""
        schema = {
            "findings": OutputField(
                type="array",
                items=OutputField(
                    type="object",
                    properties={
                        "title": OutputField(type="string"),
                        "score": OutputField(type="number"),
                    },
                ),
            )
        }
        content = {
            "findings": [
                {"title": "first", "score": 1.0},
                {"title": "second"},
            ]
        }

        with pytest.raises(ValidationError, match="Missing required output field: score"):
            validate_output(content, schema)

    def test_array_of_objects_wrong_nested_type_raises(self) -> None:
        """Wrong-typed nested field inside an array item must raise."""
        schema = {
            "findings": OutputField(
                type="array",
                items=OutputField(
                    type="object",
                    properties={
                        "title": OutputField(type="string"),
                        "score": OutputField(type="number"),
                    },
                ),
            )
        }
        content = {
            "findings": [
                {"title": 42, "score": 1.0},
            ]
        }

        with pytest.raises(ValidationError, match="wrong type"):
            validate_output(content, schema)

    def test_nested_array_of_objects_deep_error_raises(self) -> None:
        """array<array<object>> must validate the object level, pinning general recursion."""
        schema = {
            "matrix": OutputField(
                type="array",
                items=OutputField(
                    type="array",
                    items=OutputField(
                        type="object",
                        properties={"v": OutputField(type="number")},
                    ),
                ),
            )
        }
        content = {
            "matrix": [
                [{"v": 1.0}, {"v": "x"}],
            ]
        }

        with pytest.raises(ValidationError, match="wrong type"):
            validate_output(content, schema)

    def test_array_without_items_unchanged(self) -> None:
        """Arrays declared without items keep historical passthrough behavior."""
        schema = {"tags": OutputField(type="array")}
        content = {"tags": [1, "two", {"three": 3}]}

        validate_output(content, schema)

    def test_builder_shaped_deep_schema_invalid_content_raises(self) -> None:
        """A schema at the builder boundary (max_depth=10) must be
        enforceable without RecursionError."""
        from conductor.providers._schema import build_json_schema_field

        # Build 5 array/object pairs: depth 10 leaf string at the innermost level.
        inner: OutputField = OutputField(type="string")
        for _ in range(5):
            inner = OutputField(
                type="array",
                items=OutputField(
                    type="object",
                    properties={"x": inner},
                ),
            )
        schema_root = {"data": inner}

        built = build_json_schema_field(inner)
        assert isinstance(built, dict)

        # Content mirrors the nested structure with a wrong-typed leaf.
        content: dict[str, Any] = {"data": [{"x": [{"x": [{"x": [{"x": [{"x": 123}]}]}]}]}]}

        with pytest.raises(ValidationError, match="wrong type"):
            validate_output(content, schema_root)

    def test_validate_output_does_not_call_check_type_directly(self) -> None:
        """After refactor all value checks must flow through _validate_field."""
        import inspect

        from conductor.executor.output import _validate_field, validate_output

        validate_source = inspect.getsource(validate_output)
        validate_field_source = inspect.getsource(_validate_field)

        assert "_check_type(" not in validate_source
        assert "_check_type(" in validate_field_source


class TestParseJsonOutput:
    """Tests for parse_json_output function."""

    def test_parse_simple_object(self) -> None:
        """Test parsing simple JSON object."""
        raw = '{"answer": "Hello"}'
        result = parse_json_output(raw)

        assert result == {"answer": "Hello"}

    def test_parse_with_whitespace(self) -> None:
        """Test parsing JSON with leading/trailing whitespace."""
        raw = '  \n{"answer": "Hello"}  \n'
        result = parse_json_output(raw)

        assert result == {"answer": "Hello"}

    def test_parse_from_markdown_code_block(self) -> None:
        """Test parsing JSON from markdown code block."""
        raw = """Here is the answer:
```json
{"answer": "Hello", "value": 42}
```
"""
        result = parse_json_output(raw)

        assert result == {"answer": "Hello", "value": 42}

    def test_parse_from_code_block_without_language(self) -> None:
        """Test parsing JSON from code block without language specifier."""
        raw = """
```
{"result": "success"}
```
"""
        result = parse_json_output(raw)

        assert result == {"result": "success"}

    def test_parse_with_text_before_json(self) -> None:
        """Test parsing when JSON is preceded by text."""
        raw = 'Here is the result: {"answer": "test"}'
        result = parse_json_output(raw)

        assert result == {"answer": "test"}

    def test_parse_array_wraps_in_result(self) -> None:
        """Test that parsing an array wraps it in a result key."""
        raw = '["a", "b", "c"]'
        result = parse_json_output(raw)

        assert result == {"result": ["a", "b", "c"]}

    def test_parse_invalid_json_raises(self) -> None:
        """Test that invalid JSON raises ValidationError."""
        raw = "This is not JSON at all"

        with pytest.raises(ValidationError, match="Failed to parse JSON"):
            parse_json_output(raw)

    def test_parse_nested_json(self) -> None:
        """Test parsing nested JSON structure."""
        raw = '{"person": {"name": "Alice", "tags": ["dev", "py"]}}'
        result = parse_json_output(raw)

        assert result["person"]["name"] == "Alice"
        assert result["person"]["tags"] == ["dev", "py"]

    def test_parse_json_with_triple_backticks_inside_string(self) -> None:
        """Triple-backticks inside a string field must not truncate the JSON.

        Reproduces brainstorm Issue #1: the non-greedy fence-extraction regex
        closed at the first inner ``` instead of the actual closing fence,
        producing invalid JSON and triggering parse-recovery loops.
        """
        raw = '```json\n{"code": "use ```fenced``` blocks", "n": 1}\n```'
        result = parse_json_output(raw)

        assert result == {"code": "use ```fenced``` blocks", "n": 1}

    def test_parse_json_with_multiple_fenced_blocks_first_wins(self) -> None:
        """When the response contains multiple fenced JSON blocks, the first
        valid block wins.

        Pins the behavior chosen for the multi-block trade-off raised in PR
        review (greedy regex would have captured everything between the first
        and last fence and failed to parse). The current implementation tries
        each non-greedy candidate in order and returns the first that parses.
        """
        raw = '```json\n{"a": 1}\n```\n\nupdated answer:\n\n```json\n{"a": 2}\n```'
        result = parse_json_output(raw)

        assert result == {"a": 1}


class TestValidationErrorValueDescription:
    """The error names the offending value, without echoing secrets.

    `validate_output` also validates `set` and `script` step output, which may
    carry credentials, so containers are described by shape rather than dumped.
    """

    def test_scalar_mismatch_shows_the_value(self) -> None:
        schema = {"count": OutputField(type="number")}

        with pytest.raises(ValidationError, match="received: 'many'"):
            validate_output({"count": "many"}, schema)

    def test_object_is_described_by_keys_not_contents(self) -> None:
        schema = {"decision": OutputField(type="string")}
        secret = {"decision": {"decision": "APPROVE", "api_key": "sk-live-SECRET"}}

        with pytest.raises(ValidationError) as exc_info:
            validate_output(secret, schema)

        message = str(exc_info.value)
        assert "object with keys" in message
        assert "api_key" in message
        assert "sk-live-SECRET" not in message

    def test_array_is_described_by_length(self) -> None:
        schema = {"decision": OutputField(type="string")}

        with pytest.raises(ValidationError, match=r"array of 3 item\(s\)"):
            validate_output({"decision": ["a", "b", "c"]}, schema)

    def test_long_scalar_is_truncated(self) -> None:
        schema = {"count": OutputField(type="number")}

        with pytest.raises(ValidationError) as exc_info:
            validate_output({"count": "x" * 500}, schema)

        assert "..." in str(exc_info.value)

    def test_array_item_mismatch_also_describes_the_value(self) -> None:
        schema = {"tags": OutputField(type="array", items=OutputField(type="string"))}

        with pytest.raises(ValidationError, match="received: 42"):
            validate_output({"tags": ["ok", 42]}, schema)
