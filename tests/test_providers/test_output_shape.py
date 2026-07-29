"""Tests for conservative unwrapping of wrapper-shaped scalar output."""

from __future__ import annotations

from conductor.config.schema import OutputField
from conductor.executor.output import validate_output
from conductor.providers._output_shape import unwrap_scalar_wrappers


class TestUnwrapMatches:
    """Cases where the unwrap should fire."""

    def test_key_matching_field_name(self) -> None:
        """The reported case: a wrapper echoing the field name, with extra keys."""
        schema = {"decision": OutputField(type="string")}
        content = {"decision": {"decision": "APPROVE", "reasoning": "looks good"}}

        assert unwrap_scalar_wrappers(content, schema) == {"decision": "APPROVE"}

    def test_generic_value_key(self) -> None:
        schema = {"decision": OutputField(type="string")}
        content = {"decision": {"value": "APPROVE"}}

        assert unwrap_scalar_wrappers(content, schema) == {"decision": "APPROVE"}

    def test_generic_result_key(self) -> None:
        schema = {"answer": OutputField(type="string")}
        content = {"answer": {"result": "42"}}

        assert unwrap_scalar_wrappers(content, schema) == {"answer": "42"}

    def test_sole_key_with_unrelated_name(self) -> None:
        schema = {"answer": OutputField(type="string")}
        content = {"answer": {"text": "hello"}}

        assert unwrap_scalar_wrappers(content, schema) == {"answer": "hello"}

    def test_number_field(self) -> None:
        schema = {"score": OutputField(type="number")}
        content = {"score": {"score": 0.87}}

        assert unwrap_scalar_wrappers(content, schema) == {"score": 0.87}

    def test_boolean_field(self) -> None:
        schema = {"approved": OutputField(type="boolean")}
        content = {"approved": {"value": False}}

        assert unwrap_scalar_wrappers(content, schema) == {"approved": False}

    def test_field_name_wins_over_generic_key(self) -> None:
        """Field-name match is tried before the generic ``value`` key."""
        schema = {"decision": OutputField(type="string")}
        content = {"decision": {"decision": "APPROVE", "value": "REJECT"}}

        assert unwrap_scalar_wrappers(content, schema) == {"decision": "APPROVE"}

    def test_only_the_wrapped_field_is_touched(self) -> None:
        schema = {
            "decision": OutputField(type="string"),
            "summary": OutputField(type="string"),
        }
        content = {"decision": {"decision": "APPROVE"}, "summary": "all good"}

        assert unwrap_scalar_wrappers(content, schema) == {
            "decision": "APPROVE",
            "summary": "all good",
        }

    def test_unwrapped_result_passes_validation(self) -> None:
        """The unwrap must never produce a value validation would then reject."""
        schema = {"decision": OutputField(type="string")}
        content = {"decision": {"decision": "APPROVE", "reasoning": "fine"}}

        validate_output(unwrap_scalar_wrappers(content, schema), schema)


class TestUnwrapDeclines:
    """Cases where the unwrap must NOT fire, leaving the re-prompt path to run."""

    def test_object_field_is_left_alone(self) -> None:
        """A dict arriving for an ``object`` field is plausibly correct."""
        schema = {"details": OutputField(type="object")}
        content = {"details": {"a": "b"}}

        assert unwrap_scalar_wrappers(content, schema) is content

    def test_array_field_is_left_alone(self) -> None:
        schema = {"items": OutputField(type="array")}
        content = {"items": {"items": ["a"]}}

        assert unwrap_scalar_wrappers(content, schema) is content

    def test_ambiguous_multiple_keys_without_match(self) -> None:
        """Two candidates, neither the field name nor a generic key."""
        schema = {"decision": OutputField(type="string")}
        content = {"decision": {"first": "APPROVE", "second": "REJECT"}}

        assert unwrap_scalar_wrappers(content, schema) is content

    def test_candidate_of_wrong_type(self) -> None:
        """A matching key whose value is itself the wrong type is not used."""
        schema = {"decision": OutputField(type="string")}
        content = {"decision": {"decision": {"nested": "deep"}}}

        assert unwrap_scalar_wrappers(content, schema) is content

    def test_nested_wrapper_is_not_recursively_unwrapped(self) -> None:
        schema = {"score": OutputField(type="number")}
        content = {"score": {"score": {"value": 1}}}

        assert unwrap_scalar_wrappers(content, schema) is content

    def test_boolean_not_accepted_for_number(self) -> None:
        """bool is an int subclass in Python; it must not satisfy ``number``."""
        schema = {"score": OutputField(type="number")}
        content = {"score": {"value": True}}

        assert unwrap_scalar_wrappers(content, schema) is content

    def test_empty_wrapper(self) -> None:
        schema = {"decision": OutputField(type="string")}
        content: dict[str, object] = {"decision": {}}

        assert unwrap_scalar_wrappers(content, schema) is content

    def test_correct_scalar_is_untouched(self) -> None:
        schema = {"decision": OutputField(type="string")}
        content = {"decision": "APPROVE"}

        assert unwrap_scalar_wrappers(content, schema) is content

    def test_missing_field_is_untouched(self) -> None:
        schema = {"decision": OutputField(type="string")}
        content = {"other": "value"}

        assert unwrap_scalar_wrappers(content, schema) is content

    def test_does_not_mutate_input(self) -> None:
        schema = {"decision": OutputField(type="string")}
        content = {"decision": {"decision": "APPROVE"}}

        unwrap_scalar_wrappers(content, schema)

        assert content == {"decision": {"decision": "APPROVE"}}
