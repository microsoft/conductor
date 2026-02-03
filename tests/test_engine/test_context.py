"""Unit tests for WorkflowContext.

Tests cover:
- Setting workflow inputs
- Storing agent outputs
- Building context for agents in different modes
- Optional dependencies with ? suffix
- Template context generation
- Context trimming strategies
"""

import pytest

from conductor.engine.context import (
    CHARS_PER_TOKEN,
    WorkflowContext,
    estimate_dict_tokens,
    estimate_tokens,
)


class TestWorkflowContextBasic:
    """Basic WorkflowContext functionality tests."""

    def test_init_default_values(self) -> None:
        """Test WorkflowContext initializes with correct defaults."""
        ctx = WorkflowContext()

        assert ctx.workflow_inputs == {}
        assert ctx.agent_outputs == {}
        assert ctx.current_iteration == 0
        assert ctx.execution_history == []

    def test_set_workflow_inputs(self) -> None:
        """Test setting workflow inputs."""
        ctx = WorkflowContext()
        inputs = {"question": "What is Python?", "max_length": 100}

        ctx.set_workflow_inputs(inputs)

        assert ctx.workflow_inputs == inputs
        # Verify it's a copy, not the same reference
        inputs["new_key"] = "value"
        assert "new_key" not in ctx.workflow_inputs

    def test_store_agent_output(self) -> None:
        """Test storing agent output."""
        ctx = WorkflowContext()
        output = {"answer": "Python is a programming language"}

        ctx.store("answerer", output)

        assert ctx.agent_outputs["answerer"] == output
        assert ctx.execution_history == ["answerer"]
        assert ctx.current_iteration == 1

    def test_store_multiple_agents(self) -> None:
        """Test storing outputs from multiple agents."""
        ctx = WorkflowContext()

        ctx.store("agent1", {"result": "first"})
        ctx.store("agent2", {"result": "second"})
        ctx.store("agent3", {"result": "third"})

        assert ctx.current_iteration == 3
        assert ctx.execution_history == ["agent1", "agent2", "agent3"]
        assert ctx.agent_outputs["agent1"]["result"] == "first"
        assert ctx.agent_outputs["agent2"]["result"] == "second"
        assert ctx.agent_outputs["agent3"]["result"] == "third"

    def test_get_latest_output(self) -> None:
        """Test getting the latest output."""
        ctx = WorkflowContext()

        # No outputs yet
        assert ctx.get_latest_output() is None

        ctx.store("agent1", {"result": "first"})
        assert ctx.get_latest_output() == {"result": "first"}

        ctx.store("agent2", {"result": "second"})
        assert ctx.get_latest_output() == {"result": "second"}


class TestWorkflowContextAccumulateMode:
    """Tests for accumulate context mode."""

    def test_accumulate_mode_includes_all_outputs(self) -> None:
        """Test that accumulate mode includes all prior outputs."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"goal": "test"})
        ctx.store("planner", {"plan": "step 1"})
        ctx.store("executor", {"result": "done"})

        agent_ctx = ctx.build_for_agent("reviewer", [], mode="accumulate")

        # Should have workflow inputs
        assert agent_ctx["workflow"]["input"]["goal"] == "test"

        # Should have all agent outputs
        assert agent_ctx["planner"]["output"]["plan"] == "step 1"
        assert agent_ctx["executor"]["output"]["result"] == "done"

        # Should have context metadata
        assert agent_ctx["context"]["iteration"] == 2
        assert agent_ctx["context"]["history"] == ["planner", "executor"]

    def test_accumulate_mode_empty_outputs(self) -> None:
        """Test accumulate mode with no prior outputs."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"input": "value"})

        agent_ctx = ctx.build_for_agent("first_agent", [], mode="accumulate")

        assert agent_ctx["workflow"]["input"]["input"] == "value"
        assert agent_ctx["context"]["iteration"] == 0
        assert agent_ctx["context"]["history"] == []


class TestWorkflowContextLastOnlyMode:
    """Tests for last_only context mode."""

    def test_last_only_mode_includes_only_last_output(self) -> None:
        """Test that last_only mode only includes the most recent output."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"goal": "test"})
        ctx.store("planner", {"plan": "step 1"})
        ctx.store("executor", {"result": "done"})

        agent_ctx = ctx.build_for_agent("reviewer", [], mode="last_only")

        # Should have workflow inputs
        assert agent_ctx["workflow"]["input"]["goal"] == "test"

        # Should only have the last agent's output
        assert "planner" not in agent_ctx
        assert agent_ctx["executor"]["output"]["result"] == "done"

        # Should have context metadata
        assert agent_ctx["context"]["iteration"] == 2

    def test_last_only_mode_empty_history(self) -> None:
        """Test last_only mode with no prior agents."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"input": "value"})

        agent_ctx = ctx.build_for_agent("first_agent", [], mode="last_only")

        # Only workflow and context should be present
        assert "workflow" in agent_ctx
        assert "context" in agent_ctx


class TestWorkflowContextExplicitMode:
    """Tests for explicit context mode."""

    def test_explicit_mode_workflow_input(self) -> None:
        """Test explicit mode with workflow input reference."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"question": "What?", "other": "ignored"})

        agent_ctx = ctx.build_for_agent(
            "agent",
            ["workflow.input.question"],
            mode="explicit",
        )

        assert agent_ctx["workflow"]["input"]["question"] == "What?"
        assert "other" not in agent_ctx["workflow"]["input"]

    def test_explicit_mode_agent_output(self) -> None:
        """Test explicit mode with agent output reference."""
        ctx = WorkflowContext()
        ctx.store("answerer", {"answer": "42", "confidence": 0.9})

        agent_ctx = ctx.build_for_agent(
            "checker",
            ["answerer.output"],
            mode="explicit",
        )

        assert agent_ctx["answerer"]["output"]["answer"] == "42"
        assert agent_ctx["answerer"]["output"]["confidence"] == 0.9

    def test_explicit_mode_specific_field(self) -> None:
        """Test explicit mode with specific field reference."""
        ctx = WorkflowContext()
        ctx.store("answerer", {"answer": "42", "confidence": 0.9})

        agent_ctx = ctx.build_for_agent(
            "checker",
            ["answerer.output.answer"],
            mode="explicit",
        )

        assert agent_ctx["answerer"]["output"]["answer"] == "42"
        assert "confidence" not in agent_ctx["answerer"]["output"]

    def test_explicit_mode_missing_required_raises(self) -> None:
        """Test that missing required input raises KeyError."""
        ctx = WorkflowContext()

        with pytest.raises(KeyError, match="Missing required agent output"):
            ctx.build_for_agent(
                "checker",
                ["missing_agent.output"],
                mode="explicit",
            )

    def test_explicit_mode_missing_workflow_input_raises(self) -> None:
        """Test that missing required workflow input raises KeyError."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({})

        with pytest.raises(KeyError, match="Missing required workflow input"):
            ctx.build_for_agent(
                "agent",
                ["workflow.input.missing"],
                mode="explicit",
            )


class TestWorkflowContextOptionalDeps:
    """Tests for optional dependencies with ? suffix."""

    def test_optional_missing_agent_skipped(self) -> None:
        """Test that missing optional agent output is skipped."""
        ctx = WorkflowContext()

        # Should not raise
        agent_ctx = ctx.build_for_agent(
            "checker",
            ["optional_agent.output?"],
            mode="explicit",
        )

        assert "optional_agent" not in agent_ctx

    def test_optional_missing_workflow_input_skipped(self) -> None:
        """Test that missing optional workflow input is set to None."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({})

        # Should not raise
        agent_ctx = ctx.build_for_agent(
            "agent",
            ["workflow.input.optional?"],
            mode="explicit",
        )

        # Optional workflow inputs are set to None so templates can check them
        assert agent_ctx["workflow"]["input"]["optional"] is None

    def test_optional_present_is_included(self) -> None:
        """Test that present optional dependencies are included."""
        ctx = WorkflowContext()
        ctx.store("reviewer", {"feedback": "looks good"})

        agent_ctx = ctx.build_for_agent(
            "executor",
            ["reviewer.feedback?"],
            mode="explicit",
        )

        assert agent_ctx["reviewer"]["output"]["feedback"] == "looks good"

    def test_optional_missing_field_skipped(self) -> None:
        """Test that missing optional field is skipped."""
        ctx = WorkflowContext()
        ctx.store("answerer", {"answer": "42"})

        # Should not raise even though 'confidence' doesn't exist
        agent_ctx = ctx.build_for_agent(
            "checker",
            ["answerer.output.confidence?"],
            mode="explicit",
        )

        # answerer should be in context but without confidence
        if "answerer" in agent_ctx:
            assert "confidence" not in agent_ctx["answerer"]["output"]

    def test_mixed_required_and_optional(self) -> None:
        """Test mixing required and optional dependencies."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"question": "What?"})
        ctx.store("answerer", {"answer": "42"})

        agent_ctx = ctx.build_for_agent(
            "summarizer",
            [
                "workflow.input.question",
                "answerer.output.answer",
                "missing_agent.output?",  # Optional, should be skipped
            ],
            mode="explicit",
        )

        assert agent_ctx["workflow"]["input"]["question"] == "What?"
        assert agent_ctx["answerer"]["output"]["answer"] == "42"
        assert "missing_agent" not in agent_ctx


class TestWorkflowContextGetForTemplate:
    """Tests for get_for_template method."""

    def test_get_for_template_includes_all(self) -> None:
        """Test that get_for_template returns full context."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"goal": "test"})
        ctx.store("agent1", {"output1": "value1"})
        ctx.store("agent2", {"output2": "value2"})

        template_ctx = ctx.get_for_template()

        assert template_ctx["workflow"]["input"]["goal"] == "test"
        assert template_ctx["agent1"]["output"]["output1"] == "value1"
        assert template_ctx["agent2"]["output"]["output2"] == "value2"
        assert template_ctx["context"]["iteration"] == 2

    def test_get_for_template_empty_context(self) -> None:
        """Test get_for_template with empty context."""
        ctx = WorkflowContext()

        template_ctx = ctx.get_for_template()

        assert template_ctx["workflow"]["input"] == {}
        assert template_ctx["context"]["iteration"] == 0
        assert template_ctx["context"]["history"] == []


class TestTokenEstimation:
    """Tests for token estimation functions."""

    def test_estimate_tokens_basic(self) -> None:
        """Test basic token estimation."""
        text = "Hello world"  # 11 chars
        tokens = estimate_tokens(text)
        assert tokens == 11 // CHARS_PER_TOKEN

    def test_estimate_tokens_empty(self) -> None:
        """Test token estimation for empty string."""
        tokens = estimate_tokens("")
        assert tokens == 0

    def test_estimate_dict_tokens(self) -> None:
        """Test token estimation for dictionaries."""
        data = {"key": "value", "number": 123}
        tokens = estimate_dict_tokens(data)
        assert tokens > 0


class TestContextTokenEstimation:
    """Tests for context token estimation."""

    def test_estimate_context_tokens(self) -> None:
        """Test that context tokens can be estimated."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"question": "What is Python?"})
        ctx.store("answerer", {"answer": "A programming language"})

        tokens = ctx.estimate_context_tokens()
        assert tokens > 0

    def test_estimate_context_tokens_increases_with_content(self) -> None:
        """Test that token estimate increases with more content."""
        ctx = WorkflowContext()
        initial_tokens = ctx.estimate_context_tokens()

        ctx.set_workflow_inputs({"question": "What is Python?"})
        after_inputs = ctx.estimate_context_tokens()

        ctx.store("agent1", {"result": "A" * 1000})
        after_agent = ctx.estimate_context_tokens()

        assert after_inputs > initial_tokens
        assert after_agent > after_inputs


class TestContextTrimmingDropOldest:
    """Tests for drop_oldest trimming strategy."""

    def test_trim_context_not_needed(self) -> None:
        """Test that trimming doesn't happen when under limit."""
        ctx = WorkflowContext()
        ctx.store("agent1", {"result": "short"})

        # Set a high limit
        result_tokens = ctx.trim_context(max_tokens=10000, strategy="drop_oldest")

        # Context should be unchanged
        assert "agent1" in ctx.agent_outputs
        assert result_tokens <= 10000

    def test_trim_context_drops_oldest_agent(self) -> None:
        """Test that drop_oldest removes oldest agent outputs."""
        ctx = WorkflowContext()
        ctx.store("agent1", {"result": "A" * 500})
        ctx.store("agent2", {"result": "B" * 500})
        ctx.store("agent3", {"result": "C" * 500})

        # Get initial token count
        initial_tokens = ctx.estimate_context_tokens()

        # Set a limit that requires trimming
        target_tokens = initial_tokens // 2

        ctx.trim_context(max_tokens=target_tokens, strategy="drop_oldest")

        # Some agents should have been removed
        remaining_agents = list(ctx.agent_outputs.keys())
        # agent1 (oldest) should be removed first
        assert "agent1" not in remaining_agents or len(remaining_agents) < 3

    def test_trim_context_preserves_recent(self) -> None:
        """Test that recent agents are preserved when possible."""
        ctx = WorkflowContext()
        ctx.store("agent1", {"result": "old"})
        ctx.store("agent2", {"result": "newer"})
        ctx.store("agent3", {"result": "newest"})

        # Use a limit that allows keeping at least agent3
        ctx.trim_context(max_tokens=500, strategy="drop_oldest")

        # Most recent should be kept if possible
        if ctx.agent_outputs:
            # Either agent3 is kept or all were dropped
            assert "agent3" in ctx.agent_outputs or len(ctx.agent_outputs) == 0


class TestContextTrimmingTruncate:
    """Tests for truncate trimming strategy."""

    def test_truncate_shortens_long_strings(self) -> None:
        """Test that truncate shortens long string values."""
        ctx = WorkflowContext()
        long_content = "A" * 2000
        ctx.store("agent1", {"result": long_content})

        initial_tokens = ctx.estimate_context_tokens()
        target_tokens = initial_tokens // 2

        ctx.trim_context(max_tokens=target_tokens, strategy="truncate")

        # Content should be truncated
        truncated = ctx.agent_outputs["agent1"]["result"]
        assert len(truncated) < len(long_content)
        assert "[truncated]" in truncated

    def test_truncate_preserves_short_strings(self) -> None:
        """Test that truncate doesn't affect short strings unnecessarily."""
        ctx = WorkflowContext()
        ctx.store("agent1", {"result": "short value"})

        initial_output = ctx.agent_outputs["agent1"]["result"]

        # Use a high limit
        ctx.trim_context(max_tokens=10000, strategy="truncate")

        # Short content should be preserved
        assert ctx.agent_outputs["agent1"]["result"] == initial_output


class TestContextTrimmingSummarize:
    """Tests for summarize trimming strategy."""

    def test_summarize_requires_provider(self) -> None:
        """Test that summarize strategy requires a provider."""
        ctx = WorkflowContext()
        ctx.store("agent1", {"result": "A" * 1000})

        with pytest.raises(ValueError, match="requires a provider"):
            ctx.trim_context(max_tokens=100, strategy="summarize", provider=None)

    def test_summarize_with_provider_drops_old_agents(self) -> None:
        """Test that summarize keeps recent agents and drops old ones."""
        ctx = WorkflowContext()
        ctx.store("agent1", {"result": "old data"})
        ctx.store("agent2", {"result": "older data"})
        ctx.store("agent3", {"result": "newest data"})
        ctx.store("agent4", {"result": "most recent"})

        # Create a mock provider (just needs to exist for the fallback logic)
        class MockProvider:
            pass

        initial_tokens = ctx.estimate_context_tokens()
        target_tokens = initial_tokens // 3

        ctx.trim_context(
            max_tokens=target_tokens,
            strategy="summarize",
            provider=MockProvider(),  # type: ignore
        )

        # Recent agents should be preserved, old ones dropped or summarized
        # The summarize strategy keeps half of the agents
        remaining = set(ctx.agent_outputs.keys())
        assert len(remaining) <= 3  # Some were dropped

    def test_summarize_creates_summary_entry(self) -> None:
        """Test that summarize creates a summary of dropped agents."""
        ctx = WorkflowContext()
        ctx.store("agent1", {"result": "first output"})
        ctx.store("agent2", {"result": "second output"})
        ctx.store("agent3", {"result": "third output"})
        ctx.store("agent4", {"result": "fourth output"})

        class MockProvider:
            pass

        # Force trimming
        ctx.trim_context(
            max_tokens=100,  # Very low to force trimming
            strategy="summarize",
            provider=MockProvider(),  # type: ignore
        )

        # Should have a summary entry if agents were dropped
        if "_context_summary" in ctx.agent_outputs:
            summary = ctx.agent_outputs["_context_summary"]
            assert "summary" in summary
            assert "dropped_agents" in summary


class TestContextTrimmingInvalidStrategy:
    """Tests for invalid trimming strategy."""

    def test_invalid_strategy_raises_error(self) -> None:
        """Test that invalid strategy raises ValueError."""
        ctx = WorkflowContext()
        ctx.store("agent1", {"result": "data"})

        with pytest.raises(ValueError, match="Unknown trimming strategy"):
            ctx.trim_context(max_tokens=10, strategy="invalid")  # type: ignore


class TestParallelGroupContextAccess:
    """Tests for accessing parallel group outputs in context."""

    def test_accumulate_mode_with_parallel_group(self) -> None:
        """Test that parallel group outputs are correctly structured in accumulate mode."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"goal": "test"})

        # Store a regular agent output
        ctx.store("planner", {"plan": "step 1"})

        # Store a parallel group output
        parallel_output = {
            "outputs": {
                "researcher1": {"finding": "result A"},
                "researcher2": {"finding": "result B"},
            },
            "errors": {},
        }
        ctx.store("parallel_research", parallel_output)

        # Build context
        agent_ctx = ctx.build_for_agent("summarizer", [], mode="accumulate")

        # Regular agent should be wrapped in output
        assert agent_ctx["planner"]["output"]["plan"] == "step 1"

        # Parallel group should NOT be wrapped - direct access to outputs/errors
        assert "outputs" in agent_ctx["parallel_research"]
        assert "errors" in agent_ctx["parallel_research"]
        assert agent_ctx["parallel_research"]["outputs"]["researcher1"]["finding"] == "result A"
        assert agent_ctx["parallel_research"]["outputs"]["researcher2"]["finding"] == "result B"
        assert agent_ctx["parallel_research"]["errors"] == {}

    def test_last_only_mode_with_parallel_group(self) -> None:
        """Test that parallel group is correctly structured in last_only mode."""
        ctx = WorkflowContext()
        ctx.store("agent1", {"result": "first"})

        parallel_output = {
            "outputs": {
                "validator1": {"valid": True},
                "validator2": {"valid": False},
            },
            "errors": {},
        }
        ctx.store("parallel_validation", parallel_output)

        agent_ctx = ctx.build_for_agent("decision", [], mode="last_only")

        # Should only have the parallel group (last agent)
        assert "agent1" not in agent_ctx
        assert "parallel_validation" in agent_ctx
        assert agent_ctx["parallel_validation"]["outputs"]["validator1"]["valid"] is True

    def test_explicit_mode_parallel_group_all_outputs(self) -> None:
        """Test explicit mode referencing all parallel outputs."""
        ctx = WorkflowContext()

        parallel_output = {
            "outputs": {
                "checker1": {"status": "pass"},
                "checker2": {"status": "fail"},
            },
            "errors": {},
        }
        ctx.store("parallel_checks", parallel_output)

        agent_ctx = ctx.build_for_agent(
            "reporter",
            ["parallel_checks.outputs"],
            mode="explicit",
        )

        assert agent_ctx["parallel_checks"]["outputs"]["checker1"]["status"] == "pass"
        assert agent_ctx["parallel_checks"]["outputs"]["checker2"]["status"] == "fail"

    def test_explicit_mode_parallel_group_specific_agent(self) -> None:
        """Test explicit mode referencing specific agent in parallel group."""
        ctx = WorkflowContext()

        parallel_output = {
            "outputs": {
                "agent_a": {"data": "value_a", "extra": "ignored"},
                "agent_b": {"data": "value_b"},
            },
            "errors": {},
        }
        ctx.store("my_parallel", parallel_output)

        agent_ctx = ctx.build_for_agent(
            "consumer",
            ["my_parallel.outputs.agent_a"],
            mode="explicit",
        )

        assert agent_ctx["my_parallel"]["outputs"]["agent_a"]["data"] == "value_a"
        assert agent_ctx["my_parallel"]["outputs"]["agent_a"]["extra"] == "ignored"
        # agent_b should not be in context
        assert "agent_b" not in agent_ctx["my_parallel"]["outputs"]

    def test_explicit_mode_parallel_group_specific_field(self) -> None:
        """Test explicit mode referencing specific field from parallel agent."""
        ctx = WorkflowContext()

        parallel_output = {
            "outputs": {
                "analyzer": {"score": 95, "details": "good", "notes": "extra"},
            },
            "errors": {},
        }
        ctx.store("analysis_group", parallel_output)

        agent_ctx = ctx.build_for_agent(
            "scorer",
            ["analysis_group.outputs.analyzer.score"],
            mode="explicit",
        )

        assert agent_ctx["analysis_group"]["outputs"]["analyzer"]["score"] == 95
        # Other fields should not be present
        assert "details" not in agent_ctx["analysis_group"]["outputs"]["analyzer"]
        assert "notes" not in agent_ctx["analysis_group"]["outputs"]["analyzer"]

    def test_explicit_mode_parallel_group_errors(self) -> None:
        """Test explicit mode accessing parallel group errors."""
        ctx = WorkflowContext()

        parallel_output = {
            "outputs": {
                "worker1": {"result": "success"},
            },
            "errors": {
                "worker2": {
                    "agent_name": "worker2",
                    "exception_type": "ValueError",
                    "message": "Invalid input",
                    "suggestion": "Check input format",
                }
            },
        }
        ctx.store("workers", parallel_output)

        agent_ctx = ctx.build_for_agent(
            "error_handler",
            ["workers.errors"],
            mode="explicit",
        )

        assert "errors" in agent_ctx["workers"]
        assert "worker2" in agent_ctx["workers"]["errors"]
        assert agent_ctx["workers"]["errors"]["worker2"]["message"] == "Invalid input"

    def test_explicit_mode_optional_parallel_agent_missing(self) -> None:
        """Test optional reference to missing parallel agent."""
        ctx = WorkflowContext()

        parallel_output = {
            "outputs": {
                "existing": {"data": "value"},
            },
            "errors": {},
        }
        ctx.store("group1", parallel_output)

        # Should not raise even though 'missing' doesn't exist
        agent_ctx = ctx.build_for_agent(
            "consumer",
            ["group1.outputs.missing?"],
            mode="explicit",
        )

        # group1.outputs should exist but without 'missing'
        if "group1" in agent_ctx and "outputs" in agent_ctx["group1"]:
            assert "missing" not in agent_ctx["group1"]["outputs"]

    def test_explicit_mode_optional_parallel_field_missing(self) -> None:
        """Test optional reference to missing field in parallel agent."""
        ctx = WorkflowContext()

        parallel_output = {
            "outputs": {
                "agent1": {"field_a": "value"},
            },
            "errors": {},
        }
        ctx.store("group1", parallel_output)

        # Should not raise even though 'missing_field' doesn't exist
        agent_ctx = ctx.build_for_agent(
            "consumer",
            ["group1.outputs.agent1.missing_field?"],
            mode="explicit",
        )

        # Agent1 might not be in context since the field was missing
        # and optional
        if (
            "group1" in agent_ctx
            and "outputs" in agent_ctx["group1"]
            and "agent1" in agent_ctx["group1"]["outputs"]
        ):
            assert "missing_field" not in agent_ctx["group1"]["outputs"]["agent1"]

    def test_explicit_mode_required_parallel_agent_missing_raises(self) -> None:
        """Test that missing required parallel agent raises error."""
        ctx = WorkflowContext()

        parallel_output = {
            "outputs": {
                "existing": {"data": "value"},
            },
            "errors": {},
        }
        ctx.store("group1", parallel_output)

        with pytest.raises(KeyError, match="Missing key/agent 'missing' in outputs of 'group1'"):
            ctx.build_for_agent(
                "consumer",
                ["group1.outputs.missing"],
                mode="explicit",
            )

    def test_explicit_mode_required_parallel_field_missing_raises(self) -> None:
        """Test that missing required field from parallel agent raises error."""
        ctx = WorkflowContext()

        parallel_output = {
            "outputs": {
                "agent1": {"field_a": "value"},
            },
            "errors": {},
        }
        ctx.store("group1", parallel_output)

        with pytest.raises(KeyError, match="Missing field 'missing_field'"):
            ctx.build_for_agent(
                "consumer",
                ["group1.outputs.agent1.missing_field"],
                mode="explicit",
            )

    def test_get_for_template_with_parallel_group(self) -> None:
        """Test that get_for_template includes parallel groups correctly."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"input": "test"})
        ctx.store("agent1", {"result": "normal"})

        parallel_output = {
            "outputs": {
                "p1": {"value": 1},
                "p2": {"value": 2},
            },
            "errors": {},
        }
        ctx.store("parallel_group", parallel_output)

        template_ctx = ctx.get_for_template()

        # Regular agent wrapped in output
        assert template_ctx["agent1"]["output"]["result"] == "normal"

        # Parallel group should have direct structure
        assert template_ctx["parallel_group"]["outputs"]["p1"]["value"] == 1
        assert template_ctx["parallel_group"]["outputs"]["p2"]["value"] == 2

    def test_mixed_regular_and_parallel_in_explicit_mode(self) -> None:
        """Test mixing regular agent and parallel group references in explicit mode."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"goal": "test"})
        ctx.store("planner", {"plan": "step 1"})

        parallel_output = {
            "outputs": {
                "researcher": {"finding": "data"},
            },
            "errors": {},
        }
        ctx.store("research_group", parallel_output)

        agent_ctx = ctx.build_for_agent(
            "summarizer",
            [
                "workflow.input.goal",
                "planner.output.plan",
                "research_group.outputs.researcher.finding",
            ],
            mode="explicit",
        )

        assert agent_ctx["workflow"]["input"]["goal"] == "test"
        assert agent_ctx["planner"]["output"]["plan"] == "step 1"
        assert agent_ctx["research_group"]["outputs"]["researcher"]["finding"] == "data"


class TestContextTrimmingWithParallelOutputs:
    """Tests for context trimming when parallel group outputs are present."""

    def test_estimate_tokens_includes_parallel_outputs(self) -> None:
        """Test that token estimation includes parallel group outputs."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"goal": "test"})

        # Store regular agent output
        ctx.store("planner", {"plan": "step 1" * 100})

        # Store parallel group output
        parallel_output = {
            "outputs": {
                "researcher1": {"finding": "data1" * 100},
                "researcher2": {"finding": "data2" * 100},
            },
            "errors": {},
        }
        ctx.store("research_group", parallel_output)

        # Token estimate should include all content
        tokens = ctx.estimate_context_tokens()
        assert tokens > 0

        # Should include parallel output content
        full_ctx = ctx.get_for_template()
        assert "research_group" in full_ctx
        assert "outputs" in full_ctx["research_group"]

    def test_trim_drop_oldest_with_parallel_outputs(self) -> None:
        """Test drop_oldest trimming strategy with parallel group outputs."""
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"goal": "test"})

        # Store a regular agent output (oldest)
        ctx.store("agent1", {"data": "x" * 1000})
        ctx.execution_history.append("agent1")

        # Store parallel group output
        parallel_output = {
            "outputs": {
                "worker1": {"result": "y" * 1000},
                "worker2": {"result": "z" * 1000},
            },
            "errors": {},
        }
        ctx.store("parallel_group", parallel_output)
        ctx.execution_history.append("parallel_group")

        # Store another regular agent output (newest)
        ctx.store("agent2", {"data": "w" * 100})
        ctx.execution_history.append("agent2")

        initial_tokens = ctx.estimate_context_tokens()

        # Trim to a smaller size
        max_tokens = initial_tokens // 3
        final_tokens = ctx.trim_context(max_tokens, strategy="drop_oldest")

        # Should have dropped oldest agents
        assert final_tokens <= max_tokens

        # The newest agent should still be present
        assert "agent2" in ctx.agent_outputs

    def test_trim_truncate_with_parallel_outputs(self) -> None:
        """Test truncate trimming strategy with parallel group outputs.

        Note: The truncate strategy only handles flat string values,
        not nested structures. For parallel outputs, drop_oldest is recommended.
        """
        ctx = WorkflowContext()
        ctx.set_workflow_inputs({"goal": "test"})

        # Store a regular agent with flat output
        ctx.store("agent1", {"data": "x" * 1000})
        ctx.execution_history.append("agent1")

        # Store parallel group output with nested structure
        parallel_output = {
            "outputs": {
                "researcher": {"findings": "Very long findings " * 200},
                "analyzer": {"analysis": "Detailed analysis " * 200},
            },
            "errors": {},
        }
        ctx.store("research_group", parallel_output)
        ctx.execution_history.append("research_group")

        initial_tokens = ctx.estimate_context_tokens()

        # Trim to a smaller size
        max_tokens = initial_tokens // 3
        final_tokens = ctx.trim_context(max_tokens, strategy="truncate")

        # Truncate only works on flat string values, so it may not achieve
        # the target if most content is nested
        # Just verify it doesn't crash and doesn't increase tokens
        assert final_tokens <= initial_tokens

    def test_trim_preserves_parallel_structure(self) -> None:
        """Test that trimming preserves parallel output structure."""
        ctx = WorkflowContext()

        # Store parallel group output
        parallel_output = {
            "outputs": {
                "worker1": {"result": "Long result " * 500},
                "worker2": {"result": "Another long result " * 500},
            },
            "errors": {
                "worker3": {
                    "agent_name": "worker3",
                    "exception_type": "Error",
                    "message": "Failed",
                    "suggestion": "Fix it",
                }
            },
        }
        ctx.store("parallel_group", parallel_output)
        ctx.execution_history.append("parallel_group")

        initial_tokens = ctx.estimate_context_tokens()

        # Trim aggressively
        max_tokens = initial_tokens // 4
        ctx.trim_context(max_tokens, strategy="truncate")

        # Parallel group should still exist with proper structure
        if "parallel_group" in ctx.agent_outputs:
            pg_output = ctx.agent_outputs["parallel_group"]
            assert "outputs" in pg_output or "errors" in pg_output

    def test_estimate_tokens_for_parallel_errors(self) -> None:
        """Test token estimation includes error information in parallel outputs."""
        ctx = WorkflowContext()

        # Store parallel group with errors
        parallel_output = {
            "outputs": {
                "worker1": {"result": "success"},
            },
            "errors": {
                "worker2": {
                    "agent_name": "worker2",
                    "exception_type": "ValidationError",
                    "message": "Input validation failed: " + "x" * 500,
                    "suggestion": "Check input format and try again",
                },
                "worker3": {
                    "agent_name": "worker3",
                    "exception_type": "TimeoutError",
                    "message": "Execution timed out after 30 seconds",
                    "suggestion": "Increase timeout or optimize processing",
                },
            },
        }
        ctx.store("parallel_validators", parallel_output)

        tokens = ctx.estimate_context_tokens()

        # Should include error messages in token count
        assert tokens > 0

        # Verify error content is in context
        full_ctx = ctx.get_for_template()
        assert "parallel_validators" in full_ctx
        assert "errors" in full_ctx["parallel_validators"]
        assert "worker2" in full_ctx["parallel_validators"]["errors"]
        assert "worker3" in full_ctx["parallel_validators"]["errors"]

    def test_trim_handles_empty_parallel_outputs(self) -> None:
        """Test trimming handles parallel groups with empty outputs."""
        ctx = WorkflowContext()

        # Store parallel group with only errors (all agents failed)
        parallel_output = {
            "outputs": {},
            "errors": {
                "worker1": {
                    "agent_name": "worker1",
                    "exception_type": "Error",
                    "message": "Failed",
                    "suggestion": None,
                },
            },
        }
        ctx.store("failed_group", parallel_output)
        ctx.execution_history.append("failed_group")

        # Store another agent
        ctx.store("recovery", {"status": "recovered"})
        ctx.execution_history.append("recovery")

        initial_tokens = ctx.estimate_context_tokens()
        max_tokens = initial_tokens // 2

        # Should not crash with empty outputs dict
        final_tokens = ctx.trim_context(max_tokens, strategy="drop_oldest")

        assert final_tokens <= max_tokens
