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
from pathlib import Path
from unittest.mock import MagicMock

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
from conductor.engine.checkpoint import CheckpointManager
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import (
    ConfigurationError,
    ExecutionError,
    UnhandledNodeError,
    ValidationError,
)
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

    @pytest.mark.asyncio
    async def test_typed_script_failure_routes_with_error_context(self) -> None:
        """A typed failure selects error routes and is visible to its handler."""
        script = (
            "import json, os; "
            "open(os.environ['CONDUCTOR_ERROR_OUT'], 'w', encoding='utf-8').write("
            "json.dumps({'kind': 'external.git.drift', "
            "'message': 'remote changed', 'details': {'branch': 'main'}})); "
            "print('partial')"
        )
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="typed-script-route",
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
                    args=["-c", script],
                    output={"result": OutputField(type="string")},
                    routes=[
                        RouteDef(to="success_handler"),
                        RouteDef(to="recovery", on_error="external.git.drift"),
                    ],
                ),
                AgentDef(
                    name="success_handler",
                    prompt="Unexpected success",
                    routes=[RouteDef(to="$end")],
                ),
                AgentDef(
                    name="recovery",
                    prompt=(
                        "Recover {{ checker.error.kind }}: "
                        "{{ checker.error.message }} / {{ checker.output.stdout }}"
                    ),
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"path": "{{ recovery.output.result }}"},
        )
        received_prompts: list[str] = []

        def mock_handler(agent, prompt, context):
            received_prompts.append(prompt)
            return {"result": f"ran {agent.name}"}

        engine = WorkflowEngine(config, CopilotProvider(mock_handler=mock_handler))

        result = await engine.run({})

        assert result["path"] == "ran recovery"
        assert [prompt.strip() for prompt in received_prompts] == [
            "Recover external.git.drift: remote changed / partial"
        ]

    @pytest.mark.asyncio
    async def test_unhandled_typed_failure_checkpoints_before_state_commit(
        self, tmp_path: Path
    ) -> None:
        """An unhandled failure checkpoints the failing step for replay."""
        script = (
            "import json, os; "
            "open(os.environ['CONDUCTOR_ERROR_OUT'], 'w', encoding='utf-8').write("
            "json.dumps({'kind': 'external.git.drift', "
            "'message': 'remote changed', 'details': {}}))"
        )
        workflow_path = tmp_path / "workflow.yaml"
        workflow_path.write_text("workflow: {}", encoding="utf-8")
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="typed-script-unhandled",
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
                    args=["-c", script],
                )
            ],
        )
        provider = MagicMock()
        provider.get_session_ids.return_value = {}
        engine = WorkflowEngine(config, provider, workflow_path=workflow_path)

        with pytest.raises(UnhandledNodeError) as raised:
            await engine.run({})

        assert raised.value.error.kind == "external.git.drift"
        assert "checker" not in engine.context.agent_outputs
        assert engine.limits.current_iteration == 0
        assert engine._last_checkpoint_path is not None

    @pytest.mark.asyncio
    async def test_handler_failure_checkpoint_includes_routed_error_context(
        self, tmp_path: Path
    ) -> None:
        """A later handler failure checkpoints the handler and prior envelope."""
        script = (
            "import json, os; "
            "open(os.environ['CONDUCTOR_ERROR_OUT'], 'w', encoding='utf-8').write("
            "json.dumps({'kind': 'external.git.drift', "
            "'message': 'remote changed', 'details': {'branch': 'main'}})); "
            "print('partial')"
        )
        workflow_path = tmp_path / "workflow.yaml"
        workflow_path.write_text("workflow: {}", encoding="utf-8")
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="typed-script-handler-failure",
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
                    args=["-c", script],
                    routes=[
                        RouteDef(to="$end"),
                        RouteDef(to="recovery", on_error="external.git.drift"),
                    ],
                ),
                AgentDef(
                    name="recovery",
                    type="script",
                    command="definitely-not-a-real-conductor-command",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        provider = MagicMock()
        provider.get_session_ids.return_value = {}
        engine = WorkflowEngine(config, provider, workflow_path=workflow_path)

        with pytest.raises(ExecutionError, match="command not found"):
            await engine.run({})

        assert engine._last_checkpoint_path is not None
        checkpoint = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
        assert checkpoint.current_agent == "recovery"
        assert checkpoint.context["agent_outputs"]["checker"]["stdout"].strip() == "partial"
        assert checkpoint.context["step_errors"]["checker"] == {
            "kind": "external.git.drift",
            "message": "remote changed",
            "details": {"branch": "main"},
        }

    @pytest.mark.asyncio
    async def test_catch_all_preserves_kind_not_declared_in_raises(self) -> None:
        """Raises is documentation-only; catch-all routes retain the runtime kind."""
        script = (
            "import json, os; "
            "open(os.environ['CONDUCTOR_ERROR_OUT'], 'w', encoding='utf-8').write("
            "json.dumps({'kind': 'external.git.unexpected', "
            "'message': 'unexpected', 'details': {}}))"
        )
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="typed-script-catch-all",
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
                    args=["-c", script],
                    raises=["external.git.drift"],
                    routes=[
                        RouteDef(to="$end"),
                        RouteDef(to="recovery", on_error=True),
                    ],
                ),
                AgentDef(
                    name="recovery",
                    prompt="{{ checker.error.kind }}",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"kind": "{{ recovery.output.result }}"},
        )
        engine = WorkflowEngine(
            config,
            CopilotProvider(mock_handler=lambda agent, prompt, context: {"result": prompt}),
        )

        result = await engine.run({})

        assert result["kind"] == "external.git.unexpected"


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


class TestScriptJsonStdout:
    """Tests for auto-parsing script stdout as JSON into output fields.

    See PR #122. When a script's stdout is a valid JSON object, parsed fields
    are merged into output_content alongside stdout/stderr/exit_code so they
    are accessible as `output.field_name` in templates and route conditions
    (matching LLM structured-output behavior).
    """

    @staticmethod
    def _single_script_config(args: list[str]) -> WorkflowConfig:
        """Build a minimal single-script workflow config."""
        return WorkflowConfig(
            workflow=WorkflowDef(
                name="json-script",
                entry_point="detector",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="detector",
                    type="script",
                    command=sys.executable,
                    args=args,
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

    @pytest.mark.asyncio
    async def test_json_object_parsed_with_field_routing(self) -> None:
        """Happy path: JSON object stdout is parsed and fields drive routing."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="json-script",
                entry_point="detector",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="detector",
                    type="script",
                    command=sys.executable,
                    args=[
                        "-c",
                        "import json;"
                        ' print(json.dumps({"plan_exists": True, "route": "planning"}))',
                    ],
                    routes=[
                        RouteDef(to="planner", when="route == 'planning'"),
                        RouteDef(to="$end"),
                    ],
                ),
                AgentDef(
                    name="planner",
                    type="script",
                    command=sys.executable,
                    args=["-c", "print('done')"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        engine = WorkflowEngine(config, MagicMock())
        await engine.run({})

        det = engine.context.agent_outputs["detector"]
        assert det["plan_exists"] is True
        assert det["route"] == "planning"
        # Backward compat: built-in fields still present
        assert "stdout" in det
        assert det["exit_code"] == 0
        # Routing reached planner via parsed field
        assert "planner" in engine.context.agent_outputs

    @pytest.mark.asyncio
    async def test_non_json_stdout_preserved_no_extra_fields(self) -> None:
        """Non-JSON stdout: output.stdout preserved, no extra fields, no exception."""
        config = self._single_script_config(args=["-c", "print('hello world')"])
        engine = WorkflowEngine(config, MagicMock())
        await engine.run({})

        out = engine.context.agent_outputs["detector"]
        assert "hello world" in out["stdout"]
        assert set(out.keys()) == {"stdout", "stderr", "exit_code"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stdout_payload",
        ["[1, 2, 3]", "42", '"a string"', "true"],
        ids=["array", "int", "string", "bool"],
    )
    async def test_json_non_object_ignored(self, stdout_payload: str) -> None:
        """JSON arrays/scalars are not merged (only dict objects are)."""
        config = self._single_script_config(args=["-c", f"print({stdout_payload!r})"])
        engine = WorkflowEngine(config, MagicMock())
        await engine.run({})

        out = engine.context.agent_outputs["detector"]
        assert set(out.keys()) == {"stdout", "stderr", "exit_code"}

    @pytest.mark.asyncio
    async def test_json_field_shadows_builtin(self) -> None:
        """Parsed JSON value wins over built-in field of the same name (PR #122 contract)."""
        config = self._single_script_config(
            args=["-c", 'import json; print(json.dumps({"exit_code": "ok"}))'],
        )
        engine = WorkflowEngine(config, MagicMock())
        await engine.run({})

        out = engine.context.agent_outputs["detector"]
        assert out["exit_code"] == "ok"

    @pytest.mark.asyncio
    async def test_empty_stdout_no_crash(self) -> None:
        """Empty stdout: doesn't crash, no extra fields merged."""
        config = self._single_script_config(args=["-c", "pass"])
        engine = WorkflowEngine(config, MagicMock())
        await engine.run({})

        out = engine.context.agent_outputs["detector"]
        assert set(out.keys()) == {"stdout", "stderr", "exit_code"}
        assert out["stdout"] == ""


class TestScriptOutputSchema:
    """Tests for declared `output:` schemas on script agents (issue #118).

    When a script declares an output schema, the engine validates JSON stdout
    against it before emitting `script_completed`. Validation failures raise
    ValidationError and emit `script_failed` instead.
    """

    @staticmethod
    def _config_with_schema(
        args: list[str],
        output: dict[str, OutputField],
    ) -> WorkflowConfig:
        """Build a single-script workflow config with the given args + schema."""
        return WorkflowConfig(
            workflow=WorkflowDef(
                name="schema-script",
                entry_point="detector",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="detector",
                    type="script",
                    command=sys.executable,
                    args=args,
                    output=output,
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

    @staticmethod
    def _make_collector() -> tuple[WorkflowEventEmitter, list[WorkflowEvent]]:
        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)
        return emitter, events

    @staticmethod
    async def _run_expect_validation_error(config: WorkflowConfig) -> ValidationError:
        """Run the workflow and assert it raises ValidationError; return the error."""
        engine = WorkflowEngine(config, MagicMock())
        with pytest.raises(ValidationError) as exc_info:
            await engine.run({})
        return exc_info.value

    @pytest.mark.asyncio
    async def test_valid_json_matches_schema(self) -> None:
        """Happy path: stdout JSON matches declared schema → fields available."""
        config = self._config_with_schema(
            args=[
                "-c",
                'import json; print(json.dumps({"route": "planning", "count": 3}))',
            ],
            output={
                "route": OutputField(type="string"),
                "count": OutputField(type="number"),
            },
        )
        engine = WorkflowEngine(config, MagicMock())
        await engine.run({})

        out = engine.context.agent_outputs["detector"]
        assert out["route"] == "planning"
        assert out["count"] == 3
        # Built-ins still present alongside declared fields.
        assert out["exit_code"] == 0
        assert "stdout" in out
        assert "stderr" in out

    @pytest.mark.asyncio
    async def test_non_json_stdout_raises_validation_error(self) -> None:
        """Schema declared + non-JSON stdout → ValidationError surfaces parser detail."""
        config = self._config_with_schema(
            args=["-c", "print('not json')"],
            output={"route": OutputField(type="string")},
        )

        err = await self._run_expect_validation_error(config)

        msg = str(err)
        assert "detector" in msg
        assert "not valid JSON" in msg
        # The underlying json.JSONDecodeError text is surfaced so users can
        # see WHY the parse failed (e.g. "Expecting value: line 1 column 1").
        assert "line 1" in msg or "Expecting" in msg
        # The suggestion explicitly mentions writing logs to stderr.
        assert err.suggestion is not None
        assert "stderr" in err.suggestion.lower()

    @pytest.mark.asyncio
    async def test_malformed_json_surfaces_parser_detail(self) -> None:
        """Truncated/malformed JSON: parser error column/position appears in message."""
        # Truncated object missing the closing brace.
        config = self._config_with_schema(
            args=["-c", 'print(\'{"route": "plan\')'],
            output={"route": OutputField(type="string")},
        )

        err = await self._run_expect_validation_error(config)

        msg = str(err)
        # Underlying JSONDecodeError text describes the unterminated string.
        assert "Unterminated" in msg or "delimiter" in msg or "line 1" in msg

    @pytest.mark.asyncio
    async def test_empty_stdout_raises_validation_error(self) -> None:
        """Empty stdout + schema → ValidationError with stderr-for-logs suggestion."""
        config = self._config_with_schema(
            args=["-c", "pass"],
            output={"route": OutputField(type="string")},
        )

        err = await self._run_expect_validation_error(config)

        assert "detector" in str(err)
        assert err.suggestion is not None
        assert "stderr" in err.suggestion.lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stdout_payload",
        ["[1, 2, 3]", "42", '"a string"', "true"],
        ids=["array", "int", "string", "bool"],
    )
    async def test_json_non_object_raises_validation_error(self, stdout_payload: str) -> None:
        """JSON arrays/scalars at top level + schema → ValidationError."""
        config = self._config_with_schema(
            args=["-c", f"print({stdout_payload!r})"],
            output={"route": OutputField(type="string")},
        )

        err = await self._run_expect_validation_error(config)

        assert "object" in str(err).lower()

    @pytest.mark.asyncio
    async def test_missing_required_field_raises(self) -> None:
        """JSON missing a declared field → ValidationError mentions script + field."""
        config = self._config_with_schema(
            args=["-c", 'import json; print(json.dumps({"route": "planning"}))'],
            output={
                "route": OutputField(type="string"),
                "count": OutputField(type="number"),
            },
        )

        err = await self._run_expect_validation_error(config)

        msg = str(err)
        # Wrapping in workflow.py adds the script name and stdout-JSON suggestion
        # on top of the generic validate_output() message.
        assert "detector" in msg
        assert "count" in msg
        assert err.suggestion is not None
        assert "stderr" in err.suggestion.lower()

    @pytest.mark.asyncio
    async def test_wrong_field_type_raises(self) -> None:
        """JSON field has wrong type → ValidationError mentions script + field."""
        config = self._config_with_schema(
            args=[
                "-c",
                'import json; print(json.dumps({"route": 42, "count": 3}))',
            ],
            output={
                "route": OutputField(type="string"),
                "count": OutputField(type="number"),
            },
        )

        err = await self._run_expect_validation_error(config)

        msg = str(err)
        assert "detector" in msg
        assert "route" in msg

    @pytest.mark.asyncio
    async def test_extra_fields_allowed(self) -> None:
        """Extra JSON fields beyond schema are kept (parity with LLM agent output handling)."""
        config = self._config_with_schema(
            args=[
                "-c",
                'import json; print(json.dumps({"route": "planning", "extra": "ok"}))',
            ],
            output={"route": OutputField(type="string")},
        )
        engine = WorkflowEngine(config, MagicMock())
        await engine.run({})

        out = engine.context.agent_outputs["detector"]
        assert out["route"] == "planning"
        assert out["extra"] == "ok"

    @pytest.mark.asyncio
    async def test_empty_schema_requires_json_object(self) -> None:
        """`output: {}` opts into strict mode: any JSON object passes, non-JSON fails."""
        # Empty schema + JSON object → passes.
        config_ok = self._config_with_schema(
            args=["-c", 'import json; print(json.dumps({"anything": 1}))'],
            output={},
        )
        engine = WorkflowEngine(config_ok, MagicMock())
        await engine.run({})
        assert engine.context.agent_outputs["detector"]["anything"] == 1

        # Empty schema + non-JSON → ValidationError.
        config_bad = self._config_with_schema(
            args=["-c", "print('hi')"],
            output={},
        )
        engine = WorkflowEngine(config_bad, MagicMock())
        with pytest.raises(ValidationError):
            await engine.run({})

    @pytest.mark.asyncio
    async def test_builtin_fields_preserved_when_not_shadowed(self) -> None:
        """Declared schema with non-builtin fields still exposes stdout/stderr/exit_code."""
        config = self._config_with_schema(
            args=[
                "-c",
                "import sys, json;"
                ' sys.stderr.write("warning"); '
                'print(json.dumps({"route": "planning"}))',
            ],
            output={"route": OutputField(type="string")},
        )
        engine = WorkflowEngine(config, MagicMock())
        await engine.run({})

        out = engine.context.agent_outputs["detector"]
        assert out["route"] == "planning"
        assert "warning" in out["stderr"]
        assert out["exit_code"] == 0
        assert isinstance(out["stdout"], str)

    @pytest.mark.asyncio
    async def test_schema_validates_shadowed_builtin(self) -> None:
        """Validation runs on the merged dict, so shadowed built-ins are validated."""
        # Script emits {"exit_code": "ok"} which shadows the built-in int.
        # Schema says exit_code should be a string → passes (validates merged value).
        config = self._config_with_schema(
            args=["-c", 'import json; print(json.dumps({"exit_code": "ok"}))'],
            output={"exit_code": OutputField(type="string")},
        )
        engine = WorkflowEngine(config, MagicMock())
        await engine.run({})
        assert engine.context.agent_outputs["detector"]["exit_code"] == "ok"

        # Conversely: if schema requires exit_code as number but script shadows
        # it with a string → ValidationError on the merged value.
        config_bad = self._config_with_schema(
            args=["-c", 'import json; print(json.dumps({"exit_code": "ok"}))'],
            output={"exit_code": OutputField(type="number")},
        )
        engine = WorkflowEngine(config_bad, MagicMock())
        with pytest.raises(ValidationError, match="exit_code"):
            await engine.run({})

    @pytest.mark.asyncio
    async def test_validation_failure_emits_script_failed_not_completed(self) -> None:
        """On validation failure: script_failed emitted, script_completed is NOT."""
        emitter, events = self._make_collector()
        config = self._config_with_schema(
            args=["-c", "print('not json')"],
            output={"route": OutputField(type="string")},
        )
        engine = WorkflowEngine(config, MagicMock(), event_emitter=emitter)

        with pytest.raises(ValidationError):
            await engine.run({})

        event_types = [e.type for e in events]
        assert "script_started" in event_types
        assert "script_failed" in event_types
        assert "script_completed" not in event_types

        # script_failed should carry stdout/stderr/exit_code so the dashboard
        # can show what the script actually wrote.
        failed = next(e for e in events if e.type == "script_failed")
        assert failed.data["agent_name"] == "detector"
        assert failed.data["error_type"] == "ValidationError"
        assert "stdout" in failed.data
        assert "stderr" in failed.data
        assert "exit_code" in failed.data

    @pytest.mark.asyncio
    async def test_validation_failure_does_not_store_context(self) -> None:
        """On validation failure: agent output is NOT stored in context."""
        config = self._config_with_schema(
            args=["-c", "print('not json')"],
            output={"route": OutputField(type="string")},
        )
        engine = WorkflowEngine(config, MagicMock())

        with pytest.raises(ValidationError):
            await engine.run({})

        # detector should not appear in stored outputs.
        assert "detector" not in engine.context.agent_outputs

    @pytest.mark.asyncio
    async def test_schema_field_drives_route(self) -> None:
        """End-to-end: declared field drives routing to a downstream agent."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="schema-route",
                entry_point="detector",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="detector",
                    type="script",
                    command=sys.executable,
                    args=[
                        "-c",
                        'import json; print(json.dumps({"phase": "planning"}))',
                    ],
                    output={"phase": OutputField(type="string")},
                    routes=[
                        RouteDef(to="planner", when="phase == 'planning'"),
                        RouteDef(to="$end"),
                    ],
                ),
                AgentDef(
                    name="planner",
                    type="script",
                    command=sys.executable,
                    args=["-c", "print('planning done')"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        engine = WorkflowEngine(config, MagicMock())
        await engine.run({})

        assert engine.context.agent_outputs["detector"]["phase"] == "planning"
        assert "planner" in engine.context.agent_outputs


class TestScriptStdinWorkflow:
    """End-to-end stdin payload transport through the engine (issue #18)."""

    @pytest.mark.asyncio
    async def test_structured_payload_piped_via_stdin(self) -> None:
        """A structured upstream payload is handed to a script as JSON over stdin.

        The script parses it, transforms it, and the auto-parsed result flows to
        the workflow output — exercising the full render → pipe → parse round trip.
        """
        reader = (
            "import sys, json; "
            "d = json.load(sys.stdin); "
            "print(json.dumps({'received_name': d['name'], 'doubled': d['count'] * 2}))"
        )
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="script-stdin",
                entry_point="consume",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="consume",
                    type="script",
                    command=sys.executable,
                    args=["-c", reader],
                    stdin="{{ workflow.input.payload | tojson }}",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "name": "{{ consume.output.received_name }}",
                "doubled": "{{ consume.output.doubled }}",
            },
        )
        engine = WorkflowEngine(config, MagicMock())
        result = await engine.run({"payload": {"name": "widget", "count": 21}})

        assert result["name"] == "widget"
        assert str(result["doubled"]) == "42"

    @pytest.mark.asyncio
    async def test_large_payload_via_stdin_reports_byte_count(self) -> None:
        """A payload far larger than ARG_MAX flows through stdin intact.

        Also asserts the ``script_completed`` event carries ``stdin_bytes`` so the
        dashboard / JSONL log can surface payload size without the payload itself.
        """
        payload = "z" * (1024 * 1024)  # 1 MB — well beyond any OS arg-length limit
        reader = "import sys; print(len(sys.stdin.read()))"

        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="script-stdin-large",
                entry_point="sizer",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="sizer",
                    type="script",
                    command=sys.executable,
                    args=["-c", reader],
                    stdin="{{ workflow.input.blob }}",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"size": "{{ sizer.output.stdout | trim }}"},
        )
        engine = WorkflowEngine(config, MagicMock(), event_emitter=emitter)
        result = await engine.run({"blob": payload})

        # The output template auto-parses the numeric stdout to an int; compare
        # type-robustly. The point is the full 1 MB survived the stdin round trip.
        assert str(result["size"]) == str(len(payload))
        completed = [e for e in events if e.type == "script_completed"]
        assert len(completed) == 1
        assert completed[0].data["stdin_bytes"] == len(payload)
