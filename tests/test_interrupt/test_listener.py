"""Unit tests for KeyboardListener."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from typing import Any, cast
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
                0, mock_termios.TCSANOW, original_settings
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

        threadsafe_args: list[tuple[Any, ...]] = []
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

    def test_restore_keeps_settings_on_termios_error_for_retry(
        self, listener: KeyboardListener
    ) -> None:
        """Verify a failed restore keeps the baseline so it can be retried.

        Requirement: a transient restore failure must not lose the only
        correct baseline — the saved settings stay in place so a later
        atexit/SIGTERM/stop() attempt can retry the restore (issue #290).
        """
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

        assert listener._original_settings is not None
        # Drop the fake baseline so the registered atexit handler is a no-op
        listener._original_settings = None


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


class TestBaselineCacheAndIdempotentStart:
    """Tests for the module-level baseline cache and idempotent start (issue #290).

    Red phase: the terminal baseline must be captured once per process and
    cached at module level; repeated ``start()`` calls must not re-read it.
    """

    @pytest.mark.asyncio
    async def test_module_baseline_captured_once_per_process(
        self, interrupt_event: asyncio.Event
    ) -> None:
        # Requirement: termios.tcgetattr is called exactly once per process,
        # no matter how many KeyboardListener instances start (issue #290).
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

            listener_a = KeyboardListener(interrupt_event=interrupt_event)
            await listener_a.start()
            mock_termios.tcgetattr.assert_called_once()

            mock_termios.reset_mock()

            listener_b = KeyboardListener(interrupt_event=interrupt_event)
            await listener_b.start()
            mock_termios.tcgetattr.assert_not_called()

            await listener_a.stop()
            await listener_b.stop()

    @pytest.mark.asyncio
    async def test_idempotent_start_is_noop_when_active(
        self, interrupt_event: asyncio.Event
    ) -> None:
        # Requirement: calling start() on an already-active listener is a no-op
        # — no tcgetattr/setcbreak, no new reader thread (issue #290).
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

            listener = KeyboardListener(interrupt_event=interrupt_event)
            await listener.start()
            first_thread = listener._reader_thread

            mock_termios.reset_mock()
            mock_tty.reset_mock()

            await listener.start()

            mock_termios.tcgetattr.assert_not_called()
            mock_tty.setcbreak.assert_not_called()
            assert listener._reader_thread is first_thread

            await listener.stop()

    @pytest.mark.asyncio
    async def test_start_after_suspend_restarts_listener(
        self, interrupt_event: asyncio.Event
    ) -> None:
        # Requirement: start() after suspend() re-enters cbreak and spawns a
        # new reader thread, but reuses the cached baseline — tcgetattr must
        # NOT be called again (issue #290).
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

            listener = KeyboardListener(interrupt_event=interrupt_event)
            await listener.start()
            await listener.suspend()

            mock_termios.reset_mock()
            mock_tty.reset_mock()

            await listener.start()

            mock_tty.setcbreak.assert_called_once_with(0)
            assert listener._reader_thread is not None
            mock_termios.tcgetattr.assert_not_called()

            await listener.stop()

    @pytest.mark.asyncio
    async def test_suspend_keeps_baseline_for_resume(self, interrupt_event: asyncio.Event) -> None:
        # Requirement: suspend() keeps _original_settings for resume(), and
        # resume() re-enters cbreak without re-capturing the baseline
        # (issue #290; regression guard — passes on current code).
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

            listener = KeyboardListener(interrupt_event=interrupt_event)
            await listener.start()
            captured = listener._original_settings
            assert captured is not None

            await listener.suspend()
            assert listener._original_settings is captured

            mock_termios.reset_mock()

            await listener.resume()
            mock_termios.tcgetattr.assert_not_called()

            await listener.stop()
            # Drop the fake baseline so the registered atexit handler is a no-op
            listener._original_settings = None

    @pytest.mark.asyncio
    async def test_suspend_restores_with_tcsanow(self, interrupt_event: asyncio.Event) -> None:
        # Requirement: suspend() restores the terminal with TCSANOW so the
        # human gate gets a sane terminal immediately — TCSADRAIN would wait
        # for pending output and can race with the gate's first read
        # (issue #290).
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

            listener = KeyboardListener(interrupt_event=interrupt_event)
            await listener.start()
            mock_termios.reset_mock()

            await listener.suspend()

            mock_termios.tcsetattr.assert_called_once_with(
                0, mock_termios.TCSANOW, listener._original_settings
            )

            await listener.stop()

    def test_restore_clears_baseline_only_on_success(self, interrupt_event: asyncio.Event) -> None:
        # Requirement: _restore_terminal() clears _original_settings only
        # after a successful tcsetattr; on termios.error the baseline must be
        # kept so a later restore can retry (issue #290).
        mock_termios = MagicMock()
        mock_termios.error = OSError
        mock_termios.tcsetattr.side_effect = [OSError("terminal gone"), None]
        saved_settings = [1, 2, 3]

        listener = KeyboardListener(interrupt_event=interrupt_event)
        listener._original_settings = saved_settings

        with (
            patch("sys.stdin") as mock_stdin,
            patch.dict("sys.modules", {"termios": mock_termios}),
        ):
            mock_stdin.fileno.return_value = 0

            listener._restore_terminal()
            assert listener._original_settings is saved_settings

            listener._restore_terminal()
            assert listener._original_settings is None


class TestSigtermHandlerDelegation:
    """Tests for SIGTERM handler delegation semantics (issue #290).

    Red phase: the current ``_sigterm_handler`` closure restores the terminal
    and optionally calls a callable previous handler, but it does NOT re-raise
    when the previous disposition was ``SIG_DFL`` — the process silently
    survives SIGTERM, leaving the terminal in cbreak. The fixed implementation
    must restore, reset to SIG_DFL, and re-raise via ``os.kill``.
    """

    def test_sigterm_handler_restores_then_re_raises_when_previous_was_dfl(
        self, listener: KeyboardListener
    ) -> None:
        # Requirement: when the previous SIGTERM disposition is SIG_DFL, the
        # handler must (1) restore the terminal, (2) reset the disposition
        # back to SIG_DFL, and (3) re-raise SIGTERM to self via os.kill so
        # the process actually terminates with the default action (issue #290).
        import os
        import signal as signal_module

        captured_handler: list[tuple[int, object]] = []

        def fake_signal(signum: int, handler):  # type: ignore[no-untyped-def]
            captured_handler.append((signum, handler))

        with (
            patch("signal.getsignal", return_value=signal_module.SIG_DFL),
            patch("signal.signal", side_effect=fake_signal),
            patch("os.kill") as mock_kill,
        ):
            listener._original_settings = [1, 2, 3]
            listener._register_cleanup_handlers()

            assert len(captured_handler) == 1
            registered_signum, registered_handler = captured_handler[0]
            registered_handler = cast(Callable[[int, object], None], registered_handler)
            assert registered_signum == signal_module.SIGTERM

            registered_handler(signal_module.SIGTERM, None)

            assert listener._original_settings is None

            reset_calls = [
                (s, h)
                for (s, h) in captured_handler[1:]
                if s == signal_module.SIGTERM and h == signal_module.SIG_DFL
            ]
            assert len(reset_calls) == 1, (
                "handler did not reset SIGTERM disposition to SIG_DFL before re-raising"
            )

            mock_kill.assert_called_once_with(os.getpid(), signal_module.SIGTERM)

        listener._original_settings = None
        listener._atexit_registered = False

    def test_sigterm_handler_noop_when_previous_was_ign(self, listener: KeyboardListener) -> None:
        # Requirement: when the previous SIGTERM disposition is SIG_IGN, the
        # handler must restore the terminal but must NOT re-raise (the caller
        # explicitly asked to ignore SIGTERM) and must NOT touch the
        # disposition again (issue #290).
        import signal as signal_module

        captured_handler: list[tuple[int, object]] = []

        def fake_signal(signum: int, handler):  # type: ignore[no-untyped-def]
            captured_handler.append((signum, handler))

        with (
            patch("signal.getsignal", return_value=signal_module.SIG_IGN),
            patch("signal.signal", side_effect=fake_signal),
            patch("os.kill") as mock_kill,
        ):
            listener._original_settings = [1, 2, 3]
            listener._register_cleanup_handlers()

            assert len(captured_handler) == 1
            registered_signum, registered_handler = captured_handler[0]
            registered_handler = cast(Callable[[int, object], None], registered_handler)
            assert registered_signum == signal_module.SIGTERM

            registered_handler(signal_module.SIGTERM, None)

            assert listener._original_settings is None

            mock_kill.assert_not_called()
            assert len(captured_handler) == 1, (
                "handler must not modify SIGTERM disposition when previous was SIG_IGN"
            )

        listener._original_settings = None
        listener._atexit_registered = False

    def test_sigterm_handler_calls_callable_previous(self, listener: KeyboardListener) -> None:
        # Requirement: when the previous SIGTERM disposition is a callable,
        # the handler must restore the terminal first and then delegate to
        # the callable with the same (signum, frame) arguments (issue #290).
        import signal as signal_module

        previous = MagicMock()
        captured_handler: list[tuple[int, object]] = []

        def fake_signal(signum: int, handler):  # type: ignore[no-untyped-def]
            captured_handler.append((signum, handler))

        with (
            patch("signal.getsignal", return_value=previous),
            patch("signal.signal", side_effect=fake_signal),
            patch("os.kill"),
        ):
            listener._original_settings = [1, 2, 3]
            listener._register_cleanup_handlers()

            assert len(captured_handler) == 1
            _, registered_handler = captured_handler[0]
            registered_handler = cast(Callable[[int, object], None], registered_handler)

            registered_handler(signal_module.SIGTERM, None)

            assert listener._original_settings is None

            previous.assert_called_once_with(signal_module.SIGTERM, None)

        listener._original_settings = None
        listener._atexit_registered = False

    def test_register_cleanup_handlers_does_not_self_recurse(
        self, listener: KeyboardListener
    ) -> None:
        # Requirement: calling ``_register_cleanup_handlers`` twice on the same
        # instance must NOT re-capture the previously-installed own handler as
        # ``_previous_sigterm`` (that would produce unbounded self-recursion
        # when the handler delegates). The second call must detect that our
        # own handler is still installed and skip re-registration (issue #290).
        import signal as signal_module

        captured_handler: list[tuple[int, object]] = []

        def fake_signal(signum: int, handler):  # type: ignore[no-untyped-def]
            captured_handler.append((signum, handler))

        with (
            patch("signal.getsignal", return_value=signal_module.SIG_DFL),
            patch("signal.signal", side_effect=fake_signal),
            patch("os.kill"),
        ):
            listener._register_cleanup_handlers()

            assert len(captured_handler) == 1
            _, h1 = captured_handler[0]
            h1 = cast(Callable[[int, object], None], h1)

            # Interim shim: typed local keeps ruff/basedpyright green until T5
            # adds the dataclass field (commit 2 converts to direct access).
            listener_any: Any = listener
            listener_any._sigterm_handler = h1

        with (
            patch("signal.getsignal", return_value=h1),
            patch("signal.signal", side_effect=fake_signal) as mock_signal,
            patch("os.kill"),
        ):
            listener._register_cleanup_handlers()

            mock_signal.assert_not_called()
            assert len(captured_handler) == 1, (
                "second _register_cleanup_handlers call must not re-register "
                "SIGTERM handler (would self-capture and recurse)"
            )

        listener._original_settings = None
        listener._atexit_registered = False

    def test_new_listener_registers_own_handler_after_stopped_listener(
        self, interrupt_event: asyncio.Event
    ) -> None:
        # Requirement: when a second KeyboardListener starts after a first one
        # was stopped (``stop()`` does not remove signal handlers), the new
        # listener must install its OWN handler — not skip registration just
        # because a conductor SIGTERM closure is currently installed. The new
        # handler must also restore its own terminal before delegating
        # (issue #290).
        import signal as signal_module

        listener_a = KeyboardListener(interrupt_event=interrupt_event)
        listener_b = KeyboardListener(interrupt_event=interrupt_event)

        captured_a: list[tuple[int, object]] = []

        def fake_signal_a(signum: int, handler):  # type: ignore[no-untyped-def]
            captured_a.append((signum, handler))

        with (
            patch("signal.getsignal", return_value=signal_module.SIG_DFL),
            patch("signal.signal", side_effect=fake_signal_a),
            patch("os.kill"),
        ):
            listener_a._register_cleanup_handlers()

        assert len(captured_a) == 1
        _, h_a = captured_a[0]
        h_a = cast(Callable[[int, object], None], h_a)

        # Interim shim: typed local keeps ruff/basedpyright green until T5
        # adds the dataclass field (commit 2 converts to direct access).
        listener_a_any: Any = listener_a
        listener_a_any._sigterm_handler = h_a

        captured_b: list[tuple[int, object]] = []

        def fake_signal_b(signum: int, handler):  # type: ignore[no-untyped-def]
            captured_b.append((signum, handler))

        with (
            patch("signal.getsignal", return_value=h_a),
            patch("signal.signal", side_effect=fake_signal_b),
            patch("os.kill"),
        ):
            listener_b._register_cleanup_handlers()

        assert len(captured_b) == 1, (
            "new listener must install its own SIGTERM handler even when "
            "a stale conductor handler from another instance is still installed"
        )
        _, h_b = captured_b[0]
        h_b = cast(Callable[[int, object], None], h_b)
        assert h_b is not h_a, "new listener must not reuse the other instance's handler"

        # Interim shim: typed local keeps ruff/basedpyright green until T5
        # adds the dataclass field (commit 2 converts to direct access).
        listener_b_any: Any = listener_b
        assert listener_b_any._sigterm_handler is h_b

        call_order: list[str] = []

        original_a_restore = listener_a._restore_terminal
        original_b_restore = listener_b._restore_terminal

        def spy_a() -> None:
            call_order.append("A")
            original_a_restore()

        def spy_b() -> None:
            call_order.append("B")
            original_b_restore()

        listener_a._original_settings = [1, 1, 1]
        listener_b._original_settings = [2, 2, 2]

        with (
            patch.object(listener_a, "_restore_terminal", side_effect=spy_a),
            patch.object(listener_b, "_restore_terminal", side_effect=spy_b),
            patch("os.kill"),
        ):
            h_b(signal_module.SIGTERM, None)

        assert call_order, "H_B must at minimum restore B's terminal"
        assert call_order[0] == "B", (
            f"B's own terminal must be restored before any delegation; got call order {call_order}"
        )

        listener_a._original_settings = None
        listener_a._atexit_registered = False
        listener_b._original_settings = None
        listener_b._atexit_registered = False
