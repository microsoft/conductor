"""Integration tests for complete workflow execution.

These tests verify that:
- Workflows from fixture files execute correctly
- All components work together (config, engine, provider, router)
- Different workflow patterns (linear, loop, human gate) work as expected
"""

from pathlib import Path
from typing import Any

import pytest

from conductor.config.loader import load_config
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.copilot import CopilotProvider


class TestSimpleWorkflowIntegration:
    """Integration tests for simple linear workflows."""

    def test_valid_simple_workflow_executes(self, fixtures_dir: Path) -> None:
        """Test that valid_simple.yaml executes successfully."""
        workflow_file = fixtures_dir / "valid_simple.yaml"
        config = load_config(workflow_file)

        def mock_handler(agent, prompt, context):
            if agent.name == "greeter":
                # Verify prompt contains the expected template variable
                assert "workflow.input.name" in agent.prompt or "name" in prompt.lower()
                return {"greeting": "Hello, Test User!"}
            return {"result": "unknown"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        import asyncio

        result = asyncio.run(engine.run({"name": "Test User"}))

        assert "message" in result
        assert result["message"] == "Hello, Test User!"

    def test_workflow_with_provided_input(self, fixtures_dir: Path) -> None:
        """Test workflow execution with provided workflow input."""
        workflow_file = fixtures_dir / "valid_simple.yaml"
        config = load_config(workflow_file)

        def mock_handler(agent, prompt, context):
            # Use a simple greeting
            return {"greeting": "Hello there!"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        import asyncio

        # Provide the required name input
        result = asyncio.run(engine.run({"name": "World"}))

        assert "message" in result
        assert result["message"] == "Hello there!"


class TestFullWorkflowIntegration:
    """Integration tests for full-featured workflows."""

    def test_valid_full_workflow_structure(self, fixtures_dir: Path) -> None:
        """Test that valid_full.yaml loads and can be analyzed."""
        workflow_file = fixtures_dir / "valid_full.yaml"
        config = load_config(workflow_file)

        # Verify structure
        assert config.workflow.name == "full-workflow"
        assert config.workflow.entry_point == "planner"
        assert len(config.agents) == 4  # planner, refiner, reviewer, executor
        assert config.tools == ["web_search", "file_reader", "code_executor"]

    def test_full_workflow_with_mocked_agents(self, fixtures_dir: Path) -> None:
        """Test executing full workflow with all agents mocked."""
        workflow_file = fixtures_dir / "valid_full.yaml"
        config = load_config(workflow_file)

        agent_calls: list[str] = []

        def mock_handler(agent, prompt, context):
            agent_calls.append(agent.name)

            if agent.name == "planner":
                return {
                    "plan": [
                        {"step": "1", "action": "Research"},
                        {"step": "2", "action": "Execute"},
                    ],
                    "confidence": 0.9,  # High confidence, goes to reviewer
                }
            elif agent.name == "refiner":
                return {"plan": [], "confidence": 0.95}
            elif agent.name == "executor":
                return {
                    "result": {
                        "success": True,
                        "output": "Execution completed successfully",
                    }
                }
            return {}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, skip_gates=True)

        import asyncio

        asyncio.run(engine.run({"goal": "Test goal", "max_steps": 3}))

        # Verify execution flow
        assert "planner" in agent_calls
        # With confidence >= 0.8, should go to reviewer (skip_gates=True picks first option)
        assert "reviewer" in engine.context.execution_history

    def test_full_workflow_low_confidence_path(self, fixtures_dir: Path) -> None:
        """Test workflow takes refiner path on low confidence."""
        workflow_file = fixtures_dir / "valid_full.yaml"
        config = load_config(workflow_file)

        def mock_handler(agent, prompt, context):
            if agent.name == "planner":
                return {
                    "plan": [{"step": "1", "action": "Draft"}],
                    "confidence": 0.5,  # Low confidence
                }
            elif agent.name == "refiner":
                return {
                    "plan": [{"step": "1", "action": "Refined"}],
                    "confidence": 0.9,
                }
            elif agent.name == "executor":
                return {"result": {"success": True, "output": "Done"}}
            return {}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, skip_gates=True)

        import asyncio

        asyncio.run(engine.run({"goal": "Test", "max_steps": 5}))

        # Should have gone through refiner
        assert "refiner" in engine.context.execution_history


class TestLoopBackIntegration:
    """Integration tests for loop-back workflow patterns."""

    def test_loop_until_quality_threshold(self) -> None:
        """Test workflow loops until quality threshold is met."""
        from conductor.config.schema import (
            AgentDef,
            ContextConfig,
            LimitsConfig,
            OutputField,
            RouteDef,
            WorkflowConfig,
            WorkflowDef,
        )

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="quality-loop",
                entry_point="improver",
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="improver",
                    model="gpt-4",
                    prompt="Improve content. Iteration: {{ context.iteration | default(1) }}",
                    output={
                        "content": OutputField(type="string"),
                        "quality": OutputField(type="number"),
                    },
                    routes=[
                        RouteDef(to="$end", when="quality >= 0.9"),  # Use arithmetic
                        RouteDef(to="improver"),  # Loop back
                    ],
                ),
            ],
            output={
                "final_content": "{{ improver.output.content }}",
                "iterations": "{{ context.iteration }}",
            },
        )

        iteration = 0

        def mock_handler(agent, prompt, context):
            nonlocal iteration
            iteration += 1
            # Quality improves each iteration
            quality = 0.3 + (iteration * 0.2)  # 0.5, 0.7, 0.9, ...
            return {
                "content": f"Improved content v{iteration}",
                "quality": quality,
            }

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        import asyncio

        result = asyncio.run(engine.run({}))

        # Should have looped 3 times (0.5, 0.7, 0.9)
        assert iteration == 3
        assert result["iterations"] == 3
        assert "v3" in result["final_content"]

    def test_loop_with_feedback_accumulation(self) -> None:
        """Test that loop iterations accumulate context correctly."""
        from conductor.config.schema import (
            AgentDef,
            ContextConfig,
            OutputField,
            RouteDef,
            WorkflowConfig,
            WorkflowDef,
        )

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="feedback-loop",
                entry_point="creator",
                context=ContextConfig(mode="accumulate"),
            ),
            agents=[
                AgentDef(
                    name="creator",
                    model="gpt-4",
                    prompt="Create content",
                    output={"content": OutputField(type="string")},
                    routes=[RouteDef(to="critic")],
                ),
                AgentDef(
                    name="critic",
                    model="gpt-4",
                    prompt="Critique: {{ creator.output.content }}",
                    output={
                        "feedback": OutputField(type="string"),
                        "approved": OutputField(type="boolean"),
                    },
                    routes=[
                        RouteDef(to="$end", when="{{ output.approved }}"),
                        RouteDef(to="creator"),  # Loop back for revision
                    ],
                ),
            ],
            output={"result": "{{ creator.output.content }}"},
        )

        creator_calls = 0
        contexts_received: list[dict[str, Any]] = []

        def mock_handler(agent, prompt, context):
            nonlocal creator_calls
            contexts_received.append(context.copy())

            if agent.name == "creator":
                creator_calls += 1
                return {"content": f"Draft v{creator_calls}"}
            else:  # critic
                return {
                    "feedback": "Needs work" if creator_calls < 2 else "Good",
                    "approved": creator_calls >= 2,
                }

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        import asyncio

        result = asyncio.run(engine.run({}))

        # Should have looped once (creator -> critic -> creator -> critic -> $end)
        assert creator_calls == 2
        assert result["result"] == "Draft v2"


class TestHumanGateIntegration:
    """Integration tests for human gate workflows."""

    def test_human_gate_with_skip_gates(self) -> None:
        """Test human gate auto-selects first option with skip_gates."""
        from conductor.config.schema import (
            AgentDef,
            GateOption,
            OutputField,
            RouteDef,
            WorkflowConfig,
            WorkflowDef,
        )

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="gate-workflow",
                entry_point="prepare",
            ),
            agents=[
                AgentDef(
                    name="prepare",
                    model="gpt-4",
                    prompt="Prepare proposal",
                    output={"proposal": OutputField(type="string")},
                    routes=[RouteDef(to="approval")],
                ),
                AgentDef(
                    name="approval",
                    type="human_gate",
                    prompt="Review proposal: {{ prepare.output.proposal }}",
                    options=[
                        GateOption(label="Approve", value="approved", route="execute"),
                        GateOption(label="Reject", value="rejected", route="$end"),
                    ],
                ),
                AgentDef(
                    name="execute",
                    model="gpt-4",
                    prompt="Execute proposal",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "outcome": "{{ approval.output.selected }}",
                "execution": "{{ execute.output.result | default('not executed') }}",
            },
        )

        def mock_handler(agent, prompt, context):
            if agent.name == "prepare":
                return {"proposal": "Test proposal"}
            elif agent.name == "execute":
                return {"result": "Executed successfully"}
            return {}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, skip_gates=True)

        import asyncio

        result = asyncio.run(engine.run({}))

        # First option (Approve) should be auto-selected
        assert result["outcome"] == "approved"
        assert result["execution"] == "Executed successfully"
        assert "execute" in engine.context.execution_history

    def test_human_gate_routes_to_end(self) -> None:
        """Test human gate that routes directly to $end."""
        from conductor.config.schema import (
            AgentDef,
            GateOption,
            OutputField,
            WorkflowConfig,
            WorkflowDef,
        )

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="gate-to-end",
                entry_point="confirmation",
            ),
            agents=[
                AgentDef(
                    name="confirmation",
                    type="human_gate",
                    prompt="Confirm action?",
                    options=[
                        GateOption(label="Cancel", value="cancelled", route="$end"),
                        GateOption(label="Proceed", value="proceeded", route="action"),
                    ],
                ),
                AgentDef(
                    name="action",
                    model="gpt-4",
                    prompt="Take action",
                    output={"done": OutputField(type="boolean")},
                ),
            ],
            output={"status": "{{ confirmation.output.selected }}"},
        )

        provider = CopilotProvider()
        engine = WorkflowEngine(config, provider, skip_gates=True)

        import asyncio

        result = asyncio.run(engine.run({}))

        # First option (Cancel) routes to $end
        assert result["status"] == "cancelled"
        assert "action" not in engine.context.execution_history


class TestToolWorkflowIntegration:
    """Integration tests for workflows with tool configurations."""

    def test_workflow_with_tools(self) -> None:
        """Test that tools are passed to provider correctly."""
        from conductor.config.schema import (
            AgentDef,
            OutputField,
            RouteDef,
            WorkflowConfig,
            WorkflowDef,
        )

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="tool-workflow",
                entry_point="researcher",
            ),
            tools=["web_search", "calculator", "file_reader"],
            agents=[
                AgentDef(
                    name="researcher",
                    model="gpt-4",
                    prompt="Research the topic",
                    tools=["web_search"],  # Subset of workflow tools
                    output={"findings": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ researcher.output.findings }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"findings": "Research results"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        import asyncio

        result = asyncio.run(engine.run({}))

        assert result["result"] == "Research results"

        # Verify tools were in call history
        call_history = provider.get_call_history()
        assert len(call_history) == 1
        assert call_history[0]["tools"] == ["web_search"]


class TestContextModeIntegration:
    """Integration tests for different context accumulation modes."""

    def test_accumulate_mode_preserves_all_outputs(self) -> None:
        """Test that accumulate mode keeps all prior agent outputs."""
        from conductor.config.schema import (
            AgentDef,
            ContextConfig,
            OutputField,
            RouteDef,
            WorkflowConfig,
            WorkflowDef,
        )

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="accumulate-test",
                entry_point="agent1",
                context=ContextConfig(mode="accumulate"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="First",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Second",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="agent3")],
                ),
                AgentDef(
                    name="agent3",
                    model="gpt-4",
                    prompt="Third",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "done"},
        )

        contexts_seen: list[dict[str, Any]] = []

        def mock_handler(agent, prompt, context):
            contexts_seen.append((agent.name, context.copy()))
            return {"value": f"output_{agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        import asyncio

        asyncio.run(engine.run({}))

        # Agent3 should see both agent1 and agent2 outputs
        agent3_context = contexts_seen[2][1]
        assert "agent1" in agent3_context
        assert "agent2" in agent3_context

    def test_last_only_mode_has_previous_only(self) -> None:
        """Test that last_only mode only keeps previous agent output."""
        from conductor.config.schema import (
            AgentDef,
            ContextConfig,
            OutputField,
            RouteDef,
            WorkflowConfig,
            WorkflowDef,
        )

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="last-only-test",
                entry_point="agent1",
                context=ContextConfig(mode="last_only"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="First",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Second",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="agent3")],
                ),
                AgentDef(
                    name="agent3",
                    model="gpt-4",
                    prompt="Third",
                    output={"value": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "done"},
        )

        contexts_seen: list[tuple[str, dict[str, Any]]] = []

        def mock_handler(agent, prompt, context):
            contexts_seen.append((agent.name, context.copy()))
            return {"value": f"output_{agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        import asyncio

        asyncio.run(engine.run({}))

        # Agent3 should only see agent2's output
        agent3_context = contexts_seen[2][1]
        assert "agent2" in agent3_context
        assert "agent1" not in agent3_context


class TestEnvVarWorkflowIntegration:
    """Integration tests for workflows with environment variables."""

    def test_env_var_resolution(self, fixtures_dir: Path, monkeypatch) -> None:
        """Test that environment variables are resolved in workflow."""
        # Set environment variable - must match ${AGENT_MODEL:-gpt-4} from fixture
        monkeypatch.setenv("AGENT_MODEL", "gpt-4-turbo")

        workflow_file = fixtures_dir / "valid_env_vars.yaml"
        config = load_config(workflow_file)

        # The model should be resolved from env var
        agent = config.agents[0]
        assert agent.model == "gpt-4-turbo"

    def test_env_var_with_default(self, fixtures_dir: Path, monkeypatch) -> None:
        """Test that env var default values work."""
        # Don't set the env var, should use default from ${AGENT_MODEL:-gpt-4}
        monkeypatch.delenv("AGENT_MODEL", raising=False)

        workflow_file = fixtures_dir / "valid_env_vars.yaml"
        config = load_config(workflow_file)

        # Should use default value gpt-4
        agent = config.agents[0]
        assert agent.model == "gpt-4"


class TestDryRunIntegration:
    """Integration tests for dry-run execution plan."""

    def test_dry_run_plan_generation(self, fixtures_dir: Path) -> None:
        """Test that dry-run generates accurate execution plan."""
        workflow_file = fixtures_dir / "valid_full.yaml"
        config = load_config(workflow_file)

        provider = CopilotProvider()
        engine = WorkflowEngine(config, provider)

        plan = engine.build_execution_plan()

        assert plan.workflow_name == "full-workflow"
        assert plan.entry_point == "planner"
        assert len(plan.steps) > 0

        # Find the planner step
        planner_step = next(s for s in plan.steps if s.agent_name == "planner")
        assert planner_step.model is not None
        assert len(planner_step.routes) > 0

    def test_dry_run_detects_loops(self) -> None:
        """Test that dry-run identifies loop patterns."""
        from conductor.config.schema import (
            AgentDef,
            OutputField,
            RouteDef,
            WorkflowConfig,
            WorkflowDef,
        )

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="loop-detection",
                entry_point="start",
            ),
            agents=[
                AgentDef(
                    name="start",
                    model="gpt-4",
                    prompt="Start",
                    output={"value": OutputField(type="number")},
                    routes=[RouteDef(to="checker")],
                ),
                AgentDef(
                    name="checker",
                    model="gpt-4",
                    prompt="Check",
                    output={"done": OutputField(type="boolean")},
                    routes=[
                        RouteDef(to="$end", when="{{ output.done }}"),
                        RouteDef(to="start"),  # Loop back!
                    ],
                ),
            ],
            output={"result": "done"},
        )

        provider = CopilotProvider()
        engine = WorkflowEngine(config, provider)

        plan = engine.build_execution_plan()

        # Start should be marked as a loop target
        start_step = next(s for s in plan.steps if s.agent_name == "start")
        assert start_step.is_loop_target is True


class TestErrorHandlingIntegration:
    """Integration tests for error handling scenarios."""

    def test_invalid_route_target_error(self, fixtures_dir: Path) -> None:
        """Test that invalid route target raises clear error during validation."""
        from conductor.exceptions import ConfigurationError

        workflow_file = fixtures_dir / "invalid_bad_route.yaml"

        with pytest.raises(ConfigurationError) as exc_info:
            load_config(workflow_file)

        assert "route" in str(exc_info.value).lower() or "agent" in str(exc_info.value).lower()

    def test_missing_entry_point_error(self, fixtures_dir: Path) -> None:
        """Test that missing entry point raises validation error."""
        from conductor.exceptions import ConfigurationError

        workflow_file = fixtures_dir / "invalid_missing_entry.yaml"

        with pytest.raises(ConfigurationError) as exc_info:
            load_config(workflow_file)

        assert "entry" in str(exc_info.value).lower() or "not found" in str(exc_info.value).lower()


class TestBackwardCompatibility:
    """PE-7.6: Test that existing workflows are not affected by parallel execution feature."""

    def test_existing_simple_workflow_unchanged(self, fixtures_dir: Path) -> None:
        """Test that simple workflows without parallel groups work identically."""
        workflow_file = fixtures_dir / "valid_simple.yaml"
        config = load_config(workflow_file)

        # Verify no parallel groups in config
        assert (
            not hasattr(config, "parallel") or config.parallel is None or len(config.parallel) == 0
        )

        def mock_handler(agent, prompt, context):
            if agent.name == "greeter":
                return {"greeting": "Hello, Tester!"}
            return {}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        import asyncio

        result = asyncio.run(engine.run({"name": "Tester"}))

        # Should work exactly as before
        assert result["message"] == "Hello, Tester!"

    def test_existing_full_workflow_unchanged(self, fixtures_dir: Path) -> None:
        """Test that complex workflows without parallel groups work identically."""
        workflow_file = fixtures_dir / "valid_full.yaml"
        config = load_config(workflow_file)

        # Verify no parallel groups
        assert (
            not hasattr(config, "parallel") or config.parallel is None or len(config.parallel) == 0
        )

        agent_calls: list[str] = []

        def mock_handler(agent, prompt, context):
            agent_calls.append(agent.name)

            if agent.name == "planner":
                return {"plan": [{"step": "1"}], "confidence": 0.9}
            elif agent.name == "executor":
                return {"result": {"success": True, "output": "Done"}}
            return {}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, skip_gates=True)

        import asyncio

        asyncio.run(engine.run({"goal": "Test", "max_steps": 1}))

        # Should execute as before
        assert "planner" in agent_calls

    def test_loop_workflows_still_work(self) -> None:
        """Test that loop patterns continue to work without parallel."""
        from conductor.config.schema import (
            AgentDef,
            LimitsConfig,
            OutputField,
            RouteDef,
            WorkflowConfig,
            WorkflowDef,
        )

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="loop-workflow",
                entry_point="counter",
                limits=LimitsConfig(max_iterations=20),
            ),
            agents=[
                AgentDef(
                    name="counter",
                    model="gpt-4",
                    prompt="Count",
                    output={"count": OutputField(type="number")},
                    routes=[
                        RouteDef(to="$end", when="count >= 5"),
                        RouteDef(to="counter"),
                    ],
                ),
            ],
            output={"final_count": "{{ counter.output.count }}"},
        )

        call_count = {"count": 0}

        def mock_handler(agent, prompt, context):
            call_count["count"] += 1
            return {"count": call_count["count"]}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        import asyncio

        result = asyncio.run(engine.run({}))

        # Should still loop correctly
        assert result["final_count"] == 5
        assert call_count["count"] == 5

    def test_routing_workflows_still_work(self) -> None:
        """Test that conditional routing still works without parallel."""
        from conductor.config.schema import (
            AgentDef,
            OutputField,
            RouteDef,
            WorkflowConfig,
            WorkflowDef,
        )

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="routing-workflow",
                entry_point="classifier",
            ),
            agents=[
                AgentDef(
                    name="classifier",
                    model="gpt-4",
                    prompt="Classify",
                    output={"category": OutputField(type="string")},
                    routes=[
                        RouteDef(to="handler_a", when="category == 'A'"),
                        RouteDef(to="handler_b", when="category == 'B'"),
                        RouteDef(to="handler_default"),
                    ],
                ),
                AgentDef(
                    name="handler_a",
                    model="gpt-4",
                    prompt="Handle A",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
                AgentDef(
                    name="handler_b",
                    model="gpt-4",
                    prompt="Handle B",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
                AgentDef(
                    name="handler_default",
                    model="gpt-4",
                    prompt="Handle default",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "result": (
                    "{{ handler_a.output.result if handler_a is defined else "
                    "(handler_b.output.result if handler_b is defined else "
                    "handler_default.output.result) }}"
                )
            },
        )

        def mock_handler(agent, prompt, context):
            if agent.name == "classifier":
                return {"category": "A"}
            elif agent.name == "handler_a":
                return {"result": "Handled by A"}
            return {"result": "default"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        import asyncio

        result = asyncio.run(engine.run({}))

        # Should route correctly
        assert result["result"] == "Handled by A"
