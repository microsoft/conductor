"""Tests for the duration parser used by the wait step.

Covers plain numeric inputs, all supported unit suffixes, whitespace
tolerance, and rejection of malformed / unsupported values.
"""

from __future__ import annotations

import pytest

from conductor.duration import parse_duration


class TestParseDurationNumeric:
    """Plain numeric durations are interpreted as seconds."""

    def test_int(self) -> None:
        assert parse_duration(60) == 60.0

    def test_zero(self) -> None:
        assert parse_duration(0) == 0.0

    def test_float(self) -> None:
        assert parse_duration(1.5) == 1.5

    def test_returns_float(self) -> None:
        assert isinstance(parse_duration(1), float)


class TestParseDurationStrings:
    """String durations support ms/s/m/h suffixes and bare numbers."""

    def test_bare_number(self) -> None:
        assert parse_duration("60") == 60.0

    def test_bare_float(self) -> None:
        assert parse_duration("2.5") == 2.5

    def test_seconds_suffix(self) -> None:
        assert parse_duration("60s") == 60.0

    def test_milliseconds(self) -> None:
        assert parse_duration("500ms") == 0.5

    def test_minutes(self) -> None:
        assert parse_duration("5m") == 300.0

    def test_fractional_minutes(self) -> None:
        assert parse_duration("2.5m") == 150.0

    def test_hours(self) -> None:
        assert parse_duration("1h") == 3600.0

    def test_fractional_hours(self) -> None:
        assert parse_duration("0.5h") == 1800.0


class TestParseDurationWhitespace:
    """Surrounding and intra-token whitespace is tolerated."""

    def test_leading_trailing(self) -> None:
        assert parse_duration("  60s  ") == 60.0

    def test_between_value_and_unit(self) -> None:
        assert parse_duration("60 s") == 60.0

    def test_lots_of_whitespace(self) -> None:
        assert parse_duration("\t  5 m  \n") == 300.0


class TestParseDurationRejects:
    """Malformed and unsupported inputs raise ValueError."""

    def test_bool_true(self) -> None:
        with pytest.raises(ValueError, match="boolean"):
            parse_duration(True)  # type: ignore[arg-type]

    def test_bool_false(self) -> None:
        with pytest.raises(ValueError, match="boolean"):
            parse_duration(False)  # type: ignore[arg-type]

    def test_empty_string(self) -> None:
        with pytest.raises(ValueError):
            parse_duration("")

    def test_whitespace_only(self) -> None:
        with pytest.raises(ValueError):
            parse_duration("   ")

    def test_unsupported_unit_d(self) -> None:
        with pytest.raises(ValueError):
            parse_duration("1d")

    def test_unsupported_unit_us(self) -> None:
        with pytest.raises(ValueError):
            parse_duration("100us")

    def test_garbage(self) -> None:
        with pytest.raises(ValueError):
            parse_duration("forever")

    def test_negative_number(self) -> None:
        # Bare negatives are not matched by the parser. (Out-of-range
        # checks are the caller's responsibility, but we don't accept
        # negative literals because the grammar reads as "<number>[unit]"
        # with non-negative numbers.)
        with pytest.raises(ValueError):
            parse_duration("-5s")

    def test_none(self) -> None:
        with pytest.raises(ValueError):
            parse_duration(None)  # type: ignore[arg-type]

    def test_list(self) -> None:
        with pytest.raises(ValueError):
            parse_duration([60])  # type: ignore[arg-type]

    def test_number_then_garbage(self) -> None:
        with pytest.raises(ValueError):
            parse_duration("5 minutes")
