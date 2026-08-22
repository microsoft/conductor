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
import time
from collections.abc import Callable
from typing import Any

import pytest
from textual.worker import WorkerCancelled, WorkerFailed


@pytest.fixture(autouse=True)
def _no_tui_animation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable TUI animation for every test in this package.

    Tests that specifically exercise animation opt back in with
    ``monkeypatch.delenv("CONDUCTOR_FLEET_NO_ANIM", raising=False)``.

    Also clears the remote-session detection signal (``SESSIONNAME``) and
    the force-on override (``CONDUCTOR_FLEET_ANIM``). This is load-bearing,
    not hygiene: without it, running the suite inside an RDP session would
    make ``animations_enabled()`` return ``False`` even in the tests that
    deliberately opt back in by deleting ``CONDUCTOR_FLEET_NO_ANIM``
    (``test_tui_splash.py``, ``test_tui_runs.py``), which would pass on a
    local machine and fail for a developer working over a remote box.
    """
    monkeypatch.setenv("CONDUCTOR_FLEET_NO_ANIM", "1")
    monkeypatch.delenv("CONDUCTOR_FLEET_ANIM", raising=False)
    monkeypatch.delenv("SESSIONNAME", raising=False)


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


async def wait_for(
    pilot: Any,
    predicate: Callable[[], bool],
    *,
    message: str,
    timeout: float = 20.0,
) -> None:
    """Pump the app until ``predicate`` holds, instead of sleeping a fixed time.

    The screens these tests drive refresh on a **real** ``set_interval``
    timer (shrunk to 0.05s by the tests that exercise polling), and each
    tick hops through ``asyncio.to_thread`` before rendering. How long a
    tick-plus-scan actually takes is a property of the machine, not of the
    test: on CI it runs under ``coverage`` tracing on a small shared
    runner, where a measured scan is several times slower than on a
    developer box. A fixed ``await asyncio.sleep(0.3)`` followed by an
    assertion therefore encodes a guess about machine speed, and asserts
    against pre-tick state whenever the guess is wrong -- which is exactly
    how ``test_poll_tick_removes_completed_run`` failed on Windows CI while
    passing everywhere else.

    Waiting on the *condition* removes the guess: a fast machine returns on
    the first sample, a slow one takes longer, and only a genuinely wedged
    screen reaches the deadline and fails -- with ``message`` naming the
    regression rather than an opaque ``assert 1 == 0``.

    Samples with a plain ``asyncio.sleep`` rather than ``pilot.pause()``.
    That is deliberate and load-bearing: ``pilot.pause()`` waits for the
    app to go *idle*, which under a 20Hz poll timer takes ~1 second per
    call (measured), so a ``pause``-driven loop gets only a handful of
    samples however long its deadline is. Yielding to the event loop is
    all that is actually required for a worker to finish and render, since
    the render half runs on that loop. A single ``pilot.pause()`` is done
    once the predicate holds, so anything asserted about *painted* output
    afterwards sees a settled screen.

    Args:
        pilot: The Textual ``Pilot`` driving the app under test.
        predicate: Called on each sample; must be cheap and side-effect
            free. Should describe a **monotonic** condition ("the row
            appeared") rather than a transient one ("a refresh is in
            flight"), since a transient condition can switch back between
            two samples however fast the loop runs.
        message: Assertion message describing the regression a timeout
            implies.
        timeout: Seconds before giving up. Generous by default because it
            is only ever reached on failure.

    Raises:
        AssertionError: If ``predicate`` never held before the deadline.
    """
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            await pilot.pause()
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"{message} (waited {timeout:g}s)")
        await asyncio.sleep(0.01)
