"""Shared visual vocabulary for the Fleet Manager TUI.

Before this module every screen invented its own status rendering: ``runs.py``
had ``_STATUS_BADGES`` (glyph only), ``history.py`` had ``_OUTCOME_LABELS``
(glyph + word), and ``run_detail.py`` had ``_STATUS_LABELS`` (a third,
overlapping set). None of them carried colour, so "failed" and "unknown"
landed on screen with identical visual weight and a reader had to actually
parse the glyph to tell a healthy fleet from a broken one.

Everything here is a *presentation* concern only. The status values
themselves are derived in :mod:`conductor.fleet.summary` and
:mod:`conductor.fleet.history`, which stay free of any rendering opinion --
this module maps those values onto badges and theme colour keys, and nothing
else imports it but the TUI screens.

Colours are **Rich style names** (``green``, ``red``, …), not Textual CSS
variables: these render inside ``Text`` objects handed to widgets, and Rich
resolves them against the terminal's own ANSI palette. Textual's ``$success``
/ ``$error`` variables are a CSS-layer feature and would be printed literally
here. Naming ANSI colours keeps them legible across themes *and* across the
user's terminal palette, which a hard-coded hex would not.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text


@dataclass(frozen=True, slots=True)
class StatusStyle:
    """How one status value is rendered: a badge glyph, a word, and a colour."""

    badge: str
    """Single-width glyph shown in a table's primary cell."""

    label: str
    """Human-readable word, used where there is room for one."""

    color: str
    """A Rich style name (see the module docstring). Empty means "no styling"
    -- the deliberate choice for a status that is unremarkable, so colour
    stays meaningful rather than becoming decoration."""


# The design's mockup legend is "▲ = at human gate  ● = running"; the rest of
# the vocabulary (`paused`/`completed`/`failed`) is rare on the Runs screen --
# every record there already passed a liveness check -- but is styled anyway
# so a run caught in the narrow race `summary.py` documents (terminal event
# written, process not yet exited) still renders sensibly.
STATUS_STYLES: dict[str, StatusStyle] = {
    "running": StatusStyle("●", "running", "green"),
    "at-gate": StatusStyle("▲", "at gate", "yellow"),
    "paused": StatusStyle("⏸", "paused", "cyan"),
    "completed": StatusStyle("✓", "completed", "green"),
    "failed": StatusStyle("✗", "failed", "red"),
    # History-only: a log with no terminal event at all. Deliberately not
    # "running" -- `history.py` refuses to infer liveness from a log.
    #
    # Left *unstyled* rather than dimmed: it is the History screen's most
    # common row by far, and `bright_black` rendered it as near-invisible
    # dark-grey-on-dark-grey. Plain foreground keeps a wall of them readable
    # while still receding behind the runs that did finish, since those are
    # the only ones carrying colour.
    "unknown": StatusStyle("?", "unknown", ""),
}

_FALLBACK = StatusStyle(" ", "", "")


def status_style(status: str) -> StatusStyle:
    """Return the :class:`StatusStyle` for ``status``, or a blank fallback.

    Never raises for an unrecognised value: a future status reaching an
    un-updated screen should render as an unstyled word, not crash the poll
    loop that drew it.
    """
    return STATUS_STYLES.get(status, _FALLBACK)


def status_badge(status: str) -> Text:
    """Render just the badge glyph for ``status``, coloured when it has one."""
    style = status_style(status)
    return Text(style.badge, style=style.color or "")


def status_label(status: str) -> Text:
    """Render ``status`` as a coloured badge + word (``✓ completed``).

    Used where a column exists to carry the status itself (History's
    Outcome, the run-detail agent table) rather than to prefix something
    else, as :func:`status_badge` does on the Runs screen.
    """
    style = status_style(status)
    if not style.label:
        return Text(status)
    return Text(f"{style.badge} {style.label}", style=style.color or "")


# Run modes, badged so a portless foreground run is identifiable as such
# rather than by the absence of a port -- an absence that reads identically
# to "this column has no data yet", which is exactly the ambiguity the
# dash-heavy first draft of these tables suffered from.
_MODE_LABELS: dict[str, str] = {
    "fg": "fg",
    "fg-web": "fg+web",
    "bg": "bg",
}


def mode_label(mode: str) -> Text:
    """Render a run's mode (``fg`` / ``fg+web`` / ``bg``) as dim text."""
    return Text(_MODE_LABELS.get(mode, mode), style="dim")


def muted(text: str) -> Text:
    """Render secondary text (a placeholder, a unit, an aside) as dim."""
    return Text(text, style="dim")


#: The placeholder for "no value here". Centralised so the em dash is not
#: retyped at a dozen call sites, and so it is dim everywhere -- a screen
#: full of full-brightness dashes competes with the data for attention.
EMPTY = "—"


def empty_cell() -> Text:
    """Render the standard dim placeholder for a cell with nothing to show."""
    return Text(EMPTY, style="dim")
