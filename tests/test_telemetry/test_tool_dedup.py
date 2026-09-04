from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import pytest
from pydantic_ai import Agent, InstrumentationSettings
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import Tool

from conductor.cli.run import _apply_provider_override
from conductor.config.schema import WorkflowConfig
from conductor.events import WorkflowEvent
from conductor.providers._pydantic_ai.interrupt import run_with_interrupt
from conductor.telemetry.subscriber import TelemetrySubscriber

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider


class SpanExporter(Protocol):
    def get_finished_spans(self) -> tuple[ReadableSpan, ...]: ...


@dataclass(frozen=True, slots=True)
class Tracing:
    subscriber: TelemetrySubscriber
    exporter: SpanExporter
    provider: TracerProvider


@pytest.fixture
def tracing() -> Generator[Tracing]:
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
    return WorkflowEvent(type=event_type, timestamp=timestamp, data=data)


def _tool_spans(tracing: Tracing) -> list[ReadableSpan]:
    return [
        span
        for span in tracing.exporter.get_finished_spans()
        if span.name.startswith("execute_tool ")
        and span.instrumentation_scope is not None
        and span.instrumentation_scope.name == "conductor.telemetry"
    ]


def _emit_tool_lifecycle(
    subscriber: TelemetrySubscriber,
    *,
    timestamp: float,
    agent_name: str,
    tool_name: str = "echo",
    tool_call_id: str = "call-1",
    **data: object,
) -> None:
    subscriber.on_event(
        _event(
            "agent_tool_start",
            timestamp,
            agent_name=agent_name,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            **data,
        )
    )
    subscriber.on_event(
        _event(
            "agent_tool_complete",
            timestamp + 1,
            agent_name=agent_name,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            **data,
        )
    )


@pytest.mark.asyncio
async def test_native_provider_suppresses_conductor_tool_span_but_keeps_test_model_span(
    tracing: Tracing,
) -> None:
    # Given: an OpenAI agent span and an instrumented TestModel that invokes one tool.
    subscriber = tracing.subscriber
    subscriber.on_event(_event("workflow_started", 1.0, name="native", run_id="run-native"))
    subscriber.on_event(_event("agent_started", 2.0, agent_name="worker", provider="openai"))
    agent = Agent(
        TestModel(call_tools=["echo"]),
        tools=[Tool(lambda value: value, name="echo")],
        name="worker",
        retries=0,
    )
    agent.instrument = InstrumentationSettings(tracer_provider=tracing.provider)

    # When: Pydantic AI executes its native tool lifecycle through the subscriber.
    await run_with_interrupt(
        agent,
        "echo a value",
        interrupt_signal=None,
        event_callback=lambda event_type, data: subscriber.on_event(
            _event(event_type, 3.0, **data)
        ),
        has_output_schema=False,
    )
    subscriber.on_event(_event("agent_completed", 6.0, agent_name="worker"))
    subscriber.on_event(_event("workflow_completed", 7.0))

    # Then: Pydantic AI has a native tool span and Conductor contributes no duplicate.
    span_names = [span.name for span in tracing.exporter.get_finished_spans()]
    assert any("tool" in name.lower() for name in span_names)
    assert _tool_spans(tracing) == []


@pytest.mark.parametrize(
    ("provider", "native_otel_spans_active", "expected_tool_spans"),
    [
        pytest.param("fake-provider", True, (), id="event-true"),
        pytest.param("openai", False, ("execute_tool echo",), id="event-false"),
        pytest.param("openai", None, (), id="static-native"),
        pytest.param("fake-provider", None, ("execute_tool echo",), id="static-fake"),
        # Field-absent Copilot events use static capability; when it becomes true,
        # suppressing the Conductor span is the intentional legacy behavior.
        pytest.param("copilot", None, (), id="legacy-copilot"),
    ],
)
def test_tool_dedup_uses_event_marker_or_static_provider_capability(
    tracing: Tracing,
    provider: str,
    native_otel_spans_active: bool | None,
    expected_tool_spans: tuple[str, ...],
) -> None:
    # Requirement: a real event bool wins; absent fields use static provider capability.
    # Given: a provider-backed agent with an explicit marker or a legacy absent marker.
    subscriber = tracing.subscriber
    subscriber.on_event(_event("workflow_started", 1.0, name="dedup", run_id="run-dedup"))
    subscriber.on_event(
        _event(
            "agent_started",
            2.0,
            agent_name="worker",
            provider=provider,
            **(
                {"native_otel_spans_active": native_otel_spans_active}
                if native_otel_spans_active is not None
                else {}
            ),
        )
    )

    # When: the provider emits one tool lifecycle.
    _emit_tool_lifecycle(subscriber, timestamp=3.0, agent_name="worker")
    subscriber.on_event(_event("agent_completed", 5.0, agent_name="worker"))
    subscriber.on_event(_event("workflow_completed", 6.0))

    # Then: conductor spans match the event override or static fallback decision.
    assert tuple(span.name for span in _tool_spans(tracing)) == expected_tool_spans


def test_mixed_workflow_deduplicates_only_native_provider_tools(tracing: Tracing) -> None:
    # Given: one native and one non-native agent in the same workflow.
    subscriber = tracing.subscriber
    subscriber.on_event(_event("workflow_started", 1.0, name="mixed", run_id="run-mixed"))
    subscriber.on_event(_event("agent_started", 2.0, agent_name="native", provider="openai"))
    _emit_tool_lifecycle(subscriber, timestamp=3.0, agent_name="native", tool_call_id="native-call")
    subscriber.on_event(_event("agent_completed", 5.0, agent_name="native"))
    subscriber.on_event(
        _event("agent_started", 6.0, agent_name="fallback", provider="fake-provider")
    )

    # When: the non-native agent invokes the same tool.
    _emit_tool_lifecycle(
        subscriber,
        timestamp=7.0,
        agent_name="fallback",
        tool_call_id="fallback-call",
    )
    subscriber.on_event(_event("agent_completed", 9.0, agent_name="fallback"))
    subscriber.on_event(_event("workflow_completed", 10.0))

    # Then: only the non-native lifecycle creates a Conductor tool span.
    assert [span.name for span in _tool_spans(tracing)] == ["execute_tool echo"]


def test_provider_from_cli_override_suppresses_conductor_tool_spans(tracing: Tracing) -> None:
    # Given: CLI overrides a workflow's configured non-native provider after subscriber creation.
    subscriber = tracing.subscriber
    config = WorkflowConfig.model_validate(
        {
            "workflow": {
                "name": "override",
                "entry_point": "worker",
                "runtime": {"provider": "hermes"},
            },
            "agents": [{"name": "worker", "prompt": "Work"}],
        }
    )
    _apply_provider_override(config, "openai")
    subscriber.on_event(_event("workflow_started", 1.0, name="override", run_id="run-override"))
    subscriber.on_event(
        _event(
            "agent_started",
            2.0,
            agent_name="worker",
            provider=config.workflow.runtime.provider.name,
        )
    )

    # When: the overridden agent emits a tool lifecycle.
    _emit_tool_lifecycle(subscriber, timestamp=3.0, agent_name="worker")
    subscriber.on_event(_event("agent_completed", 5.0, agent_name="worker"))
    subscriber.on_event(_event("workflow_completed", 6.0))

    # Then: no precomputed workflow-provider set can reintroduce the duplicate.
    assert _tool_spans(tracing) == []


def test_for_each_inline_provider_uses_item_parent_not_group_agent_name(tracing: Tracing) -> None:
    # Given: an inline OpenAI agent whose tool events identify only the for-each group.
    subscriber = tracing.subscriber
    subscriber.on_event(_event("workflow_started", 1.0, name="loop", run_id="run-loop"))
    subscriber.on_event(_event("for_each_started", 2.0, group_name="workers"))
    subscriber.on_event(
        _event(
            "for_each_item_started",
            3.0,
            group_name="workers",
            item_key="first",
            index=0,
        )
    )
    subscriber.on_event(
        _event(
            "for_each_agent_started",
            4.0,
            group_name="workers",
            agent_name="inline-openai",
            item_key="first",
            index=0,
            provider="openai",
        )
    )

    # When: the inline agent emits tool events keyed by the group name, as the engine does.
    _emit_tool_lifecycle(
        subscriber,
        timestamp=5.0,
        agent_name="workers",
        item_key="first",
        index=0,
    )
    subscriber.on_event(
        _event(
            "for_each_item_completed",
            7.0,
            group_name="workers",
            item_key="first",
            index=0,
        )
    )
    subscriber.on_event(_event("for_each_completed", 8.0, group_name="workers"))
    subscriber.on_event(_event("workflow_completed", 9.0))

    # Then: provider lookup follows the item parent identity instead of (path, agent_name).
    assert _tool_spans(tracing) == []


def test_legacy_tool_events_without_provider_keep_conductor_spans(tracing: Tracing) -> None:
    # Given: a historical agent-start event with no provider field.
    subscriber = tracing.subscriber
    subscriber.on_event(_event("workflow_started", 1.0, name="legacy", run_id="run-legacy"))
    subscriber.on_event(_event("agent_started", 2.0, agent_name="worker"))

    # When: its provider emits a legacy tool lifecycle.
    _emit_tool_lifecycle(subscriber, timestamp=3.0, agent_name="worker")
    subscriber.on_event(_event("agent_completed", 5.0, agent_name="worker"))
    subscriber.on_event(_event("workflow_completed", 6.0))

    # Then: Conductor safely retains the tool span rather than dropping telemetry.
    assert [span.name for span in _tool_spans(tracing)] == ["execute_tool echo"]
