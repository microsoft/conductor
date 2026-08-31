"""Unit tests for compaction window/output-limit resolution."""

from __future__ import annotations

import logging
import os
from typing import Any

import pytest

from conductor.providers._pydantic_ai.compaction_window import (
    DEFAULT_OUTPUT_LIMIT,
    ENV_CONTEXT_WINDOW,
    FALLBACK_CONTEXT_WINDOW,
    OutputLimitResolution,
    WindowResolution,
    resolve_compaction_window,
    resolve_output_limit,
    target_tokens,
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


def _clear_env() -> None:
    """Remove the env override before a test."""
    os.environ.pop(ENV_CONTEXT_WINDOW, None)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure every test starts without the env override set."""
    monkeypatch.delenv(ENV_CONTEXT_WINDOW, raising=False)


class TestResolveCompactionWindow:
    """Tests for the context-window resolution cascade."""

    async def test_env_override_wins(self) -> None:
        os.environ[ENV_CONTEXT_WINDOW] = "200000"
        provider = _MockProvider(max_prompt=128_000)

        result = await resolve_compaction_window(
            provider=provider,
            model="gpt-5.2",
            has_custom_base_url=False,
        )

        assert result == WindowResolution(tokens=200_000, source="env")

    async def test_invalid_env_falls_through(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        os.environ[ENV_CONTEXT_WINDOW] = "abc"
        provider = _MockProvider(max_prompt=128_000)

        with caplog.at_level(
            logging.WARNING,
            logger="conductor.providers._pydantic_ai.compaction_window",
        ):
            result = await resolve_compaction_window(
                provider=provider,
                model="gpt-5.2",
                has_custom_base_url=False,
            )

        assert result == WindowResolution(tokens=128_000, source="provider")
        assert "Ignoring invalid" in caplog.text

    async def test_zero_env_falls_through(self, caplog: pytest.LogCaptureFixture) -> None:
        os.environ[ENV_CONTEXT_WINDOW] = "0"

        result = await resolve_compaction_window(
            provider=_MockProvider(),
            model="gpt-5.2",
            has_custom_base_url=False,
        )

        assert result.source == "registry"
        assert result.tokens > 0

    async def test_negative_env_falls_through(self, caplog: pytest.LogCaptureFixture) -> None:
        os.environ[ENV_CONTEXT_WINDOW] = "-100"

        result = await resolve_compaction_window(
            provider=_MockProvider(),
            model="gpt-5.2",
            has_custom_base_url=False,
        )

        assert result.source == "registry"
        assert result.tokens > 0

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
        os.environ[ENV_CONTEXT_WINDOW] = "abc"
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

    async def test_provider_output_limit_wins(self) -> None:
        provider = _MockProvider(max_output=32_000)

        result = await resolve_output_limit(
            provider=provider,
            model="gpt-5.2",
            has_custom_base_url=False,
        )

        assert result == OutputLimitResolution(tokens=32_000, source="provider")

    async def test_provider_output_limit_absent_uses_default(self) -> None:
        provider = _MockProvider()

        result = await resolve_output_limit(
            provider=provider,
            model="gpt-5.2",
            has_custom_base_url=False,
        )

        assert result == OutputLimitResolution(
            tokens=DEFAULT_OUTPUT_LIMIT,
            source="default",
        )

    async def test_custom_base_url_skips_registry_to_default(self) -> None:
        provider = _MockProvider()

        result = await resolve_output_limit(
            provider=provider,
            model="gpt-5.2",
            has_custom_base_url=True,
        )

        assert result == OutputLimitResolution(
            tokens=DEFAULT_OUTPUT_LIMIT,
            source="default",
        )

    async def test_provider_raises_uses_default(self) -> None:
        provider = _MockProvider(output_raises=True)

        result = await resolve_output_limit(
            provider=provider,
            model="gpt-5.2",
            has_custom_base_url=False,
        )

        assert result == OutputLimitResolution(
            tokens=DEFAULT_OUTPUT_LIMIT,
            source="default",
        )


class TestTriggerFormula:
    """Tests for trigger_tokens math."""

    def test_worked_examples(self) -> None:
        assert trigger_tokens(128_000, 64_000) == 64_000
        assert trigger_tokens(200_000, 32_000) == 160_000
        assert trigger_tokens(1_000_000, 64_000) == 936_000

    def test_output_limit_below_buffer(self) -> None:
        # When the output limit is smaller than TRIGGER_BUFFER, the buffer
        # dominates the reserve.
        assert trigger_tokens(128_000, 16_000) == 88_000

    def test_small_window_floor(self) -> None:
        # Degenerate windows collapse to a minimum trigger of 1.
        assert trigger_tokens(1, 1) == 1
        assert trigger_tokens(40_000, 40_000) == 1


class TestTargetFormula:
    """Tests for target_tokens math."""

    def test_small_window_clamps_below_trigger(self) -> None:
        # 128k window + 64k output => trigger 64k, raw 55% ceiling 70.4k.
        # The clamp pulls it strictly below the trigger.
        assert target_tokens(128_000, trigger_tokens(128_000, 64_000)) == 63_999

    def test_large_window_uses_fraction(self) -> None:
        # 1M window + 64k output => trigger 936k, raw 55% ceiling 550k.
        # Clamp inactive.
        assert target_tokens(1_000_000, trigger_tokens(1_000_000, 64_000)) == 550_000

    def test_floor(self) -> None:
        # Degenerate inputs collapse to a minimum target of 1.
        assert target_tokens(1, 1) == 1


class TestProxyUnknownModel:
    """Adversarial branch: custom base URL + unknown model."""

    async def test_proxy_unknown_model_branch(self) -> None:
        """Provider metadata unavailable, registry suppressed, window=128k.

        This pins the under-reserve fix: the resolved output limit is the
        conservative default (64k), so the trigger is 64k, not 88k.
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
            has_custom_base_url=True,
        )
        assert output_limit == OutputLimitResolution(
            tokens=DEFAULT_OUTPUT_LIMIT,
            source="default",
        )

        assert trigger_tokens(window.tokens, output_limit.tokens) == 64_000
