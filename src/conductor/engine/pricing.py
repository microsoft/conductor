"""Pricing tables and cost calculation for LLM models.

This module provides pricing information for various LLM models
and functions to calculate costs based on token usage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Track models that have already triggered a fuzzy-match warning so we only log
# once per process per unknown model name. See #137.
_FUZZY_MATCH_WARNED: set[str] = set()


def _warn_fuzzy_match(requested: str, matched_key: str, strategy: str) -> None:
    """Emit a one-time warning when ``get_pricing`` falls back to a non-exact match.

    Args:
        requested: The model name the caller asked for.
        matched_key: The key in ``DEFAULT_PRICING`` that was returned.
        strategy: How the match was made (e.g. ``"longest-prefix"``,
            ``"suffix-strip"``, ``"suffix-strip+longest-prefix"``).
    """
    if requested in _FUZZY_MATCH_WARNED:
        return
    _FUZZY_MATCH_WARNED.add(requested)
    logger.warning(
        "Pricing for model %r resolved via %s fallback to %r. "
        "Cost calculation may be inaccurate. "
        "Add %r to DEFAULT_PRICING or pass an override to silence this warning.",
        requested,
        strategy,
        matched_key,
        requested,
    )


@dataclass(frozen=True)
class ModelPricing:
    """Pricing per model.

    Attributes:
        input_per_mtok: Cost per million input tokens (USD).
        output_per_mtok: Cost per million output tokens (USD).
        cache_read_per_mtok: Cost per million cache read tokens (USD).
        cache_write_per_mtok: Cost per million cache write tokens (USD).
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0


# Default model table (pricing only).
# Context-window metadata is sourced from each provider's SDK at runtime via
# ``AgentProvider.get_max_prompt_tokens()`` — see ``providers/base.py``.
# Sources: OpenAI pricing page, Anthropic pricing page, provider docs.
DEFAULT_PRICING: dict[str, ModelPricing] = {
    # OpenAI / Copilot models
    "gpt-4-turbo": ModelPricing(input_per_mtok=10.00, output_per_mtok=30.00),
    "gpt-4o": ModelPricing(input_per_mtok=2.50, output_per_mtok=10.00),
    "gpt-4o-mini": ModelPricing(input_per_mtok=0.15, output_per_mtok=0.60),
    "gpt-4.1": ModelPricing(input_per_mtok=2.00, output_per_mtok=8.00),
    "gpt-4.1-mini": ModelPricing(input_per_mtok=0.15, output_per_mtok=0.60),
    "gpt-4": ModelPricing(input_per_mtok=30.00, output_per_mtok=60.00),
    "gpt-3.5-turbo": ModelPricing(input_per_mtok=0.50, output_per_mtok=1.50),
    # GPT-5.x Series (uniform standard rate; mini variants at the mini tier)
    "gpt-5.5": ModelPricing(input_per_mtok=2.00, output_per_mtok=8.00),
    "gpt-5.4": ModelPricing(input_per_mtok=2.00, output_per_mtok=8.00),
    "gpt-5.3-codex": ModelPricing(input_per_mtok=2.00, output_per_mtok=8.00),
    "gpt-5.2": ModelPricing(input_per_mtok=2.00, output_per_mtok=8.00),
    "gpt-5.1": ModelPricing(input_per_mtok=2.00, output_per_mtok=8.00),
    "gpt-5.4-mini": ModelPricing(input_per_mtok=0.15, output_per_mtok=0.60),
    "gpt-5-mini": ModelPricing(input_per_mtok=0.15, output_per_mtok=0.60),
    # O-series
    "o1": ModelPricing(input_per_mtok=15.00, output_per_mtok=60.00),
    "o1-mini": ModelPricing(input_per_mtok=3.00, output_per_mtok=12.00),
    "o1-preview": ModelPricing(input_per_mtok=15.00, output_per_mtok=60.00),
    "o3-mini": ModelPricing(input_per_mtok=1.10, output_per_mtok=4.40),
    # Claude 5 Series (newest)
    "claude-sonnet-5": ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    # Claude 4.5 Series
    "claude-opus-4-5": ModelPricing(
        input_per_mtok=5.00,
        output_per_mtok=25.00,
        cache_read_per_mtok=0.50,
        cache_write_per_mtok=6.25,
    ),
    "claude-sonnet-4-5": ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    "claude-haiku-4-5": ModelPricing(
        input_per_mtok=1.00,
        output_per_mtok=5.00,
        cache_read_per_mtok=0.10,
        cache_write_per_mtok=1.25,
    ),
    # Short aliases for Claude 4.5 Series (used in workflow files)
    "opus-4.5": ModelPricing(
        input_per_mtok=5.00,
        output_per_mtok=25.00,
        cache_read_per_mtok=0.50,
        cache_write_per_mtok=6.25,
    ),
    "sonnet-4.5": ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    "haiku-4.5": ModelPricing(
        input_per_mtok=1.00,
        output_per_mtok=5.00,
        cache_read_per_mtok=0.10,
        cache_write_per_mtok=1.25,
    ),
    # Claude 4.6 Series
    "claude-opus-4.6": ModelPricing(
        input_per_mtok=5.00,
        output_per_mtok=25.00,
        cache_read_per_mtok=0.50,
        cache_write_per_mtok=6.25,
    ),
    "claude-opus-4.6-1m": ModelPricing(
        input_per_mtok=5.00,
        output_per_mtok=25.00,
        cache_read_per_mtok=0.50,
        cache_write_per_mtok=6.25,
    ),
    "claude-sonnet-4.6": ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    # Claude 4.7 / 4.8 Series
    "claude-opus-4.8": ModelPricing(
        input_per_mtok=5.00,
        output_per_mtok=25.00,
        cache_read_per_mtok=0.50,
        cache_write_per_mtok=6.25,
    ),
    "claude-opus-4.7": ModelPricing(
        input_per_mtok=5.00,
        output_per_mtok=25.00,
        cache_read_per_mtok=0.50,
        cache_write_per_mtok=6.25,
    ),
    # Claude 4 Series
    "claude-opus-4": ModelPricing(
        input_per_mtok=15.00,
        output_per_mtok=75.00,
        cache_read_per_mtok=1.50,
        cache_write_per_mtok=18.75,
    ),
    "claude-sonnet-4": ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    "claude-haiku-4": ModelPricing(
        input_per_mtok=0.25,
        output_per_mtok=1.25,
        cache_read_per_mtok=0.03,
        cache_write_per_mtok=0.30,
    ),
    # Claude 3.x Series
    "claude-3-7-sonnet": ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    "claude-3.7-sonnet": ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    "claude-3-5-sonnet": ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    "claude-3.5-sonnet": ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    "claude-3-5-haiku": ModelPricing(
        input_per_mtok=0.80,
        output_per_mtok=4.00,
        cache_read_per_mtok=0.08,
        cache_write_per_mtok=1.00,
    ),
    "claude-3.5-haiku": ModelPricing(
        input_per_mtok=0.80,
        output_per_mtok=4.00,
        cache_read_per_mtok=0.08,
        cache_write_per_mtok=1.00,
    ),
    "claude-3-opus": ModelPricing(
        input_per_mtok=15.00,
        output_per_mtok=75.00,
        cache_read_per_mtok=1.50,
        cache_write_per_mtok=18.75,
    ),
    "claude-3-sonnet": ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    "claude-3-haiku": ModelPricing(
        input_per_mtok=0.25,
        output_per_mtok=1.25,
        cache_read_per_mtok=0.03,
        cache_write_per_mtok=0.30,
    ),
    # Gemini
    "gemini-3.1-pro-preview": ModelPricing(
        input_per_mtok=1.25,
        output_per_mtok=5.00,
    ),
    "gemini-3.5-flash": ModelPricing(
        input_per_mtok=0.30,
        output_per_mtok=2.50,
    ),
}


def get_pricing(
    model: str,
    overrides: dict[str, ModelPricing] | None = None,
    *,
    provider_pricing: dict[str, ModelPricing] | None = None,
) -> ModelPricing | None:
    """Get pricing for a model.

    Resolution order (see #265):

    **workflow ``cost.pricing`` override → provider hook → ``DEFAULT_PRICING``
    → ``None``.**

    ``overrides`` (workflow ``cost.pricing``) and ``provider_pricing`` (the
    :meth:`AgentProvider.get_model_pricing` hook, pre-resolved and cached by
    the :class:`~conductor.engine.usage.UsageTracker`) are both matched by
    exact model name and treated as authoritative — they never warn. Only the
    static ``DEFAULT_PRICING`` fallback supports fuzzy matching for versioned
    model names — the requested name must equal a known key, or extend it with
    a ``-`` delimiter (e.g. ``claude-sonnet-4-20250514`` matches
    ``claude-sonnet-4``). Names that share a textual prefix without the
    delimiter (e.g. ``claude-opus-4.7-high`` against ``claude-opus-4``) are
    *not* matched and will return ``None``, so callers don't silently inherit
    metadata from a sibling model family.

    Non-exact matches log a one-time warning per requested name (see #137).

    Args:
        model: The model name to look up.
        overrides: Optional user-provided pricing overrides (workflow
            ``cost.pricing``). Highest precedence.
        provider_pricing: Optional provider-supplied pricing, resolved via the
            ``get_model_pricing`` hook. Beats the static table so newly-released
            models are priced without a table update.

    Returns:
        ModelPricing if found, None otherwise.
    """
    # Check overrides first (treated as user intent — never warn).
    if overrides and model in overrides:
        return overrides[model]

    # Provider-supplied pricing (the get_model_pricing hook, see #265) beats
    # the static table so newly-released models are priced without a table
    # update. Authoritative for the exact model that was resolved — never warn.
    if provider_pricing and model in provider_pricing:
        return provider_pricing[model]

    # Exact match in the static table.
    if model in DEFAULT_PRICING:
        return DEFAULT_PRICING[model]

    # Versioned-name match: requested name must extend a known key with a
    # `-` delimiter. Sort keys longest-first so e.g. `o1-mini` matches before
    # `o1`. The delimiter check prevents cross-family bleed like
    # `claude-opus-4.7-high` matching `claude-opus-4` (#137).
    sorted_keys = sorted(DEFAULT_PRICING.keys(), key=lambda k: len(k), reverse=True)
    for known_model in sorted_keys:
        if model.startswith(known_model + "-"):
            _warn_fuzzy_match(model, known_model, "versioned-suffix")
            return DEFAULT_PRICING[known_model]

    return None


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    pricing: ModelPricing | None = None,
) -> float | None:
    """Calculate cost in USD for a model execution.

    Args:
        model: The model name used.
        input_tokens: Number of input tokens.
        output_tokens: Number of output tokens.
        cache_read_tokens: Number of tokens read from cache (default 0).
        cache_write_tokens: Number of tokens written to cache (default 0).
        pricing: Optional pre-fetched pricing. If not provided,
                 pricing is looked up from the default table.

    Returns:
        Cost in USD if pricing is available, None otherwise.
    """
    if pricing is None:
        pricing = get_pricing(model)

    if pricing is None:
        return None

    cost = (
        (input_tokens / 1_000_000) * pricing.input_per_mtok
        + (output_tokens / 1_000_000) * pricing.output_per_mtok
        + (cache_read_tokens / 1_000_000) * pricing.cache_read_per_mtok
        + (cache_write_tokens / 1_000_000) * pricing.cache_write_per_mtok
    )

    return cost
