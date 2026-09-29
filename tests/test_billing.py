"""Tests for :mod:`conductor.billing`: coercion, aggregation, wire form, and wording."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from conductor import billing
from conductor.billing import (
    AggregateBilling,
    BillingClass,
    BillingMode,
    aggregate_label,
    cell_label,
    cell_legend,
    coerce_billing_mode,
    count_mode,
    estimate_note,
    mode_label,
)

_NONE: Any = None


class TestCoerceBillingMode:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("metered_api", "metered_api"),
            ("subscription", "subscription"),
            ("unknown", "unknown"),
            ("bedrock", "unknown"),  # a newer writer's value: unproven, never dropped
            ("", "unknown"),
            ("mixed", "unknown"),  # aggregate-only state is not a per-execution mode
            (3, None),
            (1.5, None),
            (True, None),
            ({"mode": "subscription"}, None),
            (["subscription"], None),
        ],
    )
    def test_coerce_billing_mode_table(self, value: object, expected: object) -> None:
        assert coerce_billing_mode(value) == expected


# (contributing modes, breakdown, state, stated, console label, cell/bar label)
_ROWS: list[tuple[list[BillingMode | None], dict[str, int], str, bool, str | None, str | None]] = [
    ([], {}, "unknown", False, None, None),
    (["metered_api"] * 3, {"metered_api": 3}, "metered_api", True, None, None),
    (
        ["subscription"] * 3,
        {"subscription": 3},
        "subscription",
        True,
        "API-equivalent estimate",
        "est.",
    ),
    (
        ["subscription", "subscription", "metered_api"],
        {"subscription": 2, "metered_api": 1},
        "mixed",
        True,
        "mixed billing: 2 subscription, 1 metered API",
        "mixed",
    ),
    (
        ["subscription", "metered_api", "unknown"],
        {"subscription": 1, "metered_api": 1, "unknown": 1},
        "mixed",
        True,
        "mixed billing: 1 subscription, 1 metered API, 1 unknown",
        "mixed",
    ),
    (
        ["subscription", "subscription", "unknown"],
        {"subscription": 2, "unknown": 1},
        "mixed",
        True,
        "mixed billing: 2 subscription, 1 unknown",
        "mixed",
    ),
    (
        ["subscription", None],
        {"subscription": 1, "unstated": 1},
        "mixed",
        True,
        "mixed billing: 1 subscription, 1 not reported",
        "mixed",
    ),
    (
        ["metered_api", None, None],
        {"metered_api": 1, "unstated": 2},
        "mixed",
        True,
        "mixed billing: 1 metered API, 2 not reported",
        "mixed",
    ),
    (["unknown", "unknown"], {"unknown": 2}, "unknown", True, "billing source unknown", "src?"),
    (
        ["unknown", None],
        {"unknown": 1, "unstated": 1},
        "unknown",
        True,
        "billing source unknown",
        "src?",
    ),
    ([None, None, None], {"unstated": 3}, "unknown", False, None, None),
    (
        ["subscription", "metered_api"],
        {"subscription": 1, "metered_api": 1},
        "mixed",
        True,
        "mixed billing: 1 subscription, 1 metered API",
        "mixed",
    ),
]


class TestAggregation:
    @pytest.mark.parametrize(("modes", "breakdown", "state", "stated", "label", "cell"), _ROWS)
    def test_aggregate_truth_table(
        self,
        modes: list[BillingMode | None],
        breakdown: dict[str, int],
        state: str,
        stated: bool,
        label: str | None,
        cell: str | None,
    ) -> None:
        agg = AggregateBilling.from_modes(modes)
        assert dict(agg.breakdown) == breakdown
        assert agg.state == state
        assert agg.stated is stated
        assert aggregate_label(agg, detailed=True) == label
        assert cell_label(agg) == cell
        assert agg.total == len(modes)

    def test_two_subscription_one_metered_is_mixed_not_majority(self) -> None:
        agg = AggregateBilling.from_modes(["subscription", "subscription", "metered_api"])
        assert agg.state == "mixed"

    def test_known_plus_unknown_is_mixed(self) -> None:
        assert AggregateBilling.from_modes(["subscription", "unknown"]).state == "mixed"
        assert AggregateBilling.from_modes(["metered_api", "unknown"]).state == "mixed"

    def test_known_plus_unstated_is_mixed(self) -> None:
        assert AggregateBilling.from_modes(["subscription", None]).state == "mixed"

    def test_state_is_permutation_invariant(self) -> None:
        modes: list[BillingMode | None] = ["subscription", "metered_api", "unknown", None]
        seen = set()
        for perm in itertools.permutations(modes):
            agg = AggregateBilling.from_modes(perm)
            seen.add((agg.state, tuple(agg.breakdown.items())))
        assert len(seen) == 1

    def test_breakdown_holds_only_positive_counts(self) -> None:
        agg = AggregateBilling({"subscription": 2, "metered_api": 0})
        assert dict(agg.breakdown) == {"subscription": 2}

    @pytest.mark.parametrize(
        "bad",
        [
            {"subscription": -1},
            {"subscription": True},
            {"subscription": 1.5},
            {"mixed": 1},
            {"bedrock": 1},
        ],
    )
    def test_rejects_zero_negative_bool_and_unknown_class(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            AggregateBilling(bad)

    def test_breakdown_is_immutable(self) -> None:
        agg = AggregateBilling({"subscription": 1})
        mapping: Any = agg.breakdown
        with pytest.raises(TypeError):
            mapping["subscription"] = 5

    def test_count_mode_maps_none_to_unstated(self) -> None:
        counts: dict[BillingClass, int] = {}
        count_mode(counts, None)
        count_mode(counts, None)
        count_mode(counts, "subscription")
        assert counts == {"unstated": 2, "subscription": 1}

    def test_from_counts_none_only_when_empty(self) -> None:
        assert AggregateBilling.from_counts({}) is None
        assert AggregateBilling.from_counts({"subscription": 0}) is None
        only_unstated = AggregateBilling.from_counts({"unstated": 2})
        assert only_unstated is not None  # unstated-only is NOT None ...
        assert only_unstated.stated is False  # ... but it carries no label
        assert only_unstated.to_wire() is None

    def test_combine_sums_skips_none_and_ignores_order(self) -> None:
        a = AggregateBilling({"subscription": 2})
        b = AggregateBilling({"metered_api": 1, "subscription": 1})
        c = AggregateBilling({"unstated": 1})
        left = AggregateBilling.combine([a, None, b, c])
        right = AggregateBilling.combine([c, b, None, a])
        assert left == right
        assert left is not None
        assert dict(left.breakdown) == {"subscription": 3, "metered_api": 1, "unstated": 1}
        assert AggregateBilling.combine([]) is None
        assert AggregateBilling.combine([None, None]) is None


class TestWire:
    def test_to_wire_none_when_not_stated(self) -> None:
        assert AggregateBilling({}).to_wire() is None
        assert AggregateBilling({"unstated": 4}).to_wire() is None

    def test_wire_round_trip(self) -> None:
        agg = AggregateBilling({"subscription": 2, "metered_api": 1, "unstated": 1})
        wire = agg.to_wire()
        assert wire == {
            "state": "mixed",
            "breakdown": {"subscription": 2, "metered_api": 1, "unstated": 1},
        }
        assert AggregateBilling.from_wire(wire) == agg
        assert AggregateBilling.from_wire(wire, strict=True) == agg

    def test_from_wire_recomputes_state(self) -> None:
        parsed = AggregateBilling.from_wire(
            {"state": "metered_api", "breakdown": {"subscription": 1}}
        )
        assert parsed is not None
        assert parsed.state == "subscription"

    @pytest.mark.parametrize(
        "malformed",
        [
            "subscription",
            ["subscription"],
            7,
            True,
            {"breakdown": "nope"},
            {"breakdown": ["subscription"]},
            {},
            {"breakdown": {"subscription": True}},
            {"breakdown": {"subscription": "2"}},
            {"breakdown": {"subscription": -1}},
            {"breakdown": {1: 2}},
        ],
    )
    def test_from_wire_tolerant_vs_strict(self, malformed: object) -> None:
        # Non-strict callers (events, JSON consumers) treat malformed input as "not stated".
        tolerant = AggregateBilling.from_wire(malformed)
        assert tolerant is None
        with pytest.raises(ValueError):
            AggregateBilling.from_wire(malformed, strict=True)

    def test_non_string_state_is_only_an_error_when_strict(self) -> None:
        wire = {"state": 3, "breakdown": {"subscription": 1}}
        assert AggregateBilling.from_wire(wire) == AggregateBilling({"subscription": 1})
        with pytest.raises(ValueError):
            AggregateBilling.from_wire(wire, strict=True)

    def test_from_wire_none_is_none_in_both_modes(self) -> None:
        assert AggregateBilling.from_wire(None) is None
        assert AggregateBilling.from_wire(None, strict=True) is None

    def test_unrecognized_breakdown_key_counts_as_unknown(self) -> None:
        parsed = AggregateBilling.from_wire(
            {"breakdown": {"subscription": 1, "bedrock": 2, "vertex": 1}}
        )
        assert parsed is not None
        assert dict(parsed.breakdown) == {"subscription": 1, "unknown": 3}
        assert parsed.state == "mixed"

    def test_unstated_only_wire_parses_to_none(self) -> None:
        assert AggregateBilling.from_wire({"breakdown": {"unstated": 3}}) is None

    def test_zero_counts_parse_to_none(self) -> None:
        assert AggregateBilling.from_wire({"breakdown": {"subscription": 0}}) is None


class TestLabels:
    def test_label_table_exact_strings(self) -> None:
        assert billing.SUBSCRIPTION_LABEL == "API-equivalent estimate"
        assert billing.UNKNOWN_LABEL == "billing source unknown"
        assert billing.MIXED_LABEL == "mixed billing"
        assert billing.SUBSCRIPTION_NOTE == (
            "Estimated at API rates from token counts; not an invoice or an additional charge."
        )
        assert billing.MIXED_NOTE == (
            "Includes subscription executions estimated at API rates; "
            "not an invoice or an additional charge."
        )
        assert mode_label("subscription") == "API-equivalent estimate"
        assert mode_label("unknown") == "billing source unknown"
        assert mode_label("metered_api") is None
        assert mode_label(None) is None
        sub = AggregateBilling({"subscription": 1})
        assert estimate_note(sub) == billing.SUBSCRIPTION_NOTE
        assert estimate_note(AggregateBilling({"subscription": 1, "metered_api": 1})) == (
            billing.MIXED_NOTE
        )
        assert estimate_note(AggregateBilling({"unknown": 1})) is None
        assert estimate_note(AggregateBilling({"metered_api": 1})) is None
        assert estimate_note(None) is None
        # Short (non-detailed) mixed form has no breakdown.
        mixed = AggregateBilling({"subscription": 2, "metered_api": 1})
        assert aggregate_label(mixed) == "mixed billing"
        assert aggregate_label(None) is None

    def test_labels_never_contain_wire_text(self) -> None:
        hostile = "[bold red]INJECT\\[x] $(rm -rf) sk-ant-CANARY"
        parsed = AggregateBilling.from_wire(
            {"state": hostile, "breakdown": {hostile: 2, "subscription": 1}}
        )
        assert parsed is not None
        rendered = [
            aggregate_label(parsed),
            aggregate_label(parsed, detailed=True),
            estimate_note(parsed),
            cell_label(parsed),
            cell_legend([parsed]),
            mode_label(coerce_billing_mode(hostile)),
        ]
        for text in rendered:
            assert text is None or ("INJECT" not in text and "CANARY" not in text)
        assert aggregate_label(parsed, detailed=True) == "mixed billing: 1 subscription, 2 unknown"


class TestCellTier:
    def test_cell_label_table(self) -> None:
        assert cell_label(None) is None
        assert cell_label(AggregateBilling({})) is None
        assert cell_label(AggregateBilling({"unstated": 2})) is None
        assert cell_label(AggregateBilling({"metered_api": 2})) is None
        assert cell_label(AggregateBilling({"subscription": 2})) == "est."
        assert cell_label(AggregateBilling({"unknown": 1})) == "src?"
        assert cell_label(AggregateBilling({"unknown": 1, "unstated": 1})) == "src?"
        assert cell_label(AggregateBilling({"subscription": 1, "unstated": 1})) == "mixed"
        assert cell_label(AggregateBilling({"subscription": 1, "metered_api": 1})) == "mixed"

    def test_cell_labels_are_at_most_five_characters(self) -> None:
        for label in (billing.CELL_SUBSCRIPTION, billing.CELL_UNKNOWN, billing.CELL_MIXED):
            assert len(label) <= 5

    def test_cell_legend_lists_only_present_labels_in_fixed_order(self) -> None:
        sub = AggregateBilling({"subscription": 1})
        unk = AggregateBilling({"unknown": 1})
        mix = AggregateBilling({"subscription": 1, "metered_api": 1})
        met = AggregateBilling({"metered_api": 1})
        assert cell_legend([]) is None
        assert cell_legend([None, met, AggregateBilling({"unstated": 1})]) is None
        assert cell_legend([unk]) == "src? = billing source unknown"
        assert cell_legend([mix, unk, sub]) == (
            "est. = API-equivalent estimate; src? = billing source unknown; "
            "mixed = mixed billing. " + billing.LEGEND_NOTE
        )

    def test_cell_legend_note_only_with_estimates(self) -> None:
        unk = AggregateBilling({"unknown": 1})
        sub = AggregateBilling({"subscription": 1})
        mix = AggregateBilling({"subscription": 1, "unknown": 1})
        assert billing.LEGEND_NOTE not in (cell_legend([unk]) or "")
        assert (cell_legend([sub]) or "").endswith(billing.LEGEND_NOTE)
        assert (cell_legend([mix]) or "").endswith(billing.LEGEND_NOTE)

    def test_cell_label_and_legend_never_contain_wire_text(self) -> None:
        hostile = "[link=x]CANARY[/link]"
        parsed = AggregateBilling.from_wire({"breakdown": {hostile: 1}})
        assert parsed is not None
        assert cell_label(parsed) == "src?"
        assert cell_legend([parsed]) == "src? = billing source unknown"
        assert "CANARY" not in (cell_legend([parsed]) or "")


class TestEstimateNoteMixedAccuracy:
    """The subscription-specific mixed note needs an actual subscription contribution."""

    @pytest.mark.parametrize("other", ["unknown", "unstated"])
    def test_mixed_without_subscription_has_no_note(self, other: str) -> None:
        mixed = AggregateBilling({"metered_api": 1, other: 1})  # type: ignore[dict-item]
        assert mixed.state == "mixed"
        assert estimate_note(mixed) is None

    def test_mixed_with_subscription_keeps_the_note(self) -> None:
        mixed = AggregateBilling({"subscription": 1, "unknown": 1})
        assert mixed.state == "mixed"
        assert estimate_note(mixed) == billing.MIXED_NOTE
