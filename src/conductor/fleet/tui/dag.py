"""Renders a run's topology as a flowing score of status chips.

The Runs preview and the run-detail header both need to answer *where is
this run in its workflow*. The first version answered it with a single line
of ``a → b → c  +11 more``, which is accurate and almost unreadable: every
step has identical visual weight, so finding the current one means reading
the whole line, and a truncated tail hides exactly the part that has not
happened yet.

This module renders the same data as **chips** -- one per step, carrying its
own status glyph and colour, flowed across as many lines as the available
width needs. Completed work recedes, pending work is dim, and the step the
run is actually on is the only thing that moves. That inversion is the whole
point: on a screen refreshed every couple of seconds, motion is the strongest
signal available, so it is spent on the one step that matters rather than
on decoration.

Pure rendering. Nothing here reads a log or knows what a run record is -- it
takes a topology, a status map and a frame number, and returns
:class:`~rich.text.Text`.
"""

from __future__ import annotations

from rich.text import Text

from conductor.fleet.summary import RunTopology
from conductor.fleet.tui.anim import breath_style, spinner

#: Glyphs for a step's position in the run, keyed by the status vocabulary
#: `summary.py` derives. Deliberately *not* reusing `theme.STATUS_STYLES`
#: wholesale: these are steps inside one run rather than runs inside a
#: fleet, and "pending" (a step that has not started) has no fleet-level
#: equivalent at all.
_STEP_GLYPHS: dict[str, str] = {
    "completed": "✓",
    "failed": "✗",
    "running": "•",
    "at-gate": "▲",
    "pending": "·",
}

_STEP_COLORS: dict[str, str] = {
    "completed": "green",
    "failed": "red",
    "running": "bright_cyan",
    "at-gate": "yellow",
    "pending": "",
}

#: The connector drawn between chips on the same line.
_LINK = " ─ "

#: Drawn instead of a connector when the flow wraps to the next line.
_WRAP = " ↘"


def _chip(name: str, status: str, frame: int, *, animate: bool) -> Text:
    """Render one step as a glyph + name, styled for its status."""
    color = _STEP_COLORS.get(status, "")

    if status == "running" and animate:
        # The one moving thing on the screen. A spinner rather than a static
        # dot because "running" and "pending" are otherwise distinguishable
        # only by colour, which is exactly what a colour-blind reader or a
        # low-contrast terminal takes away.
        glyph = spinner(frame)
        style = f"bold {color}".strip()
    elif status == "at-gate" and animate:
        glyph = _STEP_GLYPHS[status]
        style = breath_style(frame, color)
    else:
        glyph = _STEP_GLYPHS.get(status, "·")
        style = f"bold {color}".strip() if status in ("running", "at-gate") else color

    chip = Text()
    chip.append(f"{glyph} ", style=style or "dim")
    # Completed steps are dimmed rather than coloured: a workflow that is
    # going well would otherwise end as a wall of green with the live step
    # lost inside it.
    chip.append(name, style=style if status in ("running", "at-gate", "failed") else "dim")
    return chip


def render_score(
    topology: RunTopology,
    statuses: dict[str, str],
    *,
    width: int,
    frame: int = 0,
    animate: bool = True,
    max_lines: int | None = None,
) -> Text:
    """Render ``topology`` as chips flowed to ``width`` cells.

    Args:
        topology: The run's steps, in declared order.
        statuses: Step name to status. A step missing from the map is
            treated as ``"pending"``.
        width: Available width in cells.
        frame: Animation frame counter.
        animate: Whether the live step should move.
        max_lines: Stop after this many lines, appending a ``+N more``
            marker. ``None`` renders every step.

    Returns:
        The flowed score as :class:`~rich.text.Text`.
    """
    out = Text()
    if not topology.agents or width <= 0:
        return out

    # The wrap arrow is drawn *after* the last chip on a line, so a line may
    # only fill to `width` minus its width -- without reserving it, every
    # wrapped line overflowed by two cells and the terminal re-wrapped it,
    # producing a stray line that looked like a rendering fault.
    usable = max(1, width - len(_WRAP))

    line_width = 0
    lines_used = 1
    for index, agent in enumerate(topology.agents):
        status = statuses.get(agent.name, "pending")
        chip = _chip(agent.name, status, frame, animate=animate)
        chip_width = chip.cell_len
        needed = chip_width + (len(_LINK) if line_width else 0)

        if line_width and line_width + needed > usable:
            if max_lines is not None and lines_used >= max_lines:
                _append_more(out, len(topology.agents) - index, line_width, width)
                return out
            out.append(_WRAP, style="dim")
            out.append("\n")
            lines_used += 1
            line_width = 0
        elif line_width:
            out.append(_LINK, style="dim")
            line_width += len(_LINK)

        out.append_text(chip)
        line_width += chip_width

    return out


def _append_more(out: Text, remaining: int, line_width: int, width: int) -> None:
    """Append the ``+N more`` marker, wrapping it rather than overflowing."""
    if remaining <= 0:
        return
    marker = f"  +{remaining} more"
    if line_width + len(marker) > width:
        out.append("\n")
    out.append(marker, style="dim")


def step_statuses(agent_names: list[str], current_step: str | None) -> dict[str, str]:
    """Infer per-step statuses from position alone.

    The Runs screen's streamed, prefiltered scan knows the current step
    but not the per-step history the run-detail screen derives from its
    own streamed, unfiltered scan of the same log. Position is a sound
    stand-in *for a linear run*: everything before the current step has
    been passed through, everything after has not.

    It is deliberately only a stand-in. A workflow that loops back, or one
    whose current step is unknown, gets no invented history -- callers with
    real per-step statuses should pass those instead.

    Args:
        agent_names: Step names in declared order.
        current_step: The step the run is on, if known.

    Returns:
        A status map suitable for :func:`render_score`.
    """
    if current_step is None or current_step not in agent_names:
        return {}

    cursor = agent_names.index(current_step)
    statuses = dict.fromkeys(agent_names[:cursor], "completed")
    statuses[current_step] = "running"
    return statuses
