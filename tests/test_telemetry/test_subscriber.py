"""Requirements tests for event-driven OpenTelemetry span creation."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from unittest.mock import Mock

import pytest

from conductor.events import WorkflowEvent
from conductor.telemetry import guards
from conductor.telemetry.semconv import (
    CONDUCTOR_RESUMED,
    CONDUCTOR_STEP_TYPE,
    ERROR_TYPE,
    GEN_AI_AGENT_NAME,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_OPERATION_NAME,
    GEN_AI_TOOL_NAME,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    INVOKE_AGENT,
    INVOKE_WORKFLOW,
)
from conductor.telemetry.subscriber import TelemetrySubscriber

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider


class SpanExporter(Protocol):
    """Provide finished spans for assertions without importing the optional SDK."""

    def get_finished_spans(self) -> tuple[ReadableSpan, ...]:
        """Return every span received by the in-memory exporter."""
        ...


@dataclass(frozen=True, slots=True)
class Tracing:
    """Own an enabled subscriber and its synchronous test exporter."""

    subscriber: TelemetrySubscriber
    exporter: SpanExporter
    provider: TracerProvider


@pytest.fixture
def tracing() -> Generator[Tracing]:
    """Provide an in-memory exporter with a detached-span subscriber."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    subscriber = TelemetrySubscriber(provider)
    yield Tracing(subscriber=subscriber, exporter=exporter, provider=provider)
    subscriber.close()


def _event(event_type: str, timestamp: float, **data: object) -> WorkflowEvent:
    """Build one synthetic engine event at a deterministic timestamp."""
    return WorkflowEvent(type=event_type, timestamp=timestamp, data=data)


def _spans(exporter: SpanExporter) -> list[ReadableSpan]:
    """Return exported spans without coupling tests to an internal fixture type."""
    return list(exporter.get_finished_spans())


def _span(spans: list[ReadableSpan], name: str) -> ReadableSpan:
    """Return the uniquely named exported span."""
    return next(span for span in spans if span.name == name)


def test_subscriber_creates_nested_workflow_agent_and_tool_spans(
    tracing: Tracing,
) -> None:
    """Requirement: paired events form a detached root → agent → tool span tree."""
    # Given: an enabled tracer and a complete LLM agent lifecycle.
    subscriber = tracing.subscriber

    # When: the engine event sequence reaches successful workflow completion.
    subscriber.on_event(_event("workflow_started", 10.0, name="research", run_id="run-1"))
    subscriber.on_event(
        _event("agent_started", 11.0, agent_name="planner", iteration=1, agent_type="agent")
    )
    subscriber.on_event(
        _event(
            "agent_tool_start",
            12.0,
            agent_name="planner",
            tool_name="search",
            tool_call_id="c1",
        )
    )
    subscriber.on_event(
        _event(
            "agent_tool_complete", 13.0, agent_name="planner", tool_name="search", tool_call_id="c1"
        )
    )
    subscriber.on_event(
        _event(
            "agent_completed",
            14.0,
            agent_name="planner",
            model="gpt-5",
            input_tokens=12,
            output_tokens=8,
            cost_usd=0.02,
        )
    )
    subscriber.on_event(_event("workflow_completed", 15.0))

    # Then: names, attributes, and explicit parent identities are preserved.
    spans = _spans(tracing.exporter)
    root = _span(spans, f"{INVOKE_WORKFLOW} research")
    agent = _span(spans, f"{INVOKE_AGENT} planner")
    tool = _span(spans, "execute_tool search")
    agent_parent = agent.parent
    tool_parent = tool.parent
    root_context = root.context
    agent_context = agent.context
    assert agent_parent is not None
    assert tool_parent is not None
    assert root_context is not None
    assert agent_context is not None
    assert root.attributes is not None
    assert agent.attributes is not None
    assert tool.attributes is not None
    assert agent_parent.span_id == root_context.span_id
    assert tool_parent.span_id == agent_context.span_id
    assert root.attributes[GEN_AI_OPERATION_NAME] == INVOKE_WORKFLOW
    assert root.attributes[GEN_AI_CONVERSATION_ID] == "run-1"
    assert agent.attributes[GEN_AI_AGENT_NAME] == "planner"
    assert agent.attributes[GEN_AI_USAGE_INPUT_TOKENS] == 12
    assert agent.attributes[GEN_AI_USAGE_OUTPUT_TOKENS] == 8
    assert tool.attributes[GEN_AI_TOOL_NAME] == "search"
    assert subscriber._open_spans == {}


def test_workflow_failure_closes_an_unpaired_agent_as_error(
    tracing: Tracing,
) -> None:
    """Requirement: workflow_failed closes LLM work that has no agent_failed event."""
    # Given: an agent whose provider fails before its completion event.
    subscriber = tracing.subscriber
    subscriber.on_event(_event("workflow_started", 10.0, name="failure", run_id="run-2"))
    subscriber.on_event(_event("agent_started", 11.0, agent_name="writer", iteration=1))

    # When: the engine reports only the terminal workflow failure.
    subscriber.on_event(
        _event(
            "workflow_failed",
            12.0,
            agent_name="writer",
            error_type="ProviderError",
            message="provider disconnected",
        )
    )

    # Then: both the root and pending agent end with an error status.
    from opentelemetry.trace import StatusCode

    agent = _span(_spans(tracing.exporter), f"{INVOKE_AGENT} writer")
    assert agent.attributes is not None
    assert agent.status.status_code is StatusCode.ERROR
    assert agent.attributes[ERROR_TYPE] == "ProviderError"
    assert agent.attributes["error.message"] == "provider disconnected"
    assert subscriber._open_spans == {}


def test_duplicate_workflow_start_for_same_run_reuses_the_root_span(
    tracing: Tracing,
) -> None:
    """Requirement: a resume duplicate does not create a second root trace."""
    # Given: a run whose resume path emits workflow_started a second time.
    subscriber = tracing.subscriber
    subscriber.on_event(_event("workflow_started", 10.0, name="resume", run_id="run-3"))

    # When: the duplicate uses the same run identifier.
    subscriber.on_event(_event("workflow_started", 11.0, name="resume", run_id="run-3"))
    subscriber.on_event(_event("workflow_completed", 12.0))

    # Then: exactly one root was exported and marks the continuation.
    spans = _spans(tracing.exporter)
    roots = [span for span in spans if span.name == f"{INVOKE_WORKFLOW} resume"]
    assert len(roots) == 1
    assert roots[0].attributes is not None
    assert roots[0].attributes[CONDUCTOR_RESUMED] is True


def test_gate_events_create_short_correlated_spans(
    tracing: Tracing,
) -> None:
    """Requirement: human-gate events never hold a span across user waiting time."""
    # Given: an open human-gate step.
    subscriber = tracing.subscriber
    subscriber.on_event(_event("workflow_started", 10.0, name="approval", run_id="run-4"))
    subscriber.on_event(
        _event("agent_started", 11.0, agent_name="approve", iteration=1, agent_type="human_gate")
    )

    # When: the gate is presented and resolved at distinct event times.
    subscriber.on_event(_event("gate_presented", 12.0, agent_name="approve"))
    subscriber.on_event(_event("gate_resolved", 20.0, agent_name="approve"))
    subscriber.on_event(_event("workflow_completed", 21.0))

    # Then: each gate event is represented by a zero-duration correlated span.
    gate_spans = [
        span
        for span in _spans(tracing.exporter)
        if span.attributes
        and span.attributes.get("conductor.gate.event") in {"gate_presented", "gate_resolved"}
    ]
    assert len(gate_spans) == 2
    assert {span.start_time == span.end_time for span in gate_spans} == {True}
    attributes = [span.attributes for span in gate_spans]
    assert all(attributes)
    assert {attribute[GEN_AI_CONVERSATION_ID] for attribute in attributes if attribute} == {"run-4"}
    step_types = {attribute[CONDUCTOR_STEP_TYPE] for attribute in attributes if attribute}
    assert step_types == {"human_gate"}


def test_close_ends_open_spans_and_resets_telemetry_context(
    tracing: Tracing,
) -> None:
    """Requirement: final cleanup ends unfinished spans and drops process-local state."""
    # Given: a tracer context and spans still open at CLI teardown.
    subscriber = tracing.subscriber
    guards.set_current_tracer_provider(tracing.provider)
    guards.set_current_run_id("run-5")
    subscriber.on_event(_event("workflow_started", 10.0, name="teardown", run_id="run-5"))
    subscriber.on_event(_event("agent_started", 11.0, agent_name="open", iteration=1))

    # When: the CLI's finally block closes the subscriber.
    subscriber.close()

    # Then: all spans have the default status and no latched run state remains.
    from opentelemetry.trace import StatusCode

    assert {span.status.status_code for span in _spans(tracing.exporter)} == {StatusCode.UNSET}
    assert subscriber._open_spans == {}
    assert guards.current_tracer_provider() is None
    assert guards.current_run_id() is None


def test_event_timestamps_are_converted_from_seconds_to_nanoseconds(
    tracing: Tracing,
) -> None:
    """Requirement: detached spans use event timestamps instead of subscriber wall time."""
    # Given: synthetic timestamps far from the test process's wall clock.
    subscriber = tracing.subscriber

    # When: an agent lifecycle is emitted with Unix-second values.
    subscriber.on_event(_event("workflow_started", 10_000.25, name="time", run_id="run-6"))
    subscriber.on_event(_event("agent_started", 10_001.5, agent_name="clock", iteration=1))
    subscriber.on_event(_event("agent_completed", 10_003.75, agent_name="clock"))
    subscriber.on_event(_event("workflow_completed", 10_004.0))

    # Then: OpenTelemetry receives integer nanoseconds derived from those values.
    agent = _span(_spans(tracing.exporter), f"{INVOKE_AGENT} clock")
    assert agent.start_time == 10_001_500_000_000
    assert agent.end_time == 10_003_750_000_000


def test_repeated_unnamed_tool_calls_close_in_fifo_order(
    tracing: Tracing,
) -> None:
    """Requirement: same-named tools without call IDs remain distinct span instances."""
    # Given: two overlapping calls from a provider that omits call identifiers.
    subscriber = tracing.subscriber
    subscriber.on_event(_event("workflow_started", 10.0, name="tools", run_id="run-7"))
    subscriber.on_event(_event("agent_started", 11.0, agent_name="worker", iteration=1))
    subscriber.on_event(_event("agent_tool_start", 12.0, agent_name="worker", tool_name="lookup"))
    subscriber.on_event(_event("agent_tool_start", 13.0, agent_name="worker", tool_name="lookup"))

    # When: matching completions arrive without IDs in their original order.
    subscriber.on_event(
        _event("agent_tool_complete", 14.0, agent_name="worker", tool_name="lookup")
    )
    subscriber.on_event(
        _event("agent_tool_complete", 15.0, agent_name="worker", tool_name="lookup")
    )
    subscriber.on_event(_event("agent_completed", 16.0, agent_name="worker"))
    subscriber.on_event(_event("workflow_completed", 17.0))

    # Then: each call produces a separately ended span rather than overwriting the first.
    tools = [span for span in _spans(tracing.exporter) if span.name == "execute_tool lookup"]
    assert len(tools) == 2
    span_ids = {span.context.span_id for span in tools if span.context is not None}
    assert len(span_ids) == 2


def test_same_tool_call_id_from_parallel_agents_closes_matching_spans(
    tracing: Tracing,
) -> None:
    """Requirement: tool call IDs are isolated by their concurrent agent parent."""
    # Given: two parallel agents whose providers independently emit the same call ID.
    subscriber = tracing.subscriber
    subscriber.on_event(_event("workflow_started", 10.0, name="tools", run_id="run-8"))
    subscriber.on_event(_event("parallel_started", 11.0, group_name="workers"))
    subscriber.on_event(
        _event("parallel_agent_started", 12.0, group_name="workers", agent_name="alpha")
    )
    subscriber.on_event(
        _event("parallel_agent_started", 13.0, group_name="workers", agent_name="beta")
    )
    subscriber.on_event(
        _event(
            "agent_tool_start",
            14.0,
            agent_name="alpha",
            tool_name="lookup",
            tool_call_id="shared-call",
        )
    )
    subscriber.on_event(
        _event(
            "agent_tool_start",
            15.0,
            agent_name="beta",
            tool_name="lookup",
            tool_call_id="shared-call",
        )
    )

    # When: both completions carry the shared call ID in the opposite start order.
    subscriber.on_event(
        _event(
            "agent_tool_complete",
            16.0,
            agent_name="beta",
            tool_name="lookup",
            tool_call_id="shared-call",
        )
    )
    subscriber.on_event(
        _event(
            "agent_tool_complete",
            17.0,
            agent_name="alpha",
            tool_name="lookup",
            tool_call_id="shared-call",
        )
    )
    subscriber.on_event(
        _event("parallel_agent_completed", 18.0, group_name="workers", agent_name="alpha")
    )
    subscriber.on_event(
        _event("parallel_agent_completed", 19.0, group_name="workers", agent_name="beta")
    )
    subscriber.on_event(_event("parallel_completed", 20.0, group_name="workers"))
    subscriber.on_event(_event("workflow_completed", 21.0))

    # Then: each tool span remains parented and ended by its own agent event.
    spans = _spans(tracing.exporter)
    alpha = _span(spans, f"{INVOKE_AGENT} alpha")
    beta = _span(spans, f"{INVOKE_AGENT} beta")
    assert alpha.context is not None
    assert beta.context is not None
    tools_by_parent = {
        span.parent.span_id: span
        for span in spans
        if span.name == "execute_tool lookup" and span.parent is not None
    }
    alpha_tool = tools_by_parent[alpha.context.span_id]
    beta_tool = tools_by_parent[beta.context.span_id]
    assert len(tools_by_parent) == 2
    assert alpha_tool.end_time == 17_000_000_000
    assert beta_tool.end_time == 16_000_000_000


def test_close_attempts_shutdown_when_force_flush_raises(
    tracing: Tracing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement: a flush failure cannot skip tracer-provider shutdown."""
    # Given: a tracer provider whose flush fails during non-throwing cleanup.
    force_flush = Mock(side_effect=RuntimeError("flush failed"))
    shutdown = Mock()
    monkeypatch.setattr(tracing.provider, "force_flush", force_flush)
    monkeypatch.setattr(tracing.provider, "shutdown", shutdown)

    # When: the telemetry subscriber closes.
    tracing.subscriber.close()

    # Then: shutdown is attempted and the cleanup error remains suppressed.
    force_flush.assert_called_once_with(timeout_millis=5_000)
    shutdown.assert_called_once_with()


def test_child_workflow_failure_does_not_close_the_parent_run(
    tracing: Tracing,
) -> None:
    """Requirement: a child workflow failure leaves its parent hierarchy open."""
    # Given: a parent agent that delegates to one nested workflow.
    subscriber = tracing.subscriber
    subscriber.on_event(_event("workflow_started", 10.0, name="parent", run_id="run-8"))
    subscriber.on_event(_event("agent_started", 11.0, agent_name="delegate", iteration=1))
    subscriber.on_event(
        _event(
            "subworkflow_started",
            12.0,
            agent_name="delegate",
            parent_path=[],
            slot_key="delegate",
        )
    )
    subscriber.on_event(
        _event(
            "workflow_started",
            13.0,
            name="child",
            subworkflow_path=["delegate"],
        )
    )
    subscriber.on_event(
        _event(
            "agent_started",
            14.0,
            agent_name="nested",
            iteration=1,
            subworkflow_path=["delegate"],
        )
    )

    # When: only the nested engine reports a terminal failure.
    subscriber.on_event(
        _event(
            "workflow_failed",
            15.0,
            subworkflow_path=["delegate"],
            error_type="ProviderError",
        )
    )

    # Then: the root and delegating agent remain open for their outer terminal events.
    assert len(subscriber._open_spans) == 2
    subscriber.on_event(
        _event(
            "subworkflow_failed",
            16.0,
            agent_name="delegate",
            parent_path=[],
            slot_key="delegate",
            error_type="ProviderError",
        )
    )
    subscriber.on_event(
        _event("workflow_failed", 17.0, error_type="ProviderError", message="child failed")
    )

    root = _span(_spans(tracing.exporter), f"{INVOKE_WORKFLOW} parent")
    assert root.status.status_code.name == "ERROR"
    assert subscriber._open_spans == {}
