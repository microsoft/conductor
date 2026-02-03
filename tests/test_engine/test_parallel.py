"""Tests for parallel agent execution.

Tests cover:
- Parallel group execution with context isolation
- All three failure modes (fail_fast, continue_on_error, all_or_nothing)
- Output aggregation
- Error handling and reporting
- Context snapshot isolation
"""

import asyncio
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
from conductor.engine.workflow import (
    ParallelAgentError,
    ParallelGroupOutput,
    WorkflowEngine,
)
from conductor.exceptions import ExecutionError
from conductor.providers.base import AgentOutput


@pytest.fixture
def mock_provider():
    """Create a mock provider for testing."""
    provider = MagicMock()
    provider.execute = AsyncMock()
    return provider


@pytest.fixture
def simple_parallel_workflow() -> WorkflowConfig:
    """Create a workflow with a simple parallel group."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="parallel-test",
            entry_point="parallel_group_1",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            AgentDef(
                name="agent1",
                model="gpt-4",
                prompt="Task 1: {{ workflow.input.question }}",
                output={"result": OutputField(type="string")},
            ),
            AgentDef(
                name="agent2",
                model="gpt-4",
                prompt="Task 2: {{ workflow.input.question }}",
                output={"result": OutputField(type="string")},
            ),
        ],
        parallel=[
            ParallelGroup(
                name="parallel_group_1",
                agents=["agent1", "agent2"],
                failure_mode="fail_fast",
            ),
        ],
        output={
            "agent1_result": "{{ parallel_group_1.outputs.agent1.result }}",
            "agent2_result": "{{ parallel_group_1.outputs.agent2.result }}",
        },
    )


@pytest.mark.asyncio
async def test_parallel_group_execution_success(simple_parallel_workflow, mock_provider):
    """Test successful execution of parallel agents."""
    # Mock both agents to succeed
    mock_provider.execute.side_effect = [
        AgentOutput(
            content={"result": "Agent 1 result"},
            raw_response={},
            model="gpt-4",
            tokens_used=100,
        ),
        AgentOutput(
            content={"result": "Agent 2 result"},
            raw_response={},
            model="gpt-4",
            tokens_used=100,
        ),
    ]

    engine = WorkflowEngine(simple_parallel_workflow, mock_provider)
    result = await engine.run({"question": "Test question"})

    # Verify both agents were executed
    assert mock_provider.execute.call_count == 2

    # Verify output aggregation
    assert result["agent1_result"] == "Agent 1 result"
    assert result["agent2_result"] == "Agent 2 result"


@pytest.mark.asyncio
async def test_parallel_group_fail_fast_mode(simple_parallel_workflow, mock_provider):
    """Test fail_fast mode stops immediately on first failure."""

    # Mock first agent to fail
    async def mock_execute_side_effect(*args, **kwargs):
        # Simulate agent failure
        raise ExecutionError("Agent 1 failed", suggestion="Check configuration")

    mock_provider.execute.side_effect = mock_execute_side_effect

    engine = WorkflowEngine(simple_parallel_workflow, mock_provider)

    with pytest.raises(ExecutionError) as exc_info:
        await engine.run({"question": "Test question"})

    # Verify error message mentions parallel group
    assert "parallel" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_parallel_group_continue_on_error_mode(mock_provider):
    """Test continue_on_error mode collects errors and continues if one succeeds."""
    workflow = WorkflowConfig(
        workflow=WorkflowDef(
            name="parallel-continue",
            entry_point="parallel_group_1",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
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
                name="parallel_group_1",
                agents=["agent1", "agent2", "agent3"],
                failure_mode="continue_on_error",
            ),
        ],
        output={
            "agent2_result": "{{ parallel_group_1.outputs.agent2.result }}",
            "has_errors": "{{ parallel_group_1.errors | length > 0 }}",
        },
    )

    # Mock: agent1 fails, agent2 succeeds, agent3 fails
    call_count = 0

    async def mock_execute_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ExecutionError("Agent 1 failed")
        elif call_count == 2:
            return AgentOutput(
                content={"result": "Agent 2 success"},
                raw_response={},
                model="gpt-4",
                tokens_used=100,
            )
        else:
            raise ExecutionError("Agent 3 failed")

    mock_provider.execute.side_effect = mock_execute_side_effect

    engine = WorkflowEngine(workflow, mock_provider)
    result = await engine.run({})

    # Verify at least one agent succeeded
    assert "agent2_result" in result
    assert result["agent2_result"] == "Agent 2 success"


@pytest.mark.asyncio
async def test_parallel_group_all_or_nothing_mode_all_succeed(mock_provider):
    """Test all_or_nothing mode succeeds when all agents succeed."""
    workflow = WorkflowConfig(
        workflow=WorkflowDef(
            name="parallel-all-or-nothing",
            entry_point="parallel_group_1",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
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
        ],
        parallel=[
            ParallelGroup(
                name="parallel_group_1",
                agents=["agent1", "agent2"],
                failure_mode="all_or_nothing",
            ),
        ],
        output={
            "combined": (
                "{{ parallel_group_1.outputs.agent1.result }} + "
                "{{ parallel_group_1.outputs.agent2.result }}"
            ),
        },
    )

    # Mock both agents to succeed
    mock_provider.execute.side_effect = [
        AgentOutput(
            content={"result": "Result 1"},
            raw_response={},
            model="gpt-4",
            tokens_used=100,
        ),
        AgentOutput(
            content={"result": "Result 2"},
            raw_response={},
            model="gpt-4",
            tokens_used=100,
        ),
    ]

    engine = WorkflowEngine(workflow, mock_provider)
    result = await engine.run({})

    # Verify workflow succeeded
    assert "combined" in result


@pytest.mark.asyncio
async def test_parallel_group_all_or_nothing_mode_one_fails(mock_provider):
    """Test all_or_nothing mode fails when any agent fails."""
    workflow = WorkflowConfig(
        workflow=WorkflowDef(
            name="parallel-all-or-nothing",
            entry_point="parallel_group_1",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
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
        ],
        parallel=[
            ParallelGroup(
                name="parallel_group_1",
                agents=["agent1", "agent2"],
                failure_mode="all_or_nothing",
            ),
        ],
        output={
            "combined": "Result",
        },
    )

    # Mock: agent1 succeeds, agent2 fails
    call_count = 0

    async def mock_execute_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return AgentOutput(
                content={"result": "Result 1"},
                raw_response={},
                model="gpt-4",
                tokens_used=100,
            )
        else:
            raise ExecutionError("Agent 2 failed")

    mock_provider.execute.side_effect = mock_execute_side_effect

    engine = WorkflowEngine(workflow, mock_provider)

    with pytest.raises(ExecutionError) as exc_info:
        await engine.run({})

    # Verify error message mentions failure
    assert "failed" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_context_isolation_in_parallel_execution(mock_provider):
    """Test that parallel agents receive isolated context snapshots."""
    workflow = WorkflowConfig(
        workflow=WorkflowDef(
            name="parallel-isolation",
            entry_point="parallel_group_1",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            AgentDef(
                name="agent1",
                model="gpt-4",
                prompt="Task 1: {{ workflow.input.value }}",
                output={"result": OutputField(type="string")},
            ),
            AgentDef(
                name="agent2",
                model="gpt-4",
                prompt="Task 2: {{ workflow.input.value }}",
                output={"result": OutputField(type="string")},
            ),
        ],
        parallel=[
            ParallelGroup(
                name="parallel_group_1",
                agents=["agent1", "agent2"],
                failure_mode="fail_fast",
            ),
        ],
        output={
            "done": "true",
        },
    )

    # Track contexts passed to each agent
    contexts_passed = []

    async def mock_execute_side_effect(agent, context, **kwargs):
        # Store a copy of the context
        contexts_passed.append(context.copy())
        return AgentOutput(
            content={"result": f"Result from {agent.name}"},
            raw_response={},
            model="gpt-4",
            tokens_used=100,
        )

    mock_provider.execute.side_effect = mock_execute_side_effect

    engine = WorkflowEngine(workflow, mock_provider)
    await engine.run({"value": "test_value"})

    # Verify both agents received contexts
    assert len(contexts_passed) == 2

    # Verify both contexts have the same workflow input
    for ctx in contexts_passed:
        assert "workflow" in ctx
        assert ctx["workflow"]["input"]["value"] == "test_value"


@pytest.mark.asyncio
async def test_parallel_agents_execute_concurrently(mock_provider):
    """Test that parallel agents actually execute concurrently."""
    workflow = WorkflowConfig(
        workflow=WorkflowDef(
            name="parallel-concurrent",
            entry_point="parallel_group_1",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
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
        ],
        parallel=[
            ParallelGroup(
                name="parallel_group_1",
                agents=["agent1", "agent2"],
                failure_mode="fail_fast",
            ),
        ],
        output={"done": "true"},
    )

    # Track execution timing
    execution_times = []

    async def mock_execute_with_delay(agent, context, **kwargs):
        start = asyncio.get_event_loop().time()
        await asyncio.sleep(0.1)  # Simulate work
        end = asyncio.get_event_loop().time()
        execution_times.append((agent.name, start, end))
        return AgentOutput(
            content={"result": f"Result from {agent.name}"},
            raw_response={},
            model="gpt-4",
            tokens_used=100,
        )

    mock_provider.execute.side_effect = mock_execute_with_delay

    import time

    overall_start = time.time()

    engine = WorkflowEngine(workflow, mock_provider)
    await engine.run({})

    overall_end = time.time()
    overall_duration = overall_end - overall_start

    # If agents ran sequentially, it would take ~0.2s
    # If parallel, it should take ~0.1s
    # Allow some overhead, but should be much less than 0.2s
    assert overall_duration < 0.18, "Agents should execute in parallel"

    # Verify both agents executed
    assert len(execution_times) == 2


@pytest.mark.asyncio
async def test_parallel_group_output_structure(simple_parallel_workflow, mock_provider):
    """Test that parallel group output has correct structure."""
    mock_provider.execute.side_effect = [
        AgentOutput(
            content={"result": "Result 1", "extra": "data1"},
            raw_response={},
            model="gpt-4",
            tokens_used=100,
        ),
        AgentOutput(
            content={"result": "Result 2", "extra": "data2"},
            raw_response={},
            model="gpt-4",
            tokens_used=100,
        ),
    ]

    engine = WorkflowEngine(simple_parallel_workflow, mock_provider)
    await engine.run({"question": "Test"})

    # Verify parallel group output is stored in context
    parallel_output = engine.context.agent_outputs.get("parallel_group_1")
    assert parallel_output is not None
    assert "outputs" in parallel_output
    assert "errors" in parallel_output

    # Verify outputs structure
    assert "agent1" in parallel_output["outputs"]
    assert "agent2" in parallel_output["outputs"]
    assert parallel_output["outputs"]["agent1"]["result"] == "Result 1"
    assert parallel_output["outputs"]["agent2"]["result"] == "Result 2"

    # Verify errors dict is present (even if empty)
    assert isinstance(parallel_output["errors"], dict)


def test_parallel_agent_error_dataclass():
    """Test ParallelAgentError dataclass."""
    error = ParallelAgentError(
        agent_name="test_agent",
        exception_type="ValidationError",
        message="Output validation failed",
        suggestion="Check output schema",
    )

    assert error.agent_name == "test_agent"
    assert error.exception_type == "ValidationError"
    assert error.message == "Output validation failed"
    assert error.suggestion == "Check output schema"


def test_parallel_group_output_dataclass():
    """Test ParallelGroupOutput dataclass."""
    output = ParallelGroupOutput(
        outputs={"agent1": {"result": "Success"}},
        errors={
            "agent2": ParallelAgentError(
                agent_name="agent2",
                exception_type="ExecutionError",
                message="Failed",
                suggestion=None,
            )
        },
    )

    assert len(output.outputs) == 1
    assert len(output.errors) == 1
    assert output.outputs["agent1"]["result"] == "Success"
    assert output.errors["agent2"].agent_name == "agent2"


@pytest.mark.asyncio
async def test_error_message_format_fail_fast_with_agent_name(
    simple_parallel_workflow, mock_provider
):
    """Test error message format in fail_fast mode includes agent name."""
    # First agent succeeds, second agent fails
    mock_provider.execute.side_effect = [
        AgentOutput(
            content={"result": "Agent 1 result"},
            raw_response={},
            model="gpt-4",
            tokens_used=100,
        ),
        Exception("Agent execution failed"),
    ]

    engine = WorkflowEngine(simple_parallel_workflow, mock_provider)

    with pytest.raises(ExecutionError) as exc_info:
        await engine.run({"question": "test"})

    error_msg = str(exc_info.value)
    # Should include the parallel group name
    assert "parallel_group_1" in error_msg
    # Should include "fail_fast mode"
    assert "fail_fast" in error_msg
    # Should include the agent name that failed
    assert "agent" in error_msg.lower()


@pytest.mark.asyncio
async def test_error_message_format_continue_on_error_all_failed(
    mock_provider,
):
    """Test error message format when all agents fail in continue_on_error mode."""
    workflow = WorkflowConfig(
        workflow=WorkflowDef(
            name="parallel-test",
            entry_point="parallel_group_1",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
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
        ],
        parallel=[
            ParallelGroup(
                name="parallel_group_1",
                agents=["agent1", "agent2"],
                failure_mode="continue_on_error",
            ),
        ],
    )

    # Both agents fail
    mock_provider.execute.side_effect = [
        Exception("Agent 1 failed"),
        Exception("Agent 2 failed"),
    ]

    engine = WorkflowEngine(workflow, mock_provider)

    with pytest.raises(ExecutionError) as exc_info:
        await engine.run({})

    error_msg = str(exc_info.value)
    # Should include all failed agent names
    assert "agent1" in error_msg
    assert "agent2" in error_msg
    # Should include error details
    assert "Agent 1 failed" in error_msg
    assert "Agent 2 failed" in error_msg
    # Should mention that all agents failed
    assert "All agents" in error_msg


@pytest.mark.asyncio
async def test_error_message_format_all_or_nothing_with_suggestions(
    mock_provider,
):
    """Test error message format includes suggestions in all_or_nothing mode."""
    workflow = WorkflowConfig(
        workflow=WorkflowDef(
            name="parallel-test",
            entry_point="parallel_group_1",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
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
        ],
        parallel=[
            ParallelGroup(
                name="parallel_group_1",
                agents=["agent1", "agent2"],
                failure_mode="all_or_nothing",
            ),
        ],
    )

    # First succeeds, second fails with suggestion
    error_with_suggestion = Exception("Validation failed")
    error_with_suggestion.suggestion = "Check input schema"  # type: ignore

    mock_provider.execute.side_effect = [
        AgentOutput(
            content={"result": "Success"},
            raw_response={},
            model="gpt-4",
            tokens_used=100,
        ),
        error_with_suggestion,
    ]

    engine = WorkflowEngine(workflow, mock_provider)

    with pytest.raises(ExecutionError) as exc_info:
        await engine.run({})

    error_msg = str(exc_info.value)
    # Should include agent name and error
    assert "agent2" in error_msg
    assert "Validation failed" in error_msg
    # Should include the suggestion
    assert "Check input schema" in error_msg
    # Should mention counts
    assert "1 succeeded" in error_msg
    assert "1 failed" in error_msg


@pytest.mark.asyncio
async def test_error_message_distinguishes_exception_types(
    mock_provider,
):
    """Test that error messages show the exception type."""
    workflow = WorkflowConfig(
        workflow=WorkflowDef(
            name="parallel-test",
            entry_point="parallel_group_1",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
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
        ],
        parallel=[
            ParallelGroup(
                name="parallel_group_1",
                agents=["agent1", "agent2"],
                failure_mode="continue_on_error",
            ),
        ],
    )

    # Different exception types
    mock_provider.execute.side_effect = [
        ValueError("Invalid value"),
        KeyError("Missing key"),
    ]

    engine = WorkflowEngine(workflow, mock_provider)

    with pytest.raises(ExecutionError) as exc_info:
        await engine.run({})

    error_msg = str(exc_info.value)
    # Should show exception types
    assert "ValueError" in error_msg
    assert "KeyError" in error_msg
    # Should show messages
    assert "Invalid value" in error_msg
    assert "Missing key" in error_msg


class TestParallelGroupRouting:
    """Tests for routing to and from parallel groups."""

    @pytest.mark.asyncio
    async def test_route_to_parallel_group(self, mock_provider: MagicMock) -> None:
        """Test routing from agent to parallel group."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="route-to-parallel",
                entry_point="planner",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="planner",
                    model="gpt-4",
                    prompt="Create plan",
                    output={"plan": OutputField(type="string")},
                    routes=[RouteDef(to="validators")],
                ),
                AgentDef(
                    name="validator1",
                    model="gpt-4",
                    prompt="Validate plan",
                    output={"valid": OutputField(type="boolean")},
                ),
                AgentDef(
                    name="validator2",
                    model="gpt-4",
                    prompt="Check plan",
                    output={"checked": OutputField(type="boolean")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="validators",
                    agents=["validator1", "validator2"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        mock_provider.execute.side_effect = [
            AgentOutput(content={"plan": "test plan"}, raw_response={}, model="gpt-4"),
            AgentOutput(content={"valid": True}, raw_response={}, model="gpt-4"),
            AgentOutput(content={"checked": True}, raw_response={}, model="gpt-4"),
        ]

        engine = WorkflowEngine(workflow, mock_provider)
        result = await engine.run({})

        # Planner should execute, then parallel group
        assert mock_provider.execute.call_count == 3
        assert result is not None

    @pytest.mark.asyncio
    async def test_route_from_parallel_group(self, mock_provider: MagicMock) -> None:
        """Test routing from parallel group to downstream agent."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="route-from-parallel",
                entry_point="parallel_tasks",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="task1",
                    model="gpt-4",
                    prompt="Task 1",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="task2",
                    model="gpt-4",
                    prompt="Task 2",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="aggregator",
                    model="gpt-4",
                    prompt="Aggregate results",
                    output={"summary": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_tasks",
                    agents=["task1", "task2"],
                    routes=[RouteDef(to="aggregator")],
                ),
            ],
        )

        mock_provider.execute.side_effect = [
            AgentOutput(content={"result": "result1"}, raw_response={}, model="gpt-4"),
            AgentOutput(content={"result": "result2"}, raw_response={}, model="gpt-4"),
            AgentOutput(content={"summary": "combined"}, raw_response={}, model="gpt-4"),
        ]

        engine = WorkflowEngine(workflow, mock_provider)
        result = await engine.run({})

        # Parallel tasks should execute, then aggregator
        assert mock_provider.execute.call_count == 3
        assert result is not None

    @pytest.mark.asyncio
    async def test_conditional_route_from_parallel_group(self, mock_provider: MagicMock) -> None:
        """Test conditional routing from parallel group based on outputs."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="conditional-parallel-route",
                entry_point="checkers",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="check1",
                    model="gpt-4",
                    prompt="Check 1",
                    output={"passed": OutputField(type="boolean")},
                ),
                AgentDef(
                    name="check2",
                    model="gpt-4",
                    prompt="Check 2",
                    output={"passed": OutputField(type="boolean")},
                ),
                AgentDef(
                    name="success_handler",
                    model="gpt-4",
                    prompt="Handle success",
                    output={"message": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
                AgentDef(
                    name="failure_handler",
                    model="gpt-4",
                    prompt="Handle failure",
                    output={"message": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="checkers",
                    agents=["check1", "check2"],
                    routes=[
                        RouteDef(
                            to="success_handler",
                            when=(
                                "{{ output.outputs.check1.passed and "
                                "output.outputs.check2.passed }}"
                            ),
                        ),
                        RouteDef(to="failure_handler"),
                    ],
                ),
            ],
        )

        # Test success path
        mock_provider.execute.side_effect = [
            AgentOutput(content={"passed": True}, raw_response={}, model="gpt-4"),
            AgentOutput(content={"passed": True}, raw_response={}, model="gpt-4"),
            AgentOutput(content={"message": "All checks passed"}, raw_response={}, model="gpt-4"),
        ]

        engine = WorkflowEngine(workflow, mock_provider)
        await engine.run({})

        # Should execute parallel group then success_handler
        assert mock_provider.execute.call_count == 3

    @pytest.mark.asyncio
    async def test_parallel_group_default_route_to_end(self, mock_provider: MagicMock) -> None:
        """Test parallel group with no routes defaults to $end."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-default-end",
                entry_point="parallel_tasks",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="task1",
                    model="gpt-4",
                    prompt="Task 1",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="task2",
                    model="gpt-4",
                    prompt="Task 2",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_tasks",
                    agents=["task1", "task2"],
                    # No routes - should default to $end
                ),
            ],
        )

        mock_provider.execute.side_effect = [
            AgentOutput(content={"result": "result1"}, raw_response={}, model="gpt-4"),
            AgentOutput(content={"result": "result2"}, raw_response={}, model="gpt-4"),
        ]

        engine = WorkflowEngine(workflow, mock_provider)
        result = await engine.run({})

        # Should execute parallel group and end
        assert mock_provider.execute.call_count == 2
        assert result is not None
