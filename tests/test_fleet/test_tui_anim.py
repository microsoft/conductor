"""Tests for the TUI's animation and art primitives.

These modules are deliberately pure -- frame number in, glyph or style out --
which is what makes them testable without a running app. That purity is also
what lets the screens render a sensible *still* frame when animation is
switched off, so the "frame 0 is a reasonable static image" property is
asserted here rather than left as an assumption.
"""

from __future__ import annotations

import pytest

from conductor.fleet.tui import anim, art


class TestSpinner:
    def test_cycles_through_every_frame(self) -> None:
        seen = {anim.spinner(f) for f in range(len(anim.SPINNER_FRAMES))}
        assert seen == set(anim.SPINNER_FRAMES)

    def test_wraps_around(self) -> None:
        assert anim.spinner(len(anim.SPINNER_FRAMES)) == anim.spinner(0)

    def test_tolerates_a_negative_frame(self) -> None:
        """The counter is only ever incremented, but a spinner that raised
        would take down the repaint timer that called it."""
        assert anim.spinner(-1) in anim.SPINNER_FRAMES

    def test_empty_sequence_is_not_an_error(self) -> None:
        assert anim.spinner(3, frames="") == ""

    def test_every_frame_is_a_single_cell(self) -> None:
        """A spinner whose frames differ in width shifts the text after it."""
        assert {len(f) for f in anim.SPINNER_FRAMES} == {1}


class TestBreathStyle:
    def test_includes_the_colour(self) -> None:
        assert "yellow" in anim.breath_style(0, "yellow")

    def test_varies_across_the_cycle(self) -> None:
        styles = {anim.breath_style(f, "yellow") for f in range(64)}
        assert len(styles) > 1

    def test_no_colour_yields_a_bare_style(self) -> None:
        """Must not leave a leading space -- Rich parses styles by token."""
        assert anim.breath_style(4, "") in ("", "dim", "bold")


class TestSparkline:
    def test_empty_series_renders_nothing(self) -> None:
        assert anim.sparkline([]) == ""

    def test_is_always_exactly_the_requested_width(self) -> None:
        """A sparkline that grew a cell per sample re-widened its column on
        every poll, shoving the numeric columns beside it sideways."""
        for count in range(1, 20):
            assert len(anim.sparkline([1.0] * count, width=10)) == 10

    def test_scales_against_the_series_peak(self) -> None:
        rendered = anim.sparkline([0.0, 50.0, 100.0], width=3)
        assert rendered[0] == anim.SPARK_GLYPHS[0]
        assert rendered[2] == anim.SPARK_GLYPHS[-1]

    def test_an_all_zero_series_is_a_floor_not_a_blank(self) -> None:
        """A blank reads as "no data", which is a different claim about a run
        than "this run is idle"."""
        assert anim.sparkline([0.0, 0.0], width=4).strip() == anim.SPARK_GLYPHS[0] * 2

    def test_keeps_the_most_recent_samples(self) -> None:
        rendered = anim.sparkline([100.0, 0.0, 0.0], width=2)
        assert rendered.strip() == anim.SPARK_GLYPHS[0] * 2

    def test_zero_width_renders_nothing(self) -> None:
        assert anim.sparkline([1.0], width=0) == ""


class TestMarquee:
    def test_short_text_does_not_move(self) -> None:
        """Movement should mean "there is more to see"."""
        assert {anim.marquee("hi", f, width=10) for f in range(20)} == {"hi"}

    def test_long_text_scrolls(self) -> None:
        text = "abcdefghijklmnopqrstuvwxyz"
        assert len({anim.marquee(text, f, width=5) for f in range(40)}) > 1

    def test_window_is_the_requested_width(self) -> None:
        assert len(anim.marquee("abcdefghijklmnop", 7, width=5)) == 5

    def test_zero_width_renders_nothing(self) -> None:
        assert anim.marquee("abc", 0, width=0) == ""


class TestProgressBar:
    def test_zero_total_renders_nothing(self) -> None:
        assert anim.progress_bar(0, 0).plain == ""

    def test_is_always_the_requested_width(self) -> None:
        for done in range(0, 24):
            assert len(anim.progress_bar(done, 23, width=16).plain) == 16

    def test_full_when_complete(self) -> None:
        bar = anim.progress_bar(23, 23, width=8)
        assert bar.plain == "━" * 8
        assert bar.spans[0].style == "green"

    def test_partial_splits_filled_from_remainder(self) -> None:
        bar = anim.progress_bar(4, 8, width=8)
        assert [span.style for span in bar.spans] == ["green", "dim"]


class TestAnimationsEnabled:
    def test_enabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)
        assert anim.animations_enabled() is True

    def test_disabled_by_the_environment_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONDUCTOR_FLEET_NO_ANIM", "1")
        assert anim.animations_enabled() is False

    def test_empty_value_does_not_disable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset-looking value should behave as unset."""
        monkeypatch.setenv("CONDUCTOR_FLEET_NO_ANIM", "")
        assert anim.animations_enabled() is True

    def test_no_anim_wins_over_the_force_on_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONDUCTOR_FLEET_NO_ANIM", "1")
        monkeypatch.setenv("CONDUCTOR_FLEET_ANIM", "1")
        assert anim.animations_enabled() is False

    def test_force_on_wins_over_a_detected_remote_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)
        monkeypatch.setenv("CONDUCTOR_FLEET_ANIM", "1")
        monkeypatch.setenv("SESSIONNAME", "RDP-Tcp#0")
        assert anim.animations_enabled() is True

    def test_disabled_by_a_detected_rdp_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)
        monkeypatch.setenv("SESSIONNAME", "RDP-Tcp#0")
        assert anim.animations_enabled() is False

    def test_an_ssh_session_alone_does_not_disable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SSH is deliberately *not* an automatic trigger (issue #462).

        Locks in the scoping decision rather than merely omitting a test:
        SSH ships the ANSI byte stream and the client renders it, so the
        per-frame cost is bytes of changed text, not the changed pixel
        regions RDP has to encode and ship. Measured in practice as fine.
        See :func:`conductor.fleet.tui.anim.is_remote_session`.
        """
        monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)
        monkeypatch.delenv("SESSIONNAME", raising=False)
        monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 22 5.6.7.8 22")
        monkeypatch.setenv("SSH_TTY", "/dev/pts/3")
        assert anim.animations_enabled() is True

    def test_the_console_session_is_not_a_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The physical/console session is named plain ``Console`` -- it
        must not be treated as remote."""
        monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)
        monkeypatch.setenv("SESSIONNAME", "Console")
        assert anim.animations_enabled() is True

    def test_empty_force_on_value_does_not_enable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)
        monkeypatch.setenv("CONDUCTOR_FLEET_ANIM", "")
        monkeypatch.setenv("SESSIONNAME", "RDP-Tcp#0")
        assert anim.animations_enabled() is False


class TestIsRemoteSession:
    def test_no_signal_is_not_remote(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SESSIONNAME", raising=False)
        assert anim.is_remote_session() is None

    def test_rdp_session_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SESSIONNAME", "RDP-Tcp#0")
        assert anim.is_remote_session() == "RDP"

    def test_rdp_match_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SESSIONNAME", "rdp-tcp#1")
        assert anim.is_remote_session() == "RDP"

    def test_console_session_is_not_remote(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SESSIONNAME", "Console")
        assert anim.is_remote_session() is None

    def test_ssh_signals_are_not_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Neither OpenSSH signal makes a session "remote" for this purpose.

        A negative assertion on purpose: SSH detection was implemented and
        then deliberately scoped back out (issue #462), so this pins the
        decision where a missing test would just look like an oversight
        and invite someone to "fix" it.
        """
        monkeypatch.delenv("SESSIONNAME", raising=False)
        monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 22 5.6.7.8 22")
        monkeypatch.setenv("SSH_TTY", "/dev/pts/3")
        assert anim.is_remote_session() is None


class TestDisabledReason:
    def test_none_when_animation_is_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)
        assert anim.disabled_reason() is None

    def test_none_when_disabled_by_the_explicit_off_switch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit ``CONDUCTOR_FLEET_NO_ANIM`` is the reader's own
        choice -- it must not trigger the detection notification, or every
        test in this suite (which sets it unconditionally) would."""
        monkeypatch.setenv("CONDUCTOR_FLEET_NO_ANIM", "1")
        monkeypatch.setenv("SESSIONNAME", "RDP-Tcp#0")
        assert anim.disabled_reason() is None

    def test_none_when_forced_on_over_a_detected_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)
        monkeypatch.setenv("CONDUCTOR_FLEET_ANIM", "1")
        monkeypatch.setenv("SESSIONNAME", "RDP-Tcp#0")
        assert anim.disabled_reason() is None

    def test_names_the_detected_session_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)
        monkeypatch.setenv("SESSIONNAME", "RDP-Tcp#0")
        assert anim.disabled_reason() == "RDP"


class TestArt:
    def test_wordmark_widths_match_the_art(self) -> None:
        """The declared widths are what callers use to decide whether the art
        fits, so art that outgrew its constant would wrap on screen."""
        assert max(len(x) for x in art.WORDMARK.splitlines()) == art.WORDMARK_WIDTH
        assert max(len(x) for x in art.WORDMARK_SMALL.splitlines()) == art.WORDMARK_SMALL_WIDTH

    def test_wordmark_degrades_with_available_width(self) -> None:
        assert art.wordmark(art.WORDMARK_WIDTH).plain == art.WORDMARK
        assert art.wordmark(art.WORDMARK_SMALL_WIDTH).plain == art.WORDMARK_SMALL
        assert art.wordmark(4).plain == "CONDUCTOR"

    def test_gradient_preserves_the_art_exactly(self) -> None:
        rendered = art.gradient(art.WORDMARK_SMALL, art.SPLASH_RAMP)
        assert rendered.plain == art.WORDMARK_SMALL

    def test_gradient_styles_every_line(self) -> None:
        rendered = art.gradient(art.WORDMARK_SMALL, ["red", "blue"])
        assert len(rendered.spans) == len(art.WORDMARK_SMALL.splitlines())

    def test_gradient_without_colours_is_still_the_art(self) -> None:
        assert art.gradient(art.WORDMARK_SMALL, []).plain == art.WORDMARK_SMALL
