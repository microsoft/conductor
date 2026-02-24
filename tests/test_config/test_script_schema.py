"""Tests for script type schema validation.

Tests cover:
- Valid script agent definitions
- Script field validation (command required, forbidden fields)
- Script agents in parallel groups and for_each groups
- Backward compatibility with agent and human_gate types
- Timeout field validation
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.config.schema import (
    AgentDef,
    ForEachDef,
    GateOption,
    LimitsConfig,
    OutputField,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError


class TestScriptAgentDef:
    """Tests for script type AgentDef validation."""

    def test_valid_script_agent(self) -> None:
        """Test creating a valid script agent."""
        agent = AgentDef(name="run_tests", type="script", command="pytest")
        assert agent.type == "script"
        assert agent.command == "pytest"
        assert agent.args == []
        assert agent.env == {}
        assert agent.working_dir is None
        assert agent.timeout is None

    def test_valid_script_agent_with_all_fields(self) -> None:
        """Test creating a script agent with all optional fields."""
        agent = AgentDef(
            name="build",
            type="script",
            command="make",
            args=["build", "--verbose"],
            env={"CI": "true"},
            working_dir="/tmp/build",
            timeout=60,
        )
        assert agent.command == "make"
        assert agent.args == ["build", "--verbose"]
        assert agent.env == {"CI": "true"}
        assert agent.working_dir == "/tmp/build"
        assert agent.timeout == 60

    def test_script_agent_with_routes(self) -> None:
        """Test script agent with routes validates correctly."""
        agent = AgentDef(
            name="check",
            type="script",
            command="echo",
            args=["hello"],
            routes=[
                RouteDef(to="success_handler", when="exit_code == 0"),
                RouteDef(to="failure_handler"),
            ],
        )
        assert len(agent.routes) == 2

    def test_script_without_command_raises(self) -> None:
        """Test that script agent without command raises ValidationError."""
        with pytest.raises(ValidationError, match="script agents require 'command'"):
            AgentDef(name="bad", type="script")

    def test_script_with_empty_command_raises(self) -> None:
        """Test that script agent with empty command raises ValidationError."""
        with pytest.raises(ValidationError, match="script agents require 'command'"):
            AgentDef(name="bad", type="script", command="")

    def test_script_with_prompt_raises(self) -> None:
        """Test that script agent with prompt raises ValidationError."""
        with pytest.raises(ValidationError, match="script agents cannot have 'prompt'"):
            AgentDef(name="bad", type="script", command="echo", prompt="hello")

    def test_script_with_provider_raises(self) -> None:
        """Test that script agent with provider raises ValidationError."""
        with pytest.raises(ValidationError, match="script agents cannot have 'provider'"):
            AgentDef(name="bad", type="script", command="echo", provider="copilot")

    def test_script_with_model_raises(self) -> None:
        """Test that script agent with model raises ValidationError."""
        with pytest.raises(ValidationError, match="script agents cannot have 'model'"):
            AgentDef(name="bad", type="script", command="echo", model="gpt-4")

    def test_script_with_tools_raises(self) -> None:
        """Test that script agent with tools raises ValidationError."""
        with pytest.raises(ValidationError, match="script agents cannot have 'tools'"):
            AgentDef(name="bad", type="script", command="echo", tools=["web_search"])

    def test_script_with_output_raises(self) -> None:
        """Test that script agent with output schema raises ValidationError."""
        with pytest.raises(ValidationError, match="script agents cannot have 'output'"):
            AgentDef(
                name="bad",
                type="script",
                command="echo",
                output={"result": OutputField(type="string")},
            )

    def test_script_with_system_prompt_raises(self) -> None:
        """Test that script agent with system_prompt raises ValidationError."""
        with pytest.raises(ValidationError, match="script agents cannot have 'system_prompt'"):
            AgentDef(name="bad", type="script", command="echo", system_prompt="You are...")

    def test_script_with_options_raises(self) -> None:
        """Test that script agent with options raises ValidationError."""
        with pytest.raises(ValidationError, match="script agents cannot have 'options'"):
            AgentDef(
                name="bad",
                type="script",
                command="echo",
                options=[GateOption(label="OK", value="ok", route="$end")],
            )

    def test_timeout_rejects_zero(self) -> None:
        """Test that timeout=0 raises ValidationError."""
        with pytest.raises(ValidationError, match="timeout must be a positive integer"):
            AgentDef(name="bad", type="script", command="echo", timeout=0)

    def test_timeout_rejects_negative(self) -> None:
        """Test that negative timeout raises ValidationError."""
        with pytest.raises(ValidationError, match="timeout must be a positive integer"):
            AgentDef(name="bad", type="script", command="echo", timeout=-5)


class TestScriptBackwardCompatibility:
    """Test that existing agent and human_gate types still work."""

    def test_regular_agent_still_works(self) -> None:
        """Test that a regular agent definition is unaffected."""
        agent = AgentDef(name="test", prompt="hello")
        assert agent.type is None
        assert agent.command is None

    def test_explicit_agent_type_still_works(self) -> None:
        """Test that explicit type='agent' still works."""
        agent = AgentDef(name="test", type="agent", prompt="hello")
        assert agent.type == "agent"

    def test_human_gate_still_works(self) -> None:
        """Test that human_gate type is unaffected."""
        agent = AgentDef(
            name="gate",
            type="human_gate",
            prompt="Choose:",
            options=[GateOption(label="Yes", value="yes", route="$end")],
        )
        assert agent.type == "human_gate"


class TestScriptInParallelGroup:
    """Tests for script agents in parallel groups."""

    def test_script_in_parallel_group_raises(self) -> None:
        """Test that script agent in parallel group raises ConfigurationError."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="parallel_group",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(name="agent_a", prompt="do something"),
                AgentDef(name="script_b", type="script", command="echo"),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_group",
                    agents=["agent_a", "script_b"],
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="script step"):
            validate_workflow_config(config)


class TestScriptInForEach:
    """Tests for script agents in for_each groups."""

    def test_script_in_for_each_raises(self) -> None:
        """Test that script step in for_each inline agent raises ConfigurationError."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="loop",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(name="setup", prompt="init"),
            ],
            for_each=[
                ForEachDef(
                    name="loop",
                    type="for_each",
                    source="setup.output.items",
                    **{"as": "item"},
                    agent=AgentDef(
                        name="runner",
                        type="script",
                        command="echo",
                    ),
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="Script steps cannot be used in for_each"):
            validate_workflow_config(config)


class TestScriptWorkflowConfig:
    """Tests for WorkflowConfig with script agents."""

    def test_script_at_entry_point_validates(self) -> None:
        """Test that a script agent can be the workflow entry_point."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="setup",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="setup",
                    type="script",
                    command="echo",
                    args=["hello"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        # Should not raise
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_script_with_routes_to_agents(self) -> None:
        """Test that script agent can route to other agents."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="checker",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="checker",
                    type="script",
                    command="test",
                    args=["-f", "output.txt"],
                    routes=[
                        RouteDef(to="processor", when="exit_code == 0"),
                        RouteDef(to="$end"),
                    ],
                ),
                AgentDef(
                    name="processor",
                    prompt="Process the output",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        # Should not raise
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)
