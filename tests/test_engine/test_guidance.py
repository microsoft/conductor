"""Tests for mid-run guidance (issue #400).

Covers:
- GuidanceChannel submit/drain/pending/event semantics
- Guidance submitted mid-run lands in the next agent's guidance_section
- Root-only loop-top drain (sub-workflow engines never drain the channel)
- A child engine shares the parent engine's guidance channel
- _handle_web_pause wakes on the guidance arm, drains it, and returns
  WebPauseOutcome(handled=True, guidance=[...])
- Copilot follow-up used when an interrupted session exists (no re-execute);
  fallback to a full re-execution when it is absent
- Parallel and for-each group members receive the guidance section
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    LimitsConfig,
    OutputField,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.guidance import GuidanceChannel
from conductor.engine.workflow import WebPauseOutcome, WorkflowEngine
from conductor.providers.base import AgentOutput
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


@pytest.fixture
def for_each_workflow_config() -> WorkflowConfig:
    """Workflow with a finder agent feeding a for-each group."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="for-each-workflow",
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


class TestGuidanceChannel:
    """Unit tests for GuidanceChannel semantics."""

    def test_submit_appends_and_sets_event(self) -> None:
        channel = GuidanceChannel()
        assert channel.pending == 0
        assert not channel.event.is_set()

        count = channel.submit("first")
        assert count == 1
        assert channel.pending == 1
        assert channel.event.is_set()

        count = channel.submit("second")
        assert count == 2
        assert channel.pending == 2

    def test_drain_pops_all_and_clears_event(self) -> None:
        channel = GuidanceChannel()
        channel.submit("a")
        channel.submit("b")

        drained = channel.drain()
        assert drained == ["a", "b"]
        assert channel.pending == 0
        assert not channel.event.is_set()

    def test_drain_empty_is_noop(self) -> None:
        channel = GuidanceChannel()
        assert channel.drain() == []
        assert not channel.event.is_set()

    def test_submit_after_drain_resets_event(self) -> None:
        channel = GuidanceChannel()
        channel.submit("a")
        channel.drain()
        assert not channel.event.is_set()

        channel.submit("b")
        assert channel.event.is_set()
        assert channel.drain() == ["b"]


class TestSubmitGuidanceReachesNextAgent:
    """Guidance submitted via the channel lands in the next agent's prompt."""

    @pytest.mark.asyncio
    async def test_guidance_applied_before_next_agent(
        self, two_agent_config: WorkflowConfig
    ) -> None:
        """Guidance queued while 'planner' runs is applied before 'executor' runs."""
        received_sections: dict[str, str | None] = {}

        def mock_handler(agent, prompt, context):
            key = list(agent.output.keys())[0]
            return {key: f"result from {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(two_agent_config, provider)

        original_execute = engine.executor.execute

        async def mock_execute(
            agent,
            context,
            guidance_section=None,
            interrupt_signal=None,
            event_callback=None,
        ):
            received_sections[agent.name] = guidance_section
            if agent.name == "planner":
                # Queue guidance mid-run, as POST /api/guidance would.
                engine.submit_guidance("Prefer concise answers")
            return await original_execute(agent, context, guidance_section=guidance_section)

        engine.executor.execute = mock_execute

        await engine.run({"goal": "test"})

        assert received_sections["planner"] is None
        assert received_sections["executor"] is not None
        assert "Prefer concise answers" in received_sections["executor"]
        assert "Prefer concise answers" in engine.context.user_guidance

    @pytest.mark.asyncio
    async def test_guidance_applied_emits_event_with_dashboard_source(
        self, two_agent_config: WorkflowConfig
    ) -> None:
        """add_user_guidance via the channel emits guidance_applied(source=dashboard)."""
        from conductor.events import WorkflowEvent, WorkflowEventEmitter

        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)

        provider = CopilotProvider(
            mock_handler=lambda a, p, c: {list(a.output.keys())[0]: f"r-{a.name}"}
        )
        engine = WorkflowEngine(two_agent_config, provider, event_emitter=emitter)

        original_execute = engine.executor.execute

        async def mock_execute(agent, context, guidance_section=None, **kwargs):
            if agent.name == "planner":
                engine.submit_guidance("Be concise")
            return await original_execute(agent, context, guidance_section=guidance_section)

        engine.executor.execute = mock_execute

        await engine.run({"goal": "test"})

        applied = [e for e in events if e.type == "guidance_applied"]
        assert len(applied) == 1
        assert applied[0].data["text"] == "Be concise"
        assert applied[0].data["source"] == "dashboard"


class TestRootOnlyDrain:
    """_drain_pending_guidance is a no-op below the root engine."""

    def test_drain_noop_at_subworkflow_depth(self, two_agent_config: WorkflowConfig) -> None:
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        channel = GuidanceChannel()
        channel.submit("should not apply")

        engine = WorkflowEngine(
            two_agent_config,
            provider,
            _subworkflow_depth=1,
            _guidance_channel=channel,
        )
        engine._drain_pending_guidance()

        # Still pending -- the sub-engine did not drain it.
        assert channel.pending == 1
        assert engine.context.user_guidance == []

    def test_drain_applies_at_root_depth(self, two_agent_config: WorkflowConfig) -> None:
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        channel = GuidanceChannel()
        channel.submit("apply me")

        engine = WorkflowEngine(
            two_agent_config,
            provider,
            _subworkflow_depth=0,
            _guidance_channel=channel,
        )
        engine._drain_pending_guidance()

        assert channel.pending == 0
        assert "apply me" in engine.context.user_guidance


class TestChildEngineSharesChannel:
    """A sub-workflow child engine inherits the parent's guidance channel."""

    def test_child_engine_shares_parent_channel(self, two_agent_config: WorkflowConfig) -> None:
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        parent = WorkflowEngine(two_agent_config, provider)
        parent.submit_guidance("shared guidance")

        child = WorkflowEngine(
            two_agent_config,
            provider,
            _subworkflow_depth=parent._subworkflow_depth + 1,
            _guidance_channel=parent._guidance,
        )

        assert child._guidance is parent._guidance
        assert child._guidance.pending == 1


class TestHandleWebPauseGuidanceArm:
    """The guidance wait-arm in _handle_web_pause."""

    def _make_dashboard(self) -> object:
        return SimpleNamespace(
            has_connections=lambda: True,
            resume_event=asyncio.Event(),
            kill_event=asyncio.Event(),
            disconnect_event=asyncio.Event(),
        )

    @pytest.mark.asyncio
    async def test_pause_wakes_on_guidance_submission(
        self, two_agent_config: WorkflowConfig
    ) -> None:
        """Submitting guidance while paused wakes the pause and applies it."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        web_dashboard = self._make_dashboard()
        engine = WorkflowEngine(
            two_agent_config,
            provider,
            web_dashboard=web_dashboard,  # type: ignore[arg-type]
        )

        partial = AgentOutput(content={"plan": "partial"}, raw_response="x", partial=True)

        async def submit_later() -> None:
            await asyncio.sleep(0.05)
            engine.submit_guidance("Focus on edge cases")

        result, _ = await asyncio.gather(
            engine._handle_web_pause("planner", partial),
            submit_later(),
        )

        assert isinstance(result, WebPauseOutcome)
        assert result.handled is True
        assert result.guidance == ["Focus on edge cases"]
        assert "Focus on edge cases" in engine.context.user_guidance

    @pytest.mark.asyncio
    async def test_pause_emits_agent_resumed_with_guidance_flag(
        self, two_agent_config: WorkflowConfig
    ) -> None:
        from conductor.events import WorkflowEvent, WorkflowEventEmitter

        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)

        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        web_dashboard = self._make_dashboard()
        engine = WorkflowEngine(
            two_agent_config,
            provider,
            web_dashboard=web_dashboard,  # type: ignore[arg-type]
            event_emitter=emitter,
        )

        partial = AgentOutput(content={"plan": "partial"}, raw_response="x", partial=True)

        async def submit_later() -> None:
            await asyncio.sleep(0.05)
            engine.submit_guidance("guidance text")

        await asyncio.gather(
            engine._handle_web_pause("planner", partial),
            submit_later(),
        )

        resumed = [e for e in events if e.type == "agent_resumed"]
        assert len(resumed) == 1
        assert resumed[0].data["with_guidance"] is True

        applied = [e for e in events if e.type == "guidance_applied"]
        assert len(applied) == 1
        assert applied[0].data["source"] == "dashboard"

    @pytest.mark.asyncio
    async def test_plain_resume_reports_no_guidance(self, two_agent_config: WorkflowConfig) -> None:
        """A plain Resume click (no guidance) still returns handled=True, guidance=[]."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        web_dashboard = self._make_dashboard()
        engine = WorkflowEngine(
            two_agent_config,
            provider,
            web_dashboard=web_dashboard,  # type: ignore[arg-type]
        )

        partial = AgentOutput(content={"plan": "partial"}, raw_response="x", partial=True)

        async def resume_later() -> None:
            await asyncio.sleep(0.05)
            web_dashboard.resume_event.set()  # type: ignore[attr-defined]

        result, _ = await asyncio.gather(
            engine._handle_web_pause("planner", partial),
            resume_later(),
        )

        assert result.handled is True
        assert result.guidance == []

    @pytest.mark.asyncio
    async def test_concurrent_pauses_sharing_channel_do_not_double_claim_guidance(
        self, two_agent_config: WorkflowConfig
    ) -> None:
        """Two engines sharing one GuidanceChannel both wake on one submission,
        but only the one that actually drains text reports guidance applied —
        the "loser" must not emit a misleading with_guidance=True (issue #400
        review: concurrent for-each branches share a single broadcast Event,
        and drain() is destructive).
        """
        from conductor.events import WorkflowEvent, WorkflowEventEmitter

        shared_channel = GuidanceChannel()
        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)

        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        dashboard_a = self._make_dashboard()
        dashboard_b = self._make_dashboard()
        engine_a = WorkflowEngine(
            two_agent_config,
            provider,
            web_dashboard=dashboard_a,  # type: ignore[arg-type]
            event_emitter=emitter,
            _guidance_channel=shared_channel,
        )
        engine_b = WorkflowEngine(
            two_agent_config,
            provider,
            web_dashboard=dashboard_b,  # type: ignore[arg-type]
            event_emitter=emitter,
            _guidance_channel=shared_channel,
        )
        assert engine_a._guidance is engine_b._guidance is shared_channel

        partial = AgentOutput(content={"plan": "partial"}, raw_response="x", partial=True)

        async def submit_once() -> None:
            await asyncio.sleep(0.05)
            shared_channel.submit("single correction")

        result_a, result_b, _ = await asyncio.gather(
            engine_a._handle_web_pause("planner", partial),
            engine_b._handle_web_pause("planner", partial),
            submit_once(),
        )

        # Exactly one of the two engines actually drained the text; the other
        # must resolve as a plain resume rather than falsely reporting the
        # guidance as applied to it too.
        results = [result_a, result_b]
        with_guidance = [r for r in results if r.guidance]
        without_guidance = [r for r in results if not r.guidance]
        assert len(with_guidance) == 1
        assert with_guidance[0].guidance == ["single correction"]
        assert len(without_guidance) == 1
        assert without_guidance[0].handled is True

        # Only one guidance_applied and one "with_guidance: True" agent_resumed
        # event should have fired — the loser must not emit either.
        applied = [e for e in events if e.type == "guidance_applied"]
        assert len(applied) == 1

        resumed = [e for e in events if e.type == "agent_resumed"]
        assert len(resumed) == 2
        with_guidance_events = [e for e in resumed if e.data["with_guidance"] is True]
        without_guidance_events = [e for e in resumed if e.data["with_guidance"] is False]
        assert len(with_guidance_events) == 1
        assert len(without_guidance_events) == 1


class TestSendGuidanceFollowup:
    """_send_guidance_followup: Copilot in-place resume vs. fallback re-execute."""

    @pytest.mark.asyncio
    async def test_returns_followup_output_when_session_present(
        self, two_agent_config: WorkflowConfig
    ) -> None:
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(two_agent_config, provider)

        fake_session = MagicMock()
        provider._interrupted_session = fake_session

        followup_output = AgentOutput(content={"plan": "final"}, raw_response="ok")

        async def fake_send_followup(session, guidance_text, *, agent_name, agent_model):
            assert session is fake_session
            assert guidance_text == "more guidance"
            return followup_output

        provider.send_followup = fake_send_followup  # type: ignore[method-assign]

        agent = engine._find_agent("planner")
        assert agent is not None
        result = await engine._send_guidance_followup(agent, engine.executor, "more guidance")

        assert result is followup_output
        # get_interrupted_session clears the stored session on read.
        assert provider._interrupted_session is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_interrupted_session(
        self, two_agent_config: WorkflowConfig
    ) -> None:
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        engine = WorkflowEngine(two_agent_config, provider)

        agent = engine._find_agent("planner")
        assert agent is not None
        result = await engine._send_guidance_followup(agent, engine.executor, "guidance")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_non_copilot_provider(
        self, two_agent_config: WorkflowConfig
    ) -> None:
        from conductor.providers.base import AgentProvider

        class OtherProvider(AgentProvider, abstract=True):
            async def execute(self, agent, context, **kwargs):
                return AgentOutput(content={"x": "y"}, raw_response="ok")

            async def validate_connection(self) -> bool:
                return True

            async def close(self) -> None:
                pass

        provider = OtherProvider()
        engine = WorkflowEngine(two_agent_config, provider)

        agent = engine._find_agent("planner")
        assert agent is not None
        result = await engine._send_guidance_followup(agent, engine.executor, "guidance")

        assert result is None


class TestParallelAndForEachReceiveGuidance:
    """Q3: parallel/for-each members render with the current guidance section."""

    @pytest.mark.asyncio
    async def test_parallel_members_receive_guidance_section(
        self, parallel_workflow_config: WorkflowConfig
    ) -> None:
        received_sections: dict[str, str | None] = {}

        def mock_handler(agent, prompt, context):
            key = list(agent.output.keys())[0]
            return {key: f"finding from {agent.name}"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(parallel_workflow_config, provider)
        engine.context.add_guidance("Only report verified facts")

        original_execute = engine.executor.execute

        async def mock_execute(agent, context, guidance_section=None, **kwargs):
            received_sections[agent.name] = guidance_section
            return await original_execute(agent, context, guidance_section=guidance_section)

        engine.executor.execute = mock_execute

        await engine.run({})

        for name in ("researcher_a", "researcher_b"):
            assert received_sections[name] is not None
            assert "Only report verified facts" in received_sections[name]

    @pytest.mark.asyncio
    async def test_for_each_members_receive_guidance_section(
        self, for_each_workflow_config: WorkflowConfig
    ) -> None:
        received_sections: dict[str, str | None] = {}

        def mock_handler(agent, prompt, context):
            if agent.name == "finder":
                return {"items": ["a", "b"]}
            key = list(agent.output.keys())[0]
            return {key: "processed"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(for_each_workflow_config, provider)

        original_execute = engine.executor.execute

        async def mock_execute(agent, context, guidance_section=None, **kwargs):
            received_sections[agent.name] = guidance_section
            if agent.name == "finder":
                engine.context.add_guidance("Skip malformed items")
            return await original_execute(agent, context, guidance_section=guidance_section)

        engine.executor.execute = mock_execute

        await engine.run({})

        processor_sections = [v for k, v in received_sections.items() if k.startswith("processor[")]
        assert len(processor_sections) == 2
        for section in processor_sections:
            assert section is not None
            assert "Skip malformed items" in section
