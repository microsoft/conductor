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
from unittest import mock
from unittest.mock import patch

import pytest

from conductor.cli.run import _run_with_stop_signal
from conductor.config.schema import (
    ContextConfig,
    LimitsConfig,
    RouteDef,
    RuntimeConfig,
    WaitStepDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.checkpoint import CheckpointManager
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import ExecutionError
from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.types import RunOutcome
from conductor.providers.copilot import CopilotProvider
from tests.test_execution.test_workspace_retention import (
    bundle,
    commands,
    lifecycle_engine,
)

pytest_plugins = ["tests.test_execution.test_workspace_retention"]


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
            WaitStepDef(
                name="pause",
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


@pytest.mark.asyncio
async def test_retained_cancel_finalizes_before_failed_checkpoint_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Requirement: a failed stop checkpoint warns after retaining, regardless of a stale path.
    wf_path = tmp_path / "workflow.yaml"
    wf_path.write_text("name: kill-checkpoint\n", encoding="utf-8")
    engine = WorkflowEngine(
        _wait_workflow(),
        CopilotProvider(mock_handler=lambda a, p, c: {}),
        workflow_path=wf_path,
    )
    engine._run_id = "retain-on-cancel"
    engine._last_checkpoint_path = tmp_path / "stale.json"
    started = asyncio.Event()
    order: list[str] = []

    async def pause(*_args: object) -> None:
        started.set()
        await asyncio.Future()

    async def finalize(_outcome: str, *, retain: bool = False) -> None:
        assert retain is True
        order.append("finalize")

    def failed_write(_error: BaseException) -> None:
        order.append("checkpoint")
        return None

    class StopOnStart:
        async def wait_for_stop(self) -> None:
            await started.wait()

    with (
        patch.object(engine, "_workspace_persistence", return_value="on-failure"),
        patch.object(engine, "_execute_wait", side_effect=pause),
        patch.object(engine._execution_session, "finalize_leases", side_effect=finalize),
        patch.object(engine, "_save_checkpoint_on_failure", side_effect=failed_write),
        patch("conductor.engine.workflow.acquire_workspace_claim") as claim,
        pytest.raises(ExecutionError, match="stopped by user"),
    ):
        await _run_with_stop_signal(engine, {}, StopOnStart())

    assert order == ["finalize", "checkpoint"]
    assert "retain-on-cancel" in caplog.text
    assert "docker volume rm conductor-ws-retain-on-cancel" in caplog.text
    claim.return_value.release.assert_called_once()


@pytest.mark.asyncio
async def test_dashboard_cancel_keeps_fake_docker_volume_on_failed_checkpoint(
    docker_processes: tuple[DockerRunnerBackend, str, Path, Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Requirement: stop retains Docker before reporting a failed current checkpoint write.
    _backend, _binary, store, log = docker_processes
    engine = lifecycle_engine(tmp_path, entry="pause")
    engine._last_checkpoint_path = tmp_path / "stale-checkpoint.json"
    started = asyncio.Event()
    volume = store / "volumes" / "conductor-ws-retention-engine"
    order: list[str] = []

    async def paused(*_args: object) -> None:
        started.set()
        await asyncio.Future()

    class StopWhenPaused:
        async def wait_for_stop(self) -> None:
            await started.wait()

    original_finalize = engine._execution_session.finalize_leases
    original_stop = engine.handle_dashboard_stop

    async def finalize(outcome: RunOutcome, *, retain: bool = False) -> None:
        assert retain is True
        order.append("finalize")
        await original_finalize(outcome, retain=retain)

    def stopped(message: str) -> None:
        assert order == ["finalize"]
        assert volume.exists()
        order.append("stop")
        original_stop(message)

    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=mock.AsyncMock,
            return_value=bundle(tmp_path),
        ),
        patch.object(engine, "_execute_wait", side_effect=paused),
        patch.object(engine._execution_session, "finalize_leases", side_effect=finalize),
        patch.object(engine, "handle_dashboard_stop", side_effect=stopped),
        patch.object(CheckpointManager, "save_checkpoint", return_value=None),
        pytest.raises(ExecutionError, match="stopped by user"),
    ):
        await _run_with_stop_signal(engine, {}, StopWhenPaused())
    assert order == ["finalize", "stop"]
    assert volume.exists()
    assert not any(argv[:2] == ["volume", "rm"] for argv in commands(log))
    assert "retention-engine" in caplog.text
    assert "docker volume rm conductor-ws-retention-engine" in caplog.text
