"""Tests for LimitEnforcer and safety limits.

Tests cover:
- Iteration limit enforcement
- Timeout enforcement
- MaxIterationsError context
- TimeoutError context
- Default limits
- Integration with WorkflowEngine
"""

import asyncio

import pytest

from conductor.config.schema import (
    AgentDef,
    LimitsConfig,
    OutputField,
    RouteDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.limits import LimitEnforcer
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import (
    MaxIterationsError,
)
from conductor.exceptions import (
    TimeoutError as ConductorTimeoutError,
)
from conductor.providers.copilot import CopilotProvider


class TestLimitEnforcerBasic:
    """Basic LimitEnforcer functionality tests."""

    def test_default_limits(self) -> None:
        """Test default limit values."""
        enforcer = LimitEnforcer()
        assert enforcer.max_iterations == 10
        assert enforcer.timeout_seconds is None  # Unlimited by default

    def test_custom_limits(self) -> None:
        """Test custom limit values."""
        enforcer = LimitEnforcer(max_iterations=5, timeout_seconds=30)
        assert enforcer.max_iterations == 5
        assert enforcer.timeout_seconds == 30

    def test_start_resets_state(self) -> None:
        """Test that start() resets the enforcer state."""
        enforcer = LimitEnforcer()
        enforcer.current_iteration = 5
        enforcer.execution_history = ["agent1", "agent2"]
        enforcer.current_agent = "agent3"

        enforcer.start()

        assert enforcer.current_iteration == 0
        assert enforcer.execution_history == []
        assert enforcer.current_agent is None
        assert enforcer.start_time is not None

    def test_record_execution(self) -> None:
        """Test that record_execution() updates state correctly."""
        enforcer = LimitEnforcer()
        enforcer.start()

        enforcer.record_execution("agent1")
        assert enforcer.current_iteration == 1
        assert enforcer.execution_history == ["agent1"]

        enforcer.record_execution("agent2")
        assert enforcer.current_iteration == 2
        assert enforcer.execution_history == ["agent1", "agent2"]

    def test_check_iteration_under_limit(self) -> None:
        """Test check_iteration() when under the limit."""
        enforcer = LimitEnforcer(max_iterations=3)
        enforcer.start()

        # Should not raise for iterations 0, 1, 2
        enforcer.check_iteration("agent1")
        enforcer.record_execution("agent1")
        enforcer.check_iteration("agent2")
        enforcer.record_execution("agent2")
        enforcer.check_iteration("agent3")
        # Not recording execution yet, so still at 2


class TestIterationLimit:
    """Tests for iteration limit enforcement."""

    def test_max_iterations_error_raised(self) -> None:
        """Test that MaxIterationsError is raised at the limit."""
        enforcer = LimitEnforcer(max_iterations=2)
        enforcer.start()

        enforcer.check_iteration("agent1")
        enforcer.record_execution("agent1")
        enforcer.check_iteration("agent2")
        enforcer.record_execution("agent2")

        with pytest.raises(MaxIterationsError) as exc_info:
            enforcer.check_iteration("agent3")

        error = exc_info.value
        assert error.iterations == 2
        assert error.max_iterations == 2
        assert error.agent_history == ["agent1", "agent2"]
        assert "exceeded maximum iterations" in str(error)

    def test_max_iterations_error_includes_suggestion(self) -> None:
        """Test that MaxIterationsError includes helpful suggestion."""
        enforcer = LimitEnforcer(max_iterations=3)
        enforcer.start()

        for i in range(3):
            enforcer.check_iteration(f"agent{i}")
            enforcer.record_execution(f"agent{i}")

        with pytest.raises(MaxIterationsError) as exc_info:
            enforcer.check_iteration("agent_extra")

        error = exc_info.value
        assert error.suggestion is not None
        assert "max_iterations" in error.suggestion

    def test_iteration_limit_at_exact_boundary(self) -> None:
        """Test behavior at exactly the iteration limit."""
        enforcer = LimitEnforcer(max_iterations=1)
        enforcer.start()

        # First iteration should work
        enforcer.check_iteration("agent1")
        enforcer.record_execution("agent1")

        # Second iteration should fail
        with pytest.raises(MaxIterationsError):
            enforcer.check_iteration("agent2")

    def test_zero_iterations_recorded_at_start(self) -> None:
        """Test that no iterations are recorded at start."""
        enforcer = LimitEnforcer(max_iterations=5)
        enforcer.start()
        assert enforcer.current_iteration == 0


class TestTimeoutLimit:
    """Tests for timeout limit enforcement."""

    def test_check_timeout_under_limit(self) -> None:
        """Test check_timeout() when under the limit."""
        enforcer = LimitEnforcer(timeout_seconds=60)
        enforcer.start()

        # Should not raise immediately after start
        enforcer.check_timeout()

    def test_check_timeout_not_started(self) -> None:
        """Test check_timeout() when not started."""
        enforcer = LimitEnforcer()
        # Should not raise when not started
        enforcer.check_timeout()

    def test_get_elapsed_time(self) -> None:
        """Test get_elapsed_time() returns sensible values."""
        enforcer = LimitEnforcer()

        # Before start
        assert enforcer.get_elapsed_time() == 0.0

        enforcer.start()

        # After start, should be very small but positive
        elapsed = enforcer.get_elapsed_time()
        assert elapsed >= 0.0
        assert elapsed < 1.0  # Should be nearly instantaneous

    def test_get_remaining_timeout(self) -> None:
        """Test get_remaining_timeout() returns sensible values."""
        enforcer = LimitEnforcer(timeout_seconds=60)

        # Before start
        assert enforcer.get_remaining_timeout() == 60.0

        enforcer.start()

        # After start, should be close to full timeout
        remaining = enforcer.get_remaining_timeout()
        assert remaining > 59.0
        assert remaining <= 60.0

    @pytest.mark.asyncio
    async def test_timeout_context_success(self) -> None:
        """Test timeout_context() with successful completion."""
        enforcer = LimitEnforcer(timeout_seconds=5)

        async with enforcer.timeout_context():
            await asyncio.sleep(0.01)

        # Should complete without error

    @pytest.mark.asyncio
    async def test_timeout_context_raises_error(self) -> None:
        """Test timeout_context() raises ConductorTimeoutError on timeout."""
        enforcer = LimitEnforcer(timeout_seconds=0.1)

        with pytest.raises(ConductorTimeoutError) as exc_info:
            async with enforcer.timeout_context():
                # Set current_agent after entering context (since start() resets it)
                enforcer.current_agent = "slow_agent"
                await asyncio.sleep(1.0)

        error = exc_info.value
        assert error.timeout_seconds == 0.1
        assert error.elapsed_seconds >= 0.1
        assert error.current_agent == "slow_agent"
        assert "exceeded timeout" in str(error)

    @pytest.mark.asyncio
    async def test_timeout_error_includes_suggestion(self) -> None:
        """Test that TimeoutError includes helpful suggestion."""
        enforcer = LimitEnforcer(timeout_seconds=0.05)

        with pytest.raises(ConductorTimeoutError) as exc_info:
            async with enforcer.timeout_context():
                await asyncio.sleep(1.0)

        error = exc_info.value
        assert error.suggestion is not None
        assert "timeout_seconds" in error.suggestion


class TestWorkflowEngineLimits:
    """Tests for limits integration in WorkflowEngine."""

    @pytest.fixture
    def infinite_loop_config(self) -> WorkflowConfig:
        """Create a workflow that loops infinitely."""
        return WorkflowConfig(
            workflow=WorkflowDef(
                name="infinite-loop",
                entry_point="looper",
                limits=LimitsConfig(max_iterations=3, timeout_seconds=60),
            ),
            agents=[
                AgentDef(
                    name="looper",
                    model="gpt-4",
                    prompt="Loop forever",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="looper")],  # Always loops back
                ),
            ],
            output={"result": "{{ looper.output.value }}"},
        )

    @pytest.mark.asyncio
    async def test_max_iterations_stops_infinite_loop(
        self, infinite_loop_config: WorkflowConfig
    ) -> None:
        """Test that max_iterations stops an infinite loop."""

        def mock_handler(agent, prompt, context):
            return {"value": "looped"}

        provider = CopilotProvider(mock_handler=mock_handler)
        # Use skip_gates=True to auto-stop without interactive prompt
        engine = WorkflowEngine(infinite_loop_config, provider, skip_gates=True)

        with pytest.raises(MaxIterationsError) as exc_info:
            await engine.run({})

        error = exc_info.value
        assert error.iterations == 3
        assert error.agent_history == ["looper", "looper", "looper"]

    @pytest.mark.asyncio
    async def test_default_limits_applied(self) -> None:
        """Test that default limits are applied when not specified."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="default-limits",
                entry_point="agent",
                # No limits specified - should use defaults
            ),
            agents=[
                AgentDef(
                    name="agent",
                    model="gpt-4",
                    prompt="test",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent.output.value }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"value": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        # Check default limits are set
        assert engine.limits.max_iterations == 10
        assert engine.limits.timeout_seconds is None  # Unlimited by default

        # Execute should work normally
        result = await engine.run({})
        assert result["result"] == "done"

    @pytest.mark.asyncio
    async def test_limits_configurable(self) -> None:
        """Test that limits can be configured via workflow.limits."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="custom-limits",
                entry_point="agent",
                limits=LimitsConfig(max_iterations=50, timeout_seconds=120),
            ),
            agents=[
                AgentDef(
                    name="agent",
                    model="gpt-4",
                    prompt="test",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent.output.value }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"value": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        assert engine.limits.max_iterations == 50
        assert engine.limits.timeout_seconds == 120

    @pytest.mark.asyncio
    async def test_execution_summary_includes_limits(self) -> None:
        """Test that get_execution_summary() includes limit information."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="summary-test",
                entry_point="agent",
                limits=LimitsConfig(max_iterations=10, timeout_seconds=300),
            ),
            agents=[
                AgentDef(
                    name="agent",
                    model="gpt-4",
                    prompt="test",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent.output.value }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"value": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        await engine.run({})

        summary = engine.get_execution_summary()
        assert "max_iterations" in summary
        assert "timeout_seconds" in summary
        assert "elapsed_seconds" in summary
        assert summary["max_iterations"] == 10
        assert summary["timeout_seconds"] == 300
        assert summary["elapsed_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_loop_terminates_with_condition(self) -> None:
        """Test that a loop terminates correctly when condition is met."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="terminating-loop",
                entry_point="counter",
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="counter",
                    model="gpt-4",
                    prompt="Count",
                    output={"count": OutputField(type="number")},
                    routes=[
                        RouteDef(to="$end", when="count >= 3"),
                        RouteDef(to="counter"),
                    ],
                ),
            ],
            output={"final": "{{ counter.output.count }}"},
        )

        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            return {"count": call_count}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        # Should stop at 3, not hit the iteration limit
        assert call_count == 3
        assert result["final"] == 3

        summary = engine.get_execution_summary()
        assert summary["iterations"] == 3

    @pytest.mark.asyncio
    async def test_iteration_tracking_with_multiple_agents(self) -> None:
        """Test iteration tracking with multiple agents in sequence."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="multi-agent",
                entry_point="agent1",
                limits=LimitsConfig(max_iterations=5),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Step 1",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Step 2",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="agent3")],
                ),
                AgentDef(
                    name="agent3",
                    model="gpt-4",
                    prompt="Step 3",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "done"},
        )

        def mock_handler(agent, prompt, context):
            return {"value": agent.name}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        await engine.run({})

        summary = engine.get_execution_summary()
        assert summary["iterations"] == 3
        assert summary["agents_executed"] == ["agent1", "agent2", "agent3"]


class TestEdgeCases:
    """Edge case tests for limits enforcement."""

    def test_max_iterations_one(self) -> None:
        """Test with max_iterations=1."""
        enforcer = LimitEnforcer(max_iterations=1)
        enforcer.start()

        enforcer.check_iteration("agent1")
        enforcer.record_execution("agent1")

        with pytest.raises(MaxIterationsError):
            enforcer.check_iteration("agent2")

    def test_execution_history_preserved_in_error(self) -> None:
        """Test that execution history is preserved in MaxIterationsError."""
        enforcer = LimitEnforcer(max_iterations=3)
        enforcer.start()

        agents = ["planner", "executor", "validator"]
        for agent in agents:
            enforcer.check_iteration(agent)
            enforcer.record_execution(agent)

        with pytest.raises(MaxIterationsError) as exc_info:
            enforcer.check_iteration("extra")

        assert exc_info.value.agent_history == agents

    @pytest.mark.asyncio
    async def test_timeout_context_starts_if_not_started(self) -> None:
        """Test that timeout_context() starts the enforcer if not started."""
        enforcer = LimitEnforcer(timeout_seconds=5)
        assert enforcer.start_time is None

        async with enforcer.timeout_context():
            assert enforcer.start_time is not None

    def test_multiple_starts_reset_state(self) -> None:
        """Test that calling start() multiple times resets state."""
        enforcer = LimitEnforcer()
        enforcer.start()
        enforcer.record_execution("agent1")
        enforcer.record_execution("agent2")

        first_start = enforcer.start_time

        # Start again
        enforcer.start()

        assert enforcer.current_iteration == 0
        assert enforcer.execution_history == []
        assert enforcer.start_time != first_start


class TestParallelGroupLimits:
    """Tests for parallel group iteration and timeout limits."""

    def test_check_parallel_group_iteration_within_limit(self) -> None:
        """Test check_parallel_group_iteration when within limit."""
        enforcer = LimitEnforcer(max_iterations=10)
        enforcer.start()

        # Should not raise for 3 agents when we have 10 iterations available
        enforcer.check_parallel_group_iteration("parallel_group", 3)

    def test_check_parallel_group_iteration_exceeds_limit(self) -> None:
        """Test check_parallel_group_iteration when exceeding limit."""
        enforcer = LimitEnforcer(max_iterations=5)
        enforcer.start()

        # Execute 3 agents
        enforcer.record_execution("agent1", count=3)

        # Try to execute parallel group with 3 agents (would need 6 total)
        with pytest.raises(MaxIterationsError) as exc_info:
            enforcer.check_parallel_group_iteration("parallel_group", 3)

        error = exc_info.value
        assert "would exceed maximum iterations" in str(error)
        assert "parallel_group" in str(error)
        assert error.iterations == 3
        assert error.max_iterations == 5

    def test_check_parallel_group_iteration_at_exact_boundary(self) -> None:
        """Test parallel group at exact iteration boundary."""
        enforcer = LimitEnforcer(max_iterations=6)
        enforcer.start()

        # Execute 3 agents
        enforcer.record_execution("agent1", count=3)

        # Should succeed with exactly 3 remaining
        enforcer.check_parallel_group_iteration("parallel_group", 3)

        # Record the parallel group execution
        enforcer.record_execution("parallel_group", count=3)

        # No more iterations available
        with pytest.raises(MaxIterationsError):
            enforcer.check_iteration("next_agent")

    def test_record_execution_with_count(self) -> None:
        """Test record_execution with count parameter for parallel groups."""
        enforcer = LimitEnforcer()
        enforcer.start()

        # Record a parallel group with 3 agents
        enforcer.record_execution("parallel_group_1", count=3)

        assert enforcer.current_iteration == 3
        assert enforcer.execution_history == ["parallel_group_1"]

        # Record another parallel group with 2 agents
        enforcer.record_execution("parallel_group_2", count=2)

        assert enforcer.current_iteration == 5
        assert enforcer.execution_history == ["parallel_group_1", "parallel_group_2"]

    def test_check_parallel_group_iteration_suggestion(self) -> None:
        """Test that check_parallel_group_iteration includes helpful suggestion."""
        enforcer = LimitEnforcer(max_iterations=4)
        enforcer.start()

        # Execute 2 agents
        enforcer.record_execution("agent1", count=2)

        # Try to execute parallel group with 4 agents (would need 6 total)
        with pytest.raises(MaxIterationsError) as exc_info:
            enforcer.check_parallel_group_iteration("big_group", 4)

        error = exc_info.value
        assert error.suggestion is not None
        assert "max_iterations" in error.suggestion or "reduce the number" in error.suggestion

    @pytest.mark.asyncio
    async def test_parallel_group_with_max_iterations(self) -> None:
        """Test that parallel groups count all agents toward iteration limit."""
        from conductor.config.schema import ParallelGroup

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-limits",
                entry_point="parallel_workers",
                limits=LimitsConfig(max_iterations=5),
            ),
            agents=[
                AgentDef(
                    name="worker1",
                    model="gpt-4",
                    prompt="Task 1",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="worker2",
                    model="gpt-4",
                    prompt="Task 2",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="worker3",
                    model="gpt-4",
                    prompt="Task 3",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_workers",
                    agents=["worker1", "worker2", "worker3"],
                    failure_mode="continue_on_error",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"final": "done"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": agent.name}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        await engine.run({})

        summary = engine.get_execution_summary()
        # All 3 parallel agents should count
        assert summary["iterations"] == 3
        assert summary["agents_executed"] == ["parallel_workers"]

    @pytest.mark.asyncio
    async def test_parallel_group_exceeds_iteration_limit(self) -> None:
        """Test that parallel group execution fails when exceeding iteration limit."""
        from conductor.config.schema import ParallelGroup

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-overflow",
                entry_point="parallel_workers",
                limits=LimitsConfig(max_iterations=2),  # Too low for 3 agents
            ),
            agents=[
                AgentDef(
                    name="worker1",
                    model="gpt-4",
                    prompt="Task 1",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="worker2",
                    model="gpt-4",
                    prompt="Task 2",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="worker3",
                    model="gpt-4",
                    prompt="Task 3",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_workers",
                    agents=["worker1", "worker2", "worker3"],
                    failure_mode="fail_fast",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"final": "done"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": agent.name}

        provider = CopilotProvider(mock_handler=mock_handler)
        # Use skip_gates=True to auto-stop without interactive prompt
        engine = WorkflowEngine(config, provider, skip_gates=True)

        with pytest.raises(MaxIterationsError) as exc_info:
            await engine.run({})

        error = exc_info.value
        assert "would exceed maximum iterations" in str(error)
        assert error.max_iterations == 2

    @pytest.mark.asyncio
    async def test_wait_for_with_timeout_success(self) -> None:
        """Test wait_for_with_timeout with successful completion."""
        enforcer = LimitEnforcer(timeout_seconds=5)
        enforcer.start()

        async def quick_task():
            await asyncio.sleep(0.01)
            return "done"

        result = await enforcer.wait_for_with_timeout(quick_task(), operation_name="quick_task")
        assert result == "done"

    @pytest.mark.asyncio
    async def test_wait_for_with_timeout_exceeds(self) -> None:
        """Test wait_for_with_timeout when timeout is exceeded."""
        enforcer = LimitEnforcer(timeout_seconds=0.1)
        enforcer.start()

        async def slow_task():
            await asyncio.sleep(1.0)
            return "done"

        with pytest.raises(ConductorTimeoutError) as exc_info:
            await enforcer.wait_for_with_timeout(slow_task(), operation_name="slow_task")

        error = exc_info.value
        assert "slow_task" in str(error)
        assert error.timeout_seconds == 0.1

    @pytest.mark.asyncio
    async def test_wait_for_with_no_timeout(self) -> None:
        """Test wait_for_with_timeout when no timeout is set."""
        enforcer = LimitEnforcer(timeout_seconds=None)
        enforcer.start()

        async def task():
            await asyncio.sleep(0.01)
            return "done"

        # Should complete without timeout enforcement
        result = await enforcer.wait_for_with_timeout(task(), operation_name="task")
        assert result == "done"

    @pytest.mark.asyncio
    async def test_parallel_group_with_timeout(self) -> None:
        """Test that timeout is enforced during parallel group execution."""
        from conductor.config.schema import ParallelGroup

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-timeout",
                entry_point="parallel_workers",
                limits=LimitsConfig(max_iterations=10, timeout_seconds=1),
            ),
            agents=[
                AgentDef(
                    name="worker1",
                    model="gpt-4",
                    prompt="Task 1",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="worker2",
                    model="gpt-4",
                    prompt="Task 2",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_workers",
                    agents=["worker1", "worker2"],
                    failure_mode="fail_fast",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"final": "done"},
        )

        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            # Simulate slow agent execution
            import time

            time.sleep(2.0)  # Longer than timeout
            return {"result": agent.name}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        with pytest.raises(ConductorTimeoutError) as exc_info:
            await engine.run({})

        error = exc_info.value
        # Timeout should be enforced during workflow execution
        assert error.timeout_seconds == 1
        assert "timeout" in str(error).lower()


class TestIncreaseLimitMethod:
    """Tests for LimitEnforcer.increase_limit() method."""

    def test_increase_limit_basic(self) -> None:
        """Test that increase_limit increases max_iterations."""
        enforcer = LimitEnforcer(max_iterations=10)
        enforcer.increase_limit(5)
        assert enforcer.max_iterations == 15

    def test_increase_limit_zero(self) -> None:
        """Test that increase_limit with 0 does not change limit."""
        enforcer = LimitEnforcer(max_iterations=10)
        enforcer.increase_limit(0)
        assert enforcer.max_iterations == 10

    def test_increase_limit_negative(self) -> None:
        """Test that increase_limit with negative value does not change limit."""
        enforcer = LimitEnforcer(max_iterations=10)
        enforcer.increase_limit(-5)
        assert enforcer.max_iterations == 10

    def test_increase_limit_multiple_times(self) -> None:
        """Test that increase_limit can be called multiple times."""
        enforcer = LimitEnforcer(max_iterations=5)
        enforcer.increase_limit(3)
        enforcer.increase_limit(2)
        assert enforcer.max_iterations == 10

    def test_increase_limit_allows_more_iterations(self) -> None:
        """Test that increasing limit allows more iterations."""
        enforcer = LimitEnforcer(max_iterations=2)
        enforcer.start()

        # Execute up to limit
        enforcer.check_iteration("agent1")
        enforcer.record_execution("agent1")
        enforcer.check_iteration("agent2")
        enforcer.record_execution("agent2")

        # Should now fail
        with pytest.raises(MaxIterationsError):
            enforcer.check_iteration("agent3")

        # Increase limit
        enforcer.increase_limit(2)

        # Should now succeed
        enforcer.check_iteration("agent3")
        enforcer.record_execution("agent3")
        enforcer.check_iteration("agent4")
        enforcer.record_execution("agent4")

        # Should fail again at new limit
        with pytest.raises(MaxIterationsError):
            enforcer.check_iteration("agent5")


class TestMaxIterationsWorkflowIntegration:
    """Tests for interactive max iterations prompt integration with WorkflowEngine."""

    @pytest.fixture
    def looping_workflow_config(self) -> WorkflowConfig:
        """Create a workflow that loops indefinitely."""
        return WorkflowConfig(
            workflow=WorkflowDef(
                name="looping-workflow",
                entry_point="looper",
                limits=LimitsConfig(max_iterations=2),  # Low limit
            ),
            agents=[
                AgentDef(
                    name="looper",
                    model="gpt-4",
                    prompt="Loop",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="looper")],  # Always loops back
                ),
            ],
            output={"result": "{{ looper.output.value }}"},
        )

    @pytest.mark.asyncio
    async def test_interactive_prompt_increases_limit_and_continues(
        self, looping_workflow_config: WorkflowConfig
    ) -> None:
        """Test that providing more iterations via prompt allows workflow to continue."""
        from unittest.mock import AsyncMock

        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            # Stop looping after 4 iterations by returning a special value
            if call_count >= 4:
                return {"value": "done", "stop": True}
            return {"value": f"loop_{call_count}"}

        # Modify the workflow to terminate after 4 loops
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="conditional-loop",
                entry_point="looper",
                limits=LimitsConfig(max_iterations=2),
            ),
            agents=[
                AgentDef(
                    name="looper",
                    model="gpt-4",
                    prompt="Loop",
                    output={"value": OutputField(type="string")},
                    routes=[
                        RouteDef(to="$end", when="value == 'done'"),
                        RouteDef(to="looper"),
                    ],
                ),
            ],
            output={"result": "{{ looper.output.value }}"},
        )

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, skip_gates=False)

        # Mock the max iterations handler to return 5 more iterations
        mock_result = AsyncMock()
        mock_result.continue_execution = True
        mock_result.additional_iterations = 5
        engine.max_iterations_handler.handle_limit_reached = AsyncMock(return_value=mock_result)

        result = await engine.run({})

        # Workflow should have completed successfully
        assert result["result"] == "done"
        assert call_count == 4
        # Handler should have been called once when limit was reached
        engine.max_iterations_handler.handle_limit_reached.assert_called_once()

    @pytest.mark.asyncio
    async def test_interactive_prompt_stop_raises_error(
        self, looping_workflow_config: WorkflowConfig
    ) -> None:
        """Test that choosing to stop via prompt raises MaxIterationsError."""
        from unittest.mock import AsyncMock

        def mock_handler(agent, prompt, context):
            return {"value": "looped"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(looping_workflow_config, provider, skip_gates=False)

        # Mock the max iterations handler to return stop
        mock_result = AsyncMock()
        mock_result.continue_execution = False
        mock_result.additional_iterations = 0
        engine.max_iterations_handler.handle_limit_reached = AsyncMock(return_value=mock_result)

        with pytest.raises(MaxIterationsError) as exc_info:
            await engine.run({})

        error = exc_info.value
        assert error.iterations == 2
        assert error.max_iterations == 2

    @pytest.mark.asyncio
    async def test_skip_gates_auto_stops(self, looping_workflow_config: WorkflowConfig) -> None:
        """Test that skip_gates mode auto-stops without prompting."""

        def mock_handler(agent, prompt, context):
            return {"value": "looped"}

        provider = CopilotProvider(mock_handler=mock_handler)
        # skip_gates=True should auto-stop
        engine = WorkflowEngine(looping_workflow_config, provider, skip_gates=True)

        with pytest.raises(MaxIterationsError) as exc_info:
            await engine.run({})

        error = exc_info.value
        assert error.iterations == 2
