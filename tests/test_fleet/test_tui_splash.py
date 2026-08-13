"""Pilot tests for the launch splash.

The splash is the one part of the TUI that sits *between* the user and the
screen they asked for, so the properties worth protecting are the ones that
stop it becoming an obstacle: it dismisses itself, it dismisses on input, and
it never appears at all when animation is switched off.

This module opts back into animation (the package-wide fixture in
``conftest.py`` disables it) because a splash test with animation disabled
would assert nothing.
"""

from __future__ import annotations

import asyncio

import pytest

from conductor.fleet.tui.app import FleetApp
from conductor.fleet.tui.screens.runs import RunsScreen
from conductor.fleet.tui.screens.splash import SPLASH_SECONDS, SplashScreen


@pytest.fixture(autouse=True)
def _with_animation(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Re-enable animation, and isolate the run-record directories.

    Without the isolation these tests would scan the developer's real
    ``~/.conductor/runs`` and derive summaries from live runs.
    """
    monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)

    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))

    legacy = tmp_path / "legacy_runs"
    legacy.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: legacy)


class TestSplash:
    async def test_is_shown_on_launch(self) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, SplashScreen)

    async def test_the_home_screen_is_already_mounted_behind_it(self) -> None:
        """Pushed *over* Runs rather than before it, so the fleet scan the
        splash exists to cover is actually underway while it is shown."""
        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert any(isinstance(s, RunsScreen) for s in app.screen_stack)

    async def test_dismisses_itself(self) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(SPLASH_SECONDS + 0.4)
            assert isinstance(app.screen, RunsScreen)

    async def test_any_key_dismisses_it(self) -> None:
        """The second time a user sees a splash is one time too many if they
        cannot skip it."""
        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(app.screen, RunsScreen)

    async def test_dismissing_twice_does_not_raise(self) -> None:
        """The self-dismiss timer and a keypress race each other."""
        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            await asyncio.sleep(SPLASH_SECONDS + 0.4)
            assert isinstance(app.screen, RunsScreen)

    async def test_renders_the_wordmark(self) -> None:
        app = FleetApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()

            # Polled rather than slept on: the wordmark is revealed a line
            # per animation frame, and a fixed sleep races the scheduler
            # under load (which is exactly how this failed in a full run
            # while passing on its own).
            painted = ""
            deadline = SPLASH_SECONDS
            while deadline > 0 and isinstance(app.screen, SplashScreen):
                painted = "".join(
                    "".join(segment.text for segment in strip)
                    for strip in app.screen._compositor.render_strips()
                )
                if "█" in painted:
                    break
                await asyncio.sleep(0.05)
                deadline -= 0.05

            assert "█" in painted

    async def test_is_skipped_entirely_when_animation_is_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same switch that quiets the spinners quiets this too."""
        monkeypatch.setenv("CONDUCTOR_FLEET_NO_ANIM", "1")
        app = FleetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, RunsScreen)
