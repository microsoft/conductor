"""Tests for ``type: wait`` schema validation.

Covers:
- Valid wait agent definitions (literal and templated durations).
- Required ``duration`` field.
- Forbidden fields on wait agents.
- Duration bounds (> 0 and <= 24h).
- Boolean duration rejection (pre-coercion).
- Reject wait inside parallel groups and as for-each inline agents.
- Reject ``duration`` and ``reason`` on non-wait agents.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from conductor.config.schema import (
    AgentDef,
    ForEachDef,
    GateOption,
    OutputField,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError


def _make_workflow(
    *agents: AgentDef,
    parallel: list[ParallelGroup] | None = None,
    for_each: list[ForEachDef] | None = None,
) -> WorkflowConfig:
    """Build a minimal WorkflowConfig for validator tests."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="wait-test",
            description="test",
            version="1.0.0",
            entry_point=agents[0].name,
            runtime=RuntimeConfig(provider="copilot"),
        ),
        agents=list(agents),
        parallel=parallel or [],
        for_each=for_each or [],
    )


class TestValidWait:
    """Wait agents accept duration as int/float/string or Jinja template."""

    def test_int_seconds(self) -> None:
        a = AgentDef(name="w", type="wait", duration=60)
        assert a.type == "wait"
        assert a.duration == 60

    def test_float_seconds(self) -> None:
        a = AgentDef(name="w", type="wait", duration=1.5)
        assert a.duration == 1.5

    def test_string_seconds(self) -> None:
        a = AgentDef(name="w", type="wait", duration="60s")
        assert a.duration == "60s"

    def test_string_minutes(self) -> None:
        AgentDef(name="w", type="wait", duration="5m")

    def test_string_milliseconds(self) -> None:
        AgentDef(name="w", type="wait", duration="500ms")

    def test_string_hours(self) -> None:
        AgentDef(name="w", type="wait", duration="1h")

    def test_24h_cap_inclusive(self) -> None:
        # Exactly 24h is allowed.
        AgentDef(name="w", type="wait", duration="24h")

    def test_templated_duration_deferred(self) -> None:
        # Templates are not parsed at schema time.
        a = AgentDef(name="w", type="wait", duration="{{ workflow.input.x }}s")
        assert a.duration == "{{ workflow.input.x }}s"

    def test_templated_garbage_deferred(self) -> None:
        # Even nonsense after the template is OK at schema time.
        AgentDef(name="w", type="wait", duration="{{ x }}-not-a-duration")

    def test_optional_reason(self) -> None:
        a = AgentDef(name="w", type="wait", duration="1s", reason="hello")
        assert a.reason == "hello"


class TestWaitRequiresDuration:
    def test_missing_duration(self) -> None:
        with pytest.raises(PydanticValidationError, match="require 'duration'"):
            AgentDef(name="w", type="wait")


class TestWaitDurationBounds:
    def test_zero_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="must be > 0"):
            AgentDef(name="w", type="wait", duration=0)

    def test_negative_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            AgentDef(name="w", type="wait", duration=-1)

    def test_over_24h_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="24h cap"):
            AgentDef(name="w", type="wait", duration="25h")

    def test_just_over_24h_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="24h cap"):
            AgentDef(name="w", type="wait", duration=86401)


class TestWaitDurationBool:
    def test_true_rejected(self) -> None:
        # Booleans must be rejected pre-coercion. Pydantic v2 would
        # otherwise accept True as int 1.
        with pytest.raises(PydanticValidationError, match="boolean"):
            AgentDef(name="w", type="wait", duration=True)

    def test_false_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="boolean"):
            AgentDef(name="w", type="wait", duration=False)


class TestWaitForbiddenFields:
    """Fields that don't make sense for wait must be rejected."""

    @pytest.mark.parametrize(
        "field,value,match",
        [
            ("prompt", "x", "'prompt'"),
            ("provider", "copilot", "'provider'"),
            ("model", "claude-haiku-4.5", "'model'"),
            ("system_prompt", "x", "'system_prompt'"),
            ("command", "ls", "'command'"),
            ("working_dir", "/tmp", "'working_dir'"),
            ("timeout", 5, "'timeout'"),
            ("workflow", "./sub.yaml", "'workflow'"),
            ("max_session_seconds", 30.0, "'max_session_seconds'"),
            ("max_agent_iterations", 5, "'max_agent_iterations'"),
            ("timeout_seconds", 10.0, "'timeout_seconds'"),
        ],
    )
    def test_forbidden(self, field: str, value: object, match: str) -> None:
        kwargs = {"name": "w", "type": "wait", "duration": "1s", field: value}
        with pytest.raises(PydanticValidationError, match=match):
            AgentDef(**kwargs)  # type: ignore[arg-type]

    def test_tools_list_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="'tools'"):
            AgentDef(name="w", type="wait", duration="1s", tools=["foo"])

    def test_options_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="'options'"):
            AgentDef(
                name="w",
                type="wait",
                duration="1s",
                options=[GateOption(label="x", value="x", route="$end")],
            )

    def test_args_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="'args'"):
            AgentDef(name="w", type="wait", duration="1s", args=["x"])

    def test_env_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="'env'"):
            AgentDef(name="w", type="wait", duration="1s", env={"FOO": "bar"})

    def test_input_mapping_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="'input_mapping'"):
            AgentDef(name="w", type="wait", duration="1s", input_mapping={"x": "y"})

    def test_max_depth_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="'max_depth'"):
            AgentDef(name="w", type="wait", duration="1s", max_depth=2)

    def test_output_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="'output'"):
            AgentDef(
                name="w",
                type="wait",
                duration="1s",
                output={"x": {"type": "string"}},
            )


class TestWaitFieldsOnOtherTypes:
    """duration/reason are wait-only — other types must reject them."""

    def test_duration_on_plain_agent(self) -> None:
        with pytest.raises(PydanticValidationError, match="'duration'"):
            AgentDef(name="a", duration="1s", prompt="hi", model="x")

    def test_reason_on_plain_agent(self) -> None:
        with pytest.raises(PydanticValidationError, match="'reason'"):
            AgentDef(name="a", reason="x", prompt="hi", model="x")

    def test_duration_on_script(self) -> None:
        with pytest.raises(PydanticValidationError, match="'duration'"):
            AgentDef(name="s", type="script", command="ls", duration="1s")


class TestWaitInParallelOrForEach:
    """Wait steps cannot be used in parallel groups or for-each groups."""

    def test_reject_wait_in_parallel(self) -> None:
        wait = AgentDef(name="w", type="wait", duration="1s", routes=[RouteDef(to="$end")])
        other = AgentDef(name="o", type="wait", duration="1s", routes=[RouteDef(to="$end")])
        config = _make_workflow(
            wait,
            other,
            parallel=[ParallelGroup(name="pg", agents=["w", "o"], routes=[RouteDef(to="$end")])],
        )
        with pytest.raises(ConfigurationError, match="Wait steps cannot be used in parallel"):
            validate_workflow_config(config)

    def test_reject_wait_in_for_each(self) -> None:
        wait = AgentDef(name="w", type="wait", duration="1s", routes=[RouteDef(to="$end")])
        # An entry-point agent + a producer agent (so the for-each
        # source resolves to a real agent reference).
        entry = AgentDef(
            name="entry",
            prompt="x",
            model="m",
            output={"items": OutputField(type="array", items={"type": "string"})},
            routes=[RouteDef(to="fe")],
        )
        for_each = ForEachDef(
            name="fe",
            type="for_each",
            source="entry.output.items",
            **{"as": "item"},
            agent=wait,
            routes=[RouteDef(to="$end")],
        )
        config = _make_workflow(entry, for_each=[for_each])
        with pytest.raises(ConfigurationError, match="Wait steps cannot be used in for_each"):
            validate_workflow_config(config)


class TestWaitValidationViaWorkflow:
    """Smoke test: a workflow containing only a wait step validates."""

    def test_minimal_wait_workflow(self) -> None:
        wait = AgentDef(name="w", type="wait", duration="100ms", routes=[RouteDef(to="$end")])
        config = _make_workflow(wait)
        # Should not raise.
        validate_workflow_config(config)


class TestWorkingDirTypeMatrix:
    """Requirement: ``working_dir`` is allowed on provider-backed LLM agents and
    script steps, and rejected on wait/set/terminate/human_gate/workflow types."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"name": "llm", "prompt": "hi"},
            {"name": "script", "type": "script", "command": "ls"},
        ],
        ids=["llm_agent", "script_step"],
    )
    def test_working_dir_allowed(self, kwargs: dict) -> None:
        agent = AgentDef(**kwargs, working_dir="/repo")  # type: ignore[arg-type]
        assert agent.working_dir == "/repo"

    @pytest.mark.parametrize(
        "kwargs,match",
        [
            (
                {"name": "w", "type": "wait", "duration": "1s"},
                "wait agents cannot have 'working_dir'",
            ),
            (
                {"name": "s", "type": "set", "value": "1"},
                "set agents cannot have 'working_dir'",
            ),
            (
                {"name": "t", "type": "terminate", "status": "success", "reason": "done"},
                "terminate agents cannot have 'working_dir'",
            ),
            (
                {
                    "name": "g",
                    "type": "human_gate",
                    "prompt": "Pick",
                    "options": [GateOption(label="Yes", value="yes", route="$end")],
                },
                "human_gate agents cannot have 'working_dir'",
            ),
            (
                {"name": "wf", "type": "workflow", "workflow": "./sub.yaml"},
                "workflow agents cannot have 'working_dir'",
            ),
        ],
        ids=["wait", "set", "terminate", "human_gate", "workflow"],
    )
    def test_working_dir_rejected(self, kwargs: dict, match: str) -> None:
        with pytest.raises(PydanticValidationError, match=match):
            AgentDef(**kwargs, working_dir="/repo")  # type: ignore[arg-type]
