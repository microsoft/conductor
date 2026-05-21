"""Integration tests for `type: set` steps in WorkflowEngine.

Covers:
- Linear workflow: set step → consumer script reads the value
- Single-value set step stores scalar; downstream renders ``step.output``
- Multi-value set step stores dict; routes branch on a boolean key
- Set step inside a parallel group publishes a value to context
- Set step as for-each inline agent normalises per-item values
- Output schema validation: rejected on scalar single-value, applied on multi
- Scalar set output → consumer accessing ``.output.field`` raises a clear error
- Set step counts toward ``max_iterations``
- Checkpoint round-trip preserves scalar / list / dict set outputs

Script steps drive consumer assertions where we need to observe state without
spinning up a real LLM provider.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    LimitsConfig,
    OutputField,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import TemplateError, ValidationError


def _make_engine(config: WorkflowConfig) -> WorkflowEngine:
    return WorkflowEngine(config, MagicMock())


def _collect_events(engine: WorkflowEngine) -> list[WorkflowEvent]:
    emitter = WorkflowEventEmitter()
    received: list[WorkflowEvent] = []
    emitter.subscribe(received.append)
    engine._event_emitter = emitter
    return received


class TestSetWorkflowLinear:
    """Linear single-value and multi-value set workflows."""

    @pytest.mark.asyncio
    async def test_single_value_scalar_in_output(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="set-single",
                entry_point="compute",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="compute",
                    type="set",
                    value="{{ workflow.input.org }}/{{ workflow.input.repo }}",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"slug": "{{ compute.output }}"},
        )
        engine = _make_engine(config)
        result = await engine.run({"org": "microsoft", "repo": "conductor"})
        assert result == {"slug": "microsoft/conductor"}

    @pytest.mark.asyncio
    async def test_multi_value_dict_field_access(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="set-multi",
                entry_point="derive",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="derive",
                    type="set",
                    values={
                        "is_breaking": ("{{ workflow.input.severity in ['high', 'critical'] }}"),
                        "branch": "{{ workflow.input.branch or 'main' }}",
                    },
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "is_breaking": "{{ derive.output.is_breaking }}",
                "branch": "{{ derive.output.branch }}",
            },
        )
        engine = _make_engine(config)
        result = await engine.run({"severity": "high", "branch": None})
        # The engine round-trips final output through _maybe_parse_json, so
        # `True` survives as a bool; `main` stays as a string.
        assert result == {"is_breaking": True, "branch": "main"}


class TestSetWorkflowRouting:
    """Routes attached to set steps work for both dict and scalar outputs."""

    @pytest.mark.asyncio
    async def test_route_on_boolean_set_output(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="set-route",
                entry_point="flag",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="flag",
                    type="set",
                    values={"is_breaking": "{{ workflow.input.severity == 'high' }}"},
                    routes=[
                        RouteDef(to="breaking_path", when="{{ output.is_breaking }}"),
                        RouteDef(to="safe_path"),
                    ],
                ),
                AgentDef(
                    name="breaking_path",
                    type="script",
                    command=sys.executable,
                    args=["-c", "print('breaking')"],
                    routes=[RouteDef(to="$end")],
                ),
                AgentDef(
                    name="safe_path",
                    type="script",
                    command=sys.executable,
                    args=["-c", "print('safe')"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "stdout": (
                    "{% if breaking_path is defined %}{{ breaking_path.output.stdout }}"
                    "{% else %}{{ safe_path.output.stdout }}{% endif %}"
                ),
            },
        )
        engine = _make_engine(config)
        result_breaking = await engine.run({"severity": "high"})
        assert "breaking" in result_breaking["stdout"]

        engine2 = _make_engine(config)
        result_safe = await engine2.run({"severity": "low"})
        assert "safe" in result_safe["stdout"]

    @pytest.mark.asyncio
    async def test_route_on_scalar_set_output(self) -> None:
        """Routes attached to a scalar set step see ``output`` directly."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="set-scalar-route",
                entry_point="flag",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="flag",
                    type="set",
                    value="{{ workflow.input.severity == 'high' }}",
                    routes=[
                        RouteDef(to="hi", when="{{ output }}"),
                        RouteDef(to="lo"),
                    ],
                ),
                AgentDef(
                    name="hi",
                    type="script",
                    command=sys.executable,
                    args=["-c", "print('hi')"],
                    routes=[RouteDef(to="$end")],
                ),
                AgentDef(
                    name="lo",
                    type="script",
                    command=sys.executable,
                    args=["-c", "print('lo')"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "stdout": (
                    "{% if hi is defined %}{{ hi.output.stdout }}"
                    "{% else %}{{ lo.output.stdout }}{% endif %}"
                ),
            },
        )
        result = await _make_engine(config).run({"severity": "high"})
        assert "hi" in result["stdout"]
        result2 = await _make_engine(config).run({"severity": "low"})
        assert "lo" in result2["stdout"]


class TestSetInParallelGroup:
    """Set steps inside parallel groups publish their value to context."""

    @pytest.mark.asyncio
    async def test_set_in_parallel_publishes_values(self) -> None:
        """Two set steps in a parallel group both publish to context."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="set-parallel",
                entry_point="grp",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(name="left", type="set", value="{{ workflow.input.a }}"),
                AgentDef(name="right", type="set", value="{{ workflow.input.b }}"),
            ],
            parallel=[
                ParallelGroup(
                    name="grp",
                    agents=["left", "right"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "left": "{{ grp.outputs.left }}",
                "right": "{{ grp.outputs.right }}",
            },
        )
        result = await _make_engine(config).run({"a": "alpha", "b": "beta"})
        assert result == {"left": "alpha", "right": "beta"}


class TestSetInForEach:
    """Set steps inside for-each compute a per-item value."""

    @pytest.mark.asyncio
    async def test_set_in_for_each_per_item(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="set-foreach",
                entry_point="setup",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=20),
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
            output={"all": "{{ loop.outputs | join(',') }}"},
        )
        result = await _make_engine(config).run({})
        assert result["all"] == "item-1,item-2,item-3"


class TestSetOutputSchemaValidation:
    """Output schema validation rules for set steps."""

    @pytest.mark.asyncio
    async def test_scalar_value_with_output_schema_rejected(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="set-bad-schema",
                entry_point="bind",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="bind",
                    type="set",
                    value="hello",
                    output={"ok": OutputField(type="boolean")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"x": "{{ bind.output }}"},
        )
        with pytest.raises(ValidationError, match="not a dict"):
            await _make_engine(config).run({})

    @pytest.mark.asyncio
    async def test_multi_values_with_output_schema_pass(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="set-good-schema",
                entry_point="bind",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="bind",
                    type="set",
                    values={"ok": "{{ true }}"},
                    output={"ok": OutputField(type="boolean")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"ok": "{{ bind.output.ok }}"},
        )
        result = await _make_engine(config).run({})
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_multi_values_with_output_schema_mismatch_fails(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="set-bad-schema-multi",
                entry_point="bind",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="bind",
                    type="set",
                    values={"ok": "{{ true }}"},
                    output={"missing": OutputField(type="boolean")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"x": "{{ bind.output.ok }}"},
        )
        with pytest.raises(ValidationError):
            await _make_engine(config).run({})


class TestSetEvents:
    """The engine emits set_started / set_completed / set_failed."""

    @pytest.mark.asyncio
    async def test_events_for_successful_set(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="set-events",
                entry_point="bind",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="bind",
                    type="set",
                    values={"ok": "{{ true }}"},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"x": "{{ bind.output.ok }}"},
        )
        engine = _make_engine(config)
        received = _collect_events(engine)
        await engine.run({})
        types = [ev.type for ev in received]
        assert "set_started" in types
        assert "set_completed" in types
        # set_completed payload structure
        completed = next(ev for ev in received if ev.type == "set_completed")
        assert completed.data["agent_name"] == "bind"
        assert "ok" in completed.data["output_keys"]
        assert completed.data["output_type"] == "auto"

    @pytest.mark.asyncio
    async def test_events_for_failed_set(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="set-fail",
                entry_point="bind",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="bind",
                    type="set",
                    value="{{ does_not_exist }}",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"x": "{{ bind.output }}"},
        )
        engine = _make_engine(config)
        received = _collect_events(engine)
        with pytest.raises(TemplateError):
            await engine.run({})
        types = [ev.type for ev in received]
        assert "set_failed" in types


class TestSetScalarFieldAccessErrors:
    """Accessing ``scalar.output.field`` from a downstream agent raises clearly."""

    @pytest.mark.asyncio
    async def test_downstream_scalar_field_access_raises(self) -> None:
        """Explicit-mode downstream consumer asking for ``compute.output.field``
        on a scalar-producing set step raises a helpful KeyError."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="set-bad-access",
                entry_point="compute",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="explicit"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="compute",
                    type="set",
                    value="myorg/myrepo",
                    routes=[RouteDef(to="consumer")],
                ),
                AgentDef(
                    name="consumer",
                    type="script",
                    command=sys.executable,
                    args=["-c", "import sys; print(sys.argv[1])", "{{ compute.output.field }}"],
                    input=["compute.output.field"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"x": "{{ consumer.output.stdout | default('') }}"},
        )
        with pytest.raises(KeyError, match="is a str, not a dict"):
            await _make_engine(config).run({})


class TestSetIterationCounting:
    """Set steps count toward max_iterations (matching script behaviour)."""

    @pytest.mark.asyncio
    async def test_set_counts_toward_max_iterations(self) -> None:
        """A loop of set steps exceeding the iteration limit raises."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="set-loop",
                entry_point="a",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=2),
            ),
            agents=[
                AgentDef(
                    name="a",
                    type="set",
                    value="x",
                    routes=[RouteDef(to="b")],
                ),
                AgentDef(
                    name="b",
                    type="set",
                    value="y",
                    routes=[RouteDef(to="a")],
                ),
            ],
            output={"x": "{{ a.output }}"},
        )
        from conductor.exceptions import MaxIterationsError

        # skip_gates=True so the iteration-limit prompt doesn't block on stdin.
        engine = WorkflowEngine(config, MagicMock(), skip_gates=True)
        with pytest.raises(MaxIterationsError):
            await engine.run({})


class TestSetCheckpointRoundtrip:
    """Context survives serialize/restore with scalar / list / dict set outputs."""

    def test_context_to_from_dict(self) -> None:
        """Direct round-trip via WorkflowContext (no engine restart needed)."""
        from conductor.engine.context import WorkflowContext

        ctx = WorkflowContext()
        ctx.store("scalar", "myorg/myrepo")
        ctx.store("list_out", [1, 2, 3])
        ctx.store("dict_out", {"a": 1, "b": 2})
        snapshot = ctx.to_dict()
        # JSON-safe by construction (engine forces this via _to_json_safe).
        rendered = json.dumps(snapshot)
        restored = WorkflowContext.from_dict(json.loads(rendered))
        assert restored.agent_outputs["scalar"] == "myorg/myrepo"
        assert restored.agent_outputs["list_out"] == [1, 2, 3]
        assert restored.agent_outputs["dict_out"] == {"a": 1, "b": 2}
