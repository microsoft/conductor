"""Integration tests for periodic / milestone checkpoints (issue #244).

These tests drive the real :class:`WorkflowEngine` execution loop with
provider-free ``script``/``set`` steps and assert the periodic checkpoint
behavior wired in at the loop boundary:

- disabled by default (no behavior change)
- ``every_agent`` saves at each boundary, skipping the first iteration
- ``every_seconds`` throttles saves to boundaries past the interval
- periodic checkpoints are cleaned up on successful completion
- sub-workflow engines never write periodic checkpoints
- on failure, periodic checkpoints are retained alongside the failure one
- resuming from a periodic checkpoint continues forward without re-running
  already-completed steps
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from conductor.config.schema import (
    AgentDef,
    CheckpointConfig,
    LimitsConfig,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.checkpoint import CheckpointManager
from conductor.engine.workflow import RunContext, WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import ConductorError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _script(name: str, text: str, to: str) -> AgentDef:
    """A script step that prints *text* and routes to *to*."""
    return AgentDef(
        name=name,
        type="script",
        command=sys.executable,
        args=["-c", f"print({text!r})"],
        routes=[RouteDef(to=to)],
    )


def _three_step_config(
    checkpoint: CheckpointConfig | None = None,
    *,
    last_step: AgentDef | None = None,
) -> WorkflowConfig:
    """Build a linear step1 -> step2 -> step3 -> $end workflow."""
    runtime = RuntimeConfig(provider="copilot")
    if checkpoint is not None:
        runtime.checkpoint = checkpoint
    third = last_step or _script("step3", "three", "$end")
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="periodic-ckpt",
            entry_point="step1",
            runtime=runtime,
            limits=LimitsConfig(max_iterations=20),
        ),
        agents=[
            _script("step1", "one", "step2"),
            _script("step2", "two", "step3"),
            third,
        ],
        output={},
    )


def _capture_checkpoints(emitter: WorkflowEventEmitter) -> list[dict[str, Any]]:
    """Subscribe to the emitter and collect checkpoint_saved event data."""
    saved: list[dict[str, Any]] = []

    def _on(event: WorkflowEvent) -> None:
        if event.type == "checkpoint_saved":
            saved.append(dict(event.data))

    emitter.subscribe(_on)
    return saved


def _make_engine(
    config: WorkflowConfig,
    workflow_path: Path,
    emitter: WorkflowEventEmitter,
    *,
    run_id: str = "run-1",
    subworkflow_depth: int = 0,
) -> WorkflowEngine:
    return WorkflowEngine(
        config,
        None,
        workflow_path=workflow_path,
        event_emitter=emitter,
        run_context=RunContext(run_id=run_id, log_file=""),
        _subworkflow_depth=subworkflow_depth,
    )


def _periodic_files(ckpt_dir: Path) -> list[Path]:
    return sorted(ckpt_dir.glob("*.json"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPeriodicCheckpointEngine:
    @pytest.mark.asyncio
    async def test_disabled_by_default_emits_no_periodic(self, tmp_path: Path) -> None:
        wf = tmp_path / "wf.yaml"
        wf.write_text("name: periodic-ckpt\n")
        ckpt_dir = tmp_path / "ckpts"
        ckpt_dir.mkdir()
        emitter = WorkflowEventEmitter()
        saved = _capture_checkpoints(emitter)

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=ckpt_dir):
            engine = _make_engine(_three_step_config(), wf, emitter)
            await engine.run({})

        assert saved == []
        assert _periodic_files(ckpt_dir) == []

    @pytest.mark.asyncio
    async def test_every_agent_saves_each_boundary_skipping_first(self, tmp_path: Path) -> None:
        wf = tmp_path / "wf.yaml"
        wf.write_text("name: periodic-ckpt\n")
        ckpt_dir = tmp_path / "ckpts"
        ckpt_dir.mkdir()
        emitter = WorkflowEventEmitter()
        saved = _capture_checkpoints(emitter)

        config = _three_step_config(CheckpointConfig(every_agent=True))
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=ckpt_dir):
            engine = _make_engine(config, wf, emitter)
            await engine.run({})

        # The entry-point boundary (step1) is skipped; step2 and step3 save.
        assert [d["agent_name"] for d in saved] == ["step2", "step3"]
        assert all(d["trigger"] == "periodic" for d in saved)
        assert all(d["error_type"] is None for d in saved)

    @pytest.mark.asyncio
    async def test_every_seconds_throttles_to_first_boundary(self, tmp_path: Path) -> None:
        wf = tmp_path / "wf.yaml"
        wf.write_text("name: periodic-ckpt\n")
        ckpt_dir = tmp_path / "ckpts"
        ckpt_dir.mkdir()
        emitter = WorkflowEventEmitter()
        saved = _capture_checkpoints(emitter)

        # A huge interval means only the first eligible boundary saves; later
        # boundaries are throttled out.
        config = _three_step_config(CheckpointConfig(every_seconds=9999))
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=ckpt_dir):
            engine = _make_engine(config, wf, emitter)
            await engine.run({})

        assert [d["agent_name"] for d in saved] == ["step2"]

    @pytest.mark.asyncio
    async def test_periodic_checkpoints_cleaned_up_on_success(self, tmp_path: Path) -> None:
        wf = tmp_path / "wf.yaml"
        wf.write_text("name: periodic-ckpt\n")
        ckpt_dir = tmp_path / "ckpts"
        ckpt_dir.mkdir()
        emitter = WorkflowEventEmitter()
        saved = _capture_checkpoints(emitter)

        config = _three_step_config(CheckpointConfig(every_agent=True))
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=ckpt_dir):
            engine = _make_engine(config, wf, emitter)
            await engine.run({})

        # Checkpoints were written during the run...
        assert len(saved) == 2
        # ...but cleaned up once the run completed successfully.
        assert _periodic_files(ckpt_dir) == []

    @pytest.mark.asyncio
    async def test_subworkflow_engine_does_not_checkpoint(self, tmp_path: Path) -> None:
        wf = tmp_path / "wf.yaml"
        wf.write_text("name: periodic-ckpt\n")
        ckpt_dir = tmp_path / "ckpts"
        ckpt_dir.mkdir()
        emitter = WorkflowEventEmitter()
        saved = _capture_checkpoints(emitter)

        config = _three_step_config(CheckpointConfig(every_agent=True))
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=ckpt_dir):
            engine = _make_engine(config, wf, emitter, subworkflow_depth=1)
            await engine.run({})

        assert saved == []
        assert _periodic_files(ckpt_dir) == []

    @pytest.mark.asyncio
    async def test_failure_retains_periodic_checkpoints(self, tmp_path: Path) -> None:
        wf = tmp_path / "wf.yaml"
        wf.write_text("name: periodic-ckpt\n")
        ckpt_dir = tmp_path / "ckpts"
        ckpt_dir.mkdir()
        emitter = WorkflowEventEmitter()
        _capture_checkpoints(emitter)

        # Last step is a set step that raises at render time (division by zero),
        # forcing a runtime failure after periodic checkpoints were saved.
        boom = AgentDef(
            name="step3",
            type="set",
            value="{{ 1 // 0 }}",
            routes=[RouteDef(to="$end")],
        )
        config = _three_step_config(CheckpointConfig(every_agent=True), last_step=boom)
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=ckpt_dir):
            engine = _make_engine(config, wf, emitter)
            with pytest.raises(ConductorError):
                await engine.run({})

            checkpoints = CheckpointManager.list_checkpoints(wf)

        triggers = sorted(c.trigger for c in checkpoints)
        # Periodic checkpoints (step2, step3) survive the failure, and a
        # failure checkpoint is written for the failed step.
        assert "failure" in triggers
        assert "periodic" in triggers

    @pytest.mark.asyncio
    async def test_resume_from_periodic_checkpoint_continues_forward(self, tmp_path: Path) -> None:
        wf = tmp_path / "wf.yaml"
        wf.write_text("name: periodic-ckpt\n")
        ckpt_dir = tmp_path / "ckpts"
        ckpt_dir.mkdir()

        # Each step appends to its own marker file so we can prove which steps
        # executed across the original run and the resume.
        c1, c2, c3 = (tmp_path / f"c{i}.txt" for i in (1, 2, 3))

        def counter_step(name: str, path: Path, to: str) -> AgentDef:
            code = f"open({str(path)!r}, 'a').write('x')"
            return AgentDef(
                name=name,
                type="script",
                command=sys.executable,
                args=["-c", code],
                routes=[RouteDef(to=to)],
            )

        def build() -> WorkflowConfig:
            runtime = RuntimeConfig(provider="copilot")
            runtime.checkpoint = CheckpointConfig(every_agent=True)
            return WorkflowConfig(
                workflow=WorkflowDef(
                    name="periodic-ckpt",
                    entry_point="step1",
                    runtime=runtime,
                    limits=LimitsConfig(max_iterations=20),
                ),
                agents=[
                    counter_step("step1", c1, "step2"),
                    counter_step("step2", c2, "step3"),
                    counter_step("step3", c3, "$end"),
                ],
                output={},
            )

        emitter = WorkflowEventEmitter()
        # Keep the periodic checkpoint files around after success so we can
        # resume from one (the engine would otherwise clean them up).
        with (
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=ckpt_dir),
            patch.object(CheckpointManager, "cleanup_periodic_for_run"),
        ):
            engine = _make_engine(build(), wf, emitter)
            await engine.run({})

            # After the original run each step ran exactly once.
            assert c1.read_text() == "x"
            assert c2.read_text() == "x"
            assert c3.read_text() == "x"

            # Find the periodic checkpoint taken just before step3.
            step3_cp = next(
                c
                for c in CheckpointManager.list_checkpoints(wf)
                if c.trigger == "periodic" and c.current_agent == "step3"
            )

            # Resume from it with a fresh engine.
            from conductor.engine.context import WorkflowContext
            from conductor.engine.limits import LimitEnforcer

            resume_engine = _make_engine(build(), wf, WorkflowEventEmitter())
            resume_engine.set_context(WorkflowContext.from_dict(step3_cp.context))
            resume_engine.set_limits(LimitEnforcer.from_dict(step3_cp.limits, timeout_seconds=None))
            await resume_engine.resume("step3")

        # Resume re-ran only step3; step1 and step2 were not executed again.
        assert c1.read_text() == "x"
        assert c2.read_text() == "x"
        assert c3.read_text() == "xx"
