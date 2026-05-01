"""Tests for workflow type schema validation.

Tests cover:
- Valid workflow agent definitions
- Workflow field validation (workflow path required, forbidden fields)
- Workflow agents in parallel groups and for_each groups
- Backward compatibility with other agent types
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
    RetryPolicy,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError


class TestWorkflowAgentDef:
    """Tests for workflow type AgentDef validation."""

    def test_valid_workflow_agent(self) -> None:
        """Test creating a valid workflow agent."""
        agent = AgentDef(name="sub_wf", type="workflow", workflow="./sub.yaml")
        assert agent.type == "workflow"
        assert agent.workflow == "./sub.yaml"

    def test_valid_workflow_agent_with_routes(self) -> None:
        """Test workflow agent with routes validates correctly."""
        agent = AgentDef(
            name="sub_wf",
            type="workflow",
            workflow="./sub.yaml",
            routes=[
                RouteDef(to="next_agent", when="{{ output.result == 'done' }}"),
                RouteDef(to="$end"),
            ],
        )
        assert len(agent.routes) == 2

    def test_valid_workflow_agent_with_input(self) -> None:
        """Test workflow agent with input declarations."""
        agent = AgentDef(
            name="sub_wf",
            type="workflow",
            workflow="./sub.yaml",
            input=["workflow.input.topic"],
        )
        assert agent.input == ["workflow.input.topic"]

    def test_valid_workflow_agent_with_output(self) -> None:
        """Test workflow agent with output schema."""
        agent = AgentDef(
            name="sub_wf",
            type="workflow",
            workflow="./sub.yaml",
            output={"findings": OutputField(type="string")},
        )
        assert "findings" in agent.output

    def test_workflow_without_path_raises(self) -> None:
        """Test that workflow agent without workflow path raises ValidationError."""
        with pytest.raises(ValidationError, match="workflow agents require 'workflow' path"):
            AgentDef(name="bad", type="workflow")

    def test_workflow_with_empty_path_raises(self) -> None:
        """Test that workflow agent with empty path raises ValidationError."""
        with pytest.raises(ValidationError, match="workflow agents require 'workflow' path"):
            AgentDef(name="bad", type="workflow", workflow="")

    def test_workflow_with_prompt_raises(self) -> None:
        """Test that workflow agent with prompt raises ValidationError."""
        with pytest.raises(ValidationError, match="workflow agents cannot have 'prompt'"):
            AgentDef(name="bad", type="workflow", workflow="./s.yaml", prompt="hello")

    def test_workflow_with_provider_raises(self) -> None:
        """Test that workflow agent with provider raises ValidationError."""
        with pytest.raises(ValidationError, match="workflow agents cannot have 'provider'"):
            AgentDef(name="bad", type="workflow", workflow="./s.yaml", provider="copilot")

    def test_workflow_with_model_raises(self) -> None:
        """Test that workflow agent with model raises ValidationError."""
        with pytest.raises(ValidationError, match="workflow agents cannot have 'model'"):
            AgentDef(name="bad", type="workflow", workflow="./s.yaml", model="gpt-4")

    def test_workflow_with_tools_raises(self) -> None:
        """Test that workflow agent with tools raises ValidationError."""
        with pytest.raises(ValidationError, match="workflow agents cannot have 'tools'"):
            AgentDef(name="bad", type="workflow", workflow="./s.yaml", tools=["web_search"])

    def test_workflow_with_system_prompt_raises(self) -> None:
        """Test that workflow agent with system_prompt raises ValidationError."""
        with pytest.raises(ValidationError, match="workflow agents cannot have 'system_prompt'"):
            AgentDef(name="bad", type="workflow", workflow="./s.yaml", system_prompt="You are...")

    def test_workflow_with_options_raises(self) -> None:
        """Test that workflow agent with options raises ValidationError."""
        with pytest.raises(ValidationError, match="workflow agents cannot have 'options'"):
            AgentDef(
                name="bad",
                type="workflow",
                workflow="./s.yaml",
                options=[GateOption(label="OK", value="ok", route="$end")],
            )

    def test_workflow_with_command_raises(self) -> None:
        """Test that workflow agent with command raises ValidationError."""
        with pytest.raises(ValidationError, match="workflow agents cannot have 'command'"):
            AgentDef(name="bad", type="workflow", workflow="./s.yaml", command="echo")

    def test_workflow_with_max_session_seconds_raises(self) -> None:
        """Test that workflow agent with max_session_seconds raises ValidationError."""
        with pytest.raises(
            ValidationError, match="workflow agents cannot have 'max_session_seconds'"
        ):
            AgentDef(name="bad", type="workflow", workflow="./s.yaml", max_session_seconds=60.0)

    def test_workflow_with_max_agent_iterations_raises(self) -> None:
        """Test that workflow agent with max_agent_iterations raises ValidationError."""
        with pytest.raises(
            ValidationError, match="workflow agents cannot have 'max_agent_iterations'"
        ):
            AgentDef(name="bad", type="workflow", workflow="./s.yaml", max_agent_iterations=100)

    def test_workflow_with_retry_raises(self) -> None:
        """Test that workflow agent with retry raises ValidationError."""
        with pytest.raises(ValidationError, match="workflow agents cannot have 'retry'"):
            AgentDef(
                name="bad",
                type="workflow",
                workflow="./s.yaml",
                retry=RetryPolicy(max_attempts=3),
            )


class TestWorkflowBackwardCompatibility:
    """Test that existing agent types still work after adding workflow type."""

    def test_regular_agent_still_works(self) -> None:
        """Test that a regular agent definition is unaffected."""
        agent = AgentDef(name="test", prompt="hello")
        assert agent.type is None
        assert agent.workflow is None

    def test_script_agent_still_works(self) -> None:
        """Test that script agent is unaffected."""
        agent = AgentDef(name="test", type="script", command="echo")
        assert agent.type == "script"

    def test_human_gate_still_works(self) -> None:
        """Test that human_gate type is unaffected."""
        agent = AgentDef(
            name="gate",
            type="human_gate",
            prompt="Choose:",
            options=[GateOption(label="Yes", value="yes", route="$end")],
        )
        assert agent.type == "human_gate"


class TestWorkflowInParallelGroup:
    """Tests for workflow agents in parallel groups."""

    def test_workflow_in_parallel_group_raises(self) -> None:
        """Test that workflow agent in parallel group raises ConfigurationError."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="parallel_group",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(name="agent_a", prompt="do something"),
                AgentDef(name="sub_wf", type="workflow", workflow="./sub.yaml"),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_group",
                    agents=["agent_a", "sub_wf"],
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="workflow step"):
            validate_workflow_config(config)


class TestWorkflowInForEach:
    """Tests for workflow agents in for_each groups."""

    def test_workflow_in_for_each_validates(self) -> None:
        """Test that workflow step in for_each inline agent validates successfully."""
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
                        type="workflow",
                        workflow="./sub.yaml",
                    ),
                ),
            ],
        )
        # Should not raise — workflow agents are now allowed in for_each
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)


class TestWorkflowWorkflowConfig:
    """Tests for WorkflowConfig with workflow agents."""

    def test_workflow_at_entry_point_validates(self) -> None:
        """Test that a workflow agent can be the workflow entry_point."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="sub_wf",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="./sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        # Should not raise
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_workflow_with_routes_to_agents(self) -> None:
        """Test that workflow agent can route to other agents."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="sub_wf",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="./sub.yaml",
                    routes=[
                        RouteDef(to="processor"),
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


class TestInputMapping:
    """Tests for input_mapping on workflow agents."""

    def test_valid_input_mapping(self) -> None:
        """Test that input_mapping is accepted on workflow agents."""
        agent = AgentDef(
            name="sub_wf",
            type="workflow",
            workflow="./sub.yaml",
            input_mapping={
                "work_item_id": "{{ intake.output.epic_id }}",
                "title": "{{ intake.output.epic_title }}",
            },
        )
        assert agent.input_mapping is not None
        assert len(agent.input_mapping) == 2

    def test_workflow_without_input_mapping(self) -> None:
        """Test that workflow agents work without input_mapping (backward compat)."""
        agent = AgentDef(name="sub_wf", type="workflow", workflow="./sub.yaml")
        assert agent.input_mapping is None

    def test_input_mapping_on_regular_agent_raises(self) -> None:
        """Test that input_mapping on a regular agent raises ValidationError."""
        with pytest.raises(ValidationError, match="input_mapping"):
            AgentDef(
                name="regular",
                prompt="do something",
                input_mapping={"key": "{{ value }}"},
            )

    def test_input_mapping_on_human_gate_raises(self) -> None:
        """Test that input_mapping on a human_gate raises ValidationError."""
        with pytest.raises(ValidationError, match="input_mapping"):
            AgentDef(
                name="gate",
                type="human_gate",
                prompt="Choose",
                options=[
                    GateOption(label="Yes", value="yes", route="next"),
                ],
                input_mapping={"key": "{{ value }}"},
            )

    def test_input_mapping_on_script_raises(self) -> None:
        """Test that input_mapping on a script agent raises ValidationError."""
        with pytest.raises(ValidationError, match="input_mapping"):
            AgentDef(
                name="script",
                type="script",
                command="echo hi",
                input_mapping={"key": "{{ value }}"},
            )
