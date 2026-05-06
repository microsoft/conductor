"""Tests for iteration-limit dashboard event emission (issue #134).

When a workflow hits its ``max_iterations`` limit, the engine prompts the
user via a console-only Rich ``IntPrompt``. Without an event emission,
the web dashboard goes dark. These tests verify the engine emits
``iteration_limit_reached`` (before the gate) and ``iteration_limit_resolved``
(after the gate) so subscribers can render appropriate UI.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from conductor.config.schema import (
    AgentDef,
    LimitsConfig,
    ParallelGroup,
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
    # No agent events should fire between reached and resolved — the workflow
    # must remain quiescent inside the gate. This catches regressions where
    # the engine continues executing while subscribers see an open gate.
    between = types[reached_idx + 1 : resolved_idx]
    assert not any(t.startswith("agent_") for t in between), (
        f"unexpected agent events between reached/resolved: {between}"
    )


def _looper_config(max_iterations: int) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="bounded-loop",
            entry_point="looper",
            limits=LimitsConfig(max_iterations=max_iterations),
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


@pytest.mark.asyncio
async def test_iteration_limit_interactive_user_continues() -> None:
    """Interactive prompt path: user grants more iterations.

    Verifies the resolved payload reports the user's choice (``continue=True``,
    ``additional_iterations=N``) and that the workflow actually resumes past
    the gate. ``IntPrompt.ask`` is patched to grant 5 the first time, then 0
    on the next gate hit so the workflow eventually terminates.
    """
    config = _looper_config(max_iterations=2)
    provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
    collector = EventCollector()
    engine = WorkflowEngine(
        config,
        provider=provider,
        event_emitter=collector,
        skip_gates=False,
    )

    with (
        patch("conductor.gates.human.IntPrompt.ask", side_effect=[5, 0]),
        pytest.raises(MaxIterationsError),
    ):
        await engine.run({})

    reached = collector.by_type("iteration_limit_reached")
    resolved = collector.by_type("iteration_limit_resolved")
    # First gate granted 5, second declined → exactly two of each.
    assert len(reached) == 2
    assert len(resolved) == 2
    # First gate was an interactive (non-skip_gates) prompt that continued.
    assert reached[0].data["skip_gates"] is False
    assert resolved[0].data == {
        "agent_name": "looper",
        "continue_execution": True,
        "additional_iterations": 5,
        "aborted": False,
    }
    # Second gate stopped the workflow.
    assert resolved[1].data["continue_execution"] is False
    assert resolved[1].data["additional_iterations"] == 0


@pytest.mark.asyncio
async def test_iteration_limit_interactive_user_declines() -> None:
    """Interactive prompt path: user enters 0 to stop.

    Distinct from ``skip_gates`` mode: the reached event must report
    ``skip_gates=False`` so the dashboard renders "awaiting console input"
    rather than "auto-stopping".
    """
    config = _looper_config(max_iterations=2)
    provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
    collector = EventCollector()
    engine = WorkflowEngine(
        config,
        provider=provider,
        event_emitter=collector,
        skip_gates=False,
    )

    with (
        patch("conductor.gates.human.IntPrompt.ask", return_value=0),
        pytest.raises(MaxIterationsError),
    ):
        await engine.run({})

    reached = collector.by_type("iteration_limit_reached")
    resolved = collector.by_type("iteration_limit_resolved")
    assert len(reached) == 1
    assert len(resolved) == 1
    assert reached[0].data["skip_gates"] is False
    assert resolved[0].data["continue_execution"] is False
    assert resolved[0].data["additional_iterations"] == 0
    assert resolved[0].data["aborted"] is False


@pytest.mark.asyncio
async def test_iteration_limit_resolved_emitted_when_prompt_raises() -> None:
    """Critical: if ``handle_limit_reached`` raises an unexpected exception,
    the resolved event must STILL fire (with ``aborted=True``) so the dashboard
    gate doesn't stay stuck open. See PR #162 review (silent-failure-hunter C1).
    """
    config = _looper_config(max_iterations=2)
    provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
    collector = EventCollector()
    engine = WorkflowEngine(
        config,
        provider=provider,
        event_emitter=collector,
        skip_gates=False,
    )

    # Raise an exception not caught by _prompt_for_additional_iterations
    # (which catches ValueError, KeyboardInterrupt, EOFError).
    target = "conductor.gates.human.MaxIterationsHandler.handle_limit_reached"
    with (
        patch(target, side_effect=RuntimeError("simulated console crash")),
        pytest.raises(RuntimeError, match="simulated console crash"),
    ):
        await engine.run({})

    reached = collector.by_type("iteration_limit_reached")
    resolved = collector.by_type("iteration_limit_resolved")
    assert len(reached) == 1, "reached must be emitted before the prompt"
    assert len(resolved) == 1, (
        "resolved must STILL fire when the prompt raises, otherwise the "
        "dashboard gate stays stuck open (issue #134)"
    )
    payload = resolved[0].data
    assert payload["aborted"] is True
    assert payload["continue_execution"] is False
    assert payload["additional_iterations"] == 0
    # Reached must precede resolved even on the abort path.
    types = [e.type for e in collector.events]
    assert types.index("iteration_limit_reached") < types.index("iteration_limit_resolved")


@pytest.mark.asyncio
async def test_iteration_limit_eoferror_treated_as_stop() -> None:
    """``EOFError`` from ``IntPrompt.ask`` (non-TTY environments like CI or
    ``< /dev/null``) must be caught inside the prompt helper and turned into
    a clean "stop" — NOT bubble up as an unexpected exception. This means the
    resolved event reports ``continue_execution=False`` (a deliberate stop),
    not ``aborted=True``.
    """
    config = _looper_config(max_iterations=2)
    provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
    collector = EventCollector()
    engine = WorkflowEngine(
        config,
        provider=provider,
        event_emitter=collector,
        skip_gates=False,
    )

    with (
        patch("conductor.gates.human.IntPrompt.ask", side_effect=EOFError()),
        pytest.raises(MaxIterationsError),
    ):
        await engine.run({})

    resolved = collector.by_type("iteration_limit_resolved")
    assert len(resolved) == 1
    payload = resolved[0].data
    # Caught cleanly inside the prompt helper, not aborted.
    assert payload["continue_execution"] is False
    assert payload["additional_iterations"] == 0
    assert payload["aborted"] is False


@pytest.mark.asyncio
async def test_iteration_limit_emitted_for_parallel_group() -> None:
    """The parallel-group emission site (``_check_parallel_group_iteration_with_prompt``)
    must emit a payload keyed by ``group_name`` + ``agent_count``, never
    ``agent_name``. Previously untested — guards against a copy/paste regression
    between the two helper methods.
    """
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="parallel-loop",
            entry_point="trigger",
            limits=LimitsConfig(max_iterations=2),
        ),
        agents=[
            AgentDef(
                name="trigger",
                type="agent",
                model="gpt-4",
                prompt="trigger",
                routes=[RouteDef(to="workers")],
            ),
            AgentDef(
                name="worker_a",
                type="agent",
                model="gpt-4",
                prompt="work",
            ),
            AgentDef(
                name="worker_b",
                type="agent",
                model="gpt-4",
                prompt="work",
            ),
        ],
        parallel=[
            ParallelGroup(
                name="workers",
                agents=["worker_a", "worker_b"],
                routes=[RouteDef(to="$end")],
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
    assert len(reached) == 1
    payload = reached[0].data
    # Parallel-group payload has group_name + agent_count, NOT agent_name.
    assert payload["group_name"] == "workers"
    assert payload["agent_count"] == 2
    assert "agent_name" not in payload

    resolved = collector.by_type("iteration_limit_resolved")
    assert len(resolved) == 1
    assert resolved[0].data["group_name"] == "workers"
    assert "agent_name" not in resolved[0].data


@pytest.mark.asyncio
async def test_possible_loop_false_for_mixed_history() -> None:
    """``possible_loop`` must be False when the trailing 3 entries of
    ``agent_history`` are not all the same agent. Catches regressions of the
    ``len(set(history[-3:])) <= 1`` guard.
    """
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="ping-pong",
            entry_point="ping",
            limits=LimitsConfig(max_iterations=4),
        ),
        agents=[
            AgentDef(
                name="ping",
                type="agent",
                model="gpt-4",
                prompt="p",
                routes=[RouteDef(to="pong")],
            ),
            AgentDef(
                name="pong",
                type="agent",
                model="gpt-4",
                prompt="p",
                routes=[RouteDef(to="ping")],
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
    assert len(reached) == 1
    payload = reached[0].data
    # The last three entries alternate (e.g. pong, ping, pong) so they are not
    # all equal — possible_loop must be False even though the workflow is
    # genuinely cycling between two agents.
    assert payload["possible_loop"] is False
    assert len(set(payload["agent_history"])) == 2


@pytest.mark.asyncio
async def test_possible_loop_false_for_short_history() -> None:
    """``possible_loop`` must be False when fewer than 3 agents have run, even
    if every entry is the same agent. Guards against dropping the
    ``len(history) >= 3`` length check.
    """
    config = _looper_config(max_iterations=2)
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
    assert len(reached) == 1
    payload = reached[0].data
    # Only 2 executions before the gate fires, so the heuristic must abstain.
    assert len(payload["agent_history"]) == 2
    assert payload["possible_loop"] is False
