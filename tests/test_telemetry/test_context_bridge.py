"""Requirements tests for task-local OpenTelemetry span context bridging."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from dataclasses import dataclass

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from conductor.events import WorkflowEvent
from conductor.telemetry.subscriber import TelemetrySubscriber
from conductor.telemetry.subscriber_state import SpanState


@dataclass(frozen=True, slots=True)
class Tracing:
    """Own the in-memory telemetry objects used by one requirements test."""

    subscriber: TelemetrySubscriber
    exporter: InMemorySpanExporter


@pytest.fixture
def tracing() -> Generator[Tracing]:
    """Provide a subscriber backed by a synchronous in-memory exporter."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    subscriber = TelemetrySubscriber(provider)
    yield Tracing(subscriber=subscriber, exporter=exporter)
    subscriber.close()


def _event(event_type: str, **data: object) -> WorkflowEvent:
    """Create a deterministic workflow event for subscriber-only behavior."""
    return WorkflowEvent(type=event_type, timestamp=1.0, data=data)


def _state(subscriber: TelemetrySubscriber) -> SpanState:
    """Return the enabled state owned by a test subscriber."""
    assert subscriber._state is not None
    return subscriber._state


def _current_span_id() -> int:
    """Return the currently attached span ID for parentage assertions."""
    return trace.get_current_span().get_span_context().span_id


def _current_span_is_invalid() -> bool:
    """Return whether the task holds no valid OpenTelemetry span."""
    return not trace.get_current_span().get_span_context().is_valid


@pytest.mark.asyncio
async def test_sequential_a_to_b_detaches_attached_spans_in_lifo_order(
    tracing: Tracing, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement: a task unwinds nested orchestration contexts in LIFO order."""
    # Given: a workflow that runs A and then B in its one owning asyncio task.
    subscriber = tracing.subscriber
    state = _state(subscriber)
    subscriber.on_event(_event("workflow_started", name="sequence", run_id="run-sequence"))
    subscriber.on_event(_event("agent_started", agent_name="a"))
    detached_names: list[str] = []
    original_detach = otel_context.detach

    def detach_spy(token):
        key = next(
            key for key, (saved_token, _) in state._attach_tokens.items() if saved_token is token
        )
        detached_names.append(key[0])
        original_detach(token)

    monkeypatch.setattr(otel_context, "detach", detach_spy)

    # When: each sequential agent and then the workflow reaches its terminal event.
    subscriber.on_event(_event("agent_completed", agent_name="a"))
    subscriber.on_event(_event("agent_started", agent_name="b"))
    subscriber.on_event(_event("agent_completed", agent_name="b"))
    subscriber.on_event(_event("workflow_completed"))

    # Then: each completed scope restores its owner task's previous active span.
    assert detached_names == ["invoke_agent a", "invoke_agent b", "invoke_workflow sequence"]
    assert _current_span_is_invalid()


@pytest.mark.asyncio
async def test_nested_subworkflow_restores_the_delegating_agent_context(tracing: Tracing) -> None:
    """Requirement: nested workflow completion restores the owning agent context."""
    # Given: a delegating agent that starts a nested workflow.
    subscriber = tracing.subscriber
    state = _state(subscriber)
    subscriber.on_event(_event("workflow_started", name="parent", run_id="run-1"))
    subscriber.on_event(_event("agent_started", agent_name="delegate"))
    delegate = state.latest_agent((), "delegate")
    assert delegate is not None
    subscriber.on_event(
        _event("subworkflow_started", agent_name="delegate", parent_path=[], slot_key="child")
    )
    subscriber.on_event(_event("workflow_started", name="child", subworkflow_path=["child"]))
    child = state.workflow_keys[("child",)]
    subscriber.on_event(_event("agent_started", agent_name="nested", subworkflow_path=["child"]))

    # When: the nested workflow completes before its outer agent does.
    subscriber.on_event(_event("agent_completed", agent_name="nested", subworkflow_path=["child"]))
    subscriber.on_event(_event("workflow_completed", subworkflow_path=["child"]))

    # Then: the native span starts under the restored delegate rather than the child workflow.
    native_span = state.tracer.start_span("native-child")
    native_span.end()
    native = next(
        span for span in tracing.exporter.get_finished_spans() if span.name == "native-child"
    )
    assert native.parent is not None
    assert native.parent.span_id == state.open_spans[delegate].get_span_context().span_id
    assert _current_span_id() == state.open_spans[delegate].get_span_context().span_id
    assert child not in state.open_spans
    subscriber.on_event(_event("subworkflow_completed", parent_path=[], slot_key="child"))
    subscriber.on_event(_event("workflow_completed"))
    assert _current_span_is_invalid()


@pytest.mark.asyncio
async def test_validator_completion_restores_primary_agent_context(tracing: Tracing) -> None:
    """Requirement: a validator detaches before the primary agent it validates."""
    # Given: an attached primary agent and its attached validator child.
    subscriber = tracing.subscriber
    state = _state(subscriber)
    subscriber.on_event(_event("workflow_started", name="validate", run_id="run-2"))
    subscriber.on_event(_event("agent_started", agent_name="writer"))
    agent = state.latest_agent((), "writer")
    assert agent is not None
    subscriber.on_event(_event("agent_validator_start", agent_name="writer"))

    # When: validation completes ahead of the primary completion event.
    subscriber.on_event(_event("agent_validator_complete", agent_name="writer", errored=False))

    # Then: the primary span is again current until it completes.
    assert _current_span_id() == state.open_spans[agent].get_span_context().span_id
    subscriber.on_event(_event("agent_completed", agent_name="writer"))
    subscriber.on_event(_event("workflow_completed"))
    assert _current_span_is_invalid()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "outcome", "uses_gate"),
    [
        ("empty", "completed", False),
        ("skip", "skipped_remaining", False),
        ("normal", "completed", True),
        ("abort", "aborted", True),
    ],
)
async def test_questions_events_close_once_without_creating_gate_spans(
    tracing: Tracing, case: str, outcome: str, uses_gate: bool
) -> None:
    """Requirement: each questions outcome closes its agent only at questions_completed."""
    # Given: the generic agent-start lifecycle for one questions node.
    subscriber = tracing.subscriber
    state = _state(subscriber)
    subscriber.on_event(_event("workflow_started", name=case, run_id=f"run-{case}"))
    subscriber.on_event(_event("agent_started", agent_name="ask", agent_type="questions"))
    agent = state.latest_agent((), "ask")
    assert agent is not None
    subscriber.on_event(_event("questions_presented", agent_name="ask", total=int(uses_gate)))
    assert agent in state.open_spans

    # When: optional reused gate transitions precede the questions terminal event.
    if uses_gate:
        subscriber.on_event(_event("gate_presented", agent_name="ask", step_type="questions"))
        subscriber.on_event(_event("gate_resolved", agent_name="ask", step_type="questions"))
    subscriber.on_event(_event("questions_completed", agent_name="ask", outcome=outcome))
    subscriber.on_event(_event("workflow_completed"))

    # Then: no synthetic gate spans exist and the task has no leaked current span.
    assert all(
        span.name not in {"gate_presented", "gate_resolved"}
        for span in tracing.exporter.get_finished_spans()
    )
    assert _current_span_is_invalid()


@pytest.mark.asyncio
async def test_fail_fast_parallel_defers_worker_detach_to_its_owner_task(
    tracing: Tracing, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement: a fail-fast parent never detaches a cancelled worker's context token."""
    # Given: a main task, one attached parallel group, and an attached worker task.
    subscriber = tracing.subscriber
    state = _state(subscriber)
    subscriber.on_event(_event("workflow_started", name="parallel", run_id="run-parallel"))
    subscriber.on_event(_event("parallel_started", group_name="workers"))
    worker_started = asyncio.Event()
    allow_worker_finish = asyncio.Event()

    async def run_worker() -> None:
        subscriber.on_event(
            _event("parallel_agent_started", group_name="workers", agent_name="worker")
        )
        worker_started.set()
        await allow_worker_finish.wait()
        subscriber.on_event(
            _event("parallel_agent_failed", group_name="workers", agent_name="worker")
        )

    worker = asyncio.create_task(run_worker())
    await worker_started.wait()
    worker_key = state.parallel_keys[((), "workers", "worker")]
    original_detach = otel_context.detach

    def detach_spy(token) -> None:
        owner_task = next(
            owner for saved_token, owner in state._attach_tokens.values() if saved_token is token
        )
        assert owner_task is asyncio.current_task()
        original_detach(token)

    monkeypatch.setattr(otel_context, "detach", detach_spy)

    # When: fail-fast closes members from the main task before the worker reports failure.
    subscriber.on_event(_event("parallel_completed", group_name="workers", failure_count=1))
    assert worker_key not in state.open_spans
    assert worker_key in state._attach_tokens
    allow_worker_finish.set()
    await worker
    subscriber.on_event(_event("workflow_completed"))

    # Then: only each token's owner detached it and the run leaves no active span.
    assert state._attach_tokens == {}
    assert _current_span_is_invalid()


@pytest.mark.asyncio
async def test_keyboard_interrupt_cleanup_detaches_open_context_and_is_idempotent(
    tracing: Tracing,
) -> None:
    """Requirement: CLI cleanup after KeyboardInterrupt leaves no current span behind."""
    # Given: an interrupted workflow with still-open attached orchestration spans.
    subscriber = tracing.subscriber
    subscriber.on_event(_event("workflow_started", name="interrupt", run_id="run-interrupt"))
    subscriber.on_event(_event("agent_started", agent_name="open"))

    # When: the CLI finally block closes telemetry while propagating the interrupt.
    with pytest.raises(KeyboardInterrupt):
        try:
            raise KeyboardInterrupt
        finally:
            subscriber.close()
    subscriber.close()

    # Then: cleanup is idempotent and the task-local OpenTelemetry context is invalid.
    assert subscriber._open_spans == {}
    assert _current_span_is_invalid()
