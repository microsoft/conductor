"""Tests for the Fleet Manager TUI's terminal bell / OSC 9 notifications
(Fleet Manager E13-T4).

Covers:
- ``TransitionNotifier`` fires exactly once per transition **into**
  ``at-gate`` or ``failed`` -- never once per poll tick for a run that
  stays in that status across multiple polls, and fires again on a later,
  separate transition into the same status (resolve-then-re-gate).
- ``build_notification_sequence`` produces a bell + OSC 9 escape sequence.
- ``emit_terminal_notification`` writes that sequence to the app's driver,
  except when the app is headless (as in every test here) or has no
  driver -- mirroring Textual's own ``App.bell()`` guard.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from conductor.fleet.tui.notify import (
    TransitionNotifier,
    build_notification_sequence,
    emit_terminal_notification,
)

# ---------------------------------------------------------------------------
# TransitionNotifier
# ---------------------------------------------------------------------------


class TestTransitionNotifierFiresOnceOnPerTransition:
    def test_fires_once_on_transition_into_at_gate(self) -> None:
        notifier = TransitionNotifier()
        assert notifier.observe("run-1", "running") is False
        assert notifier.observe("run-1", "at-gate") is True
        # A poll re-read of the same still-open gate must not re-fire.
        assert notifier.observe("run-1", "at-gate") is False
        assert notifier.observe("run-1", "at-gate") is False

    def test_fires_once_on_transition_into_failed(self) -> None:
        notifier = TransitionNotifier()
        assert notifier.observe("run-1", "running") is False
        assert notifier.observe("run-1", "failed") is True
        assert notifier.observe("run-1", "failed") is False

    def test_does_not_fire_for_running_paused_or_completed(self) -> None:
        notifier = TransitionNotifier()
        assert notifier.observe("run-1", "running") is False
        assert notifier.observe("run-1", "paused") is False
        assert notifier.observe("run-1", "running") is False
        assert notifier.observe("run-1", "completed") is False

    def test_first_observation_already_at_gate_fires(self) -> None:
        """A run discovered already at-gate (e.g. the TUI was just
        started) still notifies -- there is no prior status to compare
        against, so the first observation is itself a transition."""
        notifier = TransitionNotifier()
        assert notifier.observe("run-1", "at-gate") is True

    def test_first_observation_already_failed_fires(self) -> None:
        notifier = TransitionNotifier()
        assert notifier.observe("run-1", "failed") is True

    def test_leaving_and_reentering_at_gate_fires_again(self) -> None:
        """Resolving a gate and later hitting a second one is a new,
        separate transition -- not suppressed by the first."""
        notifier = TransitionNotifier()
        assert notifier.observe("run-1", "at-gate") is True
        assert notifier.observe("run-1", "running") is False
        assert notifier.observe("run-1", "at-gate") is True

    def test_tracks_multiple_runs_independently(self) -> None:
        notifier = TransitionNotifier()
        assert notifier.observe("run-1", "at-gate") is True
        assert notifier.observe("run-2", "at-gate") is True
        assert notifier.observe("run-1", "at-gate") is False
        assert notifier.observe("run-2", "at-gate") is False

    def test_forget_resets_tracking_for_a_run(self) -> None:
        notifier = TransitionNotifier()
        notifier.observe("run-1", "at-gate")
        notifier.forget("run-1")
        assert notifier.observe("run-1", "at-gate") is True

    def test_prune_drops_runs_no_longer_active(self) -> None:
        """Pruning to the currently-displayed run_id set forgets any run
        that has disappeared (completed and had its record removed,
        killed, etc.) so tracking does not grow unbounded across a long
        TUI session."""
        notifier = TransitionNotifier()
        notifier.observe("run-1", "at-gate")
        notifier.observe("run-2", "at-gate")

        notifier.prune({"run-2"})

        assert notifier.observe("run-1", "at-gate") is True  # forgotten -> fires again
        assert notifier.observe("run-2", "at-gate") is False  # still tracked


# ---------------------------------------------------------------------------
# build_notification_sequence
# ---------------------------------------------------------------------------


class TestBuildNotificationSequence:
    def test_includes_bell_and_osc9(self) -> None:
        seq = build_notification_sequence("hello")
        assert seq.startswith("\a")
        assert "\x1b]9;hello\x07" in seq

    def test_strips_control_characters_from_workflow_controlled_message(self) -> None:
        """A workflow-controlled message (run/gate name) containing a raw
        BEL or ESC must not be able to terminate the OSC 9 payload early
        or inject its own escape sequence (E13 review round 1)."""
        malicious = "evil\x07\x1b]0;pwned\x07name"
        seq = build_notification_sequence(malicious)
        # The ESC/BEL control characters are stripped from the payload;
        # only the leading terminal-bell prefix and the OSC 9 terminator
        # remain -- the payload's printable text (including a literal
        # "]0;" that isn't itself a control character) is otherwise
        # preserved verbatim, since only control bytes are stripped.
        assert seq == "\a\x1b]9;evil]0;pwnedname\x07"
        # Exactly two BEL bytes survive: the leading terminal-bell prefix
        # and the OSC 9 terminator -- the two BELs embedded in the
        # malicious message are stripped.
        assert seq.count("\x07") == 2
        assert seq.count("\x1b") == 1

    def test_leaves_ordinary_printable_text_untouched(self) -> None:
        seq = build_notification_sequence("qa-bot: waiting at gate (reviewer)")
        assert "\x1b]9;qa-bot: waiting at gate (reviewer)\x07" in seq


# ---------------------------------------------------------------------------
# emit_terminal_notification
# ---------------------------------------------------------------------------


class TestEmitTerminalNotification:
    def test_writes_sequence_to_driver_when_not_headless(self) -> None:
        app = MagicMock()
        app.is_headless = False
        driver = MagicMock()
        app._driver = driver

        emit_terminal_notification(app, "hi")

        driver.write.assert_called_once()
        written = driver.write.call_args.args[0]
        assert "\a" in written
        assert "hi" in written

    def test_noop_when_headless(self) -> None:
        """Every test app (``App.run_test()``) is headless -- notifications
        must never write to a driver in that mode, mirroring
        ``App.bell()``'s own headless guard."""
        app = MagicMock()
        app.is_headless = True
        driver = MagicMock()
        app._driver = driver

        emit_terminal_notification(app, "hi")

        driver.write.assert_not_called()

    def test_noop_when_no_driver(self) -> None:
        app = MagicMock()
        app.is_headless = False
        app._driver = None

        # Must not raise even though there is nothing to write to.
        emit_terminal_notification(app, "hi")
