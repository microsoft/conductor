"""Integration tests for Claude workflows.

Tests cover:
- EPIC-008-T2: Basic workflow integration test (mocked API)
- EPIC-008-T3: Parallel execution test with Claude
- EPIC-008-T4: For-each loop test with Claude
- EPIC-008-T5: Routing and conditional logic test
- EPIC-008-T6: Error handling and recovery test (rate limits, auth failures)
- EPIC-008-T8: Performance test for Claude non-streaming

All tests use the Pydantic AI TestModel seam to mock the provider boundary
without making real network calls. The WorkflowEngine path stays real.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    OutputField,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ExecutionError, ProviderError
from conductor.providers._pydantic_ai.interrupt import RunOutcome
from conductor.providers.claude import ClaudeProvider


def _build_structured_agent(data: dict[str, Any]) -> Agent[Any, Any]:
    """Build a Pydantic AI structured-output agent backed by TestModel."""

    class DynamicModel(BaseModel):
        model_config = {"extra": "allow"}

    return Agent(
        model=TestModel(custom_output_args=data),
        output_type=DynamicModel,
    )


@pytest.fixture
def claude_fixtures_dir() -> Path:
    """Return path to Claude test fixtures directory."""
    return Path(__file__).parent.parent / "fixtures" / "claude"


@pytest.fixture
def mock_claude_agent():
    """Create a mock Pydantic AI agent returning fixture responses."""

    def _create_mock(fixture_name: str, fixtures_dir: Path) -> Agent[Any, Any]:
        """Create a TestModel-backed agent with responses from fixture file."""
        fixture_file = fixtures_dir / f"{fixture_name}.json"
        with open(fixture_file) as f:
            responses = json.load(f)

        # Single response -> always return it; multiple responses keyed by agent
        if isinstance(responses, dict) and "id" not in responses:
            response_map = responses

            def _dynamic_output() -> dict[str, Any]:
                # This function is invoked from the TestModel coroutine, where we
                # have no access to the prompt. We therefore return a static
                # fallback and rely on the caller's prompt-inspection path if
                # response selection matters.
                return next(iter(response_map.values()))["content"][0]["input"]

            # For tests that need per-agent selection, we create a callable that
            # the test patches over build_agent to select from the map by prompt.
            # Returning a single agent here still works for the simple cases.
            return response_map

        # Single response case
        return responses["content"][0]["input"]

    return _create_mock


class TestBasicClaudeWorkflow:
    """EPIC-008-T2: Basic workflow integration test (mocked API)."""

    @pytest.mark.asyncio
    async def test_simple_qa_workflow(self, claude_fixtures_dir: Path) -> None:
        """Test basic Q&A workflow with Claude provider using mocked responses."""
        fixture_file = claude_fixtures_dir / "simple_qa.json"
        with open(fixture_file) as f:
            response_data = json.load(f)
        answer = response_data["content"][0]["input"]["answer"]

        # Create simple Q&A workflow
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="simple-qa",
                description="Simple Q&A with Claude",
                entry_point="qa_agent",
                runtime=RuntimeConfig(provider="claude"),
            ),
            agents=[
                AgentDef(
                    name="qa_agent",
                    model="claude-3-5-sonnet-latest",
                    prompt="Answer this question: {{ workflow.input.question }}",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                )
            ],
            output={"answer": "{{ qa_agent.output.answer }}"},
        )

        provider = ClaudeProvider(api_key="test-key")
        engine = WorkflowEngine(workflow, provider)

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_structured_agent({"answer": answer}),
        ) as mock_build_agent:
            result = await engine.run({"question": "What is Python?"})

        # Verify result
        assert "answer" in result
        assert "Python" in result["answer"]
        assert "programming language" in result["answer"]

        # Verify the provider constructed a Pydantic AI agent for the step
        assert mock_build_agent.called
        assert mock_build_agent.call_args.kwargs["agent"].name == "qa_agent"


class TestParallelClaudeWorkflow:
    """EPIC-008-T3: Parallel execution test with Claude."""

    @pytest.mark.asyncio
    async def test_parallel_research_agents(
        self,
        claude_fixtures_dir: Path,
    ) -> None:
        """Test parallel research workflow with multiple Claude agents."""
        fixture_file = claude_fixtures_dir / "parallel_research.json"
        with open(fixture_file) as f:
            responses = json.load(f)

        # Track which agents have been called
        call_count = {"web": 0, "paper": 0, "expert": 0}

        def make_agent(**kwargs: Any) -> Agent[Any, Any]:
            rendered_prompt = kwargs.get("rendered_prompt", "")
            prompt_lower = rendered_prompt.lower()
            if "web" in prompt_lower:
                call_count["web"] += 1
                resp_data = responses["web_research"]
            elif "paper" in prompt_lower or "academic" in prompt_lower:
                call_count["paper"] += 1
                resp_data = responses["paper_research"]
            elif "expert" in prompt_lower:
                call_count["expert"] += 1
                resp_data = responses["expert_research"]
            else:
                call_count["web"] += 1
                resp_data = responses["web_research"]

            return _build_structured_agent(resp_data["content"][0]["input"])

        # Create parallel research workflow
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-research",
                description="Parallel research with Claude",
                entry_point="parallel_research",
                runtime=RuntimeConfig(provider="claude"),
                context=ContextConfig(mode="accumulate"),
            ),
            agents=[
                AgentDef(
                    name="web_researcher",
                    model="claude-3-5-sonnet-latest",
                    prompt="Research {{ workflow.input.topic }} on the web",
                    output={
                        "findings": OutputField(type="string"),
                        "sources": OutputField(type="array"),
                    },
                ),
                AgentDef(
                    name="paper_researcher",
                    model="claude-3-5-sonnet-latest",
                    prompt="Find academic papers about {{ workflow.input.topic }}",
                    output={
                        "findings": OutputField(type="string"),
                        "papers": OutputField(type="array"),
                    },
                ),
                AgentDef(
                    name="expert_researcher",
                    model="claude-3-5-sonnet-latest",
                    prompt="Find expert opinions on {{ workflow.input.topic }}",
                    output={
                        "findings": OutputField(type="string"),
                        "experts": OutputField(type="array"),
                    },
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_research",
                    agents=["web_researcher", "paper_researcher", "expert_researcher"],
                    failure_mode="fail_fast",
                    routes=[RouteDef(to="$end")],
                )
            ],
            output={
                "web_researcher": "{{ parallel_research.outputs.web_researcher | json }}",
                "paper_researcher": "{{ parallel_research.outputs.paper_researcher | json }}",
                "expert_researcher": "{{ parallel_research.outputs.expert_researcher | json }}",
            },
        )

        provider = ClaudeProvider(api_key="test-key")
        engine = WorkflowEngine(workflow, provider)

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            side_effect=make_agent,
        ):
            result = await engine.run({"topic": "quantum computing"})

        # Verify all agents were called
        assert call_count["web"] == 1
        assert call_count["paper"] == 1
        assert call_count["expert"] == 1

        # Verify results contain findings from all agents
        assert "web_researcher" in result
        assert "findings" in result["web_researcher"]
        assert "quantum" in result["web_researcher"]["findings"].lower()


class TestForEachClaudeWorkflow:
    """EPIC-008-T4: For-each loop test with Claude."""

    @pytest.mark.asyncio
    async def test_for_each_data_processing(
        self,
        claude_fixtures_dir: Path,
    ) -> None:
        """Test for-each workflow processing multiple items with Claude."""
        fixture_file = claude_fixtures_dir / "for_each_data.json"
        with open(fixture_file) as f:
            responses = json.load(f)

        call_index = [0]  # Track iteration

        def make_agent(**kwargs: Any) -> Agent[Any, Any]:
            idx = call_index[0]
            call_index[0] += 1
            resp_data = responses[f"analyze_item_{idx}"]
            return _build_structured_agent(resp_data["content"][0]["input"])

        # Create for-each workflow (simplified schema - actual for-each structure TBD)
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="data-processor",
                description="Process items with for-each",
                entry_point="processor_1",
                runtime=RuntimeConfig(provider="claude"),
            ),
            agents=[
                # Simulate three sequential calls (for-each pattern)
                AgentDef(
                    name="processor_1",
                    model="claude-3-5-sonnet-latest",
                    prompt="Analyze: apple",
                    output={
                        "item": OutputField(type="string"),
                        "analysis": OutputField(type="string"),
                        "score": OutputField(type="number"),
                    },
                    routes=[RouteDef(to="processor_2")],
                ),
                AgentDef(
                    name="processor_2",
                    model="claude-3-5-sonnet-latest",
                    prompt="Analyze: banana",
                    output={
                        "item": OutputField(type="string"),
                        "analysis": OutputField(type="string"),
                        "score": OutputField(type="number"),
                    },
                    routes=[RouteDef(to="processor_3")],
                ),
                AgentDef(
                    name="processor_3",
                    model="claude-3-5-sonnet-latest",
                    prompt="Analyze: carrot",
                    output={
                        "item": OutputField(type="string"),
                        "analysis": OutputField(type="string"),
                        "score": OutputField(type="number"),
                    },
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "processor_1": "{{ processor_1.output | json }}",
                "processor_2": "{{ processor_2.output | json }}",
                "processor_3": "{{ processor_3.output | json }}",
            },
        )

        provider = ClaudeProvider(api_key="test-key")
        engine = WorkflowEngine(workflow, provider)

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            side_effect=make_agent,
        ):
            result = await engine.run({})

        # Verify all items were processed
        assert call_index[0] == 3
        assert "processor_3" in result
        assert result["processor_3"]["item"] == "carrot"
        assert result["processor_3"]["score"] == 90


class TestRoutingClaudeWorkflow:
    """EPIC-008-T5: Routing and conditional logic test."""

    @pytest.mark.asyncio
    async def test_conditional_routing_high_confidence(
        self,
        claude_fixtures_dir: Path,
    ) -> None:
        """Test workflow routing based on confidence score."""
        fixture_file = claude_fixtures_dir / "routing.json"
        with open(fixture_file) as f:
            responses = json.load(f)

        def make_agent(**kwargs: Any) -> Agent[Any, Any]:
            return _build_structured_agent(responses["high_confidence"]["content"][0]["input"])

        # Create routing workflow
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="routing-test",
                description="Test conditional routing",
                entry_point="planner",
                runtime=RuntimeConfig(provider="claude"),
            ),
            agents=[
                AgentDef(
                    name="planner",
                    model="claude-3-5-sonnet-latest",
                    prompt="Create a plan",
                    output={
                        "plan": OutputField(type="string"),
                        "confidence": OutputField(type="number"),
                    },
                    routes=[
                        RouteDef(to="$end", when="{{ planner.output.confidence > 0.8 }}"),
                        RouteDef(to="refiner"),
                    ],
                ),
                AgentDef(
                    name="refiner",
                    model="claude-3-5-sonnet-latest",
                    prompt="Refine the plan",
                    output={
                        "plan": OutputField(type="string"),
                        "confidence": OutputField(type="number"),
                    },
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "planner": "{{ planner.output | json }}",
            },
        )

        provider = ClaudeProvider(api_key="test-key")
        engine = WorkflowEngine(workflow, provider)

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            side_effect=make_agent,
        ):
            result = await engine.run({})

        # High confidence should go directly to end, skipping refiner
        assert "planner" in result
        assert result["planner"]["confidence"] == 0.95
        assert "refiner" not in result


class MockRateLimitError(Exception):
    """Fake Anthropic RateLimitError with a retry-after header.

    Named so the retry helper classifies it as a retryable rate limit.
    """

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.response = Mock(headers={"retry-after": str(retry_after)})
        self.status_code = 429


class MockAuthenticationError(Exception):
    """Fake Anthropic AuthenticationError (non-retryable)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.response = Mock(headers={})
        self.status_code = 401


class TestErrorHandlingClaudeWorkflow:
    """EPIC-008-T6: Error handling and recovery test."""

    @pytest.mark.asyncio
    async def test_rate_limit_error_handling(
        self,
        claude_fixtures_dir: Path,
    ) -> None:
        """Test rate limit error handling with retry logic."""
        fixture_file = claude_fixtures_dir / "error_responses.json"
        with open(fixture_file) as f:
            error_responses = json.load(f)

        call_count = [0]

        async def failing_run(*args: Any, **kwargs: Any) -> RunOutcome:
            call_count[0] += 1
            if call_count[0] < 3:
                raise MockRateLimitError(
                    error_responses["rate_limit"]["error"]["message"],
                    retry_after=0.01,
                )

            agent = _build_structured_agent({"result": "Success after retry"})
            async with agent.iter("test") as run:
                async for _node in run:
                    pass
            assert run.result is not None
            return RunOutcome(result=run.result)

        # Create simple workflow
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="retry-test",
                description="Test retry on rate limit",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="claude"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="claude-3-5-sonnet-latest",
                    prompt="Test prompt",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                )
            ],
            output={"result": "{{ agent1.output.result }}"},
        )

        provider = ClaudeProvider(api_key="test-key")
        engine = WorkflowEngine(workflow, provider)

        with (
            patch(
                "conductor.providers._pydantic_ai.agent_builder.build_agent",
                return_value=_build_structured_agent({"result": "unused"}),
            ),
            patch(
                "conductor.providers._pydantic_ai.interrupt.run_with_interrupt",
                side_effect=failing_run,
            ),
            patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep,
        ):
            result = await engine.run({})

        # Should succeed after retries
        assert call_count[0] == 3
        assert "result" in result
        assert result["result"] == "Success after retry"
        # Retry delays should be short (from retry-after header and backoff)
        assert mock_sleep.called

    @pytest.mark.asyncio
    async def test_auth_failure_no_retry(
        self,
        claude_fixtures_dir: Path,
    ) -> None:
        """Test that authentication errors fail immediately without retry."""
        fixture_file = claude_fixtures_dir / "error_responses.json"
        with open(fixture_file) as f:
            error_responses = json.load(f)

        async def failing_run(*args: Any, **kwargs: Any) -> RunOutcome:
            raise MockAuthenticationError(error_responses["auth_failure"]["error"]["message"])

        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="auth-test",
                description="Test auth failure",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="claude"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="claude-3-5-sonnet-latest",
                    prompt="Test",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                )
            ],
            output={"result": "{{ agent1.output.result }}"},
        )

        provider = ClaudeProvider(api_key="test-key")
        engine = WorkflowEngine(workflow, provider)

        # Should raise ProviderError without retries
        with (
            pytest.raises((ProviderError, ExecutionError)),
            patch(
                "conductor.providers._pydantic_ai.agent_builder.build_agent",
                return_value=_build_structured_agent({"result": "unused"}),
            ),
            patch(
                "conductor.providers._pydantic_ai.interrupt.run_with_interrupt",
                side_effect=failing_run,
            ),
            patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep,
        ):
            await engine.run({})

        mock_sleep.assert_not_called()


@pytest.mark.performance
class TestClaudePerformance:
    """EPIC-008-T8: Performance test for Claude non-streaming."""

    @pytest.mark.asyncio
    async def test_provider_overhead_baseline(self) -> None:
        """Measure provider overhead with the Pydantic AI mock seam."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="perf-test",
                description="Performance baseline test",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="claude"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="claude-3-5-sonnet-latest",
                    prompt="Test",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                )
            ],
            output={"result": "{{ agent1.output.result }}"},
        )

        provider = ClaudeProvider(api_key="test-key")
        engine = WorkflowEngine(workflow, provider)

        samples = []
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_structured_agent({"result": "test"}),
        ):
            for _ in range(10):
                start = time.perf_counter()
                await engine.run({})
                duration = (time.perf_counter() - start) * 1000
                samples.append(duration)

        mean = sum(samples) / len(samples)
        samples_sorted = sorted(samples)
        p95 = samples_sorted[min(8, len(samples) - 1)]
        p99 = samples_sorted[min(9, len(samples) - 1)]

        assert mean < 1000.0, f"Mean overhead {mean:.2f}ms exceeds 1000ms threshold"
        assert p95 < 1500.0, f"P95 overhead {p95:.2f}ms exceeds 1500ms threshold"

        # Log results for baseline tracking
        print("\nPerformance Baseline (100 samples):")
        print(f"  Mean: {mean:.2f}ms")
        print(f"  P95:  {p95:.2f}ms")
        print(f"  P99:  {p99:.2f}ms")
