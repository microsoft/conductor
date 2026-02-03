"""Pricing tables and cost calculation for LLM models.

This module provides pricing information for various LLM models
and functions to calculate costs based on token usage.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    """Pricing per million tokens for a model.

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


# Default pricing table (January 2026)
# Sources: OpenAI pricing page, Anthropic pricing page
DEFAULT_PRICING: dict[str, ModelPricing] = {
    # OpenAI / Copilot models
    "gpt-4-turbo": ModelPricing(input_per_mtok=10.00, output_per_mtok=30.00),
    "gpt-4o": ModelPricing(input_per_mtok=2.50, output_per_mtok=10.00),
    "gpt-4o-mini": ModelPricing(input_per_mtok=0.15, output_per_mtok=0.60),
    "gpt-4.1-mini": ModelPricing(input_per_mtok=0.15, output_per_mtok=0.60),  # Alias
    "gpt-4": ModelPricing(input_per_mtok=30.00, output_per_mtok=60.00),
    "gpt-3.5-turbo": ModelPricing(input_per_mtok=0.50, output_per_mtok=1.50),
    # Claude 4.5 Series (newest)
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
    # Claude 3.7 Series (aliases to 4 series for backward compatibility)
    "claude-3-7-sonnet": ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    # Claude 3.5 Series
    "claude-3-5-sonnet": ModelPricing(
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
    # Claude 3 Series (legacy)
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
}


def get_pricing(
    model: str,
    overrides: dict[str, ModelPricing] | None = None,
) -> ModelPricing | None:
    """Get pricing for a model.

    Checks user-provided overrides first, then falls back to
    the default pricing table. Supports fuzzy matching for
    versioned model names (e.g., "claude-sonnet-4-20250514"
    matches "claude-sonnet-4").

    Args:
        model: The model name to look up.
        overrides: Optional user-provided pricing overrides.

    Returns:
        ModelPricing if found, None otherwise.
    """
    # Check overrides first
    if overrides and model in overrides:
        return overrides[model]

    # Try exact match
    if model in DEFAULT_PRICING:
        return DEFAULT_PRICING[model]

    # Try fuzzy matching for versioned model names
    # e.g., "claude-sonnet-4-20250514" -> "claude-sonnet-4"
    # e.g., "gpt-4o-2024-08-06" -> "gpt-4o"
    for known_model, pricing in DEFAULT_PRICING.items():
        if model.startswith(known_model):
            return pricing

    # Try removing date suffix patterns for common formats
    # e.g., "claude-3-5-sonnet-20241022" -> "claude-3-5-sonnet"
    # e.g., "claude-3-5-sonnet-latest" -> "claude-3-5-sonnet"
    import re

    # Remove common suffixes like -20241022, -latest, -preview
    simplified = re.sub(r"-(\d{8}|latest|preview)$", "", model)
    if simplified in DEFAULT_PRICING:
        return DEFAULT_PRICING[simplified]

    # Try matching simplified version against known models
    for known_model, pricing in DEFAULT_PRICING.items():
        if simplified.startswith(known_model):
            return pricing

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
