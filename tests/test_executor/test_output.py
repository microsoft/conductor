"""Unit tests for output validation.

Tests cover:
- Type validation for all supported types
- Missing field detection
- Nested object validation
- Array item validation
- JSON parsing from raw responses
"""

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
