"""Unit tests for structured output post-processing in the Pydantic AI provider.

Tests verify that pydantic-ai's tool-based structured output is dumped to a
plain dict and passed through Conductor's own ``validate_output()`` so the final
content matches the shape produced by ClaudeProvider. They also pin the
decision to use ``ToolOutput`` (not ``NativeOutput``) and exercise the text
JSON fallback for malformed responses.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.output import ToolOutput

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import ProviderError, ValidationError
from conductor.providers._pydantic_ai.agent_builder import build_agent
from conductor.providers._pydantic_ai.converters import output_schema_to_pydantic_model
from conductor.providers._pydantic_ai.structured_output import (
    extract_content,
    parse_text_fallback,
)


@pytest.fixture(autouse=True)
def _ensure_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a dummy API key so AnthropicModel construction succeeds."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _build_agent_with_schema(
    output_schema: dict[str, OutputField],
    custom_output_args: dict[str, Any] | None = None,
) -> Agent[Any, Any]:
    """Build a Pydantic AI agent with a Conductor output schema and a test model."""
    dynamic_model = output_schema_to_pydantic_model("FormatterOutput", output_schema)
    assert dynamic_model is not None
    return Agent(
        TestModel(custom_output_args=custom_output_args),
        output_type=ToolOutput(dynamic_model),
        retries=0,
    )


class TestToolOutputAsDefault:
    """Requirement: structured output uses pydantic-ai ``ToolOutput``, not ``NativeOutput``."""

    def test_output_schema_uses_tool_output(self) -> None:
        """A non-empty output schema must be wrapped in ``ToolOutput`` so the model
        is asked to call a tool named ``final_result`` rather than using native JSON mode."""
        agent_def = AgentDef(
            name="formatter",
            output={"answer": OutputField(type="string")},
        )

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
        )

        assert isinstance(pydantic_agent.output_type, ToolOutput)
        schema = pydantic_agent._output_schema
        assert schema.mode == "tool"
        toolset = schema.toolset
        assert toolset is not None
        assert len(toolset._tool_defs) == 1
        assert toolset._tool_defs[0].name == "final_result"


class TestStructuredOutputRoundTrip:
    """Requirement: pydantic-ai validated output → dict → Conductor validate_output."""

    def test_valid_output_round_trip(self) -> None:
        """A pydantic-ai result.output (a dynamic Pydantic model instance) must be
        dumped to a dict and pass Conductor's validate_output unchanged."""
        output_schema = {
            "answer": OutputField(type="string"),
            "score": OutputField(type="number"),
        }
        pydantic_agent = _build_agent_with_schema(
            output_schema,
            custom_output_args={"answer": "hello", "score": 42},
        )

        result = pydantic_agent.run_sync("format this")

        assert isinstance(result.output, BaseModel)
        content = extract_content(result.output, output_schema, "formatter")
        assert content == {"answer": "hello", "score": 42}


class TestValidationFailurePath:
    """Requirement: Conductor-level validation failures surface as ``ValidationError``."""

    def test_missing_field_raises_validation_error(self) -> None:
        """A model output missing a required schema field must fail validate_output
        with a clear ValidationError (mirroring ClaudeProvider's fatal validation path)."""
        output_schema = {
            "answer": OutputField(type="string"),
            "score": OutputField(type="number"),
        }
        dynamic_model = output_schema_to_pydantic_model("FormatterOutput", output_schema)
        assert dynamic_model is not None
        instance = dynamic_model.model_construct(answer="hello")

        with pytest.raises(ValidationError, match="Missing required output field: score"):
            extract_content(instance, output_schema, "formatter")

    def test_wrong_type_raises_validation_error(self) -> None:
        """A numeric field that receives a string must fail Conductor's type check."""
        output_schema = {"count": OutputField(type="number")}
        dynamic_model = output_schema_to_pydantic_model("CountOutput", output_schema)
        assert dynamic_model is not None
        instance = dynamic_model.model_construct(count="many")

        with pytest.raises(ValidationError, match="wrong type"):
            extract_content(instance, output_schema, "formatter")

    def test_wrapper_object_unwrapped_for_scalar_field(self) -> None:
        """Requirement: provider parity with copilot/hermes — a wrapper object
        like {"answer": {"value": "x"}} arriving for a scalar field must be
        unwrapped by normalize_agent_output before validation."""
        output_schema = {"answer": OutputField(type="string")}
        dynamic_model = output_schema_to_pydantic_model("WrapperOutput", output_schema)
        assert dynamic_model is not None
        instance = dynamic_model.model_construct(answer={"value": "unwrapped"})

        content = extract_content(instance, output_schema, "formatter")

        assert content == {"answer": "unwrapped"}

    def test_non_structured_output_raises_provider_error(self) -> None:
        """A non-dict, non-text result for a structured schema must raise a provider error."""
        output_schema = {"answer": OutputField(type="string")}

        with pytest.raises(ProviderError, match="non-structured output"):
            extract_content(object(), output_schema, "formatter")


class TestTextFallback:
    """Requirement: text containing JSON falls back to ``parse_json_output``."""

    def test_json_text_fallback_parses_and_validates(self) -> None:
        """If the model returns text with a JSON blob, the fallback parser must
        extract it and ``validate_output`` must accept it."""
        output_schema = {"answer": OutputField(type="string")}
        text = 'Here is the result:\n\n```json\n{"answer": "parsed"}\n```'

        content = parse_text_fallback(text, output_schema, "formatter")

        assert content == {"answer": "parsed"}

    def test_json_text_fallback_missing_field_raises(self) -> None:
        """Fallback-parsed JSON that misses a required field must fail validation."""
        output_schema = {
            "answer": OutputField(type="string"),
            "score": OutputField(type="number"),
        }
        text = '{"answer": "parsed"}'

        with pytest.raises(ValidationError, match="Missing required output field: score"):
            parse_text_fallback(text, output_schema, "formatter")

    def test_invalid_json_text_raises_provider_error(self) -> None:
        """Text that cannot be parsed as JSON must raise a provider error."""
        output_schema = {"answer": OutputField(type="string")}

        with pytest.raises(ProviderError, match="could not be parsed as JSON"):
            parse_text_fallback("not valid json", output_schema, "formatter")

    def test_text_fallback_unwraps_scalar_wrapper(self) -> None:
        """Requirement: provider parity — fallback-parsed JSON with a wrapper
        object for a scalar field must be unwrapped before validation."""
        output_schema = {"answer": OutputField(type="string")}
        text = '{"answer": {"result": "from wrapper"}}'

        content = parse_text_fallback(text, output_schema, "formatter")

        assert content == {"answer": "from wrapper"}


class TestEnumConstraint:
    """Requirement: enum membership matches validate_output semantics."""

    def test_number_enum_rejects_bool(self) -> None:
        """A number enum [1] must reject a boolean value; bool is not an int."""
        output_schema = {"value": OutputField(type="number", enum=[1])}
        dynamic_model = output_schema_to_pydantic_model("EnumOutput", output_schema)
        assert dynamic_model is not None

        with pytest.raises(PydanticValidationError):
            dynamic_model(value=True)

    def test_number_enum_accepts_float_int_equality(self) -> None:
        """A number enum [1] must accept 1.0 using Python equality."""
        output_schema = {"value": OutputField(type="number", enum=[1])}
        dynamic_model = output_schema_to_pydantic_model("EnumOutput", output_schema)
        assert dynamic_model is not None

        instance = dynamic_model(value=1.0)
        assert instance.value == 1.0

    def test_string_enum_rejects_non_member(self) -> None:
        """A string enum must reject a value not listed in enum."""
        output_schema = {"value": OutputField(type="string", enum=["a", "b"])}
        dynamic_model = output_schema_to_pydantic_model("EnumOutput", output_schema)
        assert dynamic_model is not None

        with pytest.raises(PydanticValidationError):
            dynamic_model(value="c")


class TestPatternConstraint:
    """Requirement: pattern is validated with Python regex, not Rust regex."""

    def test_lookahead_pattern_builds_and_validates(self) -> None:
        """A Python-only lookahead pattern must build cleanly and reject values
        that do not match."""
        output_schema = {"value": OutputField(type="string", pattern=r"^(?=.*A).*$")}
        dynamic_model = output_schema_to_pydantic_model("PatternOutput", output_schema)
        assert dynamic_model is not None

        assert dynamic_model(value="Abc").value == "Abc"
        with pytest.raises(PydanticValidationError):
            dynamic_model(value="bc")


class TestLengthAndRangeConstraints:
    """Requirement: minLength/maxLength/minimum/maximum are enforced."""

    def test_min_length_and_max_length_reject_violations(self) -> None:
        """String length constraints must reject too-short or too-long values."""
        output_schema = {"value": OutputField(type="string", minLength=2, maxLength=3)}
        dynamic_model = output_schema_to_pydantic_model("LengthOutput", output_schema)
        assert dynamic_model is not None

        assert dynamic_model(value="ab").value == "ab"
        assert dynamic_model(value="abc").value == "abc"
        with pytest.raises(PydanticValidationError):
            dynamic_model(value="a")
        with pytest.raises(PydanticValidationError):
            dynamic_model(value="abcd")

    def test_minimum_and_maximum_reject_violations(self) -> None:
        """Number range constraints must reject out-of-bounds values."""
        output_schema = {"value": OutputField(type="number", minimum=0, maximum=10)}
        dynamic_model = output_schema_to_pydantic_model("RangeOutput", output_schema)
        assert dynamic_model is not None

        assert dynamic_model(value=0).value == 0
        assert dynamic_model(value=10).value == 10
        with pytest.raises(PydanticValidationError):
            dynamic_model(value=-1)
        with pytest.raises(PydanticValidationError):
            dynamic_model(value=11)


class TestNullableAndOptional:
    """Requirement: nullable and optional fields behave as specified."""

    def test_explicit_none_nullable_passes(self) -> None:
        """A nullable field must accept an explicit None value."""
        output_schema = {"value": OutputField(type="string", nullable=True)}
        dynamic_model = output_schema_to_pydantic_model("NullableOutput", output_schema)
        assert dynamic_model is not None

        instance = dynamic_model(value=None)
        assert instance.value is None

    def test_explicit_none_non_nullable_optional_fails(self) -> None:
        """A non-nullable optional field must reject an explicit None."""
        output_schema = {"value": OutputField(type="string", required=False, nullable=False)}
        dynamic_model = output_schema_to_pydantic_model("OptionalOutput", output_schema)
        assert dynamic_model is not None

        with pytest.raises(PydanticValidationError):
            dynamic_model(value=None)

    def test_omitted_optional_excluded_from_extract_content(self) -> None:
        """An omitted optional field must not appear in the extracted content dict."""
        output_schema = {
            "score": OutputField(type="number"),
            "extra": OutputField(type="string", required=False, nullable=False),
        }
        dynamic_model = output_schema_to_pydantic_model("OptionalOmitOutput", output_schema)
        assert dynamic_model is not None

        instance = dynamic_model.model_construct(score=1)
        content = extract_content(instance, output_schema, "formatter")
        assert "extra" not in content
        assert content == {"score": 1}


class TestCombinedConstraints:
    """Requirement: enum, pattern, and length constraints compose."""

    def test_combined_constraints_reject_partial_matches(self) -> None:
        """A value must satisfy every constraint; enum-pass/pattern-fail and
        pattern-pass/enum-fail must both be rejected."""
        output_schema = {
            "value": OutputField(
                type="string",
                enum=["abc"],
                pattern=r"^a",
                minLength=3,
                maxLength=3,
            )
        }
        dynamic_model = output_schema_to_pydantic_model("CombinedOutput", output_schema)
        assert dynamic_model is not None

        assert dynamic_model(value="abc").value == "abc"
        with pytest.raises(PydanticValidationError):
            dynamic_model(value="abd")  # enum fail
        with pytest.raises(PydanticValidationError):
            dynamic_model(value="abcd")  # pattern pass-ish, length fail


class TestToolSchemaKeywords:
    """Requirement: the generated tool JSON schema exposes constraints and omits defaults."""

    def _schema(self, output_schema: dict[str, OutputField]) -> dict[str, Any]:
        """Build an agent and return the final_result tool JSON schema."""
        agent_def = AgentDef(name="formatter", output=output_schema)
        pydantic_agent = build_agent(agent_def, system_prompt="", rendered_prompt="")
        assert isinstance(pydantic_agent.output_type, ToolOutput)
        toolset = pydantic_agent._output_schema.toolset
        assert toolset is not None
        assert len(toolset._tool_defs) == 1
        return toolset._tool_defs[0].parameters_json_schema

    def test_enum_pattern_length_range_in_schema(self) -> None:
        """The tool schema must contain enum/pattern/minLength/maxLength/minimum/maximum."""
        output_schema = {
            "tag": OutputField(
                type="string",
                enum=["a", "b"],
                pattern=r"^[ab]$",
                minLength=1,
                maxLength=1,
            ),
            "count": OutputField(type="number", minimum=0, maximum=5),
        }
        schema = self._schema(output_schema)
        props = schema["properties"]
        assert props["tag"]["enum"] == ["a", "b"]
        assert props["tag"]["pattern"] == r"^[ab]$"
        assert props["tag"]["minLength"] == 1
        assert props["tag"]["maxLength"] == 1
        assert props["count"]["minimum"] == 0
        assert props["count"]["maximum"] == 5

    def test_optional_field_has_no_default_and_not_required(self) -> None:
        """Optional fields must not have a default key and must not be in the required array."""
        output_schema = {
            "required_value": OutputField(type="string"),
            "optional_value": OutputField(type="string", required=False),
        }
        schema = self._schema(output_schema)
        props = schema["properties"]
        assert "default" not in props["optional_value"]
        assert "optional_value" not in schema.get("required", [])
        assert "required_value" in schema["required"]


class TestOmittedOptionalMaterialization:
    """Requirement: model_dump(exclude_unset=True) drops omitted optional keys."""

    def test_unset_optional_field_is_dropped(self) -> None:
        """An optional field the model never set must be absent from
        ``model_dump(exclude_unset=True)`` so ``extract_content`` does not
        materialize it as ``None``."""
        output_schema = {
            "score": OutputField(type="number"),
            "extra": OutputField(type="string", required=False, nullable=False),
        }
        dynamic_model = output_schema_to_pydantic_model("DumpOutput", output_schema)
        assert dynamic_model is not None

        instance = dynamic_model.model_construct(score=1)
        assert instance.model_dump(exclude_unset=True) == {"score": 1}


class TestArrayItemConstraints:
    """Requirement: array-item scalar constraints are enforced like top-level fields."""

    def _model(self, output_schema: dict[str, OutputField]) -> type[BaseModel]:
        dynamic_model = output_schema_to_pydantic_model("ArrayItemOutput", output_schema)
        assert dynamic_model is not None
        return dynamic_model

    def test_array_item_enum_rejects_non_member(self) -> None:
        """An array of strings with an enum must reject a non-member item."""
        output_schema = {
            "values": OutputField(
                type="array",
                items=OutputField(type="string", enum=["ok"]),
            )
        }
        dynamic_model = self._model(output_schema)

        assert dynamic_model(values=["ok"]).values == ["ok"]
        with pytest.raises(PydanticValidationError):
            dynamic_model(values=["bad"])

    def test_array_item_pattern_and_length_reject_violations(self) -> None:
        """Array-item pattern and length constraints must reject violating items."""
        output_schema = {
            "values": OutputField(
                type="array",
                items=OutputField(type="string", pattern="^o+$", minLength=2, maxLength=2),
            )
        }
        dynamic_model = self._model(output_schema)

        assert dynamic_model(values=["oo"]).values == ["oo"]
        with pytest.raises(PydanticValidationError):
            dynamic_model(values=["bad"])  # pattern fail
        with pytest.raises(PydanticValidationError):
            dynamic_model(values=["o"])  # length fail
        with pytest.raises(PydanticValidationError):
            dynamic_model(values=["ooo"])  # length fail

    def test_array_item_number_range_reject_violations(self) -> None:
        """Array-item number range constraints must reject out-of-bounds values."""
        output_schema = {
            "values": OutputField(
                type="array",
                items=OutputField(type="number", minimum=0, maximum=10),
            )
        }
        dynamic_model = self._model(output_schema)

        assert dynamic_model(values=[0, 10]).values == [0, 10]
        with pytest.raises(PydanticValidationError):
            dynamic_model(values=[-1])
        with pytest.raises(PydanticValidationError):
            dynamic_model(values=[11])

    def test_array_item_nullable_accepts_none(self) -> None:
        """An array of nullable strings must accept ``None`` items."""
        output_schema = {
            "values": OutputField(
                type="array",
                items=OutputField(type="string", nullable=True),
            )
        }
        dynamic_model = self._model(output_schema)

        instance = dynamic_model(values=["ok", None])
        assert instance.values == ["ok", None]

    def test_array_item_non_nullable_rejects_none(self) -> None:
        """An array of non-nullable strings must reject ``None`` items."""
        output_schema = {
            "values": OutputField(
                type="array",
                items=OutputField(type="string", nullable=False),
            )
        }
        dynamic_model = self._model(output_schema)

        with pytest.raises(PydanticValidationError):
            dynamic_model(values=[None])


class TestToolSchemaForbiddenKeywords:
    """Requirement: generated tool JSON schemas contain no forbidden keywords."""

    def _schema(self, output_schema: dict[str, OutputField]) -> dict[str, Any]:
        """Build an agent and return the final_result tool JSON schema."""
        agent_def = AgentDef(name="formatter", output=output_schema)
        pydantic_agent = build_agent(agent_def, system_prompt="", rendered_prompt="")
        assert isinstance(pydantic_agent.output_type, ToolOutput)
        toolset = pydantic_agent._output_schema.toolset
        assert toolset is not None
        assert len(toolset._tool_defs) == 1
        return toolset._tool_defs[0].parameters_json_schema

    def _collect_keywords(self, node: Any, found: set[str]) -> None:
        if isinstance(node, dict):
            found.update(node.keys())
            for value in node.values():
                self._collect_keywords(value, found)
        elif isinstance(node, list):
            for item in node:
                self._collect_keywords(item, found)

    def test_schema_has_no_anyof_ref_defs_for_nullable_object(self) -> None:
        """A nullable nested object must not produce anyOf, $ref, or $defs and must
        preserve the nested object structure and nullability."""
        output_schema = {
            "wrapper": OutputField(
                type="object",
                properties={
                    "item": OutputField(
                        type="object",
                        nullable=True,
                        properties={
                            "tag": OutputField(type="string", enum=["a", "b"]),
                        },
                    )
                },
            )
        }
        schema = self._schema(output_schema)
        keywords: set[str] = set()
        self._collect_keywords(schema, keywords)

        assert "anyOf" not in keywords
        assert "$ref" not in keywords
        assert "$defs" not in keywords

        item_schema = schema["properties"]["wrapper"]["properties"]["item"]
        assert set(item_schema["type"]) == {"object", "null"}
        assert item_schema["properties"]["tag"]["enum"] == ["a", "b"]

    def test_array_item_constraints_appear_in_schema(self) -> None:
        """Array-item constraints must be advertised in the generated tool schema."""
        output_schema = {
            "values": OutputField(
                type="array",
                items=OutputField(
                    type="string",
                    enum=["ok"],
                    pattern="^o$",
                    minLength=2,
                    maxLength=2,
                ),
            )
        }
        schema = self._schema(output_schema)
        item_schema = schema["properties"]["values"]["items"]

        assert item_schema["type"] == "string"
        assert item_schema["enum"] == ["ok"]
        assert item_schema["pattern"] == "^o$"
        assert item_schema["minLength"] == 2
        assert item_schema["maxLength"] == 2

    def test_array_item_nullable_schema_uses_null_type(self) -> None:
        """Nullable array items must be represented as type ["string", "null"]."""
        output_schema = {
            "values": OutputField(
                type="array",
                items=OutputField(type="string", nullable=True),
            )
        }
        schema = self._schema(output_schema)
        item_schema = schema["properties"]["values"]["items"]

        assert "anyOf" not in item_schema
        assert set(item_schema["type"]) == {"string", "null"}
