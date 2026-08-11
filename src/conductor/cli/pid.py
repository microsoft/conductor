"""PID file utilities for tracking background workflow processes.

When ``conductor run --web-bg`` launches a detached child process, a PID file is
written to ``~/.conductor/runs/`` so that ``conductor stop`` can discover and
terminate it later.  The child process removes its own PID file on exit.

PID files are JSON with the schema::

    {
        "pid": 12345,
        "port": 8080,
        "workflow": "my-workflow.yaml",
        "started_at": "2026-03-03T12:00:00"
    }
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import time
from ctypes import wintypes
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

_PID_DIR_NAME = "runs"


class Liveness(str, Enum):
    """Tri-state result of a process-liveness probe.

    ``_is_process_alive`` collapses three genuinely different states into a
    bool, which is safe for *listing* (where "assume alive" protects a live
    run's PID file) but not for *terminating*: a caller that is about to
    escalate to ``TerminateProcess`` must be able to tell "the process is
    confirmed running" from "the probe failed and we have no idea".

    Attributes:
        ALIVE: The process is confirmed to exist.
        DEAD: The process is confirmed gone.
        UNKNOWN: The probe failed. Callers must not treat this as either.
    """

    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Windows process-liveness probing
#
# ``os.kill(pid, 0)`` is the standard Unix probe but is unsafe on Windows: per
# the CPython docs, any signal value other than ``CTRL_C_EVENT`` /
# ``CTRL_BREAK_EVENT`` routes through ``TerminateProcess`` and may also raise
# ``OSError`` subclasses that the POSIX-style branches don't anticipate (e.g.
# ``WinError 11`` / ``ERROR_BAD_FORMAT``, see issue #166).  We use
# ``OpenProcess`` + ``GetExitCodeProcess`` instead.
#
# All ctypes setup is hoisted to module level so it runs once per process,
# matches the codebase's ``if sys.platform == "win32":`` idiom (see
# ``cli/app.py``, ``cli/update.py``, ``cli/bg_runner.py``), and gives tests a
# single ``_kernel32`` symbol to monkey-patch from any platform.
# --------------------------------------------------------------------------- #

# Process access right that doesn't require administrative privileges.
# ``PROCESS_QUERY_LIMITED_INFORMATION`` is granted by a more permissive default
# DACL than ``PROCESS_QUERY_INFORMATION`` and is the minimum right that
# satisfies ``GetExitCodeProcess``.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# Sentinel exit code returned by ``GetExitCodeProcess`` for a process that has
# not yet exited.  Defined as ``STILL_ACTIVE`` (alias of ``STATUS_PENDING`` =
# ``0x103``) in the Windows SDK ``WinBase.h``.
#
# Known footgun: a process that legitimately exits with status code 259 is
# indistinguishable from one that is still running.  Microsoft's documented
# workaround is ``WaitForSingleObject(handle, 0)``.  We accept this ambiguity
# because conductor child processes do not exit with code 259 in practice; the
# worst case is a stale PID-file entry that the user can remove manually.
_STILL_ACTIVE = 259
# Specific ``OpenProcess`` failure codes we interpret.  Any other failure is
# treated as "unknown — assume alive" so that a transient OS hiccup doesn't
# silently delete a still-running workflow's PID file.
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87

# Access rights and wait results used by the forceful-termination path.
# ``PROCESS_TERMINATE`` is required by ``TerminateProcess``; ``SYNCHRONIZE``
# lets us wait on the *same* handle afterwards rather than re-probing by PID.
# That distinction matters: a retained handle cannot be recycled, so waiting on
# it proves *this* process died rather than "some process with that PID is now
# gone" (see issue #344).
_PROCESS_TERMINATE = 0x0001
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
# Exit code reported for a process we terminated. Arbitrary but non-zero, and
# distinct from ``_STILL_ACTIVE`` so a subsequent probe reads it as dead.
_TERMINATION_EXIT_CODE = 1

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
else:
    # On non-Windows the kernel32 wrapper is unused in production but tests
    # patch this symbol to exercise the Windows code path on every platform.
    _kernel32 = None


def pid_dir() -> Path:
    """Return the directory used for PID files, creating it if needed.

    Returns:
        Path to ``~/.conductor/runs/``.
    """
    d = Path.home() / ".conductor" / _PID_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_pid_file(
    pid: int,
    port: int,
    workflow_path: str | Path,
    run_id: str = "",
    log_file: str = "",
) -> Path:
    """Write a PID file for a background workflow process.

    Args:
        pid: Process ID of the background child.
        port: TCP port the web dashboard is listening on.
        workflow_path: Path to the workflow YAML file.
        run_id: Unique run identifier (from event log subscriber).
        log_file: Path to the JSONL event log file.

    Returns:
        Path to the created PID file.
    """
    workflow_name = Path(workflow_path).stem
    filename = f"{workflow_name}-{port}.pid"
    filepath = pid_dir() / filename

    data = {
        "pid": pid,
        "port": port,
        "workflow": str(workflow_path),
        "started_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "log_file": log_file,
    }

    # Written atomically. ``write_text`` truncates and then streams, so a reader
    # landing inside that window sees a partial file — and every reader here
    # treats unparseable JSON as a dead run and unlinks it, which silently
    # deregisters a live workflow. Rename is atomic on POSIX and on Windows for
    # a same-directory replace, so a reader sees either the old file or the new
    # one and never a half-written one. The temp name ends in ``.tmp`` so it is
    # not picked up by the ``*.pid`` glob in :func:`read_pid_files`.
    tmp = filepath.with_name(f"{filepath.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, filepath)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    logger.debug("Wrote PID file: %s", filepath)
    return filepath


def scan_pid_files() -> list[dict]:
    """Read every PID file **without modifying anything on disk**.

    :func:`read_pid_files` is the maintenance path: it prunes as it goes, which
    is right for ``stop`` and wrong for anything whose contract is to observe.
    A reader that deletes turns a diagnostic command into the one that loses the
    run it was asked about — and because ``write_pid_file`` is not atomic, a
    scan landing inside a launch can see a half-written file and treat a live
    workflow as garbage.

    Malformed entries are skipped rather than raised on: one unparseable file in
    the directory must not take down the listing of every other run. Entries
    without an integer ``pid`` and ``port`` are skipped too, because every
    caller indexes both.

    Returns:
        List of dicts for processes that are still alive, in filename order,
        each with the PID file's contents plus ``file``.
    """
    d = pid_dir()
    results: list[dict] = []

    # Sorted rather than raw glob order: the listing is user-facing, and
    # ``Path.glob`` order is filesystem-dependent.
    for f in sorted(d.glob("*.pid")):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping unreadable PID file %s: %s", f, exc)
            continue

        if not isinstance(data, dict):
            logger.warning("Skipping PID file whose contents are not an object: %s", f)
            continue

        pid = data.get("pid")
        port = data.get("port")
        if not isinstance(pid, int) or not isinstance(port, int):
            logger.warning("Skipping PID file without an integer pid and port: %s", f)
            continue

        if _is_process_alive(pid):
            data["file"] = str(f)
            results.append(data)

    return results


def read_pid_files() -> list[dict]:
    """Read all PID files and return info for processes that are still alive.

    Stale PID files (where the process no longer exists) are automatically
    cleaned up.

    Returns:
        List of dicts with keys ``pid``, ``port``, ``workflow``,
        ``started_at``, and ``file`` (the PID file path).
    """
    d = pid_dir()
    results: list[dict] = []

    for f in d.glob("*.pid"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            # Deleting a file we could not read is a destructive act taken on
            # incomplete information, so it says so. The write side is atomic
            # now, which removes the common cause, but a genuinely corrupt file
            # still costs someone a background run they will want to explain.
            logger.warning("Removing unreadable PID file %s: %s", f, exc)
            f.unlink(missing_ok=True)
            continue

        pid = data.get("pid")
        if pid is None:
            logger.warning("Removing PID file with no 'pid' field: %s", f)
            f.unlink(missing_ok=True)
            continue

        if _is_process_alive(pid):
            data["file"] = str(f)
            results.append(data)
        else:
            # Process is gone — clean up stale PID file
            logger.debug("Cleaning up stale PID file: %s (PID %s)", f, pid)
            f.unlink(missing_ok=True)

    return results


def remove_pid_file(port: int) -> bool:
    """Remove the PID file for a given port.

    .. deprecated::
        Matching on port alone is racy: between the caller's snapshot and this
        call, the original run can exit and a *new* run can bind the same port
        and write its own PID file — which this would then delete, orphaning a
        live workflow (issue #344). ``conductor stop`` uses
        :func:`remove_pid_file_at` instead. This remains for compatibility with
        any external caller.

    Args:
        port: The web dashboard port to match.

    Returns:
        True if a PID file was found and removed, False otherwise.
    """
    d = pid_dir()
    for f in d.glob("*.pid"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("port") == port:
            f.unlink(missing_ok=True)
            logger.debug("Removed PID file: %s", f)
            return True
    return False


def remove_pid_file_at(path: str | Path, expected_pid: int) -> bool:
    """Remove a specific PID file, but only if it still describes ``expected_pid``.

    Identity is re-read immediately before unlinking rather than trusted from
    the caller's snapshot. Without that re-read, a run that exits mid-``stop``
    can be replaced by a new run reusing the same port and filename, and the
    unlink would silently deregister the *new* run — leaving a live, untracked
    workflow burning tokens with no way to find it (issue #344).

    Args:
        path: Path to the PID file, as recorded in :func:`read_pid_files`.
        expected_pid: The PID the caller believes the file describes.

    Returns:
        True if the file was removed, False if it was absent, unreadable, or
        now describes a different process.
    """
    f = Path(path)
    try:
        data = json.loads(f.read_text())
    except FileNotFoundError:
        # Already cleaned up — most likely the child removed its own file on
        # exit, which is the normal cooperative path.
        return False
    except (json.JSONDecodeError, OSError):
        logger.debug("PID file %s unreadable during removal; leaving in place", f)
        return False

    if data.get("pid") != expected_pid:
        logger.warning(
            "Refusing to remove PID file %s: expected PID %s but file now describes %s. "
            "A different run has taken over this slot.",
            f,
            expected_pid,
            data.get("pid"),
        )
        return False

    f.unlink(missing_ok=True)
    logger.debug("Removed PID file: %s", f)
    return True


def remove_pid_file_for_current_process() -> bool:
    """Find and remove the PID file matching the current process.

    This is called by the background child process on exit to clean up
    its own PID file.

    Returns:
        True if a PID file was found and removed, False otherwise.
    """
    current_pid = os.getpid()
    d = pid_dir()

    for f in d.glob("*.pid"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("pid") == current_pid:
            f.unlink(missing_ok=True)
            logger.debug("Removed PID file for current process (PID %s): %s", current_pid, f)
            return True
    return False


def _is_process_alive(pid: int) -> bool:
    """Check whether a process with the given PID is still running.

    This is the *listing* probe: it answers "should this PID file be kept?"
    and deliberately errs towards True. Callers that are about to terminate
    something must use :func:`process_liveness` instead, which distinguishes
    "confirmed alive" from "probe failed" (see issue #344).

    Args:
        pid: The process ID to check.

    Returns:
        True if the process appears to still exist, False if it is known to be
        gone. On any unexpected error this returns True so that ``conductor
        stop`` doesn't silently delete PID files for processes that may still
        be running.
    """
    if sys.platform == "win32":
        return _is_process_alive_windows(pid)
    return _is_process_alive_posix(pid)


def process_liveness(pid: int) -> Liveness:
    """Probe a process and report :class:`Liveness` without collapsing states.

    Dispatches to a platform-specific implementation. On POSIX systems this
    uses ``os.kill(pid, 0)`` to probe existence without sending a signal. On
    Windows it uses ``OpenProcess`` + ``GetExitCodeProcess`` because
    ``os.kill(pid, 0)`` is **not** a no-op probe on Windows — any signal value
    other than ``CTRL_C_EVENT`` / ``CTRL_BREAK_EVENT`` calls
    ``TerminateProcess`` and may also raise ``OSError`` subclasses that the
    POSIX-style branches don't anticipate (e.g. ``WinError 11`` /
    ``ERROR_BAD_FORMAT``).

    Args:
        pid: The process ID to check.

    Returns:
        :attr:`Liveness.ALIVE`, :attr:`Liveness.DEAD`, or
        :attr:`Liveness.UNKNOWN` when the probe itself failed.
    """
    if sys.platform == "win32":
        return _liveness_windows(pid)
    return _liveness_posix(pid)


def wait_for_exit(pid: int, timeout: float, interval: float = 0.1) -> Liveness:
    """Poll ``pid`` until it is confirmed dead or ``timeout`` elapses.

    Used between rungs of the stop ladder so that a graceful signal is given a
    bounded chance to work before escalating. A single re-probe is not enough:
    a workflow that has to flush a checkpoint takes a moment to exit, and
    declaring it "survived" too early escalates unnecessarily.

    Args:
        pid: The process ID to wait on.
        timeout: Maximum seconds to wait.
        interval: Seconds between probes.

    Returns:
        :attr:`Liveness.DEAD` as soon as the process is confirmed gone,
        otherwise the last observed liveness when the timeout expired.
    """
    deadline = time.monotonic() + timeout
    state = process_liveness(pid)
    while state is not Liveness.DEAD and time.monotonic() < deadline:
        time.sleep(interval)
        state = process_liveness(pid)
    return state


def terminate_process(pid: int, timeout: float = 2.0) -> Liveness:
    """Forcefully terminate ``pid`` and confirm the outcome.

    This is the last rung of the stop ladder and the only one that cannot be
    ignored by the target. Callers **must** confirm process identity before
    invoking it — a PID read from a stale file may since have been recycled
    onto an unrelated process (see issue #344).

    Args:
        pid: The process ID to terminate.
        timeout: Seconds to wait for the process to actually disappear.

    Returns:
        :attr:`Liveness.DEAD` if the process is confirmed gone,
        :attr:`Liveness.ALIVE` if it survived, or :attr:`Liveness.UNKNOWN` if
        the outcome could not be established.
    """
    if sys.platform == "win32":
        return _terminate_process_windows(pid, timeout)
    return _terminate_process_posix(pid, timeout)


def _terminate_process_posix(pid: int, timeout: float) -> Liveness:
    """POSIX implementation of :func:`terminate_process` using ``SIGKILL``."""
    import signal

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return Liveness.DEAD
    except PermissionError:
        logger.warning("SIGKILL to PID %s denied; cannot terminate", pid)
        return process_liveness(pid)
    except OSError:
        logger.warning("Unexpected OSError sending SIGKILL to PID %s", pid, exc_info=True)
        return process_liveness(pid)
    return wait_for_exit(pid, timeout)


def _terminate_process_windows(pid: int, timeout: float) -> Liveness:
    """Windows implementation of :func:`terminate_process`.

    Opens the process once with ``PROCESS_TERMINATE | SYNCHRONIZE`` and both
    terminates and waits on that **same handle**. Waiting on the retained
    handle is what makes the confirmation trustworthy: an open handle pins the
    PID, so the kernel cannot recycle it onto a different process midway
    through and fool a re-probe into reporting success.
    """
    assert _kernel32 is not None, "_terminate_process_windows requires _kernel32 to be initialised"
    handle = _kernel32.OpenProcess(_PROCESS_TERMINATE | _SYNCHRONIZE, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == _ERROR_INVALID_PARAMETER:
            # No such process — already gone.
            return Liveness.DEAD
        logger.warning(
            "OpenProcess(PID=%s) for termination failed with WinError %s (%s)",
            pid,
            err,
            ctypes.FormatError(err),
        )
        # Fall back to a plain probe: we could not terminate, but we can still
        # report whether it is running.
        return _liveness_windows(pid)

    try:
        if not _kernel32.TerminateProcess(handle, _TERMINATION_EXIT_CODE):
            err = ctypes.get_last_error()
            logger.warning(
                "TerminateProcess(PID=%s) failed with WinError %s (%s)",
                pid,
                err,
                ctypes.FormatError(err),
            )
            return _liveness_windows(pid)
        timeout_ms = max(0, int(timeout * 1000))
        result = _kernel32.WaitForSingleObject(handle, timeout_ms)
        if result == _WAIT_OBJECT_0:
            return Liveness.DEAD
        if result == _WAIT_TIMEOUT:
            return Liveness.ALIVE
        logger.warning("WaitForSingleObject(PID=%s) returned unexpected 0x%x", pid, result)
        return Liveness.UNKNOWN
    finally:
        _kernel32.CloseHandle(handle)


def _is_process_alive_posix(pid: int) -> bool:
    """Bool view of :func:`_liveness_posix` (see :func:`_is_process_alive`)."""
    return _liveness_posix(pid) is not Liveness.DEAD


def _is_process_alive_windows(pid: int) -> bool:
    """Bool view of :func:`_liveness_windows` (see :func:`_is_process_alive`)."""
    return _liveness_windows(pid) is not Liveness.DEAD


def _liveness_posix(pid: int) -> Liveness:
    """POSIX implementation of :func:`process_liveness` using ``os.kill``.

    Catches generic :class:`OSError` and reports :attr:`Liveness.UNKNOWN` so a
    transient OS failure doesn't crash ``conductor stop`` or silently drop a
    live workflow's PID file (regression for issue #166).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return Liveness.DEAD
    except PermissionError:
        # Process exists but we can't signal it — still alive.
        return Liveness.ALIVE
    except OSError:
        # Unknown error from the OS.  We log at warning so the user can see
        # why a phantom workflow is appearing in ``conductor stop`` listings.
        logger.warning(
            "Unexpected OSError checking PID %s; liveness unknown. "
            "The PID file in ~/.conductor/runs/ may need manual removal.",
            pid,
            exc_info=True,
        )
        return Liveness.UNKNOWN
    return Liveness.ALIVE


def _liveness_windows(pid: int) -> Liveness:
    """Windows implementation of :func:`process_liveness` using ctypes.

    Calls ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, ...)`` and then
    ``GetExitCodeProcess`` rather than ``os.kill(pid, 0)``, which on Windows
    is unsafe (it routes through ``TerminateProcess``) and can raise
    ``OSError`` subclasses such as ``WinError 11`` (``ERROR_BAD_FORMAT``).

    Limitations:

    - Relies on the ``STILL_ACTIVE`` (259) sentinel from ``GetExitCodeProcess``.
      A process that legitimately exits with status code 259 will be reported
      as still alive forever.  Microsoft's documented workaround is
      ``WaitForSingleObject(handle, 0)``; we accept the ambiguity because
      conductor child processes do not exit with code 259 in practice.
    """
    # In production this function is only reached when ``sys.platform ==
    # "win32"`` (so ``_kernel32`` is set); in tests the symbol is patched to a
    # MagicMock.  The assert narrows the type for ty / mypy and provides a
    # clear failure mode if the function is ever called incorrectly.
    assert _kernel32 is not None, "_liveness_windows requires _kernel32 to be initialised"
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == _ERROR_ACCESS_DENIED:
            # Process exists but we lack the rights to query it — treat as
            # alive.  This is an expected condition (e.g. cross-session or
            # higher-integrity targets) so a debug log is sufficient.
            logger.debug("OpenProcess(PID=%s) denied (ERROR_ACCESS_DENIED); assuming alive", pid)
            return Liveness.ALIVE
        if err == _ERROR_INVALID_PARAMETER:
            # No process with that PID exists.
            return Liveness.DEAD
        # Any other failure is unexpected.  Don't crash; report unknown and
        # warn so the user can diagnose phantom workflows.
        logger.warning(
            "OpenProcess(PID=%s) failed with WinError %s (%s); liveness unknown. "
            "The PID file in ~/.conductor/runs/ may need manual removal.",
            pid,
            err,
            ctypes.FormatError(err),
        )
        return Liveness.UNKNOWN

    try:
        exit_code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            # Couldn't read the exit code — report unknown rather than crash.
            err = ctypes.get_last_error()
            logger.warning(
                "GetExitCodeProcess(PID=%s) failed with WinError %s (%s); liveness unknown. "
                "The PID file in ~/.conductor/runs/ may need manual removal.",
                pid,
                err,
                ctypes.FormatError(err),
            )
            return Liveness.UNKNOWN
        return Liveness.ALIVE if exit_code.value == _STILL_ACTIVE else Liveness.DEAD
    finally:
        _kernel32.CloseHandle(handle)
