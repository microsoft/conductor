"""Unit tests for WorkflowContext and LimitEnforcer serialization.

Tests cover:
- WorkflowContext.to_dict() / from_dict() round-trip
- LimitEnforcer.to_dict() / from_dict() round-trip
- Empty, single-agent, multi-agent, parallel, and for-each context states
- JSON serializability of all output
- Fresh start_time on LimitEnforcer reconstruction
"""

import json
import time

import pytest

from conductor.engine.context import WorkflowContext
from conductor.engine.limits import LimitEnforcer

# ---------------------------------------------------------------------------
# WorkflowContext serialization tests
# ---------------------------------------------------------------------------


class TestWorkflowContextToDict:
    """Tests for WorkflowContext.to_dict()."""

    def test_empty_context(self) -> None:
        """Empty context serializes to expected default dict."""
        ctx = WorkflowContext()
        d = ctx.to_dict()

        assert d == {
            "workflow_inputs": {},
            "agent_outputs": {},
            "current_iteration": 0,
            "execution_history": [],
        }

    def test_single_agent_output(self) -> None:
        """Context with a single agent output serializes correctly."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"topic": "AI"})
        ctx.store("planner", {"plan": "research AI", "steps": 3})

        d = ctx.to_dict()

        assert d["workflow_inputs"] == {"topic": "AI"}
        assert d["agent_outputs"]["planner"] == {"plan": "research AI", "steps": 3}
        assert d["current_iteration"] == 1
        assert d["execution_history"] == ["planner"]

    def test_multiple_agents(self) -> None:
        """Context with multiple agent outputs serializes correctly."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"q": "hello"})
        ctx.store("agent_a", {"x": 1})
        ctx.store("agent_b", {"y": 2})
        ctx.store("agent_c", {"z": 3})

        d = ctx.to_dict()

        assert len(d["agent_outputs"]) == 3
        assert d["current_iteration"] == 3
        assert d["execution_history"] == ["agent_a", "agent_b", "agent_c"]

    def test_parallel_group_output(self) -> None:
        """Parallel group output format is preserved through serialization."""
        ctx = WorkflowContext()
        parallel_output = {
            "type": "parallel",
            "outputs": {"a1": {"r": 1}, "a2": {"r": 2}},
            "errors": {},
        }
        ctx.store("parallel_group", parallel_output)

        d = ctx.to_dict()

        assert d["agent_outputs"]["parallel_group"]["type"] == "parallel"
        assert d["agent_outputs"]["parallel_group"]["outputs"] == {"a1": {"r": 1}, "a2": {"r": 2}}
        assert d["agent_outputs"]["parallel_group"]["errors"] == {}

    def test_for_each_list_output(self) -> None:
        """For-each group with list-based outputs serializes correctly."""
        ctx = WorkflowContext()
        foreach_output = {
            "type": "for_each",
            "outputs": [{"item": "a"}, {"item": "b"}],
            "errors": {},
            "count": 2,
        }
        ctx.store("foreach_group", foreach_output)

        d = ctx.to_dict()

        assert d["agent_outputs"]["foreach_group"]["type"] == "for_each"
        assert d["agent_outputs"]["foreach_group"]["outputs"] == [{"item": "a"}, {"item": "b"}]
        assert d["agent_outputs"]["foreach_group"]["count"] == 2

    def test_for_each_dict_output(self) -> None:
        """For-each group with dict-based (key_by) outputs serializes correctly."""
        ctx = WorkflowContext()
        foreach_output = {
            "type": "for_each",
            "outputs": {"key1": {"val": 1}, "key2": {"val": 2}},
            "errors": {},
            "count": 2,
        }
        ctx.store("keyed_group", foreach_output)

        d = ctx.to_dict()

        assert d["agent_outputs"]["keyed_group"]["outputs"] == {
            "key1": {"val": 1},
            "key2": {"val": 2},
        }

    def test_json_serializable(self) -> None:
        """to_dict() output can be serialized to JSON without error."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"x": 1, "nested": {"a": [1, 2]}})
        ctx.store("agent", {"text": "hello", "list": [1, 2, 3]})

        d = ctx.to_dict()
        serialized = json.dumps(d)

        assert isinstance(serialized, str)

    def test_deep_copy_isolation(self) -> None:
        """Mutations to the original context don't affect the serialized dict."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"key": "value"})
        ctx.store("agent", {"data": [1, 2]})

        d = ctx.to_dict()

        # Mutate original
        ctx.workflow_inputs["key"] = "changed"
        ctx.agent_outputs["agent"]["data"].append(3)
        ctx.execution_history.append("extra")

        assert d["workflow_inputs"]["key"] == "value"
        assert d["agent_outputs"]["agent"]["data"] == [1, 2]
        assert d["execution_history"] == ["agent"]


class TestWorkflowContextFromDict:
    """Tests for WorkflowContext.from_dict()."""

    def test_empty_dict(self) -> None:
        """from_dict with empty values produces an empty context."""
        ctx = WorkflowContext.from_dict({})

        assert ctx.workflow_inputs == {}
        assert ctx.agent_outputs == {}
        assert ctx.current_iteration == 0
        assert ctx.execution_history == []

    def test_full_reconstruction(self) -> None:
        """from_dict fully reconstructs context state."""
        data = {
            "workflow_inputs": {"topic": "AI"},
            "agent_outputs": {
                "planner": {"plan": "do stuff"},
                "researcher": {"findings": ["a", "b"]},
            },
            "current_iteration": 2,
            "execution_history": ["planner", "researcher"],
        }

        ctx = WorkflowContext.from_dict(data)

        assert ctx.workflow_inputs == {"topic": "AI"}
        assert ctx.agent_outputs["planner"] == {"plan": "do stuff"}
        assert ctx.agent_outputs["researcher"] == {"findings": ["a", "b"]}
        assert ctx.current_iteration == 2
        assert ctx.execution_history == ["planner", "researcher"]

    def test_deep_copy_isolation(self) -> None:
        """Mutations to the source dict don't affect the reconstructed context."""
        data = {
            "workflow_inputs": {"k": "v"},
            "agent_outputs": {"a": {"list": [1]}},
            "current_iteration": 1,
            "execution_history": ["a"],
        }

        ctx = WorkflowContext.from_dict(data)

        # Mutate source
        data["workflow_inputs"]["k"] = "changed"
        data["agent_outputs"]["a"]["list"].append(2)
        data["execution_history"].append("extra")

        assert ctx.workflow_inputs["k"] == "v"
        assert ctx.agent_outputs["a"]["list"] == [1]
        assert ctx.execution_history == ["a"]


class TestWorkflowContextRoundTrip:
    """Round-trip tests for WorkflowContext.to_dict() -> from_dict()."""

    def test_empty_round_trip(self) -> None:
        """Empty context survives round-trip."""
        original = WorkflowContext()
        restored = WorkflowContext.from_dict(original.to_dict())

        assert restored.workflow_inputs == original.workflow_inputs
        assert restored.agent_outputs == original.agent_outputs
        assert restored.current_iteration == original.current_iteration
        assert restored.execution_history == original.execution_history

    def test_full_round_trip(self) -> None:
        """Context with inputs, multiple agents, and history survives round-trip."""
        original = WorkflowContext()
        original.set_workflow_inputs({"topic": "AI", "depth": "comprehensive"})
        original.store("planner", {"plan": "step1, step2", "summary": "plan summary"})
        original.store("researcher", {"findings": ["f1", "f2"], "sources": ["s1"], "coverage": 85})

        restored = WorkflowContext.from_dict(original.to_dict())

        assert restored.workflow_inputs == original.workflow_inputs
        assert restored.agent_outputs == original.agent_outputs
        assert restored.current_iteration == original.current_iteration
        assert restored.execution_history == original.execution_history

    def test_parallel_group_round_trip(self) -> None:
        """Parallel group output survives round-trip."""
        original = WorkflowContext()
        original.store(
            "parallel_research",
            {
                "type": "parallel",
                "outputs": {"r1": {"data": "x"}, "r2": {"data": "y"}},
                "errors": {},
            },
        )

        restored = WorkflowContext.from_dict(original.to_dict())

        assert restored.agent_outputs == original.agent_outputs

    def test_for_each_round_trip(self) -> None:
        """For-each group output survives round-trip."""
        original = WorkflowContext()
        original.store(
            "batch",
            {
                "type": "for_each",
                "outputs": [{"result": i} for i in range(5)],
                "errors": {},
                "count": 5,
            },
        )

        restored = WorkflowContext.from_dict(original.to_dict())

        assert restored.agent_outputs == original.agent_outputs

    def test_round_trip_json_intermediary(self) -> None:
        """Context survives round-trip through JSON serialization."""
        original = WorkflowContext()
        original.set_workflow_inputs({"nested": {"a": [1, 2, 3]}})
        original.store("agent", {"text": "hello", "count": 42})

        json_str = json.dumps(original.to_dict())
        data = json.loads(json_str)
        restored = WorkflowContext.from_dict(data)

        assert restored.workflow_inputs == original.workflow_inputs
        assert restored.agent_outputs == original.agent_outputs
        assert restored.current_iteration == original.current_iteration
        assert restored.execution_history == original.execution_history

    def test_build_for_agent_after_round_trip(self) -> None:
        """Restored context works correctly with build_for_agent()."""
        original = WorkflowContext()
        original.set_workflow_inputs({"topic": "AI"})
        original.store("planner", {"plan": "steps"})

        restored = WorkflowContext.from_dict(original.to_dict())

        agent_ctx = restored.build_for_agent("researcher", [], "accumulate")
        assert agent_ctx["planner"]["output"]["plan"] == "steps"
        assert agent_ctx["workflow"]["input"]["topic"] == "AI"


# ---------------------------------------------------------------------------
# LimitEnforcer serialization tests
# ---------------------------------------------------------------------------


class TestLimitEnforcerToDict:
    """Tests for LimitEnforcer.to_dict()."""

    def test_default_state(self) -> None:
        """Default enforcer serializes to expected dict."""
        enforcer = LimitEnforcer()
        d = enforcer.to_dict()

        assert d == {
            "current_iteration": 0,
            "max_iterations": 10,
            "execution_history": [],
        }

    def test_mid_execution_state(self) -> None:
        """Mid-execution enforcer includes iteration progress."""
        enforcer = LimitEnforcer(max_iterations=20, timeout_seconds=120)
        enforcer.start()
        enforcer.record_execution("agent_a")
        enforcer.record_execution("agent_b")
        enforcer.record_execution("agent_b")

        d = enforcer.to_dict()

        assert d["current_iteration"] == 3
        assert d["max_iterations"] == 20
        assert d["execution_history"] == ["agent_a", "agent_b", "agent_b"]

    def test_excludes_transient_state(self) -> None:
        """to_dict() does not include start_time or current_agent."""
        enforcer = LimitEnforcer()
        enforcer.start()
        enforcer.current_agent = "some_agent"

        d = enforcer.to_dict()

        assert "start_time" not in d
        assert "current_agent" not in d
        assert "timeout_seconds" not in d

    def test_parallel_group_iteration_count(self) -> None:
        """Parallel group records correct iteration count."""
        enforcer = LimitEnforcer(max_iterations=50)
        enforcer.start()
        enforcer.record_execution("parallel_group", count=5)

        d = enforcer.to_dict()

        assert d["current_iteration"] == 5
        assert d["execution_history"] == ["parallel_group"]

    def test_increased_limit_preserved(self) -> None:
        """User-increased max_iterations is preserved in serialization."""
        enforcer = LimitEnforcer(max_iterations=10)
        enforcer.increase_limit(5)

        d = enforcer.to_dict()

        assert d["max_iterations"] == 15

    def test_json_serializable(self) -> None:
        """to_dict() output can be serialized to JSON."""
        enforcer = LimitEnforcer(max_iterations=20)
        enforcer.start()
        enforcer.record_execution("a1")
        enforcer.record_execution("a2")

        serialized = json.dumps(enforcer.to_dict())
        assert isinstance(serialized, str)


class TestLimitEnforcerFromDict:
    """Tests for LimitEnforcer.from_dict()."""

    def test_basic_reconstruction(self) -> None:
        """from_dict restores iteration state."""
        data = {
            "current_iteration": 5,
            "max_iterations": 20,
            "execution_history": ["a1", "a2", "a3", "a3", "a3"],
        }

        enforcer = LimitEnforcer.from_dict(data)

        assert enforcer.current_iteration == 5
        assert enforcer.max_iterations == 20
        assert enforcer.execution_history == ["a1", "a2", "a3", "a3", "a3"]

    def test_fresh_start_time(self) -> None:
        """from_dict sets a fresh start_time (not None)."""
        data = {
            "current_iteration": 3,
            "max_iterations": 10,
            "execution_history": ["a", "b", "c"],
        }

        before = time.monotonic()
        enforcer = LimitEnforcer.from_dict(data)
        after = time.monotonic()

        assert enforcer.start_time is not None
        assert before <= enforcer.start_time <= after

    def test_timeout_from_parameter(self) -> None:
        """timeout_seconds comes from the parameter, not the checkpoint."""
        data = {
            "current_iteration": 0,
            "max_iterations": 10,
            "execution_history": [],
        }

        enforcer = LimitEnforcer.from_dict(data, timeout_seconds=300)

        assert enforcer.timeout_seconds == 300

    def test_timeout_default_none(self) -> None:
        """timeout_seconds defaults to None when not provided."""
        data = {
            "current_iteration": 0,
            "max_iterations": 10,
            "execution_history": [],
        }

        enforcer = LimitEnforcer.from_dict(data)

        assert enforcer.timeout_seconds is None

    def test_current_agent_not_set(self) -> None:
        """from_dict does not set current_agent (starts as None)."""
        data = {
            "current_iteration": 2,
            "max_iterations": 10,
            "execution_history": ["a", "b"],
        }

        enforcer = LimitEnforcer.from_dict(data)

        assert enforcer.current_agent is None

    def test_defaults_for_missing_fields(self) -> None:
        """from_dict handles missing fields gracefully with defaults."""
        enforcer = LimitEnforcer.from_dict({})

        assert enforcer.current_iteration == 0
        assert enforcer.max_iterations == 10
        assert enforcer.execution_history == []

    def test_user_increased_limit_preserved(self) -> None:
        """max_iterations from checkpoint (possibly user-increased) is preserved."""
        data = {
            "current_iteration": 8,
            "max_iterations": 25,  # originally 10, user increased
            "execution_history": ["a"] * 8,
        }

        enforcer = LimitEnforcer.from_dict(data, timeout_seconds=60)

        assert enforcer.max_iterations == 25
        assert enforcer.current_iteration == 8


class TestLimitEnforcerRoundTrip:
    """Round-trip tests for LimitEnforcer.to_dict() -> from_dict()."""

    def test_default_round_trip(self) -> None:
        """Default enforcer survives round-trip."""
        original = LimitEnforcer()
        restored = LimitEnforcer.from_dict(original.to_dict())

        assert restored.current_iteration == original.current_iteration
        assert restored.max_iterations == original.max_iterations
        assert restored.execution_history == original.execution_history

    def test_mid_execution_round_trip(self) -> None:
        """Mid-execution enforcer iteration state survives round-trip."""
        original = LimitEnforcer(max_iterations=20, timeout_seconds=120)
        original.start()
        original.record_execution("planner")
        original.record_execution("researcher")
        original.record_execution("researcher")

        restored = LimitEnforcer.from_dict(original.to_dict(), timeout_seconds=120)

        assert restored.current_iteration == original.current_iteration
        assert restored.max_iterations == original.max_iterations
        assert restored.execution_history == original.execution_history
        assert restored.timeout_seconds == 120

    def test_round_trip_json_intermediary(self) -> None:
        """Enforcer survives round-trip through JSON serialization."""
        original = LimitEnforcer(max_iterations=15)
        original.start()
        original.record_execution("a")
        original.record_execution("parallel", count=3)

        json_str = json.dumps(original.to_dict())
        data = json.loads(json_str)
        restored = LimitEnforcer.from_dict(data, timeout_seconds=60)

        assert restored.current_iteration == 4
        assert restored.max_iterations == 15
        assert restored.execution_history == ["a", "parallel"]

    def test_check_iteration_works_after_round_trip(self) -> None:
        """Restored enforcer correctly enforces iteration limits."""
        original = LimitEnforcer(max_iterations=5)
        original.start()
        original.record_execution("a")
        original.record_execution("b")
        original.record_execution("c")

        restored = LimitEnforcer.from_dict(original.to_dict())

        # Should allow 2 more iterations
        restored.check_iteration("d")
        restored.record_execution("d")
        restored.check_iteration("e")
        restored.record_execution("e")

        # Should now be at the limit
        from conductor.exceptions import MaxIterationsError

        with pytest.raises(MaxIterationsError):
            restored.check_iteration("f")
