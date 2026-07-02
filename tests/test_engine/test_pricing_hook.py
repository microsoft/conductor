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
    LimitsConfig,
    OutputField,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.pricing import ModelPricing
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.base import AgentOutput
from conductor.providers.copilot import CopilotProvider

# A model deliberately absent from DEFAULT_PRICING so only the provider hook
# (or its absence) determines whether cost is computed.
_UNPRICED_MODEL = "totally-unpriced-model-xyz"


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
        await engine._ensure_pricing_resolved(agent, _UNPRICED_MODEL)
        return engine.usage_tracker.record(name, _make_output(), elapsed=1.0).cost_usd

    costs = await asyncio.gather(*(ensure_then_record(f"a{i}") for i in range(4)))

    # Every concurrent sibling is priced; none silently undercounted.
    assert all(c == pytest.approx(40.0) for c in costs), costs
    # The hook is still consulted exactly once despite 4 concurrent callers.
    assert calls == 1
    assert engine.get_execution_summary()["usage"]["unpriced_agent_count"] == 0
