"""Real-pty integration tests for KeyboardListener terminal restore (issue #290).

These tests allocate a genuine pseudo-terminal via ``pty.openpty()`` and point
fd 0 (and ``sys.stdin``) at its slave so the production ``KeyboardListener``
lifecycle (``termios.tcgetattr`` / ``tty.setcbreak`` / ``termios.tcsetattr``)
runs against a real tty instead of mocks. This is the only reliable way to
reproduce the bug where a listener started on an already-cbreak terminal
snapshots cbreak as its "original" settings and later restores them.

Expected state on unfixed code (TDD red):

- ``test_full_lifecycle_restores_terminal``            — PASSES (regression guard)
- ``test_second_listener_does_not_capture_cbreak``     — FAILS
- ``test_double_start_same_instance_does_not_corrupt_baseline`` — FAILS
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import pty
import sys
import termios
from collections.abc import Iterator

import pytest

from conductor.interrupt.listener import KeyboardListener

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="termios/pty is Unix-only")


class _PtyStdinShim:
    """Minimal stdin replacement that satisfies the KeyboardListener contract.

    pytest's capture replaces ``sys.stdin`` with a non-tty object, so the
    listener's reader thread (which calls ``sys.stdin.buffer.read(1)``) and
    ``select.select([sys.stdin], ...)`` both need a shim that:

    - reports ``isatty()`` as True,
    - returns fd 0 from ``fileno()`` (fd 0 is dup2'd at the pty slave),
    - exposes ``.buffer`` as a real buffered binary reader on fd 0.
    """

    def __init__(self) -> None:
        # Duplicate fd 0 so the shim owns its own handle to the pty slave and
        # closing the shim cannot disturb fd 0 itself.
        self._owned_fd = os.dup(0)
        self.buffer = io.BufferedReader(io.FileIO(self._owned_fd, "rb"))

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 0

    def close(self) -> None:
        self.buffer.close()


@contextlib.contextmanager
def _replace_stdin_with_pty() -> Iterator[int]:
    """Point fd 0 and ``sys.stdin`` at a fresh pty slave; yield the master fd.

    Restores fd 0 and ``sys.stdin`` in ``finally`` even on test failure, and
    closes the shim's buffer BEFORE closing the pty fds so no reader thread
    can block on a closed fd during teardown.
    """
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError as exc:
        pytest.skip(f"pty.openpty() unavailable in this environment: {exc}")

    saved_stdin = sys.stdin
    saved_fd = os.dup(0)
    shim: _PtyStdinShim | None = None
    try:
        os.dup2(slave_fd, 0)
        shim = _PtyStdinShim()
        sys.stdin = shim
        yield master_fd
    finally:
        # Restore sys.stdin first so nothing references the shim after close.
        sys.stdin = saved_stdin
        if shim is not None:
            shim.close()
        # Restore fd 0 to its pre-test target before closing anything else.
        os.dup2(saved_fd, 0)
        os.close(saved_fd)
        os.close(master_fd)
        os.close(slave_fd)


def _tty_flags() -> int:
    """Return the ICANON|ECHO subset of the current fd-0 local-mode flags."""
    return termios.tcgetattr(0)[3] & (termios.ICANON | termios.ECHO)


async def test_full_lifecycle_restores_terminal() -> None:
    """Requirement: start -> suspend -> resume -> stop restores the terminal.

    Regression guard: a single listener driven through its full lifecycle on a
    real pty must leave ICANON|ECHO exactly as found. PASSES on unfixed code.
    """
    with _replace_stdin_with_pty():
        baseline = _tty_flags()
        listener = KeyboardListener(interrupt_event=asyncio.Event())
        try:
            await listener.start()
            # Prove the listener actually started against the pty.
            assert listener._task is not None
            assert listener._reader_thread is not None

            await listener.suspend()
            await listener.resume()
            assert listener._task is not None
            assert listener._reader_thread is not None

            await listener.stop()
        finally:
            await listener.stop()

        assert _tty_flags() == baseline, (
            f"tty flags after full lifecycle differ from baseline: "
            f"{_tty_flags():#x} != {baseline:#x}"
        )


async def test_second_listener_does_not_capture_cbreak() -> None:
    """Requirement: a second listener must not snapshot cbreak as baseline.

    Repro for issue #290: with listener A active (terminal in cbreak), starting
    listener B currently snapshots the cbreak settings as B's "original". If A
    is then stopped first, B's later stop() restores its cbreak snapshot,
    leaving the terminal in cbreak after both listeners have stopped.

    Stop order matters: A BEFORE B (reverse order masks the bug because B's
    stop would run while A's correct settings were the last ones applied).

    FAILS on unfixed code; PASSES once start() reuses the process-wide
    pre-listener baseline instead of re-snapshotting the live tty.
    """
    with _replace_stdin_with_pty():
        baseline = _tty_flags()
        listener_a = KeyboardListener(interrupt_event=asyncio.Event())
        listener_b = KeyboardListener(interrupt_event=asyncio.Event())
        try:
            await listener_a.start()
            assert listener_a._task is not None
            assert listener_a._reader_thread is not None

            # Start B WITHOUT stopping A — B sees an already-cbreak terminal.
            await listener_b.start()
            assert listener_b._task is not None
            assert listener_b._reader_thread is not None

            # Stop A first, then B (documented order above).
            await listener_a.stop()
            await listener_b.stop()
        finally:
            await listener_a.stop()
            await listener_b.stop()

        assert _tty_flags() == baseline, (
            f"tty flags after stopping both listeners differ from baseline: "
            f"{_tty_flags():#x} != {baseline:#x} "
            f"(listener B captured cbreak as its original settings)"
        )


async def test_double_start_same_instance_does_not_corrupt_baseline() -> None:
    """Requirement: calling start() twice on one listener keeps the baseline.

    Repro for issue #290: a second start() on the SAME instance currently
    overwrites ``_original_settings`` with a snapshot of the cbreak terminal
    the first start() installed. The later stop() then restores cbreak,
    leaving the terminal broken.

    FAILS on unfixed code; PASSES once start() is idempotent w.r.t. baseline
    capture (reuse the process-wide pre-listener baseline).
    """
    with _replace_stdin_with_pty():
        baseline = _tty_flags()
        listener = KeyboardListener(interrupt_event=asyncio.Event())
        try:
            await listener.start()
            assert listener._task is not None
            assert listener._reader_thread is not None

            # Second start on the SAME instance must not clobber the baseline.
            await listener.start()
            assert listener._task is not None
            assert listener._reader_thread is not None

            await listener.stop()
        finally:
            await listener.stop()

        assert _tty_flags() == baseline, (
            f"tty flags after double-start+stop differ from baseline: "
            f"{_tty_flags():#x} != {baseline:#x} "
            f"(second start() captured cbreak as the original settings)"
        )
