"""Tests for the output_mode field on AgentDef."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.config.schema import AgentDef, OutputField


class TestOutputModeValidation:
    """Tests for output_mode validation rules on AgentDef."""

    def test_raw_without_output_is_valid(self) -> None:
        """output_mode='raw' with no output schema is valid."""
        agent = AgentDef(name="a", prompt="p", output_mode="raw")
        assert agent.output_mode == "raw"
        assert agent.output is None

    def test_envelope_with_output_is_valid(self) -> None:
        """output_mode='envelope' with output schema is valid."""
        agent = AgentDef(
            name="a",
            prompt="p",
            output_mode="envelope",
            output={"field": OutputField(type="string")},
        )
        assert agent.output_mode == "envelope"
        assert agent.output is not None

    def test_raw_with_output_raises_validation_error(self) -> None:
        """output_mode='raw' combined with output schema is rejected."""
        with pytest.raises(ValidationError, match="output_mode 'raw' is incompatible"):
            AgentDef(
                name="a",
                prompt="p",
                output_mode="raw",
                output={"field": OutputField(type="string")},
            )

    def test_raw_on_script_raises_validation_error(self) -> None:
        """output_mode on script agent type is rejected."""
        with pytest.raises(ValidationError, match="script agents cannot have 'output_mode'"):
            AgentDef(
                name="a",
                type="script",
                command="echo hi",
                output_mode="raw",
            )

    def test_raw_on_human_gate_raises_validation_error(self) -> None:
        """output_mode on human_gate agent type is rejected."""
        from conductor.config.schema import GateOption

        with pytest.raises(ValidationError, match="human_gate agents cannot have 'output_mode'"):
            AgentDef(
                name="a",
                type="human_gate",
                prompt="Choose",
                options=[GateOption(value="yes", label="Yes", route="next")],
                output_mode="raw",
            )

    def test_raw_on_workflow_raises_validation_error(self) -> None:
        """output_mode on workflow agent type is rejected."""
        with pytest.raises(ValidationError, match="workflow agents cannot have 'output_mode'"):
            AgentDef(
                name="a",
                type="workflow",
                workflow="sub.yaml",
                output_mode="raw",
            )

    def test_none_with_output_is_valid(self) -> None:
        """output_mode=None (default) with output schema is valid — backward compat."""
        agent = AgentDef(
            name="a",
            prompt="p",
            output={"field": OutputField(type="string")},
        )
        assert agent.output_mode is None
        assert agent.output is not None

    def test_none_without_output_is_valid(self) -> None:
        """output_mode=None (default) without output schema is valid — backward compat."""
        agent = AgentDef(name="a", prompt="p")
        assert agent.output_mode is None
        assert agent.output is None

    def test_envelope_without_output_is_valid(self) -> None:
        """output_mode='envelope' without output schema is valid (no-op, wraps as result)."""
        agent = AgentDef(name="a", prompt="p", output_mode="envelope")
        assert agent.output_mode == "envelope"
        assert agent.output is None

    def test_invalid_output_mode_value_rejected(self) -> None:
        """An invalid output_mode string is rejected by the Literal type."""
        with pytest.raises(ValidationError):
            AgentDef(name="a", prompt="p", output_mode="invalid")  # type: ignore[arg-type]
