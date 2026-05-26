"""Tests for 'set' type schema validation.

Covers:
- Valid single-value and multi-values set agent definitions
- Mutual exclusion of value/values (both forbidden, neither forbidden)
- output_type only valid on single value (forbidden on values)
- Every forbidden field rejected on set type
- value/values/output_type rejected on non-set types
- Cross-validator (set in entry point, parallel groups, for_each)
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


class TestSetAgentDefValidConfigs:
    """Valid set-type agent definitions."""

    def test_valid_single_value(self) -> None:
        agent = AgentDef(name="compute", type="set", value="{{ workflow.input.org }}")
        assert agent.type == "set"
        assert agent.value == "{{ workflow.input.org }}"
        assert agent.values is None
        assert agent.output_type is None

    def test_valid_multi_values(self) -> None:
        agent = AgentDef(
            name="derive",
            type="set",
            values={
                "is_breaking": "{{ true }}",
                "target_branch": "main",
            },
        )
        assert agent.values is not None
        assert agent.value is None
        assert len(agent.values) == 2

    def test_valid_with_output_type_on_single(self) -> None:
        for ot in ("auto", "string", "number", "integer", "boolean", "list", "dict"):
            agent = AgentDef(name="x", type="set", value="42", output_type=ot)  # type: ignore[arg-type]
            assert agent.output_type == ot

    def test_valid_with_routes(self) -> None:
        agent = AgentDef(
            name="flag",
            type="set",
            value="{{ true }}",
            routes=[RouteDef(to="$end")],
        )
        assert len(agent.routes) == 1

    def test_valid_with_input_declarations(self) -> None:
        agent = AgentDef(
            name="combine",
            type="set",
            value="{{ research.output.summary }}",
            input=["research.output"],
        )
        assert agent.input == ["research.output"]

    def test_valid_with_output_schema(self) -> None:
        agent = AgentDef(
            name="flags",
            type="set",
            values={"ok": "{{ true }}"},
            output={"ok": OutputField(type="boolean")},
        )
        assert agent.output is not None and "ok" in agent.output


class TestSetAgentDefMutualExclusion:
    """value: / values: mutual exclusion."""

    def test_neither_value_nor_values_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of 'value' or 'values'"):
            AgentDef(name="bad", type="set")

    def test_both_value_and_values_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of 'value' or 'values'"):
            AgentDef(name="bad", type="set", value="1", values={"a": "2"})

    def test_output_type_with_values_rejected(self) -> None:
        with pytest.raises(ValidationError, match="output_type"):
            AgentDef(
                name="bad",
                type="set",
                values={"a": "1"},
                output_type="string",
            )

    def test_output_type_with_value_accepted(self) -> None:
        agent = AgentDef(name="ok", type="set", value="1", output_type="integer")
        assert agent.output_type == "integer"


class TestSetAgentDefForbiddenFields:
    """Fields forbidden on set type."""

    @pytest.mark.parametrize(
        "field,value,err",
        [
            ("prompt", "hi", "cannot have 'prompt'"),
            ("provider", "copilot", "cannot have 'provider'"),
            ("model", "gpt-4", "cannot have 'model'"),
            ("tools", ["web_search"], "cannot have 'tools'"),
            ("system_prompt", "you are", "cannot have 'system_prompt'"),
            (
                "options",
                [GateOption(label="OK", value="ok", route="$end")],
                "cannot have 'options'",
            ),
            ("command", "echo", "cannot have 'command'"),
            ("args", ["x"], "cannot have 'args'"),
            ("env", {"K": "v"}, "cannot have 'env'"),
            ("working_dir", "/tmp", "cannot have 'working_dir'"),
            ("timeout", 5, "cannot have 'timeout'"),
            ("workflow", "x.yaml", "cannot have 'workflow'"),
            ("input_mapping", {"a": "1"}, "cannot have 'input_mapping'"),
            ("max_depth", 2, "cannot have 'max_depth'"),
            ("max_session_seconds", 10.0, "cannot have 'max_session_seconds'"),
            ("max_agent_iterations", 5, "cannot have 'max_agent_iterations'"),
            ("retry", RetryPolicy(max_attempts=2), "cannot have 'retry'"),
            ("timeout_seconds", 5.0, "cannot have 'timeout_seconds'"),
        ],
    )
    def test_forbidden_field_rejected(self, field: str, value: object, err: str) -> None:
        with pytest.raises(ValidationError, match=err):
            AgentDef(name="bad", type="set", value="x", **{field: value})  # type: ignore[arg-type]


class TestSetFieldsOnOtherTypes:
    """value/values/output_type rejected on non-set types."""

    def test_value_on_default_agent_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot have 'value'"):
            AgentDef(name="bad", value="x")

    def test_values_on_default_agent_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot have 'values'"):
            AgentDef(name="bad", values={"a": "1"})

    def test_output_type_on_default_agent_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot have 'output_type'"):
            AgentDef(name="bad", output_type="string")

    def test_value_on_script_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot have 'value'"):
            AgentDef(name="bad", type="script", command="echo", value="x")

    def test_values_on_script_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot have 'values'"):
            AgentDef(name="bad", type="script", command="echo", values={"a": "1"})

    def test_output_type_on_script_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot have 'output_type'"):
            AgentDef(name="bad", type="script", command="echo", output_type="string")

    def test_value_on_human_gate_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot have 'value'"):
            AgentDef(
                name="bad",
                type="human_gate",
                prompt="?",
                options=[GateOption(label="OK", value="ok", route="$end")],
                value="x",
            )

    def test_value_on_workflow_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot have 'value'"):
            AgentDef(name="bad", type="workflow", workflow="x.yaml", value="x")


class TestSetWorkflowConfig:
    """Cross-validator scenarios."""

    def test_set_at_entry_point_validates(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="compute",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="compute",
                    type="set",
                    value="{{ true }}",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_set_routes_to_agent(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="flag",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="flag",
                    type="set",
                    value="{{ true }}",
                    routes=[RouteDef(to="downstream")],
                ),
                AgentDef(name="downstream", prompt="hi", routes=[RouteDef(to="$end")]),
            ],
        )
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_set_in_parallel_group_allowed(self) -> None:
        """Per issue #221, set steps are permitted in parallel groups."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="grp",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(name="real", prompt="hi"),
                AgentDef(name="bind", type="set", value="{{ workflow.input.x }}"),
            ],
            parallel=[
                ParallelGroup(name="grp", agents=["real", "bind"], routes=[RouteDef(to="$end")]),
            ],
        )
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_set_in_for_each_allowed(self) -> None:
        """Per issue #221, set steps may be inline agents in for_each.

        Note: the for_each ``source:`` validator requires a 3-part path, so a
        set step producing a list at ``step.output`` cannot be used directly
        as a source — use ``values: {items: ...}`` instead so ``step.output.items``
        is a valid reference.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="setup",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="setup",
                    type="set",
                    values={"items": "{{ [1, 2, 3] }}"},
                    routes=[RouteDef(to="loop")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="loop",
                    type="for_each",
                    source="setup.output.items",
                    **{"as": "item"},
                    agent=AgentDef(
                        name="binder",
                        type="set",
                        value="item-{{ item }}",
                    ),
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_set_cannot_depend_on_sibling_in_parallel_group(self) -> None:
        """Set templates referencing same-group siblings must be rejected."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="grp",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(name="sibling", prompt="hi"),
                AgentDef(
                    name="bind",
                    type="set",
                    value="{{ sibling.output.summary }}",
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="grp",
                    agents=["sibling", "bind"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="same parallel group"):
            validate_workflow_config(config)


class TestSetBackwardCompatibility:
    """Existing types still work."""

    def test_default_agent_unchanged(self) -> None:
        a = AgentDef(name="x", prompt="hi")
        assert a.type is None
        assert a.value is None
        assert a.values is None
        assert a.output_type is None

    def test_script_unchanged(self) -> None:
        a = AgentDef(name="x", type="script", command="echo")
        assert a.value is None
        assert a.values is None
