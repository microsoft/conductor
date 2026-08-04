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

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import ValidationError as ConductorValidationError
from conductor.executor.output import validate_output
from conductor.providers._pydantic_ai.converters import output_schema_to_pydantic_model
from conductor.providers.copilot import CopilotProvider


def _stub_handler(agent: AgentDef, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
    """Return a minimal dict for CopilotProvider mock-handler construction."""
    return {}


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


class TestClaudeAgentSdkOutputFormat:
    """claude_agent_sdk._build_output_format must carry every constraint keyword."""

    def test_claude_agent_sdk_output_format_contains_all_constraint_keywords(self) -> None:
        """The SDK output_format payload must contain enum, pattern, length,
        range, and nullable keywords in the inner JSON schema, and must mark
        required fields only (not optional ones)."""
        pytest.importorskip(
            "claude_agent_sdk",
            reason="claude-agent-sdk extra not installed",
        )
        from conductor.providers.claude_agent_sdk import _build_output_format

        payload = _build_output_format(SHARED_OUTPUT_SCHEMA)

        assert payload["type"] == "json_schema"
        schema = payload["schema"]
        props = schema["properties"]

        assert schema["required"] == ["category", "code", "score"]

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


class TestClaudePydanticModel:
    """output_schema_to_pydantic_model must enforce the constraints."""

    def test_claude_dynamic_model_accepts_conforming_payload(self) -> None:
        """A payload satisfying every constraint must validate cleanly."""
        model = output_schema_to_pydantic_model("Constrained", SHARED_OUTPUT_SCHEMA)
        assert model is not None

        instance = model.model_validate({"category": "A", "code": "XYZ", "score": 42})
        assert instance.model_dump()["category"] == "A"
        assert instance.model_dump()["code"] == "XYZ"
        assert instance.model_dump()["score"] == 42

    def test_claude_dynamic_model_rejects_violating_payload(self) -> None:
        """A payload violating a constraint must raise Pydantic ValidationError."""
        model = output_schema_to_pydantic_model("Constrained", SHARED_OUTPUT_SCHEMA)
        assert model is not None

        with pytest.raises(PydanticValidationError):
            model(category="Z", code="XYZ", score=42)


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
