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

import asyncio
import contextlib
from typing import Any

import pytest
from textual.worker import WorkerCancelled, WorkerFailed


@pytest.fixture(autouse=True)
def _no_tui_animation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable TUI animation for every test in this package.

    Tests that specifically exercise animation opt back in with
    ``monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)``.
    """
    monkeypatch.setenv("CONDUCTOR_FLEET_NO_ANIM", "1")


async def settle(pilot: Any) -> None:
    """Pause until every background load worker has finished and rendered.

    Every screen's data load runs as an ``async`` ``@work`` method that
    awaits ``asyncio.to_thread(...)`` before rendering (issue #437), so a
    single ``pilot.pause()`` after an action is not enough to observe the
    result -- it only lets the event loop turn over once, which may land
    before the worker's thread hop has even started. This waits for every
    worker on the app to finish, then pauses once more so the rendering
    that worker's completion triggers is actually painted.

    The **leading** ``pilot.pause()`` is load-bearing, not redundant: it
    lets the keypress or timer handler that started the worker actually run
    and register it, so there is something for ``wait_for_complete()`` to
    wait on. It also lets an already-cancelled worker be reaped first --
    ``Worker.wait()`` *raises* ``WorkerCancelled``/``WorkerFailed``, which
    is why that is caught below rather than left to fail an unrelated
    assertion.

    Bounded because this project has no ``pytest-timeout``: without a
    deadline the documented ``push_screen_wait`` misuse (see
    ``AGENTS.md``'s test caution) hangs the entire run rather than failing
    one test.

    Not usable between the two keypresses of a modal interaction -- see
    that same caution.
    """
    await pilot.pause()
    with contextlib.suppress(WorkerCancelled, WorkerFailed):
        await asyncio.wait_for(pilot.app.workers.wait_for_complete(), timeout=30)
    await pilot.pause()
