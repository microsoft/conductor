"""Billing provenance: how an execution's model usage is paid for, and how to say so honestly.

A cost figure in Conductor is always an *estimate* computed from token counts and a price
table. This module records where the underlying usage was billed (a metered API key, a
subscription login, or unproven) so renderers can label the figure instead of implying it
is an invoice. It never holds a credential, an account identity, or free text: every value
is one of a closed set of strings, and every label is a constant defined here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal, cast

BillingMode = Literal["metered_api", "subscription", "unknown"]
"""Provenance of ONE execution. Never ``"mixed"``; that is an aggregate-only state."""

BillingClass = Literal["metered_api", "subscription", "unknown", "unstated"]
"""What one contributing execution counts as. ``"unstated"`` is a provider that reports nothing."""

AggregateBillingState = Literal["metered_api", "subscription", "unknown", "mixed"]

BILLING_MODES: Final[tuple[BillingMode, ...]] = ("metered_api", "subscription", "unknown")
_DISPLAY_ORDER: Final[tuple[BillingClass, ...]] = (
    "subscription",
    "metered_api",
    "unknown",
    "unstated",
)
_MODE_BY_NAME: Final[dict[str, BillingMode]] = {mode: mode for mode in BILLING_MODES}
_CLASS_BY_NAME: Final[dict[str, BillingClass]] = {cls: cls for cls in _DISPLAY_ORDER}
_CLASS_NOUN: Final[dict[BillingClass, str]] = {
    "subscription": "subscription",
    "metered_api": "metered API",
    "unknown": "unknown",
    "unstated": "not reported",
}

SUBSCRIPTION_LABEL: Final = "API-equivalent estimate"
UNKNOWN_LABEL: Final = "billing source unknown"
MIXED_LABEL: Final = "mixed billing"
SUBSCRIPTION_NOTE: Final = (
    "Estimated at API rates from token counts; not an invoice or an additional charge."
)
MIXED_NOTE: Final = (
    "Includes subscription executions estimated at API rates; "
    "not an invoice or an additional charge."
)

# Fixed-width table cells (Fleet TUI). At most five characters, so a label adds at most six
# columns (one space plus the label). Each expands to a long label in a detail view.
CELL_SUBSCRIPTION: Final = "est."
CELL_UNKNOWN: Final = "src?"
CELL_MIXED: Final = "mixed"
_CELL_EXPANSION: Final[dict[str, str]] = {
    CELL_SUBSCRIPTION: SUBSCRIPTION_LABEL,
    CELL_UNKNOWN: UNKNOWN_LABEL,
    CELL_MIXED: MIXED_LABEL,
}
LEGEND_NOTE: Final = (
    "Estimates are computed at API rates from token counts; not an invoice or an additional charge."
)


def coerce_billing_mode(value: object) -> BillingMode | None:
    """Normalize any value read from a provider, event, or log into a per-execution mode.

    ``None`` and non-strings give ``None`` (not stated); a recognized string is returned as
    is; any other string gives ``"unknown"`` so a value this version does not understand is
    surfaced as unproven rather than dropped or displayed.
    """
    if isinstance(value, str):
        return _MODE_BY_NAME.get(value, "unknown")
    return None


def count_mode(counts: dict[BillingClass, int], mode: BillingMode | None) -> None:
    """Add one execution to a running breakdown; ``None`` (not stated) counts as ``unstated``.

    The one place a mode becomes a class, shared by :meth:`AggregateBilling.from_modes` and
    every incremental scanner, so no consumer can skip the ``None`` case on its own.
    """
    key: BillingClass = "unstated" if mode is None else mode
    counts[key] = counts.get(key, 0) + 1


@dataclass(frozen=True, slots=True)
class AggregateBilling:
    """Provenance of the executions behind one displayed dollar figure.

    ``breakdown`` maps each contributing class to an execution count and holds **only
    positive counts**. There is no dominant mode and no tie-break: :attr:`state` is derived
    from which classes are present, so it cannot disagree with the counts.
    """

    breakdown: Mapping[BillingClass, int]

    def __post_init__(self) -> None:
        unexpected = set(self.breakdown) - set(_DISPLAY_ORDER)
        if unexpected:
            raise ValueError(f"unrecognized billing classes: {sorted(unexpected)}")
        cleaned: dict[BillingClass, int] = {}
        for cls in _DISPLAY_ORDER:  # fixed order keeps equality and repr deterministic
            count = self.breakdown.get(cls, 0)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"billing count for {cls!r} must be a non-negative int")
            if count:
                cleaned[cls] = count
        object.__setattr__(self, "breakdown", MappingProxyType(cleaned))

    @property
    def total(self) -> int:
        return sum(self.breakdown.values())

    @property
    def stated(self) -> bool:
        """True when at least one contributing execution reported provenance."""
        return any(cls != "unstated" for cls in self.breakdown)

    @property
    def state(self) -> AggregateBillingState:
        present = set(self.breakdown)
        if not present:
            return "unknown"  # no relevant executions
        if present == {"metered_api"}:
            return "metered_api"
        if present == {"subscription"}:
            return "subscription"
        if present <= {"unknown", "unstated"}:
            return "unknown"
        return "mixed"  # >1 class, or a known mode alongside unknown/unstated

    @classmethod
    def from_modes(cls, modes: Iterable[BillingMode | None]) -> AggregateBilling:
        counts: dict[BillingClass, int] = {}
        for mode in modes:
            count_mode(counts, mode)
        return cls(counts)

    @classmethod
    def from_counts(cls, counts: Mapping[BillingClass, int]) -> AggregateBilling | None:
        """Build from a running breakdown; ``None`` when nothing contributed at all."""
        result = cls(counts)
        return result if result.breakdown else None

    @classmethod
    def combine(cls, items: Iterable[AggregateBilling | None]) -> AggregateBilling | None:
        """Sum breakdowns (e.g. across runs); ``None`` inputs are skipped, empty gives ``None``."""
        totals: dict[BillingClass, int] = {}
        for item in items:
            if item is None:
                continue
            for klass, count in item.breakdown.items():
                totals[klass] = totals.get(klass, 0) + count
        return cls.from_counts(totals)

    def to_wire(self) -> dict[str, Any] | None:
        """JSON-safe form, or ``None`` when no execution stated provenance (no label)."""
        if not self.stated:
            return None
        return {"state": self.state, "breakdown": dict(self.breakdown)}

    @classmethod
    def from_wire(cls, value: object, *, strict: bool = False) -> AggregateBilling | None:
        """Parse :meth:`to_wire` output.

        ``state`` is ignored (recomputed from the breakdown) other than a strict type check.
        Unrecognized breakdown keys are merged into ``unknown``. Non-strict callers (events,
        JSON consumers) treat malformed input as "not stated"; ``strict=True`` (the terminal
        record) raises ``ValueError`` for a wrong-typed present value.
        """
        if value is None:
            return None
        if not isinstance(value, Mapping):
            if strict:
                raise ValueError("billing must be an object or null")
            return None
        # ``isinstance`` narrows an ``object`` to ``Mapping[Never, ...]``; the keys are
        # checked individually below, so state the real shape once here.
        wire = cast("Mapping[str, Any]", value)
        raw = wire.get("breakdown")
        if not isinstance(raw, Mapping):
            if strict:
                raise ValueError("billing.breakdown must be an object")
            return None
        state = wire.get("state")
        if strict and state is not None and not isinstance(state, str):
            raise ValueError("billing.state must be a string")
        counts: dict[BillingClass, int] = {}
        for key, count in raw.items():
            if not isinstance(key, str) or isinstance(count, bool) or not isinstance(count, int):
                if strict:
                    raise ValueError("billing.breakdown must map strings to integers")
                continue
            if count < 0:
                if strict:
                    raise ValueError("billing.breakdown counts must be non-negative")
                continue
            klass = _CLASS_BY_NAME.get(key, "unknown")
            counts[klass] = counts.get(klass, 0) + count
        result = cls(counts)
        return result if result.stated else None


def mode_label(mode: BillingMode | None) -> str | None:
    """Label for one execution's figure, or ``None`` when it needs none."""
    if mode == "subscription":
        return SUBSCRIPTION_LABEL
    if mode == "unknown":
        return UNKNOWN_LABEL
    return None  # metered_api keeps today's presentation; None is not stated


def aggregate_label(billing: AggregateBilling | None, *, detailed: bool = False) -> str | None:
    """Label for a displayed total, or ``None`` when the figure needs none.

    ``detailed=True`` adds the breakdown to the mixed label (console summary); table cells
    and status bars use the short form and expose the breakdown elsewhere.
    """
    if billing is None or not billing.stated:
        return None
    state = billing.state
    if state == "subscription":
        return SUBSCRIPTION_LABEL
    if state == "unknown":
        return UNKNOWN_LABEL
    if state == "mixed":
        if not detailed:
            return MIXED_LABEL
        parts = ", ".join(
            f"{billing.breakdown[cls]} {_CLASS_NOUN[cls]}"
            for cls in _DISPLAY_ORDER
            if cls in billing.breakdown
        )
        return f"{MIXED_LABEL}: {parts}"
    return None  # metered_api


def estimate_note(billing: AggregateBilling | None) -> str | None:
    """Explanatory sentence for a total that contains API-equivalent estimates."""
    if billing is None or not billing.stated:
        return None
    if billing.state == "subscription":
        return SUBSCRIPTION_NOTE
    if billing.state == "mixed":
        return MIXED_NOTE
    return None


def cell_label(billing: AggregateBilling | None) -> str | None:
    """Compact label for a fixed-width table cell, or ``None`` when the figure needs none."""
    if billing is None or not billing.stated:
        return None
    state = billing.state
    if state == "subscription":
        return CELL_SUBSCRIPTION
    if state == "unknown":
        return CELL_UNKNOWN
    if state == "mixed":
        return CELL_MIXED
    return None  # metered_api


def cell_legend(items: Iterable[AggregateBilling | None]) -> str | None:
    """One line expanding every compact label present in ``items``; ``None`` if there is none."""
    present = {cell_label(item) for item in items}
    parts = [f"{cell} = {text}" for cell, text in _CELL_EXPANSION.items() if cell in present]
    if not parts:
        return None
    legend = "; ".join(parts)
    if CELL_SUBSCRIPTION in present or CELL_MIXED in present:
        legend += ". " + LEGEND_NOTE
    return legend
