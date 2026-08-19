"""Conductor-level retry wrapper for the Pydantic AI provider.

Mirrors the retry semantics of ``ClaudeProvider._execute_with_retry`` so that
Pydantic AI's tool retry budget is disabled (``retries={"tools": 0}`` in
``build_agent``) and all transient API/tool retries are handled by Conductor.
Structured-output recovery retries are left enabled in ``build_agent`` because
plain-text answers to a tool-output schema must be recovered in-session before
``execute_with_retry`` can see a result. If that internal budget is exhausted,
Conductor retries the whole call.

pydantic-ai translates the Anthropic SDK's own exceptions before Conductor
ever sees them — a private helper (``_map_api_errors`` in pydantic-ai 2.x;
written inline in ``AnthropicModel`` at the 1.44.0 floor pinned in
pyproject.toml, with identical resulting behavior) wraps the SDK call: an
HTTP error response (``APIStatusError``, including ``RateLimitError``)
becomes ``ModelHTTPError``, and a transport failure (``APIConnectionError``,
including ``APITimeoutError``) becomes a bare ``ModelAPIError``. Those are
the exception types actually observed on this path, not the SDK's own
classes, so ``_is_retryable_error`` and ``_get_retry_after`` classify those
translated types directly (issue #454). Only the public ``ModelHTTPError``/
``ModelAPIError`` types are relied on at runtime, so a change to the private
translator degrades this comment, not the code. The translation also drops
the original response headers, so a server's ``retry-after`` value is only
recoverable through ``__cause__``, which the translator sets to the
untranslated SDK exception via ``from e``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
from collections.abc import Callable, Coroutine
from typing import Any, cast

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

try:
    import openai
except ImportError:
    openai = None  # type: ignore[assignment]

from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from conductor.config.schema import RetryPolicy
from conductor.exceptions import ProviderError, ValidationError

logger = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], None]


class RetryConfig:
    """Configuration for retry behavior.

    Mirrors ``ClaudeProvider.RetryConfig`` defaults so the new provider behaves
    identically without importing from the legacy module.
    """

    max_attempts: int
    base_delay: float
    max_delay: float
    jitter: float
    backoff: str
    retry_on: list[str] | None
    max_parse_recovery_attempts: int

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        jitter: float = 0.25,
        backoff: str = "exponential",
        retry_on: list[str] | None = None,
        max_parse_recovery_attempts: int = 2,
    ) -> None:
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter = jitter
        self.backoff = backoff
        self.retry_on = retry_on
        self.max_parse_recovery_attempts = max_parse_recovery_attempts


def _resolve_retry_config(
    agent: Any,
    provider_default: RetryConfig | None = None,
) -> RetryConfig:
    """Build a ``RetryConfig`` from an agent's ``RetryPolicy`` or defaults.

    Mirrors ``ClaudeProvider._resolve_retry_config``.
    """
    default = provider_default or RetryConfig()
    retry = getattr(agent, "retry", None)
    if not isinstance(retry, RetryPolicy):
        return default

    return RetryConfig(
        max_attempts=retry.max_attempts,
        base_delay=retry.delay_seconds,
        # A user who writes delay_seconds: 60 must not have it silently clamped
        # to the 30s provider default. The cap becomes their stated value, so
        # exponential growth is clamped to it rather than below it. Issue #454.
        max_delay=max(default.max_delay, retry.delay_seconds),
        jitter=default.jitter,
        backoff=retry.backoff,
        retry_on=list(retry.retry_on),
        max_parse_recovery_attempts=(
            retry.max_parse_recovery_attempts
            if retry.max_parse_recovery_attempts is not None
            else default.max_parse_recovery_attempts
        ),
    )


def _classify_error(error: Exception) -> str:
    """Classify an exception into a retry category.

    Mirrors ``ClaudeProvider._classify_error``.
    """
    from conductor.exceptions import TimeoutError as ConductorTimeoutError

    if isinstance(error, (ConductorTimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(error, ProviderError):
        if error.status_code == 408:
            return "timeout"
        if "timeout" in str(error).lower():
            return "timeout"
    return "provider_error"


def _is_retryable_error(exception: Exception) -> bool:
    """Determine if an error should trigger a retry.

    Extended to classify the ``ModelHTTPError``/``ModelAPIError`` types
    pydantic-ai actually raises on this path (see the module docstring;
    issue #454). The SDK-class-name and ``anthropic.APIStatusError``
    fallback below is unreachable in production but kept intentionally —
    see the comment at its definition.
    """
    if isinstance(exception, ProviderError):
        return exception.is_retryable

    # pydantic-ai translates the Anthropic SDK's exceptions before Conductor
    # sees them (see the module docstring), so on this path neither the SDK
    # class names nor the anthropic.APIStatusError isinstance check below ever
    # match in production; that tail is kept as a fallback for non-pydantic-ai
    # callers, for the tests that exercise it directly via the Mock* classes
    # below, and as cheap insurance if pydantic-ai ever narrows its
    # translation. Issue #454.
    if isinstance(exception, ModelHTTPError):
        code = exception.status_code
        return code == 429 or code == 408 or 500 <= code < 600
    # A bare ModelAPIError is what APIConnectionError/APITimeoutError become —
    # a transport failure, retryable for the same reason the SDK names below
    # are. Checked with `type(...) is ...` rather than isinstance: ModelHTTPError
    # is ModelAPIError's only subclass today (see the canary test asserting
    # that), and it is already handled above, so this stays scoped to the
    # bare base class rather than silently absorbing a future subclass that
    # may not be a transient condition.
    if type(exception) is ModelAPIError:
        return True

    error_type_name = type(exception).__name__

    retryable_names = {
        "APIConnectionError",
        "RateLimitError",
        "APITimeoutError",
        "MockAPIConnectionError",
        "MockRateLimitError",
        "MockAPITimeoutError",
        "UnexpectedModelBehavior",
    }
    if error_type_name in retryable_names:
        return True

    is_api_status = False
    if anthropic is not None:
        try:
            is_api_status = isinstance(exception, anthropic.APIStatusError)
        except TypeError:
            is_api_status = error_type_name in ("APIStatusError", "MockAPIStatusError")

    if not is_api_status and openai is not None:
        with contextlib.suppress(TypeError, AttributeError):
            is_api_status = isinstance(exception, openai.APIStatusError)

    if not is_api_status:
        is_api_status = error_type_name in ("APIStatusError", "MockAPIStatusError")

    if is_api_status and hasattr(exception, "status_code"):
        status_code = getattr(exception, "status_code", None)
        if status_code is not None:
            code = int(status_code)
            if 500 <= code < 600 or code == 429:
                return True

    return False


def _retry_after_from_headers(exception: BaseException) -> float | None:
    """Extract a retry-after value from a rate-limit exception's response headers.

    Split out of ``_get_retry_after`` so it can run against both the raised
    exception and each ``__cause__`` in the chain, since pydantic-ai's
    translated ``ModelHTTPError``/``ModelAPIError`` carry no response headers
    of their own (issue #454).
    """
    error_type_name = type(exception).__name__
    is_rate_limit = False
    if anthropic is not None:
        try:
            is_rate_limit = isinstance(exception, anthropic.RateLimitError)
        except TypeError:
            is_rate_limit = error_type_name in ("RateLimitError", "MockRateLimitError")
    if not is_rate_limit:
        is_rate_limit = error_type_name in ("RateLimitError", "MockRateLimitError")

    if is_rate_limit and hasattr(exception, "response") and exception.response:
        headers = getattr(exception.response, "headers", {})
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after is not None:
            try:
                parsed = float(retry_after)
            except (TypeError, ValueError):
                return None
            if math.isfinite(parsed) and parsed > 0:
                return parsed
    return None


def _retry_after_from_body(exception: ModelHTTPError) -> float | None:
    """Extract a retry-after value from a ``ModelHTTPError``'s response body.

    ``ModelHTTPError`` carries the Anthropic API's parsed JSON ``body`` but
    not the response headers, so a server that reports retry timing in the
    body (rather than, or in addition to, a ``retry-after`` header) is only
    recoverable here. No prose parsing of message strings — that is
    unreliable and would misfire.
    """
    body = exception.body
    if not isinstance(body, dict):
        return None
    body_dict = cast("dict[str, Any]", body)

    candidates: list[Any] = [body_dict.get("retry_after"), body_dict.get("retry-after")]
    error = body_dict.get("error")
    if isinstance(error, dict):
        error_dict = cast("dict[str, Any]", error)
        candidates.extend([error_dict.get("retry_after"), error_dict.get("retry-after")])

    for value in candidates:
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(parsed) or parsed <= 0:
            logger.debug(
                "Ignoring unparseable retry_after %r in %s response body (status %s)",
                value,
                type(exception).__name__,
                getattr(exception, "status_code", None),
            )
            continue
        return parsed
    return None


def _get_retry_after(exception: Exception) -> float | None:
    """Extract retry-after value from a rate limit exception.

    Extended for pydantic-ai's translated exceptions (issue #454):
    ``ModelHTTPError``/``ModelAPIError`` carry no response headers, since the
    translator drops them. For an HTTP error the ``__cause__`` is the
    original, untranslated ``APIStatusError`` with ``.response.headers``
    intact, so it is walked and preferred over the body (the header is the
    value the server actually sent over HTTP; the body key is an unconfirmed
    convention Anthropic does not document). A transport-failure cause
    (``APIConnectionError``) has no ``.response`` at all and yields nothing
    here.
    """
    retry_after = _retry_after_from_headers(exception)
    if retry_after is not None:
        return retry_after

    seen: set[int] = {id(exception)}
    cause = exception.__cause__
    depth = 0
    while cause is not None and depth < 5 and id(cause) not in seen:
        retry_after = _retry_after_from_headers(cause)
        if retry_after is not None:
            return retry_after
        seen.add(id(cause))
        cause = cause.__cause__
        depth += 1

    if isinstance(exception, ModelHTTPError):
        retry_after = _retry_after_from_body(exception)
        if retry_after is not None:
            return retry_after

    return None


def _extract_status_code(exception: Exception) -> int | None:
    """Extract HTTP status code from exception if available.

    Mirrors ``ClaudeProvider._extract_status_code``.
    """
    if anthropic is not None:
        try:
            if isinstance(exception, anthropic.APIStatusError):
                return getattr(exception, "status_code", None)
        except TypeError:
            pass

    if hasattr(exception, "status_code"):
        status_code = getattr(exception, "status_code", None)
        if status_code is not None:
            return int(status_code)
    return None


def _describe_root_cause(error: BaseException, *, max_depth: int = 5) -> str | None:
    """Describe the deepest exception in a ``__cause__`` chain, if any.

    Mirrors the bounded, cycle-guarded walk in ``_get_retry_after`` so a
    translated ``ModelAPIError`` (issue #454) — whose own message is the
    Anthropic SDK's hardcoded "Connection error." — can surface the real
    transport failure (DNS, TLS, proxy, ...) that pydantic-ai chained onto it
    via ``from e``.
    """
    seen: set[int] = {id(error)}
    cause = error.__cause__
    depth = 0
    deepest: BaseException | None = None
    while cause is not None and depth < max_depth and id(cause) not in seen:
        deepest = cause
        seen.add(id(cause))
        cause = cause.__cause__
        depth += 1
    if deepest is None:
        return None
    return f"{type(deepest).__name__}: {deepest}"


def _calculate_delay(attempt: int, config: RetryConfig) -> float:
    """Calculate delay with backoff and jitter.

    Mirrors ``ClaudeProvider._calculate_delay``.
    """
    if config.backoff == "fixed":
        delay = config.base_delay
    else:
        delay = config.base_delay * (2 ** (attempt - 1))

    delay = min(delay, config.max_delay)

    if config.jitter > 0:
        jitter_amount = delay * config.jitter * random.random()
        delay += jitter_amount

    return delay


async def execute_with_retry[T](
    coro_factory: Callable[[], Coroutine[Any, Any, T]],
    *,
    retry_config: RetryConfig,
    event_callback: EventCallback | None,
    agent_name: str,
) -> T:
    """Execute a coroutine with Conductor-level retry semantics.

    Wraps an arbitrary coroutine factory (e.g., ``lambda:
    run_with_interrupt(...)``) so each retry starts a fresh operation. Retries
    are governed by ``retry_config`` and emit the same ``agent_retry`` event
    payload as ``ClaudeProvider``.

    Args:
        coro_factory: Callable returning the coroutine to execute.
        retry_config: Resolved retry configuration.
        event_callback: Optional Conductor event callback.
        agent_name: Name of the agent for events and logging.

    Returns:
        The result of the coroutine factory.

    Raises:
        ValidationError: Re-raised without retry. Also raised on retry-budget
            exhaustion when pydantic-ai preserved the underlying
            ``pydantic.ValidationError`` as ``__cause__`` on
            ``UnexpectedModelBehavior`` (schema-shape failures name the field
            and expected type, per the issue #343 parity contract).
        ProviderError: On fatal or exhausted retry errors.
    """
    last_error: Exception | None = None

    for attempt in range(1, retry_config.max_attempts + 1):
        try:
            return await coro_factory()
        except ValidationError:
            raise
        except Exception as e:
            last_error = e

            if anthropic is not None:
                try:
                    if (
                        hasattr(anthropic, "BadRequestError")
                        and isinstance(e, anthropic.BadRequestError)
                        and "temperature" in str(e).lower()
                    ):
                        raise ValidationError(
                            f"Temperature validation failed: {e}",
                            suggestion=(
                                "Temperature must be between 0.0 and 1.0 (enforced by Claude SDK)"
                            ),
                        ) from e
                except TypeError:
                    pass

            is_retryable = _is_retryable_error(e)

            logger.debug(
                "Execution attempt %s failed: %s: %s (retryable=%s)",
                attempt,
                type(e).__name__,
                e,
                is_retryable,
            )

            if not is_retryable:
                status_code = _extract_status_code(e)
                if status_code is not None:
                    raise ProviderError(
                        f"Pydantic AI provider error: {e}",
                        suggestion="Check API key, model name, and request parameters",
                        status_code=status_code,
                        is_retryable=False,
                    ) from e
                raise ProviderError(
                    f"Pydantic AI call failed: {e}",
                    suggestion="Check API key, model name, and request parameters",
                    is_retryable=False,
                ) from e

            if retry_config.retry_on is not None:
                error_category = _classify_error(e)
                if error_category not in retry_config.retry_on:
                    raise

            if attempt >= retry_config.max_attempts:
                break

            retry_after = _get_retry_after(e)
            if retry_after is not None:
                delay = min(retry_after, retry_config.max_delay)
                logger.warning(
                    "Retry-after value reported for %s (HTTP %s): %ss (clamped to %ss)",
                    type(e).__name__,
                    _extract_status_code(e),
                    retry_after,
                    delay,
                )
            else:
                delay = _calculate_delay(attempt, retry_config)

            logger.warning(
                "[Retry %s/%s] Retrying after %.2fs due to %s: %s",
                attempt,
                retry_config.max_attempts,
                delay,
                type(e).__name__,
                e,
            )

            if event_callback is not None:
                with contextlib.suppress(Exception):
                    event_callback(
                        "agent_retry",
                        {
                            "agent_name": agent_name,
                            "attempt": attempt,
                            "max_attempts": retry_config.max_attempts,
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "delay": delay,
                        },
                    )

            await asyncio.sleep(delay)

    if last_error is not None and type(last_error).__name__ == "UnexpectedModelBehavior":
        # Issue #343 parity contract: once the retry budget is exhausted on a
        # structured-output failure, re-raise the original pydantic
        # ValidationError (it names the field and expected type). pydantic-ai
        # preserves it as ``__cause__`` on UnexpectedModelBehavior.
        from pydantic import ValidationError as PydanticValidationError

        cause = getattr(last_error, "__cause__", None)
        if isinstance(cause, PydanticValidationError):
            raise ValidationError(
                f"Structured output validation failed: {cause}",
                suggestion=(
                    "Fix the model's output to match the declared schema; "
                    "retrying will not resolve a shape mismatch."
                ),
            ) from cause
        suggestion = (
            "Model repeatedly failed to produce valid structured output (output tool not called); "
            "retry later or simplify the output schema"
        )
    elif last_error is not None and isinstance(last_error, ModelAPIError):
        # A bare ModelAPIError (issue #454) is what a transport failure
        # (APIConnectionError/APITimeoutError) becomes; its own message is
        # the Anthropic SDK's hardcoded "Connection error." with no detail.
        # Walk __cause__ (same bounded, cycle-guarded pattern as
        # _get_retry_after) to surface what actually failed (DNS, TLS,
        # proxy, ...) instead of leaving a misconfiguration undiagnosable.
        root_cause = _describe_root_cause(last_error)
        if root_cause is not None:
            suggestion = (
                "Check API connectivity, base_url, and proxy/TLS settings. "
                f"Underlying cause: {root_cause}"
            )
        else:
            suggestion = f"Check API connectivity and rate limits. Last error: {last_error}"
    else:
        suggestion = f"Check API connectivity and rate limits. Last error: {last_error}"

    raise ProviderError(
        f"Pydantic AI call failed after {retry_config.max_attempts} attempts: {last_error}",
        suggestion=suggestion,
        is_retryable=False,
    ) from last_error
