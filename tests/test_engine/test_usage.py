"""Unit tests for the usage tracking module."""

import pytest

from conductor.engine.pricing import ModelPricing
from conductor.engine.usage import AgentUsage, UsageTracker, WorkflowUsage
from conductor.providers.base import AgentOutput


def _mk_usage(
    agent_name: str,
    model: str | None,
    cost_usd: float | None,
    *,
    input_tokens: int = 100,
    output_tokens: int = 100,
) -> AgentUsage:
    """Compact ``AgentUsage`` builder for the unpriced-surfacing tests."""
    return AgentUsage(
        agent_name=agent_name,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=cost_usd,
        elapsed_seconds=1.0,
    )


class TestAgentUsage:
    """Tests for the AgentUsage dataclass."""

    def test_agent_usage_creation(self) -> None:
        """Test creating AgentUsage with all fields."""
        usage = AgentUsage(
            agent_name="test-agent",
            model="claude-sonnet-4",
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=100,
            cache_write_tokens=50,
            cost_usd=0.0045,
            elapsed_seconds=1.5,
        )
        assert usage.agent_name == "test-agent"
        assert usage.model == "claude-sonnet-4"
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 500
        assert usage.cache_read_tokens == 100
        assert usage.cache_write_tokens == 50
        assert usage.cost_usd == 0.0045
        assert usage.elapsed_seconds == 1.5


class TestWorkflowUsage:
    """Tests for the WorkflowUsage dataclass."""

    def test_workflow_usage_empty(self) -> None:
        """Test WorkflowUsage with no agents."""
        usage = WorkflowUsage()
        assert usage.total_input_tokens == 0
        assert usage.total_output_tokens == 0
        assert usage.total_tokens == 0
        assert usage.total_cost_usd is None
        assert usage.total_elapsed_seconds == 0.0

    def test_workflow_usage_single_agent(self) -> None:
        """Test WorkflowUsage with a single agent."""
        agent_usage = AgentUsage(
            agent_name="agent1",
            model="claude-sonnet-4",
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=0.01,
            elapsed_seconds=2.0,
        )
        usage = WorkflowUsage(agents=[agent_usage])

        assert usage.total_input_tokens == 1000
        assert usage.total_output_tokens == 500
        assert usage.total_tokens == 1500
        assert usage.total_cost_usd == 0.01
        assert usage.total_elapsed_seconds == 2.0

    def test_workflow_usage_multiple_agents(self) -> None:
        """Test WorkflowUsage with multiple agents."""
        agents = [
            AgentUsage(
                agent_name="agent1",
                model="claude-sonnet-4",
                input_tokens=1000,
                output_tokens=500,
                cache_read_tokens=100,
                cache_write_tokens=50,
                cost_usd=0.01,
                elapsed_seconds=1.0,
            ),
            AgentUsage(
                agent_name="agent2",
                model="gpt-4o",
                input_tokens=2000,
                output_tokens=1000,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_usd=0.02,
                elapsed_seconds=1.5,
            ),
        ]
        usage = WorkflowUsage(agents=agents)

        assert usage.total_input_tokens == 3000
        assert usage.total_output_tokens == 1500
        assert usage.total_tokens == 4500
        assert usage.total_cache_read_tokens == 100
        assert usage.total_cache_write_tokens == 50
        assert usage.total_cost_usd == 0.03
        assert usage.total_elapsed_seconds == 2.5

    def test_workflow_usage_partial_cost_data(self) -> None:
        """Test WorkflowUsage when some agents lack cost data."""
        agents = [
            AgentUsage(
                agent_name="agent1",
                model="claude-sonnet-4",
                input_tokens=1000,
                output_tokens=500,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_usd=0.01,
                elapsed_seconds=1.0,
            ),
            AgentUsage(
                agent_name="agent2",
                model="unknown-model",
                input_tokens=2000,
                output_tokens=1000,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_usd=None,  # Unknown model, no pricing
                elapsed_seconds=1.5,
            ),
        ]
        usage = WorkflowUsage(agents=agents)

        # Should only sum known costs
        assert usage.total_cost_usd == 0.01

    def test_unpriced_agents_flags_token_spenders_without_cost(self) -> None:
        """unpriced_agents lists token-spending agents that lack cost data (#265)."""
        usage = WorkflowUsage(
            agents=[
                _mk_usage("priced", "claude-sonnet-4", 0.01),
                _mk_usage("unpriced", "gpt-5.5-future", None),
            ]
        )
        assert usage.has_unpriced is True
        assert [a.agent_name for a in usage.unpriced_agents] == ["unpriced"]
        assert usage.unpriced_models == ["gpt-5.5-future"]
        # Total stays the partial priced sum, not None.
        assert usage.total_cost_usd == 0.01

    def test_unpriced_ignores_zero_token_agents(self) -> None:
        """An agent that spent no tokens is not 'unpriced' — it cost $0."""
        usage = WorkflowUsage(
            agents=[_mk_usage("zero", "some-model", None, input_tokens=0, output_tokens=0)]
        )
        assert usage.unpriced_agents == []
        assert usage.unpriced_models == []
        assert usage.has_unpriced is False

    def test_unpriced_models_deduped_sorted_excluding_none(self) -> None:
        """unpriced_models is sorted + distinct; a None model omits the name only."""
        usage = WorkflowUsage(
            agents=[
                _mk_usage("a", "model-b", None),
                _mk_usage("b", "model-a", None),
                _mk_usage("c", "model-b", None),
                _mk_usage("d", None, None),
            ]
        )
        assert usage.unpriced_models == ["model-a", "model-b"]
        # The None-model agent still counts as an unpriced spender.
        assert len(usage.unpriced_agents) == 4
        assert usage.has_unpriced is True

    def test_no_unpriced_when_all_priced(self) -> None:
        """has_unpriced is False and the lists are empty when every agent is priced."""
        usage = WorkflowUsage(agents=[_mk_usage("a", "claude-sonnet-4", 0.005)])
        assert usage.has_unpriced is False
        assert usage.unpriced_agents == []
        assert usage.unpriced_models == []


class TestUsageTracker:
    """Tests for the UsageTracker class."""

    def test_usage_tracker_record(self) -> None:
        """Test recording usage from an agent output."""
        tracker = UsageTracker()

        output = AgentOutput(
            content={"result": "test"},
            raw_response="{}",
            tokens_used=1500,
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=100,
            cache_write_tokens=50,
            model="claude-sonnet-4",
        )

        usage = tracker.record("test-agent", output, elapsed=2.5)

        assert usage.agent_name == "test-agent"
        assert usage.model == "claude-sonnet-4"
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 500
        assert usage.cache_read_tokens == 100
        assert usage.cache_write_tokens == 50
        assert usage.elapsed_seconds == 2.5
        assert usage.cost_usd is not None
        # Pin the exact figure: a `> 0` assertion passes identically whether or
        # not the cached buckets are subtracted out of the input bucket, so it
        # cannot protect the seam it covers.
        # uncached input 1000 - 100 - 50 = 850 @ $3/M   = 0.00255
        # output              500        @ $15/M        = 0.0075
        # cache read          100        @ $0.30/M      = 0.00003
        # cache write          50        @ $3.75/M      = 0.0001875
        assert usage.cost_usd == pytest.approx(0.0102675, rel=1e-6)

    def test_usage_tracker_record_null_tokens(self) -> None:
        """Test recording when token fields are None."""
        tracker = UsageTracker()

        output = AgentOutput(
            content={"result": "test"},
            raw_response="{}",
            model="claude-sonnet-4",
            # All token fields are None
        )

        usage = tracker.record("test-agent", output, elapsed=1.0)

        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.cache_read_tokens == 0
        assert usage.cache_write_tokens == 0
        assert usage.cost_usd == 0.0  # Zero tokens = zero cost

    def test_usage_tracker_record_unknown_model(self) -> None:
        """Test recording with an unknown model."""
        tracker = UsageTracker()

        output = AgentOutput(
            content={"result": "test"},
            raw_response="{}",
            tokens_used=1500,
            input_tokens=1000,
            output_tokens=500,
            model="unknown-model-v1",
        )

        usage = tracker.record("test-agent", output, elapsed=1.0)

        assert usage.cost_usd is None  # Unknown model

    def test_usage_tracker_record_no_model(self) -> None:
        """Test recording when model is None."""
        tracker = UsageTracker()

        output = AgentOutput(
            content={"result": "test"},
            raw_response="{}",
            tokens_used=1500,
            input_tokens=1000,
            output_tokens=500,
            model=None,
        )

        usage = tracker.record("test-agent", output, elapsed=1.0)

        assert usage.cost_usd is None  # No model = no pricing

    def test_set_provider_pricing_prices_unknown_model(self) -> None:
        """Provider-supplied pricing lets record() price a model absent from the table."""
        tracker = UsageTracker()
        tracker.set_provider_pricing(
            "brand-new-model",
            ModelPricing(input_per_mtok=10.0, output_per_mtok=30.0),
        )
        output = AgentOutput(
            content={"r": "x"},
            raw_response="{}",
            tokens_used=2_000_000,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            model="brand-new-model",
        )
        usage = tracker.record("a", output, elapsed=1.0)
        # 1M input @ $10 + 1M output @ $30 = $40
        assert usage.cost_usd == pytest.approx(40.0)

    def test_provider_pricing_beats_default_table(self) -> None:
        """Provider pricing takes precedence over the static DEFAULT_PRICING (#265)."""
        tracker = UsageTracker()
        tracker.set_provider_pricing(
            "claude-sonnet-4",
            ModelPricing(input_per_mtok=1.0, output_per_mtok=1.0),
        )
        output = AgentOutput(
            content={"r": "x"},
            raw_response="{}",
            tokens_used=2_000_000,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            model="claude-sonnet-4",
        )
        usage = tracker.record("a", output, elapsed=1.0)
        # Provider rate $1+$1 = $2, not the table's $3/$15 => $18.
        assert usage.cost_usd == pytest.approx(2.0)

    def test_workflow_override_beats_provider_pricing(self) -> None:
        """Workflow cost.pricing override wins over the provider hook (#265)."""
        tracker = UsageTracker(
            pricing_overrides={"m": ModelPricing(input_per_mtok=2.0, output_per_mtok=2.0)}
        )
        tracker.set_provider_pricing("m", ModelPricing(input_per_mtok=99.0, output_per_mtok=99.0))
        output = AgentOutput(
            content={"r": "x"},
            raw_response="{}",
            tokens_used=2_000_000,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            model="m",
        )
        usage = tracker.record("a", output, elapsed=1.0)
        assert usage.cost_usd == pytest.approx(4.0)  # override 2+2

    def test_set_provider_pricing_none_is_noop(self) -> None:
        """A None result (provider supplies no pricing) leaves the model unpriced."""
        tracker = UsageTracker()
        tracker.set_provider_pricing("still-unknown", None)
        output = AgentOutput(
            content={"r": "x"},
            raw_response="{}",
            tokens_used=1500,
            input_tokens=1000,
            output_tokens=500,
            model="still-unknown",
        )
        usage = tracker.record("a", output, elapsed=1.0)
        assert usage.cost_usd is None

    def test_usage_tracker_multiple_agents(self) -> None:
        """Test tracking multiple agent executions."""
        tracker = UsageTracker()

        output1 = AgentOutput(
            content={"result": "test1"},
            raw_response="{}",
            input_tokens=1000,
            output_tokens=500,
            model="claude-sonnet-4",
        )

        output2 = AgentOutput(
            content={"result": "test2"},
            raw_response="{}",
            input_tokens=2000,
            output_tokens=1000,
            model="gpt-4o",
        )

        tracker.record("agent1", output1, elapsed=1.0)
        tracker.record("agent2", output2, elapsed=1.5)

        summary = tracker.get_summary()
        assert len(summary.agents) == 2
        assert summary.total_input_tokens == 3000
        assert summary.total_output_tokens == 1500

    def test_usage_tracker_with_pricing_overrides(self) -> None:
        """Test using custom pricing overrides."""
        custom_pricing = ModelPricing(
            input_per_mtok=100.0,  # Very high to see difference
            output_per_mtok=200.0,
        )

        tracker = UsageTracker(pricing_overrides={"custom-model": custom_pricing})

        output = AgentOutput(
            content={"result": "test"},
            raw_response="{}",
            input_tokens=1_000_000,  # 1M tokens
            output_tokens=1_000_000,
            model="custom-model",
        )

        usage = tracker.record("test-agent", output, elapsed=1.0)

        assert usage.cost_usd is not None
        assert usage.cost_usd == pytest.approx(300.0, rel=1e-6)  # $100 + $200

    def test_usage_tracker_get_summary(self) -> None:
        """Test getting summary returns a copy."""
        tracker = UsageTracker()

        output = AgentOutput(
            content={"result": "test"},
            raw_response="{}",
            input_tokens=1000,
            output_tokens=500,
            model="claude-sonnet-4",
        )

        tracker.record("agent1", output, elapsed=1.0)

        summary1 = tracker.get_summary()
        summary2 = tracker.get_summary()

        # Should be separate instances
        assert summary1.agents is not summary2.agents
        assert len(summary1.agents) == len(summary2.agents)

    def test_usage_tracker_reset(self) -> None:
        """Test resetting the tracker."""
        tracker = UsageTracker()

        output = AgentOutput(
            content={"result": "test"},
            raw_response="{}",
            input_tokens=1000,
            output_tokens=500,
            model="claude-sonnet-4",
        )

        tracker.record("agent1", output, elapsed=1.0)
        assert len(tracker.get_summary().agents) == 1

        tracker.reset()
        assert len(tracker.get_summary().agents) == 0

    def test_usage_tracker_merge(self) -> None:
        """Merging another workflow's usage rolls its per-agent records up."""
        parent = UsageTracker()
        child = UsageTracker()

        parent_output = AgentOutput(
            content={"result": "parent"},
            raw_response="{}",
            input_tokens=1000,
            output_tokens=500,
            model="claude-sonnet-4",
        )
        child_output = AgentOutput(
            content={"result": "child"},
            raw_response="{}",
            input_tokens=2000,
            output_tokens=1000,
            model="claude-sonnet-4",
        )

        parent.record("parent-agent", parent_output, elapsed=1.0)
        child.record("child-agent", child_output, elapsed=1.0)

        parent.merge(child.get_summary())

        summary = parent.get_summary()
        assert len(summary.agents) == 2
        names = {a.agent_name for a in summary.agents}
        assert names == {"parent-agent", "child-agent"}
        # Parent budget now accounts for the child's tokens too.
        assert summary.total_input_tokens == 3000
        assert summary.total_output_tokens == 1500

    def test_usage_tracker_merge_empty(self) -> None:
        """Merging an empty summary leaves the tracker unchanged."""
        tracker = UsageTracker()
        output = AgentOutput(
            content={"result": "test"},
            raw_response="{}",
            input_tokens=1000,
            output_tokens=500,
            model="claude-sonnet-4",
        )
        tracker.record("agent1", output, elapsed=1.0)

        tracker.merge(WorkflowUsage())

        assert len(tracker.get_summary().agents) == 1

        """Test budget check when under budget."""
        tracker = UsageTracker()

        output = AgentOutput(
            content={"result": "test"},
            raw_response="{}",
            input_tokens=1000,
            output_tokens=500,
            model="claude-sonnet-4",
        )

        tracker.record("agent1", output, elapsed=1.0)

        exceeded, total = tracker.check_budget(budget_usd=100.0)
        assert exceeded is False
        assert total < 100.0

    def test_usage_tracker_check_budget_exceeded(self) -> None:
        """Test budget check when over budget."""
        tracker = UsageTracker()

        output = AgentOutput(
            content={"result": "test"},
            raw_response="{}",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            model="claude-sonnet-4",
        )

        tracker.record("agent1", output, elapsed=1.0)

        # Should exceed $0.01 budget (actual cost around $18)
        exceeded, total = tracker.check_budget(budget_usd=0.01)
        assert exceeded is True
        assert total > 0.01


class TestUsageIntegration:
    """Integration tests for usage tracking."""

    def test_end_to_end_usage_tracking(self) -> None:
        """Test complete workflow usage tracking scenario."""
        tracker = UsageTracker()

        # Simulate a 3-agent workflow
        agents_data = [
            ("planner", "claude-sonnet-4", 5000, 2000),
            ("executor", "gpt-4o", 8000, 3000),
            ("reviewer", "claude-sonnet-4", 4000, 1000),
        ]

        for agent_name, model, input_tok, output_tok in agents_data:
            output = AgentOutput(
                content={"result": f"{agent_name} output"},
                raw_response="{}",
                input_tokens=input_tok,
                output_tokens=output_tok,
                model=model,
            )
            tracker.record(agent_name, output, elapsed=1.0)

        summary = tracker.get_summary()

        # Verify totals
        assert summary.total_input_tokens == 17000
        assert summary.total_output_tokens == 6000
        assert summary.total_tokens == 23000

        # Verify all costs are present
        assert summary.total_cost_usd is not None
        assert summary.total_cost_usd > 0

        # Verify individual agents
        assert len(summary.agents) == 3
        for agent in summary.agents:
            assert agent.cost_usd is not None
            assert agent.cost_usd > 0


def _billing_output(
    mode: object = None,
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    model: str | None = "claude-sonnet-4",
) -> AgentOutput:
    return AgentOutput(
        content={},
        raw_response="{}",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=model,
        billing_mode=mode,  # type: ignore[arg-type]
    )


class TestBillingProvenance:
    """``AgentUsage.billing_mode`` (per execution) and ``WorkflowUsage.billing`` (aggregate)."""

    def test_agent_usage_billing_mode_defaults_none(self) -> None:
        assert _mk_usage("a", "m", 0.1).billing_mode is None

    def test_record_copies_billing_mode(self) -> None:
        tracker = UsageTracker()
        row = tracker.record("a", _billing_output("subscription"), elapsed=1.0)
        assert row.billing_mode == "subscription"
        assert tracker.get_summary().agents[0].billing_mode == "subscription"

    def test_record_with_mock_output_gives_none(self) -> None:
        from unittest.mock import Mock

        row = UsageTracker().record("a", Mock(), elapsed=1.0)
        assert row.billing_mode is None

    def test_record_unrecognized_string_becomes_unknown(self) -> None:
        row = UsageTracker().record("a", _billing_output("bedrock"), elapsed=1.0)
        assert row.billing_mode == "unknown"

    def test_record_sandbox_row_has_no_billing_mode(self) -> None:
        assert UsageTracker().record_sandbox("a (sandbox)", 2.0).billing_mode is None

    def test_billing_excludes_zero_token_rows(self) -> None:
        tracker = UsageTracker()
        tracker.record("real", _billing_output("subscription"), elapsed=1.0)
        tracker.record(
            "empty",
            _billing_output("metered_api", input_tokens=0, output_tokens=0),
            elapsed=1.0,
        )
        assert dict(tracker.get_summary().billing.breakdown) == {"subscription": 1}

    def test_billing_excludes_sandbox_rows(self) -> None:
        tracker = UsageTracker()
        tracker.record("real", _billing_output("subscription"), elapsed=1.0)
        tracker.record_sandbox("real (sandbox)", 30.0)
        assert dict(tracker.get_summary().billing.breakdown) == {"subscription": 1}

    def test_billing_includes_unpriced_token_rows(self) -> None:
        tracker = UsageTracker()
        # No model: consumed tokens but unpriced (cost_usd is None).
        row = tracker.record("u", _billing_output("subscription", model=None), elapsed=1.0)
        assert row.cost_usd is None
        assert dict(tracker.get_summary().billing.breakdown) == {"subscription": 1}

    def test_billing_includes_validator_rows(self) -> None:
        tracker = UsageTracker()
        tracker.record("writer", _billing_output("subscription"), elapsed=1.0)
        tracker.record("writer (validator)", _billing_output("metered_api"), elapsed=1.0)
        billing = tracker.get_summary().billing
        assert dict(billing.breakdown) == {"subscription": 1, "metered_api": 1}
        assert billing.state == "mixed"

    def test_billing_counts_repeated_executions(self) -> None:
        tracker = UsageTracker()
        for _ in range(3):
            tracker.record("loop", _billing_output("subscription"), elapsed=1.0)
        # Counts are executions, not distinct agents.
        assert dict(tracker.get_summary().billing.breakdown) == {"subscription": 3}

    def test_billing_counts_unstated_providers(self) -> None:
        tracker = UsageTracker()
        tracker.record("claude", _billing_output("subscription"), elapsed=1.0)
        tracker.record("copilot", _billing_output(None), elapsed=1.0)
        billing = tracker.get_summary().billing
        assert dict(billing.breakdown) == {"subscription": 1, "unstated": 1}
        assert billing.state == "mixed"

    def test_legacy_only_usage_is_unstated_and_has_no_wire_form(self) -> None:
        tracker = UsageTracker()
        tracker.record("a", _billing_output(None), elapsed=1.0)
        billing = tracker.get_summary().billing
        assert billing.stated is False
        assert billing.to_wire() is None

    def test_empty_usage_has_empty_billing(self) -> None:
        billing = WorkflowUsage(agents=[]).billing
        assert dict(billing.breakdown) == {}
        assert billing.to_wire() is None

    def test_billing_after_merge_covers_child_rows(self) -> None:
        parent = UsageTracker()
        child = UsageTracker()
        parent.record("p", _billing_output("metered_api"), elapsed=1.0)
        child.record("c", _billing_output("subscription"), elapsed=1.0)
        parent.merge(child.get_summary())
        assert dict(parent.get_summary().billing.breakdown) == {
            "subscription": 1,
            "metered_api": 1,
        }
