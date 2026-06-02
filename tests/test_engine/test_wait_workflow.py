"""Integration tests for ``type: wait`` steps in :class:`WorkflowEngine`.

Tests cover:
- Linear workflow with a wait step that routes to ``$end``.
- Wait output (``{"waited_seconds": float}``) accessible in downstream
  agent context.
- Workflow-level ``limits.timeout_seconds`` cancels an in-flight wait
  via :class:`ConductorTimeoutError`.
- Interrupt event during a wait surfaces in events and stops execution.
- Emits expected ``agent_started`` (with ``agent_type: "wait"``),
  ``wait_started``, and ``wait_completed`` events.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    LimitsConfig,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import TimeoutError as ConductorTimeoutError


def _make_config(
    agents: list[AgentDef],
    *,
    entry: str,
    timeout_seconds: int | None = None,
    output: dict[str, str] | None = None,
) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="wait-test",
            description="wait test",
            version="1.0.0",
            entry_point=entry,
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10, timeout_seconds=timeout_seconds),
        ),
        agents=agents,
        output=output or {},
    )


class TestWaitWorkflowLinear:
    @pytest.mark.asyncio
    async def test_wait_runs_to_end(self) -> None:
        config = _make_config(
            [
                AgentDef(
                    name="pause",
                    type="wait",
                    duration="50ms",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            entry="pause",
            output={"slept": "{{ pause.output.waited_seconds }}"},
        )
        engine = WorkflowEngine(config, MagicMock())
        result = await engine.run({})
        assert "slept" in result
        # Output is rendered to a string by the workflow output template.
        # Avoid a tight lower bound — CI scheduling jitter can land the
        # measured value slightly under the requested duration.
        assert float(result["slept"]) >= 0.0
        assert float(result["slept"]) < 5.0

    @pytest.mark.asyncio
    async def test_wait_output_only_has_waited_seconds(self) -> None:
        """Per issue #218, the wait output contract is strict: only
        ``waited_seconds`` is exposed in workflow context."""
        config = _make_config(
            [
                AgentDef(
                    name="pause",
                    type="wait",
                    duration="20ms",
                    reason="should not leak into context",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            entry="pause",
        )
        engine = WorkflowEngine(config, MagicMock())
        await engine.run({})
        # Stored under pause.output — must contain ONLY waited_seconds.
        stored = engine.context.get_for_template().get("pause", {}).get("output", {})
        assert set(stored.keys()) == {"waited_seconds"}
        assert stored["waited_seconds"] >= 0.0


class TestWaitWorkflowTimeout:
    @pytest.mark.asyncio
    async def test_workflow_timeout_cancels_wait(self) -> None:
        """A long wait must be cancelled by the workflow-level timeout."""
        config = _make_config(
            [
                AgentDef(
                    name="long_pause",
                    type="wait",
                    duration="60s",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            entry="long_pause",
            timeout_seconds=1,
        )
        engine = WorkflowEngine(config, MagicMock())
        with pytest.raises(ConductorTimeoutError):
            await engine.run({})


class TestWaitWorkflowEvents:
    @pytest.mark.asyncio
    async def test_emits_wait_lifecycle(self) -> None:
        emitter = WorkflowEventEmitter()
        events: list[WorkflowEvent] = []
        emitter.subscribe(events.append)

        config = _make_config(
            [
                AgentDef(
                    name="pause",
                    type="wait",
                    duration="20ms",
                    reason="quick",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            entry="pause",
        )
        engine = WorkflowEngine(config, MagicMock(), event_emitter=emitter)
        await engine.run({})

        types = [e.type for e in events]
        assert "agent_started" in types
        assert "wait_started" in types
        assert "wait_completed" in types

        # agent_started carries the agent_type discriminator.
        started = next(e for e in events if e.type == "agent_started")
        assert started.data.get("agent_type") == "wait"

        ws = next(e for e in events if e.type == "wait_started")
        assert ws.data["agent_name"] == "pause"
        assert ws.data["duration_seconds"] == pytest.approx(0.02)
        assert ws.data["reason"] == "quick"

        wc = next(e for e in events if e.type == "wait_completed")
        assert wc.data["agent_name"] == "pause"
        assert wc.data["waited_seconds"] >= 0.0
        assert wc.data["requested_seconds"] == pytest.approx(0.02)
        assert wc.data["interrupted"] is False

    @pytest.mark.asyncio
    async def test_emits_wait_failed_on_runtime_validation(self) -> None:
        """Runtime validation errors (e.g. a templated duration that
        evaluates to a value over the 24h cap) must emit a
        ``wait_failed`` event before the exception unwinds. Without
        this, the dashboard would show a hanging "started but never
        completed" wait node on any failure."""
        from conductor.exceptions import ValidationError

        emitter = WorkflowEventEmitter()
        events: list[WorkflowEvent] = []
        emitter.subscribe(events.append)

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="wait-failed",
                entry_point="pause",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=5),
                input={
                    "hours": {  # type: ignore[dict-item]
                        "type": "number",
                        "default": 25,
                    }
                },
            ),
            agents=[
                AgentDef(
                    name="pause",
                    type="wait",
                    duration="{{ workflow.input.hours }}h",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        engine = WorkflowEngine(config, MagicMock(), event_emitter=emitter)
        with pytest.raises(ValidationError):
            await engine.run({"hours": 25})

        failed = [e for e in events if e.type == "wait_failed"]
        assert failed, "expected a wait_failed event"
        data = failed[0].data
        assert data["agent_name"] == "pause"
        assert data["error_type"] == "ValidationError"
        assert "24h cap" in data["message"]
        assert "elapsed" in data


class TestWaitWorkflowTemplatedDuration:
    @pytest.mark.asyncio
    async def test_templated_duration_from_workflow_input(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="wait-templated",
                entry_point="pause",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=5),
                input={
                    "interval_ms": {  # type: ignore[dict-item]
                        "type": "number",
                        "default": 30,
                    }
                },
            ),
            agents=[
                AgentDef(
                    name="pause",
                    type="wait",
                    duration="{{ workflow.input.interval_ms }}ms",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        engine = WorkflowEngine(config, MagicMock())
        await engine.run({"interval_ms": 25})
        stored = engine.context.get_for_template().get("pause", {}).get("output", {})
        assert stored["waited_seconds"] >= 0.0


class TestWaitWorkflowInterrupt:
    @pytest.mark.asyncio
    async def test_interrupt_event_cuts_wait_short(self) -> None:
        """When the interrupt event fires mid-wait, the wait completes
        early with ``interrupted=True`` and the next agent receives
        control as soon as the between-step interrupt check returns.

        We use ``skip_gates=True`` so the interrupt handler auto-stops
        without trying to prompt the user, and assert on the emitted
        ``wait_completed`` payload rather than the engine's outcome
        (which is governed by the generic interrupt path, not by wait).
        """
        emitter = WorkflowEventEmitter()
        events: list[WorkflowEvent] = []
        emitter.subscribe(events.append)

        interrupt_event = asyncio.Event()
        config = _make_config(
            [
                AgentDef(
                    name="long_pause",
                    type="wait",
                    duration="30s",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            entry="long_pause",
        )
        engine = WorkflowEngine(
            config,
            MagicMock(),
            event_emitter=emitter,
            interrupt_event=interrupt_event,
            skip_gates=True,
        )

        async def kick() -> None:
            await asyncio.sleep(0.1)
            interrupt_event.set()

        # We don't care what the engine ultimately raises — it depends
        # on the interactive interrupt path. We only care that the wait
        # itself was cut short.
        with contextlib.suppress(BaseException):
            await asyncio.gather(engine.run({}), kick())

        wait_completed = [e for e in events if e.type == "wait_completed"]
        assert wait_completed, "expected a wait_completed event"
        payload = wait_completed[0].data
        assert payload["interrupted"] is True
        assert payload["waited_seconds"] < 5.0  # nowhere near 30s
