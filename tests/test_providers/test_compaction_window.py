"""Unit tests for compaction window/output-limit resolution."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from conductor.config.schema import ToolOutputConfig
from conductor.providers._pydantic_ai.compaction_window import (
    DEFAULT_MAX_TOKENS,
    DISABLED_REASON_INSUFFICIENT_HEADROOM,
    FALLBACK_CONTEXT_WINDOW,
    MIN_VIABLE_TRIGGER,
    TOOL_BUFFER_MAX_FRACTION,
    CompactionPlan,
    OutputLimitResolution,
    WindowResolution,
    _reset_warning_latches,
    resolve_compaction_plan,
    resolve_compaction_window,
    resolve_output_limit,
    target_tokens,
    tool_buffer_tokens,
    trigger_tokens,
)
from conductor.providers.base import AgentProvider


@pytest.fixture(autouse=True)
def _clean_warning_latches() -> None:
    """Reset module-level one-shot warning latches before every test."""
    _reset_warning_latches()


class _MockProvider(AgentProvider, abstract=True):
    """Minimal provider with configurable metadata hooks."""

    CAPABILITIES = None

    def __init__(
        self,
        *,
        max_prompt: int | None = None,
        max_output: int | None = None,
        prompt_raises: bool = False,
        output_raises: bool = False,
    ) -> None:
        self._max_prompt = max_prompt
        self._max_output = max_output
        self._prompt_raises = prompt_raises
        self._output_raises = output_raises

    async def execute(
        self,
        agent: Any,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: Any = None,
        event_callback: Any = None,
        skill_directories: list[str] | None = None,
        custom_agents: list[dict[str, Any]] | None = None,
        extra_mcp_servers: dict[str, Any] | None = None,
    ) -> Any:
        raise NotImplementedError

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    async def get_max_prompt_tokens(self, model: str) -> int | None:
        if self._prompt_raises:
            raise RuntimeError("prompt boom")
        return self._max_prompt

    async def get_max_output_tokens(self, model: str) -> int | None:
        if self._output_raises:
            raise RuntimeError("output boom")
        return self._max_output


class TestResolveCompactionWindow:
    """Tests for the context-window resolution cascade."""

    async def test_provider_metadata_beats_registry(self) -> None:
        # Registry knows gpt-5.2 as 400k; the provider says 200k and wins.
        provider = _MockProvider(max_prompt=200_000)

        result = await resolve_compaction_window(
            provider=provider,
            model="gpt-5.2",
            has_custom_base_url=False,
        )

        assert result == WindowResolution(tokens=200_000, source="provider")

    async def test_custom_base_url_suppresses_registry(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        provider = _MockProvider()

        with caplog.at_level(
            logging.WARNING,
            logger="conductor.providers._pydantic_ai.compaction_window",
        ):
            result = await resolve_compaction_window(
                provider=provider,
                model="gpt-5.2",
                has_custom_base_url=True,
            )

        assert result == WindowResolution(
            tokens=FALLBACK_CONTEXT_WINDOW,
            source="fallback",
        )
        assert "custom base URL" in caplog.text

    async def test_fallback_path(self) -> None:
        provider = _MockProvider()

        result = await resolve_compaction_window(
            provider=provider,
            model="unknown-model-for-fallback",
            has_custom_base_url=True,
        )

        assert result == WindowResolution(
            tokens=FALLBACK_CONTEXT_WINDOW,
            source="fallback",
        )

    async def test_provider_raises_falls_back_to_registry(self) -> None:
        provider = _MockProvider(prompt_raises=True)

        result = await resolve_compaction_window(
            provider=provider,
            model="gpt-5.2",
            has_custom_base_url=False,
        )

        assert result.source == "registry"


class TestResolveOutputLimit:
    """Tests for the output-limit resolution cascade."""

    async def test_user_configured_settings_source(self) -> None:
        provider = _MockProvider()

        result = await resolve_output_limit(
            provider=provider,
            model="gpt-5.2",
            effective_max_tokens=4096,
            user_configured=True,
        )

        assert result == OutputLimitResolution(tokens=4096, source="settings")

    async def test_unconfigured_default_source(self) -> None:
        provider = _MockProvider()

        result = await resolve_output_limit(
            provider=provider,
            model="gpt-5.2",
            effective_max_tokens=DEFAULT_MAX_TOKENS,
            user_configured=False,
        )

        assert result == OutputLimitResolution(
            tokens=DEFAULT_MAX_TOKENS,
            source="default",
        )

    async def test_none_effective_max_tokens_uses_default(self) -> None:
        provider = _MockProvider()

        result = await resolve_output_limit(
            provider=provider,
            model="gpt-5.2",
            effective_max_tokens=None,
            user_configured=False,
        )

        assert result == OutputLimitResolution(
            tokens=DEFAULT_MAX_TOKENS,
            source="default",
        )

    async def test_provider_cap_clamps_settings(self) -> None:
        provider = _MockProvider(max_output=8192)

        result = await resolve_output_limit(
            provider=provider,
            model="gpt-5.2",
            effective_max_tokens=16_384,
            user_configured=True,
        )

        assert result == OutputLimitResolution(tokens=8192, source="provider-cap")

    async def test_provider_cap_clamps_default(self) -> None:
        provider = _MockProvider(max_output=8192)

        result = await resolve_output_limit(
            provider=provider,
            model="gpt-5.2",
            effective_max_tokens=DEFAULT_MAX_TOKENS,
            user_configured=False,
        )

        assert result == OutputLimitResolution(tokens=8192, source="provider-cap")

    async def test_provider_cap_ignored_when_larger(self) -> None:
        provider = _MockProvider(max_output=128_000)

        result = await resolve_output_limit(
            provider=provider,
            model="gpt-5.2",
            effective_max_tokens=16_384,
            user_configured=False,
        )

        assert result == OutputLimitResolution(tokens=16_384, source="default")

    async def test_provider_raises_uses_base(self) -> None:
        provider = _MockProvider(output_raises=True)

        result = await resolve_output_limit(
            provider=provider,
            model="gpt-5.2",
            effective_max_tokens=4096,
            user_configured=True,
        )

        assert result == OutputLimitResolution(tokens=4096, source="settings")


class TestToolBufferTokens:
    """Tests for the tool-result buffer heuristic."""

    def test_default_max_chars(self) -> None:
        cfg = ToolOutputConfig(enabled=True, max_chars=50_000)
        assert tool_buffer_tokens(cfg) == 40_000

    def test_custom_max_chars(self) -> None:
        cfg = ToolOutputConfig(enabled=True, max_chars=10_000)
        # 2 * ceil(10000 / 4) + 15000 = 2 * 2500 + 15000
        assert tool_buffer_tokens(cfg) == 20_000

    def test_uneven_max_chars_rounds_up(self) -> None:
        cfg = ToolOutputConfig(enabled=True, max_chars=9_999)
        # ceil(9999 / 4) = 2500; 2 * 2500 + 15000 = 20000
        assert tool_buffer_tokens(cfg) == 20_000

    def test_disabled_emits_default_buffer_and_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg = ToolOutputConfig(enabled=False, max_chars=9999)

        with caplog.at_level(
            logging.WARNING,
            logger="conductor.providers._pydantic_ai.compaction_window",
        ):
            result = tool_buffer_tokens(cfg)

        assert result == 40_000
        assert "unbounded" in caplog.text.lower()

    def test_none_config_uses_default(self) -> None:
        assert tool_buffer_tokens(None) == 40_000

    def test_huge_max_chars(self) -> None:
        cfg = ToolOutputConfig(enabled=True, max_chars=500_000)
        # 2 * ceil(500000 / 4) + 15000 = 2 * 125000 + 15000
        assert tool_buffer_tokens(cfg) == 265_000


class TestTriggerFormula:
    """Tests for trigger_tokens math."""

    def test_worked_examples(self) -> None:
        # Requirement: trigger = window - output_limit - min(buffer, window*0.25).
        assert trigger_tokens(128_000, 16_384, 40_000) == 128_000 - 16_384 - 32_000
        assert trigger_tokens(200_000, 16_384, 40_000) == 143_616
        assert trigger_tokens(1_000_000, 16_384, 40_000) == 943_616
        # Requirement: a larger output limit shrinks the trigger by the same amount.
        assert trigger_tokens(128_000, 64_000, 40_000) == 128_000 - 64_000 - 32_000

    def test_small_window_floor(self) -> None:
        # Requirement: a degenerate (disabled) plan maps to the historical
        # floor trigger of 1 for the legacy wrapper.
        assert trigger_tokens(1, 1, 1) == 1
        assert trigger_tokens(40_000, 40_000, 1) == 1


class TestResolveCompactionPlan:
    """Tests for resolve_compaction_plan arithmetic and disabled plans."""

    def test_tiny_windows_disable_compaction(self) -> None:
        # Requirement: a window smaller than the raw output limit + tool
        # buffer reserve (even after the buffer clamp) disables compaction
        # with a named reason instead of degenerating.
        for window, output_limit, tool_buffer in (
            (8_192, 16_384, 40_000),
            (32_768, 32_768, 40_000),
            (56_384, 56_384, 40_000),
        ):
            plan = resolve_compaction_plan(
                window=window,
                output_limit=output_limit,
                tool_buffer=tool_buffer,
            )
            assert plan.enabled is False
            assert plan.disabled_reason == DISABLED_REASON_INSUFFICIENT_HEADROOM
            assert plan.trigger_tokens is None
            assert plan.target_tokens is None

    def test_viable_windows_enable_with_hysteresis_gap(self) -> None:
        # Requirement: target = min(55% ceiling, trigger - margin) with
        # margin = max(1, int(window*0.05)); the 55% ceiling binds at 120000.
        for window in (60_000, 80_000, 120_000):
            plan = resolve_compaction_plan(
                window=window,
                output_limit=DEFAULT_MAX_TOKENS,
                tool_buffer=40_000,
            )
            assert plan.enabled is True
            assert plan.trigger_tokens is not None
            assert plan.target_tokens is not None
            margin = max(1, int(window * 0.05))
            expected_target = min(int(window * 0.55), plan.trigger_tokens - margin)
            assert plan.target_tokens == expected_target
            assert plan.trigger_tokens - plan.target_tokens >= margin
            assert 0 < plan.target_tokens < plan.trigger_tokens

    def test_large_window_buffer_clamped_to_window_fraction(self) -> None:
        # Requirement: on a 128k window the 40k buffer clamps to 32k and the
        # trigger is window - output_limit - effective buffer.
        plan = resolve_compaction_plan(
            window=128_000,
            output_limit=DEFAULT_MAX_TOKENS,
            tool_buffer=40_000,
        )
        assert plan.enabled is True
        assert plan.effective_tool_buffer == int(128_000 * TOOL_BUFFER_MAX_FRACTION)
        assert plan.trigger_tokens == 128_000 - 16_384 - 32_000
        assert plan.target_tokens == min(int(128_000 * 0.55), plan.trigger_tokens - 6_400)

    def test_huge_buffer_clamps_to_window_fraction(self) -> None:
        # Requirement: a max_chars so large it would eat the window clamps to
        # 25% of the window rather than disabling compaction.
        plan = resolve_compaction_plan(
            window=128_000,
            output_limit=DEFAULT_MAX_TOKENS,
            tool_buffer=265_000,
        )
        assert plan.enabled is True
        assert plan.effective_tool_buffer == int(128_000 * TOOL_BUFFER_MAX_FRACTION)

    def test_min_viable_trigger_boundary(self) -> None:
        # Requirement: a trigger with viable headroom above
        # MIN_VIABLE_TRIGGER is enabled; once the margin no longer leaves a
        # positive target the plan is disabled.
        window = 60_000
        tool_buffer = 15_000
        margin = int(window * 0.05)

        # trigger = MIN_VIABLE_TRIGGER + margin + 1 => target = MIN_VIABLE_TRIGGER + 1.
        output_limit = window - tool_buffer - (MIN_VIABLE_TRIGGER + margin + 1)
        enabled = resolve_compaction_plan(
            window=window, output_limit=output_limit, tool_buffer=tool_buffer
        )
        assert enabled.enabled is True
        assert enabled.trigger_tokens == MIN_VIABLE_TRIGGER + margin + 1
        assert enabled.target_tokens == MIN_VIABLE_TRIGGER + 1

        # One margin + two tokens less headroom drops the trigger just below
        # MIN_VIABLE_TRIGGER => under the viability floor => disabled.
        disabled = resolve_compaction_plan(
            window=window, output_limit=output_limit + margin + 2, tool_buffer=tool_buffer
        )
        assert disabled.enabled is False
        assert disabled.disabled_reason == DISABLED_REASON_INSUFFICIENT_HEADROOM

    def test_disabled_plan_event_fields(self) -> None:
        # Requirement: a disabled plan surfaces enabled=False plus the reason
        # through emit_compaction_config with null thresholds.
        from conductor.providers._pydantic_ai.events import emit_compaction_config

        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        plan = resolve_compaction_plan(window=8_192, output_limit=16_384, tool_buffer=40_000)
        assert plan == CompactionPlan(
            enabled=False,
            effective_tool_buffer=min(40_000, int(8_192 * TOOL_BUFFER_MAX_FRACTION)),
            disabled_reason=DISABLED_REASON_INSUFFICIENT_HEADROOM,
        )
        emit_compaction_config(
            callback,
            agent_name="a",
            model="m",
            context_window=8_192,
            context_window_source="fallback",
            output_limit=16_384,
            output_limit_source="default",
            enabled=plan.enabled,
            disabled_reason=plan.disabled_reason,
            trigger_tokens=plan.trigger_tokens,
            target_tokens=plan.target_tokens,
            tool_buffer=40_000,
            effective_tool_buffer=min(40_000, int(8_192 * TOOL_BUFFER_MAX_FRACTION)),
        )
        assert len(events) == 1
        payload = events[0][1]
        assert payload["enabled"] is False
        assert payload["disabled_reason"] == DISABLED_REASON_INSUFFICIENT_HEADROOM
        assert payload["trigger_tokens"] is None
        assert payload["target_tokens"] is None

    def test_two_plans_are_independent(self) -> None:
        # Requirement: concurrently built plans share no state.
        a = resolve_compaction_plan(window=128_000, output_limit=16_384, tool_buffer=40_000)
        b = resolve_compaction_plan(window=8_192, output_limit=16_384, tool_buffer=40_000)
        assert a.enabled is True
        assert b.enabled is False
        assert a.disabled_reason != b.disabled_reason


class TestTargetFormula:
    """Tests for target_tokens math."""

    def test_small_window_clamps_below_trigger(self) -> None:
        # 128k window + default output/buffer => trigger 79_616, raw 55% ceiling 70_400.
        trigger = trigger_tokens(128_000, 16_384, 40_000)
        assert trigger == 79_616
        assert target_tokens(128_000, trigger) == 70_400

    def test_large_window_uses_fraction(self) -> None:
        # 1M window + default output/buffer => trigger 943_616, raw 55% ceiling 550_000.
        trigger = trigger_tokens(1_000_000, 16_384, 40_000)
        assert target_tokens(1_000_000, trigger) == 550_000

    def test_floor(self) -> None:
        # Degenerate inputs collapse to a minimum target of 1.
        assert target_tokens(1, 1) == 1


class TestProxyUnknownModel:
    """Adversarial branch: custom base URL + unknown model."""

    async def test_proxy_unknown_model_branch(self) -> None:
        """Provider metadata unavailable, registry suppressed, window=128k.

        The fallback output limit is the unified default (16384) because the
        user did not configure max_tokens. The default tool buffer remains 40000.
        """
        provider = _MockProvider()

        window = await resolve_compaction_window(
            provider=provider,
            model="some-proxy-model",
            has_custom_base_url=True,
        )
        assert window == WindowResolution(
            tokens=FALLBACK_CONTEXT_WINDOW,
            source="fallback",
        )

        output_limit = await resolve_output_limit(
            provider=provider,
            model="some-proxy-model",
            effective_max_tokens=DEFAULT_MAX_TOKENS,
            user_configured=False,
        )
        assert output_limit == OutputLimitResolution(
            tokens=DEFAULT_MAX_TOKENS,
            source="default",
        )

        assert trigger_tokens(window.tokens, output_limit.tokens, 40_000) == 79_616


class TestSlimmedCascade:
    """Regression pins for the slimmed cascade (provider -> registry -> fallback)."""

    async def test_provider_advertised_limits_cascade(self) -> None:
        """Stub provider returning (200_000, 131_072) advertised limits."""
        provider = _MockProvider(max_prompt=200_000, max_output=131_072)

        window = await resolve_compaction_window(
            provider=provider,
            model="vendor-model",
            has_custom_base_url=False,
        )
        assert window == WindowResolution(tokens=200_000, source="provider")

        output_limit = await resolve_output_limit(
            provider=provider,
            model="vendor-model",
            effective_max_tokens=16_384,
            user_configured=False,
        )
        assert output_limit == OutputLimitResolution(tokens=16_384, source="default")

    async def test_provider_advertised_cap_clamps_output(self) -> None:
        """Inverse case: cap 8192 < 16384 yields provider-cap clamp."""
        provider = _MockProvider(max_prompt=200_000, max_output=8192)

        window = await resolve_compaction_window(
            provider=provider,
            model="vendor-model",
            has_custom_base_url=False,
        )
        assert window == WindowResolution(tokens=200_000, source="provider")

        output_limit = await resolve_output_limit(
            provider=provider,
            model="vendor-model",
            effective_max_tokens=16_384,
            user_configured=False,
        )
        assert output_limit == OutputLimitResolution(tokens=8192, source="provider-cap")

    async def test_first_party_registry_branch_untouched(self) -> None:
        """First-party registry branch resolves model window when provider returns None."""
        provider = _MockProvider(max_prompt=None, max_output=None)

        result = await resolve_compaction_window(
            provider=provider,
            model="gpt-5.2",
            has_custom_base_url=False,
        )

        assert result == WindowResolution(tokens=400_000, source="registry")
