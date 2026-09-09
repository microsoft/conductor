"""Unit tests for Conductor-level retry wrapper used by the Pydantic AI provider.

These tests verify that ``execute_with_retry`` mirrors ``ClaudeProvider`` retry
semantics, that Pydantic AI's own retry budget is disabled, and that the
``agent_retry`` event payload matches the legacy provider.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import Mock, patch

import httpx
import openai
import pytest
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior

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


def _make_http_request() -> httpx.Request:
    """Return a minimal httpx request for constructing openai exceptions."""
    return httpx.Request("GET", "http://example.com")


def _make_openai_rate_limit_error(retry_after: str | None = None) -> openai.RateLimitError:
    """Return a real openai RateLimitError with an optional Retry-After header."""
    request = _make_http_request()
    headers: dict[str, str] = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    response = httpx.Response(429, text="rate limited", headers=headers, request=request)
    return openai.RateLimitError("rate limited", response=response, body=None)


def _make_openai_status_error(status_code: int) -> openai.APIStatusError:
    """Return a real openai APIStatusError subclass for the given status code."""
    request = _make_http_request()
    response = httpx.Response(status_code, text="boom", request=request)
    mapping: dict[int, type[openai.APIStatusError]] = {
        400: openai.BadRequestError,
        401: openai.AuthenticationError,
        403: openai.PermissionDeniedError,
        404: openai.NotFoundError,
        429: openai.RateLimitError,
        500: openai.InternalServerError,
    }
    cls = mapping.get(status_code, openai.APIStatusError)
    return cls("boom", response=response, body=None)


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

    def test_unexpected_model_behavior_is_retryable(self) -> None:
        """UnexpectedModelBehavior must be retryable."""
        err = UnexpectedModelBehavior("Exceeded maximum output retries (2)")
        assert _is_retryable_error(err) is True

    def test_model_http_error_429_and_5xx_are_retryable(self) -> None:
        """pydantic-ai's translated ModelHTTPError must be retryable at 429/5xx.

        Regression test for issue #454: pydantic_ai.models.anthropic
        translates the Anthropic SDK's APIStatusError/RateLimitError into
        ModelHTTPError before Conductor ever sees them.
        """
        assert _is_retryable_error(ModelHTTPError(status_code=429, model_name="claude-x")) is True
        assert _is_retryable_error(ModelHTTPError(status_code=500, model_name="claude-x")) is True
        assert _is_retryable_error(ModelHTTPError(status_code=503, model_name="claude-x")) is True

    def test_model_http_error_4xx_are_not_retryable(self) -> None:
        """A ModelHTTPError for a client error must stay fatal.

        Guards against the broader ModelAPIError arm (ModelHTTPError is a
        subclass of ModelAPIError) swallowing 4xx errors.
        """
        assert _is_retryable_error(ModelHTTPError(status_code=400, model_name="claude-x")) is False
        assert _is_retryable_error(ModelHTTPError(status_code=401, model_name="claude-x")) is False

    def test_bare_model_api_error_is_retryable(self) -> None:
        """A bare ModelAPIError (connection/timeout translation) must be retryable."""
        err = ModelAPIError(model_name="claude-x", message="Connection error.")
        assert _is_retryable_error(err) is True

    def test_model_api_error_has_no_other_subclasses(self) -> None:
        """Canary: the blanket ``type(exception) is ModelAPIError`` retryable
        arm is only safe while ``ModelHTTPError`` (already handled above and
        returned first) is the *only* subclass of ``ModelAPIError``. If
        pydantic-ai adds e.g. an auth or quota error as a new subclass, this
        must fail loudly rather than let it silently become retryable."""
        assert ModelAPIError.__subclasses__() == [ModelHTTPError]


class TestOpenAIErrorClassification:
    """Tests for pydantic-ai wrapped and raw openai error classification."""

    def test_model_http_error_429_408_and_5xx_are_retryable(self) -> None:
        """Wrapped ModelHTTPError 429/408/5xx must be retryable."""
        assert _is_retryable_error(ModelHTTPError(429, model_name="gpt-4o")) is True
        assert _is_retryable_error(ModelHTTPError(408, model_name="gpt-4o")) is True
        assert _is_retryable_error(ModelHTTPError(500, model_name="gpt-4o")) is True
        assert _is_retryable_error(ModelHTTPError(503, model_name="gpt-4o")) is True

    def test_model_http_error_400_401_403_404_are_fatal(self) -> None:
        """Wrapped ModelHTTPError 400/401/403/404 must be fatal."""
        assert _is_retryable_error(ModelHTTPError(400, model_name="gpt-4o")) is False
        assert _is_retryable_error(ModelHTTPError(401, model_name="gpt-4o")) is False
        assert _is_retryable_error(ModelHTTPError(403, model_name="gpt-4o")) is False
        assert _is_retryable_error(ModelHTTPError(404, model_name="gpt-4o")) is False

    def test_model_api_error_is_retryable(self) -> None:
        """Wrapped ModelAPIError must be retryable."""
        err = ModelAPIError(model_name="gpt-4o", message="stream interrupted")
        assert _is_retryable_error(err) is True

    def test_raw_openai_rate_limit_is_retryable(self) -> None:
        """Raw openai RateLimitError must be retryable."""
        assert _is_retryable_error(_make_openai_rate_limit_error()) is True

    def test_raw_openai_api_status_5xx_and_429_are_retryable(self) -> None:
        """Raw openai APIStatusError 5xx and 429 must be retryable."""
        assert _is_retryable_error(_make_openai_status_error(500)) is True
        assert _is_retryable_error(_make_openai_status_error(503)) is True
        assert _is_retryable_error(_make_openai_status_error(429)) is True

    def test_raw_openai_stream_validation_error_is_fatal(self) -> None:
        # Requirement: a structured client-side error shaped like the one the
        # SDK raises from an SSE body must not become retryable merely because
        # the SDK reports it as a base APIError.
        error = openai.APIError(
            message="invalid request",
            request=_make_http_request(),
            body={"type": "invalid_request_error", "code": "invalid_value"},
        )

        assert _is_retryable_error(error) is False

    def test_raw_openai_stream_rate_limit_error_is_retryable(self) -> None:
        # Requirement: a rate limit delivered mid-stream must be retryable in
        # every shape OpenAI and Anthropic-shaped gateways send it. OpenAI's
        # real 429 payload is type "requests"/"tokens" with code
        # "rate_limit_exceeded" (literals in openai/types/beta/threads/run.py);
        # "rate_limit_error" is the Anthropic vocabulary a gateway like
        # LiteLLM proxies unchanged. The third row matches on the code marker
        # alone — it is what pins "rate_limit_exceeded" in the retryable set.
        request = _make_http_request()
        for body in (
            {"type": "requests", "code": "rate_limit_exceeded"},
            {"type": "tokens", "code": "rate_limit_exceeded"},
            {"type": "rate_limit_error"},
            {"type": "throttled", "code": "rate_limit_exceeded"},
        ):
            error = openai.APIError("slow down", request, body=body)
            assert _is_retryable_error(error) is True, body

    def test_raw_openai_stream_gateway_overload_errors_are_retryable(self) -> None:
        # Requirement: Anthropic-shaped gateways proxy their transient 5xx
        # vocabulary ("overloaded_error", "api_error") onto OpenAI-compatible
        # endpoints unchanged; both must retry.
        request = _make_http_request()
        for body in ({"type": "overloaded_error"}, {"type": "api_error"}):
            error = openai.APIError("upstream overloaded", request, body=body)
            assert _is_retryable_error(error) is True, body

    @pytest.mark.parametrize(
        "marker",
        [
            "server_error",
            "internal_server_error",
            "requests",
            "tokens",
            "rate_limit_exceeded",
            "rate_limit_error",
            "overloaded_error",
            "api_error",
        ],
    )
    def test_every_documented_stream_marker_is_retryable_as_type_and_code(
        self, marker: str
    ) -> None:
        # Requirement: every marker in the retryable set is load-bearing in
        # both payload positions the classifier checks, so deleting one from
        # the set is a loud failure rather than a silent behavior change.
        request = _make_http_request()
        assert (
            _is_retryable_error(openai.APIError("boom", request, body={"type": marker})) is True
        ), marker
        assert (
            _is_retryable_error(
                openai.APIError("boom", request, body={"type": "unrelated", "code": marker})
            )
            is True
        ), marker

    def test_openai_status_subclasses_are_not_captured_by_the_bare_arm(self) -> None:
        # Requirement: the bare-APIError arm is an exact-type check — an
        # APIStatusError subclass must keep its status-based classification
        # even when its payload carries a marker from the retryable set (or
        # a fatal one). This is the test that fails if the check is loosened
        # to isinstance.
        request = _make_http_request()
        assert (
            _is_retryable_error(
                openai.BadRequestError(
                    "bad",
                    response=httpx.Response(400, request=request),
                    body={"type": "server_error"},
                )
            )
            is False
        )
        assert (
            _is_retryable_error(
                openai.InternalServerError(
                    "boom",
                    response=httpx.Response(500, request=request),
                    body={"type": "invalid_request_error"},
                )
            )
            is True
        )

    @pytest.mark.parametrize("body", [None, "upstream overloaded", [1, 2], {"code": "x"}])
    def test_raw_openai_api_error_without_payload_type_is_retryable(self, body: object) -> None:
        # Requirement: a mid-stream error with no parseable `type` — a
        # non-object `error` value from an Ollama/vLLM gateway, or an
        # Azure-style {"code": ...} shape — is treated like a broken stream
        # (retryable), since auth and request validation have already passed
        # once streaming starts.
        assert _is_retryable_error(openai.APIError("boom", _make_http_request(), body=body)) is True

    def test_raw_openai_api_error_with_non_string_type_is_fatal(self) -> None:
        # Requirement: the SDK does not coerce `type` — a payload of
        # {"type": 500} arrives as an int. It must not crash classification,
        # and an unknown marker stays fatal.
        assert (
            _is_retryable_error(openai.APIError("boom", _make_http_request(), body={"type": 500}))
            is False
        )

    def test_pydantic_ai_does_not_translate_bare_openai_api_error(self) -> None:
        # Canary: the bare-APIError classification arm is only reachable while
        # pydantic-ai's ``_map_api_errors`` lets a bare ``openai.APIError``
        # through untranslated. If upstream widens its ``except`` clause, this
        # test fails — and the fatal classification for client-side payload
        # types must be revisited, because those errors would then arrive as
        # ``ModelAPIError``, which is unconditionally retryable.
        from pydantic_ai.models.openai import _map_api_errors

        error = openai.APIError(
            "boom",
            _make_http_request(),
            body={"type": "invalid_request_error"},
        )
        with pytest.raises(openai.APIError) as exc_info, _map_api_errors("gpt-4o"):
            raise error
        assert exc_info.value is error

    def test_sdk_stream_error_payload_shape_is_what_we_classify(self) -> None:
        # Canary: drive a real ``openai.Stream`` over an SSE body carrying an
        # error event and assert the SDK puts the error object itself into
        # ``APIError.body`` (so `.type` sits at the top level, which is what
        # the classifier reads). If a future SDK release nests the payload one
        # level deeper, `.type` becomes None and the marker-based
        # classification silently stops matching — this test fails instead.
        with openai.OpenAI(api_key="test-key") as client:
            request = httpx.Request("POST", "http://example.com/v1/chat/completions")
            response = httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    b'data: {"error": {"type": "server_error", "message": "boom"}}\n\n'
                    b"data: [DONE]\n\n"
                ),
                request=request,
            )
            stream = openai.Stream(cast_to=object, response=response, client=client)

            with pytest.raises(openai.APIError) as exc_info:
                for _ in stream:
                    pass

        assert exc_info.value.type == "server_error"
        assert exc_info.value.body == {"type": "server_error", "message": "boom"}
        assert _is_retryable_error(exc_info.value) is True

    def test_sdk_stream_error_with_non_object_error_value(self) -> None:
        # Canary: a gateway sending {"error": "upstream overloaded"} yields a
        # bare APIError with .type None, the raw string body, and the SDK's
        # fallback message — the exact shape the type-less retry branch
        # exists for.
        with openai.OpenAI(api_key="test-key") as client:
            request = httpx.Request("POST", "http://example.com/v1/chat/completions")
            response = httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'data: {"error": "upstream overloaded"}\n\ndata: [DONE]\n\n',
                request=request,
            )
            stream = openai.Stream(cast_to=object, response=response, client=client)

            with pytest.raises(openai.APIError) as exc_info:
                for _ in stream:
                    pass

        assert exc_info.value.type is None
        assert exc_info.value.body == "upstream overloaded"
        assert str(exc_info.value) == "An error occurred during streaming"
        assert _is_retryable_error(exc_info.value) is True

    def test_raw_openai_api_status_4xx_are_fatal(self) -> None:
        """Raw openai APIStatusError 400/401/403/404 must be fatal."""
        assert _is_retryable_error(_make_openai_status_error(400)) is False
        assert _is_retryable_error(_make_openai_status_error(401)) is False
        assert _is_retryable_error(_make_openai_status_error(403)) is False
        assert _is_retryable_error(_make_openai_status_error(404)) is False

    def test_raw_openai_connection_and_timeout_are_retryable(self) -> None:
        """Raw openai APIConnectionError and APITimeoutError must be retryable."""
        request = _make_http_request()
        assert (
            _is_retryable_error(
                openai.APIConnectionError(message="connection reset", request=request)
            )
            is True
        )
        assert _is_retryable_error(openai.APITimeoutError(request=request)) is True

    def test_raw_openai_authentication_and_bad_request_are_fatal(self) -> None:
        """Raw openai AuthenticationError and BadRequestError must be fatal."""
        request = _make_http_request()
        assert (
            _is_retryable_error(
                openai.AuthenticationError(
                    "unauthorized", response=httpx.Response(401, request=request), body=None
                )
            )
            is False
        )
        assert (
            _is_retryable_error(
                openai.BadRequestError(
                    "bad request", response=httpx.Response(400, request=request), body=None
                )
            )
            is False
        )

    def test_extract_status_code_from_model_http_error(self) -> None:
        """_extract_status_code must read ModelHTTPError.status_code."""
        assert _extract_status_code(ModelHTTPError(503, model_name="gpt-4o")) == 503

    def test_extract_status_code_from_raw_openai(self) -> None:
        """_extract_status_code must read raw openai APIStatusError status_code."""
        assert _extract_status_code(_make_openai_status_error(429)) == 429
        assert _extract_status_code(_make_openai_status_error(500)) == 500

    def test_get_retry_after_from_model_http_error_returns_none(self) -> None:
        """Wrapped ModelHTTPError loses Retry-After headers; fallback to backoff."""
        assert _get_retry_after(ModelHTTPError(429, model_name="gpt-4o")) is None

    def test_get_retry_after_from_raw_openai_rate_limit(self) -> None:
        """Raw openai RateLimitError with Retry-After header must be honored."""
        assert _get_retry_after(_make_openai_rate_limit_error("42")) == 42.0

    def test_get_retry_after_from_raw_openai_rate_limit_without_header(self) -> None:
        """Raw openai RateLimitError without header must fall back to backoff."""
        assert _get_retry_after(_make_openai_rate_limit_error()) is None

    @pytest.mark.asyncio
    async def test_execute_with_retry_retries_raw_openai_rate_limit(self) -> None:
        """execute_with_retry must retry a raw openai RateLimitError and respect Retry-After."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        config = RetryConfig(max_attempts=2, base_delay=1.0, jitter=0.0)
        factory = _make_factory([_make_openai_rate_limit_error("3"), "ok"])

        with patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep:
            result = await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=callback,
                agent_name="retryer",
            )

        assert result == "ok"
        assert len(events) == 1
        assert events[0][1]["delay"] == 3.0
        mock_sleep.assert_called_once_with(3.0)

    @pytest.mark.asyncio
    async def test_real_openai_rate_limit_error_retries_once_then_succeeds(self) -> None:
        """Requirement: openai.RateLimitError with Retry-After header flows through
        execute_with_retry, emits an agent_retry event, and succeeds on the next call."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        config = RetryConfig(max_attempts=2, base_delay=1.0, jitter=0.0)
        factory = _make_factory([_make_openai_rate_limit_error("2"), "success"])

        with patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep:
            result = await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=callback,
                agent_name="openai-retryer",
            )

        assert result == "success"
        assert len(events) == 1
        assert events[0][0] == "agent_retry"
        assert events[0][1] == {
            "agent_name": "openai-retryer",
            "attempt": 1,
            "max_attempts": 2,
            "error": "rate limited",
            "error_type": "RateLimitError",
            "delay": 2.0,
        }
        mock_sleep.assert_called_once_with(2.0)

    @pytest.mark.asyncio
    async def test_real_openai_stream_server_error_retries_once_then_succeeds(self) -> None:
        # Requirement: a transient error shaped like the one the SDK raises
        # from an SSE body must consume the configured retry attempt instead
        # of failing after the first call. (The SDK's real SSE parsing is
        # pinned separately by test_sdk_stream_error_payload_shape_is_what_we_classify.)
        attempts = 0
        request = _make_http_request()
        stream_error = openai.APIError(
            message="upstream overloaded",
            request=request,
            body={"type": "server_error", "code": "internal_server_error"},
        )

        async def factory() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise stream_error
            return "success"

        config = RetryConfig(max_attempts=2, base_delay=0.0, jitter=0.0)
        with patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep:
            result = await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=None,
                agent_name="openai-stream-retryer",
            )

        assert result == "success"
        assert attempts == 2
        mock_sleep.assert_called_once_with(0.0)

    @pytest.mark.asyncio
    async def test_real_openai_bad_request_error_is_not_retried(self) -> None:
        """Requirement: openai.BadRequestError is fatal and must not be retried;
        it propagates as a non-retryable ProviderError."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        request = _make_http_request()
        response = httpx.Response(400, text="bad request", request=request)
        bad_request = openai.BadRequestError("bad request", response=response, body=None)
        config = RetryConfig(max_attempts=3, base_delay=0.0, jitter=0.0)
        factory = _make_factory([bad_request])

        with (
            patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep,
            pytest.raises(ProviderError) as exc_info,
        ):
            await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=callback,
                agent_name="openai-bad-request",
            )

        assert exc_info.value.is_retryable is False
        assert exc_info.value.status_code == 400
        assert "bad request" in str(exc_info.value)
        assert events == []
        mock_sleep.assert_not_called()


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

    def test_max_delay_raised_to_delay_seconds_when_larger(self) -> None:
        """A user-stated delay_seconds larger than the provider default must
        raise the cap rather than be silently clamped below it. Issue #454."""
        agent = AgentDef(name="retryer", retry=RetryPolicy(delay_seconds=60.0))
        default = RetryConfig(max_delay=30.0)

        config = _resolve_retry_config(agent, default)

        assert config.max_delay == 60.0

    def test_max_delay_unaffected_when_delay_seconds_smaller(self) -> None:
        """A delay_seconds smaller than the provider default must leave the
        existing cap in place (no regression for existing users)."""
        agent = AgentDef(name="retryer", retry=RetryPolicy(delay_seconds=2.0))
        default = RetryConfig(max_delay=30.0)

        config = _resolve_retry_config(agent, default)

        assert config.max_delay == 30.0


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

    def test_retry_after_recovered_from_model_http_error_cause(self) -> None:
        """A ModelHTTPError's response headers live on its __cause__, since
        pydantic-ai's translation (_map_api_errors) drops them. Issue #454."""
        cause = MockRateLimitError("rate limited")
        err = ModelHTTPError(status_code=429, model_name="claude-x")
        err.__cause__ = cause
        assert _get_retry_after(err) == 60.0

    def test_retry_after_recovered_from_model_http_error_body(self) -> None:
        """A ModelHTTPError with no cause but a body carrying retry_after must
        still surface a delay."""
        err = ModelHTTPError(
            status_code=429,
            model_name="claude-x",
            body={"error": {"retry_after": 45}},
        )
        assert _get_retry_after(err) == 45.0

    def test_retry_after_none_when_no_cause_and_opaque_body(self) -> None:
        """A ModelHTTPError with neither a usable cause nor a recognizable body
        must fall through to None so the caller uses calculated backoff."""
        err = ModelHTTPError(status_code=429, model_name="claude-x", body={"message": "oops"})
        assert _get_retry_after(err) is None

    @pytest.mark.parametrize("bad_value", ["Infinity", "nan", "-5", "0"])
    def test_retry_after_from_body_rejects_non_finite_and_non_positive(
        self, bad_value: str
    ) -> None:
        """A body-supplied retry_after must be finite and positive, or it is
        discarded rather than used verbatim (issue #454 blocking finding:
        Infinity hangs the workflow forever, NaN crashes asyncio.sleep, and a
        negative/zero value burns the retry budget in a hot loop)."""
        err = ModelHTTPError(
            status_code=429,
            model_name="claude-x",
            body={"retry_after": bad_value},
        )
        assert _get_retry_after(err) is None

    def test_retry_after_from_headers_rejects_non_finite_and_non_positive(self) -> None:
        """Same validation must apply to the header path, not just the body."""
        for bad_value in ("Infinity", "nan", "-5", "0"):
            err = MockRateLimitError("rate limited")
            err.response = Mock(headers={"retry-after": bad_value})
            assert _get_retry_after(err) is None

    def test_extract_status_code_from_api_status_error(self) -> None:
        """HTTP status code must be extracted from APIStatusError-like errors."""
        assert _extract_status_code(MockAPIStatusError("503", 503)) == 503
        assert _extract_status_code(MockBadRequestError("bad")) == 400

    def test_execute_with_retry_clamps_retry_after_to_max_delay(self) -> None:
        """A body-supplied retry-after (issue #454's new trusted-input path)
        must still be validated even though it never reaches asyncio.sleep
        directly here — covered end-to-end in TestExecuteWithRetry."""
        err = ModelHTTPError(
            status_code=429,
            model_name="claude-x",
            body={"retry_after": 86400},
        )
        assert _get_retry_after(err) == 86400.0

    def test_clamped_delay_sequence_is_60_60_60(self) -> None:
        """A user-stated delay_seconds of 60 must produce 60s waits at every
        attempt, routed through the real _resolve_retry_config path (issue
        #454). Uses a 5xx rather than a 429 so the delay is calculated
        backoff, not a retry-after override."""
        agent = AgentDef(name="retryer", retry=RetryPolicy(max_attempts=3, delay_seconds=60.0))
        config = _resolve_retry_config(agent, RetryConfig(max_delay=30.0, jitter=0.0))
        factory = _make_factory(
            [
                MockAPIStatusError("server error", 500),
                MockAPIStatusError("server error", 500),
                "ok",
            ]
        )

        async def run() -> str:
            return await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=None,
                agent_name="retryer",
            )

        with patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep:
            result = asyncio.run(run())

        assert result == "ok"
        assert [call.args[0] for call in mock_sleep.call_args_list] == [60.0, 60.0]


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
        # Issue #454 blocking finding: exhaustion must preserve the original
        # error as __cause__ rather than raising bare.
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, MockAPIConnectionError)

    @pytest.mark.asyncio
    async def test_retries_exhausted_on_model_api_error_names_root_cause(self) -> None:
        """Exhaustion on a translated ModelAPIError must name the real
        transport failure rather than only the SDK's hardcoded "Connection
        error." message (issue #454 blocking finding), and must chain the
        original error via __cause__."""
        transport_error = ConnectionError("Name or service not known")
        api_error = ModelAPIError(model_name="claude-x", message="Connection error.")
        api_error.__cause__ = transport_error
        config = RetryConfig(max_attempts=2, base_delay=0.0, jitter=0.0)
        factory = _make_factory([api_error, api_error])

        with (
            patch("conductor.providers._pydantic_ai.retry.asyncio.sleep"),
            pytest.raises(ProviderError) as exc_info,
        ):
            await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=None,
                agent_name="retryer",
            )

        assert exc_info.value.__cause__ is api_error
        assert "Name or service not known" in exc_info.value.suggestion

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
        """Per-agent retry_on must filter which error categories are retried,
        and a declined error must surface as a non-retryable ProviderError
        chained to the original — never as a raw SDK exception escaping past
        callers that catch ProviderError."""
        callback = Mock()
        original = MockAPIStatusError("500", 500)

        config = RetryConfig(
            max_attempts=3,
            base_delay=0.0,
            jitter=0.0,
            retry_on=["timeout"],
        )
        factory = _make_factory([original])

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
        assert exc_info.value.__cause__ is original
        assert exc_info.value.suggestion is not None
        assert "provider_error" in exc_info.value.suggestion
        assert "retry_on" in exc_info.value.suggestion
        callback.assert_not_called()
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_on_filtered_stream_error_surfaces_as_provider_error(self) -> None:
        # Requirement: a stream error this PR makes retryable still reaches the
        # retry_on gate — when the filter declines it, the error must escape as
        # a ProviderError chained to the original, not a raw openai.APIError.
        stream_error = openai.APIError(
            "upstream overloaded",
            _make_http_request(),
            body={"type": "server_error", "code": "internal_server_error"},
        )
        config = RetryConfig(max_attempts=3, base_delay=0.0, jitter=0.0, retry_on=["timeout"])
        factory = _make_factory([stream_error])

        with pytest.raises(ProviderError) as exc_info:
            await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=None,
                agent_name="openai-stream-filtered",
            )

        assert exc_info.value.is_retryable is False
        assert exc_info.value.__cause__ is stream_error
        assert exc_info.value.suggestion is not None
        assert "retry_on" in exc_info.value.suggestion

    @pytest.mark.asyncio
    async def test_fatal_stream_error_message_carries_payload_details(self) -> None:
        # Requirement: a fatal bare APIError must surface its payload type/code
        # in the ProviderError message — str(e) alone does not carry them, and
        # the SDK falls back to a generic "An error occurred during streaming"
        # message when the payload has none.
        error = openai.APIError(
            "invalid request",
            _make_http_request(),
            body={"type": "invalid_request_error", "code": "invalid_value"},
        )
        config = RetryConfig(max_attempts=1, base_delay=0.0, jitter=0.0)
        factory = _make_factory([error])

        with pytest.raises(ProviderError) as exc_info:
            await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=None,
                agent_name="openai-stream-fatal",
            )

        message = str(exc_info.value)
        assert "invalid_request_error" in message
        assert "invalid_value" in message

    @pytest.mark.asyncio
    async def test_declined_retry_logs_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        # Requirement: a declined retry must leave a trace. Conductor installs
        # no logging handlers, so the debug-level classification line never
        # reaches an operator — the decision is logged at warning level,
        # mirroring the retry-taken path.
        factory = _make_factory([ValueError("fatal")])
        config = RetryConfig(max_attempts=3, base_delay=0.0, jitter=0.0)

        with (
            caplog.at_level(logging.WARNING, logger="conductor.providers._pydantic_ai.retry"),
            pytest.raises(ProviderError),
        ):
            await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=None,
                agent_name="retryer",
            )

        assert any("classified non-retryable" in record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_retry_after_header_respected(self) -> None:
        """Retry-after header must override calculated backoff."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        # max_delay must accommodate the 60s server hint, or it is clamped
        # (issue #454: an unvalidated retry-after can bypass max_delay).
        config = RetryConfig(max_attempts=2, base_delay=1.0, max_delay=60.0, jitter=0.0)
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
    async def test_retry_after_header_clamped_to_max_delay(self) -> None:
        """A server retry-after above max_delay must be clamped, not honored
        verbatim (issue #454 blocking finding: unvalidated retry-after can
        bypass max_delay and stall a run for hours)."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        config = RetryConfig(max_attempts=2, base_delay=1.0, max_delay=30.0, jitter=0.0)
        factory = _make_factory([MockRateLimitError("rate limited"), "ok"])

        with patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep:
            result = await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=callback,
                agent_name="retryer",
            )

        assert result == "ok"
        assert events[0][1]["delay"] == 30.0
        mock_sleep.assert_called_once_with(30.0)

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

    @pytest.mark.asyncio
    async def test_unexpected_model_behavior_retries_and_succeeds(self) -> None:
        """execute_with_retry must retry on UnexpectedModelBehavior and succeed later."""
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        config = RetryConfig(max_attempts=3, base_delay=0.0, jitter=0.0)
        factory = _make_factory(
            [
                UnexpectedModelBehavior("Exceeded maximum output retries (2)"),
                "success",
            ]
        )

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
        assert events[0][1]["error_type"] == "UnexpectedModelBehavior"
        mock_sleep.assert_called_once_with(0.0)

    @pytest.mark.asyncio
    async def test_unexpected_model_behavior_exhausted_raises_honest_error(self) -> None:
        """After exhausting attempts, ProviderError must mention structured output."""
        config = RetryConfig(max_attempts=2, base_delay=0.0, jitter=0.0)
        factory = _make_factory(
            [
                UnexpectedModelBehavior("Exceeded maximum output retries (2)"),
                UnexpectedModelBehavior("Exceeded maximum output retries (2)"),
            ]
        )

        with (
            patch("conductor.providers._pydantic_ai.retry.asyncio.sleep"),
            pytest.raises(ProviderError) as exc_info,
        ):
            await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=None,
                agent_name="retryer",
            )

        assert exc_info.value.is_retryable is False
        suggestion = exc_info.value.suggestion
        assert suggestion is not None
        assert "structured output" in suggestion
        assert "output tool" in suggestion
        assert "Check API key" not in suggestion

    @pytest.mark.asyncio
    async def test_unexpected_model_behavior_with_validation_cause_reraises_validation(
        self,
    ) -> None:
        """Requirement: issue #343 parity — an exhausted structured-output
        failure must re-raise the original pydantic ValidationError (naming the
        field and expected type) preserved as ``__cause__``, not a generic
        ProviderError."""
        from pydantic import BaseModel
        from pydantic import ValidationError as PydanticValidationError

        class Out(BaseModel):
            answer: str
            count: int

        umb = UnexpectedModelBehavior("Exceeded maximum output retries (2)")
        schema_error: PydanticValidationError | None = None
        try:
            Out.model_validate({"answer": "x"})
        except PydanticValidationError as e:
            schema_error = e
            umb.__cause__ = e
        assert schema_error is not None

        config = RetryConfig(max_attempts=2, base_delay=0.0, jitter=0.0)
        factory = _make_factory([umb, umb])

        with (
            patch("conductor.providers._pydantic_ai.retry.asyncio.sleep"),
            pytest.raises(ValidationError) as exc_info,
        ):
            await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=None,
                agent_name="retryer",
            )

        message = str(exc_info.value)
        assert "count" in message
        assert "Field required" in message
        # The original pydantic ValidationError must be preserved as the cause
        # so callers can introspect field-level errors (issue #343 contract).
        assert exc_info.value.__cause__ is schema_error

    @pytest.mark.asyncio
    async def test_model_http_error_429_retried_and_succeeds(self) -> None:
        """Regression test for issue #454, reproduced end to end: a
        ModelHTTPError(429) — pydantic-ai's translated form of an Anthropic
        RateLimitError — must be retried rather than raised immediately.

        ``retry_on=["provider_error"]`` mirrors the issue's YAML and exercises
        ``_classify_error``'s fall-through for a non-ProviderError exception.
        """
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        config = RetryConfig(
            max_attempts=3,
            base_delay=0.0,
            jitter=0.0,
            retry_on=["provider_error"],
        )
        factory = _make_factory([ModelHTTPError(status_code=429, model_name="claude-x"), "ok"])

        with patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep:
            result = await execute_with_retry(
                factory,
                retry_config=config,
                event_callback=callback,
                agent_name="retryer",
            )

        assert result == "ok"
        assert len(events) == 1
        assert events[0][0] == "agent_retry"
        assert events[0][1]["error_type"] == "ModelHTTPError"
        mock_sleep.assert_called_once_with(0.0)

    @pytest.mark.asyncio
    async def test_model_http_error_400_raises_immediately(self) -> None:
        """A ModelHTTPError(400) must raise a non-retryable ProviderError
        after exactly one attempt (no retry loop entered)."""
        config = RetryConfig(max_attempts=3, base_delay=0.0, jitter=0.0)
        factory = _make_factory([ModelHTTPError(status_code=400, model_name="claude-x")])
        call_count = 0

        async def counting_factory() -> Any:
            nonlocal call_count
            call_count += 1
            return await factory()

        with (
            patch("conductor.providers._pydantic_ai.retry.asyncio.sleep") as mock_sleep,
            pytest.raises(ProviderError) as exc_info,
        ):
            await execute_with_retry(
                counting_factory,
                retry_config=config,
                event_callback=None,
                agent_name="retryer",
            )

        assert exc_info.value.is_retryable is False
        assert exc_info.value.status_code == 400
        assert call_count == 1
        mock_sleep.assert_not_called()


class TestPydanticAIRetriesSplit:
    """Tests that Pydantic AI's retry budgets are split correctly."""

    def test_pydantic_ai_tool_retries_zero_output_retries_enabled(self) -> None:
        """Pydantic AI tool retries must be disabled so Conductor controls retries,
        but output retries must be enabled for structured-output recovery."""
        agent_def = AgentDef(name="single-shot")

        pydantic_agent = build_agent(agent_def, system_prompt="", rendered_prompt="")

        assert pydantic_agent._max_tool_retries == 0
        assert pydantic_agent._max_output_retries == 2
