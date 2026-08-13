"""ASCII art for the Fleet Manager TUI.

Kept in its own module rather than inline in the screens that draw it: art is
wide, whitespace-significant, and impossible to edit safely when it is
indented inside a class body. Every asset here is a plain string constant
plus a function that renders it as a Rich :class:`~rich.text.Text`, so the
screens stay about layout and the art stays editable.

Two constraints shape all of it. Every asset is **ASCII-only for its
structure** (box-drawing and block glyphs are used for texture, but nothing
here depends on an emoji or a font ligature rendering correctly), and every
asset declares its own width so a screen can decide whether it fits before
drawing it -- art that wraps is worse than no art at all.
"""

from __future__ import annotations

from rich.text import Text

# The wordmark. Block capitals rather than a font-rendered banner: this has
# to survive being copied through a terminal, an SSH session and a bug
# report, and it is the one thing on screen that says what this program is.
WORDMARK = r"""
 ██████╗ ██████╗ ███╗   ██╗██████╗ ██╗   ██╗ ██████╗████████╗ ██████╗ ██████╗
██╔════╝██╔═══██╗████╗  ██║██╔══██╗██║   ██║██╔════╝╚══██╔══╝██╔═══██╗██╔══██╗
██║     ██║   ██║██╔██╗ ██║██║  ██║██║   ██║██║        ██║   ██║   ██║██████╔╝
██║     ██║   ██║██║╚██╗██║██║  ██║██║   ██║██║        ██║   ██║   ██║██╔══██╗
╚██████╗╚██████╔╝██║ ╚████║██████╔╝╚██████╔╝╚██████╗   ██║   ╚██████╔╝██║  ██║
 ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝╚═════╝  ╚═════╝  ╚═════╝   ╚═╝    ╚═════╝ ╚═╝  ╚═╝
""".strip("\n")

#: Width of :data:`WORDMARK` in cells, so a caller can fall back before it
#: wraps. Measured rather than hard-coded so editing the art cannot make this
#: constant quietly wrong.
WORDMARK_WIDTH = max(len(line) for line in WORDMARK.splitlines())

#: A narrow wordmark for terminals too small for the full block capitals.
WORDMARK_SMALL = r"""
┌─┐┌─┐┌┐┌┌┬┐┬ ┬┌─┐┌┬┐┌─┐┬─┐
│  │ │││││││ ││││   │ │ │├┬┘
└─┘└─┘┘└┘─┴┘└─┘└─┘  ┴ └─┘┴└─
""".strip("\n")

WORDMARK_SMALL_WIDTH = max(len(line) for line in WORDMARK_SMALL.splitlines())

# The baton, drawn once beneath the wordmark on the splash. The conductor
# metaphor is the one this program is named for and it is otherwise entirely
# absent from the interface.
BATON = "╾───────────────────────────────·"

#: Shown on the Runs screen when no runs are in flight. The empty state is
#: the first thing a new user sees -- an empty table with a one-line
#: apology is a worse introduction than a deliberate one.
EMPTY_STAGE = r"""
        .  *  .        .  *   .      *   .
     *     ___________________________     .
        . /                         / .  *
         /   n o   r u n s   y e t /
    *   /_________________________/     .
       .        |         |         *
                |         |
   ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

EMPTY_STAGE_WIDTH = max(len(line) for line in EMPTY_STAGE.splitlines())

#: Shown on the History screen when nothing has been retained yet.
EMPTY_ARCHIVE = r"""
     ┌───┐ ┌───┐ ┌───┐
     │   │ │   │ │   │
     │   │ │   │ │   │      the archive is empty
     └───┘ └───┘ └───┘
    ═══════════════════
"""


def wordmark(width: int) -> Text:
    """Return the widest wordmark that fits in ``width`` cells.

    Args:
        width: Available width in cells.

    Returns:
        The block-capital wordmark, the compact one, or a plain
        ``CONDUCTOR`` when even that will not fit.
    """
    if width >= WORDMARK_WIDTH:
        return Text(WORDMARK)
    if width >= WORDMARK_SMALL_WIDTH:
        return Text(WORDMARK_SMALL)
    return Text("CONDUCTOR", style="bold")


def gradient(art: str, colors: list[str]) -> Text:
    """Colour ``art`` line by line, stepping through ``colors``.

    A vertical ramp is what makes block-capital art read as a designed
    wordmark rather than as a wall of one colour, and doing it per line
    (rather than per character) keeps the glyph shapes legible.

    Args:
        art: Multi-line art.
        colors: Rich colour names, top line first. Fewer colours than lines
            stretches the ramp across the available lines.

    Returns:
        The art as styled :class:`~rich.text.Text`.
    """
    lines = art.splitlines()
    out = Text()
    if not colors:
        return Text(art)

    for index, line in enumerate(lines):
        if index:
            out.append("\n")
        # Stretch rather than cycle: cycling a short ramp over tall art
        # produces stripes, which fights the shapes rather than shading them.
        color = colors[min(len(colors) - 1, index * len(colors) // max(1, len(lines)))]
        out.append(line, style=color)
    return out


#: The splash's colour ramp: a warm top fading to a cool base.
SPLASH_RAMP = ["bright_cyan", "cyan", "blue", "blue", "bright_black"]
