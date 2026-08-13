"""Unit tests for the pricing module."""

import logging

import pytest

from conductor.engine import pricing as pricing_module
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

    def test_dotted_claude_names_are_priced(self) -> None:
        """The SDK-advertised dotted names must resolve, not fall through to None.

        ``get_pricing``'s versioned-suffix fallback only extends a key with a
        ``-`` delimiter, so ``claude-haiku-4.5`` never matched the dashed
        ``claude-haiku-4-5`` entry and the newest models priced as ``None``.
        """
        for name, expected_input in (
            ("claude-opus-5", 5.0),
            ("claude-opus-4.5", 5.0),
            ("claude-sonnet-4.5", 3.0),
            ("claude-haiku-4.5", 1.0),
        ):
            pricing = get_pricing(name)
            assert pricing is not None, f"{name} has no pricing"
            assert pricing.input_per_mtok == expected_input
            # A Claude model priced without a cache-read rate would bill
            # cached tokens at zero once calculate_cost splits the buckets.
            assert pricing.cache_read_per_mtok > 0

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

    def test_provider_pricing_used_for_unknown_model(self) -> None:
        """Provider-supplied pricing prices a model absent from the table (#265)."""
        provider_pricing = {"brand-new": ModelPricing(input_per_mtok=7.0, output_per_mtok=9.0)}
        pricing = get_pricing("brand-new", provider_pricing=provider_pricing)
        assert pricing is not None
        assert pricing.input_per_mtok == 7.0
        assert pricing.output_per_mtok == 9.0

    def test_provider_pricing_beats_default_table(self) -> None:
        """Provider pricing takes precedence over the static table (#265)."""
        provider_pricing = {"gpt-4o": ModelPricing(input_per_mtok=1.0, output_per_mtok=1.0)}
        pricing = get_pricing("gpt-4o", provider_pricing=provider_pricing)
        assert pricing is not None
        assert pricing.input_per_mtok == 1.0  # Provider hook, not the $2.50 default

    def test_override_beats_provider_pricing(self) -> None:
        """Workflow override outranks the provider hook (#265)."""
        overrides = {"m": ModelPricing(input_per_mtok=2.0, output_per_mtok=2.0)}
        provider_pricing = {"m": ModelPricing(input_per_mtok=99.0, output_per_mtok=99.0)}
        pricing = get_pricing("m", overrides=overrides, provider_pricing=provider_pricing)
        assert pricing is not None
        assert pricing.input_per_mtok == 2.0  # Override wins

    def test_provider_pricing_is_exact_match_only(self) -> None:
        """Provider pricing matches by exact model name — no fuzzy inheritance."""
        provider_pricing = {"parent": ModelPricing(input_per_mtok=5.0, output_per_mtok=5.0)}
        # A suffixed name must NOT inherit the provider entry (unlike the static
        # table's versioned-suffix fuzzy match); it falls through to None here.
        assert get_pricing("parent-child", provider_pricing=provider_pricing) is None

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

    @pytest.mark.parametrize(
        ("model", "input_per_mtok", "output_per_mtok"),
        [
            # GPT-5.x family (standard rate) and mini variants (#266).
            ("gpt-5.5", 2.00, 8.00),
            ("gpt-5.4", 2.00, 8.00),
            ("gpt-5.3-codex", 2.00, 8.00),
            ("gpt-5-mini", 0.15, 0.60),
            ("gpt-5.4-mini", 0.15, 0.60),
            # Claude families (#266).
            ("claude-opus-4.8", 5.00, 25.00),
            ("claude-opus-4.7", 5.00, 25.00),
            ("claude-sonnet-5", 3.00, 15.00),
            # Gemini (#266).
            ("gemini-3.5-flash", 0.30, 2.50),
        ],
    )
    def test_current_models_have_exact_pricing(
        self, model: str, input_per_mtok: float, output_per_mtok: float
    ) -> None:
        """Current models resolve via an exact key, not the $0 None fallback (#266).

        These dotted version suffixes are not bridged by the ``-``-delimited
        fuzzy fallback, so a missing exact key silently costs $0.
        """
        assert model in DEFAULT_PRICING, f"Expected {model} in DEFAULT_PRICING"
        pricing = get_pricing(model)
        assert pricing is not None, f"{model} unexpectedly returned None pricing"
        assert pricing.input_per_mtok == input_per_mtok
        assert pricing.output_per_mtok == output_per_mtok


class TestGpt56Pricing:
    """Tests for the GPT-5.6 pricing entries added in #386."""

    @pytest.fixture(autouse=True)
    def _reset_warned(self) -> None:
        """Clear the per-process warning de-dupe set between tests."""
        from conductor.engine.pricing import _FUZZY_MATCH_WARNED

        _FUZZY_MATCH_WARNED.clear()

    @pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"])
    def test_gpt_56_variants_priced_at_family_rate(self, model: str) -> None:
        """Each GPT-5.6 id resolves via an exact key at the GPT-5.x family rate."""
        pricing = get_pricing(model)
        assert pricing is not None
        assert pricing.input_per_mtok == 2.00
        assert pricing.output_per_mtok == 8.00

    @pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"])
    def test_gpt_56_variants_do_not_fuzzy_warn(
        self, model: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Exact keys resolve silently — pins the "explicit ids, not a prefix
        key" decision so a later well-meaning `gpt-5.6` prefix key cannot
        reintroduce a fuzzy-match warning for these ids without a test
        failure calling it out."""
        with caplog.at_level("WARNING", logger="conductor.engine.pricing"):
            get_pricing(model)
        assert caplog.records == []

    @pytest.mark.parametrize(
        "model",
        [
            "grok-4.5",
            "gemini-3.6-flash",
            "mai-code-1.1-flash",
            "mai-code-1-flash-picker",
        ],
    )
    def test_unconfirmed_models_remain_unpriced(self, model: str) -> None:
        """These ids are deliberately absent pending a published rate (#386).

        Load-bearing regression guard: it stops a later fuzzy key silently
        inventing rates for them.
        """
        assert get_pricing(model) is None


class TestFuzzyMatchWarnings:
    """Tests for the fuzzy-match warning behavior (#137)."""

    @pytest.fixture(autouse=True)
    def _reset_warned(self) -> None:
        """Clear the per-process warning de-dupe set between tests."""
        from conductor.engine.pricing import _FUZZY_MATCH_WARNED

        _FUZZY_MATCH_WARNED.clear()

    def test_exact_match_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="conductor.engine.pricing"):
            get_pricing("gpt-4o")
        assert caplog.records == []

    def test_override_match_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        overrides = {"my-model": ModelPricing(input_per_mtok=1.0, output_per_mtok=2.0)}
        with caplog.at_level("WARNING", logger="conductor.engine.pricing"):
            get_pricing("my-model", overrides=overrides)
        assert caplog.records == []

    def test_unknown_model_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        # Names with no matching prefix return None and should not warn.
        with caplog.at_level("WARNING", logger="conductor.engine.pricing"):
            result = get_pricing("totally-unknown-xyz")
        assert result is None
        assert caplog.records == []

    def test_cross_family_name_returns_none_no_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        # Repro from #137: a dotted version suffix shares a textual prefix with
        # the dashed base key claude-opus-4 but is a different family. The
        # delimiter check must reject the match entirely (returning None and
        # degrading gracefully) rather than silently inheriting claude-opus-4's
        # pricing/context-window. A deliberately synthetic version (4.123) is
        # used so this stays absent from DEFAULT_PRICING as real dotted models
        # (4.6/4.7/4.8/…) get added over time.
        with caplog.at_level("WARNING", logger="conductor.engine.pricing"):
            assert get_pricing("claude-opus-4.123") is None
            assert get_pricing("claude-opus-4.123-high") is None
            assert get_pricing("claude-opus-4.123-xhigh") is None
            assert get_pricing("claude-opus-4.123-1m-internal") is None
        assert caplog.records == []

    def test_versioned_suffix_match_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        # Real versioned name (date suffix) should still match and warn.
        with caplog.at_level("WARNING", logger="conductor.engine.pricing"):
            pricing = get_pricing("claude-sonnet-4-20250514")
        assert pricing is not None
        assert len(caplog.records) == 1
        msg = caplog.records[0].getMessage()
        assert "claude-sonnet-4-20250514" in msg
        assert "versioned-suffix" in msg
        assert "claude-sonnet-4" in msg

    def test_warning_emitted_only_once_per_model(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="conductor.engine.pricing"):
            get_pricing("claude-sonnet-4-20250514")
            get_pricing("claude-sonnet-4-20250514")
            get_pricing("claude-sonnet-4-20250514")
        assert len(caplog.records) == 1

    def test_different_models_each_warn_once(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="conductor.engine.pricing"):
            get_pricing("claude-sonnet-4-20250514")
            get_pricing("claude-3-5-sonnet-latest")
        assert len(caplog.records) == 2


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
        """Cache buckets are subsets of ``input_tokens``, not additions to it."""
        # Using claude-sonnet-4, with a 3M-token prompt of which 1M was read
        # from cache and 1M written to cache, leaving 1M uncached:
        # uncached input: 1M * $3/M    = $3
        # output:         1M * $15/M   = $15
        # cache_read:     1M * $0.3/M  = $0.3
        # cache_write:    1M * $3.75/M = $3.75
        # Total = $22.05
        cost = calculate_cost(
            model="claude-sonnet-4",
            input_tokens=3_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
        )
        assert cost is not None
        assert cost == pytest.approx(22.05, rel=1e-6)

    def test_cache_tokens_are_not_billed_twice(self) -> None:
        """A fully-cached prompt costs the cache rate, not input + cache.

        Regression guard for the cost blow-up on long tool-calling agents:
        ``input_tokens`` is the whole prompt and already contains the cached
        buckets, so billing them additively charged every cached token at
        ``input + cache_read`` (11x for claude-sonnet-4) and reported tens of
        dollars for a workflow that cost a few.
        """
        # Entire 1M-token prompt served from cache; nothing fresh.
        cost = calculate_cost(
            model="claude-sonnet-4",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=1_000_000,
        )
        assert cost is not None
        # 1M * $0.3/M — the cache-read rate alone, not $3.00 + $0.30.
        assert cost == pytest.approx(0.3, rel=1e-6)

    def test_calculate_cost_clamps_inconsistent_cache_counts(self) -> None:
        """Cache counts exceeding ``input_tokens`` clamp instead of going negative.

        Deliberately clamps rather than raising the way ``genai-prices`` does:
        a cost annotation must never abort a running workflow. The anomaly is
        logged once per model instead.
        """
        cost = calculate_cost(
            model="claude-sonnet-4",
            input_tokens=1000,
            output_tokens=0,
            cache_read_tokens=5000,
        )
        assert cost is not None
        # Uncached input floors at 0, so only the cache read is charged.
        assert cost == pytest.approx(5000 / 1_000_000 * 0.3, rel=1e-6)

    @pytest.mark.parametrize(
        ("cache_read", "cache_write", "expected"),
        [
            # Each bucket is charged at its own rate; the input bucket floors
            # at zero rather than contributing a negative amount.
            pytest.param(5000, 0, 5000 / 1e6 * 0.3, id="read-only-overflow"),
            pytest.param(0, 5000, 5000 / 1e6 * 3.75, id="write-only-overflow"),
            pytest.param(4000, 4000, 4000 / 1e6 * 0.3 + 4000 / 1e6 * 3.75, id="combined-overflow"),
            pytest.param(500, 0, 500 / 1e6 * 0.3, id="zero-input-with-cache"),
        ],
    )
    def test_clamp_never_yields_a_negative_contribution(
        self, cache_read: int, cache_write: int, expected: float
    ) -> None:
        """Every overflow shape floors the input bucket, not just cache_read.

        A negative bucket would flow into ``UsageTracker`` and subtract from
        the workflow total, so each route into the clamp needs pinning.
        """
        cost = calculate_cost(
            model="claude-sonnet-4",
            input_tokens=0 if cache_read == 500 else 1000,
            output_tokens=0,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        assert cost is not None
        assert cost >= 0
        assert cost == pytest.approx(expected, rel=1e-6)

    def test_inconsistent_cache_counts_are_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """The clamp is audible.

        A clamped cost is an ordinary float, so it flows past every
        ``unpriced`` guard and prints as a certainty. Without a log line the
        provider bug that produced it is as invisible as the double-count was.
        """
        pricing_module._INCONSISTENT_CACHE_WARNED.discard("claude-sonnet-4")
        with caplog.at_level(logging.WARNING):
            calculate_cost(
                model="claude-sonnet-4",
                input_tokens=1000,
                output_tokens=0,
                cache_read_tokens=5000,
            )
        assert "exceeding its total input_tokens" in caplog.text

        # One line per model per process, matching the fuzzy-match warning.
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            calculate_cost(
                model="claude-sonnet-4",
                input_tokens=1000,
                output_tokens=0,
                cache_read_tokens=5000,
            )
        assert caplog.text == ""

    def test_cache_tokens_without_a_cache_rate_bill_at_the_input_rate(self) -> None:
        """A ``0.0`` cache rate means "no published rate", never "free".

        20 of the table's entries (every GPT, o-series and Gemini model) leave
        both cache rates at the dataclass default while their providers do
        report cache counts. Subtracting those buckets unconditionally would
        price the cached majority of a prompt at nothing — the same silent
        under-billing this function exists to remove, in the other direction.
        """
        pricing = get_pricing("gpt-5.5")
        assert pricing is not None
        assert pricing.cache_read_per_mtok == 0.0

        cached = calculate_cost(
            model="gpt-5.5",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=950_000,
        )
        uncached = calculate_cost(model="gpt-5.5", input_tokens=1_000_000, output_tokens=0)
        assert cached == uncached
        assert cached == pytest.approx(2.0, rel=1e-6)

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
        """Test that cache pricing is only set for Claude models.

        A ``0.0`` cache rate is not "free" — ``calculate_cost`` reads it as
        "no published rate" and leaves those tokens in the input bucket.
        """
        for model_name, pricing in DEFAULT_PRICING.items():
            if model_name.startswith("claude"):
                # Claude models should have cache pricing
                assert pricing.cache_read_per_mtok >= 0
                assert pricing.cache_write_per_mtok >= 0
            elif model_name.startswith("gpt"):
                # GPT models don't have cache pricing
                assert pricing.cache_read_per_mtok == 0
                assert pricing.cache_write_per_mtok == 0

    @pytest.mark.parametrize(
        "spellings",
        [
            pytest.param(("claude-opus-4-5", "claude-opus-4.5", "opus-4.5"), id="opus-4.5"),
            pytest.param(("claude-sonnet-4-5", "claude-sonnet-4.5", "sonnet-4.5"), id="sonnet-4.5"),
            pytest.param(("claude-haiku-4-5", "claude-haiku-4.5", "haiku-4.5"), id="haiku-4.5"),
        ],
    )
    def test_alias_spellings_agree_on_every_rate(self, spellings: tuple[str, ...]) -> None:
        """Dashed, dotted and short spellings of one model must price identically.

        Each spelling is a separate table entry repeating four float literals,
        so a rate update applied to one and not the others is silent and
        produces a plausible wrong number for whichever spelling the SDK
        happens to report.
        """
        resolved = [get_pricing(name) for name in spellings]
        assert all(p is not None for p in resolved), spellings
        first = resolved[0]
        assert first is not None
        for name, pricing in zip(spellings, resolved, strict=True):
            assert pricing == first, f"{name} has drifted from {spellings[0]}"
