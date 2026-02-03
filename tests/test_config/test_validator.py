"""Tests for the cross-field validator."""

from __future__ import annotations

import pytest

from conductor.config.schema import (
    AgentDef,
    GateOption,
    InputDef,
    RouteDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError


class TestValidateWorkflowConfig:
    """Tests for the validate_workflow_config function."""

    def test_valid_simple_config(self) -> None:
        """Test validation of a valid simple config."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Hello", routes=[RouteDef(to="$end")])
            ],
        )
        # Should not raise
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_valid_multi_agent_config(self) -> None:
        """Test validation of a valid multi-agent config."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Step 1",
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Step 2",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)


class TestRouteValidation:
    """Tests for route target validation."""

    def test_valid_route_to_agent(self) -> None:
        """Test that route to existing agent is valid."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Step 1",
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Step 2",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        # Should not raise
        validate_workflow_config(config)

    def test_valid_route_to_end(self) -> None:
        """Test that route to $end is valid."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Hello", routes=[RouteDef(to="$end")])
            ],
        )
        # Should not raise
        validate_workflow_config(config)


class TestHumanGateValidation:
    """Tests for human gate validation."""

    def test_valid_human_gate(self) -> None:
        """Test validation of a valid human gate."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="gate1"),
            agents=[
                AgentDef(
                    name="gate1",
                    type="human_gate",
                    prompt="Choose:",
                    options=[
                        GateOption(label="Yes", value="yes", route="agent2"),
                        GateOption(label="No", value="no", route="$end"),
                    ],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Hello",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        # Should not raise
        validate_workflow_config(config)

    def test_gate_option_invalid_route(self) -> None:
        """Test that gate option with invalid route raises error."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="gate1"),
            agents=[
                AgentDef(
                    name="gate1",
                    type="human_gate",
                    prompt="Choose:",
                    options=[
                        GateOption(label="Yes", value="yes", route="nonexistent"),
                    ],
                ),
            ],
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "nonexistent" in str(exc_info.value)


class TestInputReferenceValidation:
    """Tests for input reference validation."""

    def test_valid_workflow_input_reference(self) -> None:
        """Test valid workflow input reference."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="agent1",
                input={"goal": InputDef(type="string")},
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    input=["workflow.input.goal"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        # Should not raise
        validate_workflow_config(config)

    def test_valid_agent_output_reference(self) -> None:
        """Test valid agent output reference."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Step 1",
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Step 2",
                    input=["agent1.output"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        # Should not raise
        validate_workflow_config(config)

    def test_invalid_input_reference_format(self) -> None:
        """Test that invalid input reference format raises error."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    input=["invalid_format"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "invalid_format" in str(exc_info.value)

    def test_reference_to_unknown_agent(self) -> None:
        """Test that reference to unknown agent raises error."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    input=["unknown.output"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "unknown" in str(exc_info.value)

    def test_reference_to_unknown_workflow_input(self) -> None:
        """Test that reference to unknown workflow input raises error."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    input=["workflow.input.nonexistent"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "nonexistent" in str(exc_info.value)

    def test_optional_reference_to_unknown_agent_warns(self) -> None:
        """Test that optional reference to unknown agent produces warning."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    input=["unknown.output?"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        warnings = validate_workflow_config(config)
        assert any("unknown" in w for w in warnings)


class TestToolValidation:
    """Tests for tool reference validation."""

    def test_valid_tool_reference(self) -> None:
        """Test that valid tool reference passes validation."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            tools=["web_search", "calculator"],
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    tools=["web_search"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        # Should not raise
        validate_workflow_config(config)

    def test_invalid_tool_reference(self) -> None:
        """Test that invalid tool reference raises error."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            tools=["web_search"],
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    tools=["unknown_tool"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "unknown_tool" in str(exc_info.value)

    def test_no_tools_defined_but_agent_uses_some(self) -> None:
        """Test agent using tools when no tools defined at workflow level."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            tools=[],  # No tools defined
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    tools=["web_search"],  # But agent wants to use one
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "web_search" in str(exc_info.value)


class TestOutputReferenceValidation:
    """Tests for workflow output reference validation."""

    def test_valid_output_reference(self) -> None:
        """Test that valid output references pass validation."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Hello", routes=[RouteDef(to="$end")])
            ],
            output={"result": "{{ agent1.output }}"},
        )
        # Should not raise
        validate_workflow_config(config)

    def test_output_reference_to_unknown_agent(self) -> None:
        """Test that output reference to unknown agent raises error."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Hello", routes=[RouteDef(to="$end")])
            ],
            output={"result": "{{ unknown_agent.output }}"},
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "unknown_agent" in str(exc_info.value)
