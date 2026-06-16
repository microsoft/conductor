"""Integration tests for WorkflowEngine resume functionality.

Tests cover:
- Checkpoint save on ConductorError
- Checkpoint save on generic Exception
- Checkpoint save on KeyboardInterrupt
- Resume continues from the correct agent with full prior context
- Full round-trip: run → fail → checkpoint → resume → success
- Checkpoint cleanup after successful resume
- _current_agent_name tracking during execution
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    LimitsConfig,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.checkpoint import CheckpointManager
from conductor.engine.context import WorkflowContext
from conductor.engine.limits import LimitEnforcer
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ProviderError
from conductor.providers.copilot import CopilotProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_workflow(tmp_path: Path, content: str = "name: test-workflow\n") -> Path:
    """Write a dummy workflow YAML and return its path."""
    wf = tmp_path / "workflow.yaml"
    wf.write_text(content)
    return wf


def _multi_agent_config() -> WorkflowConfig:
    """Create a multi-agent config: planner → researcher → synthesizer."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="multi-agent",
            entry_point="planner",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            AgentDef(
                name="planner",
                model="gpt-4",
                prompt="Plan: {{ workflow.input.topic }}",
                output={"plan": OutputField(type="string")},
                routes=[RouteDef(to="researcher")],
            ),
            AgentDef(
                name="researcher",
                model="gpt-4",
                prompt="Research: {{ planner.output.plan }}",
                output={"findings": OutputField(type="string")},
                routes=[RouteDef(to="synthesizer")],
            ),
            AgentDef(
                name="synthesizer",
                model="gpt-4",
                prompt="Synthesize: {{ researcher.output.findings }}",
                output={"summary": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={
            "summary": "{{ synthesizer.output.summary }}",
        },
    )


# ---------------------------------------------------------------------------
# Checkpoint save on failure tests
# ---------------------------------------------------------------------------


class TestCheckpointSaveOnFailure:
    """Verify checkpoint is saved when execution fails."""

    @pytest.mark.asyncio
    async def test_checkpoint_saved_on_provider_error(self, tmp_path: Path) -> None:
        """Checkpoint is saved when a ConductorError occurs."""
        wf_path = _write_workflow(tmp_path)
        config = _multi_agent_config()

        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            if agent.name == "researcher":
                raise ProviderError("Network error")
            return {"plan": "research AI"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=wf_path)

        with (
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            pytest.raises(ProviderError, match="Network error"),
        ):
            await engine.run({"topic": "AI"})

        # Checkpoint should have been saved
        assert engine._last_checkpoint_path is not None
        assert engine._last_checkpoint_path.exists()

        # Verify checkpoint content
        cp = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
        assert cp.current_agent == "researcher"
        assert cp.failure["error_type"] == "ProviderError"
        assert cp.context["agent_outputs"]["planner"]["plan"] == "research AI"

    @pytest.mark.asyncio
    async def test_checkpoint_saved_on_generic_exception(self, tmp_path: Path) -> None:
        """Checkpoint is saved when an agent raises and the provider wraps it."""
        wf_path = _write_workflow(tmp_path)
        config = _multi_agent_config()

        def mock_handler(agent, prompt, context):
            if agent.name == "synthesizer":
                raise RuntimeError("Unexpected error")
            if agent.name == "planner":
                return {"plan": "step1"}
            return {"findings": "data"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=wf_path)

        with (
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            pytest.raises(ProviderError),
        ):
            await engine.run({"topic": "AI"})

        assert engine._last_checkpoint_path is not None
        cp = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
        assert cp.current_agent == "synthesizer"
        assert "planner" in cp.context["agent_outputs"]
        assert "researcher" in cp.context["agent_outputs"]

    @pytest.mark.asyncio
    async def test_checkpoint_saved_on_keyboard_interrupt(self, tmp_path: Path) -> None:
        """Checkpoint is saved when user presses Ctrl+C."""
        wf_path = _write_workflow(tmp_path)
        config = _multi_agent_config()

        def mock_handler(agent, prompt, context):
            if agent.name == "researcher":
                raise KeyboardInterrupt()
            return {"plan": "the plan"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=wf_path)

        with (
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            pytest.raises(KeyboardInterrupt),
        ):
            await engine.run({"topic": "AI"})

        assert engine._last_checkpoint_path is not None
        cp = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
        assert cp.current_agent == "researcher"
        assert cp.context["agent_outputs"]["planner"]["plan"] == "the plan"

    @pytest.mark.asyncio
    async def test_no_checkpoint_without_workflow_path(self, tmp_path: Path) -> None:
        """No checkpoint is saved when workflow_path is not set."""
        config = _multi_agent_config()

        def mock_handler(agent, prompt, context):
            if agent.name == "researcher":
                raise ProviderError("fail")
            return {"plan": "p"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)  # No workflow_path

        with pytest.raises(ProviderError):
            await engine.run({"topic": "AI"})

        assert engine._last_checkpoint_path is None

    @pytest.mark.asyncio
    async def test_current_agent_name_tracked(self, tmp_path: Path) -> None:
        """_current_agent_name is updated at each loop iteration."""
        config = _multi_agent_config()
        tracked_agents: list[str | None] = []

        original_find_agent = WorkflowEngine._find_agent

        def tracking_find_agent(self_inner, name):
            tracked_agents.append(self_inner._current_agent_name)
            return original_find_agent(self_inner, name)

        def mock_handler(agent, prompt, context):
            if agent.name == "planner":
                return {"plan": "p"}
            if agent.name == "researcher":
                return {"findings": "f"}
            return {"summary": "s"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        with patch.object(WorkflowEngine, "_find_agent", tracking_find_agent):
            await engine.run({"topic": "AI"})

        # Each iteration should have set _current_agent_name before _find_agent
        assert "planner" in tracked_agents
        assert "researcher" in tracked_agents
        assert "synthesizer" in tracked_agents


# ---------------------------------------------------------------------------
# handle_dashboard_stop tests (issue #245)
# ---------------------------------------------------------------------------


class TestHandleDashboardStop:
    """Verify a dashboard-cancelled run still gets a checkpoint + terminal event.

    ``handle_dashboard_stop`` is invoked by the CLI wrapper after a Stop/Kill
    cancels the engine task mid-agent — a path that bypasses the engine's own
    ``workflow_failed`` / checkpoint handling. See issue #245.
    """

    @staticmethod
    def _engine_with_emitter(config, provider, *, workflow_path=None):
        from conductor.events import WorkflowEvent, WorkflowEventEmitter

        emitter = WorkflowEventEmitter()
        events: list[WorkflowEvent] = []
        emitter.subscribe(events.append)
        engine = WorkflowEngine(
            config, provider, workflow_path=workflow_path, event_emitter=emitter
        )
        return engine, events

    @pytest.mark.asyncio
    async def test_saves_checkpoint_and_emits_stopped_failed(self, tmp_path: Path) -> None:
        """A cancelled run gets a best-effort checkpoint + stopped_by_user event."""
        wf_path = _write_workflow(tmp_path)
        config = _multi_agent_config()
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"plan": "p"})
        engine, events = self._engine_with_emitter(config, provider, workflow_path=wf_path)

        # Simulate a run that was cancelled while 'researcher' was in flight,
        # with the planner output already committed to context.
        engine.context.set_workflow_inputs({"topic": "AI"})
        engine.context.store("planner", {"plan": "research AI"})
        engine._current_agent_name = "researcher"

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            path = engine.handle_dashboard_stop("Workflow stopped by user via dashboard")

        assert path is not None
        assert path.exists()
        assert engine._last_checkpoint_path == path

        failed = [e for e in events if e.type == "workflow_failed"]
        assert len(failed) == 1
        assert failed[0].data["stopped_by_user"] is True
        assert failed[0].data["agent_name"] == "researcher"
        assert failed[0].data["error_type"] == "ExecutionError"
        assert failed[0].data["checkpoint_path"] == str(path)

        assert any(e.type == "checkpoint_saved" for e in events)

        cp = CheckpointManager.load_checkpoint(path)
        assert cp.current_agent == "researcher"
        assert cp.context["agent_outputs"]["planner"]["plan"] == "research AI"

    @pytest.mark.asyncio
    async def test_idempotent_when_checkpoint_already_saved(self, tmp_path: Path) -> None:
        """A repeat/direct call once a checkpoint is already recorded is a no-op:
        no duplicate events, same path returned. (In production the CLI wrapper
        only calls this for genuinely cancelled tasks, so this guard is a
        defensive backstop rather than the InterruptError path.)"""
        wf_path = _write_workflow(tmp_path)
        config = _multi_agent_config()
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"plan": "p"})
        engine, events = self._engine_with_emitter(config, provider, workflow_path=wf_path)
        engine.context.set_workflow_inputs({"topic": "AI"})
        engine._current_agent_name = "researcher"

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            first = engine.handle_dashboard_stop("Workflow stopped by user via dashboard")
            second = engine.handle_dashboard_stop("Workflow stopped by user via dashboard")

        assert first is not None
        assert second == first
        # Only one terminal event despite two calls.
        assert len([e for e in events if e.type == "workflow_failed"]) == 1
        assert len([e for e in events if e.type == "checkpoint_saved"]) == 1

    @pytest.mark.asyncio
    async def test_surfaces_absence_without_workflow_path(self) -> None:
        """When no checkpoint can be written, the failure event explains why."""
        config = _multi_agent_config()
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"plan": "p"})
        engine, events = self._engine_with_emitter(config, provider)  # no workflow_path
        engine._current_agent_name = "planner"

        path = engine.handle_dashboard_stop("Workflow stopped by user via dashboard")

        assert path is None
        assert engine._last_checkpoint_path is None
        failed = [e for e in events if e.type == "workflow_failed"]
        assert len(failed) == 1
        assert failed[0].data["stopped_by_user"] is True
        assert "checkpoint_path" not in failed[0].data
        assert (
            failed[0].data["checkpoint_unavailable_reason"]
            == "no workflow file is associated with this run"
        )
        assert not any(e.type == "checkpoint_saved" for e in events)

    @pytest.mark.asyncio
    async def test_surfaces_absence_when_checkpoint_write_fails(self, tmp_path: Path) -> None:
        """workflow_path IS set but the checkpoint write fails (save returns
        None): the failure event explains *that* distinct reason (issue #245
        Expected #2)."""
        wf_path = _write_workflow(tmp_path)
        config = _multi_agent_config()
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"plan": "p"})
        engine, events = self._engine_with_emitter(config, provider, workflow_path=wf_path)
        engine._current_agent_name = "researcher"

        # CheckpointManager.save_checkpoint never raises — it returns None when
        # the write fails (e.g. disk full / permissions). Simulate that.
        with patch.object(CheckpointManager, "save_checkpoint", return_value=None):
            path = engine.handle_dashboard_stop("Workflow stopped by user via dashboard")

        assert path is None
        assert engine._last_checkpoint_path is None
        failed = [e for e in events if e.type == "workflow_failed"]
        assert len(failed) == 1
        assert failed[0].data["stopped_by_user"] is True
        assert "checkpoint_path" not in failed[0].data
        assert (
            failed[0].data["checkpoint_unavailable_reason"] == "the checkpoint could not be written"
        )
        assert not any(e.type == "checkpoint_saved" for e in events)

    @pytest.mark.asyncio
    async def test_runs_on_error_hook(self, tmp_path: Path) -> None:
        """The on_error lifecycle hook runs on a dashboard stop (parity with the
        in-loop ConductorError path)."""
        wf_path = _write_workflow(tmp_path)
        config = _multi_agent_config()
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"plan": "p"})
        engine, _events = self._engine_with_emitter(config, provider, workflow_path=wf_path)
        engine._current_agent_name = "researcher"

        with (
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            patch.object(engine, "_execute_hook") as mock_hook,
        ):
            engine.handle_dashboard_stop("Workflow stopped by user via dashboard")

        assert mock_hook.call_args is not None
        assert mock_hook.call_args.args[0] == "on_error"


# ---------------------------------------------------------------------------
# Resume tests
# ---------------------------------------------------------------------------


class TestResume:
    """Verify resume continues from the checkpoint agent."""

    @pytest.mark.asyncio
    async def test_resume_from_checkpoint(self) -> None:
        """Resume executes from the specified agent with restored context."""
        config = _multi_agent_config()

        def mock_handler(agent, prompt, context):
            if agent.name == "researcher":
                return {"findings": "resumed findings"}
            if agent.name == "synthesizer":
                return {"summary": "resumed summary"}
            raise AssertionError(f"Unexpected agent: {agent.name}")

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        # Restore context as if planner already ran
        restored_ctx = WorkflowContext()
        restored_ctx.set_workflow_inputs({"topic": "AI"})
        restored_ctx.store("planner", {"plan": "step1, step2"})

        restored_limits = LimitEnforcer.from_dict(
            {"current_iteration": 1, "max_iterations": 10, "execution_history": ["planner"]},
            timeout_seconds=300,
        )

        engine.set_context(restored_ctx)
        engine.set_limits(restored_limits)

        result = await engine.resume("researcher")

        assert result["summary"] == "resumed summary"
        # Context should have all three agents
        assert "planner" in engine.context.agent_outputs
        assert "researcher" in engine.context.agent_outputs
        assert "synthesizer" in engine.context.agent_outputs

    @pytest.mark.asyncio
    async def test_resume_preserves_iteration_count(self) -> None:
        """Resume doesn't reset iteration count."""
        config = _multi_agent_config()

        def mock_handler(agent, prompt, context):
            if agent.name == "researcher":
                return {"findings": "f"}
            return {"summary": "s"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        restored_ctx = WorkflowContext()
        restored_ctx.set_workflow_inputs({"topic": "AI"})
        restored_ctx.store("planner", {"plan": "p"})

        restored_limits = LimitEnforcer.from_dict(
            {"current_iteration": 1, "max_iterations": 10, "execution_history": ["planner"]},
        )

        engine.set_context(restored_ctx)
        engine.set_limits(restored_limits)

        await engine.resume("researcher")

        # Should have incremented from 1 (restored) + 2 (researcher + synthesizer) = 3
        assert engine.limits.current_iteration == 3
        assert engine.limits.execution_history == ["planner", "researcher", "synthesizer"]


# ---------------------------------------------------------------------------
# Full round-trip tests
# ---------------------------------------------------------------------------


class TestFullRoundTrip:
    """Test the complete flow: run → fail → checkpoint → resume → success."""

    @pytest.mark.asyncio
    async def test_round_trip_checkpoint_and_resume(self, tmp_path: Path) -> None:
        """Full round-trip: run fails, checkpoint saved, resume succeeds."""
        wf_path = _write_workflow(tmp_path, "name: multi-agent\n")
        config = _multi_agent_config()

        # First run: planner succeeds, researcher fails
        fail_count = {"researcher": 0}

        def failing_handler(agent, prompt, context):
            if agent.name == "planner":
                return {"plan": "research AI topics"}
            if agent.name == "researcher":
                fail_count["researcher"] += 1
                if fail_count["researcher"] <= 1:
                    raise ProviderError("Temporary network error")
                return {"findings": "comprehensive findings"}
            return {"summary": "final summary of AI"}

        provider = CopilotProvider(mock_handler=failing_handler)
        engine = WorkflowEngine(config, provider, workflow_path=wf_path)

        with (
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            pytest.raises(ProviderError, match="Temporary network"),
        ):
            await engine.run({"topic": "AI"})

        checkpoint_path = engine._last_checkpoint_path
        assert checkpoint_path is not None

        # Load checkpoint and resume
        cp = CheckpointManager.load_checkpoint(checkpoint_path)
        assert cp.current_agent == "researcher"

        # Create a new engine and restore state
        engine2 = WorkflowEngine(config, provider, workflow_path=wf_path)
        engine2.set_context(WorkflowContext.from_dict(cp.context))
        engine2.set_limits(
            LimitEnforcer.from_dict(
                cp.limits,
                timeout_seconds=config.workflow.limits.timeout_seconds,
            )
        )

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            result = await engine2.resume(cp.current_agent)

        assert result["summary"] == "final summary of AI"

        # Cleanup
        CheckpointManager.cleanup(checkpoint_path)
        assert not checkpoint_path.exists()

    @pytest.mark.asyncio
    async def test_resume_saves_checkpoint_on_second_failure(self, tmp_path: Path) -> None:
        """If resume also fails, a new checkpoint is saved."""
        wf_path = _write_workflow(tmp_path, "name: multi-agent\n")
        config = _multi_agent_config()

        def always_fail_handler(agent, prompt, context):
            if agent.name == "researcher":
                raise ProviderError("Still broken")
            return {"plan": "p"}

        provider = CopilotProvider(mock_handler=always_fail_handler)

        # Set up engine with restored state
        engine = WorkflowEngine(config, provider, workflow_path=wf_path)

        restored_ctx = WorkflowContext()
        restored_ctx.set_workflow_inputs({"topic": "AI"})
        restored_ctx.store("planner", {"plan": "p"})
        engine.set_context(restored_ctx)

        restored_limits = LimitEnforcer.from_dict(
            {"current_iteration": 1, "max_iterations": 10, "execution_history": ["planner"]},
        )
        engine.set_limits(restored_limits)

        with (
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            pytest.raises(ProviderError, match="Still broken"),
        ):
            await engine.resume("researcher")

        # A new checkpoint should be saved
        assert engine._last_checkpoint_path is not None
        cp = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
        assert cp.current_agent == "researcher"


# ---------------------------------------------------------------------------
# Checkpoint content validation
# ---------------------------------------------------------------------------


class TestCheckpointContent:
    """Verify checkpoint content is correct and complete."""

    @pytest.mark.asyncio
    async def test_checkpoint_has_workflow_inputs(self, tmp_path: Path) -> None:
        """Checkpoint includes workflow inputs."""
        wf_path = _write_workflow(tmp_path)
        config = _multi_agent_config()

        def mock_handler(agent, prompt, context):
            raise ProviderError("fail")

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=wf_path)

        with (
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            pytest.raises(ProviderError),
        ):
            await engine.run({"topic": "AI", "depth": "comprehensive"})

        assert engine._last_checkpoint_path is not None
        cp = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
        assert cp.inputs["topic"] == "AI"
        assert cp.inputs["depth"] == "comprehensive"

    @pytest.mark.asyncio
    async def test_checkpoint_has_correct_iteration_state(self, tmp_path: Path) -> None:
        """Checkpoint has correct iteration count and history."""
        wf_path = _write_workflow(tmp_path)
        config = _multi_agent_config()

        def mock_handler(agent, prompt, context):
            if agent.name == "planner":
                return {"plan": "p"}
            if agent.name == "researcher":
                return {"findings": "f"}
            raise ProviderError("fail at synthesizer")

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=wf_path)

        with (
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            pytest.raises(ProviderError),
        ):
            await engine.run({"topic": "AI"})

        cp = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
        assert cp.limits["current_iteration"] == 2
        assert cp.limits["execution_history"] == ["planner", "researcher"]
        assert cp.context["current_iteration"] == 2
        assert cp.context["execution_history"] == ["planner", "researcher"]

    @pytest.mark.asyncio
    async def test_checkpoint_workflow_hash(self, tmp_path: Path) -> None:
        """Checkpoint contains correct workflow hash."""
        wf_content = "name: test\nagents: []\n"
        wf_path = _write_workflow(tmp_path, wf_content)
        expected_hash = CheckpointManager.compute_workflow_hash(wf_path)

        config = _multi_agent_config()

        def mock_handler(agent, prompt, context):
            raise ProviderError("fail")

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=wf_path)

        with (
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            pytest.raises(ProviderError),
        ):
            await engine.run({"topic": "AI"})

        cp = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
        assert cp.workflow_hash == expected_hash


# ---------------------------------------------------------------------------
# set_context / set_limits tests
# ---------------------------------------------------------------------------


class TestSetContextAndLimits:
    """Verify set_context and set_limits correctly replace engine state."""

    def test_set_context_replaces_context(self) -> None:
        config = _multi_agent_config()
        engine = WorkflowEngine(config)

        new_ctx = WorkflowContext()
        new_ctx.set_workflow_inputs({"x": 1})
        new_ctx.store("agent_a", {"out": "data"})

        engine.set_context(new_ctx)

        assert engine.context is new_ctx
        assert engine.context.workflow_inputs == {"x": 1}
        assert "agent_a" in engine.context.agent_outputs

    def test_set_limits_replaces_limits(self) -> None:
        config = _multi_agent_config()
        engine = WorkflowEngine(config)

        new_limits = LimitEnforcer.from_dict(
            {"current_iteration": 5, "max_iterations": 20, "execution_history": ["a"] * 5},
            timeout_seconds=120,
        )

        engine.set_limits(new_limits)

        assert engine.limits is new_limits
        assert engine.limits.current_iteration == 5
        assert engine.limits.max_iterations == 20
        assert engine.limits.timeout_seconds == 120

    def test_set_context_repopulates_workflow_metadata(self, tmp_path: Path) -> None:
        """Resume must not drop workflow_dir/file/name from the context.

        ``WorkflowContext.from_dict()`` intentionally omits absolute path
        metadata so checkpoint files stay portable. The engine, which knows
        the current ``workflow_path`` and ``config``, must repopulate those
        fields when ``set_context()`` swaps in the restored context.

        Regression test for the resume path: without this, ``{{ workflow.dir }}``
        silently disappears from templates after resume — exactly the
        registry-based script-path scenario this feature exists for.
        """
        wf_path = _write_workflow(tmp_path)
        config = _multi_agent_config()
        engine = WorkflowEngine(config, workflow_path=wf_path)

        # Simulate a context restored from checkpoint: round-trip through
        # to_dict/from_dict, which strips the metadata.
        restored = WorkflowContext.from_dict(engine.context.to_dict())
        assert restored.workflow_dir == ""
        assert restored.workflow_file == ""
        assert restored.workflow_name == ""

        engine.set_context(restored)

        assert engine.context.workflow_dir == str(tmp_path.resolve())
        assert engine.context.workflow_file == str(wf_path.resolve())
        assert engine.context.workflow_name == config.workflow.name

        # End-to-end: the restored context must render workflow metadata
        # in templates via build_for_agent.
        agent_ctx = engine.context.build_for_agent("synthesizer", [], mode="accumulate")
        assert agent_ctx["workflow"]["dir"] == str(tmp_path.resolve())
        assert agent_ctx["workflow"]["file"] == str(wf_path.resolve())
        assert agent_ctx["workflow"]["name"] == config.workflow.name

    def test_set_context_without_workflow_path_still_sets_name(self) -> None:
        """When the engine has no workflow_path, only name is repopulated.

        Path-derived fields stay empty (and are omitted from rendered context
        per ``build_for_agent`` semantics).
        """
        config = _multi_agent_config()
        engine = WorkflowEngine(config)  # no workflow_path

        restored = WorkflowContext()
        engine.set_context(restored)

        assert engine.context.workflow_dir == ""
        assert engine.context.workflow_file == ""
        assert engine.context.workflow_name == config.workflow.name


# ---------------------------------------------------------------------------
# run_id / event_log_path persistence (issue #167)
# ---------------------------------------------------------------------------


class TestRunIdAndEventLogPathPersistence:
    """Verify the engine forwards RunContext.run_id/log_file into checkpoints."""

    @pytest.mark.asyncio
    async def test_checkpoint_persists_run_id_and_event_log_path(self, tmp_path: Path) -> None:
        """When a workflow fails, the saved checkpoint records the run_id and
        log path so resume_workflow_async can replay the original timeline
        into the web dashboard (issue #167)."""
        from conductor.engine.workflow import RunContext

        wf_path = _write_workflow(tmp_path)
        config = _multi_agent_config()

        def mock_handler(agent, prompt, context):
            raise ProviderError("boom")

        provider = CopilotProvider(mock_handler=mock_handler)
        log_file = tmp_path / "conductor-test.events.jsonl"
        log_file.write_text("")  # touch
        engine = WorkflowEngine(
            config,
            provider,
            workflow_path=wf_path,
            run_context=RunContext(run_id="r12345", log_file=str(log_file)),
        )

        with (
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            pytest.raises(ProviderError),
        ):
            await engine.run({"topic": "AI"})

        cp = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
        assert cp.run_id == "r12345"
        assert cp.event_log_path == str(log_file)

    @pytest.mark.asyncio
    async def test_checkpoint_defaults_run_id_empty_when_unset(self, tmp_path: Path) -> None:
        """Without a RunContext, fields default to empty strings (parity with old
        checkpoints)."""
        wf_path = _write_workflow(tmp_path)
        config = _multi_agent_config()

        def mock_handler(agent, prompt, context):
            raise ProviderError("boom")

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=wf_path)

        with (
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            pytest.raises(ProviderError),
        ):
            await engine.run({"topic": "AI"})

        cp = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
        assert cp.run_id == ""
        assert cp.event_log_path == ""


# ---------------------------------------------------------------------------
# build_workflow_started_data + suppress_workflow_started_emit (issue #167)
# ---------------------------------------------------------------------------


class TestBuildAndSuppressWorkflowStarted:
    """Verify the CLI resume path can seed the dashboard with topology."""

    def test_build_workflow_started_data_shape(self) -> None:
        """The build helper returns a dict matching the engine's emit shape."""
        config = _multi_agent_config()
        engine = WorkflowEngine(config)

        data = engine.build_workflow_started_data()

        assert data["name"] == "multi-agent"
        assert data["entry_point"] == "planner"
        agent_names = [a["name"] for a in data["agents"]]
        assert agent_names == ["planner", "researcher", "synthesizer"]
        # Routes are flattened from agent.routes + human_gate + parallel + for_each
        assert any(r["from"] == "planner" and r["to"] == "researcher" for r in data["routes"])
        # Carries metadata, system, run_id, log_file fields
        assert "metadata" in data
        assert "system" in data
        assert "run_id" in data
        assert "log_file" in data

    @pytest.mark.asyncio
    async def test_suppress_workflow_started_emit_skips_emit(self, tmp_path: Path) -> None:
        """When suppressed, engine.resume() does not emit ``workflow_started``."""
        from conductor.events import WorkflowEvent, WorkflowEventEmitter

        wf_path = _write_workflow(tmp_path)
        config = _multi_agent_config()

        def mock_handler(agent, prompt, context):
            return {"plan": "p", "findings": "f", "summary": "s"}[
                {"planner": "plan", "researcher": "findings", "synthesizer": "summary"}[agent.name]
            ]

        # ``mock_handler`` returns a string; need to wrap as dict to match
        # the AgentDef output schema. Simpler: build per-agent stub outputs.
        def stub_handler(agent, prompt, context):
            if agent.name == "planner":
                return {"plan": "p"}
            if agent.name == "researcher":
                return {"findings": "f"}
            return {"summary": "s"}

        emitter = WorkflowEventEmitter()
        captured_types: list[str] = []

        def capture(event: WorkflowEvent) -> None:
            captured_types.append(event.type)

        emitter.subscribe(capture)

        provider = CopilotProvider(mock_handler=stub_handler)
        engine = WorkflowEngine(config, provider, workflow_path=wf_path, event_emitter=emitter)

        # Sanity: without suppression, engine.run() emits workflow_started.
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            await engine.run({"topic": "AI"})

        assert "workflow_started" in captured_types
        assert "workflow_completed" in captured_types

        # Reset and try resume with suppression.
        captured_types.clear()
        engine2 = WorkflowEngine(config, provider, workflow_path=wf_path, event_emitter=emitter)
        engine2.set_context(WorkflowContext())
        engine2.set_limits(
            LimitEnforcer.from_dict(
                {"current_iteration": 0, "max_iterations": 10, "execution_history": []},
                timeout_seconds=120,
            )
        )
        engine2.context.set_workflow_inputs({"topic": "AI"})
        engine2.suppress_workflow_started_emit()
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            await engine2.resume("planner")

        # No workflow_started should have been emitted on resume.
        assert "workflow_started" not in captured_types
        # But workflow_completed IS still emitted (only the start is suppressed).
        assert "workflow_completed" in captured_types

    def test_clear_web_dashboard_detaches_from_engine_and_dialog(self) -> None:
        """`clear_web_dashboard` drops the dashboard from engine + DialogHandler.

        Regression coverage for the ``resume_workflow_async`` post-fix:
        the engine captures the dashboard at construction time, so if
        ``dashboard.start()`` later fails, simply setting the CLI's local
        ``dashboard = None`` leaves dangling references inside the engine
        that would block on never-arriving WebSocket gate input.
        """
        from unittest.mock import MagicMock

        config = _multi_agent_config()
        fake_dashboard = MagicMock()
        engine = WorkflowEngine(config, web_dashboard=fake_dashboard)

        assert engine._web_dashboard is fake_dashboard
        assert engine._dialog_handler.web_dashboard is fake_dashboard

        engine.clear_web_dashboard()

        assert engine._web_dashboard is None
        assert engine._dialog_handler.web_dashboard is None
