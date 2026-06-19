"""Integration test for issue #245: a dashboard Kill that cancels the engine
mid-step must still write a checkpoint and emit a terminal event.

Drives the real :class:`WorkflowEngine` through the CLI stop helper
(:func:`_run_with_stop_signal`) with a long ``type: wait`` entry step so the
engine is genuinely mid-step (a cancellable ``asyncio.sleep``) when the stop
fires — the exact path that previously lost progress with no checkpoint.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.cli.run import _run_with_stop_signal
from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    LimitsConfig,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.checkpoint import CheckpointManager
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import ExecutionError
from conductor.providers.copilot import CopilotProvider


class _StopAfter:
    """Minimal dashboard stand-in whose ``wait_for_stop`` fires after a delay."""

    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def wait_for_stop(self) -> None:
        await asyncio.sleep(self._delay)


def _wait_workflow() -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="kill-checkpoint",
            entry_point="pause",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            AgentDef(
                name="pause",
                type="wait",
                duration="30s",
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={},
    )


@pytest.mark.asyncio
async def test_kill_mid_step_writes_checkpoint(tmp_path: Path) -> None:
    wf_path = tmp_path / "workflow.yaml"
    wf_path.write_text("name: kill-checkpoint\n")

    emitter = WorkflowEventEmitter()
    events: list[WorkflowEvent] = []
    emitter.subscribe(events.append)

    engine = WorkflowEngine(
        _wait_workflow(),
        CopilotProvider(mock_handler=lambda a, p, c: {}),
        workflow_path=wf_path,
        event_emitter=emitter,
    )

    dashboard = _StopAfter(delay=0.05)

    with (
        patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        pytest.raises(ExecutionError, match="stopped by user"),
    ):
        await _run_with_stop_signal(engine, {}, dashboard)

    # A best-effort checkpoint was written for the cancelled run.
    assert engine._last_checkpoint_path is not None
    assert engine._last_checkpoint_path.exists()

    # And the dashboard-facing terminal events were emitted.
    failed = [e for e in events if e.type == "workflow_failed"]
    assert len(failed) == 1
    assert failed[0].data["stopped_by_user"] is True
    assert any(e.type == "checkpoint_saved" for e in events)

    # The checkpoint resumes from the in-flight wait step.
    cp = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
    assert cp.current_agent == "pause"
