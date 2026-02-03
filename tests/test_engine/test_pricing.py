"""Unit tests for the pricing module."""

import pytest

from conductor.engine.pricing import (
    DEFAULT_PRICING,
    ModelPricing,
    calculate_cost,
    get_pricing,
)


class TestModelPricing:
    """Tests for the ModelPricing dataclass."""

    def test_model_pricing_creation(self) -> None:
        """Test creating ModelPricing with all fields."""
        pricing = ModelPricing(
            input_per_mtok=3.0,
            output_per_mtok=15.0,
            cache_read_per_mtok=0.3,
            cache_write_per_mtok=3.75,
        )
        assert pricing.input_per_mtok == 3.0
        assert pricing.output_per_mtok == 15.0
        assert pricing.cache_read_per_mtok == 0.3
        assert pricing.cache_write_per_mtok == 3.75

    def test_model_pricing_defaults(self) -> None:
        """Test ModelPricing with default cache values."""
        pricing = ModelPricing(input_per_mtok=2.5, output_per_mtok=10.0)
        assert pricing.cache_read_per_mtok == 0.0
        assert pricing.cache_write_per_mtok == 0.0

    def test_model_pricing_is_frozen(self) -> None:
        """Test that ModelPricing is immutable."""
        from dataclasses import FrozenInstanceError

        pricing = ModelPricing(input_per_mtok=3.0, output_per_mtok=15.0)
        with pytest.raises(FrozenInstanceError):
            pricing.input_per_mtok = 5.0  # type: ignore[misc]


class TestGetPricing:
    """Tests for the get_pricing function."""

    def test_get_pricing_exact_match(self) -> None:
        """Test getting pricing for an exact model name match."""
        pricing = get_pricing("gpt-4o")
        assert pricing is not None
        assert pricing.input_per_mtok == 2.5
        assert pricing.output_per_mtok == 10.0

    def test_get_pricing_claude_model(self) -> None:
        """Test getting pricing for Claude model."""
        pricing = get_pricing("claude-sonnet-4")
        assert pricing is not None
        assert pricing.input_per_mtok == 3.0
        assert pricing.output_per_mtok == 15.0
        assert pricing.cache_read_per_mtok == 0.3
        assert pricing.cache_write_per_mtok == 3.75

    def test_get_pricing_fuzzy_match_versioned(self) -> None:
        """Test fuzzy matching for versioned model names."""
        # Model with date suffix should match base model
        pricing = get_pricing("claude-sonnet-4-20250514")
        assert pricing is not None
        assert pricing.input_per_mtok == 3.0

    def test_get_pricing_fuzzy_match_latest(self) -> None:
        """Test fuzzy matching for -latest suffix."""
        pricing = get_pricing("claude-3-5-sonnet-latest")
        assert pricing is not None
        # Should match claude-3-5-sonnet
        assert pricing.input_per_mtok == 3.0

    def test_get_pricing_unknown_model(self) -> None:
        """Test that unknown models return None."""
        pricing = get_pricing("unknown-model-v1")
        assert pricing is None

    def test_get_pricing_with_overrides(self) -> None:
        """Test that overrides take precedence."""
        custom_pricing = ModelPricing(input_per_mtok=99.0, output_per_mtok=199.0)
        overrides = {"custom-model": custom_pricing}

        pricing = get_pricing("custom-model", overrides=overrides)
        assert pricing is not None
        assert pricing.input_per_mtok == 99.0
        assert pricing.output_per_mtok == 199.0

    def test_get_pricing_override_over_default(self) -> None:
        """Test that overrides take precedence over defaults."""
        custom_pricing = ModelPricing(input_per_mtok=1.0, output_per_mtok=2.0)
        overrides = {"gpt-4o": custom_pricing}

        pricing = get_pricing("gpt-4o", overrides=overrides)
        assert pricing is not None
        assert pricing.input_per_mtok == 1.0  # Override, not default
        assert pricing.output_per_mtok == 2.0

    def test_default_pricing_table_has_expected_models(self) -> None:
        """Test that default pricing table contains expected models."""
        expected_models = [
            "gpt-4o",
            "gpt-4o-mini",
            "claude-sonnet-4",
            "claude-opus-4",
            "claude-3-5-sonnet",
        ]
        for model in expected_models:
            assert model in DEFAULT_PRICING, f"Expected {model} in DEFAULT_PRICING"


class TestCalculateCost:
    """Tests for the calculate_cost function."""

    def test_calculate_cost_basic(self) -> None:
        """Test basic cost calculation."""
        # 1M input tokens at $3/M = $3
        # 1M output tokens at $15/M = $15
        # Total = $18
        cost = calculate_cost(
            model="claude-sonnet-4",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        assert cost is not None
        assert cost == pytest.approx(18.0, rel=1e-6)

    def test_calculate_cost_with_cache_tokens(self) -> None:
        """Test cost calculation with cache tokens."""
        # Using claude-sonnet-4:
        # input: 1M * $3/M = $3
        # output: 1M * $15/M = $15
        # cache_read: 1M * $0.3/M = $0.3
        # cache_write: 1M * $3.75/M = $3.75
        # Total = $22.05
        cost = calculate_cost(
            model="claude-sonnet-4",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
        )
        assert cost is not None
        assert cost == pytest.approx(22.05, rel=1e-6)

    def test_calculate_cost_small_tokens(self) -> None:
        """Test cost calculation with small token counts."""
        # 1000 input tokens at $3/M = $0.003
        # 500 output tokens at $15/M = $0.0075
        # Total = $0.0105
        cost = calculate_cost(
            model="claude-sonnet-4",
            input_tokens=1000,
            output_tokens=500,
        )
        assert cost is not None
        assert cost == pytest.approx(0.0105, rel=1e-6)

    def test_calculate_cost_zero_tokens(self) -> None:
        """Test cost calculation with zero tokens."""
        cost = calculate_cost(
            model="claude-sonnet-4",
            input_tokens=0,
            output_tokens=0,
        )
        assert cost is not None
        assert cost == 0.0

    def test_calculate_cost_unknown_model(self) -> None:
        """Test that unknown models return None."""
        cost = calculate_cost(
            model="unknown-model",
            input_tokens=1000,
            output_tokens=500,
        )
        assert cost is None

    def test_calculate_cost_with_explicit_pricing(self) -> None:
        """Test cost calculation with explicitly provided pricing."""
        custom_pricing = ModelPricing(
            input_per_mtok=1.0,
            output_per_mtok=2.0,
        )
        cost = calculate_cost(
            model="any-model",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            pricing=custom_pricing,
        )
        assert cost is not None
        assert cost == pytest.approx(3.0, rel=1e-6)  # $1 + $2

    def test_calculate_cost_gpt4o(self) -> None:
        """Test cost calculation for gpt-4o model."""
        # gpt-4o: $2.5/M input, $10/M output
        cost = calculate_cost(
            model="gpt-4o",
            input_tokens=100_000,  # $0.25
            output_tokens=50_000,  # $0.50
        )
        assert cost is not None
        assert cost == pytest.approx(0.75, rel=1e-6)


class TestPricingIntegration:
    """Integration tests for pricing functionality."""

    def test_all_default_models_have_valid_pricing(self) -> None:
        """Test that all default models can calculate costs."""
        for model_name, _pricing in DEFAULT_PRICING.items():
            cost = calculate_cost(
                model=model_name,
                input_tokens=1000,
                output_tokens=500,
            )
            assert cost is not None, f"Failed to calculate cost for {model_name}"
            assert cost >= 0, f"Negative cost for {model_name}"

    def test_cache_pricing_only_for_claude_models(self) -> None:
        """Test that cache pricing is only set for Claude models."""
        for model_name, pricing in DEFAULT_PRICING.items():
            if model_name.startswith("claude"):
                # Claude models should have cache pricing
                assert pricing.cache_read_per_mtok >= 0
                assert pricing.cache_write_per_mtok >= 0
            elif model_name.startswith("gpt"):
                # GPT models don't have cache pricing
                assert pricing.cache_read_per_mtok == 0
                assert pricing.cache_write_per_mtok == 0
