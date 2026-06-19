"""Tests for budget enforcement.

Tests cover the graduation path:
1. No budget set (default) — no tracking, no warnings, no errors.
2. Budget set in audit mode — event emitted on overshoot, workflow continues.
3. Budget set in enforce mode — event emitted, workflow stops with BudgetExceededError.
4. LimitEnforcer.check_budget() unit tests (BudgetCheckResult, re-emission, repeated calls).
5. Schema validation for budget fields.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

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

    def test_budget_usd_zero_rejected(self) -> None:
        """Zero budget is rejected: a $0 limit would trip after the first token."""
        with pytest.raises(PydanticValidationError):
            LimitsConfig(budget_usd=0.0)

    def test_budget_usd_negative_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            LimitsConfig(budget_usd=-1.0)

    def test_budget_mode_invalid_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            LimitsConfig(budget_mode="invalid")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LimitEnforcer Unit Tests
# ---------------------------------------------------------------------------


class TestLimitEnforcerBudget:
    """Unit tests for LimitEnforcer.check_budget()."""

    def test_no_budget_set_returns_not_exceeded(self) -> None:
        """When budget_usd is None, check_budget always returns not-exceeded."""
        enforcer = LimitEnforcer()
        result = enforcer.check_budget(100.0)
        assert result.exceeded is False
        assert result.should_emit is False
        assert result.budget_usd is None
        assert result.spent_usd == 100.0

    def test_under_budget_returns_not_exceeded(self) -> None:
        enforcer = LimitEnforcer(budget_usd=10.0)
        result = enforcer.check_budget(5.0)
        assert result.exceeded is False
        assert result.should_emit is False
        assert result.budget_usd == 10.0
        assert result.spent_usd == 5.0

    def test_at_budget_returns_not_exceeded(self) -> None:
        """Exactly at budget is not exceeded (> not >=)."""
        enforcer = LimitEnforcer(budget_usd=10.0)
        result = enforcer.check_budget(10.0)
        assert result.exceeded is False
        assert result.should_emit is False

    def test_over_budget_returns_exceeded_first_time(self) -> None:
        enforcer = LimitEnforcer(budget_usd=10.0)
        result = enforcer.check_budget(10.01)
        assert result.exceeded is True
        assert result.should_emit is True
        assert result.budget_usd == 10.0
        assert result.spent_usd == 10.01

    def test_over_budget_small_growth_does_not_re_emit(self) -> None:
        """After first overshoot, a small extra spend does not re-emit."""
        enforcer = LimitEnforcer(budget_usd=10.0)
        first = enforcer.check_budget(10.01)
        assert first.should_emit is True
        # Still over budget, but not by another full $10 increment.
        second = enforcer.check_budget(15.0)
        assert second.exceeded is True
        assert second.should_emit is False

    def test_over_budget_re_emits_per_budget_increment(self) -> None:
        """Crossing another full budget increment re-arms emission."""
        enforcer = LimitEnforcer(budget_usd=10.0)
        first = enforcer.check_budget(10.01)
        assert first.should_emit is True
        # Spend has now climbed past another full $10 beyond the last emission.
        third = enforcer.check_budget(20.02)
        assert third.exceeded is True
        assert third.should_emit is True

    def test_budget_mode_stored(self) -> None:
        enforcer = LimitEnforcer(budget_usd=5.0, budget_mode="enforce")
        assert enforcer.budget_mode == "enforce"

    def test_default_budget_mode_is_audit(self) -> None:
        enforcer = LimitEnforcer(budget_usd=5.0)
        assert enforcer.budget_mode == "audit"

    def test_from_dict_applies_budget_from_config(self) -> None:
        """from_dict sources budget from the current config (fresh window)."""
        enforcer = LimitEnforcer(budget_usd=10.0, budget_mode="enforce")
        # Simulate having emitted during the original run.
        enforcer.check_budget(10.01)
        data = enforcer.to_dict()

        restored = LimitEnforcer.from_dict(
            data,
            timeout_seconds=None,
            budget_usd=3.0,
            budget_mode="audit",
        )
        # Budget comes from the supplied (current config) values, not the dict.
        assert restored.budget_usd == 3.0
        assert restored.budget_mode == "audit"
        # Emission tracking is reset: resume starts a fresh budget window.
        first = restored.check_budget(3.01)
        assert first.exceeded is True
        assert first.should_emit is True

    def test_from_dict_requires_budget_kwargs(self) -> None:
        """from_dict refuses to silently default the budget kwargs (I5)."""
        with pytest.raises(TypeError):
            LimitEnforcer.from_dict({"max_iterations": 10})  # type: ignore[call-arg]


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

        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {})
        emitter = WorkflowEventEmitter()
        events: list = []
        emitter.subscribe(lambda e: events.append(e))

        engine = WorkflowEngine(config, provider, event_emitter=emitter)

        # Patch the output so usage_tracker records the expensive tokens
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

        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {})
        emitter = WorkflowEventEmitter()
        events: list = []
        emitter.subscribe(lambda e: events.append(e))

        engine = WorkflowEngine(config, provider, event_emitter=emitter)

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
    async def test_audit_mode_re_emits_as_spend_grows(self) -> None:
        """In audit mode, emission re-arms per budget increment as spend grows.

        The old behavior latched after a single event (stale "$11/$10"
        figure). Now each agent that pushes spend past another full budget
        increment re-emits with the updated ``spent_usd``.
        """
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

        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {})
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

        # Each over-budget agent crosses another full budget increment, so the
        # event re-emits with a growing spent_usd (no longer latched at one).
        budget_events = [e for e in events if e.type == "budget_exceeded"]
        assert len(budget_events) >= 2
        spent_values = [e.data["spent_usd"] for e in budget_events]
        assert spent_values == sorted(spent_values)
        assert all(e.data["budget_mode"] == "audit" for e in budget_events)


class TestBudgetGraduationStep2:
    """Graduation Step 2: Budget in enforce mode — stop on overshoot."""

    @pytest.mark.asyncio
    async def test_enforce_mode_raises_budget_exceeded(self) -> None:
        """In enforce mode, budget overshoot raises BudgetExceededError."""
        config = _make_config(budget_usd=0.001, budget_mode="enforce")
        expensive = _make_expensive_output()

        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {})
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

        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {})
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
