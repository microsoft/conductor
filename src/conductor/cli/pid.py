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


def write_pid_file(pid: int, port: int, workflow_path: str | Path) -> Path:
    """Write a PID file for a background workflow process.

    Args:
        pid: Process ID of the background child.
        port: TCP port the web dashboard is listening on.
        workflow_path: Path to the workflow YAML file.

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

    Uses ``os.kill(pid, 0)`` which checks existence without sending a signal.

    Args:
        pid: The process ID to check.

    Returns:
        True if the process exists, False otherwise.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — still alive
        return True
    return True
