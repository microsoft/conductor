"""In-run mid-execution guidance channel (issue #400).

``--web-bg`` runs have no TTY for the existing Esc/Ctrl+G interrupt flow to
fall back on, so a dashboard user or a ``conductor guide`` CLI caller needs a
way to push a correction into a running workflow without stopping it first.
``GuidanceChannel`` is that inbound queue: a plain buffer plus an
``asyncio.Event`` the engine can wait on alongside its existing resume/kill/
disconnect arms.

A leaf module with no conductor imports (like ``duration.py``). The
``GuidanceChannel`` sink pattern itself needs no import here: ``web/server.py``
hands the engine a plain callable (``WorkflowEngine.submit_guidance``) rather
than importing this module to construct one. ``web/server.py`` does still
import :func:`validate_guidance_text` below, so both the HTTP endpoint and
``resume --guidance`` share the one non-empty/length check — a small, one-way
dependency (``web`` → ``engine``, never the reverse) rather than a cycle.
"""

from __future__ import annotations

import asyncio

#: Shared bound for a single guidance submission, applied everywhere text
#: enters the run (``POST /api/guidance``, ``resume --guidance``) so no
#: entry point can push an unbounded string into the prompt/log.
MAX_GUIDANCE_CHARS = 10_000


def validate_guidance_text(text: str) -> str:
    """Strip and validate a guidance submission, returning the stripped text.

    This is the one place the "non-empty after stripping, at most
    :data:`MAX_GUIDANCE_CHARS` characters" invariant is enforced, so every
    guidance entry point shares it rather than each caller re-implementing
    (or forgetting) its own copy. ``POST /api/guidance`` and
    ``resume --guidance`` both call this before the text reaches the engine.

    Args:
        text: The raw guidance text (not yet stripped).

    Returns:
        The stripped text.

    Raises:
        ValueError: If ``text`` is empty after stripping, or exceeds
            ``MAX_GUIDANCE_CHARS``.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("guidance text must not be empty")
    if len(stripped) > MAX_GUIDANCE_CHARS:
        raise ValueError(f"guidance text exceeds maximum length of {MAX_GUIDANCE_CHARS} characters")
    return stripped


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
