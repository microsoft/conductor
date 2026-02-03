"""Unit tests for the usage tracking module."""

import pytest

from conductor.engine.pricing import ModelPricing
from conductor.engine.usage import AgentUsage, UsageTracker, WorkflowUsage
from conductor.providers.base import AgentOutput


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
        assert usage.cost_usd > 0

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

    def test_usage_tracker_check_budget_not_exceeded(self) -> None:
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
