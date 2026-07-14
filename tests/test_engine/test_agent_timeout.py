"""Tests for per-agent timeout_seconds feature (issue #82).

Tests cover:
- Schema validation: timeout_seconds accepted/rejected per agent type
- Engine: _execute_with_agent_timeout helper behavior
- Integration: agent timeout in main loop, parallel groups, for-each groups
- Event emission: agent_timeout event
- Interaction with workflow-level timeout
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

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
from conductor.exceptions import AgentTimeoutError
from conductor.providers.copilot import CopilotProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class EventCollector:
    """Collects events emitted by a WorkflowEventEmitter."""

    def __init__(self) -> None:
        self.events: list[WorkflowEvent] = []

    def __call__(self, event: WorkflowEvent) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of_type(self, event_type: str) -> list[WorkflowEvent]:
        return [e for e in self.events if e.type == event_type]


def _make_emitter_and_collector() -> tuple[WorkflowEventEmitter, EventCollector]:
    emitter = WorkflowEventEmitter()
    collector = EventCollector()
    emitter.subscribe(collector)
    return emitter, collector


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


class TestAgentTimeoutSchema:
    """Validate timeout_seconds field acceptance/rejection per agent type."""

    def test_regular_agent_accepts_timeout_seconds(self) -> None:
        """Provider-backed agents accept timeout_seconds."""
        agent = AgentDef(
            name="fast",
            model="gpt-4",
            prompt="Do something",
            timeout_seconds=30.0,
            routes=[RouteDef(to="$end")],
        )
        assert agent.timeout_seconds == 30.0

    def test_regular_agent_without_timeout_seconds(self) -> None:
        """timeout_seconds defaults to None."""
        agent = AgentDef(
            name="fast",
            model="gpt-4",
            prompt="Do something",
            routes=[RouteDef(to="$end")],
        )
        assert agent.timeout_seconds is None

    def test_timeout_seconds_must_be_positive(self) -> None:
        """timeout_seconds must be >= 1.0."""
        with pytest.raises(Exception, match="greater than or equal to 1"):
            AgentDef(
                name="bad",
                model="gpt-4",
                prompt="Do something",
                timeout_seconds=0.5,
                routes=[RouteDef(to="$end")],
            )

    def test_script_agent_rejects_timeout_seconds(self) -> None:
        """Script agents must use 'timeout', not 'timeout_seconds'."""
        with pytest.raises(ValueError, match="script agents cannot have 'timeout_seconds'"):
            AgentDef(
                name="script1",
                type="script",
                command="echo hello",
                timeout_seconds=30.0,
                routes=[RouteDef(to="$end")],
            )

    def test_human_gate_rejects_timeout_seconds(self) -> None:
        """Human gate agents cannot have timeout_seconds."""
        with pytest.raises(ValueError, match="human_gate agents cannot have 'timeout_seconds'"):
            AgentDef(
                name="gate1",
                type="human_gate",
                prompt="Choose",
                options=[GateOption(label="Yes", value="yes", route="$end")],
                timeout_seconds=30.0,
                routes=[RouteDef(to="$end")],
            )

    def test_workflow_agent_rejects_timeout_seconds(self) -> None:
        """Workflow agents cannot have timeout_seconds."""
        with pytest.raises(ValueError, match="workflow agents cannot have 'timeout_seconds'"):
            AgentDef(
                name="sub1",
                type="workflow",
                workflow="./sub.yaml",
                timeout_seconds=30.0,
                routes=[RouteDef(to="$end")],
            )


# ---------------------------------------------------------------------------
# Engine helper tests
# ---------------------------------------------------------------------------


class TestExecuteWithAgentTimeout:
    """Unit tests for WorkflowEngine._execute_with_agent_timeout."""

    def _make_engine(
        self,
        timeout_seconds: float | None = None,
        workflow_timeout: int | None = None,
        emitter: WorkflowEventEmitter | None = None,
    ) -> tuple[WorkflowEngine, AgentDef]:
        """Create a minimal engine and agent for timeout testing."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="timeout-test",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10, timeout_seconds=workflow_timeout),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Test",
                    timeout_seconds=timeout_seconds,
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent1.output.result }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)
        engine.limits.start()
        return engine, config.agents[0]

    @pytest.mark.asyncio
    async def test_no_timeout_passes_through(self) -> None:
        """When timeout_seconds is None, coroutine runs without wrapping."""
        engine, agent = self._make_engine(timeout_seconds=None)

        async def fast_coro():
            return "result"

        result = await engine._execute_with_agent_timeout(agent, fast_coro())
        assert result == "result"

    @pytest.mark.asyncio
    async def test_fast_agent_completes_within_timeout(self) -> None:
        """Agent that completes within timeout succeeds."""
        engine, agent = self._make_engine(timeout_seconds=5.0)

        async def fast_coro():
            await asyncio.sleep(0.01)
            return "done"

        result = await engine._execute_with_agent_timeout(agent, fast_coro())
        assert result == "done"

    @pytest.mark.asyncio
    async def test_slow_agent_raises_agent_timeout_error(self) -> None:
        """Agent that exceeds timeout_seconds raises AgentTimeoutError."""
        engine, agent = self._make_engine(timeout_seconds=1.0)

        async def slow_coro():
            await asyncio.sleep(10)
            return "never"

        with pytest.raises(AgentTimeoutError) as exc_info:
            await engine._execute_with_agent_timeout(agent, slow_coro())

        assert exc_info.value.agent_name == "agent1"
        assert exc_info.value.timeout_seconds == 1.0
        assert exc_info.value.elapsed_seconds > 0

    @pytest.mark.asyncio
    async def test_timeout_emits_agent_timeout_event(self) -> None:
        """agent_timeout event is emitted when agent times out."""
        emitter, collector = _make_emitter_and_collector()
        engine, agent = self._make_engine(timeout_seconds=1.0, emitter=emitter)

        async def slow_coro():
            await asyncio.sleep(10)

        with pytest.raises(AgentTimeoutError):
            await engine._execute_with_agent_timeout(agent, slow_coro())

        timeout_events = collector.of_type("agent_timeout")
        assert len(timeout_events) == 1
        assert timeout_events[0].data["agent_name"] == "agent1"
        assert timeout_events[0].data["timeout_seconds"] == 1.0
        assert timeout_events[0].data["elapsed"] > 0

    @pytest.mark.asyncio
    async def test_workflow_timeout_stricter_skips_agent_wrapper(self) -> None:
        """When workflow remaining timeout <= agent timeout, skip agent wrapper.

        This prevents mislabeling a workflow timeout as an agent timeout.
        """
        engine, agent = self._make_engine(timeout_seconds=60.0, workflow_timeout=1)
        # Workflow started with 1s timeout. Remaining is ~1s which is < 60s.
        # The helper should skip the agent timeout wrapper.

        async def fast_coro():
            return "ok"

        # Should complete without wrapping (no agent timeout applied)
        result = await engine._execute_with_agent_timeout(agent, fast_coro())
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_agent_timeout_stricter_applies_agent_wrapper(self) -> None:
        """When agent timeout is stricter than workflow, agent wrapper is used."""
        engine, agent = self._make_engine(timeout_seconds=1.0, workflow_timeout=3600)

        async def slow_coro():
            await asyncio.sleep(10)

        with pytest.raises(AgentTimeoutError):
            await engine._execute_with_agent_timeout(agent, slow_coro())


# ---------------------------------------------------------------------------
# Integration tests: full workflow execution with timeout
# ---------------------------------------------------------------------------


class TestAgentTimeoutIntegration:
    """End-to-end tests for agent timeout in workflow execution."""

    @pytest.mark.asyncio
    async def test_agent_timeout_in_main_loop(self) -> None:
        """Agent with timeout_seconds that exceeds its limit raises AgentTimeoutError."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="timeout-workflow",
                entry_point="slow_agent",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="slow_agent",
                    model="gpt-4",
                    prompt="Think deeply",
                    timeout_seconds=1.0,
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ slow_agent.output.result }}"},
        )

        # Make the provider's execute slow by patching it
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
        engine = WorkflowEngine(config, provider)

        # Patch the executor's execute to add async delay
        original_execute = engine.executor.execute

        async def slow_execute(*args, **kwargs):
            await asyncio.sleep(10)
            return await original_execute(*args, **kwargs)

        with (
            patch.object(engine.executor, "execute", side_effect=slow_execute),
            pytest.raises(AgentTimeoutError) as exc_info,
        ):
            await engine.run({"question": "test"})

        assert exc_info.value.agent_name == "slow_agent"

    @pytest.mark.asyncio
    async def test_agent_timeout_emits_event_in_workflow(self) -> None:
        """agent_timeout event is emitted during workflow execution."""
        emitter, collector = _make_emitter_and_collector()

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="timeout-workflow",
                entry_point="slow_agent",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="slow_agent",
                    model="gpt-4",
                    prompt="Think deeply",
                    timeout_seconds=1.0,
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ slow_agent.output.result }}"},
        )

        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
        engine = WorkflowEngine(config, provider, event_emitter=emitter)

        original_execute = engine.executor.execute

        async def slow_execute(*args, **kwargs):
            await asyncio.sleep(10)
            return await original_execute(*args, **kwargs)

        with (
            patch.object(engine.executor, "execute", side_effect=slow_execute),
            pytest.raises(AgentTimeoutError),
        ):
            await engine.run({"question": "test"})

        assert "agent_timeout" in collector.types()
        timeout_event = collector.of_type("agent_timeout")[0]
        assert timeout_event.data["agent_name"] == "slow_agent"
        assert timeout_event.data["timeout_seconds"] == 1.0

    @pytest.mark.asyncio
    async def test_agent_without_timeout_completes_normally(self) -> None:
        """Agent without timeout_seconds executes without wrapping."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="normal-workflow",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Answer: {{ workflow.input.question }}",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"answer": "{{ agent1.output.answer }}"},
        )

        provider = CopilotProvider(mock_handler=lambda a, p, c: {"answer": "42"})
        engine = WorkflowEngine(config, provider)
        result = await engine.run({"question": "test"})
        assert result["answer"] == 42


# ---------------------------------------------------------------------------
# Parallel group timeout tests
# ---------------------------------------------------------------------------


class TestAgentTimeoutParallel:
    """Agent timeout behavior in parallel groups."""

    @pytest.mark.asyncio
    async def test_timeout_in_parallel_group_fail_fast(self) -> None:
        """Timed-out agent in fail_fast parallel group fails the group."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-timeout",
                entry_point="group1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="fast_agent",
                    model="gpt-4",
                    prompt="Quick task",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="slow_agent",
                    model="gpt-4",
                    prompt="Slow task",
                    timeout_seconds=1.0,
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="group1",
                    agents=["fast_agent", "slow_agent"],
                    failure_mode="fail_fast",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "done"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        # Patch only slow_agent's execution to be slow
        original_get_executor = engine._get_executor_for_agent

        async def patched_get_executor(agent):
            executor = await original_get_executor(agent)
            if agent.name == "slow_agent":
                original_exec = executor.execute

                async def slow_exec(*args, **kwargs):
                    await asyncio.sleep(10)
                    return await original_exec(*args, **kwargs)

                executor.execute = slow_exec
            return executor

        with (
            patch.object(engine, "_get_executor_for_agent", side_effect=patched_get_executor),
            pytest.raises(Exception) as exc_info,
        ):
            await engine.run({})

        # The error should be related to the timeout (either AgentTimeoutError
        # or wrapped in an ExecutionError by the parallel group handler)
        error_str = str(exc_info.value)
        assert "timeout" in error_str.lower() or "slow_agent" in error_str

    @pytest.mark.asyncio
    async def test_timeout_in_parallel_group_continue_on_error(self) -> None:
        """Timed-out agent in continue_on_error parallel group allows others to succeed."""
        emitter, collector = _make_emitter_and_collector()

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-timeout-continue",
                entry_point="group1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="fast_agent",
                    model="gpt-4",
                    prompt="Quick task",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="slow_agent",
                    model="gpt-4",
                    prompt="Slow task",
                    timeout_seconds=1.0,
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="group1",
                    agents=["fast_agent", "slow_agent"],
                    failure_mode="continue_on_error",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "done"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, event_emitter=emitter)

        original_get_executor = engine._get_executor_for_agent

        async def patched_get_executor(agent):
            executor = await original_get_executor(agent)
            if agent.name == "slow_agent":
                original_exec = executor.execute

                async def slow_exec(*args, **kwargs):
                    await asyncio.sleep(10)
                    return await original_exec(*args, **kwargs)

                executor.execute = slow_exec
            return executor

        with patch.object(engine, "_get_executor_for_agent", side_effect=patched_get_executor):
            result = await engine.run({})

        # Workflow should complete (continue_on_error)
        assert result is not None
        # agent_timeout should have been emitted
        assert "agent_timeout" in collector.types()


# ---------------------------------------------------------------------------
# Exception tests
# ---------------------------------------------------------------------------


class TestAgentTimeoutError:
    """Tests for AgentTimeoutError exception."""

    def test_agent_timeout_error_attributes(self) -> None:
        """AgentTimeoutError has correct attributes."""
        err = AgentTimeoutError(
            agent_name="slow_agent",
            elapsed_seconds=15.3,
            timeout_seconds=10.0,
        )
        assert err.agent_name == "slow_agent"
        assert err.elapsed_seconds == 15.3
        assert err.timeout_seconds == 10.0
        assert err.current_agent == "slow_agent"
        assert "slow_agent" in str(err)
        assert "10" in str(err)

    def test_agent_timeout_error_is_timeout_error(self) -> None:
        """AgentTimeoutError inherits from conductor.exceptions.TimeoutError."""
        from conductor.exceptions import TimeoutError as ConductorTimeoutError

        err = AgentTimeoutError(
            agent_name="test",
            elapsed_seconds=5.0,
            timeout_seconds=3.0,
        )
        assert isinstance(err, ConductorTimeoutError)

    def test_agent_timeout_error_suggestion(self) -> None:
        """AgentTimeoutError has a helpful suggestion."""
        err = AgentTimeoutError(
            agent_name="researcher",
            elapsed_seconds=120.5,
            timeout_seconds=120.0,
        )
        assert "timeout_seconds" in err.suggestion
        assert "researcher" in err.suggestion


# ---------------------------------------------------------------------------
# For-each group timeout tests
# ---------------------------------------------------------------------------


class TestAgentTimeoutForEach:
    """Agent timeout behavior in for-each groups."""

    @pytest.mark.asyncio
    async def test_timeout_in_for_each_continue_on_error(self) -> None:
        """Timed-out agent in continue_on_error for-each group allows others to succeed."""
        emitter, collector = _make_emitter_and_collector()

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="for-each-timeout",
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
                            "prompt": "Process: {{ item }}",
                            "timeout_seconds": 1.0,
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "$end"}],
                    }
                ),
            ],
            output={"result": "done"},
        )

        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            if agent.name == "finder":
                return {"items": ["item1", "item2", "item3"]}
            return {"result": "processed"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, event_emitter=emitter)

        original_get_executor = engine._get_executor_for_agent

        async def patched_get_executor(agent):
            executor = await original_get_executor(agent)
            # for_each qualifies the per-iteration name ("processor[0]") before
            # resolving the executor, so match the base agent name by prefix.
            if agent.name.startswith("processor"):
                original_exec = executor.execute

                async def slow_exec(*args, **kwargs):
                    nonlocal call_count
                    call_count += 1
                    # Make every other item slow
                    if call_count % 2 == 0:
                        await asyncio.sleep(10)
                    return await original_exec(*args, **kwargs)

                executor.execute = slow_exec
            return executor

        with patch.object(engine, "_get_executor_for_agent", side_effect=patched_get_executor):
            result = await engine.run({})

        # Workflow should complete (continue_on_error)
        assert result is not None
        # At least one agent_timeout should have been emitted
        assert "agent_timeout" in collector.types()

    @pytest.mark.asyncio
    async def test_timeout_in_for_each_fail_fast(self) -> None:
        """Timed-out agent in fail_fast for-each group fails the group."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="for-each-timeout-fail",
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
                        "max_concurrent": 1,
                        "failure_mode": "fail_fast",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process: {{ item }}",
                            "timeout_seconds": 1.0,
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "$end"}],
                    }
                ),
            ],
            output={"result": "done"},
        )

        def mock_handler(agent, prompt, context):
            if agent.name == "finder":
                return {"items": ["item1"]}
            return {"result": "processed"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        original_get_executor = engine._get_executor_for_agent

        async def patched_get_executor(agent):
            executor = await original_get_executor(agent)
            # for_each qualifies the per-iteration name ("processor[0]") before
            # resolving the executor, so match the base agent name by prefix.
            if agent.name.startswith("processor"):
                original_exec = executor.execute

                async def slow_exec(*args, **kwargs):
                    await asyncio.sleep(10)
                    return await original_exec(*args, **kwargs)

                executor.execute = slow_exec
            return executor

        with (
            patch.object(engine, "_get_executor_for_agent", side_effect=patched_get_executor),
            pytest.raises(Exception) as exc_info,
        ):
            await engine.run({})

        error_str = str(exc_info.value)
        assert "timeout" in error_str.lower() or "processor" in error_str
