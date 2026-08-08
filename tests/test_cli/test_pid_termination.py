"""Direct tests for the process-termination primitives (issue #344).

``conductor stop``'s ladder is only as trustworthy as these functions: they
decide whether a process is confirmed dead, and they are the code that actually
issues ``SIGKILL`` / ``TerminateProcess``. Getting them wrong means either
reporting a live run as stopped (the original bug) or killing a process that
was never ours.

The ladder tests in ``test_stop_ladder.py`` patch these out to keep the control
flow readable and fast, which leaves the primitives themselves unexercised.
This module covers them directly, including every Windows branch — the Windows
implementation is driven through a mocked ``_kernel32`` so it runs on Linux CI
too, following the pattern already established in ``test_pid.py``.

What a mocked kernel32 cannot check is the real ctypes ABI (``argtypes`` /
``restype`` correctness). That needs a Windows job running the suite, which CI
does not currently have.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from conductor.cli.pid import (
    _STILL_ACTIVE,
    _TERMINATION_EXIT_CODE,
    Liveness,
    _terminate_process_posix,
    _terminate_process_windows,
    process_liveness,
    terminate_process,
    wait_for_exit,
)

_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_ERROR_INVALID_PARAMETER = 87
_ERROR_ACCESS_DENIED = 5


def _kernel32_mock(
    *,
    open_handle: int = 0xDEADBEEF,
    terminate_ok: bool = True,
    wait_result: int = _WAIT_OBJECT_0,
) -> MagicMock:
    """A ``kernel32`` stand-in for the termination path."""
    k = MagicMock(name="kernel32")
    k.OpenProcess.return_value = open_handle
    k.TerminateProcess.return_value = terminate_ok
    k.WaitForSingleObject.return_value = wait_result
    k.CloseHandle.return_value = True
    return k


class TestProcessLivenessDispatch:
    """``process_liveness`` selects the right implementation per platform."""

    def test_dispatches_to_posix_on_non_windows(self) -> None:
        with (
            patch("conductor.cli.pid.sys.platform", "linux"),
            patch("conductor.cli.pid._liveness_posix", return_value=Liveness.ALIVE) as posix,
            patch("conductor.cli.pid._liveness_windows") as win,
        ):
            assert process_liveness(42) is Liveness.ALIVE
            posix.assert_called_once_with(42)
            win.assert_not_called()

    def test_dispatches_to_windows_on_win32(self) -> None:
        with (
            patch("conductor.cli.pid.sys.platform", "win32"),
            patch("conductor.cli.pid._liveness_windows", return_value=Liveness.DEAD) as win,
            patch("conductor.cli.pid._liveness_posix") as posix,
        ):
            assert process_liveness(42) is Liveness.DEAD
            win.assert_called_once_with(42)
            posix.assert_not_called()


class TestWaitForExit:
    """The bounded wait between ladder rungs."""

    def test_returns_immediately_when_already_dead(self) -> None:
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.DEAD) as probe,
            patch("conductor.cli.pid.time.sleep") as sleep,
        ):
            assert wait_for_exit(42, timeout=5.0) is Liveness.DEAD
        # No sleeping, and no second probe: a dead process is dead.
        sleep.assert_not_called()
        probe.assert_called_once_with(42)

    def test_returns_dead_as_soon_as_the_process_exits(self) -> None:
        with (
            patch(
                "conductor.cli.pid.process_liveness",
                side_effect=[Liveness.ALIVE, Liveness.ALIVE, Liveness.DEAD],
            ),
            patch("conductor.cli.pid.time.sleep"),
        ):
            assert wait_for_exit(42, timeout=5.0) is Liveness.DEAD

    def test_returns_alive_when_the_timeout_expires(self) -> None:
        """A survivor must be reported as such, not optimistically as dead."""
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE),
            patch("conductor.cli.pid.time.sleep"),
            # monotonic: start, then a value past the deadline.
            patch("conductor.cli.pid.time.monotonic", side_effect=[0.0, 99.0]),
        ):
            assert wait_for_exit(42, timeout=5.0) is Liveness.ALIVE

    def test_unknown_is_preserved_not_coerced(self) -> None:
        """UNKNOWN must survive the wait so the caller can refuse to act."""
        with (
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.UNKNOWN),
            patch("conductor.cli.pid.time.sleep"),
            patch("conductor.cli.pid.time.monotonic", side_effect=[0.0, 99.0]),
        ):
            assert wait_for_exit(42, timeout=5.0) is Liveness.UNKNOWN


class TestTerminateProcessDispatch:
    """``terminate_process`` selects the right implementation per platform."""

    def test_dispatches_to_posix_on_non_windows(self) -> None:
        with (
            patch("conductor.cli.pid.sys.platform", "linux"),
            patch(
                "conductor.cli.pid._terminate_process_posix", return_value=Liveness.DEAD
            ) as posix,
            patch("conductor.cli.pid._terminate_process_windows") as win,
        ):
            assert terminate_process(42, 1.0) is Liveness.DEAD
            posix.assert_called_once_with(42, 1.0)
            win.assert_not_called()

    def test_dispatches_to_windows_on_win32(self) -> None:
        with (
            patch("conductor.cli.pid.sys.platform", "win32"),
            patch(
                "conductor.cli.pid._terminate_process_windows", return_value=Liveness.DEAD
            ) as win,
            patch("conductor.cli.pid._terminate_process_posix") as posix,
        ):
            assert terminate_process(42, 1.0) is Liveness.DEAD
            win.assert_called_once_with(42, 1.0)
            posix.assert_not_called()


@pytest.fixture
def sigkill(monkeypatch: pytest.MonkeyPatch) -> int:
    """Make ``signal.SIGKILL`` resolvable on every platform.

    ``_terminate_process_posix`` is POSIX-only in production, but the logic
    (which errors mean dead, which mean "probe instead") is worth exercising
    everywhere rather than only on Linux CI. Windows has no ``SIGKILL``, so it
    is injected; on POSIX the real value is used unchanged.
    """
    import signal

    existing = getattr(signal, "SIGKILL", None)
    if existing is not None:
        return int(existing)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    return 9


class TestTerminateProcessPosix:
    """SIGKILL, and the three ways it can fail to land."""

    def test_kills_and_confirms(self, sigkill: int) -> None:
        with (
            patch("conductor.cli.pid.os.kill") as kill,
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.DEAD) as wait,
        ):
            assert _terminate_process_posix(42, 2.0) is Liveness.DEAD
        # SIGKILL specifically — SIGTERM is the previous rung and is ignorable.
        kill.assert_called_once_with(42, sigkill)
        wait.assert_called_once_with(42, 2.0)

    def test_survivor_is_reported_alive(self, sigkill: int) -> None:
        with (
            patch("conductor.cli.pid.os.kill"),
            patch("conductor.cli.pid.wait_for_exit", return_value=Liveness.ALIVE),
        ):
            assert _terminate_process_posix(42, 2.0) is Liveness.ALIVE

    def test_already_gone_is_dead_without_waiting(self, sigkill: int) -> None:
        with (
            patch("conductor.cli.pid.os.kill", side_effect=ProcessLookupError),
            patch("conductor.cli.pid.wait_for_exit") as wait,
        ):
            assert _terminate_process_posix(42, 2.0) is Liveness.DEAD
        wait.assert_not_called()

    def test_permission_denied_falls_back_to_a_probe(self, sigkill: int) -> None:
        """We could not kill it, so we must report what we can observe —
        never DEAD, which would let the caller delete a live run's PID file."""
        with (
            patch("conductor.cli.pid.os.kill", side_effect=PermissionError),
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.ALIVE) as probe,
        ):
            assert _terminate_process_posix(42, 2.0) is Liveness.ALIVE
        probe.assert_called_once_with(42)

    def test_unexpected_oserror_falls_back_to_a_probe(self, sigkill: int) -> None:
        with (
            patch("conductor.cli.pid.os.kill", side_effect=OSError(11, "boom")),
            patch("conductor.cli.pid.process_liveness", return_value=Liveness.UNKNOWN),
        ):
            assert _terminate_process_posix(42, 2.0) is Liveness.UNKNOWN


class TestTerminateProcessWindows:
    """Every branch of the Win32 path, driven through a mocked kernel32."""

    def test_terminates_and_confirms_via_the_wait(self) -> None:
        k = _kernel32_mock(open_handle=0x1000, wait_result=_WAIT_OBJECT_0)
        with patch("conductor.cli.pid._kernel32", k):
            assert _terminate_process_windows(42, 2.0) is Liveness.DEAD
        k.TerminateProcess.assert_called_once_with(0x1000, _TERMINATION_EXIT_CODE)
        k.CloseHandle.assert_called_once_with(0x1000)

    def test_terminate_and_wait_use_the_same_handle(self) -> None:
        """The security property this design depends on.

        An open handle pins the PID, so the kernel cannot recycle it onto a
        different process midway through. Re-probing by PID after terminating
        would be fooled by exactly that; waiting on the retained handle cannot.
        """
        k = _kernel32_mock(open_handle=0xABCD)
        with patch("conductor.cli.pid._kernel32", k):
            _terminate_process_windows(42, 2.0)

        opened = k.OpenProcess.return_value
        assert k.TerminateProcess.call_args.args[0] == opened
        assert k.WaitForSingleObject.call_args.args[0] == opened
        # Only one handle was ever acquired.
        k.OpenProcess.assert_called_once()

    def test_requests_terminate_and_synchronize_rights(self) -> None:
        """SYNCHRONIZE is what makes ``WaitForSingleObject`` usable; without it
        the wait fails and the confirmation silently degrades."""
        from conductor.cli.pid import _PROCESS_TERMINATE, _SYNCHRONIZE

        k = _kernel32_mock()
        with patch("conductor.cli.pid._kernel32", k):
            _terminate_process_windows(42, 2.0)

        access = k.OpenProcess.call_args.args[0]
        assert access & _PROCESS_TERMINATE
        assert access & _SYNCHRONIZE

    def test_timeout_reports_alive(self) -> None:
        k = _kernel32_mock(wait_result=_WAIT_TIMEOUT)
        with patch("conductor.cli.pid._kernel32", k):
            assert _terminate_process_windows(42, 2.0) is Liveness.ALIVE

    def test_unexpected_wait_result_is_unknown_not_survived(self) -> None:
        k = _kernel32_mock(wait_result=0xFFFFFFFF)
        with patch("conductor.cli.pid._kernel32", k):
            assert _terminate_process_windows(42, 2.0) is Liveness.UNKNOWN

    def test_no_such_process_is_dead(self) -> None:
        k = _kernel32_mock(open_handle=0)
        with (
            patch("conductor.cli.pid._kernel32", k),
            patch(
                "conductor.cli.pid.ctypes.get_last_error",
                create=True,
                return_value=_ERROR_INVALID_PARAMETER,
            ),
        ):
            assert _terminate_process_windows(42, 2.0) is Liveness.DEAD
        k.TerminateProcess.assert_not_called()

    def test_open_denied_falls_back_to_a_probe(self) -> None:
        """Cannot terminate it, but can still say whether it is running."""
        k = _kernel32_mock(open_handle=0)
        with (
            patch("conductor.cli.pid._kernel32", k),
            patch(
                "conductor.cli.pid.ctypes.get_last_error",
                create=True,
                return_value=_ERROR_ACCESS_DENIED,
            ),
            # ``ctypes.FormatError`` is Windows-only; the warning path would
            # otherwise raise AttributeError on Linux CI.
            patch("conductor.cli.pid.ctypes.FormatError", create=True, return_value="msg"),
            patch("conductor.cli.pid._liveness_windows", return_value=Liveness.ALIVE) as probe,
        ):
            assert _terminate_process_windows(42, 2.0) is Liveness.ALIVE
        probe.assert_called_once_with(42)
        k.TerminateProcess.assert_not_called()

    def test_terminate_failure_falls_back_to_a_probe(self) -> None:
        k = _kernel32_mock(open_handle=0x1000, terminate_ok=False)
        with (
            patch("conductor.cli.pid._kernel32", k),
            patch("conductor.cli.pid.ctypes.get_last_error", create=True, return_value=5),
            patch("conductor.cli.pid.ctypes.FormatError", create=True, return_value="msg"),
            patch("conductor.cli.pid._liveness_windows", return_value=Liveness.ALIVE),
        ):
            assert _terminate_process_windows(42, 2.0) is Liveness.ALIVE
        k.WaitForSingleObject.assert_not_called()
        # The handle must still be released on the failure path.
        k.CloseHandle.assert_called_once_with(0x1000)

    def test_handle_is_closed_even_when_the_wait_is_unexpected(self) -> None:
        k = _kernel32_mock(open_handle=0x1000, wait_result=0xDEAD)
        with patch("conductor.cli.pid._kernel32", k):
            _terminate_process_windows(42, 2.0)
        k.CloseHandle.assert_called_once_with(0x1000)

    def test_timeout_is_converted_to_milliseconds(self) -> None:
        k = _kernel32_mock()
        with patch("conductor.cli.pid._kernel32", k):
            _terminate_process_windows(42, 2.5)
        assert k.WaitForSingleObject.call_args.args[1] == 2500

    def test_negative_timeout_does_not_become_infinite(self) -> None:
        """``INFINITE`` is 0xFFFFFFFF; a negative timeout must clamp to 0 rather
        than wrap into a wait that never returns and hangs ``conductor stop``."""
        k = _kernel32_mock()
        with patch("conductor.cli.pid._kernel32", k):
            _terminate_process_windows(42, -1.0)
        assert k.WaitForSingleObject.call_args.args[1] == 0


def test_termination_exit_code_is_not_the_still_active_sentinel() -> None:
    """Otherwise a follow-up probe would read the process we just killed as
    still running forever (``GetExitCodeProcess`` returns 259 for both)."""
    assert _TERMINATION_EXIT_CODE != _STILL_ACTIVE
