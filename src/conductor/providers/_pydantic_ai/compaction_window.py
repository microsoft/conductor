"""Context-window and output-limit resolution for compaction.

This module owns the resolution cascades, trigger formula, and escalation-ceiling
target formula used by the pydantic-ai providers' tiered compaction.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from conductor.config.schema import ToolOutputConfig

from conductor.providers.base import AgentProvider

logger = logging.getLogger(__name__)

#: Unified default ``max_tokens`` applied by the Claude and OpenAI providers
#: when the user does not configure an explicit value. Also used as the
#: compaction output-limit fallback when no other source is available.
DEFAULT_MAX_TOKENS: int = 16_384

#: Escalation ceiling as a fraction of the resolved context window.
TARGET_FRACTION = 0.55

#: Conservative context-window fallback when no authoritative source is available.
FALLBACK_CONTEXT_WINDOW = 128_000

#: Environment variable override for the compaction context window.
ENV_CONTEXT_WINDOW = "CONDUCTOR_COMPACTION_CONTEXT_WINDOW"

#: Characters-per-token estimate for tool result payloads. This is an
#: approximation; actual token counts depend on the model's tokenizer.
TOOL_RESULT_CHARS_PER_TOKEN = 4

#: Number of worst-case tool results the reserve should hold headroom for.
#: pydantic-ai may run more than two parallel tool calls per segment, so this
#: is a sizing heuristic, not a hard bound.
TOOL_RESULT_COUNT_HEURISTIC = 2

#: Extra token slack for the compaction estimator, tool schemas, and other
#: non-result overhead. This is a heuristic, not a guarantee.
ESTIMATOR_SLOP = 15_000

#: One-shot warnings per process.
_invalid_env_warned = False
_custom_base_url_warned = False
_tool_output_disabled_warned = False
_trigger_degenerate_warned = False


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


def _warn_tool_output_disabled() -> None:
    """Log a one-time warning that tool-output limits are disabled."""
    global _tool_output_disabled_warned
    if _tool_output_disabled_warned:
        return
    _tool_output_disabled_warned = True
    logger.warning(
        "runtime.tool_output.enabled is false; tool result sizes are unbounded. "
        "The compaction reserve is using a heuristic buffer only."
    )


def _warn_trigger_degenerate() -> None:
    """Log a one-time warning that the compaction trigger has degenerated."""
    global _trigger_degenerate_warned
    if _trigger_degenerate_warned:
        return
    _trigger_degenerate_warned = True
    logger.warning(
        "The compaction trigger has degenerated to 1 token (output_limit + tool_buffer "
        "reaches the context window). Hysteresis is lost. Lower runtime.max_tokens or "
        "tool_output.max_chars, or raise CONDUCTOR_COMPACTION_CONTEXT_WINDOW."
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

    source: Literal["settings", "provider-cap", "default"]
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
    effective_max_tokens: int | None,
    user_configured: bool,
) -> OutputLimitResolution:
    """Resolve the compaction output-token limit through the priority cascade.

    Priority order, highest first:

    1. The effective ``max_tokens`` the provider will actually send to the API.
       Source is ``"settings"`` when the user configured ``runtime.max_tokens``
       (``user_configured=True``), otherwise ``"default"`` (the unified 16384
       default or a value from Claude's thinking coercion).
    2. The provider-reported per-model output cap from
       ``provider.get_max_output_tokens(model)`` when it is smaller than the base
       value. Source becomes ``"provider-cap"``.

    There is no environment override for the output limit by design.

    Args:
        provider: Provider instance to query for metadata.
        model: Model identifier as sent to the SDK.
        effective_max_tokens: The output-token cap that will actually be sent
            to the API, if known.
        user_configured: Whether ``effective_max_tokens`` came from an explicit
            user configuration (``runtime.max_tokens``) rather than a default.

    Returns:
        An :class:`OutputLimitResolution` with the resolved limit and its source.
        This function never raises.
    """
    if effective_max_tokens is not None:
        base = int(effective_max_tokens)
        source: Literal["settings", "provider-cap", "default"] = (
            "settings" if user_configured else "default"
        )
    else:
        base = DEFAULT_MAX_TOKENS
        source = "default"

    try:
        cap = await provider.get_max_output_tokens(model)
    except Exception as exc:  # noqa: BLE001 - metadata is best-effort
        logger.debug("get_max_output_tokens(%r) raised: %s", model, exc)
        cap = None

    if cap is not None and 0 < cap < base:
        base = int(cap)
        source = "provider-cap"

    return OutputLimitResolution(tokens=base, source=source)


def tool_buffer_tokens(tool_output: ToolOutputConfig | None) -> int:
    """Return the token buffer reserved for tool-result payloads.

    Formula::

        TOOL_RESULT_COUNT_HEURISTIC * ceil(max_chars / TOOL_RESULT_CHARS_PER_TOKEN) + ESTIMATOR_SLOP

    The default ``max_chars=50_000`` yields exactly 40_000 tokens, preserving
    the previous hard-coded buffer magnitude.

    When ``tool_output.enabled`` is ``False``, tool result sizes are unbounded,
    so the same default-magnitude heuristic buffer is returned and a one-time
    warning is logged.

    Args:
        tool_output: Runtime tool-output limit configuration.

    Returns:
        Token buffer to reserve for tool results.
    """
    default_max_chars = 50_000
    if tool_output is None or tool_output.enabled:
        max_chars = default_max_chars if tool_output is None else tool_output.max_chars
        return (
            TOOL_RESULT_COUNT_HEURISTIC * math.ceil(max_chars / TOOL_RESULT_CHARS_PER_TOKEN)
            + ESTIMATOR_SLOP
        )
    _warn_tool_output_disabled()
    return (
        TOOL_RESULT_COUNT_HEURISTIC * math.ceil(default_max_chars / TOOL_RESULT_CHARS_PER_TOKEN)
        + ESTIMATOR_SLOP
    )


def trigger_tokens(window: int, output_limit: int, tool_buffer: int) -> int:
    """Return the reserve-based compaction trigger.

    Formula::

        max(1, window - output_limit - tool_buffer)

    The trigger reserves enough headroom for the model's own maximum answer
    plus a buffer for the largest likely tool results, preventing the provider
    from rejecting the request for exceeding its context window.

    Args:
        window: Resolved context-window size in tokens.
        output_limit: Resolved output-token cap in tokens.
        tool_buffer: Tool-result reserve in tokens.

    Returns:
        Token threshold at which compaction should fire.
    """
    if output_limit + tool_buffer >= window - 1:
        _warn_trigger_degenerate()
    return max(1, window - output_limit - tool_buffer)


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
