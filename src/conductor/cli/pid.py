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
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_PID_DIR_NAME = "runs"


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

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
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

    filepath.write_text(json.dumps(data, indent=2))
    logger.debug("Wrote PID file: %s", filepath)
    return filepath


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
        except (json.JSONDecodeError, OSError):
            # Corrupted or unreadable — remove silently
            f.unlink(missing_ok=True)
            continue

        pid = data.get("pid")
        if pid is None:
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
        True if the process appears to still exist, False if it is known to be
        gone. On any unexpected error this returns True so that ``conductor
        stop`` doesn't silently delete PID files for processes that may still
        be running.
    """
    if sys.platform == "win32":
        return _is_process_alive_windows(pid)
    return _is_process_alive_posix(pid)


def _is_process_alive_posix(pid: int) -> bool:
    """POSIX implementation of :func:`_is_process_alive` using ``os.kill``.

    Catches generic :class:`OSError` and returns True so a transient OS
    failure doesn't crash ``conductor stop`` or silently drop a live
    workflow's PID file (regression for issue #166).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — still alive.
        return True
    except OSError:
        # Unknown error from the OS — err on the side of "still alive".  We
        # log at warning so the user can see why a phantom workflow is
        # appearing in ``conductor stop`` listings.
        logger.warning(
            "Unexpected OSError checking PID %s; assuming alive. "
            "The PID file in ~/.conductor/runs/ may need manual removal.",
            pid,
            exc_info=True,
        )
        return True
    return True


def _is_process_alive_windows(pid: int) -> bool:
    """Windows implementation of :func:`_is_process_alive` using ctypes.

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
    assert _kernel32 is not None, "_is_process_alive_windows requires _kernel32 to be initialised"
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == _ERROR_ACCESS_DENIED:
            # Process exists but we lack the rights to query it — treat as
            # alive.  This is an expected condition (e.g. cross-session or
            # higher-integrity targets) so a debug log is sufficient.
            logger.debug("OpenProcess(PID=%s) denied (ERROR_ACCESS_DENIED); assuming alive", pid)
            return True
        if err == _ERROR_INVALID_PARAMETER:
            # No process with that PID exists.
            return False
        # Any other failure is unexpected.  Don't crash; assume still alive
        # but warn so the user can diagnose phantom workflows.
        logger.warning(
            "OpenProcess(PID=%s) failed with WinError %s (%s); assuming alive. "
            "The PID file in ~/.conductor/runs/ may need manual removal.",
            pid,
            err,
            ctypes.FormatError(err),
        )
        return True

    try:
        exit_code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            # Couldn't read the exit code — assume alive rather than crash.
            err = ctypes.get_last_error()
            logger.warning(
                "GetExitCodeProcess(PID=%s) failed with WinError %s (%s); assuming alive. "
                "The PID file in ~/.conductor/runs/ may need manual removal.",
                pid,
                err,
                ctypes.FormatError(err),
            )
            return True
        return exit_code.value == _STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)
