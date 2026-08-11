"""In-run mid-execution guidance channel (issue #400).

``--web-bg`` runs have no TTY for the existing Esc/Ctrl+G interrupt flow to
fall back on, so a dashboard user or a ``conductor guide`` CLI caller needs a
way to push a correction into a running workflow without stopping it first.
``GuidanceChannel`` is that inbound queue: a plain buffer plus an
``asyncio.Event`` the engine can wait on alongside its existing resume/kill/
disconnect arms.

A leaf module with no conductor imports (like ``duration.py``), so
``web/server.py`` never has to import from ``engine/`` to hand the engine a
sink callable.
"""

from __future__ import annotations

import asyncio


class GuidanceChannel:
    """Buffers pending guidance text and signals the engine when it arrives.

    ``asyncio.Event()`` is constructed eagerly in ``__init__`` — safe on
    Python 3.12 (no loop binding at construction time), matching
    ``WebDashboard.__init__``'s own eager event construction.
    """

    def __init__(self) -> None:
        self._pending: list[str] = []
        self._event = asyncio.Event()

    @property
    def event(self) -> asyncio.Event:
        """The event set whenever new guidance is submitted."""
        return self._event

    @property
    def pending(self) -> int:
        """Count of guidance entries submitted but not yet drained."""
        return len(self._pending)

    def submit(self, text: str) -> int:
        """Append a guidance entry and wake any waiter.

        Args:
            text: The guidance text to queue.

        Returns:
            The number of entries now pending (including this one).
        """
        self._pending.append(text)
        self._event.set()
        return len(self._pending)

    def drain(self) -> list[str]:
        """Pop and return all pending guidance entries, in submission order.

        Clears the event — a fresh ``submit()`` re-sets it. Returns an empty
        list (and leaves the event clear) when nothing is pending.
        """
        entries = self._pending
        self._pending = []
        self._event.clear()
        return entries
