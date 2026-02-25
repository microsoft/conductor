"""Integration tests for interrupt handling in WorkflowEngine.

Tests cover:
- Between-agent interrupt check triggers on event
- All four interrupt actions: continue, skip, stop, cancel
- Guidance injection from continue action
- Skip-to-agent overrides routing
- Stop raises InterruptError with checkpoint saved
- Cancel resumes execution normally
- Interrupt queuing during parallel/for-each group execution
- Backward compatibility when interrupt_event is None
- Ctrl+C (KeyboardInterrupt) is unaffected by interrupt handling
"""

import asyncio
from unittest.mock import patch

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
from conductor.exceptions import InterruptError
from conductor.gates.interrupt import InterruptAction, InterruptResult
from conductor.providers.copilot import CopilotProvider


@pytest.fixture
def two_agent_config() -> WorkflowConfig:
    """Workflow with two sequential agents: planner -> executor."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="two-agent",
            entry_point="planner",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            AgentDef(
                name="planner",
                model="gpt-4",
                prompt="Plan: {{ workflow.input.goal }}",
                output={"plan": OutputField(type="string")},
                routes=[RouteDef(to="executor")],
            ),
            AgentDef(
                name="executor",
                model="gpt-4",
                prompt="Execute: {{ planner.output.plan }}",
                output={"result": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={"result": "{{ executor.output.result }}"},
    )


@pytest.fixture
def three_agent_config() -> WorkflowConfig:
    """Workflow with three sequential agents: a -> b -> c."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="three-agent",
            entry_point="agent_a",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            AgentDef(
                name="agent_a",
                model="gpt-4",
                prompt="Agent A: {{ workflow.input.question }}",
                output={"answer_a": OutputField(type="string")},
                routes=[RouteDef(to="agent_b")],
            ),
            AgentDef(
                name="agent_b",
                model="gpt-4",
                prompt="Agent B: {{ agent_a.output.answer_a }}",
                output={"answer_b": OutputField(type="string")},
                routes=[RouteDef(to="agent_c")],
            ),
            AgentDef(
                name="agent_c",
                model="gpt-4",
                prompt="Agent C: {{ workflow.input.question }}",
                output={"answer_c": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={"answer": "{{ agent_c.output.answer_c }}"},
    )


@pytest.fixture
def parallel_workflow_config() -> WorkflowConfig:
    """Workflow with a parallel group followed by a finalizer agent."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="parallel-workflow",
            entry_point="researchers",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            AgentDef(
                name="researcher_a",
                model="gpt-4",
                prompt="Research A",
                output={"finding": OutputField(type="string")},
            ),
            AgentDef(
                name="researcher_b",
                model="gpt-4",
                prompt="Research B",
                output={"finding": OutputField(type="string")},
            ),
            AgentDef(
                name="finalizer",
                model="gpt-4",
                prompt="Finalize: {{ researchers.outputs }}",
                output={"summary": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
        ],
        parallel=[
            ParallelGroup(
                name="researchers",
                agents=["researcher_a", "researcher_b"],
                routes=[RouteDef(to="finalizer")],
            ),
        ],
        output={"summary": "{{ finalizer.output.summary }}"},
    )


class TestInterruptBetweenAgents:
    """Tests for between-agent interrupt handling."""

    @pytest.mark.asyncio
    async def test_no_interrupt_when_event_is_none(self, two_agent_config: WorkflowConfig) -> None:
        """Engine runs normally when interrupt_event is None (backward compat)."""
        responses = {
            "planner": {"plan": "the plan"},
            "executor": {"result": "done"},
        }

        provider = CopilotProvider(mock_handler=lambda a, p, c: responses[a.name])
        engine = WorkflowEngine(two_agent_config, provider, interrupt_event=None)

        result = await engine.run({"goal": "test"})
        assert result["result"] == "done"

    @pytest.mark.asyncio
    async def test_no_interrupt_when_event_not_set(self, two_agent_config: WorkflowConfig) -> None:
        """Engine runs normally when interrupt_event exists but is not set."""
        responses = {
            "planner": {"plan": "the plan"},
            "executor": {"result": "done"},
        }

        event = asyncio.Event()
        provider = CopilotProvider(mock_handler=lambda a, p, c: responses[a.name])
        engine = WorkflowEngine(two_agent_config, provider, interrupt_event=event)

        result = await engine.run({"goal": "test"})
        assert result["result"] == "done"

    @pytest.mark.asyncio
    async def test_cancel_action_resumes_normally(self, two_agent_config: WorkflowConfig) -> None:
        """Cancel action lets execution continue normally."""
        responses = {
            "planner": {"plan": "the plan"},
            "executor": {"result": "done"},
        }
        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            return responses[agent.name]

        event = asyncio.Event()
        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(two_agent_config, provider, interrupt_event=event)

        # Set the event so it triggers after the first agent
        def set_event_after_first(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            result = responses[agent.name]
            if agent.name == "planner":
                event.set()
            return result

        provider._mock_handler = set_event_after_first

        cancel_result = InterruptResult(action=InterruptAction.CANCEL)
        with patch.object(
            engine._interrupt_handler, "handle_interrupt", return_value=cancel_result
        ):
            result = await engine.run({"goal": "test"})

        assert result["result"] == "done"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_continue_with_guidance_injects_guidance(
        self, three_agent_config: WorkflowConfig
    ) -> None:
        """Continue action adds guidance that appears in subsequent agent prompts."""
        received_prompts: dict[str, str] = {}

        def mock_handler(agent, prompt, context):
            received_prompts[agent.name] = prompt
            return {
                "answer_a": "answer from a",
                "answer_b": "answer from b",
                "answer_c": "final answer",
            }.get(list(agent.output.keys())[0], {})
            # Return single-key dicts matching output schema
            key = list(agent.output.keys())[0]
            return {key: f"answer from {agent.name}"}

        def mock_handler_proper(agent, prompt, context):
            received_prompts[agent.name] = prompt
            key = list(agent.output.keys())[0]
            return {key: f"answer from {agent.name}"}

        event = asyncio.Event()
        provider = CopilotProvider(mock_handler=mock_handler_proper)
        engine = WorkflowEngine(three_agent_config, provider, interrupt_event=event)

        # Trigger interrupt after agent_a
        original_execute = engine.executor.execute

        async def mock_execute(agent, context, guidance_section=None, interrupt_signal=None):
            result = await original_execute(agent, context, guidance_section=guidance_section)
            if agent.name == "agent_a":
                event.set()
            return result

        engine.executor.execute = mock_execute

        guidance_result = InterruptResult(
            action=InterruptAction.CONTINUE,
            guidance="Focus on Python 3 only",
        )
        with patch.object(
            engine._interrupt_handler, "handle_interrupt", return_value=guidance_result
        ):
            await engine.run({"question": "test"})

        # Guidance should be accumulated in context
        assert "Focus on Python 3 only" in engine.context.user_guidance
        # agent_b's prompt should contain the guidance
        assert "[User Guidance]" in received_prompts["agent_b"]
        assert "Focus on Python 3 only" in received_prompts["agent_b"]

    @pytest.mark.asyncio
    async def test_skip_to_agent_overrides_routing(
        self, three_agent_config: WorkflowConfig
    ) -> None:
        """Skip action routes to the specified agent, bypassing normal routing."""
        executed_agents: list[str] = []

        def mock_handler(agent, prompt, context):
            executed_agents.append(agent.name)
            key = list(agent.output.keys())[0]
            return {key: f"answer from {agent.name}"}

        event = asyncio.Event()
        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(three_agent_config, provider, interrupt_event=event)

        # Trigger interrupt after agent_a, skip to agent_c
        original_execute = engine.executor.execute

        async def mock_execute(agent, context, guidance_section=None, interrupt_signal=None):
            result = await original_execute(agent, context, guidance_section=guidance_section)
            if agent.name == "agent_a":
                event.set()
            return result

        engine.executor.execute = mock_execute

        skip_result = InterruptResult(
            action=InterruptAction.SKIP,
            skip_target="agent_c",
        )
        with patch.object(engine._interrupt_handler, "handle_interrupt", return_value=skip_result):
            result = await engine.run({"question": "test"})

        # agent_b should be skipped
        assert executed_agents == ["agent_a", "agent_c"]
        assert result["answer"] == "answer from agent_c"

    @pytest.mark.asyncio
    async def test_stop_raises_interrupt_error(self, two_agent_config: WorkflowConfig) -> None:
        """Stop action raises InterruptError."""
        event = asyncio.Event()

        def mock_handler(agent, prompt, context):
            if agent.name == "planner":
                event.set()
            return {"plan": "the plan", "result": "done"}.get(list(agent.output.keys())[0], {})

        def mock_handler_proper(agent, prompt, context):
            if agent.name == "planner":
                event.set()
            key = list(agent.output.keys())[0]
            return {key: f"result from {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler_proper)
        engine = WorkflowEngine(two_agent_config, provider, interrupt_event=event)

        stop_result = InterruptResult(action=InterruptAction.STOP)
        with (
            patch.object(engine._interrupt_handler, "handle_interrupt", return_value=stop_result),
            pytest.raises(InterruptError, match="Workflow stopped by user interrupt"),
        ):
            await engine.run({"goal": "test"})

    @pytest.mark.asyncio
    async def test_stop_saves_checkpoint(self, two_agent_config: WorkflowConfig) -> None:
        """Stop action triggers checkpoint save via ConductorError handler."""
        event = asyncio.Event()

        def mock_handler(agent, prompt, context):
            if agent.name == "planner":
                event.set()
            key = list(agent.output.keys())[0]
            return {key: f"result from {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(
            two_agent_config,
            provider,
            interrupt_event=event,
            workflow_path=two_agent_config.workflow.name,
        )

        stop_result = InterruptResult(action=InterruptAction.STOP)
        with (
            patch.object(engine._interrupt_handler, "handle_interrupt", return_value=stop_result),
            patch.object(engine, "_save_checkpoint_on_failure") as mock_checkpoint,
            pytest.raises(InterruptError),
        ):
            await engine.run({"goal": "test"})

        # InterruptError is a subclass of ConductorError, so checkpoint should be saved
        mock_checkpoint.assert_called_once()
        saved_error = mock_checkpoint.call_args[0][0]
        assert isinstance(saved_error, InterruptError)

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_still_works(self, two_agent_config: WorkflowConfig) -> None:
        """Ctrl+C (KeyboardInterrupt) is distinct from InterruptError."""
        event = asyncio.Event()

        def mock_handler(agent, prompt, context):
            if agent.name == "executor":
                raise KeyboardInterrupt()
            return {"plan": "the plan"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(two_agent_config, provider, interrupt_event=event)

        with pytest.raises(KeyboardInterrupt):
            await engine.run({"goal": "test"})

    @pytest.mark.asyncio
    async def test_interrupt_handler_receives_correct_args(
        self, three_agent_config: WorkflowConfig
    ) -> None:
        """Verify the handler receives current agent, iteration, preview, etc."""
        event = asyncio.Event()

        def mock_handler(agent, prompt, context):
            if agent.name == "agent_a":
                event.set()
            key = list(agent.output.keys())[0]
            return {key: f"result from {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(three_agent_config, provider, interrupt_event=event)

        cancel_result = InterruptResult(action=InterruptAction.CANCEL)
        with patch.object(
            engine._interrupt_handler,
            "handle_interrupt",
            return_value=cancel_result,
        ) as mock_handle:
            await engine.run({"question": "test"})

        mock_handle.assert_called_once()
        call_kwargs = mock_handle.call_args[1] if mock_handle.call_args[1] else {}
        call_args = mock_handle.call_args[0] if mock_handle.call_args[0] else ()

        # Positional or keyword - get all the args
        if call_args:
            current_agent = call_args[0]
            iteration = call_args[1]
            last_output_preview = call_args[2]
            available_agents = call_args[3]
            accumulated_guidance = call_args[4]
        else:
            current_agent = call_kwargs["current_agent"]
            iteration = call_kwargs["iteration"]
            last_output_preview = call_kwargs["last_output_preview"]
            available_agents = call_kwargs["available_agents"]
            accumulated_guidance = call_kwargs["accumulated_guidance"]

        # The interrupt fires after agent_a, so next agent is agent_b
        assert current_agent == "agent_b"
        assert iteration == 1  # One agent has completed
        assert last_output_preview is not None
        assert "result from agent_a" in last_output_preview
        # Available agents should be all top-level agents
        assert available_agents == ["agent_a", "agent_b", "agent_c"]
        assert accumulated_guidance == []

    @pytest.mark.asyncio
    async def test_multiple_interrupts_accumulate_guidance(
        self, three_agent_config: WorkflowConfig
    ) -> None:
        """Multiple interrupt-and-continue cycles accumulate guidance."""
        event = asyncio.Event()
        interrupt_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal interrupt_count
            event.set()
            interrupt_count += 1
            key = list(agent.output.keys())[0]
            return {key: f"result from {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(three_agent_config, provider, interrupt_event=event)

        guidance_results = iter(
            [
                InterruptResult(action=InterruptAction.CONTINUE, guidance="Guidance 1"),
                InterruptResult(action=InterruptAction.CONTINUE, guidance="Guidance 2"),
                InterruptResult(action=InterruptAction.CANCEL),
            ]
        )

        with patch.object(
            engine._interrupt_handler,
            "handle_interrupt",
            side_effect=lambda *a, **kw: next(guidance_results),
        ):
            await engine.run({"question": "test"})

        assert engine.context.user_guidance == ["Guidance 1", "Guidance 2"]

    @pytest.mark.asyncio
    async def test_skip_gates_auto_cancels_interrupt(
        self, two_agent_config: WorkflowConfig
    ) -> None:
        """When skip_gates=True, interrupt handler auto-cancels."""
        event = asyncio.Event()

        def mock_handler(agent, prompt, context):
            if agent.name == "planner":
                event.set()
            key = list(agent.output.keys())[0]
            return {key: f"result from {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(two_agent_config, provider, interrupt_event=event, skip_gates=True)

        # skip_gates=True should auto-cancel - the interrupt handler returns CANCEL
        result = await engine.run({"goal": "test"})
        assert result["result"] == "result from executor"


class TestInterruptDuringParallelGroup:
    """Tests for interrupt queuing during parallel/for-each groups."""

    @pytest.mark.asyncio
    async def test_interrupt_during_parallel_deferred(
        self, parallel_workflow_config: WorkflowConfig
    ) -> None:
        """Interrupt fired during parallel group is handled after group completes."""
        event = asyncio.Event()
        handler_called = False

        def mock_handler(agent, prompt, context):
            if agent.name == "researcher_a":
                # Set interrupt during parallel execution
                event.set()
            key = list(agent.output.keys())[0]
            return {key: f"finding from {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(parallel_workflow_config, provider, interrupt_event=event)

        async def mock_handle_interrupt(*args, **kwargs):
            nonlocal handler_called
            handler_called = True
            return InterruptResult(action=InterruptAction.CANCEL)

        with patch.object(
            engine._interrupt_handler,
            "handle_interrupt",
            side_effect=mock_handle_interrupt,
        ):
            result = await engine.run({"goal": "test"})

        # The interrupt handler should be called after the parallel group completes
        assert handler_called
        # Both parallel agents should have executed
        assert "researchers" in engine.context.agent_outputs
        # Finalizer should have run because we cancelled the interrupt
        assert result["summary"] == "finding from finalizer"

    @pytest.mark.asyncio
    async def test_interrupt_during_parallel_stop_before_next(
        self, parallel_workflow_config: WorkflowConfig
    ) -> None:
        """Interrupt with stop during parallel group stops before next agent."""
        event = asyncio.Event()

        def mock_handler(agent, prompt, context):
            if agent.name == "researcher_a":
                event.set()
            key = list(agent.output.keys())[0]
            return {key: f"finding from {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(parallel_workflow_config, provider, interrupt_event=event)

        stop_result = InterruptResult(action=InterruptAction.STOP)
        with (
            patch.object(engine._interrupt_handler, "handle_interrupt", return_value=stop_result),
            pytest.raises(InterruptError),
        ):
            await engine.run({"goal": "test"})

        # Parallel group should have completed, but finalizer should not have run
        assert "researchers" in engine.context.agent_outputs
        assert "finalizer" not in engine.context.agent_outputs


class TestCheckInterruptMethod:
    """Tests for the _check_interrupt helper method."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_event(self, two_agent_config: WorkflowConfig) -> None:
        """Returns None when interrupt_event is None."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(two_agent_config, provider, interrupt_event=None)

        result = await engine._check_interrupt("some_agent")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_not_set(self, two_agent_config: WorkflowConfig) -> None:
        """Returns None when interrupt_event exists but is not set."""
        event = asyncio.Event()
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(two_agent_config, provider, interrupt_event=event)

        result = await engine._check_interrupt("some_agent")
        assert result is None

    @pytest.mark.asyncio
    async def test_clears_event_on_interrupt(self, two_agent_config: WorkflowConfig) -> None:
        """Event is cleared when interrupt is handled."""
        event = asyncio.Event()
        event.set()
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(two_agent_config, provider, interrupt_event=event)

        cancel_result = InterruptResult(action=InterruptAction.CANCEL)
        with patch.object(
            engine._interrupt_handler, "handle_interrupt", return_value=cancel_result
        ):
            await engine._check_interrupt("some_agent")

        assert not event.is_set()

    @pytest.mark.asyncio
    async def test_builds_output_preview_from_context(
        self, two_agent_config: WorkflowConfig
    ) -> None:
        """Output preview is built from the latest agent output in context."""
        event = asyncio.Event()
        event.set()
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(two_agent_config, provider, interrupt_event=event)

        # Store some output in context
        engine.context.store("planner", {"plan": "my detailed plan"})

        cancel_result = InterruptResult(action=InterruptAction.CANCEL)
        with patch.object(
            engine._interrupt_handler,
            "handle_interrupt",
            return_value=cancel_result,
        ) as mock_handle:
            await engine._check_interrupt("executor")

        call_kwargs = mock_handle.call_args
        preview = call_kwargs[1].get("last_output_preview") or call_kwargs[0][2]
        assert "my detailed plan" in preview

    @pytest.mark.asyncio
    async def test_no_output_preview_when_context_empty(
        self, two_agent_config: WorkflowConfig
    ) -> None:
        """Output preview is None when no agents have executed yet."""
        event = asyncio.Event()
        event.set()
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(two_agent_config, provider, interrupt_event=event)

        cancel_result = InterruptResult(action=InterruptAction.CANCEL)
        with patch.object(
            engine._interrupt_handler,
            "handle_interrupt",
            return_value=cancel_result,
        ) as mock_handle:
            await engine._check_interrupt("planner")

        call_kwargs = mock_handle.call_args
        # handle_interrupt uses keyword arguments
        preview = call_kwargs[1].get(
            "last_output_preview",
            call_kwargs[0][2] if len(call_kwargs[0]) > 2 else None,
        )
        assert preview is None


class TestGetTopLevelAgentNames:
    """Tests for _get_top_level_agent_names helper."""

    @pytest.mark.asyncio
    async def test_returns_all_top_level_agents(self, three_agent_config: WorkflowConfig) -> None:
        """Returns all agents defined in config.agents."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(three_agent_config, provider)

        names = engine._get_top_level_agent_names()
        assert names == ["agent_a", "agent_b", "agent_c"]

    @pytest.mark.asyncio
    async def test_includes_agents_used_in_parallel_groups(
        self, parallel_workflow_config: WorkflowConfig
    ) -> None:
        """Agents used in parallel groups are still listed as top-level agents.

        They are defined in config.agents even though they are referenced
        by parallel groups. The interrupt handler shows all top-level agents.
        """
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(parallel_workflow_config, provider)

        names = engine._get_top_level_agent_names()
        assert "researcher_a" in names
        assert "researcher_b" in names
        assert "finalizer" in names


class TestHandleInterruptResult:
    """Tests for _handle_interrupt_result helper."""

    @pytest.mark.asyncio
    async def test_continue_adds_guidance(self, two_agent_config: WorkflowConfig) -> None:
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(two_agent_config, provider)

        result = InterruptResult(action=InterruptAction.CONTINUE, guidance="Be concise")
        next_agent = await engine._handle_interrupt_result(result, "executor")

        assert next_agent == "executor"
        assert "Be concise" in engine.context.user_guidance

    @pytest.mark.asyncio
    async def test_continue_without_guidance(self, two_agent_config: WorkflowConfig) -> None:
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(two_agent_config, provider)

        result = InterruptResult(action=InterruptAction.CONTINUE, guidance=None)
        next_agent = await engine._handle_interrupt_result(result, "executor")

        assert next_agent == "executor"
        assert engine.context.user_guidance == []

    @pytest.mark.asyncio
    async def test_skip_returns_target(self, two_agent_config: WorkflowConfig) -> None:
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(two_agent_config, provider)

        result = InterruptResult(action=InterruptAction.SKIP, skip_target="executor")
        next_agent = await engine._handle_interrupt_result(result, "planner")

        assert next_agent == "executor"

    @pytest.mark.asyncio
    async def test_stop_raises_interrupt_error(self, two_agent_config: WorkflowConfig) -> None:
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(two_agent_config, provider)

        result = InterruptResult(action=InterruptAction.STOP)
        with pytest.raises(InterruptError) as exc_info:
            await engine._handle_interrupt_result(result, "planner")

        assert exc_info.value.agent_name == "planner"

    @pytest.mark.asyncio
    async def test_cancel_returns_same_agent(self, two_agent_config: WorkflowConfig) -> None:
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(two_agent_config, provider)

        result = InterruptResult(action=InterruptAction.CANCEL)
        next_agent = await engine._handle_interrupt_result(result, "executor")

        assert next_agent == "executor"


class TestPartialOutputHandling:
    """Tests for mid-agent interrupt partial output handling in the engine."""

    @pytest.mark.asyncio
    async def test_partial_output_triggers_interrupt_handler(
        self, two_agent_config: WorkflowConfig
    ) -> None:
        """When provider returns partial output, interrupt handler is invoked."""
        from conductor.providers.base import AgentOutput

        handler_called = False

        def mock_handler(agent, prompt, context):
            key = list(agent.output.keys())[0]
            return {key: f"result from {agent.name}"}

        event = asyncio.Event()
        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(two_agent_config, provider, interrupt_event=event)

        # Replace executor.execute to return partial output on first call
        original_execute = engine.executor.execute
        execute_calls = 0

        async def mock_execute(agent, context, guidance_section=None, interrupt_signal=None):
            nonlocal execute_calls
            execute_calls += 1
            result = await original_execute(agent, context, guidance_section=guidance_section)
            if agent.name == "planner" and execute_calls == 1:
                result = AgentOutput(
                    content={"plan": "partial plan"},
                    raw_response="partial",
                    partial=True,
                )
            return result

        engine.executor.execute = mock_execute

        cancel_result = InterruptResult(action=InterruptAction.CANCEL)

        async def mock_handle_interrupt(*args, **kwargs):
            nonlocal handler_called
            handler_called = True
            return cancel_result

        with patch.object(
            engine._interrupt_handler,
            "handle_interrupt",
            side_effect=mock_handle_interrupt,
        ):
            result = await engine.run({"goal": "test"})

        assert handler_called
        assert result["result"] == "result from executor"

    @pytest.mark.asyncio
    async def test_partial_output_continue_with_guidance_re_executes(
        self, two_agent_config: WorkflowConfig
    ) -> None:
        """When user provides guidance after partial output, agent is re-executed."""
        from conductor.providers.base import AgentOutput

        event = asyncio.Event()
        provider = CopilotProvider(
            mock_handler=lambda a, p, c: {list(a.output.keys())[0]: f"result from {a.name}"}
        )
        engine = WorkflowEngine(two_agent_config, provider, interrupt_event=event)

        original_execute = engine.executor.execute
        execute_calls: list[tuple] = []

        async def mock_execute(agent, context, guidance_section=None, interrupt_signal=None):
            execute_calls.append((agent.name, guidance_section))
            result = await original_execute(agent, context, guidance_section=guidance_section)
            if (
                agent.name == "planner"
                and len([c for c in execute_calls if c[0] == "planner"]) == 1
            ):
                return AgentOutput(
                    content={"plan": "partial plan"},
                    raw_response="partial",
                    partial=True,
                )
            return result

        engine.executor.execute = mock_execute

        guidance_result = InterruptResult(
            action=InterruptAction.CONTINUE,
            guidance="Be more specific",
        )
        with patch.object(
            engine._interrupt_handler,
            "handle_interrupt",
            return_value=guidance_result,
        ):
            await engine.run({"goal": "test"})

        # Guidance should be accumulated
        assert "Be more specific" in engine.context.user_guidance
        # Planner should have been called twice (first partial, then re-execute)
        planner_calls = [c for c in execute_calls if c[0] == "planner"]
        assert len(planner_calls) == 2

    @pytest.mark.asyncio
    async def test_partial_output_stop_raises_interrupt_error(
        self, two_agent_config: WorkflowConfig
    ) -> None:
        """When user selects stop after partial output, InterruptError is raised."""
        from conductor.providers.base import AgentOutput

        event = asyncio.Event()
        provider = CopilotProvider(
            mock_handler=lambda a, p, c: {list(a.output.keys())[0]: f"result from {a.name}"}
        )
        engine = WorkflowEngine(two_agent_config, provider, interrupt_event=event)

        original_execute = engine.executor.execute

        async def mock_execute(agent, context, guidance_section=None, interrupt_signal=None):
            result = await original_execute(agent, context, guidance_section=guidance_section)
            if agent.name == "planner":
                return AgentOutput(
                    content={"plan": "partial"},
                    raw_response="partial",
                    partial=True,
                )
            return result

        engine.executor.execute = mock_execute

        stop_result = InterruptResult(action=InterruptAction.STOP)
        with (
            patch.object(
                engine._interrupt_handler,
                "handle_interrupt",
                return_value=stop_result,
            ),
            pytest.raises(InterruptError),
        ):
            await engine.run({"goal": "test"})

    @pytest.mark.asyncio
    async def test_mock_providers_work_after_abc_change(self) -> None:
        """Verify all mock providers still instantiate and run after ABC signature change."""
        from conductor.providers.base import AgentOutput, AgentProvider

        class TestMockProvider(AgentProvider):
            async def execute(
                self,
                agent: AgentDef,
                context: dict,
                rendered_prompt: str,
                tools: list[str] | None = None,
                interrupt_signal: asyncio.Event | None = None,
            ) -> AgentOutput:
                return AgentOutput(content={"result": "mock"}, raw_response="mock")

            async def validate_connection(self) -> bool:
                return True

            async def close(self) -> None:
                pass

        provider = TestMockProvider()
        agent = AgentDef(name="test", model="gpt-4", prompt="test")
        result = await provider.execute(agent, {}, "prompt")
        assert result.content == {"result": "mock"}
        assert result.partial is False

        # With interrupt_signal
        event = asyncio.Event()
        result2 = await provider.execute(agent, {}, "prompt", interrupt_signal=event)
        assert result2.content == {"result": "mock"}
