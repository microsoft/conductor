"""Token-usage mapping from Pydantic AI to Conductor's ``AgentOutput`` contract.

Pydantic AI's ``RunUsage`` already aggregates per-request usage, including cache
reads/writes. This module exposes a thin mapper so the provider layer can build a
normalized :class:`~conductor.providers.base.AgentOutput` without duplicating
cost logic: cost resolution lives in ``conductor.engine.pricing`` and is applied
by :class:`~conductor.engine.usage.UsageTracker` when the workflow records the
execution.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.usage import RunUsage

from conductor.providers.base import AgentOutput


def last_request_input_tokens(messages: Sequence[ModelMessage] | None) -> int | None:
    """Return the prompt-token count of the most recent model response.

    Pydantic AI's ``RunUsage`` aggregates every request in the run, so it
    cannot answer "how big was the last call" — the question the
    context-window bar needs (issue #412). This walks ``messages`` in
    reverse and returns the first ``ModelResponse``'s per-request
    ``input_tokens``.

    Pydantic AI normalizes ``RequestUsage.input_tokens`` to *include* cache
    reads/writes (see ``UsageBase`` in ``pydantic_ai.usage``), so the value
    is directly comparable to a provider's prompt-token cap.

    Args:
        messages: The full message history for the run, or ``None``.

    Returns:
        The last response's input-token count, or ``None`` when there is no
        message history, no ``ModelResponse`` in it, or its usage reports no
        input tokens.
    """
    if not messages:
        return None
    for message in reversed(messages):
        if isinstance(message, ModelResponse):
            return message.usage.input_tokens or None
    return None


def run_usage_to_agent_output_fields(
    usage: RunUsage | None,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Map a Pydantic AI ``RunUsage`` to ``AgentOutput`` token/model fields.

    The returned dictionary contains ``tokens_used``, ``input_tokens``,
    ``output_tokens``, ``cache_read_tokens``, ``cache_write_tokens`` and
    ``model``. It is intended to be merged into an ``AgentOutput`` constructor,
    leaving ``content`` and ``raw_response`` to the caller.

    Cache fields are read from the first-class ``RunUsage`` attributes when they
    are non-zero. If those attributes are zero, we fall back to the Anthropic
    detail keys ``cache_read_input_tokens`` and ``cache_creation_input_tokens``
    that Pydantic AI preserves in ``RunUsage.details``. This mirrors the current
    ``ClaudeProvider`` mapping while staying provider-agnostic.

    Args:
        usage: Pydantic AI usage for the run, or ``None`` if unavailable.
        model: Actual model name to record on the output.

    Returns:
        Keyword-argument dictionary for the token/model fields of ``AgentOutput``.
    """
    if usage is None or not _usage_has_values(usage):
        return {
            "model": model,
            "tokens_used": None,
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
        }

    cache_read_tokens = _resolve_cache_read_tokens(usage)
    cache_write_tokens = _resolve_cache_write_tokens(usage)

    return {
        "model": model,
        "tokens_used": usage.total_tokens if usage.total_tokens else None,
        "input_tokens": usage.input_tokens if usage.input_tokens else None,
        "output_tokens": usage.output_tokens if usage.output_tokens else None,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
    }


def build_agent_output(
    content: dict[str, Any],
    raw_response: Any,
    *,
    usage: RunUsage | None = None,
    model: str | None = None,
    last_call_input_tokens: int | None = None,
) -> AgentOutput:
    """Build a normalized ``AgentOutput`` from Pydantic AI result pieces.

    This helper combines the parsed ``content``/``raw_response`` supplied by the
    provider with the token breakdown extracted from ``usage``. Cost is left to
    the engine's usage tracker, which uses ``conductor.engine.pricing``.

    Args:
        content: Parsed structured output matching the agent's output schema.
        raw_response: Provider-specific raw response for debugging/logging.
        usage: Pydantic AI usage for the run, or ``None`` if unavailable.
        model: Actual model name used (may differ from the requested alias).
        last_call_input_tokens: Prompt tokens of the most recent single API
            call, for the context-window bar (issue #412). See
            :func:`last_request_input_tokens`.

    Returns:
        A fully populated ``AgentOutput`` ready for the Conductor engine.
    """
    fields = run_usage_to_agent_output_fields(usage, model=model)
    return AgentOutput(
        content=content,
        raw_response=raw_response,
        model=fields["model"],
        tokens_used=fields["tokens_used"],
        input_tokens=fields["input_tokens"],
        output_tokens=fields["output_tokens"],
        cache_read_tokens=fields["cache_read_tokens"],
        cache_write_tokens=fields["cache_write_tokens"],
        last_call_input_tokens=last_call_input_tokens,
    )


def _usage_has_values(usage: RunUsage) -> bool:
    """Return whether ``usage`` reports any non-zero token counts."""
    return usage.has_values()


def _resolve_cache_read_tokens(usage: RunUsage) -> int | None:
    """Resolve cache-read tokens from first-class fields or Anthropic details."""
    if usage.cache_read_tokens:
        return usage.cache_read_tokens

    return _first_non_zero_int(
        usage.details.get("cache_read_input_tokens"),
        usage.details.get("cache_read_tokens"),
    )


def _resolve_cache_write_tokens(usage: RunUsage) -> int | None:
    """Resolve cache-write tokens from first-class fields or Anthropic details."""
    if usage.cache_write_tokens:
        return usage.cache_write_tokens

    return _first_non_zero_int(
        usage.details.get("cache_creation_input_tokens"),
        usage.details.get("cache_write_tokens"),
    )


def _first_non_zero_int(*values: Any) -> int | None:
    """Return the first non-zero integer value, or ``None`` if none exists."""
    for value in values:
        if isinstance(value, int) and value:
            return value
    return None
