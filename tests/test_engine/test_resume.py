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
