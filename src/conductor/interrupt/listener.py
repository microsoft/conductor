"""Async keyboard listener for interrupt detection.

This module provides the KeyboardListener class that detects Esc and Ctrl+G
keypresses asynchronously and signals them via an asyncio.Event. It handles
Esc vs ANSI escape sequence disambiguation using a 50ms read-ahead timeout.

Uses a dedicated daemon thread for blocking stdin reads, delivering bytes
into an ``asyncio.Queue`` via ``loop.call_soon_threadsafe``. This avoids
thread leaks from abandoned ``run_in_executor`` futures.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import select
import signal
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Key codes
_ESC_BYTE = 0x1B
_CTRL_G_BYTE = 0x07

# Timeout for disambiguating bare Esc from escape sequences (seconds)
_ESC_DISAMBIGUATE_TIMEOUT = 0.05


@dataclass
class KeyboardListener:
    """Async terminal keypress listener for interrupt detection.

    Puts the terminal into cbreak mode and listens for Esc (0x1b) and
    Ctrl+G (0x07). When detected, sets an ``asyncio.Event``.

    For Esc key disambiguation: waits 50ms after receiving 0x1b. If no
    follow-up bytes arrive, it is a bare Esc press. If follow-up bytes
    arrive (e.g., 0x5b for arrow keys), the sequence is discarded.

    A dedicated daemon thread performs blocking stdin reads and delivers
    bytes via an ``asyncio.Queue`` (using ``loop.call_soon_threadsafe``).
    The listen loop reads from this queue with native async operations,
    avoiding thread leaks from ``run_in_executor`` + ``wait_for`` timeouts.

    Example:
        >>> event = asyncio.Event()
        >>> listener = KeyboardListener(interrupt_event=event)
        >>> await listener.start()
        >>> # ... event will be set when Esc or Ctrl+G is pressed
        >>> await listener.stop()
    """

    interrupt_event: asyncio.Event
    """Event that is set when an interrupt key is detected."""

    _original_settings: Any = field(default=None, repr=False)
    """Saved terminal settings for restoration."""

    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    """The asyncio task running the listen loop."""

    _stop_flag: bool = field(default=False, repr=False)
    """Flag to signal the listen loop to stop."""

    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    """Reference to the event loop for thread-safe signaling."""

    _atexit_registered: bool = field(default=False, repr=False)
    """Whether the atexit handler has been registered."""

    _previous_sigterm: Any = field(default=None, repr=False)
    """Previous SIGTERM handler for restoration."""

    _byte_queue: asyncio.Queue[int | None] = field(default_factory=asyncio.Queue, repr=False)
    """Async queue for delivering bytes from the reader thread."""

    _reader_thread: threading.Thread | None = field(default=None, repr=False)
    """Dedicated daemon thread for blocking stdin reads."""

    async def start(self) -> None:
        """Enter cbreak mode and begin listening for keypresses.

        Stores the event loop reference for thread-safe signaling.
        Only activates on Unix systems with a TTY stdin.
        """
        if not sys.stdin.isatty():
            logger.debug("stdin is not a TTY, keyboard listener not started")
            return

        try:
            import termios
            import tty
        except ImportError:
            logger.debug("termios/tty not available (non-Unix), listener not started")
            return

        self._loop = asyncio.get_running_loop()
        self._stop_flag = False

        # Save original terminal settings
        try:
            self._original_settings = termios.tcgetattr(sys.stdin.fileno())
        except termios.error:
            logger.debug("Failed to get terminal settings, listener not started")
            return

        # Enter cbreak mode (not full raw mode, preserves output processing)
        try:
            tty.setcbreak(sys.stdin.fileno())
        except termios.error:
            logger.debug("Failed to set cbreak mode, listener not started")
            self._original_settings = None
            return

        # Register cleanup handlers
        self._register_cleanup_handlers()

        # Reset the queue
        self._byte_queue = asyncio.Queue()

        # Start the dedicated reader thread
        self._reader_thread = threading.Thread(
            target=self._reader_thread_main, daemon=True, name="keyboard-listener"
        )
        self._reader_thread.start()

        # Start the listen loop as an asyncio task
        self._task = asyncio.create_task(self._listen_loop())
        logger.debug("Keyboard listener started")

    async def stop(self) -> None:
        """Stop listening and restore terminal settings."""
        self._stop_flag = True

        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        # Join the reader thread to ensure it exits before interpreter shutdown.
        # The select()-based polling in _reader_thread_main checks _stop_flag
        # every 100ms, so the thread should exit within that window.
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=0.5)
            self._reader_thread = None

        self._restore_terminal()
        logger.debug("Keyboard listener stopped")

    def _restore_terminal(self) -> None:
        """Restore original terminal settings."""
        if self._original_settings is not None:
            try:
                import termios

                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._original_settings)
            except (ImportError, termios.error, ValueError, OSError):
                pass
            self._original_settings = None

    def _register_cleanup_handlers(self) -> None:
        """Register atexit and SIGTERM handlers for crash safety."""
        if not self._atexit_registered:
            atexit.register(self._restore_terminal)
            self._atexit_registered = True

        # Install SIGTERM handler that restores terminal then re-raises
        try:
            self._previous_sigterm = signal.getsignal(signal.SIGTERM)

            def _sigterm_handler(signum: int, frame: Any) -> None:
                self._restore_terminal()
                # Call previous handler if it was callable
                if callable(self._previous_sigterm):
                    self._previous_sigterm(signum, frame)

            signal.signal(signal.SIGTERM, _sigterm_handler)
        except (OSError, ValueError):
            # Can't set signal handler (not main thread, etc.)
            pass

    def _reader_thread_main(self) -> None:
        """Dedicated daemon thread that reads stdin bytes into the async queue.

        Uses ``select()`` with a 100ms timeout to poll stdin, allowing the
        thread to check ``_stop_flag`` periodically and exit cleanly on
        shutdown. This prevents the thread from holding a lock on
        ``sys.stdin.buffer`` during interpreter finalization.

        Uses ``loop.call_soon_threadsafe`` to safely deliver bytes to the
        asyncio queue from this thread.
        """
        assert self._loop is not None

        while not self._stop_flag:
            # Poll stdin with a short timeout so we can check _stop_flag
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.1)
            except (OSError, ValueError):
                # stdin closed or invalid
                break

            if not ready:
                # Timeout — no data, loop back to check _stop_flag
                continue

            byte_val = self._read_byte_blocking()
            try:
                self._loop.call_soon_threadsafe(self._byte_queue.put_nowait, byte_val)
            except RuntimeError:
                # Event loop is closed
                break
            if byte_val is None:
                break

    async def _listen_loop(self) -> None:
        """Process bytes from the async queue and detect interrupt keys.

        On receiving 0x1b, waits 50ms for follow-up bytes to disambiguate
        bare Esc from ANSI escape sequences. Uses
        ``loop.call_soon_threadsafe(event.set)`` for safe signaling.
        """
        assert self._loop is not None

        try:
            while not self._stop_flag:
                byte_val = await self._byte_queue.get()

                if byte_val is None:
                    break

                if byte_val == _CTRL_G_BYTE:
                    # Ctrl+G: immediate interrupt
                    self._loop.call_soon_threadsafe(self.interrupt_event.set)
                    logger.debug("Ctrl+G detected, interrupt event set")

                elif byte_val == _ESC_BYTE:
                    # Could be bare Esc or start of escape sequence
                    # Wait 50ms for follow-up bytes
                    is_bare_esc = await self._disambiguate_esc()
                    if is_bare_esc:
                        self._loop.call_soon_threadsafe(self.interrupt_event.set)
                        logger.debug("Bare Esc detected, interrupt event set")

                # Other bytes are ignored

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Keyboard listener loop exited with exception", exc_info=True)

    def _read_byte_blocking(self) -> int | None:
        """Blocking single-byte read from stdin.

        Only called from the dedicated reader thread.

        Returns:
            The byte value read, or None if stop flag is set or read fails.
        """
        if self._stop_flag:
            return None

        try:
            data = sys.stdin.buffer.read(1)
            if data:
                return data[0]
            return None
        except (OSError, ValueError):
            return None

    async def _disambiguate_esc(self) -> bool:
        """Disambiguate bare Esc from ANSI escape sequences.

        Waits 50ms for follow-up bytes after receiving 0x1b. If no bytes
        arrive, it is a bare Esc. If bytes arrive (e.g., 0x5b for CSI),
        the sequence is consumed and discarded.

        Returns:
            True if this was a bare Esc press, False if it was an escape sequence.
        """
        try:
            next_byte = await asyncio.wait_for(
                self._byte_queue.get(),
                timeout=_ESC_DISAMBIGUATE_TIMEOUT,
            )
        except TimeoutError:
            # No follow-up byte within 50ms: bare Esc
            return True

        if next_byte is None:
            # Read failed or stop flag set: treat as bare Esc
            return True

        # Follow-up byte arrived: this is an escape sequence
        # Consume remaining bytes of the sequence
        if next_byte == 0x5B:
            # CSI sequence (e.g., arrow keys): read until final byte (0x40-0x7E)
            await self._consume_csi_sequence()
        elif next_byte == 0x4F:
            # SS3 sequence (e.g., F1-F4): read one more byte
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._byte_queue.get(),
                    timeout=_ESC_DISAMBIGUATE_TIMEOUT,
                )
        # Other escape sequences (Alt+key, etc.) are just 2 bytes total

        return False

    async def _consume_csi_sequence(self) -> None:
        """Consume remaining bytes of a CSI escape sequence.

        CSI sequences start with ESC [ and end with a byte in the range
        0x40-0x7E (e.g., A for up arrow, B for down, C for right, D for left).
        Intermediate bytes are in the range 0x20-0x3F.
        """
        while True:
            try:
                byte_val = await asyncio.wait_for(
                    self._byte_queue.get(),
                    timeout=_ESC_DISAMBIGUATE_TIMEOUT,
                )
            except TimeoutError:
                break

            if byte_val is None:
                break

            # CSI final bytes are in range 0x40-0x7E
            if 0x40 <= byte_val <= 0x7E:
                break
