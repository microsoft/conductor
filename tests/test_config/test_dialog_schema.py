"""Tests for dialog mode schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.config.schema import (
    AgentDef,
    DialogConfig,
    GateOption,
    RouteDef,
)


class TestDialogConfig:
    """Tests for DialogConfig model."""

    def test_valid_dialog_config(self) -> None:
        """Test creating a valid dialog config."""
        config = DialogConfig(trigger_prompt="Enter dialog if uncertain")
        assert config.trigger_prompt == "Enter dialog if uncertain"

    def test_dialog_config_requires_trigger_prompt(self) -> None:
        """Test that trigger_prompt is required."""
        with pytest.raises(ValidationError, match="trigger_prompt"):
            DialogConfig()  # type: ignore[call-arg]

    def test_multiline_trigger_prompt(self) -> None:
        """Test that multiline trigger prompts work."""
        config = DialogConfig(trigger_prompt="Enter dialog if:\n- uncertain\n- needs clarification")
        assert "\n" in config.trigger_prompt


class TestAgentDefDialog:
    """Tests for dialog field on AgentDef."""

    def test_agent_with_dialog(self) -> None:
        """Test creating a regular agent with dialog config."""
        agent = AgentDef(
            name="researcher",
            prompt="Research the topic",
            dialog=DialogConfig(
                trigger_prompt="Enter dialog if uncertain about scope",
            ),
        )
        assert agent.dialog is not None
        assert agent.dialog.trigger_prompt == "Enter dialog if uncertain about scope"

    def test_agent_without_dialog(self) -> None:
        """Test that dialog defaults to None."""
        agent = AgentDef(name="researcher", prompt="Research the topic")
        assert agent.dialog is None

    def test_human_gate_cannot_have_dialog(self) -> None:
        """Test that human_gate agents cannot have dialog config."""
        with pytest.raises(ValidationError, match="human_gate agents cannot have 'dialog'"):
            AgentDef(
                name="gate",
                type="human_gate",
                prompt="Choose an option",
                options=[GateOption(label="Continue", value="continue", route="next")],
                dialog=DialogConfig(trigger_prompt="test"),
            )

    def test_script_cannot_have_dialog(self) -> None:
        """Test that script agents cannot have dialog config."""
        with pytest.raises(ValidationError, match="script agents cannot have 'dialog'"):
            AgentDef(
                name="runner",
                type="script",
                command="echo hello",
                dialog=DialogConfig(trigger_prompt="test"),
            )

    def test_workflow_cannot_have_dialog(self) -> None:
        """Test that workflow agents cannot have dialog config."""
        with pytest.raises(ValidationError, match="workflow agents cannot have 'dialog'"):
            AgentDef(
                name="sub",
                type="workflow",
                workflow="./sub.yaml",
                dialog=DialogConfig(trigger_prompt="test"),
            )

    def test_agent_with_dialog_and_routes(self) -> None:
        """Test that agents with dialog can also have routes."""
        agent = AgentDef(
            name="researcher",
            prompt="Research the topic",
            dialog=DialogConfig(trigger_prompt="Enter dialog if uncertain"),
            routes=[
                RouteDef(to="next_agent", when="{{ output.result == 'done' }}"),
                RouteDef(to="$end"),
            ],
        )
        assert agent.dialog is not None
        assert len(agent.routes) == 2
