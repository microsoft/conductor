"""Unit tests for Conductor-level retry wrapper used by the Pydantic AI provider.

These tests verify that ``execute_with_retry`` mirrors ``ClaudeProvider`` retry
semantics, that Pydantic AI's own retry budget is disabled, and that the
``agent_retry`` event payload matches the legacy provider.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import Mock, patch

import pytest

from conductor.config.schema import AgentDef, RetryPolicy
from conductor.exceptions import ProviderError, ValidationError
from conductor.providers._pydantic_ai.agent_builder import build_agent
from conductor.providers._pydantic_ai.retry import (
    RetryConfig,
    _calculate_delay,
    _classify_error,
    _extract_status_code,
    _get_retry_after,
    _is_retryable_error,
    _resolve_retry_config,
    execute_with_retry,
)


class MockRateLimitError(Exception):
    """Fake Anthropic RateLimitError with a retry-after header."""

    status_code = 429
    response = Mock(headers={"retry-after": "60"})


class MockAPIConnectionError(Exception):
    """Fake Anthropic APIConnectionError."""


class MockAPITimeoutError(Exception):
    """Fake Anthropic APITimeoutError."""


class MockAPIStatusError(Exception):
    """Fake Anthropic APIStatusError."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class MockBadRequestError(Exception):
    """Fake Anthropic BadRequestError."""

    status_code = 400


@pytest.fixture(autouse=True)
def _ensure_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a dummy API key so AnthropicModel construction succeeds."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _make_factory(
    outcomes: list[Any | Exception],
) -> Callable[[], Coroutine[Any, Any, Any]]:
    """Return a coroutine factory that yields outcomes in order."""
    call_count = 0

    async def _factory() -> Any:
        nonlocal call_count
        outcome = outcomes[call_count]
        call_count += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return _factory


class TestRetryClassification:
    """Tests for error classification parity with ClaudeProvider."""

    def test_provider_error_respects_is_retryable(self) -> None:
        """ProviderError.is_retryable must be honored directly."""
        assert _is_retryable_error(ProviderError("retry", is_retryable=True)) is True
        assert _is_retryable_error(ProviderError("no", is_retryable=False)) is False

    def test_anthropic_connection_errors_are_retryable(self) -> None:
        """Anthropic connection/timeout/rate-limit errors must be retryable."""
        assert _is_retryable_error(MockAPIConnectionError("fail")) is True
        assert _is_retryable_error(MockAPITimeoutError("fail")) is True
        assert _is_retryable_error(MockRateLimitError("fail")) is True

    def test_anthropic_5xx_and_429_are_retryable(self) -> None:
        """Anthropic APIStatusError 5xx and 429 must be retryable."""
        assert _is_retryable_error(MockAPIStatusError("500", 500)) is True
        assert _is_retryable_error(MockAPIStatusError("503", 503)) is True
        assert _is_retryable_error(MockAPIStatusError("429", 429)) is True

    def test_anthropic_4xx_are_not_retryable(self) -> None:
        """Anthropic 4xx errors (except 429) must be fatal."""
        assert _is_retryable_error(MockAPIStatusError("400", 400)) is False
        assert _is_retryable_error(MockAPIStatusError("401", 401)) is False
        assert _is_retryable_error(MockAPIStatusError("403", 403)) is False
        assert _is_retryable_error(MockAPIStatusError("404", 404)) is False

    def test_bad_request_error_is_not_retryable(self) -> None:
        """Anthropic BadRequestError must be fatal."""
        assert _is_retryable_error(MockBadRequestError("bad request")) is False

    def test_generic_errors_are_not_retryable(self) -> None:
        """Arbitrary Python exceptions must be treated as fatal."""
        assert _is_retryable_error(ValueError("fail")) is False
        assert _is_retryable_error(RuntimeError("fail")) is False

    def test_classify_error_categories(self) -> None:
        """Errors must be classified into 'timeout' or 'provider_error'."""
        assert _classify_error(TimeoutError()) == "timeout"
        assert _classify_error(ProviderError("timeout", status_code=408)) == "timeout"
        assert _classify_error(ValueError("fail")) == "provider_error"


class TestRetryConfig:
    """Tests for retry configuration resolution."""

    def test_resolve_from_retry_policy(self) -> None:
        """AgentDef.retry must map to RetryConfig with per-agent overrides."""
        agent = AgentDef(
            name="retryer",
            retry=RetryPolicy(
                max_attempts=5,
                backoff="fixed",
                delay_seconds=3.0,
                retry_on=["timeout"],
                max_parse_recovery_attempts=4,
            ),
        )
        default = RetryConfig(max_delay=60.0, jitter=0.1, max_parse_recovery_attempts=2)

        config = _resolve_retry_config(agent, default)

        assert config.max_attempts == 5
        assert config.base_delay == 3.0
        assert config.max_delay == 60.0
        assert config.jitter == 0.1
        assert config.backoff == "fixed"
        assert config.retry_on == ["timeout"]
        assert config.max_parse_recovery_attempts == 4

    def test_fallback_to_default_when_no_retry_policy(self) -> None:
        """AgentDef without retry must use the provider default RetryConfig."""
        agent = AgentDef(name="no-retry")
        default = RetryConfig(max_attempts=7, base_delay=0.5)

        config = _resolve_retry_config(agent, default)

        assert config.max_attempts == 7
        assert config.base_delay == 0.5


class TestRetryDelay:
    """Tests for backoff and retry-after behavior."""

    def test_exponential_delay_no_jitter(self) -> None:
        """Exponential backoff without jitter must double each attempt."""
        config = RetryConfig(base_delay=1.0, max_delay=100.0, jitter=0.0)
        assert _calculate_delay(1, config) == 1.0
        assert _calculate_delay(2, config) == 2.0
        assert _calculate_delay(3, config) == 4.0

    def test_fixed_delay(self) -> None:
        """Fixed backoff must return base_delay plus jitter."""
        config = RetryConfig(base_delay=2.0, max_delay=100.0, backoff="fixed", jitter=0.0)
        assert _calculate_delay(1, config) == 2.0
        assert _calculate_delay(3, config) == 2.0

    def test_delay_capped_at_max_delay(self) -> None:
        """Exponential delay must be capped at max_delay."""
        config = RetryConfig(base_delay=10.0, max_delay=15.0, jitter=0.0)
        assert _calculate_delay(3, config) == 15.0

    def test_retry_after_header_overrides_delay(self) -> None:
        """Rate-limit retry-after header must be extracted and override backoff."""
        err = MockRateLimitError("rate limited")
        assert _get_retry_after(err) == 60.0

    def test_extract_status_code_from_api_status_error(self) -> None:
        """HTTP status code must be extracted from APIStatusError-like errors."""
        assert _extract_status_code(MockAPIStatusError("503", 503)) == 503
        assert _extract_status_code(MockBadRequestError("bad")) == 400


class TestExecuteWithRetry:
    """Tests for the execute_with_retry wrapper."""

    @pytest.mark.asyncio
    async def test_retryable_error_retried_and_succeeds(self) -> None:
        """A retryable error followed by success must emit agent_retry and return the result."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        config = RetryConfig(max_attempts=3, base_delay=0.0, jitter=0.0)
        factory = _make_factory([MockAPIConnectionError("transient"), "success"])

        with patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep:
            result = await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=callback,
                agent_name="retryer",
            )

        assert result == "success"
        assert len(events) == 1
        assert events[0][0] == "agent_retry"
        assert events[0][1] == {
            "agent_name": "retryer",
            "attempt": 1,
            "max_attempts": 3,
            "error": "transient",
            "error_type": "MockAPIConnectionError",
            "delay": 0.0,
        }
        mock_sleep.assert_called_once_with(0.0)

    @pytest.mark.asyncio
    async def test_retryable_error_succeeds_on_nth_attempt(self) -> None:
        """Retry must succeed on the Nth attempt and emit one event per prior failure."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        config = RetryConfig(max_attempts=4, base_delay=0.0, jitter=0.0)
        factory = _make_factory(
            [
                MockAPIConnectionError("fail 1"),
                MockAPIConnectionError("fail 2"),
                "success",
            ]
        )

        with patch("conductor.providers._pydantic_ai.retry.asyncio.sleep"):
            result = await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=callback,
                agent_name="retryer",
            )

        assert result == "success"
        assert len(events) == 2
        assert [event[1]["attempt"] for event in events] == [1, 2]

    @pytest.mark.asyncio
    async def test_fatal_error_raises_immediately(self) -> None:
        """Non-retryable errors must raise ProviderError immediately without retry."""
        callback = Mock()

        config = RetryConfig(max_attempts=3, base_delay=0.0, jitter=0.0)
        factory = _make_factory([ValueError("fatal")])

        with (
            patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep,
            pytest.raises(ProviderError) as exc_info,
        ):
            await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=callback,
                agent_name="retryer",
            )

        assert exc_info.value.is_retryable is False
        assert "fatal" in str(exc_info.value)
        callback.assert_not_called()
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_retries_exhausted_raises(self) -> None:
        """Exhausting all retry attempts must raise a non-retryable ProviderError."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        config = RetryConfig(max_attempts=3, base_delay=0.0, jitter=0.0)
        factory = _make_factory([MockAPIConnectionError("fail")] * 5)

        with (
            patch("conductor.providers._pydantic_ai.retry.asyncio.sleep"),
            pytest.raises(ProviderError) as exc_info,
        ):
            await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=callback,
                agent_name="retryer",
            )

        assert exc_info.value.is_retryable is False
        assert "after 3 attempts" in str(exc_info.value)
        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_validation_error_not_retried(self) -> None:
        """Conductor ValidationError must be re-raised without retry."""
        callback = Mock()

        config = RetryConfig(max_attempts=3)
        factory = _make_factory([ValidationError("schema mismatch")])

        with (
            patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep,
            pytest.raises(ValidationError),
        ):
            await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=callback,
                agent_name="retryer",
            )

        callback.assert_not_called()
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_on_filter_honored(self) -> None:
        """Per-agent retry_on must filter which error categories are retried."""
        callback = Mock()

        config = RetryConfig(
            max_attempts=3,
            base_delay=0.0,
            jitter=0.0,
            retry_on=["timeout"],
        )
        factory = _make_factory([MockAPIStatusError("500", 500)])

        with (
            patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep,
            pytest.raises(MockAPIStatusError),
        ):
            await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=callback,
                agent_name="retryer",
            )

        callback.assert_not_called()
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_after_header_respected(self) -> None:
        """Retry-after header must override calculated backoff."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        config = RetryConfig(max_attempts=2, base_delay=1.0, jitter=0.0)
        factory = _make_factory([MockRateLimitError("rate limited"), "ok"])

        with patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep:
            result = await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=callback,
                agent_name="retryer",
            )

        assert result == "ok"
        assert len(events) == 1
        assert events[0][1]["delay"] == 60.0
        mock_sleep.assert_called_once_with(60.0)

    @pytest.mark.asyncio
    async def test_retry_event_callback_errors_swallowed(self) -> None:
        """A failing event callback must not break the retry loop."""

        def bad_callback(event_type: str, payload: dict[str, Any]) -> None:
            raise RuntimeError("callback exploded")

        config = RetryConfig(max_attempts=2, base_delay=0.0, jitter=0.0)
        factory = _make_factory([MockAPIConnectionError("fail"), "ok"])

        with patch("conductor.providers._pydantic_ai.retry.asyncio.sleep"):
            result = await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=bad_callback,
                agent_name="retryer",
            )

        assert result == "ok"

    @pytest.mark.asyncio
    async def test_event_payload_matches_claude_provider(self) -> None:
        """agent_retry payload must contain the same keys as ClaudeProvider."""
        payload: dict[str, Any] | None = None

        def callback(event_type: str, data: dict[str, Any]) -> None:
            nonlocal payload
            if event_type == "agent_retry":
                payload = data

        config = RetryConfig(max_attempts=3, base_delay=0.0, jitter=0.0)
        factory = _make_factory([MockAPIConnectionError("boom"), "ok"])

        with patch("conductor.providers._pydantic_ai.retry.asyncio.sleep"):
            await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=callback,
                agent_name="claude-mirror",
            )

        assert payload is not None
        assert payload.keys() == {
            "agent_name",
            "attempt",
            "max_attempts",
            "error",
            "error_type",
            "delay",
        }


class TestPydanticAIRetriesDisabled:
    """Tests that Pydantic AI's own retry mechanism is disabled."""

    def test_pydantic_ai_internal_retries_are_zero(self) -> None:
        """Pydantic AI internal retries must be disabled so Conductor controls retries."""
        agent_def = AgentDef(name="single-shot")

        pydantic_agent = build_agent(agent_def, system_prompt="", rendered_prompt="")

        assert pydantic_agent._max_output_retries == 0
        assert pydantic_agent._max_tool_retries == 0
