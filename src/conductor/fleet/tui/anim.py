"""Animation primitives for the Fleet Manager TUI.

Everything animated in the TUI reads from a single monotonically increasing
**frame counter** rather than owning a timer of its own. One screen-level
clock ticking a counter, with each widget deriving its own appearance from
that number, keeps animations in phase with each other (every spinner in a
table turns together, which reads as one system doing one thing) and keeps
the cost to one timer no matter how many animated cells are on screen.

The functions here are **pure**: frame number in, glyph or style out. That is
what lets them be unit-tested without a running app, and what lets a screen
render a sensible *static* frame when animation is switched off -- frame 0 of
every sequence is a reasonable still image, so nothing is invisible or
half-drawn when the clock never ticks.

Animation is disabled by setting ``CONDUCTOR_FLEET_NO_ANIM`` to a non-empty
value. That exists because a TUI that repaints ten times a second is
genuinely unwelcome in some places -- over a slow SSH link, inside a
terminal multiplexer being recorded, or on battery -- and because a reader
who finds movement distracting should not have to choose between that and
the whole tool. That slow-SSH-link case is no longer left to the reader to
notice and work around: :func:`is_remote_session` detects an RDP or SSH
session and :func:`animations_enabled` turns animation off automatically
for it, with ``CONDUCTOR_FLEET_ANIM`` as the explicit force-on override.
"""

from __future__ import annotations

import os

from rich.text import Text

#: Seconds between animation frames. 10fps: fast enough that a spinner reads
#: as motion rather than as a glyph that keeps changing, slow enough that the
#: repaint cost stays invisible next to the ~2s data poll it sits alongside.
FRAME_INTERVAL = 0.1

#: The braille spinner every "this is working" indicator uses. Braille rather
#: than ASCII ``|/-\\``: it occupies one cell, turns smoothly, and does not
#: flicker between glyphs of different visual weight.
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: Styles cycled to make a glyph "breathe". Deliberately an even-length
#: palindrome so the brightness ramps up and back down smoothly instead of
#: snapping from brightest to dimmest between the last frame and the first.
_BREATH_RAMP = ("dim", "", "bold", "bold", "", "dim")

#: Frames per step of the breathing cycle. A gate badge that breathed at the
#: spinner's 10fps looked like a fault rather than an invitation.
_BREATH_DIVISOR = 4

#: The block glyphs a sparkline is drawn from, lightest to heaviest.
SPARK_GLYPHS = "▁▂▃▄▅▆▇█"


def is_remote_session() -> str | None:
    """Detect an RDP or SSH session, where a 10fps repaint is genuinely costly.

    Modelled on :func:`conductor.fleet.tui.actions._is_wsl`'s style: stdlib
    environment sniffing only, nothing here raises, and each signal is
    trusted for a documented reason rather than guessed at.

    RDP: ``SESSIONNAME`` names the session type Windows assigned this
    logon, and every Remote Desktop session's name starts with
    ``RDP-Tcp`` (e.g. ``RDP-Tcp#0``) -- the physical console session is
    named plain ``Console`` instead, which is why the match is a prefix
    check rather than "is the variable set at all".

    SSH: either ``SSH_CONNECTION`` or ``SSH_TTY`` being set is how every
    OpenSSH server has told the shell it started this session since before
    this project existed; a client-side tool has no comparably reliable
    signal to add on top.

    Returns:
        ``"RDP"`` or ``"SSH"`` when detected, otherwise ``None``.
    """
    session_name = os.environ.get("SESSIONNAME", "")
    if session_name.strip().lower().startswith("rdp-tcp"):
        return "RDP"
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return "SSH"
    return None


def animations_enabled() -> bool:
    """Return whether animation should run at all.

    An explicit precedence chain, in order:

    1. ``CONDUCTOR_FLEET_NO_ANIM`` set to a non-empty value -- always wins,
       even over a forced-on request.
    2. ``CONDUCTOR_FLEET_ANIM`` set to a non-empty value -- forces animation
       back on over a detected remote session.
    3. :func:`is_remote_session` -- off by default on a detected RDP or SSH
       session.
    4. Otherwise on.

    Returns:
        Whether animation should run.
    """
    if os.environ.get("CONDUCTOR_FLEET_NO_ANIM"):
        return False
    if os.environ.get("CONDUCTOR_FLEET_ANIM"):
        return True
    return is_remote_session() is None


def disabled_reason() -> str | None:
    """Return why animation was turned off *by detection*, if it was.

    Deliberately distinct from ``not animations_enabled()``: an explicit
    ``CONDUCTOR_FLEET_NO_ANIM`` is the reader's own choice and should not
    produce a notification explaining it back to them -- and the whole test
    suite sets that variable unconditionally (see ``conftest.py``), so a
    reason tied to it would fire on every test run rather than only the
    ones that actually exercise detection.

    Returns:
        ``"RDP"`` or ``"SSH"`` when a detected remote session is what
        disabled animation, otherwise ``None``.
    """
    if os.environ.get("CONDUCTOR_FLEET_NO_ANIM"):
        return None
    if os.environ.get("CONDUCTOR_FLEET_ANIM"):
        return None
    return is_remote_session()


def spinner(frame: int, frames: str = SPINNER_FRAMES) -> str:
    """Return the spinner glyph for ``frame``.

    Args:
        frame: The current frame counter. Negative values are tolerated.
        frames: The glyph sequence to cycle through.

    Returns:
        A single glyph from ``frames``.
    """
    if not frames:
        return ""
    return frames[frame % len(frames)]


def breath_style(frame: int, color: str = "") -> str:
    """Return a Rich style that pulses ``color`` in and out of brightness.

    Args:
        frame: The current frame counter.
        color: A Rich colour name to modulate, or ``""`` for no colour.

    Returns:
        A Rich style string such as ``"bold yellow"``.
    """
    ramp = _BREATH_RAMP[(frame // _BREATH_DIVISOR) % len(_BREATH_RAMP)]
    return f"{ramp} {color}".strip()


def sparkline(values: list[float], *, width: int = 12) -> str:
    """Render ``values`` as a fixed-width block sparkline.

    Scaled against the series' own maximum rather than an absolute one: the
    interesting signal is the *shape* of a run's token burn -- steady,
    bursty, stalled -- and a fleet-wide scale would flatten every small run
    into a straight line beside one large one.

    Args:
        values: The series, oldest first. Longer than ``width`` keeps the
            most recent ``width`` samples.
        width: How many cells to render.

    Returns:
        A string exactly ``width`` cells wide (space-padded on the right
        while the series is still filling), or ``""`` for an empty series.
    """
    if not values or width <= 0:
        return ""

    recent = values[-width:]
    peak = max(recent)
    if peak <= 0:
        # A run that has burned nothing yet is a flat floor, not a blank:
        # the absence of a sparkline reads as "no data", which is a
        # different (and wrong) statement about a run that is simply idle.
        drawn = SPARK_GLYPHS[0] * len(recent)
    else:
        top = len(SPARK_GLYPHS) - 1
        drawn = "".join(SPARK_GLYPHS[min(top, int(v / peak * top + 0.5))] for v in recent)

    # Always `width` cells, so a sparkline that gains a sample per poll does
    # not re-widen its column and shove the numbers beside it sideways --
    # and left-aligned, so a new run's first samples start at the column's
    # left edge and grow rightward instead of hanging off the far edge with
    # a gap in front of them.
    return drawn.ljust(width)


def marquee(text: str, frame: int, *, width: int, gap: str = "   ·   ") -> str:
    """Scroll ``text`` horizontally within ``width`` cells.

    Returns ``text`` unchanged when it already fits, so a short line does not
    jitter for no reason -- movement should mean "there is more to see".

    Args:
        text: The full string.
        frame: The current frame counter.
        width: The visible width in cells.
        gap: Separator shown between the end of the text and its wrap-around.

    Returns:
        The ``width``-cell window of the scrolling text.
    """
    if width <= 0:
        return ""
    if len(text) <= width:
        return text

    loop = text + gap
    # Halved: at the spinner's frame rate a marquee scrolls too fast to read.
    offset = (frame // 2) % len(loop)
    doubled = loop + loop
    return doubled[offset : offset + width]


def progress_bar(done: int, total: int, *, width: int = 16) -> Text:
    """Render a compact ``done``/``total`` meter.

    Args:
        done: Completed units.
        total: Total units. Zero or fewer yields an empty bar.
        width: Bar width in cells.

    Returns:
        A Rich :class:`Text` with the filled portion styled and the remainder
        dim.
    """
    out = Text()
    if total <= 0 or width <= 0:
        return out

    filled = max(0, min(width, round(width * done / total)))
    out.append("━" * filled, style="green")
    out.append("━" * (width - filled), style="dim")
    return out
