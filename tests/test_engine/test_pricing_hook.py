"""End-to-end tests for the provider pricing hook wiring (#265).

Verifies that ``WorkflowEngine`` resolves ``AgentProvider.get_model_pricing``
before recording usage, so a model absent from ``DEFAULT_PRICING`` is still
priced when the provider supplies rates — and is surfaced as *unpriced* (not
silently dropped) when neither the provider nor the table can price it.
"""

from __future__ import annotations

import asyncio

import pytest

from conductor.config.schema import (
    AgentDef,
    ForEachDef,
    LimitsConfig,
    OutputField,
    ParallelGroup,
    RouteDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.pricing import ModelPricing
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import BudgetExceededError
from conductor.providers.base import AgentOutput, AgentProvider
from conductor.providers.copilot import CopilotProvider

# A model deliberately absent from DEFAULT_PRICING so only the provider hook
# (or its absence) determines whether cost is computed.
_UNPRICED_MODEL = "totally-unpriced-model-xyz"


def _priced_provider(
    execute_impl,
    pricing_calls: list[str] | None = None,
):
    """Build a CopilotProvider with patched execute + a hook that prices _UNPRICED_MODEL.

    ``pricing_calls`` (when given) records each model the hook is asked about, so
    tests can assert the hook is consulted once per distinct model.
    """
    provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {})

    async def fake_pricing(model: str) -> ModelPricing | None:
        if pricing_calls is not None:
            pricing_calls.append(model)
        await asyncio.sleep(0)  # yield, mirroring the real hook's await
        if model == _UNPRICED_MODEL:
            return ModelPricing(input_per_mtok=10.0, output_per_mtok=30.0)
        return None

    provider.execute = execute_impl  # type: ignore[assignment]
    provider.get_model_pricing = fake_pricing  # type: ignore[assignment]
    return provider


def _make_config() -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="pricing-hook-test",
            entry_point="agent1",
            limits=LimitsConfig(max_iterations=5),
        ),
        agents=[
            AgentDef(
                name="agent1",
                prompt="Say hello",
                output={"result": OutputField(type="string")},
            ),
        ],
        output={"result": "{{ agent1.output.result }}"},
    )


def _make_output() -> AgentOutput:
    """1M input + 1M output tokens on an otherwise-unpriced model."""
    return AgentOutput(
        content={"result": "hi"},
        raw_response="{}",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        model=_UNPRICED_MODEL,
    )


@pytest.mark.asyncio
async def test_provider_hook_prices_otherwise_unpriced_model() -> None:
    """When the provider supplies pricing, the model is priced (not unpriced)."""
    config = _make_config()
    provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {})

    calls = 0

    async def fake_execute(*_args: object, **_kwargs: object) -> AgentOutput:
        return _make_output()

    async def fake_pricing(model: str) -> ModelPricing | None:
        nonlocal calls
        calls += 1
        assert model == _UNPRICED_MODEL
        return ModelPricing(input_per_mtok=10.0, output_per_mtok=30.0)

    provider.execute = fake_execute  # type: ignore[assignment]
    provider.get_model_pricing = fake_pricing  # type: ignore[assignment]

    engine = WorkflowEngine(config, provider)
    await engine.run({})

    summary = engine.get_execution_summary()
    usage = summary["usage"]
    # 1M @ $10 + 1M @ $30 = $40
    assert usage["total_cost_usd"] == pytest.approx(40.0)
    assert usage["unpriced_agent_count"] == 0
    assert usage["unpriced_models"] == []
    # The hook is consulted exactly once for the model.
    assert calls == 1


@pytest.mark.asyncio
async def test_unpriced_model_is_surfaced_when_hook_returns_none() -> None:
    """When neither provider nor table can price the model, it is surfaced."""
    config = _make_config()
    provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {})

    async def fake_execute(*_args: object, **_kwargs: object) -> AgentOutput:
        return _make_output()

    async def fake_pricing(_model: str) -> ModelPricing | None:
        return None

    provider.execute = fake_execute  # type: ignore[assignment]
    provider.get_model_pricing = fake_pricing  # type: ignore[assignment]

    engine = WorkflowEngine(config, provider)
    await engine.run({})

    summary = engine.get_execution_summary()
    usage = summary["usage"]
    # No priced agents => total is None, and the model is surfaced as unpriced.
    assert usage["total_cost_usd"] is None
    assert usage["unpriced_agent_count"] == 1
    assert usage["unpriced_models"] == [_UNPRICED_MODEL]


@pytest.mark.asyncio
async def test_hook_failure_falls_back_to_unpriced() -> None:
    """A raising hook is swallowed; the model degrades to unpriced (never crashes)."""
    config = _make_config()
    provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {})

    async def fake_execute(*_args: object, **_kwargs: object) -> AgentOutput:
        return _make_output()

    async def boom(_model: str) -> ModelPricing | None:
        raise RuntimeError("hook exploded")

    provider.execute = fake_execute  # type: ignore[assignment]
    provider.get_model_pricing = boom  # type: ignore[assignment]

    engine = WorkflowEngine(config, provider)
    await engine.run({})

    summary = engine.get_execution_summary()
    usage = summary["usage"]
    assert usage["total_cost_usd"] is None
    assert usage["unpriced_models"] == [_UNPRICED_MODEL]


@pytest.mark.asyncio
async def test_concurrent_same_model_agents_all_priced() -> None:
    """Parallel/for-each siblings sharing a model must all get priced (#265).

    Regression for the race where the resolver marked a model "resolved" before
    awaiting the hook: a sibling arriving during that await would skip resolution
    and ``record`` the model as unpriced. Each task here mirrors the engine's
    ``await _ensure_pricing_resolved(...)`` immediately followed by ``record``.
    """
    config = _make_config()
    provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {})

    calls = 0

    async def slow_pricing(_model: str) -> ModelPricing | None:
        nonlocal calls
        calls += 1
        # Yield control so siblings interleave — the race window the real
        # Copilot hook opens via ``await list_models()``.
        await asyncio.sleep(0)
        return ModelPricing(input_per_mtok=10.0, output_per_mtok=30.0)

    provider.get_model_pricing = slow_pricing  # type: ignore[assignment]

    engine = WorkflowEngine(config, provider)
    agent = config.agents[0]

    async def ensure_then_record(name: str) -> float | None:
        source = await engine._ensure_pricing_resolved(agent, _UNPRICED_MODEL)
        return engine.usage_tracker.record(name, _make_output(), 1.0, source).cost_usd

    costs = await asyncio.gather(*(ensure_then_record(f"a{i}") for i in range(4)))

    # Every concurrent sibling is priced; none silently undercounted.
    assert all(c == pytest.approx(40.0) for c in costs), costs
    # The hook is still consulted exactly once despite 4 concurrent callers.
    assert calls == 1
    assert engine.get_execution_summary()["usage"]["unpriced_agent_count"] == 0


@pytest.mark.asyncio
async def test_parallel_group_agents_priced_via_hook() -> None:
    """The parallel-group record site resolves provider pricing end-to-end (#265).

    Drives a real parallel group through ``engine.run`` (not a simulated call
    site), so deleting ``_ensure_pricing_resolved`` from ``_execute_parallel_group``
    would fail this test.
    """
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="parallel-pricing",
            entry_point="group",
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            AgentDef(name="a1", prompt="t1", output={"result": OutputField(type="string")}),
            AgentDef(name="a2", prompt="t2", output={"result": OutputField(type="string")}),
        ],
        parallel=[ParallelGroup(name="group", agents=["a1", "a2"], routes=[RouteDef(to="$end")])],
        output={"done": "true"},
    )

    async def fake_execute(*_args: object, **_kwargs: object) -> AgentOutput:
        return _make_output()

    pricing_calls: list[str] = []
    provider = _priced_provider(fake_execute, pricing_calls)
    engine = WorkflowEngine(config, provider)
    await engine.run({})

    usage = engine.get_execution_summary()["usage"]
    # Both parallel agents priced via the hook; none undercounted.
    assert usage["unpriced_agent_count"] == 0
    assert usage["total_cost_usd"] == pytest.approx(80.0)  # 2 agents x $40
    # Hook consulted once for the shared model despite two concurrent agents.
    assert pricing_calls == [_UNPRICED_MODEL]


@pytest.mark.asyncio
async def test_for_each_agents_priced_via_hook() -> None:
    """The for-each record site resolves provider pricing end-to-end (#265)."""
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="for-each-pricing",
            entry_point="finder",
            limits=LimitsConfig(max_iterations=20),
        ),
        agents=[
            AgentDef(
                name="finder",
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
                    name="proc",
                    prompt="process {{ item }}",
                    output={"result": OutputField(type="string")},
                ),
                max_concurrent=2,
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={"done": "true"},
    )

    async def fake_execute(agent, *_args: object, **_kwargs: object) -> AgentOutput:
        if agent.name == "finder":
            # Table-priced model (in DEFAULT_PRICING) so finder does NOT resolve
            # _UNPRICED_MODEL — otherwise it would warm the cache and the for-each
            # site's resolution would be an untested no-op fast path.
            return AgentOutput(
                content={"items": ["a", "b"]},
                raw_response="{}",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
                model="gpt-4o",
            )
        return _make_output()

    pricing_calls: list[str] = []
    provider = _priced_provider(fake_execute, pricing_calls)
    engine = WorkflowEngine(config, provider)
    await engine.run({})

    usage = engine.get_execution_summary()["usage"]
    # finder (table-priced gpt-4o) + 2 for-each items (hook-priced _UNPRICED_MODEL).
    assert usage["unpriced_agent_count"] == 0
    # gpt-4o: 1M in @ $2.50 + 1M out @ $10.00 = $12.50; 2 proc items x $40 = $80.
    assert usage["total_cost_usd"] == pytest.approx(92.5)
    # The for-each site is the sole resolver of _UNPRICED_MODEL (resolved once).
    assert pricing_calls.count(_UNPRICED_MODEL) == 1


@pytest.mark.asyncio
async def test_systemic_hook_failure_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    """A raising pricing hook surfaces a single once-per-run warning (#265)."""
    config = _make_config()
    provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {})

    async def boom(_model: str) -> ModelPricing | None:
        raise RuntimeError("SDK model listing is down")

    provider.get_model_pricing = boom  # type: ignore[assignment]
    engine = WorkflowEngine(config, provider)
    agent = config.agents[0]

    with caplog.at_level("WARNING", logger="conductor.engine.workflow"):
        await engine._ensure_pricing_resolved(agent, "model-a")
        await engine._ensure_pricing_resolved(agent, "model-b")

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1  # latched — only the first failure warns
    assert "pricing hook failed" in warnings[0].getMessage()
    assert engine._pricing_hook_failed_warned is True


# A model id that several hosts sell at different rates, and that the static
# table does not carry, so only the serving host decides its price.
_SHARED_MODEL = "openai/gpt-oss-20b"


class _HostedProvider:
    """A provider double that reports only the host serving its requests.

    Inherits the base ``get_model_pricing`` (returns ``None``), so the
    ``pricing`` extra prices each execution at ``pricing_host()``'s rate.
    ``None`` stands for an endpoint with no known price list (a gateway or a
    self-hosted server).
    """

    get_model_pricing = AgentProvider.get_model_pricing

    def __init__(self, host: str | None) -> None:
        self._host = host

    def pricing_host(self) -> str | None:
        return self._host


def _chain_config(count: int, limits: LimitsConfig) -> WorkflowConfig:
    """``count`` agents run one after another: a0 -> a1 -> ... -> $end."""
    agents = [
        AgentDef(
            name=f"a{i}",
            prompt="go",
            output={"result": OutputField(type="string")},
            routes=[RouteDef(to=f"a{i + 1}" if i + 1 < count else "$end")],
        )
        for i in range(count)
    ]
    return WorkflowConfig(
        workflow=WorkflowDef(name="mixed-hosts", entry_point="a0", limits=limits),
        agents=agents,
        output={"done": "true"},
    )


def _mixed_host_engine(
    providers: list[_HostedProvider],
    limits: LimitsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> WorkflowEngine:
    """An engine whose agent ``a{i}`` is served by ``providers[i]``.

    Every agent reports the same model id with 1M input and 1M output tokens,
    so each record's cost is exactly the serving host's input plus output rate.
    """
    execution = CopilotProvider(mock_handler=lambda *_a, **_kw: {})

    async def fake_execute(*_args: object, **_kwargs: object) -> AgentOutput:
        return AgentOutput(
            content={"result": "ok"},
            raw_response="{}",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            model=_SHARED_MODEL,
        )

    execution.execute = fake_execute  # type: ignore[assignment]
    engine = WorkflowEngine(_chain_config(len(providers), limits), execution)
    serving = {f"a{i}": provider for i, provider in enumerate(providers)}

    async def provider_for(agent: AgentDef) -> _HostedProvider:
        return serving[agent.name]

    monkeypatch.setattr(engine, "_get_provider_for_agent", provider_for)
    return engine


def _host_cost(host: str | None) -> float | None:
    """The published cost of 1M input + 1M output tokens at ``host``."""
    if host is None:
        return None
    import llm_prices

    price = llm_prices.lookup_provider(host, _SHARED_MODEL)
    assert price is not None
    return price.input + price.output


@pytest.fixture
def shared_model_rates() -> None:
    """Skip without the ``pricing`` extra; check the hosts really disagree."""
    pytest.importorskip("llm_prices")
    openrouter, groq = _host_cost("openrouter"), _host_cost("groq")
    assert openrouter is not None and groq is not None
    # The budget cases below need the cheaper host to be clearly cheaper.
    assert 2 * openrouter < groq


_MIXED_ORDERS = [
    pytest.param(["openrouter", "groq"], id="openrouter-then-groq"),
    pytest.param(["groq", "openrouter"], id="groq-then-openrouter"),
    pytest.param(["openrouter", None, "groq"], id="openrouter-unknown-groq"),
    pytest.param(["groq", None, "openrouter"], id="groq-unknown-openrouter"),
    pytest.param([None, "openrouter", "groq"], id="unknown-openrouter-groq"),
    pytest.param([None, "groq", "openrouter"], id="unknown-groq-openrouter"),
]


@pytest.mark.usefixtures("shared_model_rates")
class TestMixedProviderPricing:
    """Each execution is priced at the host that served it, in any order.

    Regression: the serving host was cached by model name, so the first
    provider to resolve a model priced every later execution of it. OpenRouter
    then Groq recorded the Groq run at OpenRouter's rate ($0.108 instead of
    $0.375 for 1M + 1M tokens), undercounting spend so a budget could not fire.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hosts", _MIXED_ORDERS)
    async def test_each_record_priced_at_its_own_host(
        self, hosts: list[str | None], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        providers = [_HostedProvider(host) for host in hosts]
        engine = _mixed_host_engine(providers, LimitsConfig(max_iterations=10), monkeypatch)
        await engine.run({})

        records = engine.usage_tracker.get_summary().agents
        assert [r.agent_name for r in records] == [f"a{i}" for i in range(len(hosts))]
        for record, host in zip(records, hosts, strict=True):
            expected = _host_cost(host)
            if expected is None:
                # An unknown host stays unpriced; it never borrows a known
                # host's rate for the same model id.
                assert record.cost_usd is None, host
            else:
                assert record.cost_usd == pytest.approx(expected), host

        usage = engine.get_execution_summary()["usage"]
        priced = [c for c in map(_host_cost, hosts) if c is not None]
        assert usage["total_cost_usd"] == pytest.approx(sum(priced))
        assert usage["unpriced_agent_count"] == hosts.count(None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hosts", _MIXED_ORDERS)
    async def test_budget_fires_on_the_true_mixed_cost(
        self, hosts: list[str | None], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An enforce budget just under the true total trips on the last agent."""
        costs = [c for c in map(_host_cost, hosts) if c is not None]
        total = sum(costs)
        # Above every earlier prefix of the run, below the full run, so only
        # the last agent's real cost can cross it.
        budget = total - 0.5 * min(costs)
        providers = [_HostedProvider(host) for host in hosts]
        engine = _mixed_host_engine(
            providers, LimitsConfig(budget_usd=budget, budget_mode="enforce"), monkeypatch
        )

        with pytest.raises(BudgetExceededError) as exc_info:
            await engine.run({})

        assert exc_info.value.spent_usd == pytest.approx(total)
        assert exc_info.value.current_agent == f"a{len(hosts) - 1}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "hosts",
        [
            pytest.param(["groq", None], id="groq-then-unknown"),
            pytest.param(["openrouter", "groq", None], id="openrouter-groq-unknown"),
        ],
    )
    async def test_unknown_host_never_inherits_a_rate_into_the_budget(
        self, hosts: list[str | None], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown host after priced ones must not trip the budget.

        If the unknown endpoint inherited an earlier host's rate for the same
        model id, spend would pass this budget and the run would stop.
        """
        costs = [c for c in map(_host_cost, hosts) if c is not None]
        total = sum(costs)
        budget = total + 0.5 * min(costs)
        providers = [_HostedProvider(host) for host in hosts]
        engine = _mixed_host_engine(
            providers, LimitsConfig(budget_usd=budget, budget_mode="enforce"), monkeypatch
        )
        await engine.run({})

        summary = engine.usage_tracker.get_summary()
        assert [r.cost_usd is None for r in summary.agents] == [h is None for h in hosts]
        assert summary.total_cost_usd == pytest.approx(total)

    @pytest.mark.asyncio
    async def test_hook_rate_stays_with_the_provider_that_returned_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provider's own hook rate never prices another provider's execution."""

        class _HookPriced(_HostedProvider):
            async def get_model_pricing(self, model: str) -> ModelPricing | None:
                return ModelPricing(input_per_mtok=7.0, output_per_mtok=11.0)

        providers = [_HookPriced(None), _HostedProvider("groq"), _HostedProvider(None)]
        engine = _mixed_host_engine(providers, LimitsConfig(), monkeypatch)
        await engine.run({})

        hooked, hosted, unknown = engine.usage_tracker.get_summary().agents
        assert hooked.cost_usd == pytest.approx(18.0)
        assert hosted.cost_usd == pytest.approx(_host_cost("groq"))
        assert unknown.cost_usd is None
