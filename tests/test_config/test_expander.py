"""Tests for stage expansion feature.

Tests cover StageDef schema validation, AgentDef stages field validation,
the expand_stages() function, and edge cases in stage expansion.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.config.expander import expand_stages
from conductor.config.schema import (
    AgentDef,
    OutputField,
    RouteDef,
    StageDef,
    WorkflowConfig,
    WorkflowDef,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    agents: list[AgentDef],
    entry_point: str = "vp",
) -> WorkflowConfig:
    """Build a minimal WorkflowConfig for testing."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="test",
            entry_point=entry_point,
        ),
        agents=agents,
    )


# ---------------------------------------------------------------------------
# 1. StageDef schema validation
# ---------------------------------------------------------------------------


class TestStageDef:
    """Tests for StageDef Pydantic model validation."""

    def test_stage_def_all_none(self) -> None:
        """StageDef with no arguments should have all fields as None."""
        stage = StageDef()
        assert stage.prompt is None
        assert stage.input is None
        assert stage.output is None
        assert stage.routes is None
        assert stage.description is None

    def test_stage_def_with_prompt(self) -> None:
        """StageDef with only a prompt should set prompt and leave others None."""
        stage = StageDef(prompt="Review")
        assert stage.prompt == "Review"
        assert stage.input is None
        assert stage.output is None
        assert stage.routes is None
        assert stage.description is None

    def test_stage_def_with_all_fields(self) -> None:
        """StageDef with all five fields should retain every value."""
        stage = StageDef(
            prompt="Do work",
            input=["a.output"],
            output={"result": OutputField(type="string")},
            routes=[RouteDef(to="$end")],
            description="A test stage",
        )
        assert stage.prompt == "Do work"
        assert stage.input == ["a.output"]
        assert "result" in stage.output
        assert len(stage.routes) == 1
        assert stage.description == "A test stage"

    def test_stage_def_with_routes(self) -> None:
        """StageDef with routes should accept a list of RouteDef."""
        stage = StageDef(routes=[RouteDef(to="$end")])
        assert stage.routes is not None
        assert stage.routes[0].to == "$end"

    def test_stage_def_with_output(self) -> None:
        """StageDef with output should accept a dict of OutputField."""
        stage = StageDef(output={"field": OutputField(type="string")})
        assert stage.output is not None
        assert stage.output["field"].type == "string"


# ---------------------------------------------------------------------------
# 2. AgentDef stages field validation
# ---------------------------------------------------------------------------


class TestAgentDefStages:
    """Tests for AgentDef stages field and its validators."""

    def test_stages_default_none(self) -> None:
        """AgentDef without stages should default to None."""
        agent = AgentDef(name="a", model="gpt-4", prompt="Hello")
        assert agent.stages is None

    def test_stages_with_valid_stages(self) -> None:
        """AgentDef with a valid stages dict should be accepted."""
        agent = AgentDef(
            name="a",
            model="gpt-4",
            prompt="Hello",
            stages={"review": StageDef(prompt="Review")},
        )
        assert agent.stages is not None
        assert "review" in agent.stages

    def test_stages_reserved_default_name_rejected(self) -> None:
        """Stage name 'default' is reserved and must be rejected."""
        with pytest.raises(ValidationError, match="default"):
            AgentDef(
                name="a",
                model="gpt-4",
                prompt="Hello",
                stages={"default": StageDef()},
            )

    def test_stages_invalid_identifier_rejected(self) -> None:
        """Stage names that are not valid Python identifiers must be rejected."""
        with pytest.raises(ValidationError, match="identifier"):
            AgentDef(
                name="a",
                model="gpt-4",
                prompt="Hello",
                stages={"not-valid": StageDef()},
            )

    def test_stages_on_script_rejected(self) -> None:
        """Script agents cannot have stages."""
        with pytest.raises(ValidationError):
            AgentDef(
                name="a",
                type="script",
                command="echo hi",
                stages={"review": StageDef()},
            )

    def test_stages_on_human_gate_rejected(self) -> None:
        """Human gate agents cannot have stages."""
        with pytest.raises(ValidationError):
            AgentDef(
                name="a",
                type="human_gate",
                prompt="Choose",
                options=["yes", "no"],
                stages={"review": StageDef()},
            )

    def test_empty_stages_dict(self) -> None:
        """An empty stages dict should be accepted (valid but no-op)."""
        agent = AgentDef(
            name="a",
            model="gpt-4",
            prompt="Hello",
            stages={},
        )
        assert agent.stages == {}


# ---------------------------------------------------------------------------
# 3. Stage expansion logic
# ---------------------------------------------------------------------------


class TestExpandStages:
    """Tests for the expand_stages() function."""

    def test_no_stages_returns_unchanged(self) -> None:
        """Config with no staged agents should be returned unchanged."""
        config = _make_config(
            agents=[
                AgentDef(name="vp", model="gpt-4", prompt="Go"),
            ],
        )
        original_count = len(config.agents)
        result = expand_stages(config)
        assert len(result.agents) == original_count

    def test_expands_single_stage(self) -> None:
        """Agent with one stage should produce a default and one stage synthetic."""
        config = _make_config(
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    system_prompt="You are a VP",
                    prompt="Set direction",
                    stages={"review": StageDef(prompt="Review output")},
                ),
            ],
        )
        result = expand_stages(config)

        names = [a.name for a in result.agents]
        assert "vp" in names
        assert "vp:default" in names
        assert "vp:review" in names

        default = next(a for a in result.agents if a.name == "vp:default")
        assert default.prompt == "Set direction"
        assert default.stages is None

        review = next(a for a in result.agents if a.name == "vp:review")
        assert review.prompt == "Review output"
        assert review.stages is None
        assert review.model == "gpt-4"
        assert review.system_prompt == "You are a VP"

    def test_expands_multiple_stages(self) -> None:
        """Agent with two stages should produce three synthetic agents."""
        config = _make_config(
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Go",
                    stages={
                        "review": StageDef(prompt="Review"),
                        "summary": StageDef(prompt="Summarize"),
                    },
                ),
            ],
        )
        result = expand_stages(config)

        synthetic_names = [a.name for a in result.agents if ":" in a.name]
        assert len(synthetic_names) == 3
        assert "vp:default" in synthetic_names
        assert "vp:review" in synthetic_names
        assert "vp:summary" in synthetic_names

    def test_stage_overrides_prompt(self) -> None:
        """Stage prompt should override the base agent prompt."""
        config = _make_config(
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Base prompt",
                    stages={"review": StageDef(prompt="Stage prompt")},
                ),
            ],
        )
        result = expand_stages(config)

        review = next(a for a in result.agents if a.name == "vp:review")
        assert review.prompt == "Stage prompt"

    def test_stage_overrides_input(self) -> None:
        """Stage input should override the base agent input list."""
        config = _make_config(
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Go",
                    input=["workflow.input.project"],
                    stages={"review": StageDef(input=["ic.output"])},
                ),
            ],
        )
        result = expand_stages(config)

        review = next(a for a in result.agents if a.name == "vp:review")
        assert review.input == ["ic.output"]

    def test_stage_overrides_output(self) -> None:
        """Stage output should override the base agent output schema."""
        config = _make_config(
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Go",
                    output={"plan": OutputField(type="string")},
                    stages={
                        "review": StageDef(
                            output={"verdict": OutputField(type="boolean")},
                        ),
                    },
                ),
            ],
        )
        result = expand_stages(config)

        review = next(a for a in result.agents if a.name == "vp:review")
        assert "verdict" in review.output
        assert "plan" not in review.output

    def test_stage_overrides_routes(self) -> None:
        """Stage routes should replace the base agent routes entirely."""
        config = _make_config(
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Go",
                    routes=[RouteDef(to="$end")],
                    stages={
                        "review": StageDef(
                            routes=[RouteDef(to="$end", when="verdict == true")],
                        ),
                    },
                ),
            ],
        )
        result = expand_stages(config)

        review = next(a for a in result.agents if a.name == "vp:review")
        assert len(review.routes) == 1
        assert review.routes[0].when == "verdict == true"

    def test_stage_overrides_description(self) -> None:
        """Stage description should override the base agent description."""
        config = _make_config(
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Go",
                    description="Base description",
                    stages={
                        "review": StageDef(description="Review description"),
                    },
                ),
            ],
        )
        result = expand_stages(config)

        review = next(a for a in result.agents if a.name == "vp:review")
        assert review.description == "Review description"

    def test_stage_inherits_unoverridden_fields(self) -> None:
        """Stage with only prompt should inherit model, system_prompt, tools from base."""
        config = _make_config(
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    system_prompt="You are a VP",
                    prompt="Go",
                    tools=["read_file"],
                    stages={"review": StageDef(prompt="Review")},
                ),
            ],
        )
        result = expand_stages(config)

        review = next(a for a in result.agents if a.name == "vp:review")
        assert review.model == "gpt-4"
        assert review.system_prompt == "You are a VP"
        assert review.tools == ["read_file"]

    def test_entry_point_rewritten(self) -> None:
        """Entry point referencing a staged agent should be rewritten to default."""
        config = _make_config(
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Go",
                    stages={"review": StageDef(prompt="Review")},
                ),
            ],
            entry_point="vp",
        )
        result = expand_stages(config)
        assert result.workflow.entry_point == "vp:default"

    def test_entry_point_not_rewritten_for_unstaged(self) -> None:
        """Entry point referencing an unstaged agent should remain unchanged."""
        config = _make_config(
            agents=[
                AgentDef(name="ic", model="gpt-4", prompt="Implement"),
            ],
            entry_point="ic",
        )
        result = expand_stages(config)
        assert result.workflow.entry_point == "ic"

    def test_bare_route_targets_rewritten(self) -> None:
        """Bare routes targeting a staged agent should be rewritten to default."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="ic"),
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Go",
                    stages={"review": StageDef(prompt="Review")},
                ),
                AgentDef(
                    name="ic",
                    model="gpt-4",
                    prompt="Implement",
                    routes=[RouteDef(to="vp")],
                ),
            ],
        )
        result = expand_stages(config)

        ic = next(a for a in result.agents if a.name == "ic")
        assert ic.routes[0].to == "vp:default"

    def test_stage_qualified_route_not_rewritten(self) -> None:
        """Already-qualified routes like 'vp:review' should not be rewritten."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="ic"),
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Go",
                    stages={"review": StageDef(prompt="Review")},
                ),
                AgentDef(
                    name="ic",
                    model="gpt-4",
                    prompt="Implement",
                    routes=[RouteDef(to="vp:review")],
                ),
            ],
        )
        result = expand_stages(config)

        ic = next(a for a in result.agents if a.name == "ic")
        assert ic.routes[0].to == "vp:review"

    def test_original_agent_preserved(self) -> None:
        """The original staged agent should remain in config.agents."""
        config = _make_config(
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Go",
                    stages={"review": StageDef(prompt="Review")},
                ),
            ],
        )
        result = expand_stages(config)

        original = next(a for a in result.agents if a.name == "vp")
        assert original.stages is not None
        assert "review" in original.stages

    def test_synthetic_agents_have_no_stages(self) -> None:
        """All synthetic agents produced by expansion should have stages=None."""
        config = _make_config(
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Go",
                    stages={
                        "review": StageDef(prompt="Review"),
                        "summary": StageDef(prompt="Summarize"),
                    },
                ),
            ],
        )
        result = expand_stages(config)

        for agent in result.agents:
            if ":" in agent.name:
                assert agent.stages is None, f"{agent.name} should have stages=None"


# ---------------------------------------------------------------------------
# 4. Edge cases
# ---------------------------------------------------------------------------


class TestExpandStagesEdgeCases:
    """Edge-case tests for stage expansion."""

    def test_empty_stages_dict_no_expansion(self) -> None:
        """Agent with an empty stages dict should produce no synthetic agents."""
        config = _make_config(
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Go",
                    stages={},
                ),
            ],
        )
        original_count = len(config.agents)
        result = expand_stages(config)

        synthetic = [a for a in result.agents if ":" in a.name]
        assert len(synthetic) == 0
        assert len(result.agents) == original_count

    def test_multiple_staged_agents(self) -> None:
        """Two agents each with stages should both be fully expanded."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="vp"),
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Direct",
                    stages={"review": StageDef(prompt="VP review")},
                ),
                AgentDef(
                    name="ic",
                    model="gpt-4",
                    prompt="Implement",
                    routes=[RouteDef(to="vp:review")],
                    stages={"test": StageDef(prompt="Run tests")},
                ),
            ],
        )
        result = expand_stages(config)

        names = [a.name for a in result.agents]
        # Original agents
        assert "vp" in names
        assert "ic" in names
        # VP synthetics
        assert "vp:default" in names
        assert "vp:review" in names
        # IC synthetics
        assert "ic:default" in names
        assert "ic:test" in names


# ---------------------------------------------------------------------------
# 5. Name collision validation
# ---------------------------------------------------------------------------


class TestNameCollisionValidation:
    """Tests for name collision detection in expand_stages()."""

    def test_stage_name_collides_with_existing_agent(self) -> None:
        """Expansion should raise if a synthetic name matches an existing agent."""
        from conductor.exceptions import ConfigurationError

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="vp"),
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Go",
                    stages={"review": StageDef(prompt="Review")},
                ),
                AgentDef(
                    name="vp:review",
                    model="gpt-4",
                    prompt="Colliding agent",
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="Name collision"):
            expand_stages(config)

    def test_default_name_collides_with_existing_agent(self) -> None:
        """Expansion should raise if agent:default matches an existing agent."""
        from conductor.exceptions import ConfigurationError

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="vp"),
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Go",
                    stages={"review": StageDef(prompt="Review")},
                ),
                AgentDef(
                    name="vp:default",
                    model="gpt-4",
                    prompt="Colliding default agent",
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="Name collision"):
            expand_stages(config)

    def test_no_collision_when_names_are_unique(self) -> None:
        """Expansion should succeed when no name collisions exist."""
        config = _make_config(
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Go",
                    stages={"review": StageDef(prompt="Review")},
                ),
                AgentDef(
                    name="ic",
                    model="gpt-4",
                    prompt="Implement",
                ),
            ],
        )
        result = expand_stages(config)
        names = [a.name for a in result.agents]
        assert "vp:default" in names
        assert "vp:review" in names


# ---------------------------------------------------------------------------
# 6. For-each inline agent stages validation
# ---------------------------------------------------------------------------


class TestForEachStagesValidation:
    """Tests for rejecting stages on for-each inline agents."""

    def test_for_each_inline_agent_with_stages_rejected(self) -> None:
        """Validator should reject stages on for-each inline agents."""
        from conductor.config.schema import ForEachDef
        from conductor.config.validator import validate_workflow_config
        from conductor.exceptions import ConfigurationError

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="processor"),
            agents=[
                AgentDef(name="starter", model="gpt-4", prompt="Start"),
            ],
            for_each=[
                ForEachDef(
                    name="processor",
                    type="for_each",
                    source="starter.output.items",
                    **{"as": "item"},
                    agent=AgentDef(
                        name="worker",
                        model="gpt-4",
                        prompt="Process {{ item }}",
                        stages={"review": StageDef(prompt="Review {{ item }}")},
                    ),
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="Stages are not supported"):
            validate_workflow_config(config)

    def test_for_each_inline_agent_without_stages_ok(self) -> None:
        """For-each inline agent without stages should pass validation."""
        from conductor.config.schema import ForEachDef
        from conductor.config.validator import validate_workflow_config

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="processor"),
            agents=[
                AgentDef(name="starter", model="gpt-4", prompt="Start"),
            ],
            for_each=[
                ForEachDef(
                    name="processor",
                    type="for_each",
                    source="starter.output.items",
                    **{"as": "item"},
                    agent=AgentDef(
                        name="worker",
                        model="gpt-4",
                        prompt="Process {{ item }}",
                    ),
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        # Should not raise
        validate_workflow_config(config)
