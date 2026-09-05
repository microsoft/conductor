"""Requirements tests for native Pydantic AI telemetry instrumentation."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import pytest
from pydantic_ai import Agent, InstrumentationSettings
from pydantic_ai.models.test import TestModel

from conductor.config.schema import AgentDef
from conductor.providers._pydantic_ai.agent_builder import build_agent
from conductor.providers._pydantic_ai.interrupt import run_with_interrupt
from conductor.telemetry import guards
from conductor.telemetry.setup import init_tracer_provider

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider


class SpanExporter(Protocol):
    def get_finished_spans(self) -> tuple[ReadableSpan, ...]: ...


@dataclass(frozen=True, slots=True)
class NativeTracing:
    provider: TracerProvider
    exporter: SpanExporter


@pytest.fixture(autouse=True)
def reset_telemetry_context(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Keep per-process telemetry state isolated between requirements."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    guards.reset_telemetry_context()
    yield
    guards.reset_telemetry_context()


@pytest.fixture
def native_tracing(monkeypatch: pytest.MonkeyPatch) -> Generator[NativeTracing, Any, Any]:
    """Create and latch an in-memory provider for one telemetry-enabled run."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    exporter = InMemorySpanExporter()
    provider = init_tracer_provider(run_id="run-native")
    assert provider is not None
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    guards.set_current_tracer_provider(provider)
    yield NativeTracing(provider=provider, exporter=exporter)
    provider.shutdown()


def _agent_definition(name: str) -> AgentDef:
    return AgentDef(
        name=name,
        max_depth=None,
        timeout_seconds=None,
        max_session_seconds=None,
        max_agent_iterations=None,
    )


def test_active_telemetry_configures_pydantic_ai_without_message_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement: native tracing uses Conductor's provider and hides content by default."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    provider = init_tracer_provider(run_id="run-telemetry")
    assert provider is not None
    guards.set_current_tracer_provider(provider)

    agent = build_agent(_agent_definition("writer"), system_prompt="", rendered_prompt="")

    assert isinstance(agent.instrument, InstrumentationSettings)
    assert agent.instrument.tracer is not None
    assert agent.instrument.include_content is False

    provider.shutdown()


@pytest.mark.parametrize("capture_mode", ["true", "SPAN_ONLY", "SPAN_AND_EVENT"])
def test_content_capture_modes_enable_pydantic_ai_message_content(
    monkeypatch: pytest.MonkeyPatch, capture_mode: str
) -> None:
    """Requirement: explicit span-content modes are the only opt-in for message capture."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", capture_mode)
    provider = init_tracer_provider(run_id="run-telemetry")
    assert provider is not None
    guards.set_current_tracer_provider(provider)

    agent = build_agent(_agent_definition("writer"), system_prompt="", rendered_prompt="")

    assert isinstance(agent.instrument, InstrumentationSettings)
    assert agent.instrument.include_content is True

    provider.shutdown()


def test_event_only_capture_mode_hides_pydantic_ai_message_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement: EVENT_ONLY degrades safely because Pydantic AI has no event-only mode."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "EVENT_ONLY")
    provider = init_tracer_provider(run_id="run-telemetry")
    assert provider is not None
    guards.set_current_tracer_provider(provider)

    agent = build_agent(_agent_definition("writer"), system_prompt="", rendered_prompt="")

    assert isinstance(agent.instrument, InstrumentationSettings)
    assert agent.instrument.include_content is False

    provider.shutdown()


def test_inactive_telemetry_leaves_pydantic_ai_uninstrumented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement: workflows without telemetry preserve Pydantic AI defaults."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    guards.reset_telemetry_context()

    agent = build_agent(_agent_definition("writer"), system_prompt="", rendered_prompt="")

    assert agent.instrument is None


@pytest.mark.asyncio
async def test_native_spans_receive_the_active_conductor_run_identifier(
    native_tracing: NativeTracing,
) -> None:
    """Requirement: explicit conversation_id correlates every native span to the workflow run."""
    agent = Agent(TestModel())
    assert native_tracing.provider is not None
    agent.instrument = InstrumentationSettings(
        tracer_provider=native_tracing.provider, include_content=False
    )

    await run_with_interrupt(
        agent,
        "answer briefly",
        interrupt_signal=None,
        event_callback=None,
        has_output_schema=False,
    )

    spans = native_tracing.exporter.get_finished_spans()
    assert spans
    span_attributes = [span.attributes for span in spans]
    assert all(attributes is not None for attributes in span_attributes)
    assert {
        attributes.get("gen_ai.conversation.id")
        for attributes in span_attributes
        if attributes is not None
    } == {"run-native"}


@pytest.mark.asyncio
async def test_native_spans_share_trace_and_parent_with_conductor_agent(
    native_tracing: NativeTracing,
) -> None:
    """Requirement: native Pydantic AI spans form one tree under the active run.

    The conductor-side ``invoke_agent`` span and Pydantic AI's native ``chat``
    spans must share one trace_id, and every native span must parent to the
    conductor agent span that was current when it started.
    """
    from opentelemetry import context as otel_context
    from opentelemetry import trace

    assert native_tracing.provider is not None
    orchestration_tracer = native_tracing.provider.get_tracer("conductor.telemetry")
    agent_span = orchestration_tracer.start_span("invoke_agent writer")
    ctx = trace.set_span_in_context(agent_span)
    attach_token = otel_context.attach(ctx)

    pydantic_agent = Agent(TestModel())
    pydantic_agent.instrument = InstrumentationSettings(
        tracer_provider=native_tracing.provider, include_content=False
    )
    await run_with_interrupt(
        pydantic_agent,
        "answer briefly",
        interrupt_signal=None,
        event_callback=None,
        has_output_schema=False,
    )

    otel_context.detach(attach_token)
    agent_span.end()

    spans = native_tracing.exporter.get_finished_spans()
    assert spans
    agent_finished = [s for s in spans if s.name == "invoke_agent writer"]
    native_chat = [s for s in spans if s.name.startswith("chat ")]
    assert len(agent_finished) == 1
    assert native_chat
    assert all(s.parent is not None for s in native_chat)

    agent_context = agent_finished[0].get_span_context()
    assert agent_context is not None
    assert agent_context.is_valid

    # Pydantic AI creates its own ``invoke_agent`` wrapper span as a child of
    # the conductor span; the LLM ``chat`` spans then parent to that wrapper.
    native_invoke = [
        s for s in spans if s.name.startswith("invoke_agent ") and s is not agent_finished[0]
    ]
    assert len(native_invoke) == 1
    wrapper = native_invoke[0]
    assert wrapper.parent is not None
    assert wrapper.parent.span_id == agent_context.span_id
    assert all(s.parent.span_id == wrapper.get_span_context().span_id for s in native_chat)  # type: ignore[union-attr]
    assert all(
        s.get_span_context().trace_id == agent_context.trace_id  # type: ignore[union-attr]
        for s in spans
    )
