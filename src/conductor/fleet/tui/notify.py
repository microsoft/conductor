"""Terminal bell / OSC 9 notifications on gate-entry and run-failure
(Fleet Manager E13-T4).

Per the design's *What single-user removes*: "Push notification service |
Terminal bell / OSC 9 is the whole feature" -- there is no notification
daemon, no desktop-notification library, and no in-app toast queue here.
Two escape sequences, written directly to the terminal the TUI is running
in, are the entire mechanism:

- ``\\a`` (BEL) -- the classic terminal bell, the same one
  :meth:`textual.app.App.bell` emits.
- ``\\x1b]9;<message>\\x07`` (OSC 9) -- the de facto "growl-style" desktop
  notification sequence supported by iTerm2, Windows Terminal, and several
  other emulators, carrying a human-readable message rather than just a
  beep.

:class:`TransitionNotifier` is the debounce: the Runs screen's ~2s poll
re-reads every displayed run's status on every tick, so without tracking
each run's *previous* status a still-open gate (or a run that stays
``failed`` after it happened, since a stale record can linger briefly
before it self-prunes) would re-fire a notification on every single poll.
:meth:`TransitionNotifier.observe` fires only on an actual transition
*into* ``at-gate``/``failed`` -- the first observation of a run already in
one of those statuses counts as a transition too (there is no prior status
to compare against), but a repeated poll of the same status never re-fires.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.app import App

    from conductor.fleet.summary import RunStatus

# Only these two statuses are notification-worthy, per the design's *Future
# work* item: gate-entry and run-failure. ``running``/``paused``/``completed``
# never fire -- ``completed`` in particular is a normal, frequent outcome
# that would make the bell noise-floor rather than signal if it fired too.
_NOTIFY_STATUSES: frozenset[str] = frozenset({"at-gate", "failed"})


class TransitionNotifier:
    """Tracks each run's last-seen status and reports whether a status
    update is a *new* transition into a notification-worthy status.

    One instance is owned by the Runs screen (not module-level state) so
    each :class:`~conductor.fleet.tui.app.FleetApp` instance -- and hence
    each test -- starts with a clean slate.
    """

    def __init__(self) -> None:
        self._last_status: dict[str, str] = {}

    def observe(self, run_id: str, status: RunStatus) -> bool:
        """Record ``status`` for ``run_id`` and report a fresh transition.

        Args:
            run_id: The run's id (the same key the Runs screen already
                uses to track its displayed records).
            status: The run's current :data:`~conductor.fleet.summary.RunStatus`.

        Returns:
            ``True`` exactly once per transition *into* ``at-gate`` or
            ``failed`` -- ``False`` for every other status, and ``False``
            on a repeated observation of the same notify-worthy status
            (the debounce this class exists for).
        """
        previous = self._last_status.get(run_id)
        self._last_status[run_id] = status
        if status not in _NOTIFY_STATUSES:
            return False
        return previous != status

    def forget(self, run_id: str) -> None:
        """Drop tracking for a single run.

        Used by :meth:`prune`; also useful on its own if a caller wants to
        force the next observation of ``run_id`` to count as a fresh
        transition (e.g. after resolving its gate through this same
        session, though the engine's own ``gate_resolved`` transition to
        ``running`` already achieves that naturally).
        """
        self._last_status.pop(run_id, None)

    def prune(self, active_run_ids: set[str]) -> None:
        """Forget every tracked run not in ``active_run_ids``.

        Called once per poll tick (after building the current run list)
        so tracking does not grow unbounded across a long-lived TUI
        session, and so a run_id that disappears and is later reused
        (vanishingly unlikely, but not a correctness assumption this
        module should make) starts fresh rather than inheriting stale
        history.
        """
        stale = [run_id for run_id in self._last_status if run_id not in active_run_ids]
        for run_id in stale:
            self.forget(run_id)


def _sanitize_notification_text(message: str) -> str:
    """Strip ASCII control characters from ``message`` before framing it.

    ``message`` is built from workflow-controlled data (a run's workflow
    name, gate agent name) via ``conductor.fleet.tui.screens.runs::
    _notification_message`` -- a value containing a raw BEL (``\\x07``,
    OSC 9's own terminator) or ESC (``\\x1b``, the start of any escape
    sequence) could otherwise terminate the OSC 9 payload early or inject
    an attacker-controlled escape sequence into the terminal (E13 review
    round 1). Every C0 control character (``0x00``-``0x1f``) and DEL
    (``0x7f``) is stripped; ordinary printable text (including non-ASCII)
    is left untouched.
    """
    return "".join(ch for ch in message if ord(ch) >= 0x20 and ord(ch) != 0x7F)


def build_notification_sequence(message: str) -> str:
    """Build the bell + OSC 9 escape sequence for ``message``.

    Pure and side-effect-free (no I/O) so it can be tested directly,
    independent of :func:`emit_terminal_notification`'s headless/driver
    guards.

    Args:
        message: Human-readable notification text (e.g. naming the run
            and its new status).

    Returns:
        The literal string to write to the terminal.
    """
    return f"\a\x1b]9;{_sanitize_notification_text(message)}\x07"


def emit_terminal_notification(app: App, message: str) -> None:
    """Write a terminal bell / OSC 9 notification for ``message``.

    Mirrors :meth:`textual.app.App.bell`'s own guard (``is_headless`` and a
    live driver) rather than writing straight to ``sys.stdout`` -- every
    test app (``App.run_test()``) is headless, so this must never touch a
    driver in that mode, and there is no driver at all before the app has
    started (or after it has stopped).

    Args:
        app: The running Textual app whose terminal driver to write to.
        message: Human-readable notification text.
    """
    if app.is_headless:
        return
    driver = app._driver  # noqa: SLF001 - mirrors App.bell()'s own access to this attribute
    if driver is not None:
        driver.write(build_notification_sequence(message))
