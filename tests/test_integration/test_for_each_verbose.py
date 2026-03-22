"""Test event emission for for-each execution.

This module tests that workflow events are emitted correctly
during for-each execution, which drives console output via
the ConsoleEventSubscriber.
"""

from __future__ import annotations

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
