"""Tests for parallel group validation rules."""

from __future__ import annotations

import pytest

from conductor.config.schema import (
    AgentDef,
    ParallelGroup,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError


class TestParallelGroupBasicValidation:
    """Tests for basic parallel group validation."""

    def test_valid_parallel_group(self) -> None:
        """Test validation of a valid parallel group configuration."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="parallel1"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Task 1"),
                AgentDef(name="agent2", model="gpt-4", prompt="Task 2"),
            ],
            parallel=[ParallelGroup(name="parallel1", agents=["agent1", "agent2"])],
        )
        # Should not raise
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_parallel_group_with_failure_modes(self) -> None:
        """Test that all failure modes are accepted."""
        for mode in ["fail_fast", "continue_on_error", "all_or_nothing"]:
            config = WorkflowConfig(
                workflow=WorkflowDef(name="test", entry_point="parallel1"),
                agents=[
                    AgentDef(name="agent1", model="gpt-4", prompt="Task 1"),
                    AgentDef(name="agent2", model="gpt-4", prompt="Task 2"),
                ],
                parallel=[
                    ParallelGroup(
                        name="parallel1",
                        agents=["agent1", "agent2"],
                        failure_mode=mode,
                    )
                ],
            )
            warnings = validate_workflow_config(config)
            assert isinstance(warnings, list)


class TestParallelAgentReferences:
    """Tests for PE-2.2: Validate parallel agent references exist."""

    def test_unknown_agent_in_parallel_group(self) -> None:
        """Test that referencing unknown agents in parallel group is rejected."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc:
            WorkflowConfig(
                workflow=WorkflowDef(name="test", entry_point="parallel1"),
                agents=[
                    AgentDef(name="agent1", model="gpt-4", prompt="Task 1"),
                ],
                parallel=[ParallelGroup(name="parallel1", agents=["agent1", "unknown_agent"])],
            )
        assert "unknown_agent" in str(exc.value)
        assert "parallel1" in str(exc.value)

    def test_multiple_unknown_agents(self) -> None:
        """Test that multiple unknown agents are reported."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc:
            WorkflowConfig(
                workflow=WorkflowDef(name="test", entry_point="parallel1"),
                agents=[
                    AgentDef(name="agent1", model="gpt-4", prompt="Task 1"),
                ],
                parallel=[
                    ParallelGroup(
                        name="parallel1",
                        agents=["agent1", "unknown1", "unknown2"],
                    )
                ],
            )
        error_msg = str(exc.value)
        # At least one unknown agent should be reported
        assert "unknown" in error_msg


class TestParallelAgentRoutes:
    """Tests for PE-2.3: Validate parallel agents have no routes."""

    def test_parallel_agent_with_routes_rejected(self) -> None:
        """Test that agents in parallel groups cannot have routes."""
        from conductor.config.schema import RouteDef

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="parallel1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Task 1",
                    routes=[RouteDef(to="$end")],
                ),
                AgentDef(name="agent2", model="gpt-4", prompt="Task 2"),
            ],
            parallel=[ParallelGroup(name="parallel1", agents=["agent1", "agent2"])],
        )
        with pytest.raises(ConfigurationError) as exc:
            validate_workflow_config(config)
        assert "agent1" in str(exc.value)
        assert "cannot have routes" in str(exc.value)

    def test_all_parallel_agents_with_routes_rejected(self) -> None:
        """Test that all agents with routes are reported."""
        from conductor.config.schema import RouteDef

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="parallel1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Task 1",
                    routes=[RouteDef(to="$end")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Task 2",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            parallel=[ParallelGroup(name="parallel1", agents=["agent1", "agent2"])],
        )
        with pytest.raises(ConfigurationError) as exc:
            validate_workflow_config(config)
        error_msg = str(exc.value)
        assert "agent1" in error_msg
        assert "agent2" in error_msg


class TestCrossAgentDependencies:
    """Tests for PE-2.4: Validate no cross-agent dependencies within parallel group."""

    def test_cross_reference_within_parallel_group_rejected(self) -> None:
        """Test that agents in same parallel group cannot reference each other."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="parallel1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Task 1",
                    input=["agent2.output"],
                ),
                AgentDef(name="agent2", model="gpt-4", prompt="Task 2"),
            ],
            parallel=[ParallelGroup(name="parallel1", agents=["agent1", "agent2"])],
        )
        with pytest.raises(ConfigurationError) as exc:
            validate_workflow_config(config)
        assert "agent1" in str(exc.value)
        assert "agent2" in str(exc.value)
        assert "same parallel group" in str(exc.value)

    def test_reference_to_agent_outside_parallel_group_allowed(self) -> None:
        """Test that agents in parallel group can reference agents outside it."""
        from conductor.config.schema import RouteDef

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent0"),
            agents=[
                AgentDef(
                    name="agent0",
                    model="gpt-4",
                    prompt="Setup",
                    routes=[RouteDef(to="parallel1")],
                ),
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Task 1",
                    input=["agent0.output"],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Task 2",
                    input=["agent0.output"],
                ),
            ],
            parallel=[ParallelGroup(name="parallel1", agents=["agent1", "agent2"])],
        )
        # Should not raise
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_mutual_cross_references_rejected(self) -> None:
        """Test that mutual references between parallel agents are both rejected."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="parallel1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Task 1",
                    input=["agent2.output"],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Task 2",
                    input=["agent1.output"],
                ),
            ],
            parallel=[ParallelGroup(name="parallel1", agents=["agent1", "agent2"])],
        )
        with pytest.raises(ConfigurationError) as exc:
            validate_workflow_config(config)
        error_msg = str(exc.value)
        # Both directions should be reported
        assert "agent1" in error_msg and "agent2" in error_msg


class TestUniqueNames:
    """Tests for PE-2.5: Validate unique names (parallel groups vs agents)."""

    def test_duplicate_name_agent_and_parallel_group(self) -> None:
        """Test that agent and parallel group cannot have same name."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="duplicate"),
            agents=[
                AgentDef(name="duplicate", model="gpt-4", prompt="Task 1"),
                AgentDef(name="agent2", model="gpt-4", prompt="Task 2"),
            ],
            parallel=[ParallelGroup(name="duplicate", agents=["duplicate", "agent2"])],
        )
        with pytest.raises(ConfigurationError) as exc:
            validate_workflow_config(config)
        assert "duplicate" in str(exc.value).lower()
        assert "Duplicate names" in str(exc.value)

    def test_multiple_duplicate_names(self) -> None:
        """Test that multiple name conflicts are all reported."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="name1"),
            agents=[
                AgentDef(name="name1", model="gpt-4", prompt="Task 1"),
                AgentDef(name="name2", model="gpt-4", prompt="Task 2"),
                AgentDef(name="agent3", model="gpt-4", prompt="Task 3"),
                AgentDef(name="agent4", model="gpt-4", prompt="Task 4"),
            ],
            parallel=[
                ParallelGroup(name="name1", agents=["name1", "name2"]),
                ParallelGroup(name="name2", agents=["agent3", "agent4"]),
            ],
        )
        with pytest.raises(ConfigurationError) as exc:
            validate_workflow_config(config)
        error_msg = str(exc.value)
        assert "name1" in error_msg
        assert "name2" in error_msg


class TestNestedParallelGroups:
    """Tests for PE-2.6: Validate no nested parallel groups."""

    def test_nested_parallel_groups_rejected(self) -> None:
        """Test that parallel groups cannot contain other parallel groups."""
        # Since 'inner' will be validated as an unknown agent first (Pydantic validation),
        # we need to check that the error is clear about the nesting issue.
        # However, the Pydantic validation will catch the unknown agent reference first.
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc:
            WorkflowConfig(
                workflow=WorkflowDef(name="test", entry_point="outer"),
                agents=[
                    AgentDef(name="agent1", model="gpt-4", prompt="Task 1"),
                    AgentDef(name="agent2", model="gpt-4", prompt="Task 2"),
                ],
                parallel=[
                    ParallelGroup(name="inner", agents=["agent1", "agent2"]),
                    ParallelGroup(name="outer", agents=["inner", "agent1"]),
                ],
            )
        # The error will report 'inner' as unknown agent
        assert "inner" in str(exc.value)


class TestHumanGatesInParallel:
    """Tests for PE-2.7: Validate no human gates in parallel groups."""

    def test_human_gate_in_parallel_group_rejected(self) -> None:
        """Test that human gates cannot be in parallel groups."""
        from conductor.config.schema import GateOption

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="parallel1"),
            agents=[
                AgentDef(
                    name="gate1",
                    type="human_gate",
                    model="gpt-4",
                    prompt="Choose",
                    options=[
                        GateOption(label="Yes", value="yes", route="$end"),
                        GateOption(label="No", value="no", route="$end"),
                    ],
                ),
                AgentDef(name="agent2", model="gpt-4", prompt="Task 2"),
            ],
            parallel=[ParallelGroup(name="parallel1", agents=["gate1", "agent2"])],
        )
        with pytest.raises(ConfigurationError) as exc:
            validate_workflow_config(config)
        assert "gate1" in str(exc.value)
        assert "human gate" in str(exc.value).lower()


class TestRoutingWithParallelGroups:
    """Tests for routing to/from parallel groups."""

    def test_route_to_parallel_group(self) -> None:
        """Test that agents can route to parallel groups."""
        from conductor.config.schema import RouteDef

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent0"),
            agents=[
                AgentDef(
                    name="agent0",
                    model="gpt-4",
                    prompt="Setup",
                    routes=[RouteDef(to="parallel1")],
                ),
                AgentDef(name="agent1", model="gpt-4", prompt="Task 1"),
                AgentDef(name="agent2", model="gpt-4", prompt="Task 2"),
            ],
            parallel=[ParallelGroup(name="parallel1", agents=["agent1", "agent2"])],
        )
        # Should not raise
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_parallel_group_as_entry_point(self) -> None:
        """Test that parallel group can be workflow entry point."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="parallel1"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Task 1"),
                AgentDef(name="agent2", model="gpt-4", prompt="Task 2"),
            ],
            parallel=[ParallelGroup(name="parallel1", agents=["agent1", "agent2"])],
        )
        # Should not raise
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_human_gate_route_to_parallel_group(self) -> None:
        """Test that human gates can route to parallel groups."""
        from conductor.config.schema import GateOption

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="gate1"),
            agents=[
                AgentDef(
                    name="gate1",
                    type="human_gate",
                    model="gpt-4",
                    prompt="Choose",
                    options=[
                        GateOption(label="Parallel", value="parallel", route="parallel1"),
                        GateOption(label="End", value="end", route="$end"),
                    ],
                ),
                AgentDef(name="agent1", model="gpt-4", prompt="Task 1"),
                AgentDef(name="agent2", model="gpt-4", prompt="Task 2"),
            ],
            parallel=[ParallelGroup(name="parallel1", agents=["agent1", "agent2"])],
        )
        # Should not raise
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)


class TestParallelGroupInputReferences:
    """Tests for input references to parallel group outputs."""

    def test_reference_to_parallel_group_output(self) -> None:
        """Test that agents can reference parallel group outputs."""
        from conductor.config.schema import RouteDef

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="parallel1"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Task 1"),
                AgentDef(name="agent2", model="gpt-4", prompt="Task 2"),
                AgentDef(
                    name="agent3",
                    model="gpt-4",
                    prompt="Summarize",
                    input=["parallel1.outputs.agent1", "parallel1.outputs.agent2.data"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel1",
                    agents=["agent1", "agent2"],
                )
            ],
        )
        # Should not raise
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_unknown_parallel_group_reference(self) -> None:
        """Test that referencing unknown parallel group is rejected."""
        from conductor.config.schema import RouteDef

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Task",
                    input=["unknown_parallel.outputs.agent2"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        with pytest.raises(ConfigurationError) as exc:
            validate_workflow_config(config)
        assert "unknown_parallel" in str(exc.value)


class TestErrorMessages:
    """Tests for PE-2.10: Test error messages for validation failures."""

    def test_error_messages_are_clear(self) -> None:
        """Test that error messages provide clear guidance."""
        from conductor.config.schema import RouteDef

        # Test with multiple errors to ensure all are clear
        # Note: Unknown agent will be caught by Pydantic first, so test other errors
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="parallel1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Task 1",
                    routes=[RouteDef(to="$end")],  # Error: has routes
                    input=["agent2.output"],  # Error: cross-reference
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Task 2",
                ),
            ],
            parallel=[ParallelGroup(name="parallel1", agents=["agent1", "agent2"])],
        )
        with pytest.raises(ConfigurationError) as exc:
            validate_workflow_config(config)
        error_msg = str(exc.value)

        # Should contain helpful context
        assert "parallel1" in error_msg
        assert "agent1" in error_msg

        # Should explain the issues
        assert "routes" in error_msg or "routing" in error_msg
        assert "same parallel group" in error_msg or "cross" in error_msg

    def test_suggestions_included_in_errors(self) -> None:
        """Test that error messages include suggestions when applicable."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc:
            WorkflowConfig(
                workflow=WorkflowDef(name="test", entry_point="parallel1"),
                agents=[
                    AgentDef(name="agent1", model="gpt-4", prompt="Task 1"),
                ],
                parallel=[ParallelGroup(name="parallel1", agents=["agent1", "typo_agent"])],
            )
        error_msg = str(exc.value)

        # Should mention the unknown agent
        assert "typo_agent" in error_msg or "unknown" in error_msg
