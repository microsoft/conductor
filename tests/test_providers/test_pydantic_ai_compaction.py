"""Unit tests for pydantic-ai context compaction capability assembly.

These tests exercise the gating, tier-fallback, fail-open, and hysteresis
behavior of :mod:`conductor.providers._pydantic_ai.compaction` without
reaching a real LLM backend.  They use Pydantic AI's ``TestModel`` to drive
deterministic agent runs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from conductor.config.schema import AgentDef
from conductor.providers._pydantic_ai.agent_builder import build_agent
from conductor.providers._pydantic_ai.compaction import (
    CompactionConfig,
    _ThresholdGatedCompaction,
    _TierWrapper,
    build_tiered_compaction,
)


@pytest.fixture(autouse=True)
def _ensure_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a dummy Anthropic API key so AnthropicModel construction succeeds."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _make_config(
    *,
    trigger_tokens: int,
    target_tokens: int,
    window_tokens: int = 200_000,
    output_limit_tokens: int = 64_000,
    agent_name: str = "test-agent",
    model_name: str = "claude-3-5-sonnet-latest",
    event_callback: Any = None,
) -> CompactionConfig:
    """Build a CompactionConfig for tests with minimal boilerplate."""
    return CompactionConfig(
        window_tokens=window_tokens,
        window_source="test",
        output_limit_tokens=output_limit_tokens,
        output_limit_source="test",
        trigger_tokens=trigger_tokens,
        target_tokens=target_tokens,
        event_callback=event_callback,
        agent_name=agent_name,
        model_name=model_name,
    )


def _request_context_with_messages(messages: Any) -> Any:
    """Build a minimal ModelRequestContext around ``messages``."""
    from pydantic_ai.models import ModelRequestContext, ModelRequestParameters

    return ModelRequestContext(
        model=None,  # type: ignore[arg-type]
        model_settings=None,
        messages=messages,
        model_request_parameters=ModelRequestParameters(),
    )


def _make_run_context() -> Any:
    """Return a minimal RunContext usable by harness compaction tiers."""
    from pydantic_ai._run_context import RunContext
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.usage import RunUsage

    return RunContext(
        deps=None,
        model=TestModel(),
        usage=RunUsage(),
        messages=[],
    )


class TestThresholdGating:
    """Requirement: compaction only fires when estimated usage exceeds trigger."""

    @pytest.mark.asyncio
    async def test_below_trigger_skips_compaction(self) -> None:
        """When the message history is under the trigger, the gate must return
        the request context unchanged and never call the inner strategy."""
        inner = AsyncMock()
        gate = _ThresholdGatedCompaction(inner, trigger_tokens=10_000)

        request_context = _request_context_with_messages(
            [ModelRequest(parts=[UserPromptPart(content="hello")])]
        )
        result = await gate.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]

        assert result is request_context
        assert len(result.messages) == 1
        inner.before_model_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_above_trigger_invokes_inner(self) -> None:
        """When the message history is above the trigger, the gate must
        delegate to the inner compaction strategy."""
        inner = AsyncMock()
        compacted_messages = [ModelRequest(parts=[UserPromptPart(content="compacted")])]
        compacted_context = _request_context_with_messages(compacted_messages)
        inner.before_model_request = AsyncMock(return_value=compacted_context)

        gate = _ThresholdGatedCompaction(inner, trigger_tokens=10)

        request_context = _request_context_with_messages(
            [ModelRequest(parts=[UserPromptPart(content="x" * 50_000)])]
        )
        result = await gate.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]

        inner.before_model_request.assert_called_once()
        assert result.messages == compacted_messages

    @pytest.mark.asyncio
    async def test_anchor_short_circuit_fires_trigger(self) -> None:
        """A recent ModelResponse whose usage.input_tokens exceeds the trigger
        should short-circuit the gate to the inner strategy without requiring
        the full estimator to be the only signal."""
        inner = AsyncMock()
        compacted_context = _request_context_with_messages(
            [ModelRequest(parts=[UserPromptPart(content="short")])]
        )
        inner.before_model_request = AsyncMock(return_value=compacted_context)

        gate = _ThresholdGatedCompaction(inner, trigger_tokens=100)

        response = ModelResponse(
            parts=[TextPart(content="ok")],
            usage=RequestUsage(input_tokens=500, output_tokens=10),
        )
        request = ModelRequest(parts=[UserPromptPart(content="next")])
        request_context = _request_context_with_messages([response, request])

        result = await gate.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]

        inner.before_model_request.assert_called_once()
        assert result.messages == compacted_context.messages

    @pytest.mark.asyncio
    async def test_suffix_after_anchor_can_push_over_trigger(self) -> None:
        """A large message appended after the last usage anchor must still be
        counted by the gate, proving the estimator measures the post-anchor
        suffix and not just the previous request size."""
        inner = AsyncMock()
        compacted_context = _request_context_with_messages(
            [ModelRequest(parts=[UserPromptPart(content="compacted")])]
        )
        inner.before_model_request = AsyncMock(return_value=compacted_context)

        gate = _ThresholdGatedCompaction(inner, trigger_tokens=100)

        response = ModelResponse(
            parts=[TextPart(content="ok")],
            usage=RequestUsage(input_tokens=50, output_tokens=10),
        )
        big_request = ModelRequest(parts=[UserPromptPart(content="x" * 80_000)])
        request_context = _request_context_with_messages([response, big_request])

        result = await gate.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]

        inner.before_model_request.assert_called_once()
        assert result.messages == compacted_context.messages


class TestTierFallback:
    """Requirement: a failing non-final tier still yields to the final tier."""

    @pytest.mark.asyncio
    async def test_tier_wrapper_catches_and_continues(self) -> None:
        """``_TierWrapper.compact`` must catch an exception from its inner
        tier and return the original messages so the next tier can run."""
        failing_inner = AsyncMock()
        failing_inner.compact = AsyncMock(side_effect=RuntimeError("summarizer failed"))
        wrapper = _TierWrapper(failing_inner, tier_name="failing")

        messages = [ModelRequest(parts=[UserPromptPart(content="keep")])]
        result = await wrapper.compact(messages, _make_run_context())  # type: ignore[arg-type]

        assert result is messages
        failing_inner.compact.assert_called_once()

    @pytest.mark.asyncio
    async def test_summarizer_failure_still_reaches_sliding_window(self) -> None:
        """When the summarizing tier raises, the deterministic sliding-window
        tier must still run and shorten the history below target."""
        cfg = _make_config(trigger_tokens=10, target_tokens=5)
        capability = build_tiered_compaction(cfg)

        summarizer = capability._inner._inner.tiers[1]  # type: ignore[attr-defined]
        original_compact = summarizer._inner.compact
        summarizer._inner.compact = AsyncMock(side_effect=RuntimeError("boom"))

        messages: list[Any] = []
        for i in range(50):
            messages.append(ModelRequest(parts=[UserPromptPart(content=f"old {i:03d}")]))
        messages.append(ModelRequest(parts=[UserPromptPart(content="x" * 80_000)]))
        request_context = _request_context_with_messages(messages)

        sliding = capability._inner._inner.tiers[2]  # type: ignore[attr-defined]
        original_sliding_compact = sliding._inner.compact
        sliding_calls: list[list[Any]] = []

        async def _capture_and_call(msgs: list[Any], ctx: Any) -> list[Any]:
            sliding_calls.append(list(msgs))
            return await original_sliding_compact(msgs, ctx)

        sliding._inner.compact = _capture_and_call

        try:
            result = await capability.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]
        finally:
            summarizer._inner.compact = original_compact
            sliding._inner.compact = original_sliding_compact

        assert sliding_calls, "sliding-window tier was never reached"
        assert len(result.messages) <= 22
        assert len(result.messages) < len(messages)


class TestFailOpen:
    """Requirement: an unexpected outer compaction error returns original context."""

    @pytest.mark.asyncio
    async def test_outer_failure_returns_request_context(self) -> None:
        """If the inner gate raises unexpectedly, the fail-open wrapper must
        log a warning and return the original request context unchanged."""
        cfg = _make_config(trigger_tokens=10, target_tokens=5)
        capability = build_tiered_compaction(cfg)

        gate = capability._inner  # type: ignore[attr-defined]
        original = gate.before_model_request
        gate.before_model_request = AsyncMock(side_effect=RuntimeError("estimator bug"))

        messages = [ModelRequest(parts=[UserPromptPart(content="keep")])]
        request_context = _request_context_with_messages(messages)

        try:
            result = await capability.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]
        finally:
            gate.before_model_request = original

        assert result is request_context
        assert len(result.messages) == len(messages)


class TestToolCallPairing:
    """Requirement: ClearToolResults preserves tool-call/return pairing."""

    @pytest.mark.asyncio
    async def test_clear_tool_results_keeps_recent_pairs(self) -> None:
        """After compaction, the most recent tool-call/return pairs must
        remain paired; unpaired calls must not be produced."""
        cfg = _make_config(trigger_tokens=10, target_tokens=5)
        capability = build_tiered_compaction(cfg)

        messages: list[Any] = []
        for i in range(5):
            messages.append(
                ModelResponse(
                    parts=[ToolCallPart(tool_name=f"tool_{i}", args={"i": i})],
                    usage=RequestUsage(input_tokens=10, output_tokens=5),
                )
            )
            messages.append(
                ModelRequest(parts=[ToolReturnPart(tool_name=f"tool_{i}", content=f"result {i}")])
            )
        messages.append(ModelRequest(parts=[UserPromptPart(content="x" * 80_000)]))

        request_context = _request_context_with_messages(messages)
        result = await capability.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]

        calls: list[str] = []
        returns: list[str] = []
        for msg in result.messages:
            if isinstance(msg, ModelResponse):
                for part in msg.parts:
                    if isinstance(part, ToolCallPart):
                        calls.append(part.tool_name)
            elif isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        returns.append(part.tool_name)

        assert set(returns).issubset(set(calls))
        for call, ret in zip(calls, returns, strict=False):
            assert call == ret


class TestHysteresis:
    """Requirement: after compaction, the next request should not compact again."""

    @pytest.mark.asyncio
    async def test_no_immediate_re_compaction(self) -> None:
        """Once compaction shortens the history below target, a subsequent
        gate evaluation on the compacted history must stay below the trigger."""
        cfg = _make_config(trigger_tokens=100, target_tokens=50)
        capability = build_tiered_compaction(cfg)

        inner = AsyncMock()
        compacted_messages = [ModelRequest(parts=[UserPromptPart(content="small")])]
        compacted_context = _request_context_with_messages(compacted_messages)
        inner.before_model_request = AsyncMock(return_value=compacted_context)

        gate = capability._inner  # type: ignore[attr-defined]
        gate._inner = inner

        big_messages = [ModelRequest(parts=[UserPromptPart(content="x" * 80_000)])]
        request_context = _request_context_with_messages(big_messages)

        result1 = await capability.before_model_request(None, request_context)  # type: ignore[arg-type]
        assert inner.before_model_request.call_count == 1

        result2 = await capability.before_model_request(None, result1)  # type: ignore[arg-type]
        assert inner.before_model_request.call_count == 1
        assert result2 is result1


class TestBuildAgentWiring:
    """Requirement: build_agent accepts compaction and attaches capabilities."""

    def test_build_agent_attaches_capability(self) -> None:
        """When ``compaction`` is passed, ``build_agent`` should construct an
        Agent whose root capability contains the wrapper."""
        agent_def = AgentDef(name="wired", prompt="hello")
        cfg = _make_config(trigger_tokens=10, target_tokens=5)
        wrapper_class = type(build_tiered_compaction(cfg))

        pydantic_agent = build_agent(
            agent=agent_def,
            system_prompt="system",
            rendered_prompt="user",
            backend="anthropic",
            compaction=cfg,
        )

        root = pydantic_agent.root_capability
        assert any(isinstance(c, wrapper_class) for c in root.capabilities)

    def test_build_agent_tiered_compaction_construction_failure_fails_open(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If ``build_tiered_compaction`` raises during ``build_agent``, the agent
        must still be constructed and run without compaction, and an errored
        ``agent_compaction_complete`` event must be emitted."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        cfg = _make_config(trigger_tokens=10, target_tokens=5, event_callback=callback)
        agent_def = AgentDef(name="fails-open", prompt="hi")

        with (
            caplog.at_level("WARNING", logger="conductor.providers._pydantic_ai.agent_builder"),
            patch(
                "conductor.providers._pydantic_ai.agent_builder.build_tiered_compaction",
                side_effect=RuntimeError("constructor exploded"),
            ) as mock_build,
        ):
            pydantic_agent = build_agent(
                agent=agent_def,
                system_prompt="system",
                rendered_prompt="user",
                backend="anthropic",
                compaction=cfg,
            )
            mock_build.assert_called_once_with(cfg)

        assert pydantic_agent.root_capability is not None
        wrapper_class = type(
            build_tiered_compaction(_make_config(trigger_tokens=1, target_tokens=1))
        )
        assert not any(
            isinstance(c, wrapper_class) for c in pydantic_agent.root_capability.capabilities
        )

        complete_events = [e for e in events if e[0] == "agent_compaction_complete"]
        assert len(complete_events) == 1
        payload = complete_events[0][1]
        assert payload["errored"] is True
        assert payload["error_type"] == "RuntimeError"
        assert payload["message"] == "constructor exploded"

        assert any(
            "Failed to build tiered compaction capability for agent fails-open" in record.message
            for record in caplog.records
        )

    def test_build_agent_without_compaction_has_no_capabilities(self) -> None:
        """When ``compaction`` is omitted, the Agent should have no extra
        capabilities attached beyond the defaults added by pydantic-ai."""
        agent_def = AgentDef(name="plain", prompt="hello")
        cfg = _make_config(trigger_tokens=10, target_tokens=5)
        wrapper_class = type(build_tiered_compaction(cfg))

        pydantic_agent = build_agent(
            agent=agent_def,
            system_prompt="system",
            rendered_prompt="user",
            backend="anthropic",
        )

        assert not any(
            isinstance(c, wrapper_class) for c in pydantic_agent.root_capability.capabilities
        )


class TestRunnerCallbackClosure:
    """Requirement: runner passes intercepting_callback into build_agent_fn."""

    @pytest.mark.asyncio
    async def test_callback_reaches_compaction_config(self) -> None:
        """``run_agent_pipeline`` must pass the per-execute callback through to
        ``build_agent_fn`` so the compaction wrapper can close over it."""
        from conductor.providers._pydantic_ai.retry import RetryConfig
        from conductor.providers._pydantic_ai.runner import run_agent_pipeline

        captured: dict[str, Any] = {}

        def fake_event_callback(event_type: str, data: dict[str, Any]) -> None:
            pass

        def fake_build_agent_fn(
            toolsets: list[Any], *, max_parse_recovery_attempts: int, compaction: Any = None
        ) -> Any:
            captured["compaction"] = compaction
            captured["event_callback"] = compaction.event_callback if compaction else None
            return Agent(TestModel(), output_type=str)

        agent_def = AgentDef(name="callback-test", prompt="hello")
        retry_cfg = RetryConfig(
            max_attempts=1,
            base_delay=0.0,
            max_delay=0.0,
            jitter=False,
            backoff="exponential",
            retry_on=None,
            max_parse_recovery_attempts=0,
        )

        cfg = _make_config(trigger_tokens=10, target_tokens=5, event_callback=fake_event_callback)
        _ = await run_agent_pipeline(
            agent=agent_def,
            rendered_prompt="hi",
            mcp_manager=None,
            tools=None,
            tool_output_config=None,  # type: ignore[arg-type]
            retry_config=retry_cfg,
            interrupt_signal=None,
            event_callback=fake_event_callback,
            max_agent_iterations=3,
            max_session_seconds=None,
            default_model="claude-3-5-sonnet-latest",
            retry_history=[],
            build_agent_fn=fake_build_agent_fn,  # type: ignore[arg-type]
            compaction=cfg,
        )

        assert captured["compaction"] is cfg
        assert captured["event_callback"] is fake_event_callback
