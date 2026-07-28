"""Conductor-level retry wrapper for the Pydantic AI provider.

Mirrors the retry semantics of ``ClaudeProvider._execute_with_retry`` so that
Pydantic AI's tool retry budget is disabled (``retries={"tools": 0}`` in
``build_agent``) and all transient API/tool retries are handled by Conductor.
Structured-output recovery retries are left enabled in ``build_agent`` because
plain-text answers to a tool-output schema must be recovered in-session before
``execute_with_retry`` can see a result. If that internal budget is exhausted,
Conductor retries the whole call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable, Coroutine
from typing import Any

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

from conductor.config.schema import RetryPolicy
from conductor.exceptions import ProviderError, ValidationError

logger = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], None]

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
        max_delay=default.max_delay,
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

    Mirrors ``ClaudeProvider._is_retryable_error``.
    """
    if isinstance(exception, ProviderError):
        return exception.is_retryable

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
    if not is_api_status:
        is_api_status = error_type_name in ("APIStatusError", "MockAPIStatusError")

    if is_api_status and hasattr(exception, "status_code"):
        status_code = getattr(exception, "status_code", None)
        if status_code is not None:
            code = int(status_code)
            if 500 <= code < 600 or code == 429:
                return True

    return False


def _get_retry_after(exception: Exception) -> float | None:
    """Extract retry-after value from a rate limit exception.

    Mirrors ``ClaudeProvider._get_retry_after``.
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
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
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
        ValidationError: Re-raised without retry.
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
                delay = retry_after
                logger.warning(
                    "Rate limit hit (HTTP 429), respecting retry-after header: %ss",
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
        suggestion = (
            "Model repeatedly failed to produce valid structured output (output tool not called); "
            "retry later or simplify the output schema"
        )
    else:
        suggestion = f"Check API connectivity and rate limits. Last error: {last_error}"

    raise ProviderError(
        f"Pydantic AI call failed after {retry_config.max_attempts} attempts: {last_error}",
        suggestion=suggestion,
        is_retryable=False,
    )
