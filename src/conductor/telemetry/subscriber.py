"""Build detached OpenTelemetry spans from Conductor workflow events."""

from __future__ import annotations

import time
from contextlib import suppress
from typing import TYPE_CHECKING

from opentelemetry import trace

from conductor.events import WorkflowEvent
from conductor.telemetry import guards
from conductor.telemetry import subscriber_execution as execution
from conductor.telemetry import subscriber_workflows as workflows
from conductor.telemetry.subscriber_state import SpanState
from conductor.telemetry.subscriber_types import SpanKey

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider


class TelemetrySubscriber:
    """Translate paired workflow events into explicitly parented detached spans."""

    def __init__(self, tracer_provider: TracerProvider | None, *, resumed: bool = False) -> None:
        """Create an inert subscriber when the optional SDK is unavailable.

        Args:
            tracer_provider: OpenTelemetry SDK tracer provider for this run.
            resumed: Whether this subscriber represents a resumed workflow run.
                When True, the root workflow span is stamped with
                ``conductor.resumed=true``.
        """
        self._tracer_provider = tracer_provider
        self._state = SpanState(tracer_provider, resumed=resumed) if tracer_provider else None
        self._closed = False

    @property
    def _open_spans(self) -> dict[SpanKey, trace.Span]:
        """Expose open spans for compatibility with focused lifecycle tests."""
        return self._state.open_spans if self._state else {}

    def on_event(self, event: WorkflowEvent) -> None:
        """Consume one event without relying on a task-local current span."""
        if self._state is None or self._closed:
            return
        _dispatch(self._state, event)

    def close(self) -> None:
        """Finish open spans, flush exporters, and reset process-local guards."""
        if self._closed:
            return
        self._closed = True
        if self._state is not None:
            closed_event = WorkflowEvent(type="telemetry_closed", timestamp=time.time())
            self._state.finish_all(closed_event, failed=False)
            self._state.detach_close_tokens()
            self._state.clear_indexes()
        if self._tracer_provider is not None:
            with suppress(Exception):
                self._tracer_provider.force_flush(timeout_millis=5_000)
            with suppress(Exception):
                self._tracer_provider.shutdown()
        guards.reset_telemetry_context()


def _dispatch(state: SpanState, event: WorkflowEvent) -> None:
    """Route an engine event to its narrow lifecycle handler."""
    match event.type:
        case "workflow_started":
            workflows.workflow_started(state, event)
        case "workflow_completed":
            workflows.workflow_completed(state, event)
        case "workflow_failed":
            workflows.workflow_failed(state, event)
        case "parallel_started" | "for_each_started":
            workflows.group_started(state, event)
        case "parallel_completed" | "for_each_completed":
            workflows.group_completed(state, event)
        case "subworkflow_started":
            workflows.subworkflow_started(state, event)
        case "subworkflow_completed":
            workflows.subworkflow_completed(state, event)
        case "subworkflow_failed":
            workflows.subworkflow_failed(state, event)
        case "parallel_agent_started":
            execution.parallel_agent_started(state, event)
        case "parallel_agent_completed":
            execution.parallel_agent_completed(state, event)
        case "parallel_agent_failed":
            execution.parallel_agent_failed(state, event)
        case "for_each_item_started":
            execution.item_started(state, event)
        case "for_each_agent_started":
            execution.item_agent_started(state, event)
        case "for_each_item_completed":
            execution.item_completed(state, event)
        case "for_each_item_failed":
            execution.item_failed(state, event)
        case "agent_started":
            execution.agent_started(state, event)
        case "agent_completed":
            execution.agent_completed(state, event)
        case "agent_failed":
            execution.agent_failed(state, event)
        case "questions_completed":
            execution._finish_agent(state, event, failed=False)
        case "questions_presented":
            pass
        case "script_started" | "set_started" | "wait_started":
            execution.step_started(state, event)
        case "script_completed" | "set_completed" | "wait_completed":
            execution.step_completed(state, event)
        case "script_failed" | "set_failed" | "wait_failed":
            execution.step_failed(state, event)
        case "agent_validator_start":
            execution.validator_started(state, event)
        case "agent_validator_complete":
            execution.validator_completed(state, event)
        case "agent_tool_start":
            execution.tool_started(state, event)
        case "agent_tool_complete":
            execution.tool_completed(state, event)
        case "gate_presented" | "gate_resolved":
            execution.gate_event(state, event)
        case _:
            pass
    state.detach_finished_for_current_task()
