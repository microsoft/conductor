"""Integration tests for Claude workflows.

Tests cover:
- EPIC-008-T2: Basic workflow integration test (mocked API)
- EPIC-008-T3: Parallel execution test with Claude
- EPIC-008-T4: For-each loop test with Claude
- EPIC-008-T5: Routing and conditional logic test
- EPIC-008-T6: Error handling and recovery test (rate limits, auth failures)
- EPIC-008-T8: Performance test for Claude non-streaming
"""

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

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
from conductor.providers.claude import ClaudeProvider


@pytest.fixture
def claude_fixtures_dir() -> Path:
    """Return path to Claude test fixtures directory."""
    return Path(__file__).parent.parent / "fixtures" / "claude"


@pytest.fixture
def mock_claude_client():
    """Create a mock Claude client with recorded API responses."""

    def _create_mock(fixture_name: str, fixtures_dir: Path):
        """Create mock client with responses from fixture file."""
        fixture_file = fixtures_dir / f"{fixture_name}.json"
        with open(fixture_file) as f:
            responses = json.load(f)

        mock_client = Mock()
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(
            return_value=Mock(
                data=[
                    Mock(id="claude-3-5-sonnet-latest"),
                    Mock(id="claude-3-opus-20240229"),
                    Mock(id="claude-3-haiku-20240307"),
                ]
            )
        )

        # Convert JSON responses to mock Message objects
        def create_message_mock(response_data: dict) -> Mock:
            """Create a mock Message object from response data."""
            mock_msg = Mock()
            mock_msg.id = response_data["id"]
            mock_msg.type = response_data["type"]
            mock_msg.role = response_data["role"]
            mock_msg.model = response_data["model"]
            mock_msg.stop_reason = response_data["stop_reason"]
            mock_msg.usage = Mock(**response_data["usage"])

            # Create content blocks
            content_blocks = []
            for block in response_data["content"]:
                mock_block = Mock()
                mock_block.type = block["type"]
                if block["type"] == "tool_use":
                    mock_block.id = block["id"]
                    mock_block.name = block["name"]
                    mock_block.input = block["input"]
                content_blocks.append(mock_block)
            mock_msg.content = content_blocks

            return mock_msg

        # Store responses keyed by agent name or single response
        if isinstance(responses, dict) and "id" not in responses:
            # Multiple responses keyed by agent name
            response_map = {
                agent_name: create_message_mock(resp_data)
                for agent_name, resp_data in responses.items()
            }

            async def create_side_effect(*args, **kwargs):
                # Extract agent name from messages if available
                messages = kwargs.get("messages", [])
                if messages and "user" in str(messages[0]):
                    # Try to extract agent context from prompt
                    for agent_name in response_map:
                        if agent_name in str(messages):
                            return response_map[agent_name]
                # Default to first response
                return next(iter(response_map.values()))

            mock_client.messages.create = AsyncMock(side_effect=create_side_effect)
        else:
            # Single response
            mock_client.messages.create = AsyncMock(return_value=create_message_mock(responses))

        return mock_client

    return _create_mock


class TestBasicClaudeWorkflow:
    """EPIC-008-T2: Basic workflow integration test (mocked API)."""

    @pytest.mark.asyncio
    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    async def test_simple_qa_workflow(
        self,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
        claude_fixtures_dir: Path,
        mock_claude_client,
    ) -> None:
        """Test basic Q&A workflow with Claude provider using mocked responses."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = mock_claude_client("simple_qa", claude_fixtures_dir)
        mock_anthropic_class.return_value = mock_client

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

        provider = ClaudeProvider()
        engine = WorkflowEngine(workflow, provider)

        result = await engine.run({"question": "What is Python?"})

        # Verify result
        assert "answer" in result
        assert "Python" in result["answer"]
        assert "programming language" in result["answer"]

        # Verify API was called
        assert mock_client.messages.create.called


class TestParallelClaudeWorkflow:
    """EPIC-008-T3: Parallel execution test with Claude."""

    @pytest.mark.asyncio
    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    async def test_parallel_research_agents(
        self,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
        claude_fixtures_dir: Path,
    ) -> None:
        """Test parallel research workflow with multiple Claude agents."""
        mock_anthropic_module.__version__ = "0.77.0"

        # Load fixture responses
        fixture_file = claude_fixtures_dir / "parallel_research.json"
        with open(fixture_file) as f:
            responses = json.load(f)

        # Create mock client
        mock_client = Mock()
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(
            return_value=Mock(
                data=[
                    Mock(id="claude-3-5-sonnet-latest"),
                ]
            )
        )

        # Track which agents have been called
        call_count = {"web": 0, "paper": 0, "expert": 0}

        async def create_message(*args, **kwargs):
            messages = kwargs.get("messages", [])
            prompt = str(messages[0].get("content", "")) if messages else ""

            # Determine which agent based on prompt content
            if "web" in prompt.lower():
                call_count["web"] += 1
                resp_data = responses["web_research"]
            elif "paper" in prompt.lower():
                call_count["paper"] += 1
                resp_data = responses["paper_research"]
            elif "expert" in prompt.lower():
                call_count["expert"] += 1
                resp_data = responses["expert_research"]
            else:
                resp_data = responses["web_research"]  # default

            # Create mock message
            mock_msg = Mock()
            mock_msg.id = resp_data["id"]
            mock_msg.type = resp_data["type"]
            mock_msg.role = resp_data["role"]
            mock_msg.model = resp_data["model"]
            mock_msg.stop_reason = resp_data["stop_reason"]
            mock_msg.usage = Mock(**resp_data["usage"])

            # Create content blocks
            content_blocks = []
            for block in resp_data["content"]:
                mock_block = Mock()
                mock_block.type = block["type"]
                mock_block.id = block["id"]
                mock_block.name = block["name"]
                mock_block.input = block["input"]
                content_blocks.append(mock_block)
            mock_msg.content = content_blocks

            return mock_msg

        mock_client.messages.create = AsyncMock(side_effect=create_message)
        mock_anthropic_class.return_value = mock_client

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

        provider = ClaudeProvider()
        engine = WorkflowEngine(workflow, provider)

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
    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    async def test_for_each_data_processing(
        self,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
        claude_fixtures_dir: Path,
    ) -> None:
        """Test for-each workflow processing multiple items with Claude."""
        mock_anthropic_module.__version__ = "0.77.0"

        # Load fixture responses
        fixture_file = claude_fixtures_dir / "for_each_data.json"
        with open(fixture_file) as f:
            responses = json.load(f)

        # Create mock client
        mock_client = Mock()
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(
            return_value=Mock(data=[Mock(id="claude-3-5-sonnet-latest")])
        )

        call_index = [0]  # Track iteration

        async def create_message(*args, **kwargs):
            messages = kwargs.get("messages", [])
            str(messages[0].get("content", "")) if messages else ""

            # Determine iteration from prompt or use counter
            idx = call_index[0]
            call_index[0] += 1

            resp_data = responses[f"analyze_item_{idx}"]

            # Create mock message
            mock_msg = Mock()
            mock_msg.id = resp_data["id"]
            mock_msg.type = resp_data["type"]
            mock_msg.role = resp_data["role"]
            mock_msg.model = resp_data["model"]
            mock_msg.stop_reason = resp_data["stop_reason"]
            mock_msg.usage = Mock(**resp_data["usage"])

            content_blocks = []
            for block in resp_data["content"]:
                mock_block = Mock()
                mock_block.type = block["type"]
                mock_block.id = block["id"]
                mock_block.name = block["name"]
                mock_block.input = block["input"]
                content_blocks.append(mock_block)
            mock_msg.content = content_blocks

            return mock_msg

        mock_client.messages.create = AsyncMock(side_effect=create_message)
        mock_anthropic_class.return_value = mock_client

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

        provider = ClaudeProvider()
        engine = WorkflowEngine(workflow, provider)

        result = await engine.run({})

        # Verify all items were processed
        assert call_index[0] == 3
        assert "processor_3" in result
        assert result["processor_3"]["item"] == "carrot"
        assert result["processor_3"]["score"] == 90


class TestRoutingClaudeWorkflow:
    """EPIC-008-T5: Routing and conditional logic test."""

    @pytest.mark.asyncio
    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    async def test_conditional_routing_high_confidence(
        self,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
        claude_fixtures_dir: Path,
    ) -> None:
        """Test workflow routing based on confidence score."""
        mock_anthropic_module.__version__ = "0.77.0"

        # Load fixture responses
        fixture_file = claude_fixtures_dir / "routing.json"
        with open(fixture_file) as f:
            responses = json.load(f)

        # Create mock client
        mock_client = Mock()
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(
            return_value=Mock(data=[Mock(id="claude-3-5-sonnet-latest")])
        )

        async def create_message(*args, **kwargs):
            # Return high confidence response
            resp_data = responses["high_confidence"]

            mock_msg = Mock()
            mock_msg.id = resp_data["id"]
            mock_msg.type = resp_data["type"]
            mock_msg.role = resp_data["role"]
            mock_msg.model = resp_data["model"]
            mock_msg.stop_reason = resp_data["stop_reason"]
            mock_msg.usage = Mock(**resp_data["usage"])

            content_blocks = []
            for block in resp_data["content"]:
                mock_block = Mock()
                mock_block.type = block["type"]
                mock_block.id = block["id"]
                mock_block.name = block["name"]
                mock_block.input = block["input"]
                content_blocks.append(mock_block)
            mock_msg.content = content_blocks

            return mock_msg

        mock_client.messages.create = AsyncMock(side_effect=create_message)
        mock_anthropic_class.return_value = mock_client

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

        provider = ClaudeProvider()
        engine = WorkflowEngine(workflow, provider)

        result = await engine.run({})

        # High confidence should go directly to end, skipping refiner
        assert "planner" in result
        assert result["planner"]["confidence"] == 0.95
        assert "refiner" not in result


class TestErrorHandlingClaudeWorkflow:
    """EPIC-008-T6: Error handling and recovery test."""

    @pytest.mark.asyncio
    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    async def test_rate_limit_error_handling(
        self,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
        claude_fixtures_dir: Path,
    ) -> None:
        """Test rate limit error handling with retry logic."""
        mock_anthropic_module.__version__ = "0.77.0"

        # Load error fixture
        fixture_file = claude_fixtures_dir / "error_responses.json"
        with open(fixture_file) as f:
            error_responses = json.load(f)

        # Create mock client
        mock_client = Mock()
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(
            return_value=Mock(data=[Mock(id="claude-3-5-sonnet-latest")])
        )

        # Import the actual exception class for proper raising
        try:
            from anthropic import RateLimitError
        except ImportError:
            # Fallback if not available
            class RateLimitError(Exception):  # type: ignore[no-redef]
                pass

        call_count = [0]

        async def create_message(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                # Fail first 2 times with rate limit
                error_data = error_responses["rate_limit"]
                mock_response = Mock(
                    status_code=error_data["status_code"],
                    headers={"retry-after": "0.01"},  # Short delay for test performance
                )
                raise RateLimitError(
                    error_data["error"]["message"],
                    response=mock_response,
                    body=error_data["error"],
                )
            else:
                # Succeed on 3rd attempt
                mock_msg = Mock()
                mock_msg.id = "msg_success"
                mock_msg.type = "message"
                mock_msg.role = "assistant"
                mock_msg.model = "claude-3-5-sonnet-latest"
                mock_msg.stop_reason = "tool_use"
                mock_msg.usage = Mock(input_tokens=50, output_tokens=60)

                mock_block = Mock()
                mock_block.type = "tool_use"
                mock_block.id = "toolu_success"
                mock_block.name = "emit_output"
                mock_block.input = {"result": "Success after retry"}
                mock_msg.content = [mock_block]

                return mock_msg

        mock_client.messages.create = AsyncMock(side_effect=create_message)
        mock_anthropic_class.return_value = mock_client

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

        provider = ClaudeProvider()
        engine = WorkflowEngine(workflow, provider)

        result = await engine.run({})

        # Should succeed after retries
        assert call_count[0] == 3
        assert "result" in result
        assert result["result"] == "Success after retry"

    @pytest.mark.asyncio
    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    async def test_auth_failure_no_retry(
        self,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
        claude_fixtures_dir: Path,
    ) -> None:
        """Test that authentication errors fail immediately without retry."""
        mock_anthropic_module.__version__ = "0.77.0"

        # Load error fixture
        fixture_file = claude_fixtures_dir / "error_responses.json"
        with open(fixture_file) as f:
            error_responses = json.load(f)

        # Create mock client
        mock_client = Mock()
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(
            return_value=Mock(data=[Mock(id="claude-3-5-sonnet-latest")])
        )

        try:
            from anthropic import AuthenticationError
        except ImportError:

            class AuthenticationError(Exception):  # type: ignore[no-redef]
                pass

        async def create_message(*args, **kwargs):
            error_data = error_responses["auth_failure"]
            raise AuthenticationError(
                error_data["error"]["message"],
                response=Mock(status_code=error_data["status_code"]),
                body=error_data["error"],
            )

        mock_client.messages.create = AsyncMock(side_effect=create_message)
        mock_anthropic_class.return_value = mock_client

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

        provider = ClaudeProvider()
        engine = WorkflowEngine(workflow, provider)

        # Should raise ProviderError without retries
        with pytest.raises((ProviderError, ExecutionError)):
            await engine.run({})


@pytest.mark.performance
class TestClaudePerformance:
    """EPIC-008-T8: Performance test for Claude non-streaming."""

    @pytest.mark.asyncio
    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    async def test_provider_overhead_baseline(
        self,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
    ) -> None:
        """Measure provider overhead with mock client (100 samples, statistical rigor)."""
        mock_anthropic_module.__version__ = "0.77.0"

        # Create mock client with instant responses
        mock_client = Mock()
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(
            return_value=Mock(data=[Mock(id="claude-3-5-sonnet-latest")])
        )

        async def create_message(*args, **kwargs):
            # Instant mock response
            mock_msg = Mock()
            mock_msg.id = "msg_perf_test"
            mock_msg.type = "message"
            mock_msg.role = "assistant"
            mock_msg.model = "claude-3-5-sonnet-latest"
            mock_msg.stop_reason = "tool_use"
            mock_msg.usage = Mock(input_tokens=10, output_tokens=20)

            mock_block = Mock()
            mock_block.type = "tool_use"
            mock_block.id = "toolu_perf"
            mock_block.name = "emit_output"
            mock_block.input = {"result": "test"}
            mock_msg.content = [mock_block]

            return mock_msg

        mock_client.messages.create = AsyncMock(side_effect=create_message)
        mock_anthropic_class.return_value = mock_client

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

        provider = ClaudeProvider()
        engine = WorkflowEngine(workflow, provider)

        # Measure 100 samples
        samples = []
        for _ in range(100):
            start = time.perf_counter()
            await engine.run({})
            duration = (time.perf_counter() - start) * 1000  # Convert to ms
            samples.append(duration)

        # Calculate statistics
        mean = sum(samples) / len(samples)
        samples_sorted = sorted(samples)
        p95 = samples_sorted[94]  # 95th percentile
        p99 = samples_sorted[98]  # 99th percentile

        # Assert performance criteria (from plan: <100ms mean, <150ms p95)
        assert mean < 100.0, f"Mean overhead {mean:.2f}ms exceeds 100ms threshold"
        assert p95 < 150.0, f"P95 overhead {p95:.2f}ms exceeds 150ms threshold"

        # Log results for baseline tracking
        print("\nPerformance Baseline (100 samples):")
        print(f"  Mean: {mean:.2f}ms")
        print(f"  P95:  {p95:.2f}ms")
        print(f"  P99:  {p99:.2f}ms")
