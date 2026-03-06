"""Tests for event emission from WorkflowEngine.

Tests verify that the WorkflowEngine emits the correct events at the correct
execution points when an event_emitter is provided. All 21 event types are
covered (20 from design doc + script_failed).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    GateOption,
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
from conductor.exceptions import ConductorError, MaxIterationsError
from conductor.providers.copilot import CopilotProvider


class EventCollector:
    """Helper to collect events emitted by a WorkflowEventEmitter."""

    def __init__(self) -> None:
        self.events: list[WorkflowEvent] = []

    def __call__(self, event: WorkflowEvent) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        """Return list of event types in order."""
        return [e.type for e in self.events]

    def of_type(self, event_type: str) -> list[WorkflowEvent]:
        """Return all events of a specific type."""
        return [e for e in self.events if e.type == event_type]

    def first(self, event_type: str) -> WorkflowEvent:
        """Return the first event of a specific type."""
        matches = self.of_type(event_type)
        assert matches, f"No event of type '{event_type}' found"
        return matches[0]


def _make_emitter_and_collector() -> tuple[WorkflowEventEmitter, EventCollector]:
    """Create an emitter and collector pair."""
    emitter = WorkflowEventEmitter()
    collector = EventCollector()
    emitter.subscribe(collector)
    return emitter, collector


class TestNoEmitter:
    """Tests that passing event_emitter=None (default) works with zero overhead."""

    @pytest.mark.asyncio
    async def test_existing_workflow_no_emitter(self) -> None:
        """Existing tests pass unchanged when event_emitter is not provided."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="simple",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Answer: {{ workflow.input.q }}",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"answer": "{{ agent1.output.answer }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"answer": "ok"})
        engine = WorkflowEngine(config, provider)
        result = await engine.run({"q": "test"})
        assert result["answer"] == "ok"

    @pytest.mark.asyncio
    async def test_emitter_none_explicit(self) -> None:
        """Passing event_emitter=None explicitly works."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="simple",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Answer: {{ workflow.input.q }}",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"answer": "{{ agent1.output.answer }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"answer": "ok"})
        engine = WorkflowEngine(config, provider, event_emitter=None)
        result = await engine.run({"q": "test"})
        assert result["answer"] == "ok"


class TestWorkflowStartedEvent:
    """Tests for the workflow_started event."""

    @pytest.mark.asyncio
    async def test_workflow_started_emitted(self) -> None:
        """workflow_started event is emitted before the execution loop."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-workflow",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Do something",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent1.output.result }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "done"})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        event = collector.first("workflow_started")
        assert event.data["name"] == "test-workflow"
        assert event.data["entry_point"] == "agent1"
        assert len(event.data["agents"]) == 1
        assert event.data["agents"][0]["name"] == "agent1"
        assert event.data["agents"][0]["type"] == "agent"
        assert event.timestamp > 0

    @pytest.mark.asyncio
    async def test_workflow_started_includes_routes(self) -> None:
        """workflow_started includes route information."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="routed",
                entry_point="a",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="a",
                    model="gpt-4",
                    prompt="step a",
                    output={"x": OutputField(type="string")},
                    routes=[RouteDef(to="b")],
                ),
                AgentDef(
                    name="b",
                    model="gpt-4",
                    prompt="step b",
                    output={"y": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"y": "{{ b.output.y }}"},
        )
        provider = CopilotProvider(
            mock_handler=lambda a, p, c: {"x": "1"} if a.name == "a" else {"y": "2"}
        )
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        event = collector.first("workflow_started")
        routes = event.data["routes"]
        assert any(r["from"] == "a" and r["to"] == "b" for r in routes)
        assert any(r["from"] == "b" and r["to"] == "$end" for r in routes)


class TestAgentEvents:
    """Tests for agent_started, agent_completed, and agent_failed events."""

    @pytest.mark.asyncio
    async def test_agent_started_and_completed(self) -> None:
        """agent_started and agent_completed events are emitted for each agent."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="a",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="a",
                    model="gpt-4",
                    prompt="do a",
                    output={"val": OutputField(type="string")},
                    routes=[RouteDef(to="b")],
                ),
                AgentDef(
                    name="b",
                    model="gpt-4",
                    prompt="do b",
                    output={"val": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ b.output.val }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"val": a.name})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        # Check agent_started events
        started = collector.of_type("agent_started")
        assert len(started) == 2
        assert started[0].data["agent_name"] == "a"
        assert started[0].data["agent_type"] == "agent"
        assert started[0].data["iteration"] == 1
        assert started[1].data["agent_name"] == "b"

        # Check agent_completed events
        completed = collector.of_type("agent_completed")
        assert len(completed) == 2
        assert completed[0].data["agent_name"] == "a"
        assert completed[0].data["elapsed"] > 0
        assert completed[0].data["output"] == {"val": "a"}
        assert completed[0].data["output_keys"] == ["val"]
        assert completed[1].data["agent_name"] == "b"

    @pytest.mark.asyncio
    async def test_agent_failed_on_error(self) -> None:
        """agent_failed is covered via workflow_failed when agent raises."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="fail-test",
                entry_point="bad",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="bad",
                    model="gpt-4",
                    prompt="fail",
                    output={"x": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"x": "{{ bad.output.x }}"},
        )

        def failing_handler(a, p, c):
            raise RuntimeError("Agent exploded")

        provider = CopilotProvider(mock_handler=failing_handler)
        engine = WorkflowEngine(config, provider, event_emitter=emitter)

        with pytest.raises(ConductorError):
            await engine.run({})

        # Should have workflow_failed event
        failed = collector.of_type("workflow_failed")
        assert len(failed) == 1
        assert "agent_name" in failed[0].data
        assert failed[0].data["message"]


class TestRouteEvents:
    """Tests for route_taken events."""

    @pytest.mark.asyncio
    async def test_route_taken_emitted(self) -> None:
        """route_taken event is emitted at routing decision points."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="routed",
                entry_point="a",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="a",
                    model="gpt-4",
                    prompt="step a",
                    output={"x": OutputField(type="string")},
                    routes=[RouteDef(to="b")],
                ),
                AgentDef(
                    name="b",
                    model="gpt-4",
                    prompt="step b",
                    output={"y": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"y": "{{ b.output.y }}"},
        )
        provider = CopilotProvider(
            mock_handler=lambda a, p, c: {"x": "1"} if a.name == "a" else {"y": "2"}
        )
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        routes = collector.of_type("route_taken")
        assert len(routes) == 2
        assert routes[0].data["from_agent"] == "a"
        assert routes[0].data["to_agent"] == "b"
        assert routes[1].data["from_agent"] == "b"
        assert routes[1].data["to_agent"] == "$end"


class TestWorkflowCompletedEvent:
    """Tests for workflow_completed event."""

    @pytest.mark.asyncio
    async def test_workflow_completed_emitted(self) -> None:
        """workflow_completed is emitted when workflow reaches $end."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="complete-test",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="go",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"answer": "{{ agent1.output.answer }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"answer": "done"})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        event = collector.first("workflow_completed")
        assert event.data["elapsed"] > 0
        assert event.data["output"]["answer"] == "done"


class TestWorkflowFailedEvent:
    """Tests for workflow_failed event."""

    @pytest.mark.asyncio
    async def test_workflow_failed_emitted(self) -> None:
        """workflow_failed is emitted when workflow raises an exception."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="fail-test",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="fail",
                    output={"x": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"x": "{{ agent1.output.x }}"},
        )

        def failing(a, p, c):
            raise RuntimeError("boom")

        provider = CopilotProvider(mock_handler=failing)
        engine = WorkflowEngine(config, provider, event_emitter=emitter)

        with pytest.raises(ConductorError):
            await engine.run({})

        event = collector.first("workflow_failed")
        assert "error_type" in event.data
        assert event.data["message"]
        assert event.data["agent_name"] == "agent1"

    @pytest.mark.asyncio
    async def test_workflow_failed_error_type_is_class_name(self) -> None:
        """workflow_failed.error_type is the exception class name."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="fail-test",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=1),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="go",
                    output={"x": OutputField(type="string")},
                    routes=[RouteDef(to="agent1")],  # Loop to self
                ),
            ],
            output={"x": "{{ agent1.output.x }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"x": "loop"})
        engine = WorkflowEngine(config, provider, event_emitter=emitter, skip_gates=True)

        with pytest.raises(MaxIterationsError):
            await engine.run({})

        event = collector.first("workflow_failed")
        assert event.data["error_type"] == "MaxIterationsError"


class TestEventSequence:
    """Tests for correct event ordering in a simple workflow."""

    @pytest.mark.asyncio
    async def test_event_ordering(self) -> None:
        """Events are emitted in correct order for a single-agent workflow."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="order-test",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="go",
                    output={"x": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"x": "{{ agent1.output.x }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"x": "done"})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        types = collector.types()
        assert types == [
            "workflow_started",
            "agent_started",
            "agent_prompt_rendered",
            "agent_completed",
            "route_taken",
            "workflow_completed",
        ]


class TestScriptEvents:
    """Tests for script_started, script_completed, and script_failed events."""

    @pytest.mark.asyncio
    async def test_script_started_and_completed(self) -> None:
        """script_started and script_completed events emitted for script steps."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="script-test",
                entry_point="run_echo",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="run_echo",
                    type="script",
                    command=sys.executable,
                    args=["-c", "print('hello')"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ run_echo.output.stdout }}"},
        )
        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider, event_emitter=emitter)
        await engine.run({})

        started = collector.first("script_started")
        assert started.data["agent_name"] == "run_echo"

        completed = collector.first("script_completed")
        assert completed.data["agent_name"] == "run_echo"
        assert completed.data["elapsed"] > 0
        assert "stdout" in completed.data
        assert "exit_code" in completed.data

    @pytest.mark.asyncio
    async def test_script_failed_emitted(self) -> None:
        """script_failed event emitted when a script raises an exception."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="script-fail",
                entry_point="bad_script",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="bad_script",
                    type="script",
                    command="nonexistent_command_xyz_12345",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ bad_script.output.stdout }}"},
        )
        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider, event_emitter=emitter)

        with pytest.raises(ConductorError):
            await engine.run({})

        # script_failed should be emitted before workflow_failed
        types = collector.types()
        assert "script_failed" in types
        assert "workflow_failed" in types
        assert types.index("script_failed") < types.index("workflow_failed")

        failed = collector.first("script_failed")
        assert failed.data["agent_name"] == "bad_script"
        assert failed.data["elapsed"] >= 0
        assert failed.data["error_type"]
        assert failed.data["message"]


class TestParallelGroupEvents:
    """Tests for parallel group event emission."""

    @pytest.mark.asyncio
    async def test_parallel_lifecycle_events(self) -> None:
        """parallel_started, parallel_agent_completed, parallel_completed emitted."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-test",
                entry_point="team",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="r1",
                    model="gpt-4",
                    prompt="research 1",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="r2",
                    model="gpt-4",
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

        # Check parallel_started
        started = collector.first("parallel_started")
        assert started.data["group_name"] == "team"
        assert started.data["agents"] == ["r1", "r2"]

        # Check parallel_agent_completed (2 agents)
        agent_completed = collector.of_type("parallel_agent_completed")
        assert len(agent_completed) == 2
        agent_names = {e.data["agent_name"] for e in agent_completed}
        assert agent_names == {"r1", "r2"}
        for e in agent_completed:
            assert e.data["group_name"] == "team"
            assert e.data["elapsed"] > 0

        # Check parallel_completed
        completed = collector.first("parallel_completed")
        assert completed.data["group_name"] == "team"
        assert completed.data["success_count"] == 2
        assert completed.data["failure_count"] == 0

    @pytest.mark.asyncio
    async def test_parallel_agent_failed_event(self) -> None:
        """parallel_agent_failed is emitted when a parallel agent fails."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-fail",
                entry_point="team",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="good",
                    model="gpt-4",
                    prompt="ok",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="bad",
                    model="gpt-4",
                    prompt="fail",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="team",
                    agents=["good", "bad"],
                    failure_mode="continue_on_error",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "partial"},
        )

        def handler(a, p, c):
            if a.name == "bad":
                raise RuntimeError("bad agent failed")
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=handler)
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        # Check parallel_agent_failed
        failed = collector.of_type("parallel_agent_failed")
        assert len(failed) == 1
        assert failed[0].data["agent_name"] == "bad"
        assert failed[0].data["group_name"] == "team"
        assert failed[0].data["error_type"] == "ProviderError"
        assert "bad agent failed" in failed[0].data["message"]

        # Check parallel_completed with failure count
        completed = collector.first("parallel_completed")
        assert completed.data["success_count"] == 1
        assert completed.data["failure_count"] == 1


class TestForEachGroupEvents:
    """Tests for for-each group event emission."""

    @pytest.mark.asyncio
    async def test_for_each_lifecycle_events(self) -> None:
        """for_each_started, item_started, item_completed, for_each_completed emitted."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="foreach-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=20),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="process_items")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="process_items",
                    type="for_each",
                    source="finder.output.items",
                    **{"as": "item"},
                    agent=AgentDef(
                        name="processor",
                        model="gpt-4",
                        prompt="process {{ item }}",
                        output={"result": OutputField(type="string")},
                    ),
                    max_concurrent=5,
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "done"},
        )

        def handler(a, p, c):
            if a.name == "finder":
                return {"items": ["a", "b", "c"]}
            return {"result": "processed"}

        provider = CopilotProvider(mock_handler=handler)
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        # Check for_each_started
        started = collector.first("for_each_started")
        assert started.data["group_name"] == "process_items"
        assert started.data["item_count"] == 3
        assert started.data["max_concurrent"] == 5
        assert started.data["failure_mode"] == "fail_fast"

        # Check for_each_item_started (3 items)
        item_started = collector.of_type("for_each_item_started")
        assert len(item_started) == 3
        for e in item_started:
            assert e.data["group_name"] == "process_items"

        # Check for_each_item_completed (3 items)
        item_completed = collector.of_type("for_each_item_completed")
        assert len(item_completed) == 3
        for e in item_completed:
            assert e.data["group_name"] == "process_items"
            assert e.data["elapsed"] > 0

        # Check for_each_completed
        completed = collector.first("for_each_completed")
        assert completed.data["group_name"] == "process_items"
        assert completed.data["success_count"] == 3
        assert completed.data["failure_count"] == 0

    @pytest.mark.asyncio
    async def test_for_each_item_failed_event(self) -> None:
        """for_each_item_failed is emitted when an item fails."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="foreach-fail",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=20),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="process_items")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="process_items",
                    type="for_each",
                    source="finder.output.items",
                    **{"as": "item"},
                    agent=AgentDef(
                        name="processor",
                        model="gpt-4",
                        prompt="process {{ item }}",
                        output={"result": OutputField(type="string")},
                    ),
                    failure_mode="continue_on_error",
                    max_concurrent=5,
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "done"},
        )

        def handler(a, p, c):
            if a.name == "finder":
                return {"items": ["ok_item", "fail_item"]}
            # Use the injected item variable to decide success/failure
            if "fail_item" in p:
                raise RuntimeError("item failed")
            return {"result": "processed"}

        provider = CopilotProvider(mock_handler=handler)
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        # Check for_each_item_failed
        failed = collector.of_type("for_each_item_failed")
        assert len(failed) == 1
        assert failed[0].data["group_name"] == "process_items"
        assert failed[0].data["error_type"] == "ProviderError"

        # Check for_each_completed with failure count
        completed = collector.first("for_each_completed")
        assert completed.data["success_count"] == 1
        assert completed.data["failure_count"] == 1

    @pytest.mark.asyncio
    async def test_for_each_qualifies_agent_names_in_call_history(self) -> None:
        """Each for-each item executes with a qualified per-item agent name."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="foreach-agent-name-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=20),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="process_items")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="process_items",
                    type="for_each",
                    source="finder.output.items",
                    **{"as": "item"},
                    agent=AgentDef(
                        name="processor",
                        model="gpt-4",
                        prompt="process {{ item }}",
                        output={"result": OutputField(type="string")},
                    ),
                    max_concurrent=5,
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "done"},
        )

        def handler(a, p, c):
            if a.name == "finder":
                return {"items": ["a", "b", "c"]}
            return {"result": f"processed-{a.name}"}

        provider = CopilotProvider(mock_handler=handler)
        engine = WorkflowEngine(config, provider)
        await engine.run({})

        call_names = {entry["agent_name"] for entry in provider.get_call_history()}
        assert "finder" in call_names
        assert {"processor[0]", "processor[1]", "processor[2]"}.issubset(call_names)

    @pytest.mark.asyncio
    async def test_for_each_qualifies_agent_names_with_key_by(self) -> None:
        """for_each key_by values are reflected in qualified per-item agent names."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="foreach-key-by-agent-name-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=20),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="process_items")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="process_items",
                    type="for_each",
                    source="finder.output.items",
                    key_by="id",
                    **{"as": "item"},
                    agent=AgentDef(
                        name="processor",
                        model="gpt-4",
                        prompt="process {{ item.id }}",
                        output={"result": OutputField(type="string")},
                    ),
                    max_concurrent=5,
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "done"},
        )

        def handler(a, p, c):
            if a.name == "finder":
                return {"items": [{"id": "item_a"}, {"id": "item_b"}]}
            return {"result": f"processed-{a.name}"}

        provider = CopilotProvider(mock_handler=handler)
        engine = WorkflowEngine(config, provider)
        await engine.run({})

        call_names = {entry["agent_name"] for entry in provider.get_call_history()}
        assert {"processor[item_a]", "processor[item_b]"}.issubset(call_names)


class TestTimestamps:
    """Tests that all events have valid timestamps."""

    @pytest.mark.asyncio
    async def test_all_events_have_timestamps(self) -> None:
        """Every emitted event has a positive timestamp."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="ts-test",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="go",
                    output={"x": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"x": "{{ agent1.output.x }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"x": "done"})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        for event in collector.events:
            assert event.timestamp > 0, f"Event {event.type} has invalid timestamp"

    @pytest.mark.asyncio
    async def test_timestamps_monotonically_increase(self) -> None:
        """Event timestamps are monotonically non-decreasing."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="ts-test",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="go",
                    output={"x": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"x": "{{ agent1.output.x }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"x": "done"})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        await engine.run({})

        for i in range(1, len(collector.events)):
            assert collector.events[i].timestamp >= collector.events[i - 1].timestamp


class TestGateEvents:
    """Tests for gate_presented and gate_resolved events."""

    @pytest.mark.asyncio
    async def test_gate_presented_and_resolved(self) -> None:
        """gate_presented and gate_resolved events emitted for human_gate agents."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="gate-test",
                entry_point="reviewer",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="reviewer",
                    type="human_gate",
                    prompt="Do you approve?",
                    options=[
                        GateOption(
                            label="Approve",
                            value="approved",
                            route="finalizer",
                        ),
                        GateOption(
                            label="Reject",
                            value="rejected",
                            route="$end",
                        ),
                    ],
                ),
                AgentDef(
                    name="finalizer",
                    model="gpt-4",
                    prompt="finalize",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ finalizer.output.result }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "finalized"})
        engine = WorkflowEngine(config, provider, event_emitter=emitter, skip_gates=True)
        await engine.run({})

        # Check gate_presented
        presented = collector.first("gate_presented")
        assert presented.data["agent_name"] == "reviewer"
        assert presented.data["options"] == ["approved", "rejected"]
        assert presented.data["prompt"] == "Do you approve?"

        # Check gate_resolved (skip_gates auto-selects first option)
        resolved = collector.first("gate_resolved")
        assert resolved.data["agent_name"] == "reviewer"
        assert resolved.data["selected_option"] == "approved"
        assert resolved.data["route"] == "finalizer"
        assert resolved.data["additional_input"] == {}

    @pytest.mark.asyncio
    async def test_gate_resolved_to_end(self) -> None:
        """gate_resolved emits correctly when gate routes to $end."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="gate-end-test",
                entry_point="gate",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="gate",
                    type="human_gate",
                    prompt="Continue?",
                    options=[
                        GateOption(
                            label="Stop",
                            value="stop",
                            route="$end",
                        ),
                        GateOption(
                            label="Continue",
                            value="continue",
                            route="gate",
                        ),
                    ],
                ),
            ],
            output={"status": "stopped"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(config, provider, event_emitter=emitter, skip_gates=True)
        await engine.run({})

        # Gate resolved to $end
        resolved = collector.first("gate_resolved")
        assert resolved.data["route"] == "$end"

        # workflow_completed should follow
        types = collector.types()
        assert "workflow_completed" in types
        assert types.index("gate_resolved") < types.index("workflow_completed")

    @pytest.mark.asyncio
    async def test_gate_event_ordering(self) -> None:
        """gate_presented comes before gate_resolved in event stream."""
        emitter, collector = _make_emitter_and_collector()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="gate-order-test",
                entry_point="gate",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="gate",
                    type="human_gate",
                    prompt="Approve?",
                    options=[
                        GateOption(
                            label="Yes",
                            value="yes",
                            route="$end",
                        ),
                    ],
                ),
            ],
            output={"status": "done"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(config, provider, event_emitter=emitter, skip_gates=True)
        await engine.run({})

        types = collector.types()
        assert "gate_presented" in types
        assert "gate_resolved" in types
        assert types.index("gate_presented") < types.index("gate_resolved")
