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
