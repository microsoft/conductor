"""One step's input, output and activity (the step drill-down).

Reached with ``enter`` on a row of the run-detail screen. Answers the
question neither table above it can: *what did this step actually do*. The
Runs screen shows a run's state, run-detail shows each step's status and
usage -- this shows the prompt that went in, the structured output that came
out, and, while a step is still running (when there is no output yet), what
it has been doing.

Input and output sit in their own independently scrollable panes, side by
side while the terminal is wide enough and stacked when it is not, so a long
prompt can be read without pushing the output off the bottom. Structured
output is pretty-printed and syntax-highlighted; a step that has not
produced any yet shows its activity stream in that pane instead.

Content is read once when the screen opens rather than on a timer: a step
that has completed cannot change, and a running one is re-read by ``r``. The
poll loops elsewhere in the TUI exist to keep a *list* fresh; a drill-down
that reloaded underneath the reader would move the text they were mid-way
through.
"""

from __future__ import annotations

import asyncio
import json
import logging

from rich.console import RenderableType
from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from conductor.console import styled
from conductor.fleet.records import RunRecord
from conductor.fleet.summary import StepDetail, derive_step_detail
from conductor.fleet.tui.theme import loading_text, muted, status_label

logger = logging.getLogger(__name__)

_ACTIVITY_STYLES: dict[str, str] = {
    "message": "",
    "reasoning": "dim italic",
    "tool": "cyan",
    "tool_result": "dim",
}


def _format_output(output: object) -> RenderableType:
    """Render a step's structured output readably.

    Pretty-printed and syntax-highlighted when it is JSON-serialisable (the
    common case -- a declared ``output:`` schema produces a dict), falling
    back to ``repr`` so an unexpected shape is still shown rather than
    swallowed.

    A lone string is unwrapped rather than quoted: a single-field schema is
    frequently just prose, and rendering it as a one-line JSON string
    scrolls sideways forever instead of wrapping.

    Args:
        output: The step's recorded output value.

    Returns:
        A Rich renderable ready to hand to a ``Static``.
    """
    if isinstance(output, str):
        return Text(output)
    try:
        rendered = json.dumps(output, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return Text(repr(output))
    # `word_wrap` matters more than usual here: output values routinely
    # contain long single-line prose, which would otherwise need horizontal
    # scrolling inside an already-scrolling pane.
    return Syntax(rendered, "json", theme="ansi_dark", background_color="default", word_wrap=True)


def _activity_text(detail: StepDetail) -> Text:
    """Render the activity stream, one line per entry."""
    out = Text()
    for index, line in enumerate(detail.activity):
        if index:
            out.append("\n")
        style = _ACTIVITY_STYLES.get(line.kind, "")
        if line.kind == "tool":
            out.append("→ ", style="dim")
        elif line.kind == "tool_result":
            out.append("← ", style="dim")
        out.append(line.text, style=style)
    return out


class StepDetailScreen(Screen):
    """Input / output / activity for a single step, in scrollable panes."""

    # Below this width the two panes are stacked instead of placed side by
    # side: a prompt squeezed into half of an 80-column terminal wraps into
    # an unreadable ribbon.
    _STACK_BELOW_WIDTH = 100

    DEFAULT_CSS = """
    StepDetailScreen #step-title {
        width: 100%;
        padding: 0 2;
        background: $panel;
        text-style: bold;
    }

    StepDetailScreen #step-status {
        width: 100%;
        padding: 0 2;
    }

    StepDetailScreen #step-panes {
        height: 1fr;
        padding: 0 1;
    }

    StepDetailScreen .step-pane {
        width: 1fr;
        height: 100%;
        margin: 0 1;
    }

    StepDetailScreen #step-panes.-stacked {
        layout: vertical;
    }

    StepDetailScreen #step-panes.-stacked .step-pane {
        width: 100%;
        height: 1fr;
        margin: 0;
    }

    StepDetailScreen .pane-heading {
        width: 100%;
        text-style: bold;
        border-bottom: solid $panel;
    }

    StepDetailScreen .pane-scroll {
        height: 1fr;
        scrollbar-size-vertical: 1;
    }

    StepDetailScreen .pane-scroll:focus {
        border-left: thick $accent;
    }
    """

    BINDINGS = [
        ("escape", "back", "Back"),
        ("r", "reload", "Reload"),
        ("tab", "focus_next_pane", "Switch pane"),
    ]

    def __init__(self, record: RunRecord, agent_name: str) -> None:
        super().__init__()
        self._record = record
        self._agent_name = agent_name

    def action_back(self) -> None:
        """Pop back to the run-detail screen -- bound to ``escape``."""
        self.app.pop_screen()

    def action_focus_next_pane(self) -> None:
        """Move focus to the other pane so the arrow keys scroll it.

        Each pane scrolls independently, which is the point of splitting
        them -- but only the focused one responds to the arrow keys, so
        there has to be a way to swap without reaching for the mouse.
        """
        panes = list(self.query(".pane-scroll").results(VerticalScroll))
        if not panes:
            return
        focused = self.focused
        # Identity, not `list.index`: `focused` is typed as the general
        # `Widget` and may be neither pane (the table's own focus on first
        # mount), in which case the first pane is the right target.
        index = next((i + 1 for i, pane in enumerate(panes) if pane is focused), 0)
        panes[index % len(panes)].focus()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="step-title")
        yield Static(id="step-status")
        with Horizontal(id="step-panes"):
            with Vertical(classes="step-pane"):
                yield Static("Input", id="input-heading", classes="pane-heading")
                yield VerticalScroll(Static(id="input-content"), classes="pane-scroll")
            with Vertical(classes="step-pane"):
                yield Static("Output", id="output-heading", classes="pane-heading")
                yield VerticalScroll(Static(id="output-content"), classes="pane-scroll")
        yield Footer()

    def on_mount(self) -> None:
        self._set_title(self._record.workflow_name)
        self._apply_layout_for_width(self.size.width)
        self.load_step()

    def on_resize(self, event: Resize) -> None:
        """Stack or unstack the panes as the terminal is resized."""
        self._apply_layout_for_width(event.size.width)

    def _apply_layout_for_width(self, width: int) -> None:
        """Toggle the stacked layout below :attr:`_STACK_BELOW_WIDTH`."""
        self.query_one("#step-panes", Horizontal).set_class(
            width < self._STACK_BELOW_WIDTH, "-stacked"
        )

    def action_reload(self) -> None:
        """Re-read the log -- bound to ``r``, for a step still in flight."""
        self.load_step()

    @work
    async def load_step(self) -> None:
        """Read this step's detail from the log, off the event loop.

        ``derive_step_detail`` streams the whole (uncapped) log -- a
        step's prompt is emitted once, at its start, which a bounded tail
        window would miss on any long run -- large enough to be worth a
        thread rather than blocking the UI while it parses.
        """
        status = self.query_one("#step-status", Static)
        input_content = self.query_one("#input-content", Static)
        status.update(loading_text())
        try:
            detail = await asyncio.to_thread(derive_step_detail, self._record, self._agent_name)
        except Exception as e:  # noqa: BLE001 - surfaced, not crashed
            logger.warning("Failed to derive step detail for %s", self._agent_name, exc_info=True)
            status.update(styled("[red]Could not read this step:[/red] {}", str(e)))
            input_content.update(Text(""))
            return

        self._set_title(detail.workflow_name)
        status.update(status_label(detail.status))
        input_content.update(self._input_renderable(detail))
        self._update_output_pane(detail)

    def _set_title(self, workflow_name: str) -> None:
        """Set the heading, preferring the log's declared workflow name.

        Seeded from the run record so the screen is never blank while the
        log read is in flight, then corrected once it lands.
        """
        self.query_one("#step-title", Static).update(
            styled("{}  ·  {}", self._agent_name, workflow_name)
        )

    def _input_renderable(self, detail: StepDetail) -> RenderableType:
        """Build the left pane: the prompt this step was given."""
        if detail.prompt:
            return Text(detail.prompt)
        return muted("No rendered prompt recorded for this step.")

    def _update_output_pane(self, detail: StepDetail) -> None:
        """Fill the right pane with the output, or the activity so far.

        The pane branches rather than showing an empty "Output": a step
        still in flight has no output yet, and a blank pane reads as broken
        when the honest answer is "here is what it is doing".
        """
        heading = self.query_one("#output-heading", Static)
        content = self.query_one("#output-content", Static)

        if detail.output is not None:
            heading.update(Text("Output"))
            content.update(_format_output(detail.output))
            return

        if detail.activity:
            heading.update(styled("Activity  [dim]{}[/dim]", str(len(detail.activity))))
            content.update(_activity_text(detail))
        elif detail.status == "completed":
            heading.update(Text("Output"))
            content.update(muted("This step recorded no output."))
        else:
            heading.update(Text("Activity"))
            content.update(muted("No activity recorded yet."))
