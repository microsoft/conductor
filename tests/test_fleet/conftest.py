"""Shared fixtures for the Fleet Manager tests.

The one thing every test in this package needs is a TUI that does **not
animate**. Animation is not incidental to these tests -- it actively breaks
them in two ways:

* The launch splash (:mod:`conductor.fleet.tui.screens.splash`) is pushed
  over the Runs screen, so a pilot test that presses a key immediately sends
  it to the splash instead of the screen under test.
* A 10fps repaint timer runs alongside every assertion, which makes a test
  that inspects a table cell race a repaint of that same cell.

Disabling it here rather than in each test file keeps the suite honest about
what it is testing: these are tests of *behaviour*, and the animation layer
is deliberately pure and tested separately (``test_tui_anim.py``,
``test_tui_splash.py``), where it is switched back on explicitly.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_tui_animation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable TUI animation for every test in this package.

    Tests that specifically exercise animation opt back in with
    ``monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)``.
    """
    monkeypatch.setenv("CONDUCTOR_FLEET_NO_ANIM", "1")
