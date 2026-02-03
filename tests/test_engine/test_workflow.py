"""Integration tests for WorkflowEngine.

Tests cover:
- Linear workflow execution
- Context passing between agents
- Output template rendering
- Routing between agents
- Error handling
"""

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    GateOption,
    LimitsConfig,
    OutputField,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ExecutionError
from conductor.providers.copilot import CopilotProvider


@pytest.fixture
def simple_workflow_config() -> WorkflowConfig:
    """Create a simple single-agent workflow config."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="simple-workflow",
            entry_point="answerer",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            AgentDef(
                name="answerer",
                model="gpt-4",
                prompt="Answer: {{ workflow.input.question }}",
                output={"answer": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={
            "answer": "{{ answerer.output.answer }}",
        },
    )


@pytest.fixture
def multi_agent_workflow_config() -> WorkflowConfig:
    """Create a multi-agent workflow config."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="multi-agent-workflow",
            entry_point="planner",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            AgentDef(
                name="planner",
                model="gpt-4",
                prompt="Plan for: {{ workflow.input.goal }}",
                output={"plan": OutputField(type="string")},
                routes=[RouteDef(to="executor")],
            ),
            AgentDef(
                name="executor",
                model="gpt-4",
                prompt="Execute plan: {{ planner.output.plan }}",
                output={"result": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={
            "plan": "{{ planner.output.plan }}",
            "result": "{{ executor.output.result }}",
        },
    )


@pytest.fixture
def conditional_workflow_config() -> WorkflowConfig:
    """Create a workflow with conditional routing."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="conditional-workflow",
            entry_point="checker",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            AgentDef(
                name="checker",
                model="gpt-4",
                prompt="Check: {{ workflow.input.value }}",
                output={
                    "is_valid": OutputField(type="boolean"),
                    "message": OutputField(type="string"),
                },
                routes=[
                    RouteDef(to="success_handler", when="{{ output.is_valid }}"),
                    RouteDef(to="error_handler"),
                ],
            ),
            AgentDef(
                name="success_handler",
                model="gpt-4",
                prompt="Handle success: {{ checker.output.message }}",
                output={"result": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
            AgentDef(
                name="error_handler",
                model="gpt-4",
                prompt="Handle error: {{ checker.output.message }}",
                output={"result": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={
            "result": "{{ context.history[-1] }}",
        },
    )


class TestWorkflowEngineBasic:
    """Basic WorkflowEngine tests."""

    @pytest.mark.asyncio
    async def test_simple_workflow_execution(self, simple_workflow_config: WorkflowConfig) -> None:
        """Test executing a simple single-agent workflow."""

        def mock_handler(agent, prompt, context):
            return {"answer": "Python is a programming language"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(simple_workflow_config, provider)

        result = await engine.run({"question": "What is Python?"})

        assert "answer" in result
        assert result["answer"] == "Python is a programming language"

    @pytest.mark.asyncio
    async def test_multi_agent_workflow_execution(
        self, multi_agent_workflow_config: WorkflowConfig
    ) -> None:
        """Test executing a multi-agent workflow."""
        responses = {
            "planner": {"plan": "Step 1: Do X, Step 2: Do Y"},
            "executor": {"result": "Successfully completed X and Y"},
        }

        def mock_handler(agent, prompt, context):
            return responses[agent.name]

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(multi_agent_workflow_config, provider)

        result = await engine.run({"goal": "Complete the task"})

        assert result["plan"] == "Step 1: Do X, Step 2: Do Y"
        assert result["result"] == "Successfully completed X and Y"

    @pytest.mark.asyncio
    async def test_workflow_stores_context(
        self, multi_agent_workflow_config: WorkflowConfig
    ) -> None:
        """Test that workflow stores context between agents."""
        received_contexts = []

        def mock_handler(agent, prompt, context):
            received_contexts.append((agent.name, context.copy()))
            return {"plan": "the plan"} if agent.name == "planner" else {"result": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(multi_agent_workflow_config, provider)

        await engine.run({"goal": "test"})

        # First agent should see workflow inputs
        assert received_contexts[0][0] == "planner"
        assert received_contexts[0][1]["workflow"]["input"]["goal"] == "test"

        # Second agent should see planner's output
        assert received_contexts[1][0] == "executor"
        assert received_contexts[1][1]["planner"]["output"]["plan"] == "the plan"


class TestWorkflowEngineContextModes:
    """Tests for different context accumulation modes."""

    @pytest.mark.asyncio
    async def test_last_only_mode(self) -> None:
        """Test last_only context mode."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="last-only-workflow",
                entry_point="agent1",
                context=ContextConfig(mode="last_only"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="First",
                    output={"out1": OutputField(type="string")},
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Second",
                    output={"out2": OutputField(type="string")},
                    routes=[RouteDef(to="agent3")],
                ),
                AgentDef(
                    name="agent3",
                    model="gpt-4",
                    prompt="Third",
                    output={"out3": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"final": "{{ context.iteration }}"},
        )

        received_contexts = []

        def mock_handler(agent, prompt, context):
            received_contexts.append((agent.name, context.copy()))
            return {f"out{agent.name[-1]}": f"output_{agent.name[-1]}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        await engine.run({})

        # Agent3 should only see agent2's output (last_only mode)
        agent3_context = received_contexts[2][1]
        assert "agent2" in agent3_context
        assert "agent1" not in agent3_context

    @pytest.mark.asyncio
    async def test_explicit_mode(self) -> None:
        """Test explicit context mode."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="explicit-workflow",
                entry_point="agent1",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="First",
                    input=["workflow.input.goal"],
                    output={"out1": OutputField(type="string")},
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Second",
                    input=["agent1.output"],
                    output={"out2": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "done"},
        )

        received_contexts = []

        def mock_handler(agent, prompt, context):
            received_contexts.append((agent.name, context.copy()))
            return {f"out{agent.name[-1]}": f"output_{agent.name[-1]}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        await engine.run({"goal": "test", "other": "ignored"})

        # Agent2 should only see agent1's output (explicit mode)
        agent2_context = received_contexts[1][1]
        assert "agent1" in agent2_context
        # Workflow.input.goal should not be in agent2's context since it's not in input list
        assert "other" not in agent2_context.get("workflow", {}).get("input", {})


class TestWorkflowEngineRouting:
    """Tests for workflow routing."""

    @pytest.mark.asyncio
    async def test_conditional_route_true(
        self, conditional_workflow_config: WorkflowConfig
    ) -> None:
        """Test conditional routing when condition is true."""

        def mock_handler(agent, prompt, context):
            if agent.name == "checker":
                return {"is_valid": True, "message": "All good"}
            return {"result": f"Handled by {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(conditional_workflow_config, provider)

        await engine.run({"value": "test"})

        # Should have routed to success_handler
        assert "success_handler" in engine.context.execution_history
        assert "error_handler" not in engine.context.execution_history

    @pytest.mark.asyncio
    async def test_conditional_route_false(
        self, conditional_workflow_config: WorkflowConfig
    ) -> None:
        """Test conditional routing when condition is false."""

        def mock_handler(agent, prompt, context):
            if agent.name == "checker":
                return {"is_valid": False, "message": "Invalid"}
            return {"result": f"Handled by {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(conditional_workflow_config, provider)

        await engine.run({"value": "test"})

        # Should have routed to error_handler (fallthrough)
        assert "error_handler" in engine.context.execution_history
        assert "success_handler" not in engine.context.execution_history

    @pytest.mark.asyncio
    async def test_no_routes_ends_workflow(self) -> None:
        """Test that agent with no routes ends the workflow."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="no-routes",
                entry_point="agent1",
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    output={"result": OutputField(type="string")},
                    routes=[],  # No routes
                ),
            ],
            output={"result": "{{ agent1.output.result }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        assert result["result"] == "done"


class TestWorkflowEngineErrors:
    """Tests for error handling."""

    @pytest.mark.asyncio
    async def test_missing_agent_raises_error(self, simple_workflow_config: WorkflowConfig) -> None:
        """Test that missing entry point agent raises error."""
        simple_workflow_config.workflow.entry_point = "nonexistent"
        # Need to bypass the validation since we're modifying after creation
        simple_workflow_config.agents = simple_workflow_config.agents

        def mock_handler(agent, prompt, context):
            return {"answer": "test"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(simple_workflow_config, provider)

        with pytest.raises(
            ExecutionError, match="Agent, parallel group, or for-each group not found"
        ):
            await engine.run({"question": "test"})

    @pytest.mark.asyncio
    async def test_execution_summary(self, multi_agent_workflow_config: WorkflowConfig) -> None:
        """Test getting execution summary."""

        def mock_handler(agent, prompt, context):
            if agent.name == "planner":
                return {"plan": "the plan"}
            return {"result": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(multi_agent_workflow_config, provider)

        await engine.run({"goal": "test"})

        summary = engine.get_execution_summary()

        assert summary["iterations"] == 2
        assert summary["agents_executed"] == ["planner", "executor"]
        assert summary["context_mode"] == "accumulate"

    @pytest.mark.asyncio
    async def test_execution_summary_with_parallel_groups(self) -> None:
        """Test execution summary includes parallel group statistics."""
        from conductor.config.schema import ParallelGroup

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-summary-test",
                entry_point="coordinator",
            ),
            agents=[
                AgentDef(
                    name="coordinator",
                    model="gpt-4",
                    prompt="Start",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="parallel_tasks")],
                ),
                AgentDef(
                    name="task_a",
                    model="gpt-4",
                    prompt="Task A",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="task_b",
                    model="gpt-4",
                    prompt="Task B",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="task_c",
                    model="gpt-4",
                    prompt="Task C",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_tasks",
                    agents=["task_a", "task_b", "task_c"],
                    failure_mode="fail_fast",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "done"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": f"Output from {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        await engine.run({})

        summary = engine.get_execution_summary()

        # Verify basic stats
        assert summary["iterations"] == 4  # coordinator + 3 parallel agents
        assert "coordinator" in summary["agents_executed"]
        assert "parallel_tasks" in summary["agents_executed"]

        # Verify parallel group stats
        assert "parallel_groups_executed" in summary
        assert summary["parallel_groups_executed"] == ["parallel_tasks"]
        assert "parallel_agents_count" in summary
        assert summary["parallel_agents_count"] == 3


class TestWorkflowEngineOutputTemplates:
    """Tests for output template rendering."""

    @pytest.mark.asyncio
    async def test_output_template_with_json(self) -> None:
        """Test output template with json filter."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="json-output",
                entry_point="agent1",
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    output={
                        "data_list": OutputField(type="array"),
                    },
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "data_list": "{{ agent1.output.data_list | json }}",
            },
        )

        def mock_handler(agent, prompt, context):
            return {"data_list": ["a", "b", "c"]}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        # The json filter should produce valid JSON that gets parsed back
        assert result["data_list"] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_output_template_numeric(self) -> None:
        """Test output template with numeric value."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="numeric-output",
                entry_point="agent1",
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Count",
                    output={"count": OutputField(type="number")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "total": "{{ agent1.output.count }}",
            },
        )

        def mock_handler(agent, prompt, context):
            return {"count": 42}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        assert result["total"] == 42


class TestWorkflowEngineLoopBack:
    """Tests for loop-back routing patterns."""

    @pytest.mark.asyncio
    async def test_simple_loop_with_iteration_limit(self) -> None:
        """Test a simple loop that terminates after iterations."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="loop-workflow",
                entry_point="refiner",
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="refiner",
                    model="gpt-4",
                    prompt="Refine iteration {{ context.iteration }}",
                    output={
                        "quality": OutputField(type="number"),
                        "result": OutputField(type="string"),
                    },
                    routes=[
                        RouteDef(to="$end", when="{{ output.quality >= 8 }}"),
                        RouteDef(to="refiner"),  # Loop back
                    ],
                ),
            ],
            output={
                "result": "{{ refiner.output.result }}",
                "iterations": "{{ context.iteration }}",
            },
        )

        iteration_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal iteration_count
            iteration_count += 1
            # Quality improves with each iteration
            quality = 5 + iteration_count
            return {"quality": quality, "result": f"Refined {iteration_count} times"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        # Should have looped until quality >= 8
        assert iteration_count == 3  # quality: 6, 7, 8
        assert result["result"] == "Refined 3 times"

    @pytest.mark.asyncio
    async def test_loop_with_arithmetic_condition(self) -> None:
        """Test loop with arithmetic expression condition."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="arithmetic-loop",
                entry_point="counter",
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
            output={"final_count": "{{ counter.output.count }}"},
        )

        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            return {"count": call_count}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        assert call_count == 3
        assert result["final_count"] == 3

    @pytest.mark.asyncio
    async def test_loop_back_to_different_agent(self) -> None:
        """Test loop-back to a different previously executed agent."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="multi-agent-loop",
                entry_point="drafter",
            ),
            agents=[
                AgentDef(
                    name="drafter",
                    model="gpt-4",
                    prompt="Draft content",
                    output={"draft": OutputField(type="string")},
                    routes=[RouteDef(to="reviewer")],
                ),
                AgentDef(
                    name="reviewer",
                    model="gpt-4",
                    prompt="Review: {{ drafter.output.draft }}",
                    output={
                        "approved": OutputField(type="boolean"),
                        "feedback": OutputField(type="string"),
                    },
                    routes=[
                        RouteDef(to="$end", when="{{ output.approved }}"),
                        RouteDef(to="drafter"),  # Loop back to drafter
                    ],
                ),
            ],
            output={"final_draft": "{{ drafter.output.draft }}"},
        )

        loop_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal loop_count
            if agent.name == "drafter":
                loop_count += 1
                return {"draft": f"Draft v{loop_count}"}
            else:  # reviewer
                # Approve on second review
                return {
                    "approved": loop_count >= 2,
                    "feedback": "Needs work" if loop_count < 2 else "Approved",
                }

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        assert loop_count == 2
        assert result["final_draft"] == "Draft v2"
        # Verify execution history shows the loop
        summary = engine.get_execution_summary()
        assert summary["agents_executed"] == ["drafter", "reviewer", "drafter", "reviewer"]

    @pytest.mark.asyncio
    async def test_iteration_tracking_across_loops(self) -> None:
        """Test that iteration count is tracked across loop iterations."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="iteration-tracking",
                entry_point="agent",
            ),
            agents=[
                AgentDef(
                    name="agent",
                    model="gpt-4",
                    prompt="Iteration: {{ context.iteration }}",
                    output={"done": OutputField(type="boolean")},
                    routes=[
                        RouteDef(to="$end", when="{{ output.done }}"),
                        RouteDef(to="agent"),
                    ],
                ),
            ],
            output={"total_iterations": "{{ context.iteration }}"},
        )

        received_iterations = []

        def mock_handler(agent, prompt, context):
            received_iterations.append(context.get("context", {}).get("iteration", 0))
            return {"done": len(received_iterations) >= 3}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        # Iterations should be tracked
        assert len(received_iterations) == 3
        assert result["total_iterations"] == 3


class TestWorkflowEngineRouterIntegration:
    """Tests for Router integration with WorkflowEngine."""

    @pytest.mark.asyncio
    async def test_route_output_transform(self) -> None:
        """Test that route output transforms are applied on $end."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="output-transform",
                entry_point="processor",
            ),
            agents=[
                AgentDef(
                    name="processor",
                    model="gpt-4",
                    prompt="Process",
                    output={"value": OutputField(type="string")},
                    routes=[
                        RouteDef(
                            to="$end",
                            output={"transformed": "Transformed: {{ output.value }}"},
                        ),
                    ],
                ),
            ],
            output={
                "original": "{{ processor.output.value }}",
            },
        )

        def mock_handler(agent, prompt, context):
            return {"value": "test_value"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        assert result["original"] == "test_value"
        assert result["transformed"] == "Transformed: test_value"

    @pytest.mark.asyncio
    async def test_no_matching_routes_error(self) -> None:
        """Test that no matching routes raises clear error."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="no-match",
                entry_point="agent",
            ),
            agents=[
                AgentDef(
                    name="agent",
                    model="gpt-4",
                    prompt="Test",
                    output={"flag": OutputField(type="boolean")},
                    routes=[
                        RouteDef(to="$end", when="{{ output.flag }}"),
                        # Missing catch-all route!
                    ],
                ),
            ],
            output={},
        )

        def mock_handler(agent, prompt, context):
            return {"flag": False}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        with pytest.raises(ValueError, match="No matching route found"):
            await engine.run({})

    @pytest.mark.asyncio
    async def test_mixed_jinja_and_arithmetic_conditions(self) -> None:
        """Test workflow with mixed Jinja2 and arithmetic conditions."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mixed-conditions",
                entry_point="evaluator",
            ),
            agents=[
                AgentDef(
                    name="evaluator",
                    model="gpt-4",
                    prompt="Evaluate",
                    output={
                        "score": OutputField(type="number"),
                        "valid": OutputField(type="boolean"),
                    },
                    routes=[
                        RouteDef(to="high", when="score >= 8"),  # arithmetic
                        RouteDef(to="valid", when="{{ output.valid }}"),  # jinja
                        RouteDef(to="default"),
                    ],
                ),
                AgentDef(
                    name="high",
                    model="gpt-4",
                    prompt="High score",
                    routes=[RouteDef(to="$end")],
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="valid",
                    model="gpt-4",
                    prompt="Valid",
                    routes=[RouteDef(to="$end")],
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="default",
                    model="gpt-4",
                    prompt="Default",
                    routes=[RouteDef(to="$end")],
                    output={"result": OutputField(type="string")},
                ),
            ],
            output={"path": "{{ context.history[-1] }}"},
        )

        def mock_handler(agent, prompt, context):
            if agent.name == "evaluator":
                return {"score": 5, "valid": True}
            return {"result": agent.name}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        await engine.run({})

        # Score 5 < 8, but valid is True, so should go to 'valid'
        assert "valid" in engine.context.execution_history


class TestWorkflowEngineHumanGates:
    """Tests for human gate integration in workflows."""

    @pytest.fixture
    def human_gate_workflow_config(self) -> WorkflowConfig:
        """Create a workflow with a human gate."""
        return WorkflowConfig(
            workflow=WorkflowDef(
                name="human-gate-workflow",
                entry_point="drafter",
                runtime=RuntimeConfig(provider="copilot"),
            ),
            agents=[
                AgentDef(
                    name="drafter",
                    model="gpt-4",
                    prompt="Draft content",
                    output={"draft": OutputField(type="string")},
                    routes=[RouteDef(to="approval_gate")],
                ),
                AgentDef(
                    name="approval_gate",
                    type="human_gate",
                    prompt="Review the draft:\n\n{{ drafter.output.draft }}",
                    options=[
                        GateOption(
                            label="Approve",
                            value="approved",
                            route="publisher",
                        ),
                        GateOption(
                            label="Request changes",
                            value="changes_requested",
                            route="drafter",
                        ),
                        GateOption(
                            label="Reject",
                            value="rejected",
                            route="$end",
                        ),
                    ],
                ),
                AgentDef(
                    name="publisher",
                    model="gpt-4",
                    prompt="Publish: {{ drafter.output.draft }}",
                    output={"published": OutputField(type="boolean")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "draft": "{{ drafter.output.draft }}",
                "gate_result": "{{ approval_gate.output.selected }}",
            },
        )

    @pytest.mark.asyncio
    async def test_human_gate_with_skip_gates(
        self,
        human_gate_workflow_config: WorkflowConfig,
    ) -> None:
        """Test workflow with human gate using --skip-gates mode."""

        def mock_handler(agent, prompt, context):
            if agent.name == "drafter":
                return {"draft": "This is the draft content"}
            elif agent.name == "publisher":
                return {"published": True}
            return {}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(human_gate_workflow_config, provider, skip_gates=True)

        result = await engine.run({})

        # Should have auto-selected first option (Approve)
        assert result["gate_result"] == "approved"
        # Publisher should have been executed
        assert "publisher" in engine.context.execution_history

    @pytest.mark.asyncio
    async def test_human_gate_stores_selection_in_context(
        self,
        human_gate_workflow_config: WorkflowConfig,
    ) -> None:
        """Test that human gate selection is stored in context."""

        def mock_handler(agent, prompt, context):
            if agent.name == "drafter":
                return {"draft": "Draft content"}
            elif agent.name == "publisher":
                # Check that gate selection is in context
                gate_result = context.get("approval_gate", {})
                assert gate_result.get("output", {}).get("selected") == "approved"
                return {"published": True}
            return {}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(human_gate_workflow_config, provider, skip_gates=True)

        result = await engine.run({})

        # Verify gate result is in final output
        assert result["gate_result"] == "approved"

    @pytest.mark.asyncio
    async def test_human_gate_with_end_route(self) -> None:
        """Test human gate that routes to $end."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="gate-to-end",
                entry_point="gate",
            ),
            agents=[
                AgentDef(
                    name="gate",
                    type="human_gate",
                    prompt="Confirm action",
                    options=[
                        GateOption(
                            label="Cancel",
                            value="cancelled",
                            route="$end",
                        ),
                        GateOption(
                            label="Continue",
                            value="continued",
                            route="next",
                        ),
                    ],
                ),
                AgentDef(
                    name="next",
                    model="gpt-4",
                    prompt="Continue",
                    routes=[RouteDef(to="$end")],
                    output={"done": OutputField(type="boolean")},
                ),
            ],
            output={"status": "{{ gate.output.selected }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"done": True}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, skip_gates=True)

        result = await engine.run({})

        # First option routes to $end
        assert result["status"] == "cancelled"
        # "next" agent should NOT have been executed
        assert "next" not in engine.context.execution_history

    @pytest.mark.asyncio
    async def test_human_gate_iteration_tracking(
        self,
        human_gate_workflow_config: WorkflowConfig,
    ) -> None:
        """Test that human gates are tracked in iteration counts."""

        def mock_handler(agent, prompt, context):
            if agent.name == "drafter":
                return {"draft": "Draft"}
            return {"published": True}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(human_gate_workflow_config, provider, skip_gates=True)

        await engine.run({})

        summary = engine.get_execution_summary()
        # Should show drafter, approval_gate, publisher
        assert "approval_gate" in summary["agents_executed"]
        assert summary["iterations"] == 3


class TestWorkflowEngineLifecycleHooks:
    """Tests for lifecycle hooks execution."""

    @pytest.mark.asyncio
    async def test_on_start_hook_executed(self) -> None:
        """Test that on_start hook is executed at workflow start."""
        from conductor.config.schema import HooksConfig

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="hooks-workflow",
                entry_point="agent1",
                hooks=HooksConfig(
                    on_start="Workflow started with input: {{ workflow.input.question }}",
                ),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent1.output.result }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({"question": "test"})

        assert result["result"] == "done"

    @pytest.mark.asyncio
    async def test_on_complete_hook_executed(self) -> None:
        """Test that on_complete hook is executed on success."""
        from conductor.config.schema import HooksConfig

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="hooks-workflow",
                entry_point="agent1",
                hooks=HooksConfig(
                    on_complete="Workflow completed with result: {{ result }}",
                ),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent1.output.result }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": "success"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        assert result["result"] == "success"

    @pytest.mark.asyncio
    async def test_on_error_hook_executed(self) -> None:
        """Test that on_error hook is executed on failure."""
        from conductor.config.schema import HooksConfig
        from conductor.exceptions import ProviderError

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="hooks-workflow",
                entry_point="agent1",
                hooks=HooksConfig(
                    on_error="Error: {{ error.type }} - {{ error.message }}",
                ),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent1.output.result }}"},
        )

        def mock_handler(agent, prompt, context):
            # Simulate a provider error during execution
            raise ProviderError("API request failed", provider_name="copilot", status_code=500)

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        with pytest.raises(ProviderError, match="API request failed"):
            await engine.run({})

    @pytest.mark.asyncio
    async def test_hooks_not_defined(self) -> None:
        """Test that workflow works without hooks defined."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="no-hooks-workflow",
                entry_point="agent1",
                # No hooks defined
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent1.output.result }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        assert result["result"] == "done"


class TestWorkflowEngineContextTrimming:
    """Tests for context trimming integration in workflow engine."""

    @pytest.mark.asyncio
    async def test_context_trimming_with_max_tokens(self) -> None:
        """Test that context trimming is applied when max_tokens is set."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="trimming-workflow",
                entry_point="agent1",
                context=ContextConfig(
                    mode="accumulate",
                    max_tokens=500,  # Low limit to trigger trimming
                    trim_strategy="truncate",
                ),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="First",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Second",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="agent3")],
                ),
                AgentDef(
                    name="agent3",
                    model="gpt-4",
                    prompt="Third",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent3.output.result }}"},
        )

        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            # Return large content to trigger trimming
            return {"result": "A" * 200}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        assert call_count == 3
        # Workflow should complete even with trimming
        assert "result" in result

    @pytest.mark.asyncio
    async def test_context_trimming_not_applied_without_max_tokens(self) -> None:
        """Test that context is not trimmed when max_tokens is not set."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="no-trimming-workflow",
                entry_point="agent1",
                context=ContextConfig(
                    mode="accumulate",
                    # No max_tokens set
                ),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="First",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Second",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent2.output.result }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": "Large content: " + "X" * 500}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        await engine.run({})

        # Both agent outputs should be fully preserved
        assert len(engine.context.agent_outputs["agent1"]["result"]) > 500
        assert len(engine.context.agent_outputs["agent2"]["result"]) > 500


class TestExecutionPlanWithParallelGroups:
    """Tests for execution plan generation with parallel groups."""

    def test_execution_plan_with_parallel_group(self) -> None:
        """Test execution plan includes parallel groups."""
        from unittest.mock import MagicMock

        mock_provider = MagicMock()

        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="plan-with-parallel",
                entry_point="planner",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="planner",
                    model="gpt-4",
                    prompt="Plan",
                    routes=[RouteDef(to="validators")],
                ),
                AgentDef(
                    name="validator1",
                    model="gpt-4",
                    prompt="Validate 1",
                ),
                AgentDef(
                    name="validator2",
                    model="gpt-4",
                    prompt="Validate 2",
                ),
                AgentDef(
                    name="aggregator",
                    model="gpt-4",
                    prompt="Aggregate",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="validators",
                    agents=["validator1", "validator2"],
                    routes=[RouteDef(to="aggregator")],
                ),
            ],
        )

        engine = WorkflowEngine(workflow, mock_provider)
        plan = engine.build_execution_plan()

        # Should have 3 steps: planner, validators (parallel group), aggregator
        assert len(plan.steps) == 3

        # Check planner step
        planner_step = plan.steps[0]
        assert planner_step.agent_name == "planner"
        assert planner_step.agent_type == "agent"
        assert len(planner_step.routes) == 1
        assert planner_step.routes[0]["to"] == "validators"

        # Check validators parallel group step
        validators_step = plan.steps[1]
        assert validators_step.agent_name == "validators"
        assert validators_step.agent_type == "parallel_group"
        assert validators_step.model is None
        assert validators_step.parallel_agents == ["validator1", "validator2"]
        assert len(validators_step.routes) == 1
        assert validators_step.routes[0]["to"] == "aggregator"

        # Check aggregator step
        aggregator_step = plan.steps[2]
        assert aggregator_step.agent_name == "aggregator"
        assert aggregator_step.agent_type == "agent"

    def test_execution_plan_parallel_group_as_entry(self) -> None:
        """Test execution plan with parallel group as entry point."""
        from unittest.mock import MagicMock

        mock_provider = MagicMock()

        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-entry",
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
                ),
                AgentDef(
                    name="task2",
                    model="gpt-4",
                    prompt="Task 2",
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_tasks",
                    agents=["task1", "task2"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        engine = WorkflowEngine(workflow, mock_provider)
        plan = engine.build_execution_plan()

        # Should have 1 step: parallel_tasks
        assert len(plan.steps) == 1
        assert plan.steps[0].agent_name == "parallel_tasks"
        assert plan.steps[0].agent_type == "parallel_group"
        assert plan.steps[0].parallel_agents == ["task1", "task2"]


class TestResolveArrayReference:
    """Tests for _resolve_array_reference() method."""

    @pytest.fixture
    def workflow_engine_with_context(self) -> WorkflowEngine:
        """Create a WorkflowEngine with pre-populated context."""
        from unittest.mock import MagicMock

        mock_provider = MagicMock()

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-workflow",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                ),
            ],
        )
        engine = WorkflowEngine(config, mock_provider)

        # Populate context with test data (stored directly, will be wrapped by resolution logic)
        engine.context.store(
            "finder",
            {
                "kpis": [
                    {"kpi_id": "K1", "name": "Revenue"},
                    {"kpi_id": "K2", "name": "Profit"},
                    {"kpi_id": "K3", "name": "Growth"},
                ],
                "metadata": {"total": 3},
            },
        )

        engine.context.store(
            "nested_agent", {"data": {"items": ["item1", "item2", "item3"], "count": 3}}
        )

        return engine

    def test_resolve_valid_array_reference(self, workflow_engine_with_context: WorkflowEngine):
        """Test successful array resolution with valid path."""
        result = workflow_engine_with_context._resolve_array_reference("finder.output.kpis")

        assert isinstance(result, list)
        assert len(result) == 3
        assert result[0]["kpi_id"] == "K1"
        assert result[1]["kpi_id"] == "K2"
        assert result[2]["kpi_id"] == "K3"

    def test_resolve_nested_array_reference(self, workflow_engine_with_context: WorkflowEngine):
        """Test array resolution with deeper nesting."""
        result = workflow_engine_with_context._resolve_array_reference(
            "nested_agent.output.data.items"
        )

        assert isinstance(result, list)
        assert len(result) == 3
        assert result == ["item1", "item2", "item3"]

    def test_resolve_empty_array(self, workflow_engine_with_context: WorkflowEngine):
        """Test resolution of empty array (should succeed)."""
        # Add agent with empty array output
        workflow_engine_with_context.context.store("empty_agent", {"items": []})

        result = workflow_engine_with_context._resolve_array_reference("empty_agent.output.items")

        assert isinstance(result, list)
        assert len(result) == 0

    def test_resolve_invalid_format_too_short(self, workflow_engine_with_context: WorkflowEngine):
        """Test error for source with less than 3 parts."""
        with pytest.raises(ExecutionError) as exc_info:
            workflow_engine_with_context._resolve_array_reference("finder.output")

        assert "Invalid source reference format: 'finder.output'" in str(exc_info.value)
        assert "at least 3 parts" in str(exc_info.value.suggestion)

    def test_resolve_agent_not_found(self, workflow_engine_with_context: WorkflowEngine):
        """Test error when agent hasn't executed yet."""
        with pytest.raises(ExecutionError) as exc_info:
            workflow_engine_with_context._resolve_array_reference("nonexistent.output.items")

        assert "Agent 'nonexistent' output not found" in str(exc_info.value)
        assert "must execute before this for-each group" in str(exc_info.value.suggestion)
        assert "finder" in str(exc_info.value.suggestion)  # Shows executed agents

    def test_resolve_agent_not_found_no_executed_agents(self):
        """Test error when no agents have executed yet."""
        from unittest.mock import MagicMock

        mock_provider = MagicMock()

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-workflow",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                ),
            ],
        )
        engine = WorkflowEngine(config, mock_provider)

        with pytest.raises(ExecutionError) as exc_info:
            engine._resolve_array_reference("finder.output.items")

        assert "Agent 'finder' output not found" in str(exc_info.value)
        assert "must execute before this for-each group" in str(exc_info.value.suggestion)

    def test_resolve_field_not_found(self, workflow_engine_with_context: WorkflowEngine):
        """Test error when field doesn't exist in agent output."""
        with pytest.raises(ExecutionError) as exc_info:
            workflow_engine_with_context._resolve_array_reference("finder.output.nonexistent")

        assert "Field 'nonexistent' not found" in str(exc_info.value)
        assert "Available keys: ['kpis', 'metadata']" in str(exc_info.value.suggestion)

    def test_resolve_nested_field_not_found(self, workflow_engine_with_context: WorkflowEngine):
        """Test error when nested field doesn't exist."""
        with pytest.raises(ExecutionError) as exc_info:
            workflow_engine_with_context._resolve_array_reference(
                "nested_agent.output.data.missing"
            )

        assert "Field 'missing' not found in 'nested_agent.output.data'" in str(exc_info.value)
        assert "Available keys:" in str(exc_info.value.suggestion)

    def test_resolve_wrong_type_not_dict(self, workflow_engine_with_context: WorkflowEngine):
        """Test error when trying to navigate through non-dict value."""
        # Add agent with string output
        workflow_engine_with_context.context.store("string_agent", {"result": "just a string"})

        with pytest.raises(ExecutionError) as exc_info:
            workflow_engine_with_context._resolve_array_reference(
                "string_agent.output.result.items"
            )

        assert "Cannot navigate to 'items'" in str(exc_info.value)
        assert "is not a dictionary (type: str)" in str(exc_info.value)

    def test_resolve_wrong_type_not_list(self, workflow_engine_with_context: WorkflowEngine):
        """Test error when resolved value is not a list."""
        with pytest.raises(ExecutionError) as exc_info:
            workflow_engine_with_context._resolve_array_reference("finder.output.metadata")

        assert "resolved to dict, expected list" in str(exc_info.value)
        assert "Ensure 'finder.output.metadata' returns an array/list" in str(
            exc_info.value.suggestion
        )

    def test_resolve_wrong_type_string(self, workflow_engine_with_context: WorkflowEngine):
        """Test error when resolved value is a string instead of list."""
        workflow_engine_with_context.context.store("text_agent", {"text": "not an array"})

        with pytest.raises(ExecutionError) as exc_info:
            workflow_engine_with_context._resolve_array_reference("text_agent.output.text")

        assert "resolved to str, expected list" in str(exc_info.value)

    def test_resolve_wrong_type_number(self, workflow_engine_with_context: WorkflowEngine):
        """Test error when resolved value is a number instead of list."""
        workflow_engine_with_context.context.store("number_agent", {"count": 42})

        with pytest.raises(ExecutionError) as exc_info:
            workflow_engine_with_context._resolve_array_reference("number_agent.output.count")

        assert "resolved to int, expected list" in str(exc_info.value)

    def test_resolve_array_reference_accepts_tuple(
        self, workflow_engine_with_context: WorkflowEngine
    ):
        """Test that array resolution accepts tuples as valid array types."""
        workflow_engine_with_context.context.store(
            "tuple_agent", {"items": ("item1", "item2", "item3")}
        )

        result = workflow_engine_with_context._resolve_array_reference("tuple_agent.output.items")

        assert isinstance(result, tuple)
        assert len(result) == 3
        assert result == ("item1", "item2", "item3")


class TestInjectLoopVariables:
    """Tests for _inject_loop_variables() method."""

    @pytest.fixture
    def workflow_engine(self) -> WorkflowEngine:
        """Create a basic WorkflowEngine for testing."""
        from unittest.mock import MagicMock

        mock_provider = MagicMock()

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-workflow",
                entry_point="test",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="test",
                    model="gpt-4",
                    prompt="Test",
                    output={"result": OutputField(type="string")},
                ),
            ],
        )
        return WorkflowEngine(config, mock_provider)

    def test_inject_basic_loop_variables(self, workflow_engine: WorkflowEngine):
        """Test injection of basic loop variables (item, index)."""
        context = {
            "workflow": {"input": {"goal": "test"}},
            "context": {"iteration": 1},
        }

        item = {"kpi_id": "K1", "name": "Revenue"}
        workflow_engine._inject_loop_variables(
            context=context,
            var_name="kpi",
            item=item,
            index=0,
        )

        # Verify loop variable was injected
        assert "kpi" in context
        assert context["kpi"] == {"kpi_id": "K1", "name": "Revenue"}

        # Verify _index was injected
        assert "_index" in context
        assert context["_index"] == 0

        # Verify _key was not injected (not provided)
        assert "_key" not in context

        # Verify original context keys were preserved
        assert "workflow" in context
        assert context["workflow"]["input"]["goal"] == "test"

    def test_inject_loop_variables_with_key(self, workflow_engine: WorkflowEngine):
        """Test injection with key extraction enabled."""
        context = {
            "workflow": {"input": {}},
            "context": {"iteration": 1},
        }

        item = {"kpi_id": "KPI_123", "value": 100}
        workflow_engine._inject_loop_variables(
            context=context,
            var_name="kpi",
            item=item,
            index=5,
            key="KPI_123",
        )

        # Verify all three variables were injected
        assert context["kpi"] == {"kpi_id": "KPI_123", "value": 100}
        assert context["_index"] == 5
        assert context["_key"] == "KPI_123"

    def test_inject_loop_variables_with_string_item(self, workflow_engine: WorkflowEngine):
        """Test injection when item is a simple string (not a dict)."""
        context = {}

        workflow_engine._inject_loop_variables(
            context=context,
            var_name="color",
            item="red",
            index=2,
        )

        assert context["color"] == "red"
        assert context["_index"] == 2
        assert "_key" not in context

    def test_inject_loop_variables_with_number_item(self, workflow_engine: WorkflowEngine):
        """Test injection when item is a number."""
        context = {}

        workflow_engine._inject_loop_variables(
            context=context,
            var_name="value",
            item=42,
            index=10,
        )

        assert context["value"] == 42
        assert context["_index"] == 10

    def test_inject_loop_variables_with_list_item(self, workflow_engine: WorkflowEngine):
        """Test injection when item is a list (nested array)."""
        context = {}

        workflow_engine._inject_loop_variables(
            context=context,
            var_name="batch",
            item=["a", "b", "c"],
            index=3,
        )

        assert context["batch"] == ["a", "b", "c"]
        assert context["_index"] == 3

    def test_inject_loop_variables_overwrites_existing(self, workflow_engine: WorkflowEngine):
        """Test that injection overwrites existing variables in context."""
        context = {
            "kpi": "old_value",
            "_index": 999,
            "_key": "old_key",
        }

        workflow_engine._inject_loop_variables(
            context=context,
            var_name="kpi",
            item={"new": "value"},
            index=0,
            key="new_key",
        )

        # All should be overwritten
        assert context["kpi"] == {"new": "value"}
        assert context["_index"] == 0
        assert context["_key"] == "new_key"

    def test_inject_loop_variables_zero_index(self, workflow_engine: WorkflowEngine):
        """Test that zero index is properly injected (not treated as falsy)."""
        context = {}

        workflow_engine._inject_loop_variables(
            context=context,
            var_name="item",
            item="first",
            index=0,
        )

        assert context["_index"] == 0
        assert context["_index"] is not None

    def test_inject_loop_variables_preserves_agent_outputs(self, workflow_engine: WorkflowEngine):
        """Test that injection doesn't interfere with agent outputs in context."""
        context = {
            "workflow": {"input": {"goal": "test"}},
            "finder": {"output": {"kpis": ["K1", "K2"]}},
            "analyzer": {"output": {"results": [1, 2]}},
            "context": {"iteration": 2},
        }

        workflow_engine._inject_loop_variables(
            context=context,
            var_name="kpi",
            item={"kpi_id": "K1"},
            index=0,
            key="K1",
        )

        # Verify loop variables were injected
        assert context["kpi"] == {"kpi_id": "K1"}
        assert context["_index"] == 0
        assert context["_key"] == "K1"

        # Verify existing context was preserved
        assert context["workflow"]["input"]["goal"] == "test"
        assert context["finder"]["output"]["kpis"] == ["K1", "K2"]
        assert context["analyzer"]["output"]["results"] == [1, 2]
        assert context["context"]["iteration"] == 2

    def test_inject_loop_variables_complex_nested_item(self, workflow_engine: WorkflowEngine):
        """Test injection with a complex nested item structure."""
        context = {}

        complex_item = {
            "kpi": {
                "id": "revenue",
                "metrics": {
                    "current": 1000,
                    "target": 1500,
                },
                "tags": ["financial", "quarterly"],
            }
        }

        workflow_engine._inject_loop_variables(
            context=context,
            var_name="kpi_data",
            item=complex_item,
            index=7,
        )

        assert context["kpi_data"] == complex_item
        assert context["kpi_data"]["kpi"]["metrics"]["current"] == 1000
        assert context["_index"] == 7

    def test_inject_loop_variables_none_key(self, workflow_engine: WorkflowEngine):
        """Test that None key is explicitly not injected."""
        context = {}

        workflow_engine._inject_loop_variables(
            context=context,
            var_name="item",
            item="value",
            index=0,
            key=None,
        )

        assert "_key" not in context

    def test_inject_loop_variables_empty_string_key(self, workflow_engine: WorkflowEngine):
        """Test that empty string key is injected (it's a valid key)."""
        context = {}

        workflow_engine._inject_loop_variables(
            context=context,
            var_name="item",
            item="value",
            index=0,
            key="",
        )

        # Empty string is a valid key, should be injected
        assert "_key" in context
        assert context["_key"] == ""


class TestExtractKeyFromItem:
    """Tests for _extract_key_from_item() method."""

    @pytest.fixture
    def workflow_engine(self) -> WorkflowEngine:
        """Create a basic WorkflowEngine for testing."""
        from unittest.mock import MagicMock

        mock_provider = MagicMock()

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-workflow",
                entry_point="test",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="test",
                    model="gpt-4",
                    prompt="Test",
                    output={"result": OutputField(type="string")},
                ),
            ],
        )
        return WorkflowEngine(config, mock_provider)

    def test_extract_key_from_dict_item(self, workflow_engine: WorkflowEngine):
        """Test extracting key from dict item."""
        item = {"kpi_id": "K123", "name": "Revenue"}
        key = workflow_engine._extract_key_from_item(item, "kpi_id", fallback_index=0)
        assert key == "K123"

    def test_extract_key_from_nested_dict(self, workflow_engine: WorkflowEngine):
        """Test extracting key from nested dict using dotted path."""
        item = {"kpi": {"kpi_id": "REV001", "metadata": {"type": "financial"}}}
        key = workflow_engine._extract_key_from_item(item, "kpi.kpi_id", fallback_index=5)
        assert key == "REV001"

    def test_extract_key_from_object_attribute(self, workflow_engine: WorkflowEngine):
        """Test extracting key from object with attributes."""
        from dataclasses import dataclass

        @dataclass
        class KPI:
            kpi_id: str
            value: int

        item = KPI(kpi_id="COST001", value=100)
        key = workflow_engine._extract_key_from_item(item, "kpi_id", fallback_index=2)
        assert key == "COST001"

    def test_extract_key_converts_to_string(self, workflow_engine: WorkflowEngine):
        """Test that extracted key is converted to string."""
        item = {"id": 12345}
        key = workflow_engine._extract_key_from_item(item, "id", fallback_index=0)
        assert key == "12345"
        assert isinstance(key, str)

    def test_extract_key_fallback_on_missing_key(self, workflow_engine: WorkflowEngine):
        """Test fallback to index when key field is missing."""
        item = {"name": "Revenue"}  # Missing kpi_id field
        key = workflow_engine._extract_key_from_item(item, "kpi_id", fallback_index=7)
        assert key == "7"

    def test_extract_key_fallback_on_nested_missing(self, workflow_engine: WorkflowEngine):
        """Test fallback when nested path doesn't exist."""
        item = {"kpi": {"name": "Revenue"}}  # Missing kpi.kpi_id
        key = workflow_engine._extract_key_from_item(item, "kpi.kpi_id", fallback_index=3)
        assert key == "3"

    def test_extract_key_fallback_on_type_mismatch(self, workflow_engine: WorkflowEngine):
        """Test fallback when item type doesn't support requested access."""
        item = "simple_string"  # Not a dict or object
        key = workflow_engine._extract_key_from_item(item, "kpi_id", fallback_index=9)
        assert key == "9"

    def test_extract_key_from_deeply_nested_path(self, workflow_engine: WorkflowEngine):
        """Test extracting key from deeply nested structure."""
        item = {"data": {"metrics": {"financial": {"id": "DEEP123"}}}}
        key = workflow_engine._extract_key_from_item(
            item, "data.metrics.financial.id", fallback_index=0
        )
        assert key == "DEEP123"

    def test_extract_key_fallback_returns_string(self, workflow_engine: WorkflowEngine):
        """Test that fallback index is returned as string."""
        item = {}
        key = workflow_engine._extract_key_from_item(item, "missing", fallback_index=42)
        assert key == "42"
        assert isinstance(key, str)
