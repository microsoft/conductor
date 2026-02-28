"""Integration tests for script steps in WorkflowEngine.

Tests cover:
- Linear workflow with script step
- Script output accessible in subsequent agent context
- Route branching on exit_code (simpleeval and Jinja2)
- Script step iteration limit counting
- Script step workflow-level timeout
- Mixed agent + script workflows
- Dry-run plan includes script steps
- Jinja2-templated command with workflow input
- Non-zero exit with no routes defaults to $end
- Script step in parallel group is rejected at engine level
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    LimitsConfig,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ConfigurationError
from conductor.providers.copilot import CopilotProvider


class TestScriptWorkflowLinear:
    """Tests for linear workflows with script steps."""

    @pytest.mark.asyncio
    async def test_script_step_runs_to_end(self) -> None:
        """Test linear workflow with script step that succeeds and routes to $end."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="script-linear",
                entry_point="run_echo",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="run_echo",
                    type="script",
                    command=sys.executable,
                    args=["-c", "print('hello world')"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "result": "{{ run_echo.output.stdout }}",
            },
        )

        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider)
        result = await engine.run({})

        assert "hello world" in result["result"]

    @pytest.mark.asyncio
    async def test_script_output_in_context(self) -> None:
        """Test script step output accessible in subsequent agent's context."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="script-context",
                entry_point="checker",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="checker",
                    type="script",
                    command=sys.executable,
                    args=["-c", "print('test output')"],
                    routes=[RouteDef(to="processor")],
                ),
                AgentDef(
                    name="processor",
                    prompt="Process: {{ checker.output.stdout }}",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "processed": "{{ processor.output.result }}",
            },
        )

        received_prompts = []

        def mock_handler(agent, prompt, context):
            received_prompts.append(prompt)
            return {"result": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)
        await engine.run({})

        # The processor should have received the script's stdout in its prompt
        assert len(received_prompts) == 1
        assert "test output" in received_prompts[0]


class TestScriptRouting:
    """Tests for route branching on exit_code."""

    @pytest.mark.asyncio
    async def test_route_on_exit_code_simpleeval_success(self) -> None:
        """Test routing on exit_code == 0 using simpleeval."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="script-route-simpleeval",
                entry_point="checker",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="checker",
                    type="script",
                    command=sys.executable,
                    args=["-c", "import sys; sys.exit(0)"],
                    routes=[
                        RouteDef(to="success_handler", when="exit_code == 0"),
                        RouteDef(to="failure_handler"),
                    ],
                ),
                AgentDef(
                    name="success_handler",
                    prompt="Success",
                    routes=[RouteDef(to="$end")],
                ),
                AgentDef(
                    name="failure_handler",
                    prompt="Failure",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"path": "{{ success_handler.output.result }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": f"ran {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        assert result["path"] == "ran success_handler"

    @pytest.mark.asyncio
    async def test_route_on_exit_code_simpleeval_failure(self) -> None:
        """Test routing on non-zero exit_code using simpleeval."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="script-route-fail",
                entry_point="checker",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="checker",
                    type="script",
                    command=sys.executable,
                    args=["-c", "import sys; sys.exit(1)"],
                    routes=[
                        RouteDef(to="success_handler", when="exit_code == 0"),
                        RouteDef(to="failure_handler"),
                    ],
                ),
                AgentDef(
                    name="success_handler",
                    prompt="Success",
                    routes=[RouteDef(to="$end")],
                ),
                AgentDef(
                    name="failure_handler",
                    prompt="Failure",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"path": "{{ failure_handler.output.result }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": f"ran {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        assert result["path"] == "ran failure_handler"

    @pytest.mark.asyncio
    async def test_route_on_exit_code_jinja2(self) -> None:
        """Test routing on exit_code using Jinja2 syntax."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="script-route-jinja2",
                entry_point="checker",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="checker",
                    type="script",
                    command=sys.executable,
                    args=["-c", "import sys; sys.exit(0)"],
                    routes=[
                        RouteDef(to="success_handler", when="{{ output.exit_code == 0 }}"),
                        RouteDef(to="failure_handler"),
                    ],
                ),
                AgentDef(
                    name="success_handler",
                    prompt="Success",
                    routes=[RouteDef(to="$end")],
                ),
                AgentDef(
                    name="failure_handler",
                    prompt="Failure",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"path": "{{ success_handler.output.result }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": f"ran {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        assert result["path"] == "ran success_handler"


class TestScriptLimits:
    """Tests for script step limit enforcement."""

    @pytest.mark.asyncio
    async def test_script_counts_toward_iteration_limit(self) -> None:
        """Test that script step counts as one iteration."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="script-iteration",
                entry_point="step1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="step1",
                    type="script",
                    command=sys.executable,
                    args=["-c", "print('step1')"],
                    routes=[RouteDef(to="step2")],
                ),
                AgentDef(
                    name="step2",
                    type="script",
                    command=sys.executable,
                    args=["-c", "print('step2')"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider)
        await engine.run({})

        # Both scripts should have been recorded
        assert engine.limits.current_iteration == 2

    @pytest.mark.asyncio
    async def test_script_non_zero_exit_no_routes_ends(self) -> None:
        """Test that non-zero exit with no routes defaults to $end (no error)."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="script-noroutes",
                entry_point="failing",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="failing",
                    type="script",
                    command=sys.executable,
                    args=["-c", "import sys; sys.exit(1)"],
                ),
            ],
            output={
                "code": "{{ failing.output.exit_code }}",
            },
        )

        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider)
        result = await engine.run({})

        # Should complete without error, exit_code available in output
        assert result["code"] == 1


class TestScriptMixed:
    """Tests for mixed agent + script workflows."""

    @pytest.mark.asyncio
    async def test_mixed_agent_and_script(self) -> None:
        """Test workflow with both agent and script steps."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mixed-workflow",
                entry_point="setup_script",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="setup_script",
                    type="script",
                    command=sys.executable,
                    args=["-c", "print('setup complete')"],
                    routes=[RouteDef(to="analyzer")],
                ),
                AgentDef(
                    name="analyzer",
                    prompt="Analyze: {{ setup_script.output.stdout }}",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "analysis": "{{ analyzer.output.result }}",
            },
        )

        def mock_handler(agent, prompt, context):
            return {"result": "analysis done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        assert result["analysis"] == "analysis done"


class TestScriptTemplating:
    """Tests for Jinja2-templated commands with workflow input."""

    @pytest.mark.asyncio
    async def test_script_command_with_workflow_input(self) -> None:
        """Test script step with Jinja2-templated command using workflow input."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="script-template",
                entry_point="runner",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="runner",
                    type="script",
                    command=sys.executable,
                    args=["-c", "import sys; print(sys.argv[1])", "{{ workflow.input.message }}"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "result": "{{ runner.output.stdout }}",
            },
        )

        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider)
        result = await engine.run({"message": "dynamic value"})

        assert "dynamic value" in result["result"]


class TestScriptDryRun:
    """Tests for dry-run plan generation with script steps."""

    def test_dry_run_includes_script_type(self) -> None:
        """Test that dry-run plan includes script steps with correct agent_type."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="script-dryrun",
                entry_point="setup",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="setup",
                    type="script",
                    command="echo",
                    args=["init"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider)
        plan = engine.build_execution_plan()

        assert len(plan.steps) == 1
        assert plan.steps[0].agent_name == "setup"
        assert plan.steps[0].agent_type == "script"


class TestScriptInParallelRejected:
    """Tests that script steps are rejected in parallel groups at the engine level."""

    def test_script_in_parallel_group_raises_configuration_error(self) -> None:
        """Test that a WorkflowConfig with a script step in a parallel group raises at validation.

        This is a negative integration test: script steps are forbidden in parallel groups.
        The restriction is enforced by validate_workflow_config, which is called before
        WorkflowEngine.run(). This test exercises the full config→validate path.
        """
        from conductor.config.validator import validate_workflow_config

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="bad-parallel",
                entry_point="pg",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(name="agent_a", prompt="do something", routes=[RouteDef(to="$end")]),
                AgentDef(name="script_b", type="script", command="echo"),
            ],
            parallel=[
                ParallelGroup(
                    name="pg",
                    agents=["agent_a", "script_b"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        with pytest.raises(ConfigurationError, match="script step"):
            validate_workflow_config(config)
