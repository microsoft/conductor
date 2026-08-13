"""Shared Textual widgets for the Fleet Manager TUI."""

from __future__ import annotations

from collections.abc import Iterable

from textual.app import ComposeResult
from textual.widgets import Footer
from textual.widgets._footer import FooterKey

__all__ = ["BlockFooter"]


class BlockFooter(Footer):
    """A :class:`~textual.widgets.Footer` that draws a rule between two
    groups of keys.

    The Runs screen's bindings are two different kinds of thing -- keys that
    act on the highlighted run, and keys that navigate the app or command the
    whole fleet -- and ordering them into blocks is not enough on its own:
    every key renders with identical styling and spacing, so a reader sees one
    undifferentiated run of nine keys and the boundary is invisible.

    Textual 8.x *does* have native binding groups
    (``Binding(group=...)``), but its ``Footer`` renders a group by dropping
    every per-key description and emitting a single label for the group -- so
    ``w Dash  k Kill`` collapses to a bare ``w  k``. That trades one
    discoverability problem for a worse one, since the keys are exactly what a
    newcomer cannot guess. This keeps the descriptions and draws the divider
    instead, reusing the ``vkey`` left border Textual's own stylesheet already
    uses to fence off the docked command-palette key.

    The divider is attached to the *first* key of the second block rather than
    emitted as its own widget, so it costs no additional footer columns -- the
    footer is a single non-wrapping line with no room to spare.
    """

    DEFAULT_CSS = """
    BlockFooter {
        FooterKey.-block-start {
            border-left: vkey $foreground 20%;
            margin-left: 1;
        }
    }
    """

    def __init__(
        self,
        *,
        first_block_actions: Iterable[str],
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """
        Args:
            first_block_actions: Action names making up the first block. The
                divider is drawn before the first key whose action is *not* in
                this set, so the caller names one block rather than having to
                keep two lists in sync with ``BINDINGS``.
            id: The ID of the widget in the DOM.
            classes: The CSS classes for the widget.
        """
        super().__init__(id=id, classes=classes)
        self._first_block_actions = frozenset(first_block_actions)

    def compose(self) -> ComposeResult:
        """Tag the first second-block key, delegating the rest to ``Footer``.

        Wrapping the parent's generator rather than reimplementing it keeps
        this immune to changes in how Textual builds the footer (disabled
        keys, the docked command-palette entry, group handling).

        The tag is only applied once a first-block key has actually been
        seen: :meth:`RunsScreen.check_action` hides every row-scoped key while
        the fleet is empty, and a divider hanging off the leading key with
        nothing to its left reads as a rendering fault rather than a grouping.
        """
        seen_first_block = False
        tagged = False
        for widget in super().compose():
            if isinstance(widget, FooterKey):
                if widget.action in self._first_block_actions:
                    seen_first_block = True
                elif seen_first_block and not tagged:
                    widget.add_class("-block-start")
                    tagged = True
            yield widget
