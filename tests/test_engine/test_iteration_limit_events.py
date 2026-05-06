"""Tests for iteration-limit dashboard event emission (issue #134).

When a workflow hits its ``max_iterations`` limit, the engine prompts the
user via a console-only Rich ``IntPrompt``. Without an event emission,
the web dashboard goes dark. These tests verify the engine emits
``iteration_limit_reached`` (before the gate) and ``iteration_limit_resolved``
(after the gate) so subscribers can render appropriate UI.
"""

from __future__ import annotations

import pytest

from conductor.config.schema import (
    AgentDef,
    LimitsConfig,
    RouteDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import MaxIterationsError
from conductor.providers.copilot import CopilotProvider


class EventCollector(WorkflowEventEmitter):
    """Simple emitter that collects events for inspection."""

    def __init__(self) -> None:
        self.events: list[WorkflowEvent] = []

    def emit(self, event: WorkflowEvent) -> None:
        self.events.append(event)

    def by_type(self, event_type: str) -> list[WorkflowEvent]:
        return [e for e in self.events if e.type == event_type]


@pytest.mark.asyncio
async def test_iteration_limit_emits_reached_event_in_skip_gates_mode() -> None:
    """When max_iterations is hit and skip_gates auto-stops the workflow,
    the dashboard must still see an ``iteration_limit_reached`` event."""

    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="bounded-loop",
            entry_point="looper",
            limits=LimitsConfig(max_iterations=3),
        ),
        agents=[
            AgentDef(
                name="looper",
                type="agent",
                model="gpt-4",
                prompt="loop",
                routes=[RouteDef(to="looper")],
            ),
        ],
        output={"result": "done"},
    )

    provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
    collector = EventCollector()

    engine = WorkflowEngine(
        config,
        provider=provider,
        event_emitter=collector,
        skip_gates=True,
    )

    with pytest.raises(MaxIterationsError):
        await engine.run({})

    reached = collector.by_type("iteration_limit_reached")
    assert len(reached) == 1, (
        f"expected one iteration_limit_reached event, got {len(reached)} "
        f"(all events: {[e.type for e in collector.events]})"
    )
    payload = reached[0].data
    assert payload["agent_name"] == "looper"
    assert payload["current_iteration"] == 3
    assert payload["max_iterations"] == 3
    assert payload["agent_history"][-1] == "looper"
    assert payload["skip_gates"] is True
    # Three consecutive 'looper' executions trip the loop heuristic
    assert payload["possible_loop"] is True


@pytest.mark.asyncio
async def test_iteration_limit_emits_resolved_event_in_skip_gates_mode() -> None:
    """``iteration_limit_resolved`` must follow the reached event so the
    dashboard can clear its gate UI even when the user chose to stop."""

    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="bounded-loop",
            entry_point="looper",
            limits=LimitsConfig(max_iterations=2),
        ),
        agents=[
            AgentDef(
                name="looper",
                type="agent",
                model="gpt-4",
                prompt="loop",
                routes=[RouteDef(to="looper")],
            ),
        ],
        output={"result": "done"},
    )

    provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
    collector = EventCollector()

    engine = WorkflowEngine(
        config,
        provider=provider,
        event_emitter=collector,
        skip_gates=True,
    )

    with pytest.raises(MaxIterationsError):
        await engine.run({})

    resolved = collector.by_type("iteration_limit_resolved")
    assert len(resolved) == 1, f"expected one iteration_limit_resolved event, got {len(resolved)}"
    payload = resolved[0].data
    assert payload["agent_name"] == "looper"
    # In skip_gates mode the auto-decision is to stop.
    assert payload["continue_execution"] is False
    assert payload["additional_iterations"] == 0


@pytest.mark.asyncio
async def test_iteration_limit_reached_emitted_before_resolved() -> None:
    """``reached`` must come before ``resolved`` so subscribers see the gate
    open and then close in order."""

    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="bounded-loop",
            entry_point="looper",
            limits=LimitsConfig(max_iterations=2),
        ),
        agents=[
            AgentDef(
                name="looper",
                type="agent",
                model="gpt-4",
                prompt="loop",
                routes=[RouteDef(to="looper")],
            ),
        ],
        output={"result": "done"},
    )

    provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
    collector = EventCollector()

    engine = WorkflowEngine(
        config,
        provider=provider,
        event_emitter=collector,
        skip_gates=True,
    )

    with pytest.raises(MaxIterationsError):
        await engine.run({})

    types = [e.type for e in collector.events]
    reached_idx = types.index("iteration_limit_reached")
    resolved_idx = types.index("iteration_limit_resolved")
    assert reached_idx < resolved_idx
