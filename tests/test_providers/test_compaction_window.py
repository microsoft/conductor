"""Unit tests for compaction window/output-limit resolution."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from conductor.config.schema import ToolOutputConfig
from conductor.providers._pydantic_ai.compaction_window import (
    DEFAULT_MAX_TOKENS,
    FALLBACK_CONTEXT_WINDOW,
    OutputLimitResolution,
    WindowResolution,
    resolve_compaction_window,
    resolve_output_limit,
    target_tokens,
    tool_buffer_tokens,
    trigger_tokens,
)
from conductor.providers.base import AgentProvider


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
        # Default output_limit (16384) + default buffer (40000) on common windows.
        assert trigger_tokens(128_000, 16_384, 40_000) == 71_616
        assert trigger_tokens(200_000, 16_384, 40_000) == 143_616
        assert trigger_tokens(1_000_000, 16_384, 40_000) == 943_616
        # Larger output limit with default buffer.
        assert trigger_tokens(128_000, 64_000, 40_000) == 24_000

    def test_small_window_floor(self) -> None:
        # Degenerate windows collapse to a minimum trigger of 1.
        assert trigger_tokens(1, 1, 1) == 1
        assert trigger_tokens(40_000, 40_000, 1) == 1

    def test_huge_buffer_degenerates_to_one(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import conductor.providers._pydantic_ai.compaction_window as cw

        cw._trigger_degenerate_warned = False
        with caplog.at_level(
            logging.WARNING,
            logger="conductor.providers._pydantic_ai.compaction_window",
        ):
            # 128k window cannot hold 16_384 + 265_000 of reserve.
            result = trigger_tokens(128_000, 16_384, 265_000)

        assert result == 1
        assert "degenerated" in caplog.text.lower()


class TestTargetFormula:
    """Tests for target_tokens math."""

    def test_small_window_clamps_below_trigger(self) -> None:
        # 128k window + default output/buffer => trigger 71_616, raw 55% ceiling 70_400.
        trigger = trigger_tokens(128_000, 16_384, 40_000)
        assert trigger == 71_616
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

        assert trigger_tokens(window.tokens, output_limit.tokens, 40_000) == 71_616
