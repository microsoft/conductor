"""Tests that workflow events include context_window fields."""

from __future__ import annotations

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    LimitsConfig,
    OutputField,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.providers.copilot import CopilotProvider


class EventCollector:
    """Helper to collect events emitted by a WorkflowEventEmitter."""

    def __init__(self) -> None:
        self.events: list[WorkflowEvent] = []

    def __call__(self, event: WorkflowEvent) -> None:
        self.events.append(event)

    def of_type(self, event_type: str) -> list[WorkflowEvent]:
        return [e for e in self.events if e.type == event_type]

    def first(self, event_type: str) -> WorkflowEvent:
        matches = self.of_type(event_type)
        assert matches, f"No event of type {event_type!r} found"
        return matches[0]


def _make_emitter_and_collector() -> tuple[WorkflowEventEmitter, EventCollector]:
    emitter = WorkflowEventEmitter()
    collector = EventCollector()
    emitter.subscribe(collector)
    return emitter, collector


class TestAgentStartedContextWindow:
    """agent_started event includes context_window_max."""

    @pytest.mark.asyncio
    async def test_agent_started_has_context_window_max(self) -> None:
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="a1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="a1",
                    model="gpt-4o",
                    prompt="Hello",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"answer": "{{ a1.output.answer }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"answer": "hi"})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        event = collector.first("agent_started")
        assert "context_window_max" in event.data
        assert event.data["context_window_max"] == 128000


class TestAgentCompletedContextWindow:
    """agent_completed event includes context_window_used and context_window_max."""

    @pytest.mark.asyncio
    async def test_agent_completed_has_context_window_fields(self) -> None:
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="a1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="a1",
                    model="gpt-4o",
                    prompt="Hello",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"answer": "{{ a1.output.answer }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"answer": "hi"})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        event = collector.first("agent_completed")
        assert "context_window_used" in event.data
        assert "context_window_max" in event.data
        assert event.data["context_window_max"] == 128000


class TestContextWindowNoneForUnknownModel:
    """context_window_max is None when model is unknown."""

    @pytest.mark.asyncio
    async def test_unknown_model_returns_none(self) -> None:
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="a1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="a1",
                    model="unknown-exotic-model",
                    prompt="Hello",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"answer": "{{ a1.output.answer }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"answer": "hi"})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        event = collector.first("agent_started")
        assert event.data["context_window_max"] is None


class TestParallelAgentContextWindow:
    """parallel_agent_completed event includes context_window_used and context_window_max."""

    @pytest.mark.asyncio
    async def test_parallel_agent_completed_has_context_window_fields(self) -> None:
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-parallel-ctx",
                entry_point="team",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="r1",
                    model="gpt-4o",
                    prompt="research 1",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="r2",
                    model="gpt-4o",
                    prompt="research 2",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="team",
                    agents=["r1", "r2"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "done"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": a.name})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        events = collector.of_type("parallel_agent_completed")
        assert len(events) == 2
        for event in events:
            assert "context_window_used" in event.data
            assert "context_window_max" in event.data
            assert event.data["context_window_max"] == 128000

    @pytest.mark.asyncio
    async def test_parallel_agent_unknown_model_context_window_none(self) -> None:
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-parallel-unknown",
                entry_point="team",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="r1",
                    model="exotic-model-x",
                    prompt="research",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="r2",
                    model="exotic-model-x",
                    prompt="research 2",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="team",
                    agents=["r1", "r2"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "done"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        events = collector.of_type("parallel_agent_completed")
        for event in events:
            assert event.data["context_window_max"] is None
