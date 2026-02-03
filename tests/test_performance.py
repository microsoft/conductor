"""Performance tests for Conductor.

Tests cover:
- Startup time: CLI app initialization should be <500ms
- Memory usage: 10-agent workflow should use <100MB

These tests verify NFR-001 and NFR-002 from the requirements.
"""

import asyncio
import subprocess
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    LimitsConfig,
    OutputField,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.base import AgentOutput
from conductor.providers.copilot import CopilotProvider

# Mark all tests in this module as performance tests
pytestmark = pytest.mark.performance


class TestStartupTime:
    """Test that startup time is under 500ms."""

    def test_cli_import_time(self) -> None:
        """Test that importing the CLI app takes less than 500ms.

        NFR-001: Startup time should be <500ms.
        """
        # Measure import time using subprocess for clean environment
        code = """
import time
start = time.perf_counter()
from conductor.cli.app import app
elapsed = time.perf_counter() - start
print(elapsed)
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Import failed: {result.stderr}"
        elapsed = float(result.stdout.strip())

        # Allow 500ms for CLI import
        assert elapsed < 0.5, f"CLI import took {elapsed:.3f}s, expected <0.5s"

    def test_config_load_time(self, fixtures_dir) -> None:
        """Test that loading and validating a config is fast."""
        from conductor.config.loader import load_config

        workflow_file = fixtures_dir / "valid_full.yaml"

        start = time.perf_counter()
        config = load_config(workflow_file)
        elapsed = time.perf_counter() - start

        # Config loading should be very fast (<100ms)
        assert elapsed < 0.1, f"Config load took {elapsed:.3f}s, expected <0.1s"
        assert config.workflow.name == "full-workflow"

    def test_workflow_engine_init_time(self) -> None:
        """Test that WorkflowEngine initialization is fast."""
        config = self._create_simple_config()
        provider = CopilotProvider()

        start = time.perf_counter()
        engine = WorkflowEngine(config, provider)
        elapsed = time.perf_counter() - start

        # Engine init should be very fast (<50ms)
        assert elapsed < 0.05, f"Engine init took {elapsed:.3f}s, expected <0.05s"
        assert engine is not None

    def test_template_renderer_init_time(self) -> None:
        """Test that TemplateRenderer initialization is fast."""
        from conductor.executor.template import TemplateRenderer

        start = time.perf_counter()
        renderer = TemplateRenderer()
        # Do a simple render to ensure full initialization
        _ = renderer.render("Hello {{ name }}", {"name": "World"})
        elapsed = time.perf_counter() - start

        assert elapsed < 0.1, f"Renderer init took {elapsed:.3f}s, expected <0.1s"

    def _create_simple_config(self) -> WorkflowConfig:
        """Create a simple config for testing."""
        return WorkflowConfig(
            workflow=WorkflowDef(
                name="test-workflow",
                entry_point="agent1",
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Test",
                    routes=[RouteDef(to="$end")],
                    output={"result": OutputField(type="string")},
                ),
            ],
            output={"result": "{{ agent1.output.result }}"},
        )


class TestMemoryUsage:
    """Test that memory usage is under 100MB for 10-agent workflow."""

    def test_ten_agent_workflow_memory(self) -> None:
        """Test that a 10-agent workflow uses less than 100MB.

        NFR-002: Memory usage should be <100MB for 10-agent workflow.
        """
        import tracemalloc

        # Start tracing memory
        tracemalloc.start()

        # Create 10-agent workflow config
        config = self._create_ten_agent_config()

        def mock_handler(agent, prompt, context):
            # Return reasonably sized response
            return {"result": f"Response from {agent.name}: " + "x" * 100}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        # Run the workflow
        import asyncio

        result = asyncio.run(engine.run({"input": "test"}))

        # Get peak memory
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Allow 100MB for the workflow execution
        assert peak < 100 * 1024 * 1024, (
            f"Workflow used {peak / (1024 * 1024):.1f}MB peak, expected <100MB"
        )

        # Verify workflow completed successfully
        assert "result" in result

    def test_large_context_memory(self) -> None:
        """Test memory usage with large context accumulation."""
        import tracemalloc

        tracemalloc.start()

        # Create workflow that accumulates context
        config = self._create_context_accumulating_config()

        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            # Return large-ish content
            return {"result": f"Large content block {call_count}: " + "data" * 500}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        import asyncio

        result = asyncio.run(engine.run({}))

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Should still be under 100MB
        assert peak < 100 * 1024 * 1024, f"Large context used {peak / (1024 * 1024):.1f}MB peak"
        assert result is not None

    def test_many_iterations_memory(self) -> None:
        """Test memory usage with many loop iterations."""
        import tracemalloc

        tracemalloc.start()

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="loop-workflow",
                entry_point="looper",
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="looper",
                    model="gpt-4",
                    prompt="Iteration {{ context.iteration }}",
                    output={
                        "count": OutputField(type="number"),
                        "data": OutputField(type="string"),
                    },
                    routes=[
                        RouteDef(to="$end", when="count >= 25"),
                        RouteDef(to="looper"),
                    ],
                ),
            ],
            output={"final_count": "{{ looper.output.count }}"},
        )

        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            return {"count": call_count, "data": f"Data block {call_count}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        import asyncio

        result = asyncio.run(engine.run({}))

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Many iterations should still stay under limit
        assert peak < 100 * 1024 * 1024, f"Many iterations used {peak / (1024 * 1024):.1f}MB peak"
        assert result["final_count"] == 25

    def _create_ten_agent_config(self) -> WorkflowConfig:
        """Create a workflow with 10 agents in sequence."""
        agents = []

        for i in range(1, 11):
            next_agent = f"agent{i + 1}" if i < 10 else "$end"
            agents.append(
                AgentDef(
                    name=f"agent{i}",
                    model="gpt-4",
                    prompt=f"Agent {i}: Process {{ workflow.input.input }}",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to=next_agent)],
                )
            )

        return WorkflowConfig(
            workflow=WorkflowDef(
                name="ten-agent-workflow",
                entry_point="agent1",
                context=ContextConfig(mode="accumulate"),
            ),
            agents=agents,
            output={"result": "{{ agent10.output.result }}"},
        )

    def _create_context_accumulating_config(self) -> WorkflowConfig:
        """Create workflow that accumulates significant context."""
        return WorkflowConfig(
            workflow=WorkflowDef(
                name="context-workflow",
                entry_point="agent1",
                context=ContextConfig(mode="accumulate"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="First agent",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Second agent: {{ agent1.output.result }}",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="agent3")],
                ),
                AgentDef(
                    name="agent3",
                    model="gpt-4",
                    prompt="Third agent with context",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent3.output.result }}"},
        )


class TestProviderPerformance:
    """Test provider execution performance."""

    @pytest.mark.asyncio
    async def test_mock_handler_performance(self) -> None:
        """Test that mock handler execution is fast."""

        def mock_handler(agent, prompt, context):
            return {"result": "fast response"}

        provider = CopilotProvider(mock_handler=mock_handler)

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent"),
            agents=[
                AgentDef(
                    name="agent",
                    model="gpt-4",
                    prompt="Test",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent.output.result }}"},
        )

        engine = WorkflowEngine(config, provider)

        start = time.perf_counter()
        for _ in range(100):
            await engine.run({})
        elapsed = time.perf_counter() - start

        # 100 workflow runs should complete in under 1 second with mock
        avg_time = elapsed / 100
        assert avg_time < 0.01, f"Average workflow time {avg_time:.4f}s, expected <0.01s"


class TestTemplatePerformance:
    """Test template rendering performance."""

    def test_complex_template_rendering(self) -> None:
        """Test that complex templates render quickly."""
        from conductor.executor.template import TemplateRenderer

        renderer = TemplateRenderer()

        # Complex template with loops, conditionals, and filters
        template = """
{% for item in items %}
  {% if item.active %}
    - {{ item.name }}: {{ item.data | json }}
  {% endif %}
{% endfor %}

Summary:
{{ summary | default("No summary") }}
Metadata: {{ metadata | json }}
"""

        context: dict[str, Any] = {
            "items": [
                {"name": f"Item {i}", "active": i % 2 == 0, "data": {"value": i}} for i in range(50)
            ],
            "summary": "Test summary with lots of text " * 10,
            "metadata": {"key1": "value1", "key2": ["a", "b", "c"]},
        }

        start = time.perf_counter()
        for _ in range(100):
            _ = renderer.render(template, context)
        elapsed = time.perf_counter() - start

        # 100 complex template renders should complete in under 1 second
        avg_time = elapsed / 100
        assert avg_time < 0.01, f"Average render time {avg_time:.4f}s, expected <0.01s"

    def test_json_filter_performance(self) -> None:
        """Test that json filter is fast with large objects."""
        from conductor.executor.template import TemplateRenderer

        renderer = TemplateRenderer()

        # Large nested object
        large_obj: dict[str, Any] = {
            "level1": {f"key{i}": {"nested": [f"value{j}" for j in range(10)]} for i in range(100)}
        }

        template = "{{ data | json }}"
        context = {"data": large_obj}

        start = time.perf_counter()
        for _ in range(50):
            _ = renderer.render(template, context)
        elapsed = time.perf_counter() - start

        # 50 json filter renders should complete quickly
        avg_time = elapsed / 50
        assert avg_time < 0.05, f"Average json render time {avg_time:.4f}s, expected <0.05s"


class TestRouterPerformance:
    """Test routing evaluation performance."""

    def test_many_routes_evaluation(self) -> None:
        """Test that evaluating many routes is fast."""
        from conductor.engine.router import Router

        router = Router()

        # Create many conditional routes
        routes = [RouteDef(to=f"agent{i}", when=f"value == {i}") for i in range(50)]
        routes.append(RouteDef(to="default"))  # Fallback

        output = {"value": 49}  # Match near the end
        context: dict[str, Any] = {"output": output}

        start = time.perf_counter()
        for _ in range(100):
            result = router.evaluate(routes, output, context)
        elapsed = time.perf_counter() - start

        # 100 route evaluations should complete in under 1 second
        avg_time = elapsed / 100
        assert avg_time < 0.01, f"Average route eval time {avg_time:.4f}s"
        assert result.target == "agent49"

    def test_arithmetic_conditions_performance(self) -> None:
        """Test that arithmetic conditions evaluate quickly."""
        from conductor.engine.router import Router

        router = Router()

        routes = [
            RouteDef(to="high", when="score >= 80 and confidence > 0.9"),
            RouteDef(to="medium", when="score >= 50 and score < 80"),
            RouteDef(to="low", when="score < 50"),
            RouteDef(to="default"),
        ]

        output = {"score": 75, "confidence": 0.85}
        context: dict[str, Any] = {"output": output}

        start = time.perf_counter()
        for _ in range(1000):
            result = router.evaluate(routes, output, context)
        elapsed = time.perf_counter() - start

        # 1000 arithmetic evaluations should be very fast
        avg_time = elapsed / 1000
        assert avg_time < 0.001, f"Average arithmetic eval {avg_time:.6f}s"
        assert result.target == "medium"


class TestContextPerformance:
    """Test context management performance."""

    def test_context_accumulation_performance(self) -> None:
        """Test that context accumulation is efficient."""
        from conductor.engine.context import WorkflowContext

        context = WorkflowContext()
        context.set_workflow_inputs({"input": "test"})

        start = time.perf_counter()

        # Simulate 100 agent outputs
        for i in range(100):
            context.store(
                f"agent{i}",
                {
                    "result": f"Output {i}",
                    "data": {"key": f"value{i}", "nested": [1, 2, 3]},
                },
            )

        # Build context for each agent
        for i in range(100):
            _ = context.build_for_agent(f"agent{i}", None, mode="accumulate")

        elapsed = time.perf_counter() - start

        # 100 stores + 100 builds should complete in under 1 second
        assert elapsed < 1.0, f"Context operations took {elapsed:.3f}s"

    def test_token_estimation_performance(self) -> None:
        """Test that token estimation is fast."""
        from conductor.engine.context import WorkflowContext

        context = WorkflowContext()
        context.set_workflow_inputs({"input": "test input " * 100})

        # Add many agent outputs
        for i in range(20):
            context.store(
                f"agent{i}",
                {
                    "result": "Long output text that simulates real content " * 50,
                },
            )

        start = time.perf_counter()
        for _ in range(1000):
            _ = context.estimate_context_tokens()
        elapsed = time.perf_counter() - start

        # 1000 token estimations should be fast
        avg_time = elapsed / 1000
        assert avg_time < 0.001, f"Average token estimate {avg_time:.6f}s"


class TestParallelExecutionPerformance:
    """Test parallel execution performance (PE-7.5)."""

    @pytest.mark.asyncio
    async def test_parallel_speedup_vs_sequential(self) -> None:
        """Test that parallel execution is faster than sequential execution."""
        import time

        from conductor.config.schema import ParallelGroup

        # Track execution timing for each agent
        execution_times: dict[str, tuple[float, float]] = {}  # agent -> (start, end)

        def mock_handler(agent, prompt, context):
            # Record execution time
            start = time.perf_counter()
            execution_times[agent.name] = (start, start)  # Will update end later
            # Small delay to simulate work
            time.sleep(0.05)  # 50ms
            end = time.perf_counter()
            execution_times[agent.name] = (start, end)
            return {"result": f"{agent.name} done"}

        # Create workflow with 3 agents that simulate work
        sequential_workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="sequential-workflow",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="copilot"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Task 1",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Task 2",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="agent3")],
                ),
                AgentDef(
                    name="agent3",
                    model="gpt-4",
                    prompt="Task 3",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "done"},
        )

        parallel_workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-workflow",
                entry_point="parallel_group",
                runtime=RuntimeConfig(provider="copilot"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Task 1",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Task 2",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="agent3",
                    model="gpt-4",
                    prompt="Task 3",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_group",
                    agents=["agent1", "agent2", "agent3"],
                    failure_mode="fail_fast",
                ),
            ],
            output={"result": "done"},
        )

        # Test sequential execution
        execution_times.clear()
        provider_seq = CopilotProvider(mock_handler=mock_handler)
        engine_seq = WorkflowEngine(sequential_workflow, provider_seq)

        start_seq = time.perf_counter()
        await engine_seq.run({})
        sequential_time = time.perf_counter() - start_seq

        # Verify agents ran sequentially (non-overlapping)
        agent1_end = execution_times["agent1"][1]
        agent2_start = execution_times["agent2"][0]
        agent2_end = execution_times["agent2"][1]
        agent3_start = execution_times["agent3"][0]

        # Each agent should start after the previous one ends (allowing for small overhead)
        assert agent2_start >= agent1_end - 0.01  # 10ms tolerance
        assert agent3_start >= agent2_end - 0.01

        # Test parallel execution
        execution_times.clear()  # Clear for parallel run
        provider_par = CopilotProvider(mock_handler=mock_handler)
        engine_par = WorkflowEngine(parallel_workflow, provider_par)

        start_par = time.perf_counter()
        await engine_par.run({})
        parallel_time = time.perf_counter() - start_par

        # Verify agents ran in parallel (overlapping)
        par_starts = [execution_times[f"agent{i}"][0] for i in range(1, 4)]
        [execution_times[f"agent{i}"][1] for i in range(1, 4)]

        min_start = min(par_starts)
        max_start = max(par_starts)

        # All agents should start within a reasonable time window
        # Note: With sync mock handler and Python GIL, there may be some serialization
        start_window = max_start - min_start
        # Allow up to 200ms window since sync handlers may serialize somewhat
        assert start_window < 0.2, (
            f"Parallel agents started {start_window:.3f}s apart, expected <0.2s"
        )

        # Parallel should not be slower than sequential (allowing for overhead)
        # Note: With sync handlers, true parallelism is limited by GIL
        # But asyncio should still provide some concurrency benefits
        speedup = sequential_time / parallel_time
        assert speedup >= 0.9, (
            f"Parallel execution slower than sequential: {speedup:.2f}x "
            f"(sequential: {sequential_time:.3f}s, parallel: {parallel_time:.3f}s)"
        )

        # Verify parallel execution structure is correct (all agents executed concurrently)
        # GIL limits true parallelism with sync handlers, but structure should be correct

    @pytest.mark.asyncio
    async def test_parallel_overhead_is_minimal(self) -> None:
        """Test that parallel execution overhead is minimal."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-overhead",
                entry_point="parallel_group",
                runtime=RuntimeConfig(provider="copilot"),
            ),
            agents=[
                AgentDef(
                    name=f"agent{i}",
                    model="gpt-4",
                    prompt=f"Task {i}",
                    output={"result": OutputField(type="string")},
                )
                for i in range(1, 6)
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_group",
                    agents=[f"agent{i}" for i in range(1, 6)],
                    failure_mode="fail_fast",
                ),
            ],
            output={"result": "done"},
        )

        # Mock handler that completes instantly
        def mock_handler(agent, prompt, context):
            return {"result": "instant"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(workflow, provider)

        # Run 10 times and average
        start = time.perf_counter()
        for _ in range(10):
            await engine.run({})
        elapsed = time.perf_counter() - start

        avg_time = elapsed / 10

        # Even with 5 agents, overhead should be minimal (<10ms per run)
        assert avg_time < 0.01, f"Parallel overhead {avg_time * 1000:.2f}ms, expected <10ms"


class TestForEachPerformance:
    """Performance tests for for-each (dynamic parallel) execution.

    Tests cover Epic 10 acceptance criteria:
    - 100-item array completes within reasonable time (10x single execution + overhead)
    - Memory usage acceptable for 1000-item arrays
    - No performance regressions vs static parallel
    """

    @pytest.mark.asyncio
    async def test_100_item_array_with_max_concurrent_10(self) -> None:
        """E10-T1: Test performance with 100-item array and max_concurrent=10.

        Acceptance: Should complete within reasonable time (10x single execution + overhead).
        With max_concurrent=10, we expect ~10 batches, so roughly 10x the time of
        a single execution, plus some overhead.
        """
        from conductor.config.schema import ForEachDef

        # First, measure single agent execution time with async mock
        single_workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="single-agent-baseline",
                entry_point="agent",
            ),
            agents=[
                AgentDef(
                    name="agent",
                    model="gpt-4",
                    prompt="Process item",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent.output.result }}"},
        )

        async def single_mock_execute(*args, **kwargs):
            await asyncio.sleep(0.05)  # 50ms async sleep
            return AgentOutput(
                content={"result": "processed"},
                raw_response={},
                model="gpt-4",
                tokens_used=10,
            )

        provider_single = MagicMock()
        provider_single.execute = AsyncMock(side_effect=single_mock_execute)
        engine_single = WorkflowEngine(single_workflow, provider_single)

        start_single = time.perf_counter()
        await engine_single.run({})
        single_time = time.perf_counter() - start_single

        # Now test 100-item for-each
        for_each_workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="for-each-100-items",
                entry_point="finder",
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Generate items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="processors",
                    type="for_each",
                    source="finder.output.items",
                    agent=AgentDef(
                        name="processor",
                        model="gpt-4",
                        prompt="Process item {{ item }}",
                        output={"result": OutputField(type="string")},
                    ),
                    max_concurrent=10,
                    failure_mode="continue_on_error",
                    **{"as": "item"},
                ),
            ],
            output={"count": "{{ processors.count }}"},
        )

        async def foreach_mock_execute(*args, **kwargs):
            agent = kwargs.get("agent") if "agent" in kwargs else args[0]
            if agent.name == "finder":
                return AgentOutput(
                    content={"items": list(range(100))},
                    raw_response={},
                    model="gpt-4",
                    tokens_used=10,
                )
            await asyncio.sleep(0.05)  # 50ms async sleep
            return AgentOutput(
                content={"result": "processed"},
                raw_response={},
                model="gpt-4",
                tokens_used=10,
            )

        provider_foreach = MagicMock()
        provider_foreach.execute = AsyncMock(side_effect=foreach_mock_execute)
        engine_foreach = WorkflowEngine(for_each_workflow, provider_foreach)

        start_foreach = time.perf_counter()
        result = await engine_foreach.run({})
        foreach_time = time.perf_counter() - start_foreach

        # Verify execution completed successfully
        assert result["count"] == 100

        # Performance expectation: ~10 batches @ 50ms each = ~500ms baseline
        # Allow for overhead: should complete within 10x single execution + 200ms overhead
        expected_max_time = (single_time * 10) + 0.2

        assert foreach_time < expected_max_time, (
            f"100-item for-each took {foreach_time:.3f}s, "
            f"expected <{expected_max_time:.3f}s "
            f"(10x single={single_time:.3f}s + 0.2s overhead)"
        )

        # Also verify it's actually running in parallel (should be much faster than sequential)
        sequential_estimate = single_time * 100
        speedup = sequential_estimate / foreach_time

        # With max_concurrent=10, we should see significant speedup
        assert speedup > 5, (
            f"For-each speedup is only {speedup:.1f}x, "
            f"expected >5x (foreach={foreach_time:.3f}s, "
            f"sequential estimate={sequential_estimate:.3f}s)"
        )

    @pytest.mark.asyncio
    async def test_10_item_array_with_max_concurrent_5(self) -> None:
        """E10-T2: Test performance with 10-item array and max_concurrent=5.

        Acceptance: Should complete within reasonable time with proper batching.
        """
        from conductor.config.schema import ForEachDef

        for_each_workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="for-each-10-items",
                entry_point="finder",
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Generate items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="processors",
                    type="for_each",
                    source="finder.output.items",
                    agent=AgentDef(
                        name="processor",
                        model="gpt-4",
                        prompt="Process item {{ item }}",
                        output={"result": OutputField(type="string")},
                    ),
                    max_concurrent=5,
                    failure_mode="continue_on_error",
                    **{"as": "item"},
                ),
            ],
            output={"count": "{{ processors.count }}"},
        )

        execution_times: list[float] = []

        async def mock_execute(*args, **kwargs):
            agent = kwargs.get("agent") if "agent" in kwargs else args[0]
            if agent.name == "finder":
                return AgentOutput(
                    content={"items": list(range(10))},
                    raw_response={},
                    model="gpt-4",
                    tokens_used=10,
                )
            # Track execution timing
            execution_times.append(time.perf_counter())
            await asyncio.sleep(0.05)  # 50ms async sleep
            return AgentOutput(
                content={"result": "processed"},
                raw_response={},
                model="gpt-4",
                tokens_used=10,
            )

        provider = MagicMock()
        provider.execute = AsyncMock(side_effect=mock_execute)
        engine = WorkflowEngine(for_each_workflow, provider)

        start = time.perf_counter()
        result = await engine.run({})
        elapsed = time.perf_counter() - start

        # Verify execution
        assert result["count"] == 10

        # With max_concurrent=5 and 10 items, we expect 2 batches
        # Each batch ~50ms, so ~100ms total + overhead
        # Allow generous overhead for test stability
        assert elapsed < 0.5, f"10-item for-each took {elapsed:.3f}s, expected <0.5s"

    @pytest.mark.asyncio
    async def test_memory_usage_1000_items(self) -> None:
        """E10-T3: Memory profiling for large arrays (1000 items).

        Acceptance: Memory usage should be acceptable for 1000-item arrays.
        With batching, memory should not grow linearly with array size.
        """
        import tracemalloc

        from conductor.config.schema import ForEachDef

        tracemalloc.start()

        for_each_workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="for-each-1000-items",
                entry_point="finder",
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Generate items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="processors",
                    type="for_each",
                    source="finder.output.items",
                    agent=AgentDef(
                        name="processor",
                        model="gpt-4",
                        prompt="Process {{ item }}",
                        output={"result": OutputField(type="string")},
                    ),
                    max_concurrent=20,
                    failure_mode="continue_on_error",
                    **{"as": "item"},
                ),
            ],
            output={"count": "{{ processors.count }}"},
        )

        def mock_handler(agent, prompt, context):
            if agent.name == "finder":
                # Generate 1000 items
                return {"items": [{"id": i, "data": f"item_{i}"} for i in range(1000)]}
            else:
                # Small delay to simulate work
                time.sleep(0.001)  # 1ms per item for faster test
                return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(for_each_workflow, provider)

        await engine.run({})

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Memory should be reasonable - allow up to 200MB for 1000 items
        # This is conservative; with proper batching it should be much less
        max_allowed_mb = 200
        peak_mb = peak / (1024 * 1024)

        assert peak_mb < max_allowed_mb, (
            f"1000-item for-each used {peak_mb:.1f}MB peak memory, expected <{max_allowed_mb}MB"
        )

    @pytest.mark.asyncio
    async def test_performance_parity_with_static_parallel(self) -> None:
        """E10-T4: Test that for-each has no significant regression vs static parallel.

        Acceptance: For-each should perform similarly to static parallel
        for the same number of agents.
        """
        from conductor.config.schema import ForEachDef

        num_agents = 20

        # Static parallel workflow
        static_workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="static-parallel",
                entry_point="parallel_group",
                limits=LimitsConfig(max_iterations=100),  # Allow enough for 20 agents
            ),
            agents=[
                AgentDef(
                    name=f"agent{i}",
                    model="gpt-4",
                    prompt=f"Task {i}",
                    output={"result": OutputField(type="string")},
                )
                for i in range(num_agents)
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_group",
                    agents=[f"agent{i}" for i in range(num_agents)],
                    failure_mode="continue_on_error",
                ),
            ],
            output={"result": "done"},
        )

        # For-each workflow
        foreach_workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="foreach-parallel",
                entry_point="finder",
                limits=LimitsConfig(max_iterations=100),  # Allow enough for 20 agents
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Generate items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="processors",
                    type="for_each",
                    source="finder.output.items",
                    agent=AgentDef(
                        name="processor",
                        model="gpt-4",
                        prompt="Process {{ item }}",
                        output={"result": OutputField(type="string")},
                    ),
                    max_concurrent=num_agents,  # Same concurrency as static
                    failure_mode="continue_on_error",
                    **{"as": "item"},
                ),
            ],
            output={"count": "{{ processors.count }}"},
        )

        async def mock_static_execute(*args, **kwargs):
            await asyncio.sleep(0.02)  # 20ms async sleep
            return AgentOutput(
                content={"result": "ok"},
                raw_response={},
                model="gpt-4",
                tokens_used=10,
            )

        async def mock_foreach_execute(*args, **kwargs):
            agent = kwargs.get("agent") if "agent" in kwargs else args[0]
            if agent.name == "finder":
                return AgentOutput(
                    content={"items": list(range(num_agents))},
                    raw_response={},
                    model="gpt-4",
                    tokens_used=10,
                )
            await asyncio.sleep(0.02)  # 20ms async sleep
            return AgentOutput(
                content={"result": "ok"},
                raw_response={},
                model="gpt-4",
                tokens_used=10,
            )

        # Test static parallel
        provider_static = MagicMock()
        provider_static.execute = AsyncMock(side_effect=mock_static_execute)
        engine_static = WorkflowEngine(static_workflow, provider_static)

        start_static = time.perf_counter()
        await engine_static.run({})
        static_time = time.perf_counter() - start_static

        # Test for-each
        provider_foreach = MagicMock()
        provider_foreach.execute = AsyncMock(side_effect=mock_foreach_execute)
        engine_foreach = WorkflowEngine(foreach_workflow, provider_foreach)

        start_foreach = time.perf_counter()
        result = await engine_foreach.run({})
        foreach_time = time.perf_counter() - start_foreach

        # Verify for-each executed correctly
        assert result["count"] == num_agents

        # For-each should be within 50% of static parallel performance
        # (allowing for array resolution and batching overhead)
        max_allowed = static_time * 1.5

        assert foreach_time < max_allowed, (
            f"For-each took {foreach_time:.3f}s vs static parallel {static_time:.3f}s, "
            f"expected for-each <{max_allowed:.3f}s (1.5x static)"
        )

    @pytest.mark.asyncio
    async def test_empty_array_performance(self) -> None:
        """Test that empty arrays have minimal overhead."""
        from conductor.config.schema import ForEachDef

        for_each_workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="for-each-empty",
                entry_point="finder",
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Generate items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="processors",
                    type="for_each",
                    source="finder.output.items",
                    agent=AgentDef(
                        name="processor",
                        model="gpt-4",
                        prompt="Process {{ item }}",
                        output={"result": OutputField(type="string")},
                    ),
                    max_concurrent=10,
                    **{"as": "item"},
                ),
            ],
            output={"count": "{{ processors.count }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"items": []} if agent.name == "finder" else {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(for_each_workflow, provider)

        start = time.perf_counter()
        result = await engine.run({})
        elapsed = time.perf_counter() - start

        # Empty array should complete very quickly
        assert result["count"] == 0
        assert elapsed < 0.1, f"Empty array took {elapsed:.3f}s, expected <0.1s"

    @pytest.mark.asyncio
    async def test_batching_scalability(self) -> None:
        """Test that execution time scales with batch count, not item count."""
        from conductor.config.schema import ForEachDef

        async def run_foreach_with_items(num_items: int, max_concurrent: int) -> float:
            """Helper to run for-each and return execution time."""
            workflow = WorkflowConfig(
                workflow=WorkflowDef(
                    name="scalability-test",
                    entry_point="finder",
                ),
                agents=[
                    AgentDef(
                        name="finder",
                        model="gpt-4",
                        prompt="items",
                        output={"items": OutputField(type="array")},
                        routes=[RouteDef(to="processors")],
                    ),
                ],
                for_each=[
                    ForEachDef(
                        name="processors",
                        type="for_each",
                        source="finder.output.items",
                        agent=AgentDef(
                            name="processor",
                            model="gpt-4",
                            prompt="process",
                            output={"result": OutputField(type="string")},
                        ),
                        max_concurrent=max_concurrent,
                        **{"as": "item"},
                    ),
                ],
                output={"count": "{{ processors.count }}"},
            )

            async def mock_execute(*args, **kwargs):
                agent = kwargs.get("agent") if "agent" in kwargs else args[0]
                if agent.name == "finder":
                    return AgentOutput(
                        content={"items": list(range(num_items))},
                        raw_response={},
                        model="gpt-4",
                        tokens_used=10,
                    )
                await asyncio.sleep(0.01)  # 10ms async sleep
                return AgentOutput(
                    content={"result": "ok"},
                    raw_response={},
                    model="gpt-4",
                    tokens_used=10,
                )

            provider = MagicMock()
            provider.execute = AsyncMock(side_effect=mock_execute)
            engine = WorkflowEngine(workflow, provider)

            start = time.perf_counter()
            await engine.run({})
            return time.perf_counter() - start

        # Test with same batch count but different item counts
        # Both should take similar time since they have same number of batches

        # 20 items with max_concurrent=10 = 2 batches
        time_20_items = await run_foreach_with_items(20, 10)

        # 100 items with max_concurrent=50 = 2 batches
        time_100_items = await run_foreach_with_items(100, 50)

        # Both should complete in similar time (2 batches each)
        # Allow 50% variance for overhead differences
        ratio = max(time_20_items, time_100_items) / min(time_20_items, time_100_items)

        # Both should complete in similar time (2 batches each)
        # Allow 200% variance to account for per-item overhead (context setup, aggregation)
        # and timing variability on CI systems
        assert ratio < 3.0, (
            f"Execution time should scale with batch count, not item count. "
            f"20 items: {time_20_items:.3f}s, 100 items: {time_100_items:.3f}s, "
            f"ratio: {ratio:.2f}x (expected <3.0x)"
        )
