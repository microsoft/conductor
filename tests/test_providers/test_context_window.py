"""Tests for context window lookups via the unified pricing registry."""

from __future__ import annotations

from conductor.engine.pricing import DEFAULT_PRICING, get_pricing


def _context_window(model: str) -> int | None:
    """Helper: look up context window via the pricing registry."""
    pricing = get_pricing(model)
    return pricing.context_window if pricing else None


class TestExactMatch:
    """Exact model name lookups."""

    def test_claude_sonnet_4(self) -> None:
        assert _context_window("claude-sonnet-4") == 200_000

    def test_claude_opus_4_6_1m(self) -> None:
        assert _context_window("claude-opus-4.6-1m") == 1_000_000

    def test_gpt_4o(self) -> None:
        assert _context_window("gpt-4o") == 128_000

    def test_gpt_4_legacy(self) -> None:
        assert _context_window("gpt-4") == 8_192

    def test_gpt_4_1(self) -> None:
        assert _context_window("gpt-4.1") == 1_047_576

    def test_short_alias(self) -> None:
        assert _context_window("sonnet-4.5") == 200_000

    def test_gemini(self) -> None:
        assert _context_window("gemini-3.1-pro-preview") == 1_000_000


class TestPrefixMatch:
    """Prefix-based fuzzy matching."""

    def test_dated_suffix(self) -> None:
        assert _context_window("claude-sonnet-4-20250514") == 200_000

    def test_latest_suffix(self) -> None:
        assert _context_window("claude-3-5-sonnet-latest") == 200_000

    def test_preview_suffix(self) -> None:
        assert _context_window("claude-3-5-sonnet-preview") == 200_000

    def test_o1_mini_prefers_longer_key(self) -> None:
        """o1-mini must match 'o1-mini' (128K), not 'o1' (200K)."""
        assert _context_window("o1-mini") == 128_000

    def test_o1_mini_with_date(self) -> None:
        assert _context_window("o1-mini-20240101") == 128_000

    def test_dot_and_dash_notation(self) -> None:
        assert _context_window("claude-3.5-sonnet") == 200_000
        assert _context_window("claude-3-5-sonnet") == 200_000


class TestUnknownModel:
    """Unknown models return None."""

    def test_completely_unknown(self) -> None:
        assert _context_window("totally-unknown-model") is None

    def test_empty_string(self) -> None:
        assert _context_window("") is None

    def test_partial_gpt(self) -> None:
        assert _context_window("gpt") is None


class TestTableConsistency:
    """Sanity checks on the registry."""

    def test_all_context_windows_positive(self) -> None:
        for model, pricing in DEFAULT_PRICING.items():
            if pricing.context_window is not None:
                assert pricing.context_window > 0, f"{model} has non-positive context window"

    def test_all_have_context_window(self) -> None:
        for model, pricing in DEFAULT_PRICING.items():
            assert pricing.context_window is not None, f"{model} is missing context_window"
