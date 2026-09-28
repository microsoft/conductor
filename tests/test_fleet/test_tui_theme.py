"""Tests for the TUI's shared visual vocabulary (``fleet/tui/theme.py``).

The point of this module is that Runs, History and run-detail render the
*same* status the same way -- before it, each screen carried its own glyph
map and they had already drifted apart. These tests pin that single source
of truth rather than any particular glyph.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.text import Text

from conductor.billing import AggregateBilling
from conductor.fleet.tui.theme import (
    EMPTY,
    STATUS_STYLES,
    empty_cell,
    format_cost_text,
    mode_label,
    muted,
    shorten_home,
    status_badge,
    status_label,
    status_style,
)


class TestStatusVocabulary:
    def test_covers_every_run_status(self) -> None:
        """Every status `summary.py` can derive must be styleable, or a real
        run renders as a blank cell on the screen that shows it."""
        from conductor.fleet.summary import RunStatus

        for status in RunStatus.__args__:  # type: ignore[attr-defined]
            assert status in STATUS_STYLES, f"no style for run status {status!r}"

    def test_covers_history_unknown(self) -> None:
        """History classifies a log with no terminal event as ``unknown`` --
        deliberately not ``running`` -- so it needs its own style."""
        assert "unknown" in STATUS_STYLES

    def test_unrecognised_status_does_not_raise(self) -> None:
        """A future status reaching an un-updated screen must render, not
        take down the poll loop that drew it."""
        style = status_style("some-new-status")
        assert style.badge == " "
        assert status_label("some-new-status").plain == "some-new-status"

    def test_notable_statuses_carry_a_colour(self) -> None:
        """Colour marks the runs worth reacting to. `unknown` is excluded on
        purpose (see below), so this asserts the rule rather than "every
        entry has a colour" -- which would forbid that choice."""
        for status in ("running", "at-gate", "paused", "completed", "failed"):
            assert STATUS_STYLES[status].color, f"{status} has no colour"

    def test_unknown_is_unstyled_so_a_wall_of_it_stays_readable(self) -> None:
        """History's most common row. Dark grey on a dark background made it
        near-invisible; plain foreground recedes behind the coloured rows
        without disappearing."""
        assert STATUS_STYLES["unknown"].color == ""

    def test_failed_and_unknown_are_visually_distinct(self) -> None:
        assert STATUS_STYLES["failed"].color != STATUS_STYLES["unknown"].color


class TestRenderers:
    def test_badge_is_coloured_text(self) -> None:
        badge = status_badge("running")
        assert isinstance(badge, Text)
        assert badge.plain == STATUS_STYLES["running"].badge
        assert badge.style == STATUS_STYLES["running"].color

    def test_label_pairs_badge_and_word(self) -> None:
        label = status_label("completed")
        assert label.plain == "✓ completed"

    def test_mode_label_distinguishes_the_three_modes(self) -> None:
        """A foreground run used to be identifiable only by a blank port,
        which reads identically to "no data yet"."""
        rendered = {mode_label(m).plain for m in ("fg", "fg-web", "bg")}
        assert len(rendered) == 3

    def test_unknown_mode_falls_back_to_itself(self) -> None:
        assert mode_label("weird").plain == "weird"

    def test_placeholders_are_dim(self) -> None:
        assert empty_cell().plain == EMPTY
        assert empty_cell().style == "dim"
        assert muted("x").style == "dim"

    def test_renderers_return_text_not_markup_strings(self) -> None:
        """Returning ``Text`` (not a markup string) is what keeps a run whose
        name contains ``[/red]`` from being mangled or raising -- the
        codebase-wide rule these screens have to honour."""
        for value in (status_badge("failed"), status_label("failed"), mode_label("bg"), muted("x")):
            assert isinstance(value, Text)


class TestShortenHome:
    """Recommendation 9 (issue #477 review): ``shorten_home`` must compare
    by path component, not a bare string prefix -- a string-prefix test
    mangles a sibling home directory that merely shares a prefix (e.g.
    ``/home/jasonx`` under ``HOME=/home/jason``) into ``~x``.

    Patches ``Path.home`` rather than the ``HOME`` env var: that is the
    exact call ``shorten_home`` makes, and ``HOME`` is inert on Windows
    (``ntpath.expanduser`` reads ``USERPROFILE``/``HOMEDRIVE``+``HOMEPATH``
    instead), which made these tests pass locally while failing on Windows
    CI (issue #486). Expectations are built via ``str(Path(...))`` rather
    than hard-coded ``/``-separated literals for the same reason -- a
    literal ``"~/src/proj"`` is not what ``Path("~", "src", "proj")``
    renders to on Windows.
    """

    def test_path_under_home_is_shortened(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: Path("/home/jason"))
        assert shorten_home("/home/jason/src/proj") == str(Path("~", "src", "proj"))

    def test_path_outside_home_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: Path("/home/jason"))
        assert shorten_home("/tmp/a") == str(Path("/tmp/a"))

    def test_sibling_directory_sharing_home_prefix_is_not_mangled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: Path("/home/jason"))
        assert shorten_home("/home/jasonx/proj") == str(Path("/home/jasonx/proj"))


def _previous_format_cost(total: float | None, unpriced: int) -> str:
    """The formatter that existed before billing provenance (the oracle for byte-identity)."""
    if total is None:
        if unpriced > 0:
            return f"({unpriced} unpriced)"
        return "\u2014"
    if unpriced > 0:
        return f"~${total:.2f} ({unpriced} unpriced)"
    return f"~${total:.2f}"


_SUB = AggregateBilling({"subscription": 2})
_UNK = AggregateBilling({"unknown": 1})
_MIXED = AggregateBilling({"subscription": 1, "metered_api": 1})
_METERED = AggregateBilling({"metered_api": 2})
_UNSTATED = AggregateBilling({"unstated": 3})


class TestFormatCostText:
    """``format_cost_text`` is the one Fleet cost formatter (issue #265's ``~$X (N unpriced)``
    convention, plus the compact billing label): every Fleet screen that renders a cost cell
    calls this function rather than formatting a total itself."""

    @pytest.mark.parametrize(
        ("total", "unpriced", "billing", "expected"),
        [
            # no contributing event / legacy / metered: no label, ever
            (None, 0, None, EMPTY),
            (0.0, 0, None, "~$0.00"),
            (0.42, 0, None, "~$0.42"),
            (0.42, 0, _UNSTATED, "~$0.42"),
            (0.42, 0, _METERED, "~$0.42"),
            (0.42, 2, _UNSTATED, "~$0.42 (2 unpriced)"),
            (0.42, 2, _METERED, "~$0.42 (2 unpriced)"),
            # labelled, with and without unpriced executions
            (0.42, 0, _SUB, "~$0.42 est."),
            (0.42, 2, _SUB, "~$0.42 est. (2 unpriced)"),
            (0.42, 0, _UNK, "~$0.42 src?"),
            (0.42, 2, _UNK, "~$0.42 src? (2 unpriced)"),
            (0.42, 0, _MIXED, "~$0.42 mixed"),
            (0.42, 2, _MIXED, "~$0.42 mixed (2 unpriced)"),
            # no dollar figure: never a label
            (None, 2, _SUB, "(2 unpriced)"),
            (None, 2, _UNK, "(2 unpriced)"),
            (None, 0, _MIXED, EMPTY),
        ],
    )
    def test_format_cost_text_table(
        self, total: float | None, unpriced: int, billing: AggregateBilling | None, expected: str
    ) -> None:
        assert format_cost_text(total, unpriced, billing) == expected

    @pytest.mark.parametrize("billing", [None, _UNSTATED, _METERED, AggregateBilling({})])
    def test_unlabelled_output_is_byte_identical_to_the_previous_formatter(
        self, billing: AggregateBilling | None
    ) -> None:
        totals = [None, 0.0, 0.004, 0.005, 0.0149, 0.42, 1.0, 12.345, 1234.5]
        for total in totals:
            for unpriced in (0, 1, 2, 7):
                assert format_cost_text(total, unpriced, billing) == _previous_format_cost(
                    total, unpriced
                ), (total, unpriced)

    def test_label_precedes_the_unpriced_suffix(self) -> None:
        text = format_cost_text(0.42, 3, _SUB)
        assert text.index("est.") < text.index("(3 unpriced)")

    @pytest.mark.parametrize("billing", [_SUB, _UNK, _MIXED])
    def test_label_never_added_without_a_dollar_figure(self, billing: AggregateBilling) -> None:
        for unpriced in (0, 1, 5):
            text = format_cost_text(None, unpriced, billing)
            assert text == _previous_format_cost(None, unpriced)
            assert not any(label in text for label in ("est.", "src?", "mixed"))

    @pytest.mark.parametrize("billing", [_SUB, _UNK, _MIXED])
    def test_labelled_cell_grows_by_at_most_six_characters(self, billing: AggregateBilling) -> None:
        for unpriced in (0, 2):
            labelled = format_cost_text(12.345, unpriced, billing)
            plain = format_cost_text(12.345, unpriced, None)
            assert 0 < len(labelled) - len(plain) <= 6
