"""Tests for per-agent iteration counter in workflow events.

This module tests that the iteration counter sent in agent_started events
is agent-specific (counts how many times each agent has run), not a global
workflow iteration counter.
"""

import pytest

from conductor.config.schema import AgentDef, RouteDef, WorkflowConfig, WorkflowDef
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.providers.copilot import CopilotProvider


class EventCollector(WorkflowEventEmitter):
    """Simple event emitter that collects events for testing."""

    def __init__(self):
        self.events: list[WorkflowEvent] = []

    def emit(self, event: WorkflowEvent) -> None:
        """Store event in the list."""
        self.events.append(event)

    def get_agent_started_events(self, agent_name: str) -> list[WorkflowEvent]:
        """Get all agent_started events for a specific agent."""
        return [
            e
            for e in self.events
            if e.type == "agent_started" and e.data.get("agent_name") == agent_name
        ]


class TestPerAgentIterationCounter:
    """Test that iteration counters are per-agent, not global."""

    @pytest.mark.asyncio
    async def test_single_agent_loop_counter(self):
        """Test that a single looping agent gets correct iteration counts."""
        # Create a workflow where one agent loops back to itself
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="single-loop",
                entry_point="looper",
            ),
            agents=[
                AgentDef(
                    name="looper",
                    type="agent",
                    model="gpt-4",
                    prompt="Loop iteration {{ context.iteration }}",
                    routes=[
                        RouteDef(to="looper", when="{{ context.iteration < 3 }}"),
                        RouteDef(to="$end"),
                    ],
                )
            ],
            output={"result": "done"},
        )

        # Use mock provider to avoid external dependencies
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
        collector = EventCollector()

        engine = WorkflowEngine(
            config,
            provider=provider,
            event_emitter=collector,
            skip_gates=True,
        )

        await engine.run({})

        # Get agent_started events for 'looper'
        looper_events = collector.get_agent_started_events("looper")

        # Loop runs while context.iteration < 3 (iterations 0, 1, 2)
        assert len(looper_events) == 3

        # Check iteration counts - should be [1, 2, 3]
        # (first execution is iteration 1, second is 2, third is 3)
        iterations = [e.data["iteration"] for e in looper_events]
        assert iterations == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_multi_agent_loop_counter(self):
        """Test that each agent in a loop gets independent iteration counts."""
        # Create a workflow where two agents loop back and forth
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="multi-loop",
                entry_point="agent_a",
            ),
            agents=[
                AgentDef(
                    name="agent_a",
                    type="agent",
                    model="gpt-4",
                    prompt="Agent A iteration {{ context.iteration }}",
                    routes=[
                        RouteDef(to="agent_b"),
                    ],
                ),
                AgentDef(
                    name="agent_b",
                    type="agent",
                    model="gpt-4",
                    prompt="Agent B iteration {{ context.iteration }}",
                    routes=[
                        RouteDef(to="agent_a", when="{{ context.iteration < 5 }}"),
                        RouteDef(to="$end"),
                    ],
                ),
            ],
            output={"result": "done"},
        )

        # Use mock provider to avoid external dependencies
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
        collector = EventCollector()

        engine = WorkflowEngine(
            config,
            provider=provider,
            event_emitter=collector,
            skip_gates=True,
        )

        await engine.run({})

        # Get agent_started events for each agent
        agent_a_events = collector.get_agent_started_events("agent_a")
        agent_b_events = collector.get_agent_started_events("agent_b")

        # agent_a runs 3 times (iterations 0, 2, 4)
        assert len(agent_a_events) == 3
        # agent_b runs 3 times (iterations 1, 3, 5)
        assert len(agent_b_events) == 3

        # Check that each agent's iteration counter is independent
        # agent_a should have iteration counts [1, 2, 3]
        agent_a_iterations = [e.data["iteration"] for e in agent_a_events]
        assert agent_a_iterations == [1, 2, 3]

        # agent_b should have iteration counts [1, 2, 3]
        agent_b_iterations = [e.data["iteration"] for e in agent_b_events]
        assert agent_b_iterations == [1, 2, 3]
