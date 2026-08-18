"""Pilot tests for :class:`~conductor.fleet.tui.app.FleetApp`'s remote-session
detection wiring (issue #462 review).

The package-wide fixture in ``conftest.py`` sets ``CONDUCTOR_FLEET_NO_ANIM=1``
for every test in this package, which makes ``anim.disabled_reason()`` return
``None`` unconditionally and the ``self.notify(...)`` branch in
``FleetApp.on_mount`` unreachable -- these tests opt back out of that default
(the same way ``test_tui_splash.py`` and parts of ``test_tui_runs.py`` opt
back into animation) so the branch that only ever fires under RDP is
actually exercised somewhere in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.fleet.tui.app import FleetApp
from conductor.fleet.tui.screens.splash import SplashScreen
from tests.test_fleet.conftest import settle


@pytest.fixture()
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the run-record directories, mirroring ``test_tui_runs.py``'s
    fixture of the same name, so these pilot tests never scan the developer's
    real ``~/.conductor/``."""
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))

    legacy_dir = tmp_path / "legacy_runs"
    legacy_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: legacy_dir)

    return home


class TestRemoteSessionDetectionWiring:
    """`app.py::on_mount` reacts to `anim.disabled_reason()` by disabling
    Textual's own `App.animation_level` and telling the user why. The
    detection rules themselves (`is_remote_session`, `animations_enabled`,
    `disabled_reason`) are unit-tested in isolation in `test_tui_anim.py`;
    these tests cover the wiring those functions feed into.
    """

    async def test_detected_remote_session_disables_animation_and_notifies(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)
        monkeypatch.setenv("SESSIONNAME", "RDP-Tcp#0")

        app = FleetApp()
        async with app.run_test(notifications=True) as pilot:
            await settle(pilot)

            assert not isinstance(app.screen, SplashScreen), (
                "a detected remote session must skip the splash entirely"
            )
            assert app.animation_level == "none"
            assert len(app._notifications) == 1
            (notification,) = list(app._notifications)
            assert "RDP" in notification.message

    async def test_explicit_no_anim_disables_animation_without_a_notification(
        self, fleet_env: Path
    ) -> None:
        """`CONDUCTOR_FLEET_NO_ANIM` is left set by the package-wide fixture
        here, so this pins the other half of `disabled_reason`'s contract:
        an explicit opt-out disables animation exactly like detection does,
        but must never produce the notification reserved for detection."""
        app = FleetApp()
        async with app.run_test(notifications=True) as pilot:
            await settle(pilot)

            assert not isinstance(app.screen, SplashScreen)
            assert app.animation_level == "none"
            assert len(app._notifications) == 0

    async def test_forced_on_animation_shows_splash_without_a_notification(
        self, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)
        monkeypatch.setenv("CONDUCTOR_FLEET_ANIM", "1")
        monkeypatch.setenv("SESSIONNAME", "RDP-Tcp#0")

        app = FleetApp()
        async with app.run_test(notifications=True) as pilot:
            await pilot.pause()

            assert isinstance(app.screen, SplashScreen)
            assert len(app._notifications) == 0
