"""Tests for budget enforcement.

Tests cover the graduation path:
1. No budget set (default) — no tracking, no warnings, no errors.
2. Budget set in audit mode — event emitted on overshoot, workflow continues.
3. Budget set in enforce mode — event emitted, workflow stops with BudgetExceededError.
4. LimitEnforcer.check_budget() unit tests (first-time flag, repeated calls).
5. Schema validation for budget fields.
"""

from __future__ import annotations

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
from conductor.engine.usage import UsageTracker
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEventEmitter
from conductor.exceptions import BudgetExceededError
from conductor.providers.base import AgentOutput
from conductor.providers.copilot import CopilotProvider


# ---------------------------------------------------------------------------
# Schema / Config Tests
# ---------------------------------------------------------------------------


class TestBudgetSchema:
    """Test LimitsConfig budget fields."""

    def test_default_no_budget(self) -> None:
        """No budget by default — graduation step 0."""
        limits = LimitsConfig()
        assert limits.budget_usd is None
        assert limits.budget_mode == "audit"

    def test_budget_usd_set(self) -> None:
        limits = LimitsConfig(budget_usd=5.0)
        assert limits.budget_usd == 5.0
        assert limits.budget_mode == "audit"

    def test_budget_enforce_mode(self) -> None:
        limits = LimitsConfig(budget_usd=5.0, budget_mode="enforce")
        assert limits.budget_usd == 5.0
        assert limits.budget_mode == "enforce"

    def test_budget_usd_zero_allowed(self) -> None:
        limits = LimitsConfig(budget_usd=0.0)
        assert limits.budget_usd == 0.0

    def test_budget_usd_negative_rejected(self) -> None:
        with pytest.raises(Exception):
            LimitsConfig(budget_usd=-1.0)

    def test_budget_mode_invalid_rejected(self) -> None:
        with pytest.raises(Exception):
            LimitsConfig(budget_mode="invalid")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LimitEnforcer Unit Tests
# ---------------------------------------------------------------------------


class TestLimitEnforcerBudget:
    """Unit tests for LimitEnforcer.check_budget()."""

    def test_no_budget_set_returns_not_exceeded(self) -> None:
        """When budget_usd is None, check_budget always returns (False, False)."""
        enforcer = LimitEnforcer()
        exceeded, first_time = enforcer.check_budget(100.0)
        assert exceeded is False
        assert first_time is False

    def test_under_budget_returns_not_exceeded(self) -> None:
        enforcer = LimitEnforcer(budget_usd=10.0)
        exceeded, first_time = enforcer.check_budget(5.0)
        assert exceeded is False
        assert first_time is False

    def test_at_budget_returns_not_exceeded(self) -> None:
        """Exactly at budget is not exceeded (> not >=)."""
        enforcer = LimitEnforcer(budget_usd=10.0)
        exceeded, first_time = enforcer.check_budget(10.0)
        assert exceeded is False
        assert first_time is False

    def test_over_budget_returns_exceeded_first_time(self) -> None:
        enforcer = LimitEnforcer(budget_usd=10.0)
        exceeded, first_time = enforcer.check_budget(10.01)
        assert exceeded is True
        assert first_time is True

    def test_over_budget_second_call_not_first_time(self) -> None:
        """After first overshoot detection, subsequent calls return first_time=False."""
        enforcer = LimitEnforcer(budget_usd=10.0)
        _, first1 = enforcer.check_budget(10.01)
        assert first1 is True
        _, first2 = enforcer.check_budget(15.0)
        assert first2 is False

    def test_budget_mode_stored(self) -> None:
        enforcer = LimitEnforcer(budget_usd=5.0, budget_mode="enforce")
        assert enforcer.budget_mode == "enforce"

    def test_default_budget_mode_is_audit(self) -> None:
        enforcer = LimitEnforcer(budget_usd=5.0)
        assert enforcer.budget_mode == "audit"


# ---------------------------------------------------------------------------
# WorkflowEngine Integration Tests
# ---------------------------------------------------------------------------


def _make_config(
    budget_usd: float | None = None,
    budget_mode: str = "audit",
    max_iterations: int = 10,
) -> WorkflowConfig:
    """Build a minimal single-agent workflow config for budget tests."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="budget-test",
            entry_point="agent1",
            limits=LimitsConfig(
                max_iterations=max_iterations,
                budget_usd=budget_usd,
                budget_mode=budget_mode,
            ),
        ),
        agents=[
            AgentDef(
                name="agent1",
                prompt="Say hello",
                output={"result": OutputField(type="string")},
            ),
        ],
        output={"result": "{{ agent1.output.result }}"},
    )


def _make_expensive_output() -> AgentOutput:
    """Build an AgentOutput that costs roughly $18 (1M input + 1M output on claude-sonnet-4)."""
    return AgentOutput(
        content={"result": "expensive"},
        raw_response="{}",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        model="claude-sonnet-4",
    )


def _make_cheap_output() -> AgentOutput:
    """Build an AgentOutput that costs a fraction of a cent."""
    return AgentOutput(
        content={"result": "cheap"},
        raw_response="{}",
        input_tokens=100,
        output_tokens=50,
        model="claude-sonnet-4",
    )


class TestBudgetGraduationStep0:
    """Graduation Step 0: No budget set — default behavior unchanged."""

    @pytest.mark.asyncio
    async def test_no_budget_workflow_completes(self) -> None:
        """Without budget_usd, expensive agents run without any budget checks."""
        config = _make_config(budget_usd=None)
        expensive = _make_expensive_output()

        def mock_handler(agent, prompt, context):
            return expensive.content

        provider = CopilotProvider(mock_handler=mock_handler)
        emitter = WorkflowEventEmitter()
        events: list = []
        emitter.subscribe(lambda e: events.append(e))

        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        # Patch the output so usage_tracker records the expensive tokens
        original_execute = provider.execute

        async def patched_execute(*args, **kwargs):
            return expensive

        provider.execute = patched_execute  # type: ignore[assignment]

        result = await engine.run({})
        assert result is not None

        # No budget_exceeded event
        budget_events = [e for e in events if e.type == "budget_exceeded"]
        assert len(budget_events) == 0


class TestBudgetGraduationStep1:
    """Graduation Step 1: Budget in audit mode — warn but continue."""

    @pytest.mark.asyncio
    async def test_audit_mode_emits_event_and_continues(self) -> None:
        """In audit mode, budget overshoot emits event but workflow completes."""
        config = _make_config(budget_usd=0.001, budget_mode="audit")
        expensive = _make_expensive_output()

        def mock_handler(agent, prompt, context):
            return expensive.content

        provider = CopilotProvider(mock_handler=mock_handler)
        emitter = WorkflowEventEmitter()
        events: list = []
        emitter.subscribe(lambda e: events.append(e))

        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        original_execute = provider.execute

        async def patched_execute(*args, **kwargs):
            return expensive

        provider.execute = patched_execute  # type: ignore[assignment]

        result = await engine.run({})
        assert result is not None  # Workflow completed despite overshoot

        # budget_exceeded event was emitted
        budget_events = [e for e in events if e.type == "budget_exceeded"]
        assert len(budget_events) == 1
        assert budget_events[0].data["budget_mode"] == "audit"
        assert budget_events[0].data["budget_usd"] == 0.001
        assert budget_events[0].data["spent_usd"] > 0.001

    @pytest.mark.asyncio
    async def test_audit_mode_emits_event_only_once(self) -> None:
        """In audit mode with looping workflow, event emits only on first overshoot."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="budget-loop-test",
                entry_point="agent1",
                limits=LimitsConfig(
                    max_iterations=5,
                    budget_usd=0.001,
                    budget_mode="audit",
                ),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    prompt="Say hello",
                    output={"result": OutputField(type="string")},
                    routes=[
                        RouteDef(
                            to="agent1",
                            when="{{ context.iteration < 3 }}",
                        ),
                        RouteDef(to="$end"),
                    ],
                ),
            ],
            output={"result": "{{ agent1.output.result }}"},
        )
        expensive = _make_expensive_output()

        call_count = 0

        def mock_handler(agent, prompt, context):
            return expensive.content

        provider = CopilotProvider(mock_handler=mock_handler)
        emitter = WorkflowEventEmitter()
        events: list = []
        emitter.subscribe(lambda e: events.append(e))

        engine = WorkflowEngine(config, provider, event_emitter=emitter)

        async def patched_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return expensive

        provider.execute = patched_execute  # type: ignore[assignment]

        result = await engine.run({})
        assert result is not None
        assert call_count >= 2

        # budget_exceeded emitted exactly once despite multiple over-budget agents
        budget_events = [e for e in events if e.type == "budget_exceeded"]
        assert len(budget_events) == 1


class TestBudgetGraduationStep2:
    """Graduation Step 2: Budget in enforce mode — stop on overshoot."""

    @pytest.mark.asyncio
    async def test_enforce_mode_raises_budget_exceeded(self) -> None:
        """In enforce mode, budget overshoot raises BudgetExceededError."""
        config = _make_config(budget_usd=0.001, budget_mode="enforce")
        expensive = _make_expensive_output()

        def mock_handler(agent, prompt, context):
            return expensive.content

        provider = CopilotProvider(mock_handler=mock_handler)
        emitter = WorkflowEventEmitter()
        events: list = []
        emitter.subscribe(lambda e: events.append(e))

        engine = WorkflowEngine(config, provider, event_emitter=emitter)

        async def patched_execute(*args, **kwargs):
            return expensive

        provider.execute = patched_execute  # type: ignore[assignment]

        with pytest.raises(BudgetExceededError) as exc_info:
            await engine.run({})

        assert exc_info.value.budget_usd == 0.001
        assert exc_info.value.spent_usd > 0.001

        # budget_exceeded event was emitted before the error
        budget_events = [e for e in events if e.type == "budget_exceeded"]
        assert len(budget_events) == 1
        assert budget_events[0].data["budget_mode"] == "enforce"

        # workflow_failed event was also emitted
        fail_events = [e for e in events if e.type == "workflow_failed"]
        assert len(fail_events) == 1
        assert fail_events[0].data["error_type"] == "BudgetExceededError"
        assert "budget_usd" in fail_events[0].data
        assert "spent_usd" in fail_events[0].data

    @pytest.mark.asyncio
    async def test_enforce_mode_under_budget_completes(self) -> None:
        """In enforce mode, a cheap workflow under budget completes normally."""
        config = _make_config(budget_usd=100.0, budget_mode="enforce")
        cheap = _make_cheap_output()

        def mock_handler(agent, prompt, context):
            return cheap.content

        provider = CopilotProvider(mock_handler=mock_handler)
        emitter = WorkflowEventEmitter()
        events: list = []
        emitter.subscribe(lambda e: events.append(e))

        engine = WorkflowEngine(config, provider, event_emitter=emitter)

        async def patched_execute(*args, **kwargs):
            return cheap

        provider.execute = patched_execute  # type: ignore[assignment]

        result = await engine.run({})
        assert result is not None

        # No budget_exceeded events
        budget_events = [e for e in events if e.type == "budget_exceeded"]
        assert len(budget_events) == 0


# ---------------------------------------------------------------------------
# BudgetExceededError Tests
# ---------------------------------------------------------------------------


class TestBudgetExceededError:
    """Test BudgetExceededError exception."""

    def test_error_attributes(self) -> None:
        error = BudgetExceededError(
            "over budget",
            budget_usd=5.0,
            spent_usd=7.50,
            current_agent="agent1",
        )
        assert error.budget_usd == 5.0
        assert error.spent_usd == 7.50
        assert error.current_agent == "agent1"
        assert "5.00" in error.suggestion
        assert "agent1" in error.suggestion

    def test_error_auto_suggestion(self) -> None:
        error = BudgetExceededError(
            "over budget",
            budget_usd=10.0,
            spent_usd=15.0,
        )
        assert "limits.budget_usd" in error.suggestion
        assert "audit" in error.suggestion

    def test_error_is_execution_error(self) -> None:
        from conductor.exceptions import ExecutionError

        error = BudgetExceededError(
            "over budget",
            budget_usd=1.0,
            spent_usd=2.0,
        )
        assert isinstance(error, ExecutionError)
