"""Context-window and output-limit resolution for compaction.

This module owns the resolution cascades, trigger formula, and escalation-ceiling
target formula used by the pydantic-ai providers' tiered compaction.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

from conductor.providers.base import AgentProvider

logger = logging.getLogger(__name__)

#: Reserve the trigger must hold beyond the output limit (~2 worst-case tool
#: results at the default ``tool_output.max_chars=50000`` plus estimator slop).
TRIGGER_BUFFER = 40_000

#: Default output-token cap used when neither provider metadata nor the registry
#: knows the model's real output limit. Conservatively the largest common cap.
DEFAULT_OUTPUT_LIMIT = 64_000

#: Escalation ceiling as a fraction of the resolved context window.
TARGET_FRACTION = 0.55

#: Conservative context-window fallback when no authoritative source is available.
FALLBACK_CONTEXT_WINDOW = 128_000

#: Environment variable override for the compaction context window.
ENV_CONTEXT_WINDOW = "CONDUCTOR_COMPACTION_CONTEXT_WINDOW"

#: One-shot warnings per process.
_invalid_env_warned = False
_custom_base_url_warned = False


def _warn_invalid_env(value: str) -> None:
    """Log a one-time warning about an invalid env override."""
    global _invalid_env_warned
    if _invalid_env_warned:
        return
    _invalid_env_warned = True
    logger.warning(
        "Ignoring invalid %s value %r; expected a positive integer.",
        ENV_CONTEXT_WINDOW,
        value,
    )


def _warn_custom_base_url() -> None:
    """Log a one-time warning that compaction uses the conservative fallback."""
    global _custom_base_url_warned
    if _custom_base_url_warned:
        return
    _custom_base_url_warned = True
    logger.warning(
        "A custom base URL is configured; model metadata from the public registry "
        "will not be used for compaction. Falling back to a conservative "
        "%d-token context window.",
        FALLBACK_CONTEXT_WINDOW,
    )


def _warn_registry_lookup_failed(model: str, exc: Exception) -> None:
    """Log a one-time debug message when the registry lookup fails."""
    logger.debug("Failed to resolve context window from registry for %r: %s", model, exc)


@dataclass(frozen=True)
class WindowResolution:
    """Resolved context window for compaction."""

    tokens: int
    """Resolved context-window size in tokens."""

    source: Literal["env", "provider", "registry", "fallback"]
    """Source that supplied the value."""


@dataclass(frozen=True)
class OutputLimitResolution:
    """Resolved output-token limit for compaction."""

    tokens: int
    """Resolved output-token cap in tokens."""

    source: Literal["provider", "registry", "default"]
    """Source that supplied the value."""


def _resolve_from_env() -> int | None:
    """Parse ``CONDUCTOR_COMPACTION_CONTEXT_WINDOW`` as a positive int.

    Returns ``None`` when the variable is unset, empty, or not a positive integer.
    """
    raw = os.environ.get(ENV_CONTEXT_WINDOW)
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        _warn_invalid_env(raw)
        return None
    if value <= 0:
        _warn_invalid_env(raw)
        return None
    return value


def _resolve_window_from_registry(model: str) -> int | None:
    """Look up ``model`` in the genai-prices registry.

    Returns the model's ``context_window`` when found, otherwise ``None``.
    Any lookup failure is logged at debug level and returns ``None``.
    """
    try:
        from genai_prices.data_snapshot import get_snapshot
    except Exception as exc:  # noqa: BLE001 - registry is best-effort
        _warn_registry_lookup_failed(model, exc)
        return None

    try:
        snap = get_snapshot()
        _provider, info = snap.find_provider_model(model, None, None, None)
    except Exception as exc:  # noqa: BLE001 - registry is best-effort
        _warn_registry_lookup_failed(model, exc)
        return None

    window = getattr(info, "context_window", None)
    if window is None or window <= 0:
        return None
    return int(window)


def _resolve_output_limit_from_registry(model: str) -> int | None:
    """Attempt to look up the model's max output tokens in the registry.

    The installed genai-prices version does not expose ``max_output_tokens`` on
    its ``ModelInfo`` dataclass, so this always returns ``None`` today. It is kept
    as a well-defined seam so a future registry version that includes the field
    can be adopted without changing the cascade logic.
    """
    del model
    return None


async def resolve_compaction_window(
    *,
    provider: AgentProvider,
    model: str,
    has_custom_base_url: bool,
) -> WindowResolution:
    """Resolve the compaction context window through the priority cascade.

    Priority order, highest first:

    1. ``CONDUCTOR_COMPACTION_CONTEXT_WINDOW`` environment variable.
    2. ``provider.get_max_prompt_tokens(model)`` (authoritative provider metadata).
    3. ``genai-prices`` registry, but only for first-party endpoints
       (``has_custom_base_url`` is ``False``).
    4. Conservative ``FALLBACK_CONTEXT_WINDOW`` fallback.

    Args:
        provider: Provider instance to query for metadata.
        model: Model identifier as sent to the SDK.
        has_custom_base_url: Whether the provider is pointed at a custom API
            proxy. When ``True``, registry lookups are skipped because a proxy
            model id may describe a different deployment.

    Returns:
        A :class:`WindowResolution` with the resolved window and its source.
        This function never raises.
    """
    env_value = _resolve_from_env()
    if env_value is not None:
        return WindowResolution(tokens=env_value, source="env")

    try:
        provider_value = await provider.get_max_prompt_tokens(model)
    except Exception as exc:  # noqa: BLE001 - metadata is best-effort
        logger.debug("get_max_prompt_tokens(%r) raised: %s", model, exc)
        provider_value = None
    if provider_value is not None and provider_value > 0:
        return WindowResolution(tokens=int(provider_value), source="provider")

    if not has_custom_base_url:
        registry_value = _resolve_window_from_registry(model)
        if registry_value is not None:
            return WindowResolution(tokens=registry_value, source="registry")
    else:
        _warn_custom_base_url()

    return WindowResolution(tokens=FALLBACK_CONTEXT_WINDOW, source="fallback")


async def resolve_output_limit(
    *,
    provider: AgentProvider,
    model: str,
    has_custom_base_url: bool,
) -> OutputLimitResolution:
    """Resolve the compaction output-token limit through the priority cascade.

    Priority order, highest first:

    1. ``provider.get_max_output_tokens(model)`` (authoritative provider metadata).
    2. ``genai-prices`` registry, but only for first-party endpoints
       (``has_custom_base_url`` is ``False``).
    3. ``DEFAULT_OUTPUT_LIMIT``.

    There is no environment override for the output limit by design.

    Args:
        provider: Provider instance to query for metadata.
        model: Model identifier as sent to the SDK.
        has_custom_base_url: Whether the provider is pointed at a custom API
            proxy. When ``True``, registry lookups are skipped.

    Returns:
        An :class:`OutputLimitResolution` with the resolved limit and its source.
        This function never raises.
    """
    try:
        provider_value = await provider.get_max_output_tokens(model)
    except Exception as exc:  # noqa: BLE001 - metadata is best-effort
        logger.debug("get_max_output_tokens(%r) raised: %s", model, exc)
        provider_value = None
    if provider_value is not None and provider_value > 0:
        return OutputLimitResolution(tokens=int(provider_value), source="provider")

    if not has_custom_base_url:
        registry_value = _resolve_output_limit_from_registry(model)
        if registry_value is not None:
            return OutputLimitResolution(tokens=registry_value, source="registry")

    return OutputLimitResolution(tokens=DEFAULT_OUTPUT_LIMIT, source="default")


def trigger_tokens(window: int, output_limit: int) -> int:
    """Return the reserve-based compaction trigger.

    Formula::

        max(1, window - max(output_limit, TRIGGER_BUFFER))

    The trigger reserves enough headroom for the model's own maximum answer
    plus a buffer for the largest likely tool results, preventing the provider
    from rejecting the request for exceeding its context window.

    Args:
        window: Resolved context-window size in tokens.
        output_limit: Resolved output-token cap in tokens.

    Returns:
        Token threshold at which compaction should fire.
    """
    return max(1, window - max(output_limit, TRIGGER_BUFFER))


def target_tokens(window: int, trigger: int) -> int:
    """Return the escalation-ceiling target for tiered compaction.

    Formula::

        max(1, min(int(window * TARGET_FRACTION), trigger - 1))

    This is the inner target passed to the harness. It is clamped strictly
    below the trigger so that, after a successful compaction, the history is
    far enough below the trigger to provide hysteresis against immediate
    re-compaction.

    Args:
        window: Resolved context-window size in tokens.
        trigger: Trigger threshold from :func:`trigger_tokens`.

    Returns:
        Escalation-ceiling target in tokens.
    """
    return max(1, min(int(window * TARGET_FRACTION), trigger - 1))
