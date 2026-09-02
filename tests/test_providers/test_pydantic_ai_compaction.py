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
    _FailOpenCompactionWrapper,
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


def _build_test_agent(
    *,
    output_type: type | None = None,
    custom_output_text: str | None = None,
    custom_output_args: dict[str, Any] | None = None,
    compaction: Any = None,
) -> Agent[Any, Any]:
    """Build a Pydantic AI agent backed by TestModel.

    Using TestModel avoids network calls. The agent is built with the default
    anthropic backend settings but with the test model passed directly, so
    ``build_agent`` does not need to resolve a real API key or model.
    """
    model = TestModel(
        custom_output_text=custom_output_text,
        custom_output_args=custom_output_args,
    )
    capabilities: list[Any] = []
    if compaction is not None:
        capabilities.append(build_tiered_compaction(compaction))
    return Agent(
        model=model,
        output_type=output_type or str,
        system_prompt="",
        name="test-agent",
        capabilities=capabilities,
    )


def _build_agent_fn_for_test(
    *,
    output_type: type | None = None,
    custom_output_text: str | None = None,
    custom_output_args: dict[str, Any] | None = None,
) -> Any:
    """Return a build_agent_fn closure that returns a TestModel-backed agent."""

    def build_agent_fn(
        toolsets: list[Any], *, max_parse_recovery_attempts: int, compaction: Any = None
    ) -> Any:
        return _build_test_agent(
            output_type=output_type,
            custom_output_text=custom_output_text,
            custom_output_args=custom_output_args,
            compaction=compaction,
        )

    return build_agent_fn


def _streaming_function_model(
    function: Any,
) -> Any:
    """Wrap a synchronous/async model function for both request and stream paths.

    Pydantic AI's ``Agent.run`` may call ``request_stream`` depending on build
    options, so a ``FunctionModel`` needs a ``stream_function`` that yields the
    same content as deltas.
    """
    from collections.abc import AsyncIterator

    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    async def _stream_function(messages: list[Any], info: Any) -> AsyncIterator[Any]:  # type: ignore[return-type]
        if asyncio.iscoroutinefunction(function):
            response = await function(messages, info)
        else:
            response = function(messages, info)
        if isinstance(response, ModelResponse):
            for part in response.parts:
                if isinstance(part, TextPart):
                    yield part.content
                elif isinstance(part, ToolCallPart):
                    yield DeltaToolCall(
                        name=part.tool_name,
                        json_args=part.args_as_json_str(),
                        tool_call_id=part.tool_call_id,
                    )
                else:
                    yield part
        else:
            yield response

    return FunctionModel(function=function, stream_function=_stream_function)


def _build_agent_fn_with_tool_call(
    *,
    output_type: type | None = None,
    summarizer_keep_messages: int | None = None,
) -> Any:
    """Return a build_agent_fn closure that returns a FunctionModel-backed agent.

    The model returns a tool call on its first request and a text answer on the
    second. This lets tests exercise multi-request runs and usage-limit
    accounting without calling a real API.

    Args:
        output_type: Optional output type for the agent.
        summarizer_keep_messages: When set, overrides the default
            ``keep_messages=20`` on the summarizing compaction tier so
            summarization fires even on a short message history.
    """
    from pydantic_ai.tools import Tool

    def noop_tool() -> str:
        return "ok"

    call_count: list[int] = [0]

    async def _model_func(messages: list[Any], info: Any) -> Any:
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

        call_count[0] += 1
        has_tools = bool(getattr(info, "function_tools", None))
        if call_count[0] == 1 and has_tools:
            return ModelResponse(parts=[ToolCallPart(tool_name="noop_tool", args={})])
        return ModelResponse(parts=[TextPart(content="done")])

    def build_agent_fn(
        toolsets: list[Any], *, max_parse_recovery_attempts: int, compaction: Any = None
    ) -> Any:
        capabilities: list[Any] = []
        if compaction is not None:
            if summarizer_keep_messages is not None:
                # Rebuild the tiered stack with a low keep_messages so the
                # summarizer fires on the short test history.
                capabilities.append(
                    _build_tiered_compaction_with_keep(compaction, summarizer_keep_messages)
                )
            else:
                capabilities.append(build_tiered_compaction(compaction))
        agent = Agent(
            model=_streaming_function_model(_model_func),
            output_type=output_type or str,
            system_prompt="",
            name="test-agent",
            tools=[Tool(noop_tool)],
            capabilities=capabilities,
        )
        return agent

    return build_agent_fn


def _build_tiered_compaction_with_keep(config: Any, keep_messages: int) -> Any:
    """Build the tiered compaction stack with a custom summarizer keep_messages."""
    from pydantic_ai_harness.compaction import (  # type: ignore[import-not-found]
        ClearToolResults,
        SlidingWindowCompaction,
        SummarizingCompaction,
        TieredCompaction,
    )

    clear_tier = _TierWrapper(
        ClearToolResults(max_messages=1, keep_pairs=3),
        tier_name="clear_tool_results",
    )
    summarize_tier = _TierWrapper(
        SummarizingCompaction(max_messages=1, keep_messages=keep_messages, model=None),
        tier_name="summarizing",
    )
    slide_tier = _TierWrapper(
        SlidingWindowCompaction(max_messages=1, keep_messages=20),
        tier_name="sliding_window",
    )

    tiered = TieredCompaction(
        tiers=[clear_tier, summarize_tier, slide_tier],
        target_tokens=config.target_tokens,
        tokenizer=None,
    )

    return _FailOpenCompactionWrapper(
        tiered,
        config=config,
        tier_wrappers=[clear_tier, summarize_tier, slide_tier],
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
    """Requirement: the wrapper gate compacts only above the trigger."""

    @pytest.mark.asyncio
    async def test_below_trigger_skips_compaction(self) -> None:
        # Requirement: a below-trigger estimate returns the request context
        # unchanged and never delegates to the inner strategy.
        inner = AsyncMock()
        cfg = _make_config(trigger_tokens=10_000, target_tokens=5_000)
        capability = _FailOpenCompactionWrapper(inner, config=cfg)

        request_context = _request_context_with_messages(
            [ModelRequest(parts=[UserPromptPart(content="hello")])]
        )
        result = await capability.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]

        assert result is request_context
        assert len(result.messages) == 1
        inner.before_model_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_above_trigger_invokes_inner(self) -> None:
        # Requirement: an above-trigger estimate delegates to the inner
        # compaction strategy exactly once.
        inner = AsyncMock()
        compacted_messages = [ModelRequest(parts=[UserPromptPart(content="compacted")])]
        compacted_context = _request_context_with_messages(compacted_messages)
        inner.before_model_request = AsyncMock(return_value=compacted_context)

        cfg = _make_config(trigger_tokens=10, target_tokens=5)
        capability = _FailOpenCompactionWrapper(inner, config=cfg)

        request_context = _request_context_with_messages(
            [ModelRequest(parts=[UserPromptPart(content="x" * 50_000)])]
        )
        result = await capability.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]

        inner.before_model_request.assert_called_once()
        assert result.messages == compacted_messages

    @pytest.mark.asyncio
    async def test_gate_measures_once_via_wrapper_estimate(self) -> None:
        # Requirement: the gate decision is driven by the wrapper's own
        # token estimate — patching it below the trigger must skip the inner
        # strategy (a removed gate fails this test).
        cfg = _make_config(trigger_tokens=10, target_tokens=5)
        capability = build_tiered_compaction(cfg)

        request_context = _request_context_with_messages(
            [ModelRequest(parts=[UserPromptPart(content="x" * 50_000)])]
        )
        with patch(
            "conductor.providers._pydantic_ai.compaction._estimate_context_tokens",
            new=AsyncMock(return_value=5),
        ):
            result = await capability.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]

        assert result is request_context
        assert len(result.messages) == 1


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

        summarizer = capability._inner.tiers[1]  # type: ignore[attr-defined]
        original_compact = summarizer._inner.compact
        summarizer._inner.compact = AsyncMock(side_effect=RuntimeError("boom"))

        messages: list[Any] = []
        for i in range(50):
            messages.append(ModelRequest(parts=[UserPromptPart(content=f"old {i:03d}")]))
        messages.append(ModelRequest(parts=[UserPromptPart(content="x" * 80_000)]))
        request_context = _request_context_with_messages(messages)

        sliding = capability._inner.tiers[2]  # type: ignore[attr-defined]
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
        # Requirement: once compaction shortens the history below target, the
        # next gate evaluation on the compacted history stays below the trigger.
        cfg = _make_config(trigger_tokens=100, target_tokens=50)
        capability = build_tiered_compaction(cfg)

        inner = AsyncMock()
        compacted_messages = [ModelRequest(parts=[UserPromptPart(content="small")])]
        compacted_context = _request_context_with_messages(compacted_messages)
        inner.before_model_request = AsyncMock(return_value=compacted_context)

        capability._inner = inner  # type: ignore[attr-defined]

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
        agent_def = AgentDef(
            name="wired",
            prompt="hello",
            max_depth=None,
            timeout_seconds=None,
            max_session_seconds=None,
            max_agent_iterations=None,
        )
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
        agent_def = AgentDef(
            name="plain",
            prompt="hello",
            max_depth=None,
            timeout_seconds=None,
            max_session_seconds=None,
            max_agent_iterations=None,
        )
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

        agent_def = AgentDef(
            name="callback-test",
            prompt="hello",
            max_depth=None,
            timeout_seconds=None,
            max_session_seconds=None,
            max_agent_iterations=None,
        )
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


class TestEventEmission:
    """Requirement: compaction lifecycle events flow through the callback."""

    @pytest.mark.asyncio
    async def test_emits_start_and_complete_when_compacting(self) -> None:
        """A request above the trigger must emit ``agent_compaction_start`` and
        a success-shaped ``agent_compaction_complete`` with the exact payload keys."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        cfg = _make_config(
            trigger_tokens=10,
            target_tokens=5,
            event_callback=callback,
            window_tokens=200_000,
            output_limit_tokens=32_000,
        )
        capability = build_tiered_compaction(cfg)

        messages: list[Any] = []
        for i in range(30):
            messages.append(ModelRequest(parts=[UserPromptPart(content=f"old {i:03d}")]))
        messages.append(ModelRequest(parts=[UserPromptPart(content="x" * 80_000)]))
        request_context = _request_context_with_messages(messages)

        result = await capability.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]

        assert len(result.messages) < len(messages)
        types = [e[0] for e in events]
        assert "agent_compaction_start" in types
        assert "agent_compaction_complete" in types

        start = events[types.index("agent_compaction_start")][1]
        expected_start_keys = {
            "agent_name",
            "strategy",
            "model",
            "context_window",
            "context_window_source",
            "output_limit",
            "output_limit_source",
            "trigger_tokens",
            "target_tokens",
            "messages_before",
            "tokens_before",
        }
        assert set(start.keys()) == expected_start_keys
        assert start["agent_name"] == cfg.agent_name
        assert start["strategy"] == "tiered"
        assert start["model"] == cfg.model_name
        assert start["context_window"] == cfg.window_tokens
        assert start["context_window_source"] == cfg.window_source
        assert start["output_limit"] == cfg.output_limit_tokens
        assert start["output_limit_source"] == cfg.output_limit_source
        assert start["trigger_tokens"] == cfg.trigger_tokens
        assert start["target_tokens"] == cfg.target_tokens
        assert start["messages_before"] == len(messages)
        assert start["tokens_before"] > cfg.trigger_tokens

        complete = events[types.index("agent_compaction_complete")][1]
        expected_complete_keys = {
            "agent_name",
            "strategy",
            "model",
            "context_window",
            "context_window_source",
            "messages_before",
            "messages_after",
            "tokens_before",
            "tokens_after",
            "tokens_saved",
            "elapsed",
            "errored",
            "degraded_tiers",
            "still_over_trigger",
        }
        assert set(complete.keys()) == expected_complete_keys
        assert complete["errored"] is False
        # The summarizing tier has no model outside a real run, so it degrades
        # and the sliding-window fallback produces the compacted history.
        assert complete["degraded_tiers"] == ["summarizing"]
        assert complete["agent_name"] == cfg.agent_name
        assert complete["strategy"] == "tiered"
        assert complete["model"] == cfg.model_name
        assert complete["context_window"] == cfg.window_tokens
        assert complete["context_window_source"] == cfg.window_source
        assert complete["tokens_before"] >= complete["tokens_after"]
        assert complete["tokens_saved"] == max(
            0, complete["tokens_before"] - complete["tokens_after"]
        )

    @pytest.mark.asyncio
    async def test_start_emitted_before_inner_strategy_runs(self) -> None:
        """Requirement: ``agent_compaction_start`` is emitted before the inner
        compaction strategy runs, and the complete event carries the real
        elapsed time of the strategy call (not a hardcoded zero)."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        cfg = _make_config(trigger_tokens=10, target_tokens=5, event_callback=callback)
        capability = build_tiered_compaction(cfg)

        call_order: list[str] = []
        gate = capability._inner  # type: ignore[attr-defined]
        original = gate.before_model_request

        async def _slow_inner(ctx: Any, request_context: Any) -> Any:
            call_order.append("inner")
            assert any(e[0] == "agent_compaction_start" for e in events), (
                "agent_compaction_start must be emitted before the inner strategy runs"
            )
            await asyncio.sleep(0.01)
            return await original(ctx, request_context)

        gate.before_model_request = _slow_inner

        try:
            await capability.before_model_request(  # type: ignore[arg-type]
                _make_run_context(),
                _request_context_with_messages(
                    [ModelRequest(parts=[UserPromptPart(content="x" * 80_000)])]
                ),
            )
        finally:
            gate.before_model_request = original

        types = [e[0] for e in events]
        start_index = types.index("agent_compaction_start")
        complete_index = types.index("agent_compaction_complete")
        assert start_index < complete_index
        assert call_order == ["inner"]
        complete = events[complete_index][1]
        assert complete["elapsed"] > 0

    @pytest.mark.asyncio
    async def test_no_events_when_below_trigger(self) -> None:
        """A request below the trigger must not emit any compaction lifecycle
        events because compaction never runs."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        cfg = _make_config(trigger_tokens=10_000, target_tokens=5_000, event_callback=callback)
        capability = build_tiered_compaction(cfg)

        request_context = _request_context_with_messages(
            [ModelRequest(parts=[UserPromptPart(content="hi")])]
        )
        result = await capability.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]

        assert result is request_context
        assert not events

    @pytest.mark.asyncio
    async def test_outer_failure_emits_errored_complete_and_latches(self) -> None:
        """An outer failure emits an errored complete event and disables further
        compaction attempts for this execution."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        cfg = _make_config(
            trigger_tokens=10,
            target_tokens=5,
            event_callback=callback,
            window_tokens=128_000,
            output_limit_tokens=64_000,
        )
        capability = build_tiered_compaction(cfg)

        gate = capability._inner  # type: ignore[attr-defined]
        original = gate.before_model_request
        mock_before = AsyncMock(side_effect=RuntimeError("boom"))
        gate.before_model_request = mock_before

        try:
            result1 = await capability.before_model_request(
                _make_run_context(),
                _request_context_with_messages(
                    [ModelRequest(parts=[UserPromptPart(content="x" * 80_000)])]
                ),  # type: ignore[arg-type]
            )
            result2 = await capability.before_model_request(
                _make_run_context(),
                _request_context_with_messages(
                    [ModelRequest(parts=[UserPromptPart(content="y" * 80_000)])]
                ),  # type: ignore[arg-type]
            )
        finally:
            gate.before_model_request = original

        complete = [e[1] for e in events if e[0] == "agent_compaction_complete"]
        assert len(complete) == 1, "expected exactly one errored complete event"
        errored = complete[0]
        expected_errored_keys = {
            "agent_name",
            "strategy",
            "model",
            "errored",
            "error_type",
            "message",
            "context_window",
            "context_window_source",
        }
        assert set(errored.keys()) == expected_errored_keys
        assert errored["errored"] is True
        assert errored["error_type"] == "RuntimeError"
        assert errored["agent_name"] == cfg.agent_name
        assert errored["strategy"] == "tiered"
        assert errored["model"] == cfg.model_name
        assert errored["context_window"] == cfg.window_tokens
        assert errored["context_window_source"] == cfg.window_source
        assert result1 is not None
        assert result2 is not None
        assert mock_before.call_count == 1, "latch should short-circuit second call"

    @pytest.mark.asyncio
    async def test_non_final_tier_failure_emits_success_complete_no_latch(self) -> None:
        """When a non-final tier fails but the sliding-window tier still
        compacts the history, the complete event is success-shaped and the
        disable latch is NOT engaged."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        cfg = _make_config(trigger_tokens=10, target_tokens=5, event_callback=callback)
        capability = build_tiered_compaction(cfg)

        summarizer = capability._inner.tiers[1]  # type: ignore[attr-defined]
        original_compact = summarizer._inner.compact
        summarizer._inner.compact = AsyncMock(side_effect=RuntimeError("summarizer boom"))

        messages: list[Any] = []
        for i in range(40):
            messages.append(ModelRequest(parts=[UserPromptPart(content=f"old {i:03d}")]))
        messages.append(ModelRequest(parts=[UserPromptPart(content="x" * 80_000)]))
        request_context = _request_context_with_messages(messages)

        try:
            result = await capability.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]
        finally:
            summarizer._inner.compact = original_compact

        assert len(result.messages) < len(messages)
        complete = [e[1] for e in events if e[0] == "agent_compaction_complete"]
        assert len(complete) == 1
        assert complete[0]["errored"] is False
        assert "error_type" not in complete[0]
        # A recovered tier failure is named in the event payload.
        assert complete[0]["degraded_tiers"] == ["summarizing"]

        # A subsequent request above the trigger should still compact (latch off).
        events.clear()
        messages2: list[Any] = []
        for i in range(40):
            messages2.append(ModelRequest(parts=[UserPromptPart(content=f"old {i:03d}")]))
        messages2.append(ModelRequest(parts=[UserPromptPart(content="y" * 80_000)]))
        request_context2 = _request_context_with_messages(messages2)
        result2 = await capability.before_model_request(_make_run_context(), request_context2)  # type: ignore[arg-type]
        assert len(result2.messages) < len(messages2)
        assert any(e[0] == "agent_compaction_complete" for e in events)

    @pytest.mark.asyncio
    async def test_gate_measurement_failure_skips_without_latch_or_event(self) -> None:
        # A failing gate estimate warns, returns the
        # original context, emits no event, and does not engage the latch.
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        cfg = _make_config(trigger_tokens=10, target_tokens=5, event_callback=callback)
        capability = build_tiered_compaction(cfg)

        request_context = _request_context_with_messages(
            [ModelRequest(parts=[UserPromptPart(content="x" * 80_000)])]
        )
        with patch(
            "conductor.providers._pydantic_ai.compaction._estimate_context_tokens",
            new=AsyncMock(side_effect=RuntimeError("estimator exploded")),
        ):
            result = await capability.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]

        assert result is request_context
        assert capability._disabled is False  # type: ignore[attr-defined]
        assert not events

    @pytest.mark.asyncio
    async def test_telemetry_failure_keeps_compacted_result_without_latch(self) -> None:
        # When the after-estimate raises, the
        # compacted result is still returned, no errored event is emitted, the
        # latch stays off, and a subsequent above-trigger request compacts.
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        cfg = _make_config(trigger_tokens=10, target_tokens=5, event_callback=callback)
        capability = build_tiered_compaction(cfg)

        messages: list[Any] = []
        for i in range(40):
            messages.append(ModelRequest(parts=[UserPromptPart(content=f"old {i:03d}")]))
        messages.append(ModelRequest(parts=[UserPromptPart(content="x" * 80_000)]))
        request_context = _request_context_with_messages(messages)

        with patch(
            "conductor.providers._pydantic_ai.compaction._estimate_after_compaction_tokens",
            side_effect=RuntimeError("telemetry exploded"),
        ):
            result = await capability.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]

        assert len(result.messages) < len(messages)
        complete = [e[1] for e in events if e[0] == "agent_compaction_complete"]
        assert not any(e.get("errored") for e in complete)
        assert capability._disabled is False  # type: ignore[attr-defined]

        # A subsequent above-trigger request still compacts.
        events.clear()
        messages2: list[Any] = []
        for i in range(40):
            messages2.append(ModelRequest(parts=[UserPromptPart(content=f"old {i:03d}")]))
        messages2.append(ModelRequest(parts=[UserPromptPart(content="y" * 80_000)]))
        request_context2 = _request_context_with_messages(messages2)
        result2 = await capability.before_model_request(_make_run_context(), request_context2)  # type: ignore[arg-type]
        assert len(result2.messages) < len(messages2)
        assert any(e[0] == "agent_compaction_complete" for e in events)

    @pytest.mark.asyncio
    async def test_tokens_saved_positive_with_surviving_usage_anchor(self) -> None:
        # With a usage-carrying ModelResponse in the
        # retained tail, tokens_saved on the complete event is positive
        # (the anchored estimator's overestimate is compensated).
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        cfg = _make_config(trigger_tokens=100, target_tokens=50, event_callback=callback)
        capability = build_tiered_compaction(cfg)

        old_large = ModelRequest(parts=[UserPromptPart(content="z" * 400_000)])
        # The anchor's usage reports the full pre-compaction request size, so
        # the before-estimate is anchored above the trigger while the tier
        # escalation can genuinely reclaim the old large message.
        anchor_response = ModelResponse(
            parts=[TextPart(content="ok")],
            usage=RequestUsage(input_tokens=100_500, output_tokens=10),
        )
        filler = [ModelRequest(parts=[UserPromptPart(content=f"turn {i:03d}")]) for i in range(30)]
        recent_request = ModelRequest(parts=[UserPromptPart(content="latest")])
        request_context = _request_context_with_messages(
            [old_large, anchor_response, *filler, recent_request]
        )

        result = await capability.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]

        assert len(result.messages) < len(filler) + 3
        complete = [e[1] for e in events if e[0] == "agent_compaction_complete"]
        assert len(complete) == 1
        assert complete[0]["errored"] is False
        assert complete[0]["tokens_saved"] > 0
        assert complete[0]["tokens_after"] < complete[0]["tokens_before"]

    @pytest.mark.asyncio
    async def test_still_over_trigger_flagged_when_compaction_cannot_reach(self) -> None:
        # When every tier is degraded and the estimate stays
        # above the trigger, the complete event flags still_over_trigger.
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        cfg = _make_config(trigger_tokens=100, target_tokens=50, event_callback=callback)
        capability = build_tiered_compaction(cfg)

        # Force every tier to fail so the history is returned unchanged.
        for tier in capability._inner.tiers:  # type: ignore[attr-defined]
            tier._inner.compact = AsyncMock(side_effect=RuntimeError("tier boom"))

        request_context = _request_context_with_messages(
            [ModelRequest(parts=[UserPromptPart(content="x" * 80_000)])]
        )
        result = await capability.before_model_request(_make_run_context(), request_context)  # type: ignore[arg-type]

        assert len(result.messages) == 1
        complete = [e[1] for e in events if e[0] == "agent_compaction_complete"]
        assert len(complete) == 1
        assert complete[0]["errored"] is False
        assert complete[0]["still_over_trigger"] is True
        assert set(complete[0]["degraded_tiers"]) == {
            "clear_tool_results",
            "summarizing",
            "sliding_window",
        }

    @pytest.mark.asyncio
    async def test_concurrent_stacks_share_no_state(self) -> None:
        # Two concurrently built stacks share neither the
        # disable latch nor the event callback.
        cfg_a = _make_config(
            trigger_tokens=10,
            target_tokens=5,
            agent_name="a",
            event_callback=lambda event_type, data: None,
        )
        cfg_b = _make_config(
            trigger_tokens=10,
            target_tokens=5,
            agent_name="b",
            event_callback=lambda event_type, data: None,
        )
        stack_a = build_tiered_compaction(cfg_a)
        stack_b = build_tiered_compaction(cfg_b)

        stack_a._disabled = True  # type: ignore[attr-defined]

        assert stack_b._disabled is False  # type: ignore[attr-defined]
        assert stack_a._config.event_callback is not stack_b._config.event_callback  # type: ignore[attr-defined]


class TestProviderIntegration:
    """Requirement: claude/openai execute() resolves compaction and emits config."""

    @pytest.mark.asyncio
    async def test_claude_execute_emits_compaction_config(self) -> None:
        """ClaudeProvider.execute() must emit exactly one ``agent_compaction_config``
        event per execution with the full payload."""
        from conductor.providers.claude import ClaudeProvider

        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        provider = ClaudeProvider(api_key="test-key")
        with (
            patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None),
            patch(
                "conductor.providers._pydantic_ai.agent_builder.build_agent",
                return_value=Agent(TestModel(custom_output_text="hello"), output_type=str),
            ) as mock_build_agent,
            patch(
                "conductor.providers._pydantic_ai.compaction_window.resolve_compaction_window",
                return_value=MockResolution(tokens=128_000, source="fallback"),
            ),
            patch(
                "conductor.providers._pydantic_ai.compaction_window.resolve_output_limit",
                return_value=MockResolution(tokens=64_000, source="default"),
            ),
        ):
            agent = AgentDef(
                name="cfg-test",
                prompt="hi",
                model="test",
                max_depth=None,
                timeout_seconds=None,
                max_session_seconds=None,
                max_agent_iterations=None,
            )
            output = await provider.execute(agent, {}, "hi", event_callback=callback)

        assert output.content == {"result": "hello"}
        config_events = [e for e in events if e[0] == "agent_compaction_config"]
        assert len(config_events) == 1
        payload = config_events[0][1]
        expected_keys = {
            "agent_name",
            "model",
            "context_window",
            "context_window_source",
            "output_limit",
            "output_limit_source",
            "enabled",
            "disabled_reason",
            "trigger_tokens",
            "target_tokens",
            "tool_buffer",
            "effective_tool_buffer",
        }
        assert set(payload.keys()) == expected_keys
        assert payload["agent_name"] == "cfg-test"
        assert payload["enabled"] is True
        assert payload["disabled_reason"] is None
        assert payload["context_window"] == 128_000
        assert payload["output_limit"] == 64_000
        assert mock_build_agent.call_args.kwargs["compaction"] is not None

    @pytest.mark.asyncio
    async def test_openai_execute_emits_compaction_config(self) -> None:
        """OpenAIProvider.execute() must emit exactly one ``agent_compaction_config``
        event per execution with the full payload."""
        from conductor.providers.openai import OpenAIProvider

        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        provider = OpenAIProvider(api_key="test-key")
        with (
            patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None),
            patch(
                "conductor.providers._pydantic_ai.agent_builder.build_agent",
                return_value=Agent(TestModel(custom_output_text="hello"), output_type=str),
            ) as mock_build_agent,
            patch(
                "conductor.providers._pydantic_ai.compaction_window.resolve_compaction_window",
                return_value=MockResolution(tokens=128_000, source="fallback"),
            ),
            patch(
                "conductor.providers._pydantic_ai.compaction_window.resolve_output_limit",
                return_value=MockResolution(tokens=64_000, source="default"),
            ),
        ):
            agent = AgentDef(
                name="cfg-test",
                prompt="hi",
                model="test",
                max_depth=None,
                timeout_seconds=None,
                max_session_seconds=None,
                max_agent_iterations=None,
            )
            output = await provider.execute(agent, {}, "hi", event_callback=callback)

        assert output.content == {"result": "hello"}
        config_events = [e for e in events if e[0] == "agent_compaction_config"]
        assert len(config_events) == 1
        payload = config_events[0][1]
        expected_keys = {
            "agent_name",
            "model",
            "context_window",
            "context_window_source",
            "output_limit",
            "output_limit_source",
            "enabled",
            "disabled_reason",
            "trigger_tokens",
            "target_tokens",
            "tool_buffer",
            "effective_tool_buffer",
        }
        assert set(payload.keys()) == expected_keys
        assert payload["agent_name"] == "cfg-test"
        assert payload["enabled"] is True
        assert payload["disabled_reason"] is None
        assert payload["context_window"] == 128_000
        assert payload["output_limit"] == 64_000
        assert mock_build_agent.call_args.kwargs["compaction"] is not None


class MockResolution:
    """Tiny stand-in for WindowResolution / OutputLimitResolution."""

    def __init__(self, tokens: int, source: str) -> None:
        self.tokens = tokens
        self.source = source


class TestMultiTurnCompaction:
    """Requirement: a long TestModel run crosses the trigger and compacts."""

    @pytest.mark.asyncio
    async def test_multi_turn_run_compacts_and_completes(self) -> None:
        """A large prompt makes the first request exceed the trigger; the run
        still completes with a valid result."""
        from conductor.providers._pydantic_ai.retry import RetryConfig
        from conductor.providers._pydantic_ai.runner import run_agent_pipeline

        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        agent_def = AgentDef(
            name="multi",
            prompt="go",
            model="test",
            max_depth=None,
            timeout_seconds=None,
            max_session_seconds=None,
            max_agent_iterations=None,
        )
        retry_cfg = RetryConfig(
            max_attempts=1,
            base_delay=0.0,
            max_delay=0.0,
            jitter=False,
            backoff="exponential",
            retry_on=None,
            max_parse_recovery_attempts=0,
        )
        cfg = _make_config(
            trigger_tokens=8_000,
            target_tokens=4_000,
            event_callback=callback,
        )
        large_prompt = "x" * 40_000

        output = await run_agent_pipeline(
            agent=agent_def,
            rendered_prompt=large_prompt,
            mcp_manager=None,
            tools=None,
            tool_output_config=None,  # type: ignore[arg-type]
            retry_config=retry_cfg,
            interrupt_signal=None,
            event_callback=callback,
            max_agent_iterations=10,
            max_session_seconds=None,
            default_model="test",
            retry_history=[],
            build_agent_fn=_build_agent_fn_for_test(custom_output_text="done"),
            compaction=cfg,
        )

        assert output.content == {"result": "done"}
        assert any(e[0] == "agent_compaction_start" for e in events)
        assert any(e[0] == "agent_compaction_complete" for e in events)

    @pytest.mark.asyncio
    async def test_interrupt_after_compaction(self) -> None:
        """Setting the interrupt signal after compaction must still yield a
        partial result without crashing."""
        import asyncio

        from conductor.providers._pydantic_ai.retry import RetryConfig
        from conductor.providers._pydantic_ai.runner import run_agent_pipeline

        agent_def = AgentDef(
            name="interrupt",
            prompt="go",
            model="test",
            max_depth=None,
            timeout_seconds=None,
            max_session_seconds=None,
            max_agent_iterations=None,
        )
        interrupt = asyncio.Event()
        interrupt.set()

        retry_cfg = RetryConfig(
            max_attempts=1,
            base_delay=0.0,
            max_delay=0.0,
            jitter=False,
            backoff="exponential",
            retry_on=None,
            max_parse_recovery_attempts=0,
        )
        cfg = _make_config(
            trigger_tokens=10,
            target_tokens=5,
        )

        output = await run_agent_pipeline(
            agent=agent_def,
            rendered_prompt="go",
            mcp_manager=None,
            tools=None,
            tool_output_config=None,  # type: ignore[arg-type]
            retry_config=retry_cfg,
            interrupt_signal=interrupt,
            event_callback=None,
            max_agent_iterations=10,
            max_session_seconds=None,
            default_model="test",
            retry_history=[],
            build_agent_fn=_build_agent_fn_for_test(custom_output_text="partial"),
            compaction=cfg,
        )

        assert output.partial is True

    @pytest.mark.asyncio
    async def test_parse_recovery_after_compaction(self) -> None:
        """A malformed first response with history above the trigger must still
        reach parse recovery and eventually produce valid structured output.

        ``FunctionModel`` returns an invalid response first, then a valid one,
        exercising the recovery path inside the runner without real API calls.
        """
        from pydantic import BaseModel

        from conductor.config.schema import OutputField
        from conductor.providers._pydantic_ai.retry import RetryConfig
        from conductor.providers._pydantic_ai.runner import run_agent_pipeline

        class AnswerModel(BaseModel):
            answer: str

        agent_def = AgentDef(
            name="recovery",
            prompt="answer",
            model="test",
            max_depth=None,
            timeout_seconds=None,
            max_session_seconds=None,
            max_agent_iterations=None,
            output={"answer": OutputField(type="string")},
        )
        retry_cfg = RetryConfig(
            max_attempts=1,
            base_delay=0.0,
            max_delay=0.0,
            jitter=False,
            backoff="exponential",
            retry_on=None,
            max_parse_recovery_attempts=2,
        )
        cfg = _make_config(trigger_tokens=10, target_tokens=5)
        large_prompt = "x" * 40_000

        call_count: list[int] = [0]

        async def _model_func(messages: list[Any], info: Any) -> Any:
            from pydantic_ai.messages import ModelResponse, TextPart

            call_count[0] += 1
            if call_count[0] == 1:
                # First response is not valid JSON for AnswerModel, forcing parse
                # recovery to append a correction prompt and call the model again.
                return ModelResponse(parts=[TextPart(content="not json")])
            return ModelResponse(parts=[TextPart(content='{"answer": "Paris"}')])

        def build_agent_fn(
            toolsets: list[Any], *, max_parse_recovery_attempts: int, compaction: Any = None
        ) -> Any:
            capabilities: list[Any] = []
            if compaction is not None:
                capabilities.append(build_tiered_compaction(compaction))
            return Agent(
                model=_streaming_function_model(_model_func),
                output_type=AnswerModel,
                system_prompt="",
                name="test-agent",
                capabilities=capabilities,
            )

        output = await run_agent_pipeline(
            agent=agent_def,
            rendered_prompt=large_prompt,
            mcp_manager=None,
            tools=None,
            tool_output_config=None,  # type: ignore[arg-type]
            retry_config=retry_cfg,
            interrupt_signal=None,
            event_callback=None,
            max_agent_iterations=5,
            max_session_seconds=None,
            default_model="test",
            retry_history=[],
            build_agent_fn=build_agent_fn,  # type: ignore[arg-type]
            compaction=cfg,
        )

        assert output.content == {"answer": "Paris"}

    @pytest.mark.asyncio
    async def test_usage_limits_interaction(self) -> None:
        """A compaction summary consumes one request-limit slot.

        A run whose normal agentic loop needs two model requests (tool call,
        then answer) uses three requests when compaction fires once, because the
        summarization tier performs an internal model call that shares the same
        usage budget.
        """
        from conductor.providers._pydantic_ai.retry import RetryConfig
        from conductor.providers._pydantic_ai.runner import run_agent_pipeline

        agent_def = AgentDef(
            name="limits",
            prompt="go",
            model="test",
            max_depth=None,
            timeout_seconds=None,
            max_session_seconds=None,
            max_agent_iterations=None,
        )
        retry_cfg = RetryConfig(
            max_attempts=1,
            base_delay=0.0,
            max_delay=0.0,
            jitter=False,
            backoff="exponential",
            retry_on=None,
            max_parse_recovery_attempts=0,
        )
        cfg = _make_config(trigger_tokens=10, target_tokens=5)
        large_prompt = "x" * 40_000

        output = await run_agent_pipeline(
            agent=agent_def,
            rendered_prompt=large_prompt,
            mcp_manager=None,
            tools=None,
            tool_output_config=None,  # type: ignore[arg-type]
            retry_config=retry_cfg,
            interrupt_signal=None,
            event_callback=None,
            max_agent_iterations=10,
            max_session_seconds=None,
            default_model="test",
            retry_history=[],
            build_agent_fn=_build_agent_fn_with_tool_call(summarizer_keep_messages=1),
            compaction=cfg,
        )

        assert output.content == {"result": "done"}
        assert output.raw_response.usage.requests == 3, (
            "compaction summary must consume one request"
        )


import asyncio  # noqa: E402
