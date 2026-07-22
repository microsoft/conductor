"""End-to-end tests for session-seconds usage surfacing (issue #284, E6, FR7).

Verifies that when a provider's `AgentOutput.session_seconds` is set (as the
`aca` provider populates it from the runner's terminal `result` frame), the
engine records a separate `"<agent> (sandbox)"` usage row — cost `None`,
`elapsed_seconds = session_seconds` — without disturbing the primary row's
tokens/cost. Mirrors the `"(validator)"` row integration coverage in
`test_validator_integration.py`, but drives `provider.execute` directly (like
`test_pricing_hook.py`) since no real provider other than `aca` ever sets
`session_seconds`, and `CopilotProvider`'s `mock_handler` only controls output
content, not the full `AgentOutput`.
"""

from __future__ import annotations

from typing import Any

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
from conductor.engine.usage import AgentUsage, UsageTracker
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.base import AgentOutput
from conductor.providers.copilot import CopilotProvider


def _sandbox_rows(engine: WorkflowEngine) -> list[AgentUsage]:
    return [
        a for a in engine.usage_tracker.get_summary().agents if a.agent_name.endswith("(sandbox)")
    ]


class TestUsageTrackerRecordSandbox:
    """Unit tests for `UsageTracker.record_sandbox` (E6-T2)."""

    def test_record_sandbox_row_has_no_cost_or_tokens(self) -> None:
        tracker = UsageTracker()

        usage = tracker.record_sandbox("worker (sandbox)", 12.5)

        assert usage.agent_name == "worker (sandbox)"
        assert usage.model is None
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.cache_read_tokens == 0
        assert usage.cache_write_tokens == 0
        assert usage.cost_usd is None
        assert usage.elapsed_seconds == 12.5

    def test_record_sandbox_does_not_affect_total_cost(self) -> None:
        """A sandbox row must not be double-counted as an unpriced *token* row."""
        tracker = UsageTracker()
        output = AgentOutput(
            content={},
            raw_response="{}",
            input_tokens=100,
            output_tokens=50,
            model="gpt-4",
        )
        tracker.record("worker", output, elapsed=1.0)
        tracker.record_sandbox("worker (sandbox)", 9.0)

        summary = tracker.get_summary()
        assert summary.total_cost_usd is not None
        assert summary.total_cost_usd > 0
        # The sandbox row consumed no tokens, so it is not flagged unpriced.
        assert summary.unpriced_agents == []


def _make_config() -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="aca-usage-main-loop",
            entry_point="agent1",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=5),
        ),
        agents=[
            AgentDef(
                name="agent1",
                model="gpt-4",
                prompt="do the thing",
                output={"result": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={"result": "{{ agent1.output.result }}"},
    )


def _output_with_session_seconds(session_seconds: float) -> AgentOutput:
    return AgentOutput(
        content={"result": "done"},
        raw_response="{}",
        input_tokens=100,
        output_tokens=50,
        model="gpt-4",
        session_seconds=session_seconds,
    )


class TestSandboxUsageMainLoop:
    @pytest.mark.asyncio
    async def test_session_seconds_produces_distinct_sandbox_row(self) -> None:
        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {"result": "done"})

        async def fake_execute(*_args: object, **_kwargs: object) -> AgentOutput:
            return _output_with_session_seconds(30.0)

        provider.execute = fake_execute  # type: ignore[assignment]

        engine = WorkflowEngine(_make_config(), provider)
        await engine.run({})

        rows = _sandbox_rows(engine)
        assert len(rows) == 1
        sandbox_row = rows[0]
        assert sandbox_row.agent_name == "agent1 (sandbox)"
        assert sandbox_row.cost_usd is None
        assert sandbox_row.elapsed_seconds == 30.0
        assert sandbox_row.input_tokens == 0
        assert sandbox_row.output_tokens == 0

        primary_row = next(
            a for a in engine.usage_tracker.get_summary().agents if a.agent_name == "agent1"
        )
        assert primary_row.input_tokens == 100
        assert primary_row.output_tokens == 50
        assert primary_row.cost_usd is not None
        assert primary_row.cost_usd > 0

    @pytest.mark.asyncio
    async def test_session_seconds_none_produces_no_sandbox_row(self) -> None:
        """Non-`aca` providers never set `session_seconds`; no sandbox row appears."""
        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {"result": "done"})

        engine = WorkflowEngine(_make_config(), provider)
        await engine.run({})

        assert _sandbox_rows(engine) == []
        primary_row = next(
            a for a in engine.usage_tracker.get_summary().agents if a.agent_name == "agent1"
        )
        assert primary_row.cost_usd is not None


class TestSandboxUsageParallel:
    @pytest.mark.asyncio
    async def test_session_seconds_recorded_per_parallel_member(self) -> None:
        async def fake_execute(agent: AgentDef, *_args: object, **_kwargs: object) -> AgentOutput:
            if agent.name == "worker":
                return _output_with_session_seconds(15.0)
            return AgentOutput(content={"result": "no-sandbox"}, raw_response="{}", model="gpt-4")

        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {})
        provider.execute = fake_execute  # type: ignore[assignment]

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="aca-usage-parallel",
                entry_point="team",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="worker",
                    model="gpt-4",
                    prompt="work",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="sidekick",
                    model="gpt-4",
                    prompt="assist",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="team", agents=["worker", "sidekick"], routes=[RouteDef(to="$end")]
                ),
            ],
            output={"done": "true"},
        )
        engine = WorkflowEngine(config, provider)
        await engine.run({})

        rows = _sandbox_rows(engine)
        assert [r.agent_name for r in rows] == ["worker (sandbox)"]
        assert rows[0].elapsed_seconds == 15.0
        assert rows[0].cost_usd is None


class TestSandboxUsageForEach:
    @pytest.mark.asyncio
    async def test_session_seconds_recorded_per_item(self) -> None:
        seconds_by_item = {"a": 5.0, "b": 7.0}

        async def fake_execute(
            agent: AgentDef, context: dict[str, Any], **_kwargs: object
        ) -> AgentOutput:
            if agent.name == "finder":
                return AgentOutput(content={"items": ["a", "b"]}, raw_response="{}", model="gpt-4")
            item = context.get("item")
            return _output_with_session_seconds(seconds_by_item[item])

        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {})
        provider.execute = fake_execute  # type: ignore[assignment]

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="aca-usage-for-each",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=20),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="find",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="process")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="process",
                    type="for_each",
                    source="finder.output.items",
                    **{"as": "item"},
                    agent=AgentDef(
                        name="processor",
                        model="gpt-4",
                        prompt="process {{ item }}",
                        output={"result": OutputField(type="string")},
                    ),
                    max_concurrent=1,
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"done": "true"},
        )
        engine = WorkflowEngine(config, provider)
        await engine.run({})

        rows = {r.agent_name: r for r in _sandbox_rows(engine)}
        assert set(rows) == {"process[0] (sandbox)", "process[1] (sandbox)"}
        assert rows["process[0] (sandbox)"].elapsed_seconds == 5.0
        assert rows["process[1] (sandbox)"].elapsed_seconds == 7.0
        assert all(r.cost_usd is None for r in rows.values())
