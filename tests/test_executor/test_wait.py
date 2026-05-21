"""Tests for :class:`WaitExecutor`.

Covers:
- Plain numeric and suffixed-string durations.
- Templated durations rendered from context.
- Interrupt event cancels sleep early; ``interrupted=True`` and
  elapsed < requested.
- Runtime validation errors for unparseable / out-of-range durations.
"""

from __future__ import annotations

import asyncio

import pytest

from conductor.config.schema import AgentDef
from conductor.exceptions import ValidationError
from conductor.executor.wait import WaitExecutor, WaitOutput


@pytest.fixture
def executor() -> WaitExecutor:
    return WaitExecutor()


class TestWaitOutput:
    def test_fields(self) -> None:
        out = WaitOutput(waited_seconds=0.1, requested_seconds=0.1, reason=None, interrupted=False)
        assert out.waited_seconds == 0.1
        assert out.interrupted is False


class TestWaitExecutorBasic:
    @pytest.mark.asyncio
    async def test_short_sleep(self, executor: WaitExecutor) -> None:
        agent = AgentDef(name="w", type="wait", duration="100ms")
        out = await executor.execute(agent, {})
        assert 0.09 <= out.waited_seconds < 1.0
        assert out.requested_seconds == 0.1
        assert out.interrupted is False
        assert out.reason is None

    @pytest.mark.asyncio
    async def test_numeric_duration(self, executor: WaitExecutor) -> None:
        agent = AgentDef(name="w", type="wait", duration=0.05)
        out = await executor.execute(agent, {})
        assert out.requested_seconds == 0.05
        assert out.waited_seconds >= 0.04

    @pytest.mark.asyncio
    async def test_reason_rendered(self, executor: WaitExecutor) -> None:
        agent = AgentDef(name="w", type="wait", duration="50ms", reason="hi {{ name }}")
        out = await executor.execute(agent, {"name": "there"})
        assert out.reason == "hi there"

    @pytest.mark.asyncio
    async def test_templated_duration(self, executor: WaitExecutor) -> None:
        agent = AgentDef(
            name="w",
            type="wait",
            duration="{{ workflow.input.interval }}ms",
        )
        out = await executor.execute(agent, {"workflow": {"input": {"interval": 50}}})
        assert out.requested_seconds == 0.05


class TestWaitExecutorInterrupt:
    @pytest.mark.asyncio
    async def test_interrupt_cancels_early(self, executor: WaitExecutor) -> None:
        agent = AgentDef(name="w", type="wait", duration="10s")
        ev = asyncio.Event()
        task = asyncio.create_task(executor.execute(agent, {}, interrupt_event=ev))
        await asyncio.sleep(0.05)
        ev.set()
        out = await task
        assert out.interrupted is True
        assert out.waited_seconds < 1.0
        # The event MUST remain set so the engine's between-step
        # _check_interrupt can consume it and trigger the user menu.
        assert ev.is_set()

    @pytest.mark.asyncio
    async def test_no_interrupt_runs_to_completion(self, executor: WaitExecutor) -> None:
        agent = AgentDef(name="w", type="wait", duration="50ms")
        ev = asyncio.Event()
        out = await executor.execute(agent, {}, interrupt_event=ev)
        assert out.interrupted is False
        assert out.waited_seconds >= 0.04

    @pytest.mark.asyncio
    async def test_outer_cancellation_propagates(self, executor: WaitExecutor) -> None:
        agent = AgentDef(name="w", type="wait", duration="10s")
        ev = asyncio.Event()
        task = asyncio.create_task(executor.execute(agent, {}, interrupt_event=ev))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestWaitExecutorRuntimeValidation:
    """Bounds and parse errors that can arise from templated durations."""

    @pytest.mark.asyncio
    async def test_unparseable_duration(self, executor: WaitExecutor) -> None:
        # Bypass the schema by constructing via model_construct (skips
        # validation), then trip the runtime parser.
        agent = AgentDef.model_construct(name="w", type="wait", duration="forever")
        with pytest.raises(ValidationError, match="Wait 'w'"):
            await executor.execute(agent, {})

    @pytest.mark.asyncio
    async def test_zero_duration_via_template(self, executor: WaitExecutor) -> None:
        agent = AgentDef(
            name="w",
            type="wait",
            duration="{{ workflow.input.interval }}s",
        )
        with pytest.raises(ValidationError, match="must be > 0"):
            await executor.execute(agent, {"workflow": {"input": {"interval": 0}}})

    @pytest.mark.asyncio
    async def test_over_cap_via_template(self, executor: WaitExecutor) -> None:
        agent = AgentDef(
            name="w",
            type="wait",
            duration="{{ workflow.input.interval }}h",
        )
        with pytest.raises(ValidationError, match="24h cap"):
            await executor.execute(agent, {"workflow": {"input": {"interval": 25}}})
