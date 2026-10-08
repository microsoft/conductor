"""Realm identity on the existing agent spans."""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING, Literal

import pytest

from conductor.events import WorkflowEvent
from conductor.telemetry.subscriber import TelemetrySubscriber

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def tracing() -> Generator[tuple[TelemetrySubscriber, InMemorySpanExporter]]:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    subscriber = TelemetrySubscriber(provider)
    yield subscriber, exporter
    subscriber.close()


def _event(kind: str, timestamp: float, **data: object) -> WorkflowEvent:
    return WorkflowEvent(type=kind, timestamp=timestamp, data=data)


@pytest.mark.parametrize(
    ("started", "completed", "prefix", "suffix", "fields", "span_name"),
    [
        (
            "agent_started",
            "agent_completed",
            (),
            (),
            {"agent_name": "writer"},
            "invoke_agent writer",
        ),
        (
            "parallel_agent_started",
            "parallel_agent_completed",
            (("parallel_started", {"group_name": "team"}),),
            (("parallel_completed", {"group_name": "team"}),),
            {"group_name": "team", "agent_name": "writer"},
            "invoke_agent writer",
        ),
        (
            "for_each_agent_started",
            "for_each_item_completed",
            (
                ("for_each_started", {"group_name": "team"}),
                ("for_each_item_started", {"group_name": "team", "item_key": "0", "index": 0}),
            ),
            (("for_each_completed", {"group_name": "team"}),),
            {"group_name": "team", "item_key": "0", "index": 0, "agent_name": "writer[0]"},
            "invoke_agent team[0]",
        ),
    ],
    ids=["sequential", "parallel", "for_each"],
)
@pytest.mark.parametrize("source", ["started", "completed"])
@pytest.mark.parametrize("remote", [True, False], ids=["remote", "local"])
def test_agent_span_realm_identity(
    tracing: tuple[TelemetrySubscriber, InMemorySpanExporter],
    started: str,
    completed: str,
    prefix: tuple[tuple[str, dict[str, object]], ...],
    suffix: tuple[tuple[str, dict[str, object]], ...],
    fields: dict[str, object],
    span_name: str,
    source: Literal["started", "completed"],
    remote: bool,
) -> None:
    # Requirement: only remote agent spans carry realm identity, even if it arrives at completion.
    subscriber, exporter = tracing
    identity: dict[str, object] = {
        "execution_backend": "docker",
        "realm_image": "runner:test",
    }
    start_data = identity if remote and source == "started" else {}
    finish_data = identity if remote and source == "completed" else {}
    if not remote and source == "started":
        start_data = {"execution_backend": None, "realm_image": None}

    subscriber.on_event(_event("workflow_started", 10.0, name="test", run_id="run-realm"))
    for event_type, data in prefix:
        subscriber.on_event(_event(event_type, 11.0, **data))
    subscriber.on_event(_event(started, 12.0, **fields, **start_data))
    subscriber.on_event(_event(completed, 13.0, **fields, **finish_data))
    for event_type, data in suffix:
        subscriber.on_event(_event(event_type, 14.0, **data))
    subscriber.on_event(_event("workflow_completed", 15.0))

    spans = [span for span in exporter.get_finished_spans() if span.name == span_name]
    assert len(spans) == 1
    attributes = spans[0].attributes
    assert attributes is not None
    if remote:
        assert attributes["conductor.execution.backend"] == "docker"
        assert attributes["conductor.realm.image"] == "runner:test"
    else:
        assert "conductor.execution.backend" not in attributes
        assert "conductor.realm.image" not in attributes
