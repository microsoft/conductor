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

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_PID_DIR_NAME = "runs"


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
    """POSIX implementation of :func:`_is_process_alive` using ``os.kill``."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — still alive.
        return True
    except OSError:
        # Unknown error from the OS — err on the side of "still alive" so that
        # a transient failure doesn't crash ``conductor stop`` or cause us to
        # silently drop a still-running workflow's PID file.
        logger.debug("Unexpected OSError checking PID %s; assuming alive", pid, exc_info=True)
        return True
    return True


def _is_process_alive_windows(pid: int) -> bool:
    """Windows implementation of :func:`_is_process_alive` using ctypes.

    Calls ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, ...)`` and then
    ``GetExitCodeProcess`` rather than ``os.kill(pid, 0)``, which on Windows
    is unsafe (it routes through ``TerminateProcess``) and can raise
    ``OSError`` subclasses such as ``WinError 11`` (``ERROR_BAD_FORMAT``).
    """
    import ctypes
    from ctypes import wintypes

    # Process access right that doesn't require administrative privileges and
    # is sufficient to call GetExitCodeProcess.
    process_query_limited_information = 0x1000
    # Sentinel exit code returned by GetExitCodeProcess for a process that has
    # not yet exited.  See the Windows SDK ``WinBase.h`` (``STILL_ACTIVE``).
    still_active = 259
    # ``OpenProcess`` failure error codes we want to interpret specifically.
    error_access_denied = 5
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[unresolved-attribute]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        err = ctypes.get_last_error()  # type: ignore[unresolved-attribute]
        if err == error_access_denied:
            # Process exists but we lack the rights to query it — treat as alive.
            return True
        if err == error_invalid_parameter:
            # No process with that PID exists.
            return False
        # Any other failure is unexpected. Don't crash; assume still alive so
        # we don't silently delete a PID file for a process that may be
        # running.
        logger.debug("OpenProcess failed for PID %s with error %s; assuming alive", pid, err)
        return True

    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            # Couldn't read the exit code — assume alive rather than crash.
            logger.debug(
                "GetExitCodeProcess failed for PID %s with error %s; assuming alive",
                pid,
                ctypes.get_last_error(),  # type: ignore[unresolved-attribute]
            )
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)
