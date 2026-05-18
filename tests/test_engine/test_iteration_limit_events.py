"""Tests for iteration-limit dashboard event emission (issues #134 and #198).

When a workflow hits its ``max_iterations`` limit, the engine normally prompts
the user via a console Rich ``IntPrompt``. Issue #134 added events so the
dashboard knows a gate is open. Issue #198 added a web-only resolution path
so ``--web-bg`` (and ``--web``) can actually resolve the gate from the
dashboard instead of exiting silently when stdin is ``/dev/null``.

These tests verify the engine:

- emits ``iteration_limit_reached`` (before the gate) and
  ``iteration_limit_resolved`` (after) so subscribers can render UI;
- correlates the two events with a per-gate ``gate_id`` (issue #198);
- chooses the right resolution path (CLI / web-only / race) based on the
  presence of a web dashboard and whether stdin is interactive.
"""

from __future__ import annotations

import asyncio
from typing import Any
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
from conductor.engine.workflow import RunContext, WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import MaxIterationsError
from conductor.providers.copilot import CopilotProvider


class EventCollector(WorkflowEventEmitter):
    """Simple emitter that collects events for inspection.

    Also forwards events to any subscribers (``self.subscribe(callback)``)
    so tests can react to events as they're emitted — useful for
    ``iteration_limit_reached`` follow-up actions like enqueueing a
    dashboard response.
    """

    def __init__(self) -> None:
        super().__init__()
        self.events: list[WorkflowEvent] = []

    def emit(self, event: WorkflowEvent) -> None:
        self.events.append(event)
        # Forward to base-class subscribers so tests can react to events.
        super().emit(event)

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
    # gate_id is generated per-gate and must match between reached and resolved
    # so subscribers can correlate the pair (issue #198).
    assert isinstance(resolved[0].data.get("gate_id"), str)
    assert resolved[0].data["gate_id"] == reached[0].data["gate_id"]
    assert resolved[1].data["gate_id"] == reached[1].data["gate_id"]
    assert resolved[0].data["gate_id"] != resolved[1].data["gate_id"]
    payload0 = {k: v for k, v in resolved[0].data.items() if k != "gate_id"}
    assert payload0 == {
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
    ``skip_gates=False`` so the dashboard renders an interactive prompt
    (issue #198) rather than "auto-stopping".
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


# ---------------------------------------------------------------------------
# Issue #198: ``--web-bg`` (and ``--web``) iteration-limit resolution
# ---------------------------------------------------------------------------


class WebDashboardStub:
    """Minimal stand-in for ``WebDashboard`` in iteration-limit tests.

    Implements just enough surface for ``_resolve_max_iterations_gate`` and
    ``_wait_for_web_iteration_limit``: a queued response delivered to
    ``wait_for_iteration_limit_response`` and a settable ``_stop_event``
    consumed via ``wait_for_stop``.
    """

    def __init__(self) -> None:
        self._response_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stop_event = asyncio.Event()
        # Spy fields so tests can assert what the engine asked for.
        self.awaited_gate_ids: list[str] = []

    def enqueue_response(
        self,
        *,
        gate_id: str,
        additional_iterations: int,
        agent_name: str | None = None,
        group_name: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "gate_id": gate_id,
            "additional_iterations": additional_iterations,
        }
        if agent_name is not None:
            payload["agent_name"] = agent_name
        if group_name is not None:
            payload["group_name"] = group_name
        self._response_queue.put_nowait(payload)

    def trigger_stop(self) -> None:
        self._stop_event.set()

    async def wait_for_iteration_limit_response(self, gate_id: str) -> dict[str, Any]:
        self.awaited_gate_ids.append(gate_id)
        while True:
            msg = await self._response_queue.get()
            if msg.get("gate_id") == gate_id:
                return msg
            # Mirror the production discard policy. Tests that exercise the
            # "no matching response → stop wins" path use ``trigger_stop``
            # rather than enqueueing a stale message.
            continue

    async def wait_for_stop(self) -> None:
        await self._stop_event.wait()


@pytest.mark.asyncio
async def test_web_only_path_continues_via_dashboard_in_bg_mode() -> None:
    """In ``--web-bg`` (bg_mode=True) the engine must NOT call the CLI
    prompt — the dashboard response alone resolves the gate.

    Regression for the original #198 silent-exit bug: previously the
    CLI prompt would race-win immediately on EOFError → stop, beating
    any actual dashboard click. After the fix, the CLI prompt is never
    invoked when bg_mode is on.
    """
    config = _looper_config(max_iterations=2)
    provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
    collector = EventCollector()
    dashboard = WebDashboardStub()

    engine = WorkflowEngine(
        config,
        provider=provider,
        event_emitter=collector,
        skip_gates=False,
        web_dashboard=dashboard,  # type: ignore[arg-type]
        run_context=RunContext(bg_mode=True),
    )

    # Pre-arm dashboard responses: grant 3 the first time, stop the second.
    # We don't know the gate_ids yet — pre-seed using a wrapper that snags
    # the gate_id from the emitted event and enqueues just-in-time.
    cli_call_count = 0

    async def fake_cli(*args: Any, **kwargs: Any) -> Any:
        nonlocal cli_call_count
        cli_call_count += 1
        raise AssertionError(
            "CLI prompt was invoked in --web-bg mode; "
            "the engine must wait for the dashboard instead (issue #198)"
        )

    def enqueue_on_reached(event: WorkflowEvent) -> None:
        if event.type != "iteration_limit_reached":
            return
        gid = event.data["gate_id"]
        # First gate: continue with 3. Second: stop.
        additional = 3 if len(dashboard.awaited_gate_ids) == 0 else 0
        # Enqueue via a task-friendly path: the queue is fed before the
        # engine is scheduled to await it, so this works synchronously.
        dashboard.enqueue_response(
            gate_id=gid,
            additional_iterations=additional,
            agent_name="looper",
        )

    collector.subscribe(enqueue_on_reached)

    with (
        patch(
            "conductor.gates.human.MaxIterationsHandler.handle_limit_reached",
            side_effect=fake_cli,
        ),
        pytest.raises(MaxIterationsError),
    ):
        await engine.run({})

    assert cli_call_count == 0, "CLI prompt must not run in bg mode"

    reached = collector.by_type("iteration_limit_reached")
    resolved = collector.by_type("iteration_limit_resolved")
    assert len(reached) == 2
    assert len(resolved) == 2
    # Engine queried the dashboard with the matching gate_ids.
    assert dashboard.awaited_gate_ids == [
        reached[0].data["gate_id"],
        reached[1].data["gate_id"],
    ]
    # First gate granted 3 additional iterations.
    assert resolved[0].data["continue_execution"] is True
    assert resolved[0].data["additional_iterations"] == 3
    # Second gate stopped the workflow.
    assert resolved[1].data["continue_execution"] is False
    assert resolved[1].data["additional_iterations"] == 0


@pytest.mark.asyncio
async def test_web_only_path_stops_when_dashboard_returns_zero() -> None:
    """Dashboard sending ``additional_iterations=0`` in bg mode is treated
    as an explicit stop — same semantics as the legacy CLI prompt."""
    config = _looper_config(max_iterations=2)
    provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
    collector = EventCollector()
    dashboard = WebDashboardStub()

    def enqueue_on_reached(event: WorkflowEvent) -> None:
        if event.type == "iteration_limit_reached":
            dashboard.enqueue_response(
                gate_id=event.data["gate_id"],
                additional_iterations=0,
                agent_name="looper",
            )

    collector.subscribe(enqueue_on_reached)

    engine = WorkflowEngine(
        config,
        provider=provider,
        event_emitter=collector,
        skip_gates=False,
        web_dashboard=dashboard,  # type: ignore[arg-type]
        run_context=RunContext(bg_mode=True),
    )

    with pytest.raises(MaxIterationsError):
        await engine.run({})

    resolved = collector.by_type("iteration_limit_resolved")
    assert len(resolved) == 1
    assert resolved[0].data["continue_execution"] is False
    assert resolved[0].data["additional_iterations"] == 0
    assert resolved[0].data["aborted"] is False


@pytest.mark.asyncio
async def test_web_only_path_dashboard_stop_event_terminates_wait() -> None:
    """If the user triggers ``POST /api/stop`` while the engine is waiting
    on the dashboard, the wait must terminate (treated as stop) rather
    than block forever. Without this, a ``--web-bg`` run with no
    dashboard tab open would hang at the gate indefinitely.
    """
    config = _looper_config(max_iterations=2)
    provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
    collector = EventCollector()
    dashboard = WebDashboardStub()

    # Fire the dashboard stop signal as soon as the gate opens — no
    # ``iteration_limit_response`` is ever enqueued.
    def trip_stop_on_reached(event: WorkflowEvent) -> None:
        if event.type == "iteration_limit_reached":
            dashboard.trigger_stop()

    collector.subscribe(trip_stop_on_reached)

    engine = WorkflowEngine(
        config,
        provider=provider,
        event_emitter=collector,
        skip_gates=False,
        web_dashboard=dashboard,  # type: ignore[arg-type]
        run_context=RunContext(bg_mode=True),
    )

    with pytest.raises(MaxIterationsError):
        await asyncio.wait_for(engine.run({}), timeout=5.0)

    resolved = collector.by_type("iteration_limit_resolved")
    assert len(resolved) == 1
    # Stop signal wins → continue=False, additional=0, aborted=False
    # (deliberate stop, not an unexpected exception).
    assert resolved[0].data["continue_execution"] is False
    assert resolved[0].data["additional_iterations"] == 0
    assert resolved[0].data["aborted"] is False


@pytest.mark.asyncio
async def test_skip_gates_with_web_dashboard_does_not_wait_for_web() -> None:
    """``--skip-gates`` must auto-stop synchronously even when a web
    dashboard is attached — it would be surprising if dashboards turned
    skip_gates into an interactive prompt."""
    config = _looper_config(max_iterations=2)
    provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
    collector = EventCollector()
    dashboard = WebDashboardStub()

    engine = WorkflowEngine(
        config,
        provider=provider,
        event_emitter=collector,
        skip_gates=True,
        web_dashboard=dashboard,  # type: ignore[arg-type]
        run_context=RunContext(bg_mode=True),
    )

    with pytest.raises(MaxIterationsError):
        await asyncio.wait_for(engine.run({}), timeout=5.0)

    # Engine never queried the dashboard.
    assert dashboard.awaited_gate_ids == []
    resolved = collector.by_type("iteration_limit_resolved")
    assert len(resolved) == 1
    assert resolved[0].data["continue_execution"] is False


@pytest.mark.asyncio
async def test_iteration_limit_reached_includes_gate_id() -> None:
    """Each ``iteration_limit_reached`` payload carries a unique ``gate_id``
    and the matching ``iteration_limit_resolved`` echoes it back. This is
    the correlation key the dashboard needs (issue #198) to ensure a stale
    response from a previous gate cannot resolve a later one.
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
    resolved = collector.by_type("iteration_limit_resolved")
    assert len(reached) == 1
    assert len(resolved) == 1
    assert isinstance(reached[0].data["gate_id"], str)
    assert len(reached[0].data["gate_id"]) >= 16  # uuid4 hex is 32 chars
    assert resolved[0].data["gate_id"] == reached[0].data["gate_id"]


@pytest.mark.asyncio
async def test_parallel_group_gate_id_correlates_reached_and_resolved() -> None:
    """The parallel-group emission path also stamps a ``gate_id`` on both
    events. Different gates get distinct ids so a stale dashboard click
    can't be misapplied."""
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
            AgentDef(name="worker_a", type="agent", model="gpt-4", prompt="work"),
            AgentDef(name="worker_b", type="agent", model="gpt-4", prompt="work"),
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
    resolved = collector.by_type("iteration_limit_resolved")
    assert len(reached) == 1
    assert len(resolved) == 1
    assert reached[0].data["group_name"] == "workers"
    assert isinstance(reached[0].data["gate_id"], str)
    assert resolved[0].data["gate_id"] == reached[0].data["gate_id"]
    assert resolved[0].data["group_name"] == "workers"


@pytest.mark.asyncio
async def test_no_web_dashboard_falls_back_to_cli_path() -> None:
    """Without a web dashboard, the engine still uses the legacy CLI
    prompt path — including its ``EOFError → stop`` fallback on non-TTY
    stdin. Regression guard: the #198 fix must not break the path that
    runs without ``--web`` or ``--web-bg``.
    """
    config = _looper_config(max_iterations=2)
    provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
    collector = EventCollector()
    engine = WorkflowEngine(
        config,
        provider=provider,
        event_emitter=collector,
        skip_gates=False,
        # No web_dashboard, no bg_mode.
    )

    with (
        patch("conductor.gates.human.IntPrompt.ask", side_effect=EOFError()),
        pytest.raises(MaxIterationsError),
    ):
        await engine.run({})

    resolved = collector.by_type("iteration_limit_resolved")
    assert len(resolved) == 1
    # EOFError is caught inside the handler and converted to stop.
    assert resolved[0].data["continue_execution"] is False
    assert resolved[0].data["aborted"] is False


@pytest.mark.asyncio
async def test_tty_with_web_dashboard_races_cli_and_web() -> None:
    """When stdin is a real TTY AND a web dashboard is attached
    (``conductor run --web`` from an interactive terminal), the engine
    races the CLI prompt and the web response. The web response wins
    here because it's pre-armed; the CLI side is patched to hang.
    """
    config = _looper_config(max_iterations=2)
    provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
    collector = EventCollector()
    dashboard = WebDashboardStub()

    def enqueue_on_reached(event: WorkflowEvent) -> None:
        if event.type == "iteration_limit_reached":
            # Stop on the first gate so the test terminates promptly.
            dashboard.enqueue_response(
                gate_id=event.data["gate_id"],
                additional_iterations=0,
                agent_name="looper",
            )

    collector.subscribe(enqueue_on_reached)

    engine = WorkflowEngine(
        config,
        provider=provider,
        event_emitter=collector,
        skip_gates=False,
        web_dashboard=dashboard,  # type: ignore[arg-type]
        # bg_mode=False to take the race path — but we still need to
        # claim stdin is a TTY because the helper checks both.
        run_context=RunContext(bg_mode=False),
    )

    # Patch isatty so the engine takes the race path, and patch the CLI
    # prompt to "hang" forever — the web task must win.
    async def hanging_cli(*args: Any, **kwargs: Any) -> Any:
        # Sleep for far longer than the test timeout — if the race is
        # broken and the CLI task wins, the test times out instead of
        # silently passing.
        await asyncio.sleep(60)
        raise AssertionError("CLI prompt finished before web — race broken")

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch(
            "conductor.gates.human.MaxIterationsHandler.handle_limit_reached",
            side_effect=hanging_cli,
        ),
        pytest.raises(MaxIterationsError),
    ):
        await asyncio.wait_for(engine.run({}), timeout=5.0)

    # Web won the race and chose stop.
    resolved = collector.by_type("iteration_limit_resolved")
    assert len(resolved) == 1
    assert resolved[0].data["continue_execution"] is False
    assert resolved[0].data["additional_iterations"] == 0
    # Dashboard was queried for the actual gate_id.
    reached = collector.by_type("iteration_limit_reached")
    assert dashboard.awaited_gate_ids == [reached[0].data["gate_id"]]


@pytest.mark.asyncio
async def test_web_only_path_clamps_negative_additional_iterations() -> None:
    """Defensive: a malformed dashboard response (negative value, non-int)
    must be coerced to a clean stop rather than propagating bad state."""
    config = _looper_config(max_iterations=2)
    provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
    collector = EventCollector()
    dashboard = WebDashboardStub()

    def enqueue_on_reached(event: WorkflowEvent) -> None:
        if event.type == "iteration_limit_reached":
            dashboard._response_queue.put_nowait(
                {
                    "gate_id": event.data["gate_id"],
                    "agent_name": "looper",
                    "additional_iterations": -42,  # malformed
                }
            )

    collector.subscribe(enqueue_on_reached)

    engine = WorkflowEngine(
        config,
        provider=provider,
        event_emitter=collector,
        skip_gates=False,
        web_dashboard=dashboard,  # type: ignore[arg-type]
        run_context=RunContext(bg_mode=True),
    )

    with pytest.raises(MaxIterationsError):
        await asyncio.wait_for(engine.run({}), timeout=5.0)

    resolved = collector.by_type("iteration_limit_resolved")
    assert len(resolved) == 1
    assert resolved[0].data["continue_execution"] is False
    assert resolved[0].data["additional_iterations"] == 0
