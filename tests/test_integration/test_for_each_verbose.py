"""Test event emission for for-each execution.

This module tests that workflow events are emitted correctly
during for-each execution, which drives console output via
the ConsoleEventSubscriber.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    LimitsConfig,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.providers.base import AgentOutput


class TestForEachEventEmission:
    """Tests for event emission during for-each execution."""

    @pytest.mark.asyncio
    async def test_events_emitted_for_each_execution(self):
        """Test that correct events are emitted during for-each execution."""
        # Create a minimal workflow with for-each
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="event-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "max_concurrent": 2,
                        "failure_mode": "continue_on_error",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "$end"}],
                    }
                ),
            ],
            output={
                "count": "{{ processors.count }}",
            },
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: finder returns 3 items, all process successfully
        provider.execute.side_effect = [
            AgentOutput(
                content={"items": ["A", "B", "C"]},
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(content={"result": "ok-A"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(content={"result": "ok-B"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(content={"result": "ok-C"}, raw_response={}, model="gpt-4", tokens_used=10),
        ]

        # Capture emitted events
        emitter = WorkflowEventEmitter()
        events: list[WorkflowEvent] = []
        emitter.subscribe(lambda e: events.append(e))

        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        result = await engine.run({})

        # Verify for_each_started was emitted
        started = [e for e in events if e.type == "for_each_started"]
        assert len(started) == 1
        assert started[0].data["group_name"] == "processors"
        assert started[0].data["item_count"] == 3
        assert started[0].data["max_concurrent"] == 2
        assert started[0].data["failure_mode"] == "continue_on_error"

        # Verify for_each_item_completed was emitted for each item
        completed = [e for e in events if e.type == "for_each_item_completed"]
        assert len(completed) == 3
        item_keys = {e.data["item_key"] for e in completed}
        assert item_keys == {"0", "1", "2"}

        # Verify no item failures
        failed = [e for e in events if e.type == "for_each_item_failed"]
        assert len(failed) == 0

        # Verify for_each_completed summary
        summary = [e for e in events if e.type == "for_each_completed"]
        assert len(summary) == 1
        assert summary[0].data["group_name"] == "processors"
        assert summary[0].data["success_count"] == 3
        assert summary[0].data["failure_count"] == 0

        # Verify the workflow completed successfully
        assert result["count"] == 3

    @pytest.mark.asyncio
    async def test_events_emitted_for_each_failures(self):
        """Test event emission when items fail."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="event-fail-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "max_concurrent": 10,
                        "failure_mode": "continue_on_error",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "$end"}],
                    }
                ),
            ],
            output={
                "count": "{{ processors.count }}",
            },
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: finder returns 3 items, second one fails
        provider.execute.side_effect = [
            AgentOutput(
                content={"items": ["A", "B", "C"]},
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(content={"result": "ok-A"}, raw_response={}, model="gpt-4", tokens_used=10),
            Exception("Processing failed"),
            AgentOutput(content={"result": "ok-C"}, raw_response={}, model="gpt-4", tokens_used=10),
        ]

        # Capture emitted events
        emitter = WorkflowEventEmitter()
        events: list[WorkflowEvent] = []
        emitter.subscribe(lambda e: events.append(e))

        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        result = await engine.run({})

        # Verify for_each_item_completed was emitted twice (for A and C)
        completed = [e for e in events if e.type == "for_each_item_completed"]
        assert len(completed) == 2

        # Verify for_each_item_failed was emitted once (for B)
        failed = [e for e in events if e.type == "for_each_item_failed"]
        assert len(failed) == 1
        assert failed[0].data["item_key"] == "1"
        assert failed[0].data["error_type"] == "Exception"
        assert "Processing failed" in failed[0].data["message"]

        # Verify summary shows 2 succeeded, 1 failed
        summary = [e for e in events if e.type == "for_each_completed"]
        assert len(summary) == 1
        assert summary[0].data["success_count"] == 2
        assert summary[0].data["failure_count"] == 1

        # Verify workflow completed (continue_on_error allows this)
        assert result["count"] == 3


class TestForEachAgentNameAttribution:
    """Tests that for-each iterations expose a qualified agent name.

    Each iteration of a for-each group must pass a per-iteration ``AgentDef``
    to the executor whose ``name`` field is qualified with the item key
    (e.g. ``"processor[0]"``). This is what enables provider-side verbose
    logging (e.g. CopilotProvider tool/reasoning lines) to attribute
    interleaved output to a specific iteration. See issue #16.
    """

    def _make_config(self, key_by: str | None = None) -> WorkflowConfig:
        """Build a minimal config with one for-each group of 3 items."""
        for_each_spec: dict[str, Any] = {
            "name": "processors",
            "type": "for_each",
            "source": "finder.output.items",
            "as": "item",
            "max_concurrent": 3,
            "agent": {
                "name": "processor",
                "model": "gpt-4",
                "prompt": "Process {{ item }}",
                "output": {"result": {"type": "string"}},
            },
            "routes": [{"to": "$end"}],
        }
        if key_by is not None:
            for_each_spec["key_by"] = key_by

        return WorkflowConfig(
            workflow=WorkflowDef(
                name="qualify-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[ForEachDef.model_validate(for_each_spec)],
            output={"count": "{{ processors.count }}"},
        )

    @pytest.mark.asyncio
    async def test_executor_receives_qualified_agent_name_by_index(self) -> None:
        """Without ``key_by``, iterations are keyed by their integer index."""
        config = self._make_config()

        items = ["A", "B", "C"]

        provider = MagicMock()
        provider.execute = AsyncMock()

        seen_agent_names: list[str] = []

        async def capture_execute(*args: Any, **kwargs: Any) -> AgentOutput:
            agent = kwargs.get("agent") if "agent" in kwargs else args[0]
            seen_agent_names.append(agent.name)
            if agent.name == "finder":
                return AgentOutput(
                    content={"items": items},
                    raw_response={},
                    model="gpt-4",
                    tokens_used=20,
                )
            return AgentOutput(
                content={"result": f"ok-{agent.name}"},
                raw_response={},
                model="gpt-4",
                tokens_used=10,
            )

        provider.execute.side_effect = capture_execute

        engine = WorkflowEngine(config, provider)
        await engine.run({})

        # Sequential agent stays unqualified
        assert "finder" in seen_agent_names
        # For-each iterations receive qualified names per index
        for i in range(len(items)):
            assert f"processor[{i}]" in seen_agent_names
        # The unqualified name MUST NOT reach the executor for for-each items
        assert "processor" not in seen_agent_names

    @pytest.mark.asyncio
    async def test_executor_receives_qualified_agent_name_by_key(self) -> None:
        """With ``key_by``, iterations are keyed by the resolved item field."""
        # key_by extracts a value from each dict item via dotted path.
        config = self._make_config(key_by="id")

        items = [
            {"id": "alpha", "value": 1},
            {"id": "beta", "value": 2},
            {"id": "gamma", "value": 3},
        ]

        provider = MagicMock()
        provider.execute = AsyncMock()

        seen_agent_names: list[str] = []

        async def capture_execute(*args: Any, **kwargs: Any) -> AgentOutput:
            agent = kwargs.get("agent") if "agent" in kwargs else args[0]
            seen_agent_names.append(agent.name)
            if agent.name == "finder":
                return AgentOutput(
                    content={"items": items},
                    raw_response={},
                    model="gpt-4",
                    tokens_used=20,
                )
            return AgentOutput(
                content={"result": "ok"},
                raw_response={},
                model="gpt-4",
                tokens_used=10,
            )

        provider.execute.side_effect = capture_execute

        engine = WorkflowEngine(config, provider)
        await engine.run({})

        for item in items:
            assert f"processor[{item['id']}]" in seen_agent_names

    @pytest.mark.asyncio
    async def test_original_agent_def_is_unmodified(self) -> None:
        """``model_copy`` must not mutate the workflow's original ``AgentDef``."""
        config = self._make_config()
        original_name = config.for_each[0].agent.name

        provider = MagicMock()
        provider.execute = AsyncMock()

        async def stub_execute(*args: Any, **kwargs: Any) -> AgentOutput:
            agent = kwargs.get("agent") if "agent" in kwargs else args[0]
            if agent.name == "finder":
                return AgentOutput(
                    content={"items": ["A", "B"]},
                    raw_response={},
                    model="gpt-4",
                    tokens_used=20,
                )
            return AgentOutput(
                content={"result": "ok"},
                raw_response={},
                model="gpt-4",
                tokens_used=10,
            )

        provider.execute.side_effect = stub_execute

        engine = WorkflowEngine(config, provider)
        await engine.run({})

        # Workflow-config-attached AgentDef must keep its original name
        assert config.for_each[0].agent.name == original_name == "processor"

    @pytest.mark.asyncio
    async def test_parallel_agents_keep_original_names(self) -> None:
        """Static parallel groups must NOT qualify agent names; each agent
        in a parallel group already has its own unique name."""
        from conductor.config.schema import ParallelGroup

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-name-test",
                entry_point="fan_out",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="alpha",
                    model="gpt-4",
                    prompt="Run alpha",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="beta",
                    model="gpt-4",
                    prompt="Run beta",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="fan_out",
                    agents=["alpha", "beta"],
                ),
            ],
            output={"result": "done"},
        )

        provider = MagicMock()
        provider.execute = AsyncMock()
        seen_agent_names: list[str] = []

        async def capture_execute(*args: Any, **kwargs: Any) -> AgentOutput:
            agent = kwargs.get("agent") if "agent" in kwargs else args[0]
            seen_agent_names.append(agent.name)
            return AgentOutput(
                content={"result": "ok"},
                raw_response={},
                model="gpt-4",
                tokens_used=10,
            )

        provider.execute.side_effect = capture_execute

        engine = WorkflowEngine(config, provider)
        await engine.run({})

        # Parallel agents reach the executor with their original names —
        # no per-iteration qualification.
        assert sorted(seen_agent_names) == ["alpha", "beta"]

    @pytest.mark.asyncio
    async def test_timeout_wrapper_receives_qualified_agent_name(self) -> None:
        """The per-item agent timeout wrapper must observe the qualified
        agent name, so that ``agent_timeout`` events emitted on timeout in a
        for-each iteration are attributed to the specific item — not the
        unqualified agent name."""
        import asyncio
        import contextlib

        from conductor.exceptions import ExecutionError

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="timeout-attribution-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "max_concurrent": 2,
                        "failure_mode": "continue_on_error",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                            # Short per-agent timeout so the wrapper fires
                            "timeout_seconds": 1,
                        },
                        "routes": [{"to": "$end"}],
                    }
                ),
            ],
            output={"count": "{{ processors.count }}"},
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        async def execute_side_effect(*args: Any, **kwargs: Any) -> AgentOutput:
            agent = kwargs.get("agent") if "agent" in kwargs else args[0]
            if agent.name == "finder":
                return AgentOutput(
                    content={"items": ["A", "B"]},
                    raw_response={},
                    model="gpt-4",
                    tokens_used=20,
                )
            # Sleep long enough to trigger the per-agent timeout wrapper
            await asyncio.sleep(5)
            return AgentOutput(  # pragma: no cover — should always time out first
                content={"result": "ok"},
                raw_response={},
                model="gpt-4",
                tokens_used=10,
            )

        provider.execute.side_effect = execute_side_effect

        emitter = WorkflowEventEmitter()
        events: list[WorkflowEvent] = []
        emitter.subscribe(lambda e: events.append(e))

        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        # Both items time out and `continue_on_error` raises because none
        # succeeded. The events we care about are still emitted before the
        # raise, so swallow it and inspect them.
        with contextlib.suppress(ExecutionError):
            await engine.run({})

        # The wrapper's ``agent_timeout`` event must carry the qualified
        # per-iteration name, not the unqualified for-each group agent name.
        timeouts = [e for e in events if e.type == "agent_timeout"]
        assert len(timeouts) == 2, timeouts
        timeout_names = {ev.data["agent_name"] for ev in timeouts}
        assert timeout_names == {"processor[0]", "processor[1]"}


class TestForEachItemCallbackMergeOrder:
    """Tests the ``_item_callback`` merge order in ``_execute_for_each_group``.

    The wrapper places ``agent_name`` (the for-each group name) and
    ``item_key`` *after* ``**data`` so they override any qualified
    ``agent_name`` the provider emits (e.g. via ``agent_retry``). This keeps
    the dashboard/JSONL event contract stable: ``agent_name`` always equals
    the for-each group name; ``item_key`` disambiguates iterations.
    """

    @pytest.mark.asyncio
    async def test_callback_overrides_agent_name_from_provider(self) -> None:
        """Even if the provider's callback already carries a qualified
        ``agent_name``, the wrapper must overwrite it with the group name."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="callback-merge-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "max_concurrent": 2,
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "$end"}],
                    }
                ),
            ],
            output={"count": "{{ processors.count }}"},
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        async def simulate_provider(*args: Any, **kwargs: Any) -> AgentOutput:
            agent = kwargs.get("agent") if "agent" in kwargs else args[0]
            event_callback = kwargs.get("event_callback")
            if agent.name == "finder":
                return AgentOutput(
                    content={"items": ["A", "B"]},
                    raw_response={},
                    model="gpt-4",
                    tokens_used=20,
                )

            # Simulate a provider event that includes its own qualified
            # ``agent_name`` (mirrors what CopilotProvider's ``agent_retry``
            # event emits — see ``copilot.py`` _execute_with_retry).
            if event_callback is not None:
                event_callback(
                    "agent_retry",
                    {
                        "agent_name": agent.name,  # qualified, e.g. processor[0]
                        "attempt": 1,
                        "max_attempts": 3,
                    },
                )
            return AgentOutput(
                content={"result": "ok"},
                raw_response={},
                model="gpt-4",
                tokens_used=10,
            )

        provider.execute.side_effect = simulate_provider

        emitter = WorkflowEventEmitter()
        events: list[WorkflowEvent] = []
        emitter.subscribe(lambda e: events.append(e))

        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        # All agent_retry events emitted from inside a for-each must keep
        # ``agent_name`` equal to the for-each *group* name and expose
        # ``item_key`` separately — even though the provider sent a
        # qualified name on the inner payload.
        retries = [e for e in events if e.type == "agent_retry"]
        assert len(retries) == 2, retries
        for ev in retries:
            assert ev.data["agent_name"] == "processors"
            assert ev.data["item_key"] in {"0", "1"}
