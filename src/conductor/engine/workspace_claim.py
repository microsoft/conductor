"""Local-machine fencing for a retained workspace run.

The claim is a process-lifetime reservation, not a distributed lock. A stable
advisory lock serializes claim changes; the claim's token fences old owners.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_LOCK_DEADLINE_SECONDS = 5.0


class WorkspaceClaimError(RuntimeError):
    """A workspace claim cannot be acquired or released safely."""


def _claim_directory() -> Path:
    home = Path(os.environ.get("CONDUCTOR_HOME", Path.home() / ".conductor"))
    directory = home / "workspaces" / "claims"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    with path.open("a+b") as lock_file:
        lock_file.seek(0)
        if sys.platform == "win32":
            import msvcrt

            deadline = time.monotonic() + _LOCK_DEADLINE_SECONDS
            while True:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise WorkspaceClaimError(f"Timed out locking {path}") from exc
                    time.sleep(0.01)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # Access denied/unknown is possibly alive.
        try:
            code = wintypes.DWORD()
            return not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


@dataclass(frozen=True, slots=True)
class WorkspaceClaim:
    """Claim owned by this process; release removes only its own token."""

    run_id: str
    path: Path
    token: str

    def release(self) -> None:
        """Remove this claim if its token still matches, preserving replacements."""
        with _locked(self.path.with_suffix(".lock")):
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise WorkspaceClaimError(f"Cannot read claim {self.path}") from exc
            if data.get("token") != self.token:
                raise WorkspaceClaimError(f"Workspace claim token mismatch for {self.run_id}")
            self.path.unlink()


def acquire_workspace_claim(run_id: str) -> WorkspaceClaim:
    """Acquire a local-machine claim, reclaiming only a confirmed dead owner."""
    if not _RUN_ID.fullmatch(run_id):
        raise WorkspaceClaimError("Invalid workspace run_id")
    path = _claim_directory() / f"{run_id}.json"
    with _locked(path.with_suffix(".lock")):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                pid = data["pid"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise WorkspaceClaimError(f"Cannot read claim {path}") from exc
            if not isinstance(pid, int) or isinstance(pid, bool):
                pid = 0
            if _pid_alive(pid):
                raise WorkspaceClaimError(f"Workspace claim for {run_id} is held by PID {pid}")
            path.unlink()
        token = uuid.uuid4().hex
        payload = {
            "pid": os.getpid(),
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "token": token,
        }
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            json.dump(payload, file)
    return WorkspaceClaim(run_id, path, token)
