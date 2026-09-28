"""Engine-level billing provenance: events, usage rows, summary dict, and budget wording.

A real ``WorkflowEngine`` runs against a ``CopilotProvider`` whose ``execute`` is replaced
by a scripted function returning ``AgentOutput(billing_mode=...)``. No SDK, network, or
credential is involved; the provider under test is irrelevant because the engine only sees
the ``AgentOutput`` it returns.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.billing import BillingMode
from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    LimitsConfig,
    MCPServerDef,
    MCPStepDef,
    OutputField,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    SetStepDef,
    ValidatorConfig,
    WorkflowConfig,
    WorkflowDef,
    WorkflowStepDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import BudgetExceededError
from conductor.gates.interrupt import InterruptAction, InterruptResult
from conductor.providers.base import AgentOutput
from conductor.providers.copilot import CopilotProvider

_MODEL = "claude-sonnet-4"  # priced in the static table, so cost_usd is a number

Modes = dict[str, BillingMode | None]


def _collect() -> tuple[WorkflowEventEmitter, list[WorkflowEvent]]:
    emitter = WorkflowEventEmitter()
    events: list[WorkflowEvent] = []
    emitter.subscribe(events.append)
    return emitter, events


def _of(events: list[WorkflowEvent], event_type: str) -> list[dict[str, Any]]:
    return [e.data for e in events if e.type == event_type]


def _scripted_provider(
    modes: Modes,
    *,
    contents: dict[str, dict[str, Any]] | None = None,
    tokens: dict[str, tuple[int, int]] | None = None,
    partial: set[str] | None = None,
    validator_passes: bool = True,
) -> CopilotProvider:
    """A provider whose executions report ``modes[agent_name]`` as their billing mode.

    The validator's synthetic agent is named ``"<agent> (validator)"`` and looked up under
    that name. A name absent from ``modes`` reports ``None`` (a provider that states nothing).
    A for-each item agent (``"<agent>[<key>]"``) falls back to its base name.
    """
    provider = CopilotProvider(mock_handler=lambda *_a, **_k: {})

    async def execute(agent: AgentDef, context: Any, rendered_prompt: str, **kwargs: Any) -> Any:
        base_name = agent.name.split("[", 1)[0]
        if agent.output is not None and "passed" in agent.output:
            content: dict[str, Any] = {"passed": validator_passes, "issues": []}
        elif contents and agent.name in contents:
            content = contents[agent.name]
        else:
            content = dict.fromkeys(agent.output or {}, "ok")
        input_tokens, output_tokens = (tokens or {}).get(agent.name, (100, 50))
        return AgentOutput(
            content=content,
            raw_response="",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=_MODEL,
            partial=agent.name in (partial or set()),
            billing_mode=modes.get(agent.name, modes.get(base_name)),
        )

    provider.execute = execute  # type: ignore[method-assign]
    return provider


def _agent(name: str, *, route: str = "$end", **kwargs: Any) -> AgentDef:
    return AgentDef(
        name=name,
        model=_MODEL,
        prompt=f"do {name}",
        output={"result": OutputField(type="string")},
        routes=[RouteDef(to=route)],
        **kwargs,
    )


def _config(agents: list[Any], **sections: Any) -> WorkflowConfig:
    limits = sections.pop("limits", LimitsConfig(max_iterations=20))
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="billing-it",
            entry_point=agents[0].name,
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=limits,
        ),
        agents=agents,
        output={"done": "true"},
        **sections,
    )


async def _run(
    config: WorkflowConfig,
    provider: CopilotProvider,
    *,
    workflow_path: Path | None = None,
) -> tuple[WorkflowEngine, list[WorkflowEvent]]:
    emitter, events = _collect()
    engine = WorkflowEngine(config, provider, event_emitter=emitter, workflow_path=workflow_path)
    await engine.run({})
    return engine, events


class TestAgentCompleted:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["subscription", "metered_api", "unknown"])
    async def test_agent_completed_carries_billing_mode(self, mode: BillingMode) -> None:
        provider = _scripted_provider({"solo": mode})
        _, events = await _run(_config([_agent("solo")]), provider)
        (completed,) = _of(events, "agent_completed")
        assert completed["billing_mode"] == mode

    @pytest.mark.asyncio
    async def test_key_omitted_when_none(self) -> None:
        provider = _scripted_provider({})
        _, events = await _run(_config([_agent("solo")]), provider)
        (completed,) = _of(events, "agent_completed")
        assert "billing_mode" not in completed

    @pytest.mark.asyncio
    async def test_unrecognized_provider_value_is_reported_as_unknown(self) -> None:
        provider = _scripted_provider({"solo": "bedrock"})  # type: ignore[dict-item]
        _, events = await _run(_config([_agent("solo")]), provider)
        (completed,) = _of(events, "agent_completed")
        assert completed["billing_mode"] == "unknown"


class TestParallelGroup:
    @staticmethod
    def _parallel_config(second: Any) -> WorkflowConfig:
        return _config(
            [_agent("solo", route="team"), _agent("worker"), second],
            parallel=[
                ParallelGroup(
                    name="team",
                    agents=["worker", second.name],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )

    @pytest.mark.asyncio
    async def test_parallel_agent_completed_carries_billing_mode(self) -> None:
        provider = _scripted_provider(
            {"solo": None, "worker": "subscription", "sidekick": "metered_api"}
        )
        _, events = await _run(self._parallel_config(_agent("sidekick")), provider)
        by_name = {d["agent_name"]: d for d in _of(events, "parallel_agent_completed")}
        assert by_name["worker"]["billing_mode"] == "subscription"
        assert by_name["sidekick"]["billing_mode"] == "metered_api"

    @pytest.mark.asyncio
    async def test_parallel_llm_member_omits_key_when_none(self) -> None:
        _, events = await _run(
            self._parallel_config(_agent("sidekick")), _scripted_provider({"worker": "unknown"})
        )
        by_name = {d["agent_name"]: d for d in _of(events, "parallel_agent_completed")}
        assert by_name["worker"]["billing_mode"] == "unknown"
        assert "billing_mode" not in by_name["sidekick"]

    @staticmethod
    def _zero_usage_member(kind: str) -> tuple[Any, RuntimeConfig | None]:
        """A non-LLM member (no usage row, no billing) and the runtime it needs, if any."""
        if kind == "set":
            return SetStepDef(name="tagger", type="set", value="x"), None
        return (
            MCPStepDef(name="tagger", server="srv", tool="echo", arguments={"q": "hi"}),
            RuntimeConfig(
                provider="copilot", mcp_servers={"srv": MCPServerDef(type="stdio", command="npx")}
            ),
        )

    @staticmethod
    @contextmanager
    def _fake_mcp_manager() -> Iterator[None]:
        """Patch ``MCPManager`` where the engine imports it: one tool, ``echo``, always succeeds."""
        with patch("conductor.mcp.manager.MCPManager") as manager_cls:
            manager = manager_cls.return_value
            manager.connect_server = AsyncMock(return_value=[])
            manager.close = AsyncMock()
            manager.get_server_tools = MagicMock(
                return_value=[{"name": "srv__echo", "original_name": "echo"}]
            )
            manager.call_tool_structured = AsyncMock(
                return_value={
                    "content": [{"type": "text", "text": "r", "truncated": False}],
                    "structured": {"answer": 42},
                    "is_error": False,
                }
            )
            yield

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["set", "mcp"])
    async def test_zero_usage_members_carry_no_billing_mode(self, kind: str) -> None:
        member, runtime = self._zero_usage_member(kind)
        config = self._parallel_config(member)
        if runtime is not None:
            config.workflow.runtime = runtime
        with self._fake_mcp_manager():
            engine, events = await _run(config, _scripted_provider({"worker": "subscription"}))
        by_name = {d["agent_name"]: d for d in _of(events, "parallel_agent_completed")}
        assert by_name["worker"]["billing_mode"] == "subscription"
        assert "billing_mode" not in by_name["tagger"]
        assert by_name["tagger"]["tokens"] == 0
        if kind == "mcp":
            assert by_name["tagger"]["agent_type"] == "mcp"
        # The member has no usage row, so it cannot dilute the aggregate.
        summary = engine.usage_tracker.get_summary()
        assert "tagger" not in [a.agent_name for a in summary.agents]
        assert dict(summary.billing.breakdown) == {"unstated": 1, "subscription": 1}


class TestForEach:
    def _config(self, item: AgentDef | WorkflowStepDef) -> WorkflowConfig:
        return _config(
            [
                AgentDef(
                    name="finder",
                    model=_MODEL,
                    prompt="find",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="batch")],
                )
            ],
            for_each=[
                ForEachDef(
                    name="batch",
                    type="for_each",
                    source="finder.output.items",
                    **{"as": "item"},
                    agent=item,
                    max_concurrent=1,
                    routes=[RouteDef(to="$end")],
                )
            ],
        )

    @pytest.mark.asyncio
    async def test_for_each_item_completed_carries_billing_mode(self) -> None:
        provider = _scripted_provider(
            {"finder": None, "batch[0]": None, "processor": "subscription"},
            contents={"finder": {"items": ["a", "b"]}},
        )
        item = _agent("processor")
        _, events = await _run(self._config(item), provider)
        completed = _of(events, "for_each_item_completed")
        assert len(completed) == 2
        assert {d["billing_mode"] for d in completed} == {"subscription"}

    @pytest.mark.asyncio
    async def test_for_each_agent_item_omits_key_when_none(self) -> None:
        provider = _scripted_provider({}, contents={"finder": {"items": ["a"]}})
        _, events = await _run(self._config(_agent("processor")), provider)
        (completed,) = _of(events, "for_each_item_completed")
        assert "billing_mode" not in completed
        assert "billing" not in completed

    @pytest.mark.asyncio
    async def test_for_each_workflow_item_carries_aggregate_billing(self, tmp_path: Path) -> None:
        (tmp_path / "sub.yaml").write_text(
            textwrap.dedent(
                """\
                workflow:
                  name: child
                  entry_point: inner
                  runtime:
                    provider: copilot
                  limits:
                    max_iterations: 5
                agents:
                  - name: inner
                    prompt: "Inner work"
                    routes:
                      - to: "$end"
                output:
                  result: "{{ inner.output }}"
                """
            ),
            encoding="utf-8",
        )
        parent_path = tmp_path / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")
        item = WorkflowStepDef(name="runner", workflow="sub.yaml")
        provider = _scripted_provider(
            {"inner": "subscription"},
            contents={"finder": {"items": ["a"]}, "inner": {}},
        )
        engine, events = await _run(self._config(item), provider, workflow_path=parent_path)
        (completed,) = _of(events, "for_each_item_completed")
        assert completed["billing"] == {"state": "subscription", "breakdown": {"subscription": 1}}
        assert "billing_mode" not in completed
        # The child's row also rolled into the parent aggregate (``finder`` states nothing).
        assert dict(engine.usage_tracker.get_summary().billing.breakdown) == {
            "unstated": 1,
            "subscription": 1,
        }


class TestSubworkflowRollup:
    @pytest.mark.asyncio
    async def test_subworkflow_rows_roll_into_parent_aggregate(self, tmp_path: Path) -> None:
        (tmp_path / "child.yaml").write_text(
            textwrap.dedent(
                """\
                workflow:
                  name: child
                  entry_point: inner
                  runtime:
                    provider: copilot
                  limits:
                    max_iterations: 5
                agents:
                  - name: inner
                    prompt: "Inner work"
                    routes:
                      - to: "$end"
                output:
                  result: "{{ inner.output }}"
                """
            ),
            encoding="utf-8",
        )
        parent_path = tmp_path / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")
        config = _config(
            [
                _agent("front", route="delegate"),
                WorkflowStepDef(
                    name="delegate", workflow="child.yaml", routes=[RouteDef(to="$end")]
                ),
            ]
        )
        provider = _scripted_provider(
            {"front": "metered_api", "inner": "subscription"}, contents={"inner": {}}
        )
        engine, _ = await _run(config, provider, workflow_path=parent_path)
        billing = engine.usage_tracker.get_summary().billing
        assert dict(billing.breakdown) == {"metered_api": 1, "subscription": 1}
        assert billing.state == "mixed"


class TestValidator:
    def _config(self) -> WorkflowConfig:
        return _config([_agent("writer", validator=ValidatorConfig(criteria="Be right"))])

    @pytest.mark.asyncio
    async def test_validator_complete_carries_billing_mode(self) -> None:
        provider = _scripted_provider(
            {"writer": "metered_api", "writer (validator)": "subscription"}
        )
        _, events = await _run(self._config(), provider)
        (complete,) = _of(events, "agent_validator_complete")
        assert complete["billing_mode"] == "subscription"

    @pytest.mark.asyncio
    async def test_validator_complete_omits_key_when_none(self) -> None:
        provider = _scripted_provider({"writer": "subscription"})
        _, events = await _run(self._config(), provider)
        (complete,) = _of(events, "agent_validator_complete")
        assert "billing_mode" not in complete

    @pytest.mark.asyncio
    async def test_validator_row_participates_in_aggregate(self) -> None:
        provider = _scripted_provider(
            {"writer": "subscription", "writer (validator)": "metered_api"}
        )
        engine, _ = await _run(self._config(), provider)
        billing = engine.usage_tracker.get_summary().billing
        assert dict(billing.breakdown) == {"subscription": 1, "metered_api": 1}


class TestPartialOutput:
    @pytest.mark.asyncio
    async def test_partial_output_accepted_as_final_keeps_billing_mode(self) -> None:
        provider = _scripted_provider({"solo": "subscription"}, partial={"solo"})
        emitter, events = _collect()
        engine = WorkflowEngine(_config([_agent("solo")]), provider, event_emitter=emitter)

        async def cancel(**_kw: Any) -> InterruptResult:
            return InterruptResult(action=InterruptAction.CANCEL)

        engine._interrupt_handler.handle_interrupt = cancel  # type: ignore[method-assign]
        await engine.run({})

        (completed,) = _of(events, "agent_completed")
        assert completed["billing_mode"] == "subscription"
        rows = engine.usage_tracker.get_summary().agents
        assert [r.billing_mode for r in rows] == ["subscription"]


class TestExecutionSummary:
    @pytest.mark.asyncio
    async def test_execution_summary_usage_billing_and_row_keys(self) -> None:
        config = _config([_agent("a", route="b"), _agent("b")])
        provider = _scripted_provider({"a": "subscription", "b": None})
        engine, _ = await _run(config, provider)
        usage = engine.get_execution_summary()["usage"]
        assert usage["billing"] == {
            "state": "mixed",
            "breakdown": {"subscription": 1, "unstated": 1},
        }
        rows = {r["agent_name"]: r for r in usage["agents"]}
        assert rows["a"]["billing_mode"] == "subscription"
        assert "billing_mode" not in rows["b"]

    @pytest.mark.asyncio
    async def test_summary_billing_is_present_and_none_when_not_stated(self) -> None:
        engine, _ = await _run(_config([_agent("a")]), _scripted_provider({}))
        usage = engine.get_execution_summary()["usage"]
        assert "billing" in usage
        assert usage["billing"] is None

    @pytest.mark.asyncio
    async def test_mixed_run_never_reports_single_mode(self) -> None:
        config = _config([_agent("a", route="b"), _agent("b", route="c"), _agent("c")])
        provider = _scripted_provider(
            {"a": "subscription", "b": "subscription", "c": "metered_api"}
        )
        engine, _ = await _run(config, provider)
        billing = engine.get_execution_summary()["usage"]["billing"]
        assert billing["state"] == "mixed"  # 2-to-1 is not a majority mode
        assert billing["breakdown"] == {"subscription": 2, "metered_api": 1}


class TestBudget:
    def _config(self, mode: str) -> WorkflowConfig:
        return _config(
            [_agent("a", route="b"), _agent("b")],
            limits=LimitsConfig(max_iterations=10, budget_usd=0.0000001, budget_mode=mode),
        )

    @pytest.mark.asyncio
    async def test_budget_exceeded_event_and_error_carry_label_only_when_stated(self) -> None:
        # Stated (subscription): event carries the aggregate, the enforce error names it.
        emitter, events = _collect()
        engine = WorkflowEngine(
            self._config("enforce"),
            _scripted_provider({"a": "subscription"}),
            event_emitter=emitter,
        )
        with pytest.raises(BudgetExceededError) as exc_info:
            await engine.run({})
        (exceeded,) = _of(events, "budget_exceeded")
        assert exceeded["billing"] == {"state": "subscription", "breakdown": {"subscription": 1}}
        assert str(exc_info.value).splitlines()[0].endswith("(API-equivalent estimate)")
        assert exc_info.value.spent_usd > 0

        # Not stated: no key, and the message is byte-identical to the pre-change wording.
        emitter, events = _collect()
        engine = WorkflowEngine(
            self._config("enforce"), _scripted_provider({}), event_emitter=emitter
        )
        with pytest.raises(BudgetExceededError) as exc_info:
            await engine.run({})
        (exceeded,) = _of(events, "budget_exceeded")
        assert "billing" not in exceeded
        message = str(exc_info.value).splitlines()[0]
        assert message.startswith("Workflow exceeded cost budget ($0.00): spent $0.")
        assert "(" not in message.split("spent", 1)[1]

    @pytest.mark.asyncio
    async def test_budget_still_uses_estimate_for_subscription(self) -> None:
        """A subscription execution is priced like any other; the budget trips on it."""
        emitter, events = _collect()
        engine = WorkflowEngine(
            self._config("audit"),
            _scripted_provider({"a": "subscription", "b": "subscription"}),
            event_emitter=emitter,
        )
        await engine.run({})
        assert _of(events, "budget_exceeded"), "audit budget must still trip on the estimate"
        usage = engine.usage_tracker.get_summary()
        assert usage.total_cost_usd is not None and usage.total_cost_usd > 0
