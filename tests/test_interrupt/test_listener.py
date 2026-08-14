"""Unit tests for KeyboardListener."""

from __future__ import annotations

import asyncio
import contextlib
import os
import select
import signal
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from conductor.interrupt.listener import (
    _CTRL_G_BYTE,
    _ESC_BYTE,
    _ESC_DISAMBIGUATE_TIMEOUT,
    KeyboardListener,
)


@pytest.fixture
def interrupt_event() -> asyncio.Event:
    """Create an asyncio Event for interrupt signaling."""
    return asyncio.Event()


@pytest.fixture
def listener(interrupt_event: asyncio.Event) -> KeyboardListener:
    """Create a KeyboardListener instance."""
    return KeyboardListener(interrupt_event=interrupt_event)


class TestKeyboardListenerInit:
    """Tests for KeyboardListener initialization."""

    def test_init_stores_event(self, interrupt_event: asyncio.Event) -> None:
        """Verify the listener stores the interrupt event."""
        listener = KeyboardListener(interrupt_event=interrupt_event)
        assert listener.interrupt_event is interrupt_event

    def test_init_defaults(self, listener: KeyboardListener) -> None:
        """Verify default field values."""
        assert listener._original_settings is None
        assert listener._task is None
        assert listener._stop_flag is False
        assert listener._loop is None


class TestKeyboardListenerStartStop:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_not_tty_is_noop(self, listener: KeyboardListener) -> None:
        """Verify listener is a no-op when stdin is not a TTY."""
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            await listener.start()
            assert listener._task is None
            assert listener._original_settings is None

    @pytest.mark.asyncio
    async def test_start_no_termios_is_noop(self, listener: KeyboardListener) -> None:
        """Verify listener is a no-op when termios is unavailable."""
        with (
            patch("sys.stdin") as mock_stdin,
            patch("conductor.interrupt.listener.sys") as mock_sys,
        ):
            mock_stdin.isatty.return_value = True
            mock_sys.stdin = mock_stdin
            # Simulate ImportError for termios
            import builtins

            original_import = builtins.__import__

            def mock_import(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
                if name in ("termios", "tty"):
                    raise ImportError(f"No module named '{name}'")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=mock_import):
                await listener.start()
                assert listener._task is None

    @pytest.mark.asyncio
    async def test_start_sets_cbreak_and_creates_task(self, listener: KeyboardListener) -> None:
        """Verify start enters cbreak mode and creates listen task."""
        mock_termios = MagicMock()
        mock_tty = MagicMock()
        mock_termios.tcgetattr.return_value = [1, 2, 3]
        mock_termios.error = OSError

        with (
            patch("sys.stdin") as mock_stdin,
            patch.dict("sys.modules", {"termios": mock_termios, "tty": mock_tty}),
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0

            await listener.start()

            assert listener._original_settings == [1, 2, 3]
            mock_tty.setcbreak.assert_called_once_with(0)
            assert listener._task is not None
            assert listener._loop is not None
            assert listener._reader_thread is not None

            # Cleanup
            await listener.stop()

    @pytest.mark.asyncio
    async def test_stop_restores_terminal(self, listener: KeyboardListener) -> None:
        """Verify stop restores original terminal settings."""
        mock_termios = MagicMock()
        mock_termios.error = OSError
        original_settings = [1, 2, 3]
        listener._original_settings = original_settings

        with (
            patch("sys.stdin") as mock_stdin,
            patch.dict("sys.modules", {"termios": mock_termios}),
        ):
            mock_stdin.fileno.return_value = 0
            listener._restore_terminal()

            mock_termios.tcsetattr.assert_called_once_with(
                0, mock_termios.TCSADRAIN, original_settings
            )
            assert listener._original_settings is None

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, listener: KeyboardListener) -> None:
        """Verify stop cancels the listen task."""

        # Create a simple task that waits forever
        async def wait_forever() -> None:
            await asyncio.sleep(9999)

        listener._task = asyncio.create_task(wait_forever())
        listener._stop_flag = False

        await listener.stop()

        assert listener._task is None
        assert listener._stop_flag is True


class TestKeyboardListenerDetection:
    """Tests for key detection logic.

    These tests feed bytes directly into the listener's queue to simulate
    the reader thread delivering keypress data.
    """

    @pytest.mark.asyncio
    async def test_ctrl_g_sets_event(self, interrupt_event: asyncio.Event) -> None:
        """Verify Ctrl+G (0x07) sets the interrupt event immediately."""
        listener = KeyboardListener(interrupt_event=interrupt_event)
        listener._loop = asyncio.get_running_loop()
        listener._stop_flag = False

        # Feed Ctrl+G then None (stop) into the queue
        listener._byte_queue.put_nowait(_CTRL_G_BYTE)
        listener._byte_queue.put_nowait(None)

        await listener._listen_loop()
        # Allow event loop to process call_soon_threadsafe callbacks
        await asyncio.sleep(0)

        assert interrupt_event.is_set()

    @pytest.mark.asyncio
    async def test_bare_esc_sets_event(self, interrupt_event: asyncio.Event) -> None:
        """Verify bare Esc (0x1b with no follow-up) sets the interrupt event."""
        listener = KeyboardListener(interrupt_event=interrupt_event)
        listener._loop = asyncio.get_running_loop()
        listener._stop_flag = False

        # Feed Esc only — the queue will be empty after that,
        # causing _read_byte_async to timeout (bare Esc)
        listener._byte_queue.put_nowait(_ESC_BYTE)

        async def stop_after_delay() -> None:
            await asyncio.sleep(0.15)
            listener._stop_flag = True
            listener._byte_queue.put_nowait(None)

        asyncio.create_task(stop_after_delay())

        await listener._listen_loop()

        assert interrupt_event.is_set()

    @pytest.mark.asyncio
    async def test_arrow_key_does_not_set_event(self, interrupt_event: asyncio.Event) -> None:
        """Verify arrow key sequence (0x1b 0x5b 0x41) does NOT set the event."""
        listener = KeyboardListener(interrupt_event=interrupt_event)
        listener._loop = asyncio.get_running_loop()
        listener._stop_flag = False

        # Arrow up: ESC [ A (0x1b 0x5b 0x41)
        listener._byte_queue.put_nowait(_ESC_BYTE)
        listener._byte_queue.put_nowait(0x5B)
        listener._byte_queue.put_nowait(0x41)
        listener._byte_queue.put_nowait(None)

        await listener._listen_loop()

        assert not interrupt_event.is_set()

    @pytest.mark.asyncio
    async def test_function_key_does_not_set_event(self, interrupt_event: asyncio.Event) -> None:
        """Verify F1 key (ESC O P) does NOT set the event."""
        listener = KeyboardListener(interrupt_event=interrupt_event)
        listener._loop = asyncio.get_running_loop()
        listener._stop_flag = False

        # F1: ESC O P (0x1b 0x4f 0x50)
        listener._byte_queue.put_nowait(_ESC_BYTE)
        listener._byte_queue.put_nowait(0x4F)
        listener._byte_queue.put_nowait(0x50)
        listener._byte_queue.put_nowait(None)

        await listener._listen_loop()

        assert not interrupt_event.is_set()

    @pytest.mark.asyncio
    async def test_regular_keys_ignored(self, interrupt_event: asyncio.Event) -> None:
        """Verify regular key presses are ignored."""
        listener = KeyboardListener(interrupt_event=interrupt_event)
        listener._loop = asyncio.get_running_loop()
        listener._stop_flag = False

        for key in [ord("a"), ord("b"), ord("c")]:
            listener._byte_queue.put_nowait(key)
        listener._byte_queue.put_nowait(None)

        await listener._listen_loop()

        assert not interrupt_event.is_set()


class TestEscDisambiguation:
    """Tests for Esc vs escape sequence disambiguation."""

    @pytest.mark.asyncio
    async def test_disambiguate_timeout_returns_true(self, interrupt_event: asyncio.Event) -> None:
        """Verify timeout (no follow-up) returns True (bare Esc)."""
        listener = KeyboardListener(interrupt_event=interrupt_event)
        listener._loop = asyncio.get_running_loop()

        # Queue is empty — _read_byte_async will timeout
        result = await listener._disambiguate_esc()

        assert result is True

    @pytest.mark.asyncio
    async def test_disambiguate_csi_returns_false(self, interrupt_event: asyncio.Event) -> None:
        """Verify CSI sequence start (0x5b) returns False."""
        listener = KeyboardListener(interrupt_event=interrupt_event)
        listener._loop = asyncio.get_running_loop()

        # Simulate CSI: [ then A (arrow up final byte)
        listener._byte_queue.put_nowait(0x5B)
        listener._byte_queue.put_nowait(0x41)

        result = await listener._disambiguate_esc()

        assert result is False

    @pytest.mark.asyncio
    async def test_disambiguate_ss3_returns_false(self, interrupt_event: asyncio.Event) -> None:
        """Verify SS3 sequence start (0x4f) returns False."""
        listener = KeyboardListener(interrupt_event=interrupt_event)
        listener._loop = asyncio.get_running_loop()

        listener._byte_queue.put_nowait(0x4F)
        listener._byte_queue.put_nowait(0x50)  # SS3 P (F1)

        result = await listener._disambiguate_esc()

        assert result is False


class TestConsumeCSISequence:
    """Tests for CSI sequence consumption."""

    @pytest.mark.asyncio
    async def test_consume_simple_csi(self, interrupt_event: asyncio.Event) -> None:
        """Verify simple CSI sequence is consumed (e.g., arrow key)."""
        listener = KeyboardListener(interrupt_event=interrupt_event)
        listener._loop = asyncio.get_running_loop()

        # Arrow up final byte: A (0x41)
        listener._byte_queue.put_nowait(0x41)

        await listener._consume_csi_sequence()

        assert listener._byte_queue.empty()

    @pytest.mark.asyncio
    async def test_consume_extended_csi(self, interrupt_event: asyncio.Event) -> None:
        """Verify extended CSI sequence with intermediate bytes is consumed."""
        listener = KeyboardListener(interrupt_event=interrupt_event)
        listener._loop = asyncio.get_running_loop()

        # Extended CSI: intermediate bytes (0x31 '1'), semicolon (0x3B ';'),
        # then final byte (0x7E '~')
        for byte_val in [0x31, 0x3B, 0x32, 0x7E]:
            listener._byte_queue.put_nowait(byte_val)

        await listener._consume_csi_sequence()

        assert listener._byte_queue.empty()


class TestThreadSafety:
    """Tests for thread-safe event signaling."""

    @pytest.mark.asyncio
    async def test_call_soon_threadsafe_used_for_ctrl_g(
        self, interrupt_event: asyncio.Event
    ) -> None:
        """Verify call_soon_threadsafe is used when Ctrl+G detected."""
        listener = KeyboardListener(interrupt_event=interrupt_event)
        loop = asyncio.get_running_loop()
        listener._loop = loop
        listener._stop_flag = False

        listener._byte_queue.put_nowait(_CTRL_G_BYTE)
        listener._byte_queue.put_nowait(None)

        threadsafe_args: list[tuple] = []
        original_call = loop.call_soon_threadsafe

        def tracking_call(*args, **kwargs):  # type: ignore[no-untyped-def]
            threadsafe_args.append(args)
            return original_call(*args, **kwargs)

        with patch.object(loop, "call_soon_threadsafe", side_effect=tracking_call):
            await listener._listen_loop()

        # Allow event loop to process call_soon_threadsafe callbacks
        await asyncio.sleep(0)

        # Verify event.set was passed to call_soon_threadsafe
        event_set_calls = [a for a in threadsafe_args if len(a) > 0 and a[0].__name__ == "set"]
        assert len(event_set_calls) == 1
        assert interrupt_event.is_set()


class TestReaderThread:
    """Tests for the dedicated reader thread."""

    @pytest.mark.asyncio
    async def test_reader_thread_populates_queue(self, interrupt_event: asyncio.Event) -> None:
        """Verify the reader thread puts bytes into the async queue."""
        listener = KeyboardListener(interrupt_event=interrupt_event)
        loop = asyncio.get_running_loop()
        listener._loop = loop
        listener._stop_flag = False

        bytes_to_read = [ord("x"), ord("y")]
        read_count = 0

        def mock_read() -> int | None:
            nonlocal read_count
            if read_count < len(bytes_to_read):
                val = bytes_to_read[read_count]
                read_count += 1
                return val
            listener._stop_flag = True
            return None

        with (
            patch.object(listener, "_read_byte_blocking", side_effect=mock_read),
            patch("conductor.interrupt.listener.select") as mock_select,
        ):
            # Make select always report stdin as ready
            mock_select.select.return_value = ([sys.stdin], [], [])
            listener._reader_thread_main()

        # Allow event loop to process call_soon_threadsafe callbacks
        await asyncio.sleep(0)

        # Queue should have the bytes plus a trailing None
        results = []
        while not listener._byte_queue.empty():
            results.append(listener._byte_queue.get_nowait())
        assert results == [ord("x"), ord("y"), None]

    @pytest.mark.asyncio
    async def test_reader_thread_stops_on_flag(self, interrupt_event: asyncio.Event) -> None:
        """Verify the reader thread stops when stop_flag is set."""
        listener = KeyboardListener(interrupt_event=interrupt_event)
        listener._loop = asyncio.get_running_loop()
        listener._stop_flag = True

        # Should return immediately without reading
        with patch.object(listener, "_read_byte_blocking") as mock_read:
            listener._reader_thread_main()
            mock_read.assert_not_called()


class TestRestoreTerminal:
    """Tests for terminal restoration."""

    def test_restore_with_no_settings_is_noop(self, listener: KeyboardListener) -> None:
        """Verify restore is a no-op when no settings were saved."""
        listener._original_settings = None
        listener._restore_terminal()  # Should not raise
        assert listener._original_settings is None

    def test_restore_clears_original_settings(self, listener: KeyboardListener) -> None:
        """Verify restore clears the saved settings after restoring."""
        mock_termios = MagicMock()
        mock_termios.error = OSError
        listener._original_settings = [1, 2, 3]

        with (
            patch("sys.stdin") as mock_stdin,
            patch.dict("sys.modules", {"termios": mock_termios}),
        ):
            mock_stdin.fileno.return_value = 0
            listener._restore_terminal()

        assert listener._original_settings is None

    def test_restore_handles_termios_error_gracefully(self, listener: KeyboardListener) -> None:
        """Verify restore handles termios errors without raising."""
        mock_termios = MagicMock()
        mock_termios.error = OSError
        mock_termios.tcsetattr.side_effect = OSError("terminal gone")
        listener._original_settings = [1, 2, 3]

        with (
            patch("sys.stdin") as mock_stdin,
            patch.dict("sys.modules", {"termios": mock_termios}),
        ):
            mock_stdin.fileno.return_value = 0
            listener._restore_terminal()  # Should not raise

        assert listener._original_settings is None


class TestConstants:
    """Tests for module constants."""

    def test_esc_byte_value(self) -> None:
        """Verify ESC byte is 0x1b (27)."""
        assert _ESC_BYTE == 0x1B

    def test_ctrl_g_byte_value(self) -> None:
        """Verify Ctrl+G byte is 0x07 (7)."""
        assert _CTRL_G_BYTE == 0x07

    def test_disambiguate_timeout_value(self) -> None:
        """Verify Esc disambiguation timeout is 50ms."""
        assert _ESC_DISAMBIGUATE_TIMEOUT == 0.05


class TestSigtermHandlerFix:
    """Regression tests for Fleet Manager E3-T9 (the ``SIGTERM``-swallowing bug).

    Prior to this fix, ``_sigterm_handler`` restored the terminal and
    returned without re-raising whenever the previous handler was not
    callable -- the normal case, since ``signal.getsignal(SIGTERM)`` is
    ``signal.Handlers.SIG_DFL`` (an ``IntEnum`` member) in an unmodified
    process. That silently swallowed the ``SIGTERM``: the process survived
    and kept running forever, so ``conductor stop`` could never actually
    terminate a ``mode == "fg"`` run. See the correction in Open Question 1
    of ``docs/projects/fleet-manager/fleet-manager.plan.md``.
    """

    def test_reraises_default_disposition_when_previous_not_callable(
        self, listener: KeyboardListener
    ) -> None:
        """SIG_DFL (not callable) must restore the default disposition and
        re-signal the process rather than silently returning."""
        with (
            patch(
                "conductor.interrupt.listener.signal.getsignal",
                return_value=signal.Handlers.SIG_DFL,
            ),
            patch("conductor.interrupt.listener.signal.signal") as mock_signal,
            patch("conductor.interrupt.listener.os.kill") as mock_kill,
            patch.object(listener, "_restore_terminal") as mock_restore,
        ):
            listener._register_cleanup_handlers()
            # The handler actually registered via signal.signal(SIGTERM, ...).
            registered_handler = mock_signal.call_args_list[-1].args[1]
            registered_handler(signal.SIGTERM, None)

        mock_restore.assert_called_once()
        # Restores the default disposition, then re-raises against itself.
        assert mock_signal.call_args_list[-1].args == (signal.SIGTERM, signal.SIG_DFL)
        mock_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)

    def test_inherited_sig_ign_is_respected_not_overridden(
        self, listener: KeyboardListener
    ) -> None:
        """An inherited SIG_IGN means "this process does not die on SIGTERM".

        It is an ``IntEnum`` member like SIG_DFL, so the not-callable branch
        would otherwise take the re-raise path and terminate a process a
        supervisor or container init shim explicitly configured not to.
        """
        with (
            patch(
                "conductor.interrupt.listener.signal.getsignal",
                return_value=signal.Handlers.SIG_IGN,
            ),
            patch("conductor.interrupt.listener.signal.signal") as mock_signal,
            patch("conductor.interrupt.listener.os.kill") as mock_kill,
            patch.object(listener, "_restore_terminal") as mock_restore,
        ):
            listener._register_cleanup_handlers()
            registered_handler = mock_signal.call_args_list[-1].args[1]
            registered_handler(signal.SIGTERM, None)

        # The terminal is still restored -- only the termination is skipped.
        mock_restore.assert_called_once()
        mock_kill.assert_not_called()
        assert mock_signal.call_args_list[-1].args[1] is not signal.SIG_DFL

    def test_delegates_to_previous_handler_when_callable(self, listener: KeyboardListener) -> None:
        """A real, callable previous handler (e.g. installed by another
        library) must still be invoked -- this behavior is unchanged."""
        previous_handler = MagicMock()
        with (
            patch(
                "conductor.interrupt.listener.signal.getsignal",
                return_value=previous_handler,
            ),
            patch("conductor.interrupt.listener.signal.signal") as mock_signal,
            patch("conductor.interrupt.listener.os.kill") as mock_kill,
            patch.object(listener, "_restore_terminal") as mock_restore,
        ):
            listener._register_cleanup_handlers()
            registered_handler = mock_signal.call_args_list[-1].args[1]
            registered_handler(signal.SIGTERM, "frame")

        mock_restore.assert_called_once()
        previous_handler.assert_called_once_with(signal.SIGTERM, "frame")
        mock_kill.assert_not_called()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM/PTY semantics are POSIX-only")
class TestSigtermActuallyTerminatesForegroundRun:
    """Empirical, PTY-backed regression test for Fleet Manager E3-T9 / E3-T11.

    ``KeyboardListener.start()`` only installs the ``SIGTERM`` handler when
    ``sys.stdin.isatty()`` is true, which is exactly the condition
    ``cli/run.py`` uses to decide whether to install a real listener
    (``mode == "fg"``). A mocked stdin can't reproduce the real swallowing
    bug because the handler only misbehaves once actually installed and
    signaled against a real process -- so this spawns a genuine child
    process attached to a pseudo-terminal (mirroring the reviewer's
    empirical repro described in Open Question 1) and verifies it actually
    exits when sent ``SIGTERM``, rather than hanging forever.
    """

    _CHILD_SCRIPT = (
        "import asyncio\n"
        "from conductor.interrupt.listener import KeyboardListener\n"
        "async def main():\n"
        "    event = asyncio.Event()\n"
        "    listener = KeyboardListener(interrupt_event=event)\n"
        "    await listener.start()\n"
        "    print('READY', flush=True)\n"
        "    await asyncio.Event().wait()\n"
        "asyncio.run(main())\n"
    )

    @staticmethod
    def _wait_for_ready(master_fd: int, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        buf = b""
        while b"READY" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"child process never signaled readiness (got: {buf!r})")
            ready, _, _ = select.select([master_fd], [], [], remaining)
            if not ready:
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk

    @staticmethod
    def _wait_for_exit(pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            done_pid, _status = os.waitpid(pid, os.WNOHANG)
            if done_pid == pid:
                return True
            time.sleep(0.05)
        # Timed out -- force-kill so the test doesn't leak a zombie/orphan
        # process regardless of the assertion outcome below.
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)
        return False

    def test_process_exits_on_sigterm(self) -> None:
        import pty

        pid, master_fd = pty.fork()
        if pid == 0:  # pragma: no cover -- runs in the forked child
            os.execvp(sys.executable, [sys.executable, "-c", self._CHILD_SCRIPT])
            os._exit(127)

        try:
            self._wait_for_ready(master_fd)
            os.kill(pid, signal.SIGTERM)
            exited = self._wait_for_exit(pid, timeout=10.0)
        finally:
            with contextlib.suppress(OSError):
                os.close(master_fd)

        assert exited, "child process ignored SIGTERM and did not exit (E3-T9 regression)"
