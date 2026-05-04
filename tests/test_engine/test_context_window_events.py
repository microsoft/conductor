"""Tests that workflow events include context_window fields.

Context-window metadata is now sourced from each provider's SDK at runtime
(``AgentProvider.get_max_prompt_tokens``). In mock-handler mode the Copilot
provider has no SDK to query and returns ``None`` by default, so these tests
monkeypatch the provider method to inject the values being asserted.
"""

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


def _provider_with_max_prompt(values: dict[str, int | None]) -> CopilotProvider:
    """Build a mock-handler Copilot provider whose ``get_max_prompt_tokens``
    returns values from ``values`` (or ``None`` for unknown models)."""
    provider = CopilotProvider(mock_handler=lambda a, p, c: {"answer": "hi", "result": a.name})

    async def fake_get_max_prompt_tokens(model: str) -> int | None:
        return values.get(model)

    provider.get_max_prompt_tokens = fake_get_max_prompt_tokens  # type: ignore[method-assign]
    return provider


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
        provider = _provider_with_max_prompt({"gpt-4o": 128000})
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
        provider = _provider_with_max_prompt({"gpt-4o": 128000})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        event = collector.first("agent_completed")
        assert "context_window_used" in event.data
        assert "context_window_max" in event.data
        assert event.data["context_window_max"] == 128000


class TestContextWindowNoneForUnknownModel:
    """context_window_max is None when the provider has no metadata for the model."""

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
        # Empty metadata table — every lookup returns None.
        provider = _provider_with_max_prompt({})
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
        provider = _provider_with_max_prompt({"gpt-4o": 128000})
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
        provider = _provider_with_max_prompt({})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        events = collector.of_type("parallel_agent_completed")
        for event in events:
            assert event.data["context_window_max"] is None


class TestContextWindowResolutionOrder:
    """The model is resolved from output.model first, then agent.model, then default."""

    @pytest.mark.asyncio
    async def test_default_model_used_when_agent_has_no_model(self) -> None:
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-default",
                entry_point="a1",
                runtime=RuntimeConfig(provider="copilot", default_model="gpt-4o"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="a1",
                    prompt="Hello",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"answer": "{{ a1.output.answer }}"},
        )
        provider = _provider_with_max_prompt({"gpt-4o": 128000})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        # agent_started has no output yet, but should resolve via default_model.
        assert collector.first("agent_started").data["context_window_max"] == 128000

    @pytest.mark.asyncio
    async def test_output_model_preferred_over_configured(self) -> None:
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-output-model",
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

        # Mock handler reports a different model than the agent requested
        # (e.g. the SDK aliased or substituted it).
        def handler(agent, prompt, context):  # type: ignore[no-untyped-def]
            return {"answer": "hi"}

        provider = CopilotProvider(mock_handler=handler)

        async def fake_get_max_prompt_tokens(model: str) -> int | None:
            return {"gpt-4o": 128000, "gpt-5.2": 400000}.get(model)

        provider.get_max_prompt_tokens = fake_get_max_prompt_tokens  # type: ignore[method-assign]

        # Force the AgentOutput.model field via a wrapper. The simplest hook
        # here is to set the model on the mock-execute return — done by
        # patching the provider's execute to override output.model.
        original_execute = provider.execute

        async def execute_with_model(*args, **kwargs):  # type: ignore[no-untyped-def]
            output = await original_execute(*args, **kwargs)
            output.model = "gpt-5.2"
            return output

        provider.execute = execute_with_model  # type: ignore[method-assign]

        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        # agent_started runs before execution, no output yet — uses agent.model
        assert collector.first("agent_started").data["context_window_max"] == 128000
        # agent_completed has output.model — uses that
        assert collector.first("agent_completed").data["context_window_max"] == 400000

    @pytest.mark.asyncio
    async def test_falls_back_to_agent_model_when_output_model_unknown(self) -> None:
        """If output.model is an SDK-unknown variant (e.g. a reasoning-effort
        tier the provider doesn't list), the chain retries with agent.model
        rather than returning None."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-fallback",
                entry_point="a1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="a1",
                    model="claude-opus-4.7",
                    prompt="Hello",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"answer": "{{ a1.output.answer }}"},
        )

        provider = CopilotProvider(mock_handler=lambda a, p, c: {"answer": "hi"})

        # Provider only knows the base name, not the reasoning-effort variant.
        async def fake_get_max_prompt_tokens(model: str) -> int | None:
            return {"claude-opus-4.7": 200_000}.get(model)

        provider.get_max_prompt_tokens = fake_get_max_prompt_tokens  # type: ignore[method-assign]

        original_execute = provider.execute

        async def execute_with_variant_model(*args, **kwargs):  # type: ignore[no-untyped-def]
            output = await original_execute(*args, **kwargs)
            output.model = "claude-opus-4.7-xhigh"  # SDK doesn't know this name
            return output

        provider.execute = execute_with_variant_model  # type: ignore[method-assign]

        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        # output.model returned None; chain fell back to agent.model.
        assert collector.first("agent_completed").data["context_window_max"] == 200_000
