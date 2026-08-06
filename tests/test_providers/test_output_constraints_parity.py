"""Provider parity tests for OutputField constraint extensions.

These tests assert that a single shared output schema containing every
constraint keyword (enum, pattern, range, length, optional, nullable) is
translated consistently across all provider surfaces. They do not repeat the
individual constraint checks of the validator and schema builders; they verify
parity.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError as PydanticValidationError
from pydantic_ai.output import ToolOutput

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import ValidationError as ConductorValidationError
from conductor.executor.output import validate_output
from conductor.providers._pydantic_ai.agent_builder import build_agent
from conductor.providers._pydantic_ai.converters import output_schema_to_pydantic_model
from conductor.providers.copilot import CopilotProvider


def _stub_handler(agent: AgentDef, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
    """Return a minimal dict for CopilotProvider mock-handler construction."""
    return {}


def _extract_output_tool_schema(agent: Any) -> dict[str, Any] | None:
    """Return the sanitized parameters_json_schema for the output tool."""
    if not isinstance(agent.output_type, ToolOutput):
        return None
    toolset = agent._output_schema.toolset
    if toolset is None or not toolset._tool_defs:
        return None
    return toolset._tool_defs[0].parameters_json_schema


def _assert_no_keys(node: Any, *keys: str) -> None:
    """Recursively assert that none of the given JSON Schema keys appear."""
    if isinstance(node, dict):
        for key in keys:
            assert key not in node, f"forbidden key {key!r} found in schema: {node}"
        for value in node.values():
            _assert_no_keys(value, *keys)
    elif isinstance(node, list):
        for item in node:
            _assert_no_keys(item, *keys)


# Shared schema exercising every constraint keyword supported by OutputField.
# It intentionally mixes nullable, optional, and required fields so the tests
# can verify that each keyword survives its provider-specific transformation.
SHARED_OUTPUT_SCHEMA = {
    "category": OutputField(type="string", enum=["A", "B", "C"]),
    "code": OutputField(
        type="string",
        pattern=r"^[A-Z]{3}$",
        minLength=3,
        maxLength=3,
        nullable=True,
    ),
    "score": OutputField(type="number", minimum=0, maximum=100),
    "label": OutputField(
        type="string",
        minLength=1,
        maxLength=10,
        required=False,
    ),
    "ratio": OutputField(type="number", minimum=0, maximum=10, nullable=True),
    "nullable_enum": OutputField(type="string", enum=["A", "B"], nullable=True),
    "rows": OutputField(
        type="array",
        items=OutputField(
            type="object",
            nullable=True,
            properties={"name": OutputField(type="string")},
        ),
    ),
    "nested": OutputField(
        type="object",
        properties={
            "req": OutputField(type="string"),
            "opt": OutputField(type="string", required=False),
        },
    ),
}


class TestCopilotPromptSchema:
    """CopilotProvider._build_prompt_schema must carry every constraint keyword."""

    def test_copilot_prompt_schema_contains_all_constraint_keywords(self) -> None:
        """The prompt-facing schema produced for Copilot must include enum,
        pattern, minLength, maxLength, minimum, maximum, nullable (as a type
        list containing 'null'), and must omit optional fields from any implicit
        required list."""
        provider = CopilotProvider(mock_handler=_stub_handler)
        schema = provider._build_prompt_schema(SHARED_OUTPUT_SCHEMA)

        category = schema["category"]
        assert category["type"] == "string"
        assert category["enum"] == ["A", "B", "C"]

        code = schema["code"]
        assert code["type"] == ["string", "null"]
        assert code["pattern"] == r"^[A-Z]{3}$"
        assert code["minLength"] == 3
        assert code["maxLength"] == 3

        score = schema["score"]
        assert score["type"] == "number"
        assert score["minimum"] == 0
        assert score["maximum"] == 100

        label = schema["label"]
        assert label["type"] == "string"
        assert label["minLength"] == 1
        assert label["maxLength"] == 10

        ratio = schema["ratio"]
        assert ratio["type"] == ["number", "null"]
        assert ratio["minimum"] == 0
        assert ratio["maximum"] == 10

        nullable_enum = schema["nullable_enum"]
        assert nullable_enum["type"] == ["string", "null"]
        assert nullable_enum["enum"] == ["A", "B", None]

        rows = schema["rows"]
        assert rows["type"] == "array"
        assert rows["items"]["type"] == ["object", "null"]
        assert rows["items"]["properties"]["name"]["type"] == "string"
        assert rows["items"]["required"] == ["name"]

        nested = schema["nested"]
        assert nested["type"] == "object"
        assert nested["required"] == ["req"]
        assert nested["properties"]["req"]["type"] == "string"
        assert nested["properties"]["opt"]["type"] == "string"


class TestHermesPromptSchema:
    """HermesProvider._build_prompt_schema must carry every constraint keyword."""

    @patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True)
    @patch("conductor.providers.hermes.AIAgent", MagicMock())
    def test_hermes_prompt_schema_contains_all_constraint_keywords(self) -> None:
        """The prompt-facing schema produced for Hermes must include the same
        constraint keywords as the Copilot surface, since both delegate to the
        shared builder."""
        from conductor.providers.hermes import _build_prompt_schema

        schema = _build_prompt_schema(SHARED_OUTPUT_SCHEMA)

        category = schema["category"]
        assert category["type"] == "string"
        assert category["enum"] == ["A", "B", "C"]

        code = schema["code"]
        assert code["type"] == ["string", "null"]
        assert code["pattern"] == r"^[A-Z]{3}$"
        assert code["minLength"] == 3
        assert code["maxLength"] == 3

        score = schema["score"]
        assert score["type"] == "number"
        assert score["minimum"] == 0
        assert score["maximum"] == 100

        label = schema["label"]
        assert label["type"] == "string"
        assert label["minLength"] == 1
        assert label["maxLength"] == 10

        ratio = schema["ratio"]
        assert ratio["type"] == ["number", "null"]
        assert ratio["minimum"] == 0
        assert ratio["maximum"] == 10

        nullable_enum = schema["nullable_enum"]
        assert nullable_enum["type"] == ["string", "null"]
        assert nullable_enum["enum"] == ["A", "B", None]

        rows = schema["rows"]
        assert rows["type"] == "array"
        assert rows["items"]["type"] == ["object", "null"]
        assert rows["items"]["properties"]["name"]["type"] == "string"
        assert rows["items"]["required"] == ["name"]

        nested = schema["nested"]
        assert nested["type"] == "object"
        assert nested["required"] == ["req"]
        assert nested["properties"]["req"]["type"] == "string"
        assert nested["properties"]["opt"]["type"] == "string"


class TestClaudeAgentSdkOutputFormat:
    """claude_agent_sdk._build_output_format must carry every constraint keyword."""

    def test_claude_agent_sdk_output_format_contains_all_constraint_keywords(self) -> None:
        """The SDK output_format payload must contain enum, pattern, length,
        range, and nullable keywords in the inner JSON schema, and must mark
        all declared fields required in the schema sent to the SDK."""
        pytest.importorskip(
            "claude_agent_sdk",
            reason="claude-agent-sdk extra not installed",
        )
        from conductor.providers.claude_agent_sdk import _build_output_format

        payload = _build_output_format(SHARED_OUTPUT_SCHEMA)

        assert payload["type"] == "json_schema"
        schema = payload["schema"]
        props = schema["properties"]

        assert schema["required"] == list(SHARED_OUTPUT_SCHEMA.keys())

        category = props["category"]
        assert category["type"] == "string"
        assert category["enum"] == ["A", "B", "C"]

        code = props["code"]
        assert code["type"] == ["string", "null"]
        assert code["pattern"] == r"^[A-Z]{3}$"
        assert code["minLength"] == 3
        assert code["maxLength"] == 3

        score = props["score"]
        assert score["type"] == "number"
        assert score["minimum"] == 0
        assert score["maximum"] == 100

        label = props["label"]
        assert label["type"] == "string"
        assert label["minLength"] == 1
        assert label["maxLength"] == 10

        ratio = props["ratio"]
        assert ratio["type"] == ["number", "null"]
        assert ratio["minimum"] == 0
        assert ratio["maximum"] == 10

        nullable_enum = props["nullable_enum"]
        assert nullable_enum["type"] == ["string", "null"]
        assert nullable_enum["enum"] == ["A", "B", None]

        rows = props["rows"]
        assert rows["type"] == "array"
        assert rows["items"]["type"] == ["object", "null"]
        assert rows["items"]["properties"]["name"]["type"] == "string"
        assert rows["items"]["required"] == ["name"]

        nested = props["nested"]
        assert nested["type"] == "object"
        assert nested["required"] == ["req"]
        assert nested["properties"]["req"]["type"] == "string"
        assert nested["properties"]["opt"]["type"] == "string"


@pytest.fixture(scope="module")
def parity_model() -> type[Any]:
    """Build the Claude dynamic model once for the payload matrix."""
    model = output_schema_to_pydantic_model("Parity", SHARED_OUTPUT_SCHEMA)
    assert model is not None
    return model


class TestClaudePydanticModel:
    """output_schema_to_pydantic_model must enforce the constraints."""

    def test_claude_dynamic_model_accepts_conforming_payload(self, parity_model: type[Any]) -> None:
        """A payload satisfying every constraint must validate cleanly."""
        instance = parity_model.model_validate(
            {
                "category": "A",
                "code": "XYZ",
                "score": 42,
                "ratio": None,
                "nullable_enum": None,
                "rows": [None, {"name": "x"}],
                "nested": {"req": "r"},
            }
        )
        dumped = instance.model_dump()
        assert dumped["category"] == "A"
        assert dumped["code"] == "XYZ"
        assert dumped["score"] == 42
        assert dumped["ratio"] is None
        assert dumped["nullable_enum"] is None
        assert dumped["rows"][0] is None
        assert dumped["rows"][1]["name"] == "x"
        assert dumped["nested"]["req"] == "r"

    def test_claude_dynamic_model_rejects_violating_payload(self, parity_model: type[Any]) -> None:
        """A payload violating a constraint must raise Pydantic ValidationError."""
        with pytest.raises(PydanticValidationError):
            parity_model(category="Z", code="XYZ", score=42)


class TestClaudeValidateOutputParity:
    """The Claude dynamic model and validate_output must agree on every payload.

    This matrix exists because the reviewer mutation-tested the original parity
    fixture: deleting all range/length enforcement or all nullable handling from
    the Claude path left the test file green. Each row pins a concrete accept or
    reject decision and asserts that both enforcement paths reach the same
    verdict, so a missing constraint in either path breaks the test.
    """

    _BASE = {
        "category": "A",
        "code": "ABC",
        "score": 5,
        "ratio": None,
        "nullable_enum": None,
        "rows": [],
        "nested": {"req": "r"},
    }

    @pytest.mark.parametrize(
        ("payload", "expect_accept"),
        [
            (
                {
                    "category": "A",
                    "code": "ABC",
                    "score": 5,
                    "label": "ok",
                    "ratio": None,
                    "nullable_enum": None,
                    "rows": [None, {"name": "x"}],
                    "nested": {"req": "r"},
                },
                True,
            ),
            (
                # Minimal valid payload: optional label omitted, nullable
                # fields supplied as None, nested optional property omitted.
                {
                    "category": "A",
                    "code": None,
                    "score": 5,
                    "ratio": None,
                    "nullable_enum": None,
                    "rows": [],
                    "nested": {"req": "r"},
                },
                True,
            ),
            ({**_BASE, "ratio": 0}, True),
            ({**_BASE, "ratio": 10}, True),
            ({**_BASE, "ratio": 2.5}, True),
            ({**_BASE, "nullable_enum": "B"}, True),
            ({**_BASE, "ratio": 42}, False),
            ({**_BASE, "ratio": -1}, False),
            ({**_BASE, "nullable_enum": "Z"}, False),
            ({**_BASE, "score": None}, False),
            ({**_BASE, "rows": [{"name": 7}]}, False),
            ({**_BASE, "nested": {"req": "r", "opt": 5}}, False),
            ({**_BASE, "nested": {}}, False),
            ({**_BASE, "code": "AB"}, False),
            ({**_BASE, "category": "D"}, False),
            ({**_BASE, "score": 101}, False),
        ],
    )
    def test_claude_and_validate_output_agree(
        self,
        parity_model: type[Any],
        payload: dict[str, Any],
        expect_accept: bool,
    ) -> None:
        """Both enforcement paths must accept or reject the payload together."""
        model_accepted = self._call_accepts(parity_model.model_validate, payload)
        validate_accepted = self._call_accepts(validate_output, payload, SHARED_OUTPUT_SCHEMA)

        assert model_accepted == validate_accepted, (
            f"payload {payload!r}: model accepted={model_accepted}, "
            f"validate_output accepted={validate_accepted}"
        )
        assert model_accepted == expect_accept, (
            f"payload {payload!r}: expected accept={expect_accept}, got {model_accepted}"
        )

    @staticmethod
    def _call_accepts(func: Any, *args: Any, **kwargs: Any) -> bool:
        """Return True if the call succeeds, False if it raises a validation error."""
        try:
            func(*args, **kwargs)
        except (PydanticValidationError, ConductorValidationError):
            return False
        return True


class TestClaudeToolSchemaHygiene:
    """The Pydantic AI output tool schema must sanitize internal pydantic keys."""

    def test_tool_schema_for_shared_schema_carries_constraints_and_no_defaults(self) -> None:
        """The schema attached to the output tool must advertise nullable range
        and enum constraints, must keep nullable object/array items honest, and
        must never contain the pydantic-internal ``ge``/``le``/``default`` keys
        anywhere in the schema tree."""
        agent_def = AgentDef(name="parity", output=SHARED_OUTPUT_SCHEMA)
        pydantic_agent = build_agent(
            agent_def,
            system_prompt="sys",
            rendered_prompt="p",
            default_model="claude-sonnet-5",
            api_key="sk-test-dummy",
        )

        schema = _extract_output_tool_schema(pydantic_agent)
        assert schema is not None
        _assert_no_keys(schema, "ge", "le", "default")

        props = schema["properties"]

        ratio_schema = props["ratio"]
        assert isinstance(ratio_schema["type"], list)
        assert "null" in ratio_schema["type"]
        assert ratio_schema["minimum"] == 0
        assert ratio_schema["maximum"] == 10

        nullable_enum_schema = props["nullable_enum"]
        assert isinstance(nullable_enum_schema["type"], list)
        assert "null" in nullable_enum_schema["type"]
        assert None in nullable_enum_schema["enum"]

        rows_schema = props["rows"]
        items_schema = rows_schema["items"]
        assert isinstance(items_schema["type"], list)
        assert "null" in items_schema["type"]
        assert items_schema["properties"]["name"]["type"] == "string"
        assert items_schema["required"] == ["name"]

        nested_schema = props["nested"]
        assert nested_schema["type"] == "object"
        assert nested_schema["required"] == ["req"]
        assert nested_schema["properties"]["req"]["type"] == "string"
        assert nested_schema["properties"]["opt"]["type"] == "string"


class TestValidateOutputParity:
    """validate_output must raise identical messages regardless of provider path."""

    def test_validate_output_raises_identical_message_for_enum_violation(self) -> None:
        """The provider-agnostic validation path must surface the same
        constraint error message for a payload that violates an enum."""
        with pytest.raises(ConductorValidationError) as exc_info:
            validate_output(
                {"category": "Z", "code": "ABC", "score": 50},
                SHARED_OUTPUT_SCHEMA,
            )

        assert "must be one of" in str(exc_info.value)
        assert "category" in str(exc_info.value)


class TestAcaWireBoundary:
    """ACA host->runner serialization must preserve every constraint field."""

    def _make_provider(self) -> Any:
        from conductor.config.schema import ProviderSettings
        from conductor.providers.aca import AcaRuntimeProvider

        settings = ProviderSettings(
            name="aca",
            pool_endpoint="https://pool.example.com",
            api_version="2025-07-01",
        )
        with patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True):
            return AcaRuntimeProvider(provider_settings=settings)

    def test_aca_request_carries_constraint_fields(self) -> None:
        """AcaRuntimeProvider._build_request must serialize enum, pattern,
        range, length, required, and nullable into request.agent.output."""
        provider = self._make_provider()
        agent = AgentDef(
            name="constrained",
            prompt="test",
            output=SHARED_OUTPUT_SCHEMA,
        )

        request = provider._build_request(agent, {}, "rendered", None)
        wire_output = request.agent.output
        assert wire_output is not None

        category = wire_output["category"]
        assert category["type"] == "string"
        assert category["enum"] == ["A", "B", "C"]
        # required=True and nullable=False are defaults and should be omitted
        # to keep the wire payload small.
        assert "required" not in category
        assert "nullable" not in category

        code = wire_output["code"]
        assert code["type"] == "string"
        assert code["pattern"] == r"^[A-Z]{3}$"
        assert code["minLength"] == 3
        assert code["maxLength"] == 3
        assert code["nullable"] is True
        assert "required" not in code

        score = wire_output["score"]
        assert score["type"] == "number"
        assert score["minimum"] == 0
        assert score["maximum"] == 100

        label = wire_output["label"]
        assert label["type"] == "string"
        assert label["minLength"] == 1
        assert label["maxLength"] == 10
        assert label["required"] is False
        assert "nullable" not in label

        ratio = wire_output["ratio"]
        assert ratio["type"] == "number"
        assert ratio["nullable"] is True
        assert ratio["minimum"] == 0
        assert ratio["maximum"] == 10
        assert isinstance(ratio["minimum"], int)
        assert isinstance(ratio["maximum"], int)

        rows = wire_output["rows"]
        assert rows["type"] == "array"
        assert rows["items"]["type"] == "object"
        assert rows["items"]["nullable"] is True
        assert rows["items"]["properties"]["name"]["type"] == "string"

        nested = wire_output["nested"]
        assert nested["type"] == "object"
        assert nested["properties"]["req"]["type"] == "string"
        assert nested["properties"]["opt"]["type"] == "string"
        assert nested["properties"]["opt"]["required"] is False

    def test_aca_runner_reconstructs_identical_output_schema(self) -> None:
        """The runner's OutputField.model_validate must reconstruct the same
        effective field values from the wire payload."""
        provider = self._make_provider()
        agent = AgentDef(
            name="constrained",
            prompt="test",
            output=SHARED_OUTPUT_SCHEMA,
        )

        request = provider._build_request(agent, {}, "rendered", None)
        wire_output = request.agent.output
        assert wire_output is not None

        # Simulate the runner reconstruction path from aca_runner/server.py.
        reconstructed = {
            name: OutputField.model_validate(field) for name, field in wire_output.items()
        }

        for name in SHARED_OUTPUT_SCHEMA:
            original = SHARED_OUTPUT_SCHEMA[name]
            rebuilt = reconstructed[name]
            assert original.type == rebuilt.type
            assert original.enum == rebuilt.enum
            assert original.pattern == rebuilt.pattern
            assert original.minimum == rebuilt.minimum
            assert original.maximum == rebuilt.maximum
            assert original.minLength == rebuilt.minLength
            assert original.maxLength == rebuilt.maxLength
            assert original.required == rebuilt.required
            assert original.nullable == rebuilt.nullable

        # Reconstruct nested object and array-item shape from the wire payload.
        rows_original = SHARED_OUTPUT_SCHEMA["rows"].items
        rows_rebuilt = reconstructed["rows"].items
        assert rows_original is not None
        assert rows_rebuilt is not None
        assert rows_original.type == rows_rebuilt.type
        assert rows_original.nullable == rows_rebuilt.nullable
        assert rows_original.properties is not None
        assert rows_rebuilt.properties is not None
        assert rows_original.properties["name"].type == rows_rebuilt.properties["name"].type

        nested_original = SHARED_OUTPUT_SCHEMA["nested"].properties
        nested_rebuilt = reconstructed["nested"].properties
        assert nested_original is not None
        assert nested_rebuilt is not None
        assert nested_original["req"].type == nested_rebuilt["req"].type
        assert nested_original["req"].required == nested_rebuilt["req"].required
        assert nested_original["opt"].type == nested_rebuilt["opt"].type
        assert nested_original["opt"].required == nested_rebuilt["opt"].required

    def test_aca_wire_body_preserves_constraint_fields(self) -> None:
        """The actual JSON body sent to the runner must contain the constraint
        fields after SecretStr unwrapping."""
        provider = self._make_provider()
        agent = AgentDef(
            name="constrained",
            prompt="test",
            output=SHARED_OUTPUT_SCHEMA,
        )

        request = provider._build_request(agent, {}, "rendered", None)
        body = provider._wire_body(request)

        wire_output = body["agent"]["output"]
        assert wire_output is not None
        assert wire_output["category"]["enum"] == ["A", "B", "C"]
        assert wire_output["code"]["pattern"] == r"^[A-Z]{3}$"
        assert wire_output["code"]["nullable"] is True
        assert wire_output["score"]["minimum"] == 0
        assert wire_output["score"]["maximum"] == 100
        assert wire_output["label"]["required"] is False

        ratio = wire_output["ratio"]
        assert ratio["nullable"] is True
        assert ratio["minimum"] == 0
        assert ratio["maximum"] == 10
        assert isinstance(ratio["minimum"], int)
        assert isinstance(ratio["maximum"], int)

        rows = wire_output["rows"]
        assert rows["items"]["type"] == "object"
        assert rows["items"]["nullable"] is True
        assert rows["items"]["properties"]["name"]["type"] == "string"

        nested = wire_output["nested"]
        assert nested["properties"]["req"]["type"] == "string"
        assert nested["properties"]["opt"]["type"] == "string"
        assert nested["properties"]["opt"]["required"] is False
