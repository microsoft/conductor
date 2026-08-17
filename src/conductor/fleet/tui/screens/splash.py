"""The launch splash: an animated wordmark shown while the fleet is scanned.

Deliberately *not* decoration for its own sake. The Runs screen's first
render is preceded by a run-record scan and a bounded log read per run, which
on a machine with several long-running workflows is a visible pause with
nothing on screen -- the program looked like it had hung before it had drawn
anything at all. This fills that moment with something that says what the
program is.

Three rules keep it from becoming an obstacle:

* It is **time-boxed** (:data:`SPLASH_SECONDS`), so it never gates the UI on
  work that has not finished.
* It is **skippable** by any key or click, because the second time a user
  sees a splash is already one time too many if they cannot dismiss it.
* It is **suppressed entirely** when animations are disabled, so the same
  environment variable that quiets the spinners quiets this too.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Middle
from textual.screen import Screen
from textual.widgets import Static

from conductor.fleet.tui.anim import FRAME_INTERVAL, spinner
from conductor.fleet.tui.art import BATON, SPLASH_RAMP, gradient, wordmark

#: How long the splash holds before dismissing itself.
SPLASH_SECONDS = 1.4

#: Frames over which the wordmark fades in, one line at a time.
_REVEAL_FRAMES = 6


class SplashScreen(Screen[None]):
    """A brief animated wordmark, dismissed on a timer or any input."""

    DEFAULT_CSS = """
    SplashScreen {
        background: $background;
    }

    SplashScreen #splash-art {
        width: auto;
        text-align: center;
    }

    SplashScreen #splash-tag {
        width: auto;
        text-align: center;
        color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._frame = 0

    def compose(self) -> ComposeResult:
        with Middle(), Center():
            yield Static(id="splash-art")
            yield Static(id="splash-tag")

    def on_mount(self) -> None:
        self._render_frame()
        self.set_interval(FRAME_INTERVAL, self._tick)
        self.set_timer(SPLASH_SECONDS, self._dismiss)

    def _tick(self) -> None:
        self._frame += 1
        self._render_frame()

    def _render_frame(self) -> None:
        art = wordmark(self.size.width)
        # Revealed line by line rather than all at once: a wordmark that
        # simply appears is a static image, and the reveal is what makes the
        # 1.4 seconds feel like the program starting rather than stalling.
        lines = art.plain.splitlines()
        visible = min(len(lines), 1 + self._frame * len(lines) // max(1, _REVEAL_FRAMES))
        self.query_one("#splash-art", Static).update(
            gradient("\n".join(lines[:visible]), SPLASH_RAMP)
        )

        tag = self.query_one("#splash-tag", Static)
        if visible >= len(lines):
            tag.update(f"{BATON}\n{spinner(self._frame)} scanning the fleet")
        else:
            tag.update("")

    def _dismiss(self) -> None:
        # `is_active`, not `is_current`: this pops whatever is on top, so it
        # must only fire while *this* screen is what's on top. `is_current`
        # means "still being composited", which stays true for a covered
        # screen -- so a splash covered at the moment its timer fired would
        # have popped the thing covering it. It also still guards the race
        # this was written for (the timer and a keypress both dismiss, and
        # popping an already-popped screen raises), since a popped screen is
        # neither active nor current.
        if self.is_active:
            self.app.pop_screen()

    def on_key(self) -> None:
        self._dismiss()

    def on_click(self) -> None:
        self._dismiss()
