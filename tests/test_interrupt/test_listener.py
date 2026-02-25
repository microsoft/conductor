"""Unit tests for KeyboardListener."""

from __future__ import annotations

import asyncio
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

        with patch.object(listener, "_read_byte_blocking", side_effect=mock_read):
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
