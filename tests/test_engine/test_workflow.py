"""Integration tests for WorkflowEngine.

Tests cover:
- Linear workflow execution
- Context passing between agents
- Output template rendering
- Routing between agents
- Error handling
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    GateOption,
    HooksConfig,
    InputDef,
    LimitsConfig,
    OutputField,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEventEmitter
from conductor.exceptions import ExecutionError
from conductor.providers.base import AgentOutput
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

    def test_engine_populates_workflow_metadata(
        self, tmp_path, simple_workflow_config: WorkflowConfig
    ) -> None:
        """``WorkflowEngine.__init__`` wires ``workflow_path`` into context fields.

        Guards against a regression where someone refactors ``__init__`` and
        reverts to a bare ``WorkflowContext()``, silently dropping
        ``workflow.dir``/``workflow.file``/``workflow.name`` from templates.
        """
        wf_file = tmp_path / "wf.yaml"
        wf_file.write_text("name: test\n")

        engine = WorkflowEngine(simple_workflow_config, workflow_path=wf_file)

        assert engine.context.workflow_dir == str(tmp_path.resolve())
        assert engine.context.workflow_file == str(wf_file.resolve())
        assert engine.context.workflow_name == simple_workflow_config.workflow.name

    def test_engine_workflow_metadata_empty_without_path(
        self, simple_workflow_config: WorkflowConfig
    ) -> None:
        """Without ``workflow_path``, path-derived fields stay empty.

        Empty strings are omitted from the rendered context (see
        ``WorkflowContext.build_for_agent``), so this preserves the existing
        no-pollution behaviour for path-less engines (e.g., test fixtures).
        """
        engine = WorkflowEngine(simple_workflow_config)

        assert engine.context.workflow_dir == ""
        assert engine.context.workflow_file == ""
        assert engine.context.workflow_name == simple_workflow_config.workflow.name


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

    @pytest.mark.asyncio
    async def test_explicit_mode_script_gets_workflow_inputs(self) -> None:
        """Regression: script agents in explicit mode see workflow.input.

        ``workflow.input`` is the workflow's external interface — set once at
        startup and present for the lifetime of the run. For local-render
        agent types (``script``, ``workflow``) it is always available, even
        in explicit mode where prior agent outputs remain explicitly declared.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="explicit-script",
                entry_point="detector",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                AgentDef(
                    name="detector",
                    type="script",
                    command=sys.executable,
                    args=[
                        "-c",
                        "print('{{ workflow.input.work_item_id }}')",
                    ],
                    # No input: list — should still see workflow.input
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(config, provider)

        await engine.run({"work_item_id": 42})

        # Script should have rendered the template successfully
        assert engine.context.agent_outputs["detector"]["stdout"].strip() == "42"


class TestApplyInputDefaults:
    """Tests for `_apply_input_defaults` / `_zero_value_for_type`.

    Optional inputs without an explicit ``default:`` must resolve to a
    type-appropriate zero value (not ``None``) so templates render cleanly
    without requiring ``| default()`` guards.
    """

    @staticmethod
    def _engine_with_input(name: str, input_def: InputDef) -> WorkflowEngine:
        """Build a minimal engine whose only declared input is `input_def`."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="defaults-probe",
                entry_point="noop",
                input={name: input_def},
            ),
            agents=[
                AgentDef(
                    name="noop",
                    model="gpt-4",
                    prompt="noop",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        return WorkflowEngine(config, CopilotProvider(mock_handler=lambda a, p, c: {}))

    @pytest.mark.parametrize(
        ("type_name", "expected_zero"),
        [
            ("string", ""),
            ("number", 0),
            ("boolean", False),
            ("array", []),
            ("object", {}),
        ],
    )
    def test_optional_input_with_no_default_gets_type_zero(
        self, type_name: str, expected_zero: object
    ) -> None:
        """Every InputDef.type resolves to its type-appropriate zero, not None."""
        engine = self._engine_with_input("opt", InputDef(type=type_name, required=False))

        merged = engine._apply_input_defaults({})

        assert "opt" in merged
        assert merged["opt"] == expected_zero
        assert merged["opt"] is not None

    def test_explicit_default_is_honored_over_zero(self) -> None:
        """A declared ``default:`` wins; the zero-value path must not override it."""
        engine = self._engine_with_input(
            "with_default", InputDef(type="string", required=False, default="hello")
        )

        merged = engine._apply_input_defaults({})

        assert merged["with_default"] == "hello"

    def test_provided_value_passes_through_unchanged(self) -> None:
        """Caller-provided values are never overwritten by defaults."""
        engine = self._engine_with_input("opt", InputDef(type="string", required=False))

        merged = engine._apply_input_defaults({"opt": "explicit"})

        assert merged["opt"] == "explicit"

    def test_required_input_is_left_alone_when_missing(self) -> None:
        """Missing required inputs are not silently filled — let validation flag them."""
        engine = self._engine_with_input("must_have", InputDef(type="string", required=True))

        merged = engine._apply_input_defaults({})

        assert "must_have" not in merged

    @pytest.mark.parametrize("type_name", ["array", "object"])
    def test_zero_value_for_mutable_type_returns_fresh_instance(self, type_name: str) -> None:
        """Mutable zeros must not be shared — guards against the classic
        shared-mutable-default bug if someone later "optimizes" the lookup
        into a single cached instance.
        """
        engine = self._engine_with_input("opt", InputDef(type=type_name, required=False))

        first = engine._zero_value_for_type(type_name)
        second = engine._zero_value_for_type(type_name)

        assert first == second
        assert first is not second


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

    @pytest.mark.asyncio
    async def test_output_template_python_bool_literals(self) -> None:
        """Python str(bool) outputs ('True'/'False') from Jinja expressions
        coerce to native bool, not truthy non-empty strings.

        Without this, ``{{ a == b }}`` in a workflow ``output:`` block renders
        ``"True"`` / ``"False"`` and downstream ``when:`` clauses comparing to
        ``true`` / ``false`` silently misbehave (the strings are truthy).
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="bool-output",
                entry_point="agent1",
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="x",
                    output={
                        "left": OutputField(type="string"),
                        "right": OutputField(type="string"),
                    },
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "matched": "{{ agent1.output.left == agent1.output.right }}",
                "differs": "{{ agent1.output.left != agent1.output.right }}",
            },
        )

        def mock_handler(agent, prompt, context):
            return {"left": "abc", "right": "abc"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        assert result["matched"] is True
        assert result["differs"] is False

    @pytest.mark.asyncio
    async def test_output_template_python_none_literal(self) -> None:
        """Python str(None) ('None') from Jinja coerces to native None."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="none-output",
                entry_point="agent1",
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="x",
                    output={"thing": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                # Jinja's `none` value renders as the string "None" via str(None).
                "missing": "{{ none }}",
            },
        )

        def mock_handler(agent, prompt, context):
            return {"thing": "value"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        assert result["missing"] is None

    @pytest.mark.asyncio
    async def test_output_template_lowercase_json_literals_still_work(self) -> None:
        """Regression: lowercase JSON literals 'true'/'false'/'null' remain coerced."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="json-literals-output",
                entry_point="agent1",
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="x",
                    output={"v": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "t": "true",
                "f": "false",
                "n": "null",
            },
        )

        def mock_handler(agent, prompt, context):
            return {"v": "x"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        assert result["t"] is True
        assert result["f"] is False
        assert result["n"] is None


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

    @pytest.mark.asyncio
    async def test_human_gate_stores_additional_input_nested_in_context(self) -> None:
        """Test that prompt_for values are stored nested under additional_input."""
        from unittest.mock import patch

        config = WorkflowConfig(
            workflow=WorkflowDef(name="gate-prompt-for", entry_point="ask_human"),
            agents=[
                AgentDef(
                    name="ask_human",
                    type="human_gate",
                    prompt="Provide input:",
                    options=[
                        GateOption(
                            label="Provide answer",
                            value="provide_answer",
                            route="next",
                            prompt_for="answer",
                        ),
                    ],
                ),
                AgentDef(
                    name="next",
                    model="gpt-4",
                    prompt="Next",
                    output={"received": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"received": "{{ next.output.received }}"},
        )

        captured_context: dict = {}

        def mock_handler(agent, prompt, context):
            captured_context.update(context)
            return {"received": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, skip_gates=False)

        with patch("conductor.gates.human.Prompt.ask", side_effect=["1", "README.md"]):
            await engine.run({})

        gate_output = captured_context.get("ask_human", {}).get("output", {})
        assert gate_output["selected"] == "provide_answer"
        assert gate_output["additional_input"] == {"answer": "README.md"}

    @pytest.mark.asyncio
    async def test_human_gate_no_prompt_for_has_empty_additional_input(
        self,
        human_gate_workflow_config: WorkflowConfig,
    ) -> None:
        """Test that gates without prompt_for produce additional_input: {} in context."""
        captured_context: dict = {}

        def mock_handler(agent, prompt, context):
            captured_context.update(context)
            if agent.name == "drafter":
                return {"draft": "Draft content"}
            return {"published": True}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(human_gate_workflow_config, provider, skip_gates=True)

        await engine.run({})

        gate_output = captured_context.get("approval_gate", {}).get("output", {})
        assert gate_output["selected"] == "approved"
        assert gate_output["additional_input"] == {}

    @pytest.mark.asyncio
    async def test_human_gate_prompt_for_named_selected_does_not_corrupt_selected(
        self,
    ) -> None:
        """Test that prompt_for field named 'selected' cannot overwrite the selected value.

        The flat spread form had a silent data-corruption bug: if prompt_for used
        the field name 'selected', the user's typed text would overwrite the chosen
        option value. The nested form prevents this.
        """
        from unittest.mock import patch

        config = WorkflowConfig(
            workflow=WorkflowDef(name="gate-collision", entry_point="gate"),
            agents=[
                AgentDef(
                    name="gate",
                    type="human_gate",
                    prompt="Go:",
                    options=[
                        GateOption(
                            label="Go",
                            value="go",
                            route="next",
                            prompt_for="selected",  # collides with reserved key
                        ),
                    ],
                ),
                AgentDef(
                    name="next",
                    model="gpt-4",
                    prompt="Next",
                    output={"done": OutputField(type="boolean")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"done": "{{ next.output.done }}"},
        )

        captured_context: dict = {}

        def mock_handler(agent, prompt, context):
            captured_context.update(context)
            return {"done": True}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, skip_gates=False)

        with patch("conductor.gates.human.Prompt.ask", side_effect=["1", "user typed text"]):
            await engine.run({})

        gate_output = captured_context.get("gate", {}).get("output", {})
        # The chosen option value must not be overwritten by the user's typed text
        assert gate_output["selected"] == "go"
        # The user's typed text is safely nested
        assert gate_output["additional_input"]["selected"] == "user typed text"

    @pytest.mark.asyncio
    async def test_human_gate_web_response_nests_additional_input(self) -> None:
        """Web-gate responses must land under the same nested ``additional_input`` key.

        The store-site at engine/workflow.py is shared between the CLI gate
        handler and ``_wait_for_web_gate``. This test stubs the web dashboard so
        the web task wins the race and asserts the web-supplied
        ``additional_input`` is stored under ``output.additional_input``, not
        spread flat into ``output``.
        """
        from unittest.mock import AsyncMock, MagicMock

        config = WorkflowConfig(
            workflow=WorkflowDef(name="gate-web", entry_point="approval_gate"),
            agents=[
                AgentDef(
                    name="approval_gate",
                    type="human_gate",
                    prompt="Approve?",
                    options=[
                        GateOption(
                            label="Approve",
                            value="approved",
                            route="next",
                            prompt_for="comment",
                        ),
                    ],
                ),
                AgentDef(
                    name="next",
                    model="gpt-4",
                    prompt="next",
                    output={"received": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"received": "{{ next.output.received }}"},
        )

        # Stub the web dashboard so the web task returns immediately with a
        # realistic gate-response payload from a browser client.
        mock_dashboard = MagicMock()
        mock_dashboard.wait_for_gate_response = AsyncMock(
            return_value={
                "selected_value": "approved",
                "additional_input": {"comment": "looks good"},
            }
        )

        captured_context: dict[str, object] = {}

        def mock_handler(agent, prompt, context):
            captured_context.update(context)
            return {"received": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(
            config,
            provider,
            skip_gates=False,
            web_dashboard=mock_dashboard,
        )

        # Make the CLI side of the race never return so the web task wins
        # deterministically. The engine's _handle_gate_with_web cancels the
        # losing task and suppresses CancelledError, so this is safe.
        async def _never_returns(*_args, **_kwargs):
            await asyncio.Event().wait()

        from unittest.mock import patch

        with patch.object(engine.gate_handler, "handle_gate", side_effect=_never_returns):
            await engine.run({})

        gate_output = captured_context.get("approval_gate", {})  # type: ignore[assignment]
        assert isinstance(gate_output, dict)
        gate_output = gate_output.get("output", {})
        assert gate_output["selected"] == "approved"
        assert gate_output["additional_input"] == {"comment": "looks good"}
        # Belt-and-braces: web-supplied keys must not appear flat at the root.
        assert "comment" not in gate_output

    @pytest.mark.asyncio
    async def test_human_gate_additional_input_readable_via_template(self) -> None:
        """A downstream agent's prompt template must be able to read the nested value.

        This exercises the actual user-facing contract — templates resolving
        ``{{ <gate>.output.additional_input.<field> }}`` — rather than only the
        internal context shape. Locks in the rendered-prompt path so any
        regression in template rendering or dict-attr resolution would fail
        loudly here.
        """
        from unittest.mock import patch

        config = WorkflowConfig(
            workflow=WorkflowDef(name="gate-template-readthrough", entry_point="ask_human"),
            agents=[
                AgentDef(
                    name="ask_human",
                    type="human_gate",
                    prompt="Provide input:",
                    options=[
                        GateOption(
                            label="Provide answer",
                            value="provide_answer",
                            route="echo",
                            prompt_for="answer",
                        ),
                    ],
                ),
                AgentDef(
                    name="echo",
                    model="gpt-4",
                    prompt="User said: {{ ask_human.output.additional_input.answer }}",
                    output={"echoed": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"echoed": "{{ echo.output.echoed }}"},
        )

        rendered_prompts: list[str] = []

        def mock_handler(agent, prompt, context):
            if agent.name == "echo":
                rendered_prompts.append(prompt)
            return {"echoed": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, skip_gates=False)

        with patch("conductor.gates.human.Prompt.ask", side_effect=["1", "README.md"]):
            await engine.run({})

        assert rendered_prompts, "echo agent's mock_handler was never invoked"
        assert "User said: README.md" in rendered_prompts[0]

    @pytest.mark.asyncio
    async def test_human_gate_bg_mode_waits_web_only_and_skips_cli_prompt(self) -> None:
        """In ``--web-bg`` (bg_mode), the gate must wait web-only (issue #286).

        Before the fix, ``_handle_gate_with_web`` raced the CLI prompt against
        the web arm unconditionally. With ``stdin`` not a TTY (as in the
        detached ``--web-bg`` child), ``Prompt.ask`` raises ``EOFError``
        instantly, race-winning and crashing the workflow before a dashboard
        user could respond. This test asserts the CLI handler is never even
        invoked when ``_bg_mode`` is set — only the web dashboard is awaited.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from conductor.engine.workflow import RunContext

        config = WorkflowConfig(
            workflow=WorkflowDef(name="gate-bg", entry_point="approval_gate"),
            agents=[
                AgentDef(
                    name="approval_gate",
                    type="human_gate",
                    prompt="Approve?",
                    options=[
                        GateOption(label="Approve", value="approved", route="next"),
                    ],
                ),
                AgentDef(
                    name="next",
                    model="gpt-4",
                    prompt="next",
                    output={"received": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"received": "{{ next.output.received }}"},
        )

        mock_dashboard = MagicMock()
        mock_dashboard.wait_for_gate_response = AsyncMock(
            return_value={"selected_value": "approved", "additional_input": {}}
        )

        def mock_handler(agent, prompt, context):
            return {"received": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(
            config,
            provider,
            skip_gates=False,
            web_dashboard=mock_dashboard,
            run_context=RunContext(bg_mode=True),
        )

        with patch.object(engine.gate_handler, "handle_gate") as mock_cli_handle:
            result = await engine.run({})

        assert result["received"] == "ok"
        mock_dashboard.wait_for_gate_response.assert_awaited_once_with("approval_gate")
        mock_cli_handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_human_gate_foreground_tty_still_races_cli_and_web(self) -> None:
        """Foreground ``--web`` from a real terminal still races CLI vs web.

        Confirms the #286 web-only tier is scoped to bg/non-TTY only — an
        interactive terminal keeps the pre-existing race behavior, letting
        whichever of the CLI prompt or the dashboard responds first win.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from conductor.engine.workflow import RunContext

        config = WorkflowConfig(
            workflow=WorkflowDef(name="gate-tty", entry_point="approval_gate"),
            agents=[
                AgentDef(
                    name="approval_gate",
                    type="human_gate",
                    prompt="Approve?",
                    options=[
                        GateOption(label="Approve", value="approved", route="next"),
                        GateOption(label="Reject", value="rejected", route="next"),
                    ],
                ),
                AgentDef(
                    name="next",
                    model="gpt-4",
                    prompt="next",
                    output={"received": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"received": "{{ next.output.received }}"},
        )

        mock_dashboard = MagicMock()
        mock_dashboard.wait_for_gate_response = AsyncMock(
            return_value={"selected_value": "approved", "additional_input": {}}
        )

        def mock_handler(agent, prompt, context):
            return {"received": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(
            config,
            provider,
            skip_gates=False,
            web_dashboard=mock_dashboard,
            run_context=RunContext(bg_mode=False),
        )

        # Foreground TTY: stdin.isatty() True, and the CLI prompt never
        # returns so the web task deterministically wins the race (mirrors
        # the existing test_human_gate_web_response_nests_additional_input
        # pattern above).
        async def _never_returns(*_args, **_kwargs):
            await asyncio.Event().wait()

        with (
            patch("sys.stdin.isatty", return_value=True),
            patch.object(engine.gate_handler, "handle_gate", side_effect=_never_returns),
        ):
            result = await engine.run({})

        assert result["received"] == "ok"
        mock_dashboard.wait_for_gate_response.assert_awaited_once_with("approval_gate")


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

    def test_resolve_workflow_input_array(self, workflow_engine_with_context: WorkflowEngine):
        """Test resolving an array from workflow.input.*."""
        workflow_engine_with_context.context.set_workflow_inputs(
            {"items": ["apple", "banana", "cherry"]}
        )

        result = workflow_engine_with_context._resolve_array_reference("workflow.input.items")

        assert isinstance(result, list)
        assert result == ["apple", "banana", "cherry"]

    def test_resolve_workflow_input_json_string(self, workflow_engine_with_context: WorkflowEngine):
        """Test resolving a JSON string array from workflow.input.* (CLI passes strings)."""
        workflow_engine_with_context.context.set_workflow_inputs(
            {"items": '["apple", "banana", "cherry"]'}
        )

        result = workflow_engine_with_context._resolve_array_reference("workflow.input.items")

        assert isinstance(result, list)
        assert result == ["apple", "banana", "cherry"]

    def test_resolve_workflow_input_nested(self, workflow_engine_with_context: WorkflowEngine):
        """Test resolving a nested path from workflow.input.*."""
        workflow_engine_with_context.context.set_workflow_inputs(
            {"config": {"tasks": ["task1", "task2"]}}
        )

        result = workflow_engine_with_context._resolve_array_reference(
            "workflow.input.config.tasks"
        )

        assert isinstance(result, list)
        assert result == ["task1", "task2"]

    def test_resolve_workflow_input_missing_field(
        self, workflow_engine_with_context: WorkflowEngine
    ):
        """Test error when workflow input field doesn't exist."""
        workflow_engine_with_context.context.set_workflow_inputs({"other": "value"})

        with pytest.raises(ExecutionError) as exc_info:
            workflow_engine_with_context._resolve_array_reference("workflow.input.items")

        assert "Field 'items' not found" in str(exc_info.value)
        assert "Available keys:" in str(exc_info.value.suggestion)

    def test_resolve_workflow_input_not_array(self, workflow_engine_with_context: WorkflowEngine):
        """Test error when workflow input value is not an array."""
        workflow_engine_with_context.context.set_workflow_inputs({"items": 42})

        with pytest.raises(ExecutionError) as exc_info:
            workflow_engine_with_context._resolve_array_reference("workflow.input.items")

        assert "resolved to int" in str(exc_info.value)

    def test_resolve_workflow_input_invalid_json_string(
        self, workflow_engine_with_context: WorkflowEngine
    ):
        """Test error when workflow input string is not valid JSON."""
        workflow_engine_with_context.context.set_workflow_inputs({"items": "not json at all"})

        with pytest.raises(ExecutionError) as exc_info:
            workflow_engine_with_context._resolve_array_reference("workflow.input.items")

        assert "not valid JSON" in str(exc_info.value)

    def test_resolve_workflow_input_json_string_not_array(
        self, workflow_engine_with_context: WorkflowEngine
    ):
        """Test error when workflow input JSON string parses to non-array."""
        workflow_engine_with_context.context.set_workflow_inputs({"items": '{"key": "value"}'})

        with pytest.raises(ExecutionError) as exc_info:
            workflow_engine_with_context._resolve_array_reference("workflow.input.items")

        assert "parsed from JSON string but got dict" in str(exc_info.value)

    def test_resolve_workflow_input_empty_array(self, workflow_engine_with_context: WorkflowEngine):
        """Test resolving an empty array from workflow.input.*."""
        workflow_engine_with_context.context.set_workflow_inputs({"items": []})

        result = workflow_engine_with_context._resolve_array_reference("workflow.input.items")

        assert isinstance(result, list)
        assert len(result) == 0

    def test_resolve_workflow_input_no_field_parts(
        self, workflow_engine_with_context: WorkflowEngine
    ):
        """Test error when workflow.input has no field name."""
        # This would be caught by the len(parts) < 3 check since
        # "workflow.input" only has 2 parts, but test the boundary.
        with pytest.raises(ExecutionError):
            workflow_engine_with_context._resolve_array_reference("workflow.input")


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


class _RecordingReasoningProvider:
    """Minimal AgentProvider that records the resolved reasoning effort per agent.

    Mirrors what real providers (Copilot/Claude) do: it stores
    ``default_reasoning_effort`` on init and calls
    :func:`conductor.providers.reasoning.resolve_reasoning_effort` on each
    ``execute()`` so we can verify the full plumbing path end-to-end.
    """

    def __init__(self, default_reasoning_effort=None):
        from conductor.providers.base import AgentProvider

        assert isinstance(self, object)
        self._default_reasoning_effort = default_reasoning_effort
        self.resolved_efforts: dict[str, str | None] = {}
        # Sanity: ensure the protocol the engine expects is satisfied via duck typing.
        _ = AgentProvider

    async def execute(
        self,
        agent,
        context,
        rendered_prompt,
        tools=None,
        interrupt_signal=None,
        event_callback=None,
    ):
        from conductor.providers.base import AgentOutput
        from conductor.providers.reasoning import resolve_reasoning_effort

        effort = resolve_reasoning_effort(agent, self._default_reasoning_effort)
        self.resolved_efforts[agent.name] = effort

        content: dict[str, object] = {}
        if agent.output:
            for field_name in agent.output:
                content[field_name] = f"{agent.name}:{effort}"
        return AgentOutput(content=content, raw_response=None, model=agent.model)

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def get_max_prompt_tokens(self, model: str):
        return None


class TestReasoningEffortPlumbing:
    """End-to-end plumbing for ``runtime.default_reasoning_effort`` and
    per-agent ``reasoning.effort`` overrides.
    """

    @pytest.mark.asyncio
    async def test_runtime_default_and_per_agent_override_reach_provider(self) -> None:
        """Agents inherit the runtime default; per-agent ``reasoning`` overrides it."""
        from conductor.config.schema import ReasoningConfig

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="reasoning-effort-plumbing",
                entry_point="inheritor",
                runtime=RuntimeConfig(provider="copilot", default_reasoning_effort="medium"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="inheritor",
                    model="gpt-4",
                    prompt="Inherit the runtime default",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="overrider")],
                ),
                AgentDef(
                    name="overrider",
                    model="gpt-4",
                    prompt="Override with high",
                    reasoning=ReasoningConfig(effort="high"),
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "inheritor": "{{ inheritor.output.answer }}",
                "overrider": "{{ overrider.output.answer }}",
            },
        )

        provider = _RecordingReasoningProvider(default_reasoning_effort="medium")
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        assert provider.resolved_efforts == {
            "inheritor": "medium",
            "overrider": "high",
        }
        # The recording provider encodes the effort into the output, confirming
        # the engine actually consumed the value the provider produced.
        assert result["inheritor"] == "inheritor:medium"
        assert result["overrider"] == "overrider:high"

    @pytest.mark.asyncio
    async def test_no_runtime_default_and_no_agent_reasoning_resolves_to_none(self) -> None:
        """When neither side sets reasoning, the resolver returns ``None``."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="reasoning-effort-unset",
                entry_point="solo",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="solo",
                    model="gpt-4",
                    prompt="No reasoning configured",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"answer": "{{ solo.output.answer }}"},
        )

        provider = _RecordingReasoningProvider(default_reasoning_effort=None)
        engine = WorkflowEngine(config, provider)

        await engine.run({})

        assert provider.resolved_efforts == {"solo": None}


class TestProviderFactoryReasoningEffortWiring:
    """``ProviderFactory.create_provider`` must forward
    ``default_reasoning_effort`` from the ``RuntimeConfig`` to the concrete
    provider's ``_default_reasoning_effort`` attribute.
    """

    @pytest.mark.asyncio
    async def test_factory_forwards_to_copilot(self) -> None:
        from conductor.providers.copilot import CopilotProvider as _CopilotProvider
        from conductor.providers.factory import ProviderFactory

        runtime = RuntimeConfig(provider="copilot", default_reasoning_effort="high")
        provider = await ProviderFactory.create_provider(runtime, validate=False)
        try:
            assert isinstance(provider, _CopilotProvider)
            assert provider._default_reasoning_effort == "high"
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_factory_forwards_to_claude(self) -> None:
        from conductor.providers.claude import (
            ANTHROPIC_SDK_AVAILABLE,
        )
        from conductor.providers.claude import (
            ClaudeProvider as _ClaudeProvider,
        )
        from conductor.providers.factory import ProviderFactory

        if not ANTHROPIC_SDK_AVAILABLE:
            pytest.skip("anthropic SDK not installed")

        runtime = RuntimeConfig(provider="claude", default_reasoning_effort="high")
        provider = await ProviderFactory.create_provider(runtime, validate=False)
        try:
            assert isinstance(provider, _ClaudeProvider)
            assert provider._default_reasoning_effort == "high"
        finally:
            await provider.close()


# ---------------------------------------------------------------------------
# Exception-handling arms in _execute_loop (issue #116)
# ---------------------------------------------------------------------------


class TestExecuteLoopExceptionArms:
    """Tests for the SystemExit / BaseException / CancelledError arms.

    Issue #116 added a final ``except BaseException`` arm so silent
    startup crashes leave a ``workflow_failed`` event in the JSONL log,
    plus an explicit ``except asyncio.CancelledError: raise`` arm so a
    user-initiated dashboard stop is not labelled as a spurious failure.
    """

    @pytest.mark.asyncio
    async def test_systemexit_emits_workflow_failed_event(
        self, simple_workflow_config: WorkflowConfig
    ) -> None:
        """A ``SystemExit`` from the provider is captured as ``workflow_failed``.

        Without the ``except BaseException`` arm this exception would
        propagate past the engine's existing ``except Exception`` arm and
        the JSONL log would silently end after ``agent_started`` — the
        exact symptom reported in issue #116.
        """
        from conductor.events import WorkflowEvent, WorkflowEventEmitter

        def mock_handler(agent, prompt, context):
            raise SystemExit("simulated startup crash")

        provider = CopilotProvider(mock_handler=mock_handler)

        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)

        engine = WorkflowEngine(simple_workflow_config, provider, event_emitter=emitter)

        with pytest.raises(SystemExit):
            await engine.run({"question": "anything"})

        failed_events = [e for e in events if e.type == "workflow_failed"]
        assert len(failed_events) == 1, (
            f"expected exactly one workflow_failed event; got {[e.type for e in events]}"
        )
        fail = failed_events[0]
        assert fail.data["error_type"] == "SystemExit"
        assert fail.data.get("is_base_exception") is True
        assert "simulated startup crash" in fail.data["message"]

    @pytest.mark.asyncio
    async def test_regular_exception_does_not_set_is_base_exception_flag(
        self, simple_workflow_config: WorkflowConfig
    ) -> None:
        """A regular ``Exception`` must NOT set ``is_base_exception`` on workflow_failed.

        This is the contract Phase 2 will rely on to tell genuine
        ``BaseException`` crashes (e.g., ``SystemExit``) apart from regular
        workflow errors. Without this baseline test, a buggy refactor could
        set the flag unconditionally and the issue would go unnoticed.
        """
        from conductor.events import WorkflowEvent, WorkflowEventEmitter

        def mock_handler(agent, prompt, context):
            raise RuntimeError("regular failure, not a base exception")

        provider = CopilotProvider(mock_handler=mock_handler)

        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)

        engine = WorkflowEngine(simple_workflow_config, provider, event_emitter=emitter)

        with pytest.raises(Exception):  # noqa: B017 - exception type varies by retry wrapping
            await engine.run({"question": "anything"})

        failed_events = [e for e in events if e.type == "workflow_failed"]
        assert failed_events, "expected a workflow_failed event for a regular Exception"
        # The provider's retry loop may wrap RuntimeError into ProviderError,
        # so we don't assert on error_type — only on the flag's absence.
        for ev in failed_events:
            assert ev.data.get("is_base_exception") is not True, (
                f"regular Exception must NOT set is_base_exception=True; got: {ev.data}"
            )

    @pytest.mark.asyncio
    async def test_cancellederror_via_external_task_cancel(
        self, simple_workflow_config: WorkflowConfig
    ) -> None:
        """External ``task.cancel()`` propagates as ``CancelledError`` with no failure event.

        This exercises the real dashboard-stop / parent-cancellation flow:
        the engine task is running, external code calls ``task.cancel()``,
        ``CancelledError`` is injected at the next ``await`` point inside
        the engine, and it must hit the new ``except asyncio.CancelledError:
        raise`` arm WITHOUT firing ``workflow_failed``.

        We patch ``AgentExecutor.execute`` to be an async ``sleep`` so the
        cancellation has a real ``await`` point to fire at — the
        ``mock_handler`` path on the provider runs synchronously and would
        not be cancellable from outside.
        """
        from unittest.mock import patch

        from conductor.events import WorkflowEvent, WorkflowEventEmitter

        started = asyncio.Event()

        async def slow_execute(*args, **kwargs):
            started.set()
            await asyncio.sleep(60.0)  # cancellable await point
            raise AssertionError("should never get here — the task is cancelled first")

        def mock_handler(agent, prompt, context):  # pragma: no cover - patched out
            return {"answer": "unused"}

        provider = CopilotProvider(mock_handler=mock_handler)

        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)

        engine = WorkflowEngine(simple_workflow_config, provider, event_emitter=emitter)

        with patch(
            "conductor.executor.agent.AgentExecutor.execute",
            side_effect=slow_execute,
        ):
            task = asyncio.create_task(engine.run({"question": "anything"}))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        failed_events = [e for e in events if e.type == "workflow_failed"]
        assert failed_events == [], (
            "External task.cancel() must propagate without emitting workflow_failed; "
            f"got: {[e.data for e in failed_events]}"
        )

    @pytest.mark.asyncio
    async def test_bg_log_paths_in_workflow_started_system_metadata(
        self,
        simple_workflow_config: WorkflowConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``bg_mode=True``, ``CONDUCTOR_BG_*_LOG`` env vars surface in system metadata.

        This is the contract the web dashboard relies on to display the
        captured stderr/stdout log paths to the user (issue #116).
        """
        from conductor.engine.workflow import RunContext
        from conductor.events import WorkflowEvent, WorkflowEventEmitter

        monkeypatch.setenv("CONDUCTOR_BG_STDERR_LOG", "/tmp/conductor-test.bg.stderr.log")
        monkeypatch.setenv("CONDUCTOR_BG_STDOUT_LOG", "/tmp/conductor-test.bg.stdout.log")

        def mock_handler(agent, prompt, context):
            return {"answer": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)

        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)

        engine = WorkflowEngine(
            simple_workflow_config,
            provider,
            event_emitter=emitter,
            run_context=RunContext(bg_mode=True),
        )

        await engine.run({"question": "anything"})

        started = [e for e in events if e.type == "workflow_started"]
        assert started, "expected a workflow_started event"
        system = started[0].data["system"]
        assert system["bg_mode"] is True
        assert system["bg_stderr_log"] == "/tmp/conductor-test.bg.stderr.log"
        assert system["bg_stdout_log"] == "/tmp/conductor-test.bg.stdout.log"

    @pytest.mark.asyncio
    async def test_bg_log_paths_omitted_when_bg_mode_false(
        self,
        simple_workflow_config: WorkflowConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``bg_mode=False``, ``CONDUCTOR_BG_*_LOG`` env vars are NOT surfaced.

        Non-bg runs don't write to bg log files, so emitting these fields
        with stale env values from a previous shell session would be
        misleading. The metadata block is gated on ``bg_mode``.
        """
        from conductor.events import WorkflowEvent, WorkflowEventEmitter

        monkeypatch.setenv("CONDUCTOR_BG_STDERR_LOG", "/tmp/stale.log")
        monkeypatch.setenv("CONDUCTOR_BG_STDOUT_LOG", "/tmp/stale.log")

        def mock_handler(agent, prompt, context):
            return {"answer": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)

        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)

        engine = WorkflowEngine(simple_workflow_config, provider, event_emitter=emitter)

        await engine.run({"question": "anything"})

        started = [e for e in events if e.type == "workflow_started"]
        assert started
        system = started[0].data["system"]
        assert "bg_stderr_log" not in system
        assert "bg_stdout_log" not in system


class TestWorkflowEngineTerminate:
    """Engine-level tests for ``type: terminate`` steps (issue #219).

    These tests drive the engine through real terminate dispatch (success and
    failure), verify the correct event payloads, and confirm
    ``WorkflowTerminated`` semantics: explicit termination must surface as a
    non-resumable failure with rich metadata, distinguishable from a generic
    exception. Tests use a stub provider via ``CopilotProvider(mock_handler=…)``
    only for the upstream agent; the terminate dispatch branch needs no
    provider call.
    """

    @staticmethod
    def _config_with_terminate(
        status: str,
        *,
        output_template: dict[str, str] | None = None,
        reason: str = "Document already up to date; no edits needed.",
        also_workflow_output: bool = True,
    ) -> WorkflowConfig:
        """Build a small workflow whose entry agent unconditionally routes to a terminate step.

        The entry agent is a real provider-backed agent so the dispatch path
        exercises every event the dashboard listens for (agent_started /
        agent_completed) before reaching the terminate branch.
        """
        agents: list[AgentDef] = [
            AgentDef(
                name="upstream",
                model="gpt-4",
                prompt="x",
                output={"value": OutputField(type="string")},
                routes=[RouteDef(to="finish")],
            ),
            AgentDef(
                name="finish",
                type="terminate",
                status=status,  # type: ignore[arg-type]
                reason=reason,
                output_template=output_template,
            ),
        ]
        return WorkflowConfig(
            workflow=WorkflowDef(
                name="terminate-test",
                entry_point="upstream",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=agents,
            output={"result": "{{ upstream.output.value }}"} if also_workflow_output else {},
        )

    @pytest.mark.asyncio
    async def test_success_terminate_returns_output_template(self) -> None:
        """`status: success` returns the rendered output_template cleanly.

        The workflow-level ``output:`` mapping is REPLACED by the terminate
        step's ``output_template`` (not merged). This contract lets callers
        use a terminate step as an early-exit short-circuit that emits a
        differently-shaped final payload from the normal `$end` path.
        """
        from conductor.events import WorkflowEvent, WorkflowEventEmitter

        config = self._config_with_terminate(
            "success",
            output_template={
                "result": "no-op",
                "reason": "{{ finish.output.reason }}",
            },
        )
        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {"value": "upstream-value"})
        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)

        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        result = await engine.run({})

        assert result == {
            "result": "no-op",
            "reason": "Document already up to date; no edits needed.",
        }
        completed = [e for e in events if e.type == "workflow_completed"]
        assert len(completed) == 1
        data = completed[0].data
        assert data["is_explicit"] is True
        assert data["termination_reason"] == "Document already up to date; no edits needed."
        assert data["terminated_by"] == "finish"
        assert data["status"] == "success"

    @pytest.mark.asyncio
    async def test_success_terminate_without_output_template_falls_back(self) -> None:
        """Without ``output_template`` the workflow-level ``output:`` is rendered.

        This is the behaviour-preserving default: existing workflows that route
        to a terminate step without supplying ``output_template`` still produce
        the same final-output shape as a normal `$end` path.
        """
        config = self._config_with_terminate("success", output_template=None)
        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {"value": "v"})

        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        assert result == {"result": "v"}

    @pytest.mark.asyncio
    async def test_failed_terminate_raises_workflow_terminated(self) -> None:
        """`status: failed` raises ``WorkflowTerminated`` with structured fields.

        The CLI / dashboard rely on these attributes (``output``, ``reason``,
        ``terminated_by``) to render the explicit termination distinctly from a
        generic exception. The reason is the *rendered* string, not the
        template.
        """
        from conductor.exceptions import WorkflowTerminated

        config = self._config_with_terminate(
            "failed",
            reason="upstream said {{ upstream.output.value }}",
            output_template={"aborted": "true", "msg": "{{ upstream.output.value }}"},
        )
        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {"value": "stop now"})

        engine = WorkflowEngine(config, provider)
        with pytest.raises(WorkflowTerminated) as excinfo:
            await engine.run({})

        err = excinfo.value
        assert err.terminated_by == "finish"
        assert err.reason == "upstream said stop now"
        assert err.output == {"aborted": True, "msg": "stop now"}
        assert err.status == "failed"

    @pytest.mark.asyncio
    async def test_failed_terminate_emits_workflow_failed_with_explicit_flag(self) -> None:
        """`status: failed` emits a `workflow_failed` event with `is_explicit: true`.

        Downstream tooling (CI, notifications, dashboards) distinguishes an
        intentional termination from a generic crash by reading this flag.
        Without it, every terminate would look indistinguishable from an
        unhandled exception.
        """
        from conductor.events import WorkflowEvent, WorkflowEventEmitter
        from conductor.exceptions import WorkflowTerminated

        config = self._config_with_terminate("failed", reason="halt")
        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {"value": "v"})

        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)

        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        with pytest.raises(WorkflowTerminated):
            await engine.run({})

        failed = [e for e in events if e.type == "workflow_failed"]
        assert len(failed) == 1
        data = failed[0].data
        assert data["is_explicit"] is True
        assert data["error_type"] == "WorkflowTerminated"
        assert data["terminated_by"] == "finish"
        assert data["termination_reason"] == "halt"
        assert data["status"] == "failed"
        # The agent-lifecycle event for a failed terminate is `agent_failed`
        # (not `agent_completed`) so dashboard counters stay accurate.
        agent_lifecycle = [e for e in events if e.type in ("agent_completed", "agent_failed")]
        terminate_lifecycle = [e for e in agent_lifecycle if e.data.get("agent_name") == "finish"]
        assert terminate_lifecycle and terminate_lifecycle[0].type == "agent_failed", (
            f"terminate with status=failed must emit agent_failed; "
            f"got: {[e.type for e in terminate_lifecycle]}"
        )

    @pytest.mark.asyncio
    async def test_failed_terminate_does_not_save_checkpoint(self, tmp_path) -> None:
        """Explicit termination is intentional; no on-failure checkpoint is saved.

        Without this carve-out, every terminate-failed run would leave a
        checkpoint behind for the next ``conductor resume`` to pick up — but
        terminating with `status: failed` is the author saying "this run is
        complete and the outcome is failure," not "please resume this later."
        """
        from conductor.exceptions import WorkflowTerminated

        config = self._config_with_terminate("failed", reason="halt")
        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {"value": "v"})

        wf_file = tmp_path / "failed_terminate_checkpoint_test.yaml"
        wf_file.write_text("name: t\n")
        engine = WorkflowEngine(config, provider, workflow_path=wf_file)

        with pytest.raises(WorkflowTerminated):
            await engine.run({})

        # No checkpoint files should exist for this workflow run.
        from conductor.engine.checkpoint import CheckpointManager

        checkpoints = CheckpointManager.list_checkpoints(workflow_path=wf_file)
        assert not checkpoints, f"failed-terminate must not save a checkpoint; got: {checkpoints!r}"

    @pytest.mark.asyncio
    async def test_terminate_step_stored_in_context(self) -> None:
        """The terminate step records its own context entry before output renders.

        Order matters: workflow-level ``output:`` templates can reference
        ``{{ <terminate>.reason }}`` only if the entry is stored BEFORE the
        final output is rendered.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="upstream",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="upstream",
                    model="gpt-4",
                    prompt="x",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="finish")],
                ),
                AgentDef(
                    name="finish",
                    type="terminate",
                    status="success",
                    reason="all done",
                ),
            ],
            # Reference the terminate step's stored entry from workflow.output
            # to assert ordering.
            output={
                "reason": "{{ finish.output.reason }}",
                "by": "{{ finish.output.terminated_by }}",
            },
        )
        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {"value": "v"})

        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        assert result == {"reason": "all done", "by": "finish"}

    @pytest.mark.asyncio
    async def test_reason_rendered_against_context(self) -> None:
        """``reason`` is a Jinja2 template; refs resolve against accumulated context."""
        from conductor.exceptions import WorkflowTerminated

        config = self._config_with_terminate(
            "failed",
            reason="value was {{ upstream.output.value }}",
        )
        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {"value": "unsafe-input"})
        engine = WorkflowEngine(config, provider)
        with pytest.raises(WorkflowTerminated) as excinfo:
            await engine.run({})
        assert excinfo.value.reason == "value was unsafe-input"

    @pytest.mark.asyncio
    async def test_terminate_with_input_declared(self) -> None:
        """A terminate step may declare ``input:`` refs for template rendering.

        Mirrors the contract for other step types: declaring an input forces
        the engine to materialize that ref in the agent's context.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="upstream",
                runtime=RuntimeConfig(provider="copilot"),
                # explicit mode requires inputs to be declared
                context=ContextConfig(mode="explicit"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="upstream",
                    model="gpt-4",
                    prompt="x",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="finish")],
                ),
                AgentDef(
                    name="finish",
                    type="terminate",
                    status="success",
                    reason="ok",
                    input=["upstream.output"],
                    output_template={"echo": "{{ upstream.output.value }}"},
                ),
            ],
        )
        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {"value": "VAL"})
        engine = WorkflowEngine(config, provider)
        result = await engine.run({})
        assert result == {"echo": "VAL"}


class TestWorkflowEngineTerminateAdditionalScenarios:
    """Additional terminate-step engine coverage (issue #219).

    The base class above covers single-agent → terminate sequences. These
    tests exercise the corners surfaced during PR review:

    - Terminate as the workflow entry point (the dispatch path runs without
      any upstream context).
    - Terminate as a route target from a parallel group's routes and from a
      for_each group's routes (the main routing loop must dispatch to the
      terminate branch after the group completes).
    - Lifecycle hooks (``on_complete`` for success, ``on_error`` for failed)
      fire with the right arguments.
    """

    @pytest.mark.asyncio
    async def test_terminate_as_entry_point(self) -> None:
        """Workflow whose `entry_point` IS a terminate step ends immediately.

        Schema validation allows this; this test pins the engine actually
        dispatches it correctly when there is no upstream agent context to
        accumulate before the terminate branch runs.
        """
        from conductor.events import WorkflowEvent, WorkflowEventEmitter

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="entry-terminate",
                entry_point="bye",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=5),
            ),
            agents=[
                AgentDef(
                    name="bye",
                    type="terminate",
                    status="success",
                    reason="nothing to do",
                    output_template={"result": "no-op"},
                ),
            ],
            output={},
        )
        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)
        engine = WorkflowEngine(config, CopilotProvider(), event_emitter=emitter)

        result = await engine.run({})

        assert result == {"result": "no-op"}
        completed = [e for e in events if e.type == "workflow_completed"]
        assert len(completed) == 1
        assert completed[0].data["terminated_by"] == "bye"

    @pytest.mark.asyncio
    async def test_terminate_routed_from_parallel_group(self) -> None:
        """A parallel-group route may target a terminate step.

        The main routing loop dispatches the terminate step after the group
        completes; without this coverage a refactor of the parallel-group
        post-execution dispatch could silently break that hop.
        """
        from conductor.events import WorkflowEvent, WorkflowEventEmitter

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="par-terminate",
                entry_point="group",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="a",
                    model="gpt-4",
                    prompt="a",
                    output={"x": OutputField(type="string")},
                ),
                AgentDef(
                    name="b",
                    model="gpt-4",
                    prompt="b",
                    output={"y": OutputField(type="string")},
                ),
                AgentDef(
                    name="finish",
                    type="terminate",
                    status="success",
                    reason="parallel branches done",
                    output_template={"result": "from-parallel"},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="group",
                    agents=["a", "b"],
                    routes=[RouteDef(to="finish")],
                ),
            ],
            output={},
        )

        provider = CopilotProvider(
            mock_handler=lambda agent, *_a, **_kw: {"x": "ax"} if agent.name == "a" else {"y": "by"}
        )
        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)

        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        result = await engine.run({})

        assert result == {"result": "from-parallel"}
        # Verify the dispatch hop: a `route_taken` event must show
        # group → finish, and the terminate completion must follow.
        route_events = [
            e for e in events if e.type == "route_taken" and e.data.get("from_agent") == "group"
        ]
        assert route_events, f"expected route_taken from group; got {[e.type for e in events]}"
        assert route_events[0].data["to_agent"] == "finish"

    @pytest.mark.asyncio
    async def test_terminate_routed_from_for_each_group(self) -> None:
        """A for_each-group route may target a terminate step."""
        from conductor.config.schema import ForEachDef

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="fe-terminate",
                entry_point="finder",
                input={"items": InputDef(type="array", default=["a", "b"])},
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=20),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="x",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="loop")],
                ),
                AgentDef(
                    name="finish",
                    type="terminate",
                    status="success",
                    reason="for_each done",
                    output_template={"result": "from-for-each"},
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "loop",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "agent": AgentDef(
                            name="worker",
                            model="gpt-4",
                            prompt="process {{ item }}",
                            output={"r": OutputField(type="string")},
                        ),
                        "routes": [RouteDef(to="finish")],
                    }
                ),
            ],
            output={},
        )

        def mock_handler(agent, *_a, **_kw):
            if agent.name == "finder":
                return {"items": ["a", "b"]}
            return {"r": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)
        result = await engine.run({})
        assert result == {"result": "from-for-each"}

    @pytest.mark.asyncio
    async def test_on_complete_hook_fires_for_success_terminate(self) -> None:
        """`on_complete` hook must fire when a terminate step ends with success.

        Hooks are a public extension point; a refactor that dropped the
        `_execute_hook("on_complete", ...)` call from the success-terminate
        branch would silently regress every workflow that relies on the
        hook for completion notifications.
        """
        from unittest.mock import patch as _patch

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="hook-success",
                entry_point="bye",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=5),
                hooks=HooksConfig(on_complete="completed: {{ result }}"),
            ),
            agents=[
                AgentDef(
                    name="bye",
                    type="terminate",
                    status="success",
                    reason="all done",
                    output_template={"r": "ok"},
                ),
            ],
            output={},
        )
        engine = WorkflowEngine(config, CopilotProvider())
        with _patch.object(engine, "_execute_hook", wraps=engine._execute_hook) as spy:
            result = await engine.run({})
        assert result == {"r": "ok"}
        completion_calls = [
            call for call in spy.call_args_list if call.args and call.args[0] == "on_complete"
        ]
        assert len(completion_calls) == 1, (
            f"on_complete must fire exactly once; got: {spy.call_args_list}"
        )
        # The hook must receive the rendered output dict, not a raw template.
        assert completion_calls[0].kwargs.get("result") == {"r": "ok"}

    @pytest.mark.asyncio
    async def test_on_error_hook_fires_for_failed_terminate(self) -> None:
        """`on_error` hook must fire when a terminate step ends with failed.

        The hook receives the `WorkflowTerminated` exception so authors can
        notify on the structured `reason`/`terminated_by` rather than a
        generic error message.
        """
        from unittest.mock import patch as _patch

        from conductor.exceptions import WorkflowTerminated

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="hook-error",
                entry_point="abort",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=5),
                hooks=HooksConfig(on_error="failed: {{ error.message }}"),
            ),
            agents=[
                AgentDef(
                    name="abort",
                    type="terminate",
                    status="failed",
                    reason="halt",
                ),
            ],
            output={},
        )
        engine = WorkflowEngine(config, CopilotProvider())
        with (
            _patch.object(engine, "_execute_hook", wraps=engine._execute_hook) as spy,
            pytest.raises(WorkflowTerminated),
        ):
            await engine.run({})
        error_calls = [
            call for call in spy.call_args_list if call.args and call.args[0] == "on_error"
        ]
        assert len(error_calls) == 1, f"on_error must fire exactly once; got: {spy.call_args_list}"
        passed_error = error_calls[0].kwargs.get("error")
        assert isinstance(passed_error, WorkflowTerminated)
        assert passed_error.reason == "halt"
        assert passed_error.terminated_by == "abort"

    @pytest.mark.asyncio
    async def test_lifecycle_event_ordering_failed_terminate(self) -> None:
        """`agent_failed` must fire BEFORE `workflow_failed` for failed terminate.

        The dashboard's failure-counter UI relies on this ordering. Without
        the ordering assertion, a refactor that reversed the emits (or
        dropped `agent_failed`) would visually decouple agent and workflow
        failure states in the dashboard.
        """
        from conductor.events import WorkflowEvent, WorkflowEventEmitter
        from conductor.exceptions import WorkflowTerminated

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="order-test",
                entry_point="abort",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=5),
            ),
            agents=[
                AgentDef(name="abort", type="terminate", status="failed", reason="halt"),
            ],
            output={},
        )
        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)
        engine = WorkflowEngine(config, CopilotProvider(), event_emitter=emitter)
        with pytest.raises(WorkflowTerminated):
            await engine.run({})

        types_in_order = [e.type for e in events]
        af_index = types_in_order.index("agent_failed")
        wf_index = types_in_order.index("workflow_failed")
        assert af_index < wf_index, (
            f"agent_failed must precede workflow_failed; got order: {types_in_order}"
        )


class _RecordingWorkingDirProvider:
    """Minimal provider that records the ``working_dir`` of every agent passed to it.

    Duck-types the ``AgentProvider`` contract the engine consumes. Returns one
    structured field per declared output key so ``engine.run({})`` reaches a
    clean ``$end``.
    """

    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None]] = []
        self.calls: int = 0

    async def execute(
        self,
        agent,
        context,
        rendered_prompt,
        tools=None,
        interrupt_signal=None,
        event_callback=None,
    ):
        self.calls += 1
        self.seen.append((agent.name, agent.working_dir))
        content = dict.fromkeys(agent.output or {}, f"{agent.name}-ok")
        return AgentOutput(
            content=content,
            raw_response=None,
            model=agent.model,
            input_tokens=1,
            output_tokens=1,
        )

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def get_max_prompt_tokens(self, model: str):
        return None


def _single_agent_config(
    *,
    working_dir: str | None = None,
    runtime_working_dir: str | None = None,
    model: str = "gpt-4",
    max_tokens: int | None = None,
) -> WorkflowConfig:
    """One LLM agent routing straight to ``$end``."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="wd-single",
            entry_point="worker",
            runtime=RuntimeConfig(provider="copilot", working_dir=runtime_working_dir),
            context=ContextConfig(mode="accumulate", max_tokens=max_tokens),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            AgentDef(
                name="worker",
                model=model,
                prompt="Do work",
                working_dir=working_dir,
                output={"result": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={"result": "{{ worker.output.result }}"},
    )


def _workflow_file(tmp_path: Path) -> Path:
    wf_file = tmp_path / "workflow.yaml"
    wf_file.write_text("name: wd\n")
    return wf_file


class TestAgentWorkingDirResolution:
    """Engine resolution of ``AgentDef.working_dir`` / ``runtime.working_dir``.

    Covers linear, parallel, and for-each execution paths, the
    ``agent > runtime`` precedence, Jinja rendering against the per-agent
    context, relative-path resolution against the workflow file's directory,
    ``~`` expansion, and the missing-directory ``ExecutionError``.
    """

    @pytest.mark.asyncio
    async def test_linear_absolute_working_dir_reaches_provider(self, tmp_path: Path) -> None:
        """Requirement: an absolute ``working_dir`` on the agent is resolved by the
        engine and reaches the provider (the resolved value is set on AgentDef)."""
        target = tmp_path / "repo"
        target.mkdir()
        provider = _RecordingWorkingDirProvider()
        engine = WorkflowEngine(
            _single_agent_config(working_dir=str(target)),
            provider,
            workflow_path=_workflow_file(tmp_path),
        )

        await engine.run({})

        assert provider.seen == [("worker", os.path.normpath(str(target)))]

    @pytest.mark.asyncio
    async def test_agent_beats_runtime_precedence(self, tmp_path: Path) -> None:
        """Requirement: precedence ``agent.working_dir`` > ``runtime.working_dir``
        — when both are set, the provider sees the agent's value."""
        agent_dir = tmp_path / "agent-dir"
        runtime_dir = tmp_path / "runtime-dir"
        agent_dir.mkdir()
        runtime_dir.mkdir()
        provider = _RecordingWorkingDirProvider()
        engine = WorkflowEngine(
            _single_agent_config(working_dir=str(agent_dir), runtime_working_dir=str(runtime_dir)),
            provider,
            workflow_path=_workflow_file(tmp_path),
        )

        await engine.run({})

        assert provider.seen == [("worker", os.path.normpath(str(agent_dir)))]

    @pytest.mark.asyncio
    async def test_templated_runtime_working_dir_is_rendered(self, tmp_path: Path) -> None:
        """Requirement (Oracle r2 amendment): ``runtime.working_dir`` is also
        Jinja-rendered against the agent's context, not passed through raw."""
        target = tmp_path / "from-input"
        target.mkdir()
        provider = _RecordingWorkingDirProvider()
        engine = WorkflowEngine(
            _single_agent_config(runtime_working_dir="{{ workflow.input.target }}"),
            provider,
            workflow_path=_workflow_file(tmp_path),
        )

        await engine.run({"target": str(target)})

        assert provider.seen == [("worker", os.path.normpath(str(target)))]

    @pytest.mark.asyncio
    async def test_relative_working_dir_resolves_against_workflow_dir(self, tmp_path: Path) -> None:
        """Requirement: a relative ``working_dir`` resolves against the workflow
        file's directory (``self._workflow_dir``), not the process's current cwd."""
        (tmp_path / "sub").mkdir()
        provider = _RecordingWorkingDirProvider()
        engine = WorkflowEngine(
            _single_agent_config(working_dir="./sub"),
            provider,
            workflow_path=_workflow_file(tmp_path),
        )

        await engine.run({})

        assert provider.seen == [("worker", os.path.normpath(str(tmp_path / "sub")))]

    @pytest.mark.asyncio
    async def test_expanduser_tilde(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Requirement: ``~`` in ``working_dir`` is expanded via
        ``Path.expanduser()`` to an absolute home-directory path."""
        fake_home = tmp_path / "home"
        (fake_home / "proj").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))
        provider = _RecordingWorkingDirProvider()
        engine = WorkflowEngine(
            _single_agent_config(working_dir="~/proj"),
            provider,
            workflow_path=_workflow_file(tmp_path),
        )

        await engine.run({})

        assert provider.seen == [("worker", os.path.normpath(str(fake_home / "proj")))]

    @pytest.mark.asyncio
    async def test_missing_dir_raises_before_provider_call(self, tmp_path: Path) -> None:
        """Requirement: a nonexistent directory raises ``ExecutionError`` BEFORE
        the provider call (the provider must not be called at all)."""
        provider = _RecordingWorkingDirProvider()
        engine = WorkflowEngine(
            _single_agent_config(working_dir=str(tmp_path / "does-not-exist")),
            provider,
            workflow_path=_workflow_file(tmp_path),
        )

        with pytest.raises(ExecutionError, match="working_dir"):
            await engine.run({})

        assert provider.calls == 0

    @pytest.mark.asyncio
    async def test_no_working_dir_anywhere_passes_none(self, tmp_path: Path) -> None:
        """Requirement: when neither agent nor runtime sets ``working_dir``, the
        provider receives ``None`` (and falls back to ``os.getcwd()``)."""
        provider = _RecordingWorkingDirProvider()
        engine = WorkflowEngine(
            _single_agent_config(),
            provider,
            workflow_path=_workflow_file(tmp_path),
        )

        await engine.run({})

        assert provider.seen == [("worker", None)]

    @pytest.mark.asyncio
    async def test_parallel_agents_resolve_their_own_working_dir(self, tmp_path: Path) -> None:
        """Requirement: in a parallel group each agent is resolved individually
        after its own per-agent context is built (agent-level > runtime-level)."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        runtime_dir = tmp_path / "runtime"
        dir_a.mkdir()
        dir_b.mkdir()
        runtime_dir.mkdir()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="wd-parallel",
                entry_point="fan",
                runtime=RuntimeConfig(provider="copilot", working_dir=str(runtime_dir)),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="explicit_a",
                    model="gpt-4",
                    prompt="A",
                    working_dir=str(dir_a),
                    output={"r": OutputField(type="string")},
                ),
                AgentDef(
                    name="inherit_b",
                    model="gpt-4",
                    prompt="B",
                    output={"r": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="fan",
                    agents=["explicit_a", "inherit_b"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={},
        )
        provider = _RecordingWorkingDirProvider()
        engine = WorkflowEngine(config, provider, workflow_path=_workflow_file(tmp_path))

        await engine.run({})

        assert sorted(provider.seen) == [
            ("explicit_a", os.path.normpath(str(dir_a))),
            ("inherit_b", os.path.normpath(str(runtime_dir))),
        ]

    @pytest.mark.asyncio
    async def test_for_each_resolves_item_template_per_iteration(self, tmp_path: Path) -> None:
        """Requirement: in for_each, ``{{ item }}`` in the path resolves AFTER the
        loop variables are substituted — each iteration gets its own directory."""
        for name in ("one", "two"):
            (tmp_path / name).mkdir()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="wd-for-each",
                entry_point="lister",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="lister",
                    model="gpt-4",
                    prompt="List",
                    output={"repos": OutputField(type="array")},
                    routes=[RouteDef(to="fans")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="fans",
                    type="for_each",
                    source="lister.output.repos",
                    **{"as": "repo"},
                    agent=AgentDef(
                        name="fan_agent",
                        model="gpt-4",
                        prompt="Work {{ repo }}",
                        working_dir=str(tmp_path / "{{ repo }}"),
                        output={"r": OutputField(type="string")},
                    ),
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={},
        )
        provider = _RecordingWorkingDirProvider()

        async def _execute(agent, context, rendered_prompt, tools=None, **kwargs):
            if agent.name == "lister":
                return AgentOutput(
                    content={"repos": ["one", "two"]},
                    raw_response=None,
                    model=agent.model,
                    input_tokens=1,
                    output_tokens=1,
                )
            provider.seen.append((agent.name, agent.working_dir))
            return AgentOutput(
                content={"r": "ok"},
                raw_response=None,
                model=agent.model,
                input_tokens=1,
                output_tokens=1,
            )

        provider.execute = _execute  # type: ignore[method-assign]
        engine = WorkflowEngine(config, provider, workflow_path=_workflow_file(tmp_path))

        await engine.run({})

        assert sorted(provider.seen) == [
            ("fan_agent[0]", os.path.normpath(str(tmp_path / "one"))),
            ("fan_agent[1]", os.path.normpath(str(tmp_path / "two"))),
        ]

    @pytest.mark.asyncio
    async def test_templated_model_does_not_clobber_resolved_working_dir(
        self, tmp_path: Path
    ) -> None:
        """Requirement (Oracle r3 amendment, regression): a templated ``model`` is
        rendered in ``AgentExecutor`` via ``model_copy(update={...})`` — a merge,
        so the engine-resolved ``working_dir`` must survive to the provider."""
        target = tmp_path / "repo"
        target.mkdir()
        provider = _RecordingWorkingDirProvider()
        engine = WorkflowEngine(
            _single_agent_config(working_dir=str(target), model="{{ workflow.input.model }}"),
            provider,
            workflow_path=_workflow_file(tmp_path),
        )

        await engine.run({"model": "gpt-4o"})

        assert provider.seen == [("worker", os.path.normpath(str(target)))]

    @pytest.mark.asyncio
    async def test_agent_started_carries_working_dir_and_context_window_after_trim(
        self, tmp_path: Path
    ) -> None:
        """Requirement (Oracle r1 amendment, trim ordering): with a small
        ``max_tokens``, the ``agent_started`` event carries BOTH the resolved
        ``working_dir`` AND ``context_window_max``, and the context trim ran
        BEFORE agent_context was built (the prompt is already trimmed)."""
        target = tmp_path / "repo"
        target.mkdir()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="wd-trim",
                entry_point="first",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate", max_tokens=1),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="first",
                    model="gpt-4",
                    prompt="First",
                    output={"blob": OutputField(type="string")},
                    routes=[RouteDef(to="second")],
                ),
                AgentDef(
                    name="second",
                    model="gpt-4",
                    prompt="Second",
                    working_dir=str(target),
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ second.output.result }}"},
        )
        provider = _RecordingWorkingDirProvider()
        events: list = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)
        engine = WorkflowEngine(
            config, provider, event_emitter=emitter, workflow_path=_workflow_file(tmp_path)
        )

        await engine.run({})

        started = [
            e for e in events if e.type == "agent_started" and e.data["agent_name"] == "second"
        ]
        assert len(started) == 1
        assert started[0].data["working_dir"] == os.path.normpath(str(target))
        assert "context_window_max" in started[0].data
        assert provider.seen[-1][0] == "second"


class TestWorkingDirEvents:
    """Observability events carrying the engine-resolved ``working_dir``.

    The linear path is covered by ``agent_started`` (todo 2, commit 075436b);
    this class pins the two additive LLM-only events —
    ``parallel_agent_started`` and ``for_each_agent_started`` — emitted right
    after each per-agent/per-item ``working_dir`` resolution, plus regression
    guards that the pre-existing envelope events keep their exact payloads.
    """

    @pytest.mark.asyncio
    async def test_parallel_agent_started_carries_resolved_working_dir(
        self, tmp_path: Path
    ) -> None:
        """Requirement: ``parallel_agent_started`` fires per LLM member AFTER its
        own resolution and carries ``group_name``, ``agent_name`` and the
        resolved ``working_dir`` (agent-level beats the runtime default)."""
        dir_a = tmp_path / "a"
        runtime_dir = tmp_path / "runtime"
        dir_a.mkdir()
        runtime_dir.mkdir()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="wd-ev-parallel",
                entry_point="fan",
                runtime=RuntimeConfig(provider="copilot", working_dir=str(runtime_dir)),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="explicit_a",
                    model="gpt-4",
                    prompt="A",
                    working_dir=str(dir_a),
                    output={"r": OutputField(type="string")},
                ),
                AgentDef(
                    name="inherit_b",
                    model="gpt-4",
                    prompt="B",
                    output={"r": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="fan",
                    agents=["explicit_a", "inherit_b"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={},
        )
        provider = _RecordingWorkingDirProvider()
        events: list = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)
        engine = WorkflowEngine(
            config, provider, event_emitter=emitter, workflow_path=_workflow_file(tmp_path)
        )

        await engine.run({})

        started = sorted(
            (e for e in events if e.type == "parallel_agent_started"),
            key=lambda e: e.data["agent_name"],
        )
        observed = [
            (e.data["group_name"], e.data["agent_name"], e.data["working_dir"]) for e in started
        ]
        assert observed == [
            ("fan", "explicit_a", os.path.normpath(str(dir_a))),
            ("fan", "inherit_b", os.path.normpath(str(runtime_dir))),
        ]

    @pytest.mark.asyncio
    async def test_parallel_agent_started_working_dir_none_when_unset(self, tmp_path: Path) -> None:
        """Requirement: without any ``working_dir`` the event stays valid and
        carries ``working_dir=None`` (the provider falls back to its own cwd)."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="wd-ev-parallel-none",
                entry_point="fan",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="solo",
                    model="gpt-4",
                    prompt="Solo",
                    output={"r": OutputField(type="string")},
                ),
                AgentDef(
                    name="other",
                    model="gpt-4",
                    prompt="Other",
                    output={"r": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(name="fan", agents=["solo", "other"], routes=[RouteDef(to="$end")]),
            ],
            output={},
        )
        provider = _RecordingWorkingDirProvider()
        events: list = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)
        engine = WorkflowEngine(
            config, provider, event_emitter=emitter, workflow_path=_workflow_file(tmp_path)
        )

        await engine.run({})

        started = [e for e in events if e.type == "parallel_agent_started"]
        assert len(started) == 2
        solo = next(e for e in started if e.data["agent_name"] == "solo")
        assert solo.data["group_name"] == "fan"
        assert solo.data["working_dir"] is None

    @pytest.mark.asyncio
    async def test_parallel_started_payload_unchanged_regression(self, tmp_path: Path) -> None:
        """Requirement (regression): the existing ``parallel_started`` envelope
        event keeps its exact pre-change payload (``group_name`` + ``agents``
        only) — ``working_dir`` observability is strictly additive."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="wd-ev-parallel-reg",
                entry_point="fan",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="solo",
                    model="gpt-4",
                    prompt="Solo",
                    output={"r": OutputField(type="string")},
                ),
                AgentDef(
                    name="other",
                    model="gpt-4",
                    prompt="Other",
                    output={"r": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(name="fan", agents=["solo", "other"], routes=[RouteDef(to="$end")]),
            ],
            output={},
        )
        provider = _RecordingWorkingDirProvider()
        events: list = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)
        engine = WorkflowEngine(
            config, provider, event_emitter=emitter, workflow_path=_workflow_file(tmp_path)
        )

        await engine.run({})

        envelope = [e for e in events if e.type == "parallel_started"]
        assert len(envelope) == 1
        assert envelope[0].data == {"group_name": "fan", "agents": ["solo", "other"]}

    @pytest.mark.asyncio
    async def test_for_each_agent_started_carries_resolved_working_dir_per_item(
        self, tmp_path: Path
    ) -> None:
        """Requirement: ``for_each_agent_started`` fires per item AFTER the
        ``{{ item }}`` template resolves and carries ``group_name``, the
        qualified ``agent_name`` (``name[key]``), ``item_key`` and the resolved
        per-iteration ``working_dir``."""
        for name in ("one", "two"):
            (tmp_path / name).mkdir()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="wd-ev-for-each",
                entry_point="lister",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="lister",
                    model="gpt-4",
                    prompt="List",
                    output={"repos": OutputField(type="array")},
                    routes=[RouteDef(to="fans")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="fans",
                    type="for_each",
                    source="lister.output.repos",
                    **{"as": "repo"},
                    agent=AgentDef(
                        name="fan_agent",
                        model="gpt-4",
                        prompt="Work {{ repo }}",
                        working_dir=str(tmp_path / "{{ repo }}"),
                        output={"r": OutputField(type="string")},
                    ),
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={},
        )
        provider = _RecordingWorkingDirProvider()

        async def _execute(agent, context, rendered_prompt, tools=None, **kwargs):
            if agent.name == "lister":
                return AgentOutput(
                    content={"repos": ["one", "two"]},
                    raw_response=None,
                    model=agent.model,
                    input_tokens=1,
                    output_tokens=1,
                )
            return AgentOutput(
                content={"r": "ok"},
                raw_response=None,
                model=agent.model,
                input_tokens=1,
                output_tokens=1,
            )

        provider.execute = _execute  # type: ignore[method-assign]
        events: list = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)
        engine = WorkflowEngine(
            config, provider, event_emitter=emitter, workflow_path=_workflow_file(tmp_path)
        )

        await engine.run({})

        started = sorted(
            (e for e in events if e.type == "for_each_agent_started"),
            key=lambda e: e.data["item_key"],
        )
        assert [
            (e.data["group_name"], e.data["agent_name"], e.data["item_key"], e.data["working_dir"])
            for e in started
        ] == [
            ("fans", "fan_agent[0]", "0", os.path.normpath(str(tmp_path / "one"))),
            ("fans", "fan_agent[1]", "1", os.path.normpath(str(tmp_path / "two"))),
        ]

    @pytest.mark.asyncio
    async def test_for_each_agent_started_working_dir_none_when_unset(self, tmp_path: Path) -> None:
        """Requirement: without any ``working_dir`` the per-item event stays
        valid and carries ``working_dir=None``."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="wd-ev-for-each-none",
                entry_point="lister",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="lister",
                    model="gpt-4",
                    prompt="List",
                    output={"repos": OutputField(type="array")},
                    routes=[RouteDef(to="fans")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="fans",
                    type="for_each",
                    source="lister.output.repos",
                    **{"as": "repo"},
                    agent=AgentDef(
                        name="fan_agent",
                        model="gpt-4",
                        prompt="Work {{ repo }}",
                        output={"r": OutputField(type="string")},
                    ),
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={},
        )
        provider = _RecordingWorkingDirProvider()

        async def _execute(agent, context, rendered_prompt, tools=None, **kwargs):
            if agent.name == "lister":
                return AgentOutput(
                    content={"repos": ["one"]},
                    raw_response=None,
                    model=agent.model,
                    input_tokens=1,
                    output_tokens=1,
                )
            return AgentOutput(
                content={"r": "ok"},
                raw_response=None,
                model=agent.model,
                input_tokens=1,
                output_tokens=1,
            )

        provider.execute = _execute  # type: ignore[method-assign]
        events: list = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)
        engine = WorkflowEngine(
            config, provider, event_emitter=emitter, workflow_path=_workflow_file(tmp_path)
        )

        await engine.run({})

        started = [e for e in events if e.type == "for_each_agent_started"]
        assert len(started) == 1
        assert started[0].data["agent_name"] == "fan_agent[0]"
        assert started[0].data["item_key"] == "0"
        assert started[0].data["working_dir"] is None

    @pytest.mark.asyncio
    async def test_for_each_item_started_payload_unchanged_regression(self, tmp_path: Path) -> None:
        """Requirement (regression): the existing ``for_each_item_started``
        envelope event keeps its exact pre-change payload (``group_name`` +
        ``item_key`` + ``index``) — the new event is strictly additive."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="wd-ev-for-each-reg",
                entry_point="lister",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="lister",
                    model="gpt-4",
                    prompt="List",
                    output={"repos": OutputField(type="array")},
                    routes=[RouteDef(to="fans")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="fans",
                    type="for_each",
                    source="lister.output.repos",
                    **{"as": "repo"},
                    agent=AgentDef(
                        name="fan_agent",
                        model="gpt-4",
                        prompt="Work {{ repo }}",
                        output={"r": OutputField(type="string")},
                    ),
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={},
        )
        provider = _RecordingWorkingDirProvider()

        async def _execute(agent, context, rendered_prompt, tools=None, **kwargs):
            if agent.name == "lister":
                return AgentOutput(
                    content={"repos": ["one"]},
                    raw_response=None,
                    model=agent.model,
                    input_tokens=1,
                    output_tokens=1,
                )
            return AgentOutput(
                content={"r": "ok"},
                raw_response=None,
                model=agent.model,
                input_tokens=1,
                output_tokens=1,
            )

        provider.execute = _execute  # type: ignore[method-assign]
        events: list = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)
        engine = WorkflowEngine(
            config, provider, event_emitter=emitter, workflow_path=_workflow_file(tmp_path)
        )

        await engine.run({})

        envelope = [e for e in events if e.type == "for_each_item_started"]
        assert len(envelope) == 1
        assert envelope[0].data == {"group_name": "fans", "item_key": "0", "index": 0}
