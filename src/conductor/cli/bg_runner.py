"""Background runner for ``--web-bg`` mode.

When ``conductor run --web-bg`` or ``conductor resume --web-bg`` is used, this
module forks a detached child process that runs the workflow with ``--web``
enabled, then the parent process prints the dashboard URL and exits
immediately.

The child process is fully detached (new session on Unix, new process group on
Windows) so it outlives the parent. It auto-shuts down after the workflow
completes and all WebSocket clients disconnect (the existing ``--web`` +
``bg=True`` behavior in ``WebDashboard``).

The child's stdout and stderr are redirected into log files under
``$TMPDIR/conductor/`` (not ``DEVNULL``) so a silent crash — an uncaught
Python exception, a ``faulthandler`` dump, or anything else the child
would normally write to ``sys.stderr`` — leaves a forensic trail. See
issue #116 for context.

This is also why we deliberately do NOT pass ``--silent`` to the child
even though no human is watching its console: ``--silent`` would also
set ``verbose_mode=False``, which gates provider-side SDK event logging
that ``--log-file`` writes to disk. Leaving verbosity at the default and
capturing the stream to a file keeps both the log files and any
``--log-file`` trace populated for detached children (see issue #196).

**Three-stage readiness contract** (issues #410, #435, #444): a bg launch
is considered "finalized" only after three separate probes, not one.
Stage one (:func:`_wait_for_server`) is a plain TCP connect — it proves a
process is listening on the port, nothing more. Stage one-and-a-half
polls the child's own fleet run record (:mod:`conductor.fleet.records`)
until it appears matching this launch's ``mode``/``port`` and either its
``pid`` equals the spawned :class:`subprocess.Popen`'s pid *or* it is
*fresh* — written at or after the moment this launcher spawned the child
(see :func:`_record_is_fresh`). The freshness arm exists because
``Popen.pid`` is **not** always the pid of the process that ends up
running the workflow: on some platforms (notably a Windows ``uv tool
install``) ``sys.executable`` is a trampoline that re-execs, so the
process this module spawns and the process that actually serves the
dashboard are related but distinct pids. Comparing ``Popen.pid`` against
that fact is a false negative, not a safety check — see issue #444. If
the poll's own deadline passes while the child stays alive and its
dashboard stays reachable, this is downgraded to a warning
(``run_record_written=False`` on the returned :class:`BackgroundLaunch`)
rather than treated as a launch failure, since a bookkeeping failure must
not kill an otherwise healthy workflow. Once the record matches, the
launcher carries the record's ``pid`` forward as the *confirmed* identity
of the process actually running the workflow — this is what stage two
compares against instead of ``Popen.pid``. Stage two
(:func:`_wait_for_workflow_start`) polls ``GET /api/info`` until the
payload carries a ``started_at`` key, which only appears once the
child's engine has actually emitted ``workflow_started`` — proving the
workflow itself began rather than just the dashboard's HTTP server. It
also classifies the dashboard's reported identity against the confirmed
pid (falling back to ``run_id`` only when the payload itself has no
usable pid to compare, mirroring ``cli/app.py::_confirm_identity``/
``Identity``); a mismatch is only fatal
(``StartProbe.PORT_CONFLICT``) when the identity was confirmed -- an
*unconfirmed* mismatch keeps polling rather than killing a possibly
healthy run on unproven suspicion. All three stages check ``proc.poll()``
on every iteration so a child that exits early (e.g. a
``ConfigurationError`` from a bad workflow) is reported in about a
second instead of after the full timeout. The stage-two wait defaults to
30s and is tunable via ``CONDUCTOR_WEB_BG_START_TIMEOUT`` (``0`` disables
it, restoring pre-#410 behavior); passing that deadline with the child
still alive is not treated as a failure — the URL is still printed,
since the workflow may simply be slow to start (plugin fetch, MCP server
startup, provider connection). There is no parent-side PID file: the
child writes its own fleet run record (Fleet Manager D2), which is what
stage one-and-a-half polls for.

**Terminating the process tree, not just ``Popen.pid`` (issue #447):**
every failure branch of the readiness gate above eventually needs to
clean up a still-running child, and doing that by calling ``terminate()``
on the spawned :class:`subprocess.Popen` handle alone is unsound under a
trampoline ``sys.executable`` (issue #444's Q1) -- the pid that ends up
running the workflow is not always the pid this module spawned, so
terminating only the latter can leave the former orphaned and still
burning tokens. :func:`_terminate_child` therefore kills the whole
*process tree* before falling back to the single-handle ladder: on
Windows, the child is created suspended and immediately assigned to a
fresh job object (see :func:`_spawn_detached_windows`,
:class:`_WindowsDetachedProcess`) with ``JOB_OBJECT_LIMIT_BREAKAWAY_OK``
set but **not** ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` (the job must
survive the launcher exiting -- that's the entire point of
``--web-bg``), so ``TerminateJobObject`` reaches every process the child
spawned, however many exec layers deep; on POSIX,
``start_new_session=True`` already makes the child a process-group
leader, so ``os.killpg`` is the equivalent, gated by a registry
(:data:`_SPAWNED_GROUP_LEADERS`) of pids this module actually spawned so
it is never called against an arbitrary pid. After the tree kill and the
single-handle ladder, a final identity-checked sweep
(``conductor.cli.pid.is_process_alive``/``terminate_process``) confirms
the outcome rather than assuming it, naming any pid that survives in a
:class:`_TerminationOutcome` instead of unconditionally claiming success.
:func:`_cleanup_record_after_termination` only removes a child's run
record once its pid is confirmed dead by that sweep, so a surviving
orphan keeps the run record that is ``conductor stop``'s only remaining
handle on it.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from io import IOBase
from pathlib import Path
from typing import TYPE_CHECKING, Any

from conductor.run_id import is_valid_run_id, new_run_id

if TYPE_CHECKING:
    # Typing-only: both ``_finalize_background_launch`` and its helpers
    # import ``conductor.fleet.records`` lazily at runtime (matching the
    # existing lazy imports elsewhere in this module).
    from conductor.fleet.records import RunRecord

logger = logging.getLogger(__name__)

# Default wall-clock budget for the stage-two "did the workflow actually
# start" probe (issue #410), and the env var that overrides it. See the
# module docstring's "Two-stage readiness contract" section.
_START_TIMEOUT_DEFAULT = 30.0
_START_TIMEOUT_ENV = "CONDUCTOR_WEB_BG_START_TIMEOUT"

# Windows process creation flags. Exposed via ``getattr`` with documented
# fallbacks so this module can be imported on POSIX (where these attributes do
# not exist on ``subprocess``) and so tests can patch ``sys.platform`` to
# ``"win32"`` from a Linux/macOS host.
_CREATE_NEW_PROCESS_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_CREATE_BREAKAWAY_FROM_JOB: int = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)

# Win32 ERROR_ACCESS_DENIED — the error code raised when CreateProcess is
# called with ``CREATE_BREAKAWAY_FROM_JOB`` and the parent's job object has
# ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` cleared (some hardened CI environments).
_ERROR_ACCESS_DENIED = 5

# ``CREATE_SUSPENDED`` — the child's primary thread never starts running
# until ``ResumeThread`` is called. This is what lets the Windows spawn path
# (below) assign the child to a job object *before* it can run and
# potentially re-exec through a trampoline ``sys.executable`` out of the
# job's reach (issue #447). ``subprocess`` has no public name for this flag.
_CREATE_SUSPENDED = 0x00000004

# ``STILL_ACTIVE`` / ``WAIT_TIMEOUT`` — duplicated from ``cli/pid.py`` rather
# than imported (those names are private to that module and this one has no
# other reason to import it at module scope). Values are the same Win32 SDK
# constants documented there.
_STILL_ACTIVE = 259
_WAIT_TIMEOUT = 0x00000102

# Job-object limit flags and info-class constant used to create a job with
# ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` set and, deliberately,
# ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` NOT set -- see ``_create_job_object``.
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JobObjectExtendedLimitInformation = 9

if sys.platform == "win32":
    import _winapi
    import msvcrt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
else:
    # ``_winapi``/``msvcrt`` don't exist off Windows at all (not even as
    # importable stubs), so this module can't `import` them unconditionally.
    # Tests patch these two symbols (plus ``sys.platform``) to exercise the
    # Windows spawn/termination path from any host -- the same convention
    # ``cli/pid.py`` uses for ``_kernel32``.
    _winapi = None
    msvcrt = None
    _kernel32 = None


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    """``JOBOBJECT_BASIC_LIMIT_INFORMATION`` -- only ``LimitFlags`` is set."""

    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    )


class _IO_COUNTERS(ctypes.Structure):
    """``IO_COUNTERS`` -- required padding for ``JOBOBJECT_EXTENDED_LIMIT_INFORMATION``."""

    _fields_ = (
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    )


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    """``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` -- the struct ``SetInformationJobObject`` needs."""

    _fields_ = (
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


# NOTE: run-id format validation here defers to
# ``conductor.run_id.is_valid_run_id`` -- the single shared contract also
# used by ``fleet.records`` and ``EventLogSubscriber`` -- rather than a
# hand-rolled hex-only regex. A resumed child reuses a checkpoint's
# ``run_id`` verbatim -- ``EventLogSubscriber``'s
# ``existing_path``/``existing_run_id`` branch performs no format check of
# its own -- and the only thing that actually gates whether that id can be
# used is whether the child's own ``write_run_record`` call accepts it as a
# filename component. A local hex-only copy of that rule (this module used
# to keep one) can reject a checkpoint ``run_id`` the child would happily
# reuse, causing the parent to poll for a freshly generated id the resumed
# child never writes its run record under (see ``_peek_resume_run_id``'s
# docstring). ``conductor.run_id`` is a stdlib-only leaf module, so
# importing it here at module load time (unlike ``conductor.fleet.records``,
# which would drag in ``conductor.cli`` and close an import cycle) is safe.


def _detachment_kwargs() -> dict[str, Any]:
    """Return Popen kwargs that detach the child from the parent's lifecycle.

    On POSIX, ``start_new_session=True`` puts the child in its own session so
    it survives the parent and any controlling terminal closing.

    On Windows, ``CREATE_NEW_PROCESS_GROUP`` gives the child its own console
    process group (no shared Ctrl+C delivery) and ``CREATE_BREAKAWAY_FROM_JOB``
    detaches the child from the parent's Windows job object. The latter is
    required when the parent shell runs inside a job with
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` set (e.g. GitHub Actions runners,
    VS Code integrated terminal, JetBrains IDE terminals, the GitHub Copilot
    CLI shell tool): without breakaway, the bg child inherits the job and is
    killed when the parent exits and the job tears down.

    Returns:
        Platform-specific Popen keyword arguments. The full Popen call should
        merge these with stdio + env kwargs.
    """
    if sys.platform == "win32":
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP | _CREATE_BREAKAWAY_FROM_JOB}
    return {"start_new_session": True}


def _is_breakaway_denied(exc: OSError) -> bool:
    """Return True if a Popen ``OSError`` was caused by the parent job forbidding breakaway.

    Windows raises ``OSError`` with ``winerror == 5`` (ERROR_ACCESS_DENIED)
    when ``CREATE_BREAKAWAY_FROM_JOB`` is passed but the parent's job object
    has ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` cleared.

    Args:
        exc: The exception raised by ``subprocess.Popen``.

    Returns:
        True only for the access-denied breakaway case; other ``OSError``
        causes (e.g. ``FileNotFoundError`` for a missing executable) return
        False so the original error can propagate.
    """
    return getattr(exc, "winerror", None) == _ERROR_ACCESS_DENIED


# Pids this module has itself spawned as a POSIX process-group leader (via
# ``start_new_session=True``). ``_terminate_child`` (below) only calls
# ``os.killpg`` against a pid found here -- never against an arbitrary pid a
# caller hands in -- because ``os.killpg(1, SIGKILL)`` against, say, a stale
# ``MagicMock(pid=1)`` in a test, or a pid recycled by the OS onto an
# unrelated process, would be a real (if rare) footgun. Membership only ever
# grows; a pid is never removed, since a dead pid re-added later by the OS
# recycling it is out of scope for this module (the same accepted risk as
# every other liveness probe in this codebase).
_SPAWNED_GROUP_LEADERS: set[int] = set()


class _StartupInfo:
    """Duck-typed stand-in for ``subprocess.STARTUPINFO``.

    That class only exists in the standard library's ``subprocess`` module
    on Windows (it's defined inside an ``if _mswindows:`` block), so it
    can't be constructed here for cross-platform unit testing -- patching
    ``_winapi``/``sys.platform`` (this module's test convention, mirroring
    ``cli/pid.py``) doesn't help, since the class itself is simply absent
    from ``subprocess`` on POSIX. ``_winapi.CreateProcess`` reads its
    ``startup_info`` argument via plain attribute access
    (``dwFlags``/``hStdInput``/``hStdOutput``/``hStdError``/``wShowWindow``/
    ``lpAttributeList``), so any object exposing them works.
    """

    def __init__(self) -> None:
        self.dwFlags = 0
        self.hStdInput: int | None = None
        self.hStdOutput: int | None = None
        self.hStdError: int | None = None
        self.wShowWindow = 0
        self.lpAttributeList: dict[str, Any] = {}


def _create_job_object() -> int | None:
    """Create a Windows job object with ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` set.

    Deliberately does NOT set ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: the
    whole point of ``--web-bg`` is that the tree outlives the launcher, so
    the job must not tear the tree down when the launcher's own handle to
    it is closed (see ``_release_child_handles``). ``BREAKAWAY_OK`` is set
    so a nested ``--web-bg`` launched from inside this workflow still gets
    its own breakaway, matching today's behavior, instead of tripping
    ``_is_breakaway_denied``'s warning path against conductor's own job.

    Never raises: a launcher that cannot create a job object should still
    spawn the child (degrading to the pre-#447 best-effort termination)
    rather than fail the whole launch over a missing durable-kill
    guarantee.

    Returns:
        The job handle, or ``None`` on any failure.
    """
    if _kernel32 is None:
        return None
    try:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_BREAKAWAY_OK
        ok = _kernel32.SetInformationJobObject(
            job,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _kernel32.CloseHandle(job)
            return None
        return job
    except OSError:
        logger.debug("Failed to create Windows job object for bg child", exc_info=True)
        return None


class _WindowsDetachedProcess:
    """``subprocess.Popen``-shaped wrapper around a directly-created Windows child.

    ``subprocess.Popen`` cannot be reused for the suspend-assign-resume
    sequence :func:`_spawn_detached_windows` needs (issue #447, Q1):
    CPython's Windows ``_execute_child`` closes the child's primary thread
    handle (``_winapi.CloseHandle(ht)``) before ``Popen.__init__`` returns,
    so there is no handle left to call ``ResumeThread`` on by the time a
    caller could assign the process to a job object. This class exposes
    only the ``Popen`` surface this module actually uses -- ``pid``,
    ``poll()``, ``wait()``, ``terminate()``/``kill()`` -- plus
    ``terminate_tree()`` (kills the whole job, not just this one process)
    and ``close()`` (releases the retained handles once the launch gate no
    longer needs them; see ``_release_child_handles``).
    """

    def __init__(self, *, pid: int, h_process: int, h_job: int | None) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._h_process: int | None = h_process
        self._h_job: int | None = h_job

    def poll(self) -> int | None:
        """Return the exit code if the process has exited, else ``None``."""
        if self.returncode is not None:
            return self.returncode
        assert _kernel32 is not None and self._h_process is not None
        exit_code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(self._h_process, ctypes.byref(exit_code)):
            return None
        if exit_code.value == _STILL_ACTIVE:
            return None
        self.returncode = exit_code.value
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Block until the process exits, raising ``TimeoutExpired`` like ``Popen.wait``."""
        if self.returncode is not None:
            return self.returncode
        assert _kernel32 is not None and self._h_process is not None
        timeout_ms = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        result = _kernel32.WaitForSingleObject(self._h_process, timeout_ms)
        if result == _WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(cmd="<detached child>", timeout=timeout or 0.0)
        code = self.poll()
        return code if code is not None else 0

    def terminate(self) -> None:
        """Terminate this single process (not the tree) -- mirrors ``Popen.terminate``."""
        assert _kernel32 is not None and self._h_process is not None
        with contextlib.suppress(OSError):
            _kernel32.TerminateProcess(self._h_process, 1)

    kill = terminate

    def terminate_tree(self) -> None:
        """Terminate every process in this child's job, not just this one (issue #447)."""
        if self._h_job is None or _kernel32 is None:
            return
        with contextlib.suppress(OSError):
            _kernel32.TerminateJobObject(self._h_job, 1)

    def close(self) -> None:
        """Release the retained process/job handles (see ``_release_child_handles``)."""
        if _kernel32 is None:
            return
        if self._h_process is not None:
            with contextlib.suppress(OSError):
                _kernel32.CloseHandle(self._h_process)
            self._h_process = None
        if self._h_job is not None:
            with contextlib.suppress(OSError):
                _kernel32.CloseHandle(self._h_job)
            self._h_job = None


# The type this module's spawn/termination helpers accept and return: a
# genuine ``subprocess.Popen`` on POSIX, or the ``Popen``-shaped
# ``_WindowsDetachedProcess`` on Windows (issue #447).
_DetachedChild = subprocess.Popen[Any] | _WindowsDetachedProcess


def _resolve_stdio_handle(stream: Any, *, for_write: bool) -> int:
    """Return an inheritable Windows handle for a Popen-style stdio argument.

    Mirrors what ``subprocess.Popen._get_handles``/``_make_inheritable`` do
    on Windows: resolve the stream to a raw OS handle, then duplicate it as
    inheritable via ``DuplicateHandle`` (the duplicate is what actually
    gets inherited; the original is left untouched here and closed by the
    caller once ``CreateProcess`` returns).

    Args:
        stream: Either ``subprocess.DEVNULL`` or an open file-like object
            with a ``fileno()`` method (this module never passes
            ``subprocess.PIPE`` to a detached child).
        for_write: Whether a freshly-opened ``os.devnull`` should be opened
            for writing (stdout/stderr) or reading (stdin).

    Returns:
        An inheritable duplicate handle.
    """
    assert _winapi is not None and msvcrt is not None
    if stream is subprocess.DEVNULL:
        flags = os.O_WRONLY if for_write else os.O_RDONLY
        fd = os.open(os.devnull, flags)
        try:
            raw_handle = msvcrt.get_osfhandle(fd)
            return _winapi.DuplicateHandle(
                _winapi.GetCurrentProcess(),
                raw_handle,
                _winapi.GetCurrentProcess(),
                0,
                1,
                _winapi.DUPLICATE_SAME_ACCESS,
            )
        finally:
            os.close(fd)
    if hasattr(stream, "fileno"):
        raw_handle = msvcrt.get_osfhandle(stream.fileno())
        return _winapi.DuplicateHandle(
            _winapi.GetCurrentProcess(),
            raw_handle,
            _winapi.GetCurrentProcess(),
            0,
            1,
            _winapi.DUPLICATE_SAME_ACCESS,
        )
    raise TypeError(f"Unsupported stdio stream for a detached Windows child: {stream!r}")


def _spawn_detached_windows(
    cmd: list[str],
    env: dict[str, str],
    *,
    stdout: Any,
    stderr: Any,
    stdin: Any,
) -> _WindowsDetachedProcess:
    """Windows ``_spawn_detached``: suspend, job-assign, then resume (issue #447).

    Replicates what ``subprocess.Popen._execute_child`` does on Windows
    using the same stdlib building blocks (``list2cmdline``,
    ``DuplicateHandle``, ``CreateProcess`` with a
    ``lpAttributeList["handle_list"]`` restricting inheritance to exactly
    the three stdio handles), with one addition: ``CREATE_SUSPENDED`` is
    OR'd into the creation flags so the child cannot run -- and therefore
    cannot re-exec through a trampoline ``sys.executable`` out of reach --
    before it has been assigned to a fresh job object. The child's primary
    thread is always resumed in a ``finally``, whether or not the job
    could be created or the assignment succeeded: a suspended process that
    is never resumed would be a worse bug than the orphan this fixes.

    On breakaway-denied (``ERROR_ACCESS_DENIED`` from the parent's job
    forbidding ``CREATE_BREAKAWAY_FROM_JOB``), retries without that flag
    and prints the same warning as the POSIX-era implementation.

    Args:
        cmd: The fully-resolved command-line argv to execute.
        env: The environment dict to pass to the child.
        stdout: ``subprocess.DEVNULL`` or an open file-like object.
        stderr: ``subprocess.DEVNULL`` or an open file-like object.
        stdin: ``subprocess.DEVNULL`` or an open file-like object.

    Returns:
        A :class:`_WindowsDetachedProcess` wrapping the running child,
        already assigned to (and resumed within) its own job object when
        job creation succeeded.
    """
    assert _winapi is not None and _kernel32 is not None

    handles = [
        _resolve_stdio_handle(stdin, for_write=False),
        _resolve_stdio_handle(stdout, for_write=True),
        _resolve_stdio_handle(stderr, for_write=True),
    ]
    try:
        si = _StartupInfo()
        si.dwFlags |= _winapi.STARTF_USESTDHANDLES
        si.hStdInput, si.hStdOutput, si.hStdError = handles
        si.lpAttributeList = {"handle_list": handles}

        cmd_line = subprocess.list2cmdline(cmd)
        creationflags = _CREATE_NEW_PROCESS_GROUP | _CREATE_BREAKAWAY_FROM_JOB | _CREATE_SUSPENDED
        try:
            hp, ht, pid, _tid = _winapi.CreateProcess(
                None, cmd_line, None, None, True, creationflags, env, None, si
            )
        except OSError as exc:
            if not _is_breakaway_denied(exc):
                raise
            sys.stderr.write(
                "warning: parent shell forbids Windows job breakaway; the "
                "background workflow may not survive shell exit. Run "
                "--web-bg from a non-job-managed shell (e.g. a regular "
                "PowerShell window) for reliable persistence.\n"
            )
            creationflags &= ~_CREATE_BREAKAWAY_FROM_JOB
            hp, ht, pid, _tid = _winapi.CreateProcess(
                None, cmd_line, None, None, True, creationflags, env, None, si
            )
    finally:
        for handle in handles:
            with contextlib.suppress(OSError):
                _winapi.CloseHandle(handle)

    job = _create_job_object()
    try:
        if job is not None and not _kernel32.AssignProcessToJobObject(job, hp):
            with contextlib.suppress(OSError):
                _kernel32.CloseHandle(job)
            job = None
    finally:
        # Resume no matter what happened above: a job that could not be
        # created or assigned still means a *running* child (degrading to
        # the pre-#447 single-process termination), whereas a child left
        # permanently suspended would be strictly worse than the bug this
        # fixes.
        _kernel32.ResumeThread(ht)
        with contextlib.suppress(OSError):
            _kernel32.CloseHandle(ht)

    return _WindowsDetachedProcess(pid=pid, h_process=hp, h_job=job)


def _spawn_detached_posix(
    cmd: list[str],
    env: dict[str, str],
    *,
    stdout: Any,
    stderr: Any,
    stdin: Any,
) -> subprocess.Popen[Any]:
    """POSIX ``_spawn_detached`` -- unchanged ``subprocess.Popen`` call.

    ``start_new_session=True`` makes the child both a session leader and
    the leader of its own process group, which is what lets
    :func:`_terminate_child` reach the whole tree via
    ``os.killpg(proc.pid, ...)`` (issue #447) rather than only the direct
    child. ``proc.pid`` is recorded in :data:`_SPAWNED_GROUP_LEADERS` so
    that call is only ever made against a pid this module spawned as a
    group leader.
    """
    base: dict[str, Any] = {"stdout": stdout, "stderr": stderr, "stdin": stdin, "env": env}
    proc = subprocess.Popen(cmd, **base, start_new_session=True)  # noqa: S603
    _SPAWNED_GROUP_LEADERS.add(proc.pid)
    return proc


def _spawn_detached(
    cmd: list[str],
    env: dict[str, str],
    *,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    stdin: Any = subprocess.DEVNULL,
) -> _DetachedChild:
    """Launch a fully-detached child process for ``--web-bg`` mode.

    Dispatches to :func:`_spawn_detached_posix` (an ordinary
    ``subprocess.Popen`` with ``start_new_session=True``) or
    :func:`_spawn_detached_windows` (a job-object-assigned child, issue
    #447) depending on platform. The default stdio is ``DEVNULL`` for all
    three streams; callers that need to capture the child's stderr/stdout
    (for diagnostics — see issue #116) can pass open file handles via the
    ``stdout`` / ``stderr`` kwargs.

    Args:
        cmd: The fully-resolved command-line argv to execute.
        env: The environment dict to pass to the child (callers prepare this
            via :func:`_build_bg_env` with ``CONDUCTOR_WEB_BG`` /
            ``CONDUCTOR_WEB_PORT`` and the bg-diagnostics vars set).
        stdout: Popen ``stdout`` argument; defaults to ``DEVNULL``. Pass
            an open file handle to capture the child's stdout.
        stderr: Popen ``stderr`` argument; defaults to ``DEVNULL``. Pass
            an open file handle to capture the child's stderr.
        stdin: Popen ``stdin`` argument; defaults to ``DEVNULL``.

    Returns:
        The running detached child -- a :class:`subprocess.Popen` on
        POSIX, a :class:`_WindowsDetachedProcess` on Windows.

    Raises:
        OSError: Propagated for any spawn failure other than the Windows
            breakaway-denied case (e.g. ``FileNotFoundError`` for a
            missing executable). Callers wrap this in a ``RuntimeError``.
    """
    if sys.platform == "win32":
        return _spawn_detached_windows(cmd, env, stdout=stdout, stderr=stderr, stdin=stdin)
    return _spawn_detached_posix(cmd, env, stdout=stdout, stderr=stderr, stdin=stdin)


@dataclass(frozen=True, slots=True)
class BackgroundLaunch:
    """Result of launching a ``--web-bg`` child process.

    Attributes:
        url: The dashboard URL (e.g. ``http://127.0.0.1:8080``).
        stderr_log: Path to the file capturing the child's stderr — the
            first place to look when a bg run misbehaves silently.
        stdout_log: Path to the file capturing the child's stdout.
        run_id: The run id that ties this bg launch to its ``.events.jsonl``
            peer via ``CONDUCTOR_RUN_ID``. Normally exactly 8 lowercase hex
            characters (a fresh ``conductor.run_id.new_run_id()``), but a
            resumed launch may force-carry a checkpoint's original run id
            instead (see ``_peek_resume_run_id``), which is validated
            against the same path-safe contract the fleet run-record store
            itself uses.
        workflow_started: Whether the launcher observed the workflow actually
            start (via ``GET /api/info`` reporting a ``workflow_started``
            event — see ``_wait_for_workflow_start``) before its wait
            deadline. ``True`` means only that the launcher did **not**
            observe a failure to start — it is the default because the
            stage-two probe can be disabled entirely via
            ``CONDUCTOR_WEB_BG_START_TIMEOUT=0``, in which case nothing
            contradicts it. ``False`` means the deadline passed with the
            child still alive but not yet reporting a start; callers should
            surface this as a "still initializing" note rather than a
            failure — see issue #410. Only meaningful once ``still_running``
            has already been checked (see below) — check ``still_running``
            first.
        still_running: Whether the child process was still alive when the
            launcher finished waiting. ``True`` in the common case (the
            workflow is a genuine long-running background run). ``False``
            when the child completed (exit code 0) inside the launcher's
            wait window — either before the port opened or during the
            stage-two workflow-start probe. This is deliberately a separate
            field from ``workflow_started``: a clean sub-second run makes
            ``workflow_started`` stay ``True`` while ``still_running``
            becomes ``False`` — the two fields deliberately diverge in
            exactly this case, rather than both reading ``True``, because
            callers must not report a URL/"running in background" for a
            process that has already exited (issue #410) — printing that
            message unconditionally on ``workflow_started`` alone would
            reintroduce a narrower form of the same false-success bug this
            PR fixes. Check this field *before* ``workflow_started``: the
            latter is only meaningful when this one is ``True``.
        run_record_written: Whether the child's fleet run record was
            actually observed by the launch gate (see
            ``_finalize_background_launch``). ``True`` in the overwhelming
            common case. ``False`` means the run is executing normally —
            the dashboard came up and stayed reachable — but the discovery
            record itself could not be confirmed within the poll window,
            so this run will **not** appear in ``conductor status`` /
            ``fleet list`` / the ``fleet`` TUI and cannot be stopped with
            ``conductor stop`` (issue #435: a bookkeeping failure must not
            be treated as a launch failure and kill an otherwise-healthy
            workflow). Callers should surface this with a warning pointing
            at the captured stderr log, where the child's own
            ``_write_run_record_for_current_process`` failure handler
            names the underlying cause.

    Invariants (enforced in ``__post_init__``):
        * ``run_id`` is a valid fleet run id -- see
          ``conductor.run_id.is_valid_run_id``, the same contract
          the child's own ``write_run_record`` call enforces.
        * ``url`` is a localhost URL (``http://127.0.0.1:<port>``).
        * ``run_id`` appears in both ``stderr_log.name`` and
          ``stdout_log.name`` — this is what lets the bg log files and
          the child's ``.events.jsonl`` correlate by filename. See
          ``_open_bg_log_files``.
    """

    url: str
    stderr_log: Path
    stdout_log: Path
    run_id: str
    workflow_started: bool = True
    still_running: bool = True
    run_record_written: bool = True

    def __post_init__(self) -> None:
        if not is_valid_run_id(self.run_id):
            raise ValueError(
                f"BackgroundLaunch.run_id must be a valid run id, got: {self.run_id!r}"
            )
        if not self.url.startswith("http://127.0.0.1:"):
            raise ValueError(f"BackgroundLaunch.url must be a localhost URL, got: {self.url!r}")
        if self.run_id not in self.stderr_log.name:
            raise ValueError(
                f"BackgroundLaunch.run_id {self.run_id!r} not embedded in "
                f"stderr_log filename {self.stderr_log.name!r}"
            )
        if self.run_id not in self.stdout_log.name:
            raise ValueError(
                f"BackgroundLaunch.run_id {self.run_id!r} not embedded in "
                f"stdout_log filename {self.stdout_log.name!r}"
            )


def _find_free_port() -> int:
    """Find an available TCP port on localhost.

    Returns:
        An available port number.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(
    port: int, timeout: float = 15.0, *, proc: _DetachedChild | None = None
) -> bool:
    """Wait until the web server is accepting connections on *port*.

    Args:
        port: The TCP port to check.
        timeout: Maximum seconds to wait.
        proc: When given, checked with ``proc.poll()`` on every iteration so
            a dead child is detected in well under *timeout* instead of
            waiting out the full socket-connect deadline. Keyword-only so
            the many existing ``patch(..., "_wait_for_server")`` call sites
            (which invoke this positionally as ``(port, timeout)``) are
            unaffected by the added parameter.

    Returns:
        True if the server became reachable within *timeout*, False if the
        timeout elapsed or (when *proc* is given) the child exited first.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


@dataclass(frozen=True, slots=True)
class _TerminationOutcome:
    """Result of :func:`_terminate_child`'s best-effort process-tree kill.

    Replaces a bare ``None`` return, which let every call site assert a
    termination it could not actually prove (issue #447) -- following the
    ``_GateOutcome``/``StartProbe``/``WebPauseOutcome`` precedent of naming
    an outcome instead of discarding the information a bool would lose.
    """

    confirmed: bool
    """True only when every pid this call knew about was independently
    confirmed dead by the final liveness sweep. False means at least one
    survived (or its liveness could not be established) -- see
    ``surviving_pids``."""

    surviving_pids: tuple[int, ...]
    """Pids confirmed alive, or whose liveness could not be established,
    after every termination rung was tried. Empty when ``confirmed`` is
    True."""


def _terminate_child(
    proc: _DetachedChild, *, confirmed_child_pid: int | None = None
) -> _TerminationOutcome:
    """Best-effort terminate a still-running child process **tree**.

    Used to avoid orphaned background workflows when post-launch validation
    (server reachability, the child's run record appearing) fails. Three
    rungs, none individually load-bearing (issue #447):

    1. Tree kill: ``terminate_tree()`` (Windows job object) when *proc*
       is a :class:`_WindowsDetachedProcess`, else ``os.killpg(proc.pid,
       SIGKILL)`` on POSIX -- but only when ``proc.pid`` is a pid this
       module itself spawned as a process-group leader (see
       :data:`_SPAWNED_GROUP_LEADERS`), never against an arbitrary pid.
       Runs unconditionally, regardless of ``proc.poll()``: a Windows job
       object and a POSIX process group both outlive their initial
       process, and this is precisely the ``StartProbe.CHILD_EXITED`` case
       issue #447 exists for -- the outer trampoline shim has already
       exited while the re-exec'd workflow it started lives on.
    2. The original polite/forceful ladder on *proc* itself
       (``terminate()`` → wait 5s → ``kill()`` → wait 2s), skipped when
       ``proc.poll()`` already shows *proc* dead.
    3. A final, identity-checked sweep over every pid this call knows
       about (``proc.pid`` and, if different, *confirmed_child_pid*) via
       ``conductor.cli.pid``'s ``is_process_alive``/``terminate_process``.
       This is what actually *confirms* the outcome rather than assuming
       it: a tree kill or the ladder above can each fail silently (a
       permission error, a race, a trampoline child that escaped a job
       the launcher couldn't create).

    Rung 3 runs even when *proc* already looks dead, because under a
    trampoline ``sys.executable`` the outer *proc* exiting says nothing
    about whether *confirmed_child_pid* -- the pid actually running the
    workflow -- is still alive.

    Never raises: any errors from rungs 1–2 are swallowed so the
    original failure that triggered termination surfaces to the caller.

    Args:
        proc: The detached child handle to terminate.
        confirmed_child_pid: The pid of the process actually running the
            workflow, when known (e.g. from a confirmed run record) and
            different from ``proc.pid`` (the trampoline case, issue #444).

    Returns:
        A :class:`_TerminationOutcome` describing whether every known pid
        was confirmed dead, and naming any that survived.
    """
    candidates = {proc.pid}
    if confirmed_child_pid is not None:
        candidates.add(confirmed_child_pid)

    # Rung 1 runs regardless of proc.poll(): a Windows job object and a
    # POSIX process group both outlive their initial process, and this is
    # precisely the StartProbe.CHILD_EXITED case issue #447 exists for --
    # the outer trampoline shim has already exited while the re-exec'd
    # workflow it started lives on. killpg on a dead leader raises
    # ProcessLookupError, already suppressed below.
    if isinstance(proc, _WindowsDetachedProcess):
        with contextlib.suppress(Exception):
            proc.terminate_tree()
    elif proc.pid in _SPAWNED_GROUP_LEADERS:
        with contextlib.suppress(Exception):
            os.killpg(proc.pid, signal.SIGKILL)

    if proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001 - cleanup must not raise
            pass

    from conductor.cli.pid import is_process_alive, terminate_process

    surviving: list[int] = []
    for pid in sorted(candidates):
        if not is_process_alive(pid):
            continue
        with contextlib.suppress(Exception):
            terminate_process(pid, timeout=2.0)
        if is_process_alive(pid):
            surviving.append(pid)

    return _TerminationOutcome(confirmed=not surviving, surviving_pids=tuple(surviving))


def _termination_note(outcome: _TerminationOutcome, *, web_port: int) -> str:
    """Render the termination clause of a launch-gate failure message.

    ``outcome.confirmed`` only means "every pid we knew about was
    individually confirmed dead" -- prior to issue #447 every failure
    message unconditionally claimed "The background process was
    terminated." even when only ``proc.pid`` (not the real, possibly
    trampolined, workflow process) had been signalled. A survivor is now
    named explicitly, with a pointer at how to find and stop it manually.

    Args:
        outcome: The result of :func:`_terminate_child`.
        web_port: The port this launch requested, used to build the
            ``conductor stop`` hint.

    Returns:
        A trailing-space-terminated sentence (or two) to splice into a
        ``RuntimeError`` message.
    """
    if outcome.confirmed:
        return "The background process was terminated. "
    pids = ", ".join(str(pid) for pid in outcome.surviving_pids)
    return (
        f"WARNING: could not confirm termination of PID(s) {pids}. "
        f"Check `conductor status` or run `conductor stop --port {web_port}` "
        "to clean it up manually. "
    )


def _cleanup_record_after_termination(
    run_id: str, outcome: _TerminationOutcome, candidate_pid: int | None
) -> None:
    """Remove the child's run record, but only once its pid is confirmed dead.

    A survivor keeps its run record: that record is `conductor stop`'s only
    remaining handle on it (the same concern :func:`_remove_dead_child_record`
    itself documents).

    Args:
        run_id: The run id whose record should be removed.
        outcome: The result of :func:`_terminate_child`.
        candidate_pid: The pid the caller believes the record should name
            (``proc.pid`` or a confirmed identity). No-op when ``None``.
    """
    if candidate_pid is None:
        return
    if candidate_pid in outcome.surviving_pids:
        return
    _remove_dead_child_record(run_id, candidate_pid)


def _read_record_or_fail(
    run_id: str, proc: _DetachedChild, *, web_port: int, stderr_log: Path
) -> RunRecord | None:
    """Read the child's run record, terminating and raising on failure.

    A failure here (a truncated or concurrently-rewritten record file, an
    ``OSError``) must not escape uncontained: without this, the child would
    never be terminated and would orphan itself, contradicting
    :func:`_finalize_background_launch`'s own contract.
    """
    from conductor.fleet.records import read_run_record

    try:
        return read_run_record(run_id)
    except Exception as exc:
        outcome = _terminate_child(proc)
        raise RuntimeError(
            f"Failed to read background process's run record "
            f"(run_id={run_id}): {exc}. {_termination_note(outcome, web_port=web_port)}"
            f"See child stderr log: "
            f"{stderr_log}{_tail_log(stderr_log)}"
        ) from exc


# Filename pattern used by ``conductor.engine.event_log.EventLogSubscriber``
# for the events JSONL file. The bg stderr/stdout log files share the same
# ``<ts>-<runid>`` infix so all three artefacts for a single bg run sort
# next to each other in ``$TMPDIR/conductor/``.
_LOG_FILENAME_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_name(name: str) -> str:
    """Strip path-unsafe characters out of a workflow filename stem."""
    cleaned = _LOG_FILENAME_SAFE_NAME.sub("-", name).strip("-")
    return cleaned or "workflow"


def _open_bg_log_files(
    workflow_ref: Path, *, forced_run_id: str | None = None
) -> tuple[str, Path, Path, IOBase, IOBase]:
    """Create the bg child's stderr/stdout log files and return open handles.

    Generates a fresh 8-hex-character run id (unless ``forced_run_id`` is
    given) and opens two log files in ``$TMPDIR/conductor/`` whose names
    match the convention used by ``EventLogSubscriber`` (timestamp + run
    id) so all three artefacts of a single bg run group together by
    filename.

    The caller is responsible for closing the returned handles once
    ``subprocess.Popen`` has returned (the child has its own inherited OS
    handles by that point).

    Args:
        workflow_ref: The workflow file (or checkpoint) used to derive the
            ``<name>`` segment of the filename.
        forced_run_id: When given, use this run id instead of generating a
            random one. Used by ``launch_background_resume`` when the
            child is expected to reuse a checkpoint's original ``run_id``
            (see ``_peek_resume_run_id``) — the parent must use the exact
            same id for the bg log filenames, ``CONDUCTOR_RUN_ID``, and
            the run-record poll key, or the D2 launch gate polls for an id
            the child never writes.

    Returns:
        Tuple of ``(run_id, stderr_path, stdout_path, stderr_handle,
        stdout_handle)``.

    Raises:
        OSError: If the log directory cannot be created or the files
            cannot be opened. The caller is expected to surface this as a
            ``RuntimeError`` with context.
    """
    run_id = forced_run_id or new_run_id()
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = _sanitize_name(workflow_ref.stem) if workflow_ref.stem else "workflow"
    log_dir = Path(tempfile.gettempdir()) / "conductor"
    log_dir.mkdir(parents=True, exist_ok=True)

    stderr_path = log_dir / f"conductor-{base}-{ts}-{run_id}.bg.stderr.log"
    stdout_path = log_dir / f"conductor-{base}-{ts}-{run_id}.bg.stdout.log"
    # Line-buffered text mode so a tail of the file shows fresh output as
    # the child writes it. ``errors="replace"`` keeps the file readable
    # even if the child emits invalid UTF-8 (e.g. raw bytes from a
    # mis-encoded subprocess).
    stderr_handle = open(  # noqa: SIM115 - caller closes after Popen
        stderr_path, "w", encoding="utf-8", errors="replace", buffering=1
    )
    stdout_handle = open(  # noqa: SIM115 - caller closes after Popen
        stdout_path, "w", encoding="utf-8", errors="replace", buffering=1
    )
    return run_id, stderr_path, stdout_path, stderr_handle, stdout_handle


def _close_quietly(*handles: IOBase) -> None:
    """Close file handles, logging close errors to stderr but never raising.

    The parent's stdout/stderr file handles for the bg child should be
    released as soon as ``Popen`` returns (the child has its own duplicated
    OS handles by then), but ``handle.close()`` can still raise — most
    notably ``OSError`` from a buffer flush on a disk-full filesystem, or
    a Windows ``PermissionError`` if antivirus is scanning the file. The
    captured-log promise (#116) would be quietly broken in those cases,
    so print a warning to the parent's real stderr instead of suppressing
    silently. Never raise — callers run this in cleanup paths where
    propagating would mask an earlier, more relevant exception.
    """
    for h in handles:
        try:
            h.close()
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            name = getattr(h, "name", "<unknown handle>")
            print(
                f"conductor: WARNING: failed to close bg log handle {name}: {exc}",
                file=sys.stderr,
            )


class StartProbe(str, Enum):
    """Result of :func:`_wait_for_workflow_start`'s stage-two readiness probe.

    Mirrors the ``Liveness`` / ``Identity`` enum pattern in ``cli/pid.py`` and
    ``cli/app.py`` — a bool would collapse genuinely different outcomes that
    callers must react to differently.

    Attributes:
        STARTED: ``/api/info`` reported a ``started_at`` key — the child's
            engine has emitted ``workflow_started``.
        CHILD_EXITED: ``proc.poll()`` returned non-``None`` before the
            workflow was observed to start. The caller must still check the
            return code: a clean ``0`` exit (a sub-second workflow that
            finished within the wait window) is not a failure.
        PORT_CONFLICT: the dashboard on the port identified itself as a
            *different run* (by pid, or by ``run_id`` when the payload had
            no usable pid), while this launch's own identity was
            independently confirmed via the run-record poll (issue #444
            — comparing against the raw ``Popen.pid`` produced false
            conflicts under a trampoline ``sys.executable``, e.g. a
            Windows ``uv tool install``). A mismatch with no confirmed
            identity to compare against is *not* reported this way — see
            :func:`_classify_dashboard_identity`.
        TIMED_OUT: The wait deadline passed with the child alive and no
            ``workflow_started`` observed yet.
    """

    STARTED = "started"
    CHILD_EXITED = "child_exited"
    PORT_CONFLICT = "port_conflict"
    TIMED_OUT = "timed_out"


def _resolve_start_timeout() -> float:
    """Resolve the stage-two start-wait deadline from the environment.

    Returns:
        ``CONDUCTOR_WEB_BG_START_TIMEOUT`` parsed as a float when it is a
        valid non-negative number (``0`` disables the probe entirely, which
        restores pre-#410 behavior). An unset, unparseable, or negative value
        falls back to :data:`_START_TIMEOUT_DEFAULT`, logging a warning for
        the latter two cases so a typo'd env var doesn't silently do nothing.
    """
    raw = os.environ.get(_START_TIMEOUT_ENV)
    if raw is None:
        return _START_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a valid number; using the default %.0fs.",
            _START_TIMEOUT_ENV,
            raw,
            _START_TIMEOUT_DEFAULT,
        )
        return _START_TIMEOUT_DEFAULT
    if value < 0:
        logger.warning(
            "%s=%r must not be negative; using the default %.0fs.",
            _START_TIMEOUT_ENV,
            raw,
            _START_TIMEOUT_DEFAULT,
        )
        return _START_TIMEOUT_DEFAULT
    return value


def _probe_workflow_info(port: int) -> dict[str, Any] | None:
    """Fetch ``GET /api/info`` from the dashboard on *port*, once.

    This is the same identity endpoint ``conductor stop`` polls (see
    ``cli/app.py::_confirm_identity``), so this adds no new endpoint and no
    new dependency — ``httpx`` is already a hard dependency, imported lazily
    inside functions elsewhere in this codebase.

    Args:
        port: The TCP port the dashboard should be listening on.

    Returns:
        The parsed JSON body when the request succeeds, returns a 2xx status,
        and decodes to a dict. ``None`` on a connection/timeout/HTTP-status
        failure or a non-dict body (logged at debug — a single failed probe
        is expected while the child is still starting up and not actionable
        on its own). An exception outside that expected set (a bug in this
        function, or a body large enough to raise on decode) is logged at
        *warning* instead of being folded into the same "not ready yet"
        bucket — a genuinely broken probe must not be indistinguishable from
        normal startup latency for the caller's full wait window.
    """
    import httpx

    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/api/info", timeout=1.0)
        resp.raise_for_status()
        info = resp.json()
    except httpx.HTTPError as exc:
        # Connection refused/reset, timeout, or a non-2xx status (including a
        # foreign, non-Conductor process answering the port) — all expected
        # "not ready yet" outcomes for a dashboard that hasn't bound the port
        # or started serving yet.
        logger.debug("Workflow-start probe on port %s failed: %s", port, exc)
        return None
    except ValueError as exc:
        # ``.json()`` raises a ``JSONDecodeError`` (a ``ValueError`` subclass)
        # on a non-JSON body — plausible from the same foreign-process case
        # above. Still just "not ready yet" from this caller's perspective.
        logger.debug("Workflow-start probe on port %s returned unparseable JSON: %s", port, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - see docstring: log loudly, don't hide a real bug
        logger.warning(
            "Workflow-start probe on port %s failed with an unexpected %s: %s",
            port,
            type(exc).__name__,
            exc,
        )
        return None
    return info if isinstance(info, dict) else None


class _DashboardIdentity(str, Enum):
    """Result of comparing an ``/api/info`` payload against this launch's identity.

    Modelled on ``cli/app.py::Identity`` (``CONFIRMED``/``UNCONFIRMED``/
    ``MISMATCHED``) — the same central rule applies: only *positive
    evidence of someone else* should block an action, so the enum keeps
    "we don't actually know" (:attr:`UNKNOWN`) distinct from "this is
    provably someone else" (:attr:`FOREIGN`).

    Attributes:
        OURS: The payload's identity signal (pid, or ``run_id`` as
            fallback) matches this launch's.
        FOREIGN: The payload's identity signal positively does *not*
            match this launch's.
        UNKNOWN: No comparable identity signal was present in the
            payload at all (e.g. an older Conductor's dashboard reporting
            no usable ``pid`` and no ``run_id``), or the payload reports a
            usable ``pid`` but this launch has no confirmed ``child_pid``
            of its own to compare it against yet.
    """

    OURS = "ours"
    FOREIGN = "foreign"
    UNKNOWN = "unknown"


def _classify_dashboard_identity(
    info: dict[str, Any], *, expected_run_id: str, child_pid: int | None
) -> _DashboardIdentity:
    """Classify an ``/api/info`` payload's identity against this launch's own.

    ``pid`` is the primary signal, but only when the caller has a
    *confirmed* ``child_pid`` to compare against (see
    :func:`_confirmed_pid_from_record`) — comparing against the raw
    ``subprocess.Popen.pid`` is exactly the bug issue #444 fixes, because
    that is not always the pid of the process that ends up running the
    workflow (a trampoline ``sys.executable`` re-execs). ``run_id`` is the
    fallback signal, mirroring ``cli/app.py::_confirm_identity`` -- but,
    matching that function exactly, the fallback only applies when the
    payload itself has no usable pid to compare. A payload reporting a
    usable ``pid`` while *this launch's own* ``child_pid`` is unconfirmed
    is :attr:`_DashboardIdentity.UNKNOWN`, not a ``run_id`` comparison:
    ``run_id`` legitimately differs from this launch's expectation on a
    resume (the child adopts the checkpoint's original run id), so
    treating that mismatch as :attr:`_DashboardIdentity.FOREIGN` would
    misjudge a resumed run's own healthy dashboard as someone else's.

    Args:
        info: The parsed ``/api/info`` JSON body.
        expected_run_id: The run id this launch expects to see once the
            child reports ``workflow_started``.
        child_pid: The confirmed pid of the process actually running the
            workflow (from a matched run record), or ``None`` when no run
            record has been confirmed yet.

    Returns:
        :class:`_DashboardIdentity`.
    """
    reported_pid = info.get("pid")
    if isinstance(reported_pid, int):
        if child_pid is None:
            return _DashboardIdentity.UNKNOWN
        return _DashboardIdentity.OURS if reported_pid == child_pid else _DashboardIdentity.FOREIGN

    reported_run_id = str(info.get("run_id") or "")
    if not reported_run_id:
        return _DashboardIdentity.UNKNOWN
    return (
        _DashboardIdentity.OURS
        if reported_run_id == expected_run_id
        else _DashboardIdentity.FOREIGN
    )


def _wait_for_workflow_start(
    port: int,
    proc: _DetachedChild,
    *,
    timeout: float,
    expected_run_id: str,
    confirmed_child_pid: int | None,
    last_seen_info: dict[str, Any] | None = None,
) -> StartProbe:
    """Poll ``/api/info`` until the workflow reports having started.

    Args:
        port: The TCP port the dashboard is listening on.
        proc: The detached child process, checked with ``proc.poll()`` on
            every iteration so a dead child is detected immediately rather
            than after the full timeout.
        timeout: Maximum seconds to wait.
        expected_run_id: The run id this launch expects the child to
            report — the ``run_id`` fallback signal for
            :func:`_classify_dashboard_identity` when no pid was
            confirmed.
        confirmed_child_pid: The pid confirmed by the run-record poll
            (:func:`_confirmed_pid_from_record`), or ``None`` when no
            record was confirmed (issue #435's downgrade path, or a
            resume whose predicted run id never matched). ``None`` means
            identity is *unverified* — a positive mismatch is then never
            treated as fatal, only as a reason to keep waiting, because
            there is no trustworthy basis for concluding the port is
            genuinely held by someone else (issue #444).
        last_seen_info: When given, updated in place with the most
            recently probed ``/api/info`` payload on every successful
            probe, so a caller returning :attr:`StartProbe.PORT_CONFLICT`
            can report the foreign pid without a second, possibly-racy
            round trip after the child has already been terminated.

    Returns:
        :class:`StartProbe` describing why the wait ended.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return StartProbe.CHILD_EXITED

        info = _probe_workflow_info(port)
        if info is not None:
            if last_seen_info is not None:
                last_seen_info.clear()
                last_seen_info.update(info)

            identity = _classify_dashboard_identity(
                info, expected_run_id=expected_run_id, child_pid=confirmed_child_pid
            )
            if identity is _DashboardIdentity.FOREIGN:
                if confirmed_child_pid is not None:
                    return StartProbe.PORT_CONFLICT
                logger.debug(
                    "Workflow-start probe on port %s reported a mismatched identity "
                    "(run_id=%r) but this launch's own identity was never confirmed; "
                    "continuing to wait rather than treating this as a port conflict.",
                    port,
                    info.get("run_id"),
                )
            # Key presence, not truthiness: ``started_at`` is
            # ``event.get("timestamp", 0)`` server-side and could
            # legitimately be ``0``. That key only exists on the
            # ``workflow_started`` branch of the endpoint. Skipped for an
            # unconfirmed-mismatch payload above -- an unverified foreign
            # identity reporting a start is not proof *our* workflow started.
            elif "started_at" in info:
                return StartProbe.STARTED

        time.sleep(0.3)
    return StartProbe.TIMED_OUT


def _tail_log(path: Path, max_lines: int = 20, max_chars: int = 2000) -> str:
    """Return a bounded tail of a captured bg log file for error messages.

    Never raises — a diagnostics helper must not be the thing that breaks
    the launch. Returns ``""`` when the file is missing or unreadable.

    Args:
        path: Path to the captured stderr/stdout log file.
        max_lines: Maximum number of trailing lines to include.
        max_chars: Maximum total characters to include (applied after the
            line limit, in case a single line is enormous).

    Returns:
        A header plus the tail of the file's contents, or ``""`` on failure.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()[-max_lines:]
    tail = "\n".join(lines)
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    if not tail:
        return ""
    return f"\n--- last {len(lines)} line(s) of {path} ---\n{tail}"


def _remove_dead_child_record(run_id: str, child_pid: int) -> None:
    """Remove the child's run record after the launcher gave up on it.

    The counterpart to the old parent-side ``remove_pid_file_at`` cleanup:
    the child writes the record (D2), so when the launcher terminates it or
    finds it dead, the record it wrote would otherwise be left behind
    advertising a process that is gone.

    Identity-checked on ``pid`` for the same reason ``remove_pid_file_at``
    re-read its file (issue #344): a resumed launch can carry a checkpoint's
    original ``run_id``, so the record under this key may belong to a
    different, live process. Never raises -- this is cleanup on a path that
    is already reporting a failure, and masking that failure with a
    secondary one would hide the reason the launch was abandoned.
    """
    try:
        from conductor.fleet.records import read_run_record, remove_run_record

        record = read_run_record(run_id)
        if record is not None and record.pid == child_pid:
            remove_run_record(run_id)
    except Exception:  # noqa: BLE001 - cleanup must not mask the launch failure
        logger.debug("Could not remove run record for run_id=%s", run_id, exc_info=True)


def _record_is_fresh(record: RunRecord, launched_at: datetime) -> bool:
    """Return whether *record* was written at or after this launch spawned its child.

    Replaces exactly what a ``pid == proc.pid`` comparison was doing when
    the run-record poll's actual job is ruling out a *stale* record left
    under the same ``run_id`` key (see
    :func:`_confirmed_pid_from_record`'s docstring) -- which, unlike a
    trampoline re-exec (issue #444), really is distinguishable by time: a
    record written after we spawned this launch's child cannot be a
    leftover from something else.

    Args:
        record: The polled run record.
        launched_at: The timestamp captured immediately before this
            launch's ``subprocess.Popen`` call (see ``_spawn_bg_child``) --
            captured there, not at the start of the poll loop, so "fresh"
            strictly means "written by a process we started".

    Returns:
        ``True`` only when ``record.started_at`` is a string that parses
        (via ``datetime.fromisoformat``) into a *timezone-aware* datetime
        at or after ``launched_at``. Anything unparseable, missing, naive,
        or simply the wrong type returns ``False`` rather than raising,
        falling back to the pid-equality arm.
    """
    started_at = getattr(record, "started_at", None)
    if not isinstance(started_at, str):
        return False
    try:
        parsed = datetime.fromisoformat(started_at)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        # A naive stamp (e.g. from a legacy pre-#435 record) can't be
        # compared against ``launched_at`` (always UTC-aware) without
        # guessing a timezone -- treat as unparseable rather than guess.
        return False
    return parsed >= launched_at


def _confirmed_pid_from_record(
    record: RunRecord | None,
    proc: _DetachedChild,
    web_port: int,
    launched_at: datetime,
) -> int | None:
    """Return the confirmed workflow pid if a polled run record can be trusted.

    ``mode`` and ``port`` must always match (see
    :func:`_finalize_background_launch`'s docstring for what each rules
    out); the remaining question is identity, satisfied by *either* of:

    * ``record.pid == proc.pid`` -- the non-trampoline case, where
      ``subprocess.Popen``'s pid genuinely is the process running the
      workflow. Kept as an accepted alternative (not replaced) so this
      path, and every existing test built around it, is unchanged.
    * :func:`_record_is_fresh` -- the trampoline case (issue #444), where
      ``proc.pid`` is a re-exec wrapper's pid and the real workflow
      process has a different one. A record written after we spawned the
      child cannot be the stale leftover the pid check was there to catch.

    Args:
        record: The polled run record, or ``None`` if none was found yet.
        proc: This launch's spawned (possibly trampoline) process.
        web_port: The port this launch requested.
        launched_at: See :func:`_record_is_fresh`.

    Returns:
        ``record.pid`` when the record can be trusted as this launch's own
        child's record, ``None`` otherwise. Returning the pid rather than a
        bare ``bool`` lets both call sites assign it directly as the
        confirmed identity without a separate, unchecked ``record.pid``
        access.
    """
    if record is None:
        return None
    if record.mode != "bg" or record.port != web_port:
        return None
    if record.pid == proc.pid or _record_is_fresh(record, launched_at):
        return record.pid
    return None


def _peek_confirmed_pid(
    run_id: str, proc: _DetachedChild, web_port: int, launched_at: datetime
) -> int | None:
    """Best-effort, out-of-band read of the child's run record.

    Used by the dashboard-unreachable branch of
    :func:`_finalize_background_launch`, which fires before the run-record
    poll has run at all -- and is the highest-impact path for issue #447,
    since a dead-on-arrival dashboard is exactly when a trampoline re-exec
    is most likely to already have happened. The child may have written
    its record even though its dashboard never came up (or came up on a
    different pid than ``proc.pid``), so this opportunistically reuses
    :func:`_confirmed_pid_from_record` rather than leaving that branch with
    no trustworthy termination target at all.

    Swallows everything: this is a best-effort improvement to *which* pid
    gets terminated, not a new failure mode of its own.

    Returns:
        The confirmed pid, or ``None`` if no record could be read or
        trusted.
    """
    try:
        from conductor.fleet.records import read_run_record

        record = read_run_record(run_id)
    except Exception:  # noqa: BLE001 - best-effort only
        return None
    return _confirmed_pid_from_record(record, proc, web_port, launched_at)


@dataclass(frozen=True, slots=True)
class _GateOutcome:
    """Result of :func:`_finalize_background_launch`'s readiness gate.

    Replaces a bare ``bool`` (which only ever meant ``workflow_started``)
    so the run-record poll's own, independent signal --
    ``run_record_written`` -- survives the call rather than being folded
    away, following the ``WebPauseOutcome`` / ``StartProbe`` precedent of
    naming an outcome instead of widening a single boolean.
    """

    workflow_started: bool
    """See ``BackgroundLaunch.workflow_started`` -- same meaning."""

    run_record_written: bool
    """See ``BackgroundLaunch.run_record_written`` -- same meaning. ``True``
    on every path except the one where the run-record poll deadline passed
    while the child was confirmed alive and still serving its port
    (issue #435)."""


def _finalize_background_launch(
    proc: _DetachedChild,
    web_port: int,
    run_id: str,
    stderr_log: Path,
    launched_at: datetime,
) -> _GateOutcome:
    """Wait for the dashboard, wait for the child's run record, then confirm it started.

    On a fatal failure (server didn't start, child died early, the port is
    taken by a foreign process), the still-running child is terminated to
    avoid orphaned processes holding the dashboard port. The stderr log path
    (with a bounded tail of its contents) is included in the
    ``RuntimeError`` so callers can point users at the captured crash
    output.

    The run-record poll (stage one-and-a-half, below) is a *readiness
    signal*, not a kill switch (issue #435): if its own deadline passes
    while the child is confirmed alive and still serving its port, that
    means the child's discovery bookkeeping failed, not that the workflow
    itself is unhealthy -- terminating a working run over a failed
    diagnostic write would be strictly worse than leaving it running
    undiscoverable. Only a child that is actually dead, or that has gone
    unreachable, fails the launch at this stage.

    Per D2 (the child owns the write in every mode — see
    ``docs/projects/fleet-manager/fleet-manager.design.md``), the detached
    child is responsible for writing its own fleet run record (keyed by
    ``run_id``) once it starts executing. This function is the parent-side
    gate: it polls :func:`conductor.fleet.records.read_run_record` rather
    than writing a PID/record file itself, so there is exactly one writer
    (and hence exactly one record) per background run. This replaces the
    parent-side ``write_pid_file`` the two-stage contract originally wrote
    here; the run record supersedes the PID file and is written by the
    child, so re-introducing a parent-side write would create a second
    writer for one run.

    The polled record must match this launch on three fields before the
    gate accepts it as readiness (see :func:`_confirmed_pid_from_record`)
    -- a record merely found under the right ``run_id`` is not proof by
    itself that *this* launch's child is up:

    * ``mode`` must be ``"bg"`` (rules out a record some other, differently
      launched process happens to have written under a colliding key).
    * ``port`` must equal ``web_port`` (rules out advertising an unrelated
      service's record when the requested port was occupied and the child
      fell back to a different, or no, port).
    * identity: either ``pid`` equals the spawned child's ``proc.pid``, or
      the record is *fresh* -- written at or after ``launched_at`` (issue
      #444). The pid check alone rules out a stale record left behind
      under the same key (e.g., in the forced-resume-run-id case, a
      leftover record from the *original*, now-dead run that the resumed
      child hasn't yet overwritten), but ``proc.pid`` is not always the
      pid of the process that ends up running the workflow: under a
      trampoline ``sys.executable`` (a Windows ``uv tool install``, the
      documented install path) the spawned process re-execs into a
      different one, so the two pids legitimately differ even on a
      perfectly healthy launch. A record written after we spawned this
      launch's child cannot be that stale leftover, so freshness is
      accepted as an alternative rather than a replacement -- the
      non-trampoline path, and every test built around plain pid
      equality, is unaffected.

    A failure while reading the record itself (as opposed to the record
    simply not existing yet, which :func:`conductor.fleet.records.read_run_record`
    already reports as ``None``) also terminates the child and raises,
    rather than letting the exception escape uncontained and orphan the
    background process.

    Because the child writes its record as it starts executing, seeing a
    matching record is a *stronger* readiness signal than the PID write it
    replaces -- but it is still not proof the engine reached
    ``workflow_started``, so the stage-two ``/api/info`` probe (issue #410)
    is retained below. Once a record is confirmed, its ``pid`` -- not
    ``proc.pid`` -- becomes the *trusted* identity passed to stage two,
    since it is the pid of the process actually running the workflow.

    Args:
        proc: The detached child process.
        web_port: The TCP port the child should be listening on.
        run_id: The run id shared with the child via ``CONDUCTOR_RUN_ID``,
            used to look up its run record once written.
        stderr_log: Path to the file capturing the child's stderr. Included
            in failure messages so users know where to look.
        launched_at: Timestamp captured immediately before ``proc`` was
            spawned (see ``_spawn_bg_child``), used by
            :func:`_confirmed_pid_from_record` to decide whether a polled
            record is fresh enough to trust despite a pid mismatch.

    Returns:
        A :class:`_GateOutcome`. ``workflow_started`` is ``True`` if the
        workflow was observed to start (or the stage-two probe is
        disabled, or the child exited cleanly within the wait window),
        ``False`` if the stage-two wait deadline passed with the child
        still alive and not yet reporting a start (not a failure, just
        "still initializing"). ``run_record_written`` is ``False`` only
        when the run-record poll's own deadline passed while the child was
        confirmed alive and reachable (issue #435).

    Raises:
        RuntimeError: If the child died early (with a non-zero exit code),
            the dashboard didn't start within the timeout, reading the run
            record itself failed, the run-record poll deadline passed
            while the child was dead or its dashboard had become
            unreachable (the alive-and-reachable case is downgraded --
            see Returns), the child died before the workflow started
            (non-zero exit), or a foreign process -- one whose identity
            this launch was able to confirm as *not* its own -- already
            holds the port.
    """
    if not _wait_for_server(web_port, timeout=15.0, proc=proc):
        retcode = proc.poll()
        if retcode is not None:
            if retcode == 0:
                # A sub-second run that finished before the socket became
                # reachable is not a failure, and there's no live process to
                # track — no PID file to write.
                return _GateOutcome(workflow_started=True, run_record_written=True)
            raise RuntimeError(
                f"Background process exited immediately with code {retcode}. "
                f"See child stderr log: {stderr_log}{_tail_log(stderr_log)}"
            )
        pid = _peek_confirmed_pid(run_id, proc, web_port, launched_at)
        outcome = _terminate_child(proc, confirmed_child_pid=pid)
        _cleanup_record_after_termination(run_id, outcome, pid)
        raise RuntimeError(
            f"Dashboard did not start within 15 seconds on port {web_port}. "
            f"{_termination_note(outcome, web_port=web_port)}"
            f"See child stderr log: {stderr_log}{_tail_log(stderr_log)}"
        )

    # The child's own run record, not a parent-side PID write (D2): one
    # writer per run. This is stage one-and-a-half -- the port is open and
    # the child has reached the point of registering itself.
    run_record_written = True
    # The pid confirmed by a matching run record -- the trusted identity
    # passed to stage two (see docstring). Stays ``None`` down the issue
    # #435 downgrade path, where no record was ever confirmed and hence
    # there is no trustworthy identity for stage two to compare against.
    confirmed_child_pid: int | None = None
    deadline = time.monotonic() + 15.0
    while True:
        retcode = proc.poll()
        if retcode is not None:
            if retcode == 0:
                # Completed inside the window; the child removed its own
                # record on exit, so there is nothing to wait for.
                return _GateOutcome(workflow_started=True, run_record_written=True)
            raise RuntimeError(
                f"Background process exited immediately with code {retcode}. "
                f"See child stderr log: {stderr_log}{_tail_log(stderr_log)}"
            )
        record = _read_record_or_fail(run_id, proc, web_port=web_port, stderr_log=stderr_log)
        pid_from_record = _confirmed_pid_from_record(record, proc, web_port, launched_at)
        if pid_from_record is not None:
            confirmed_child_pid = pid_from_record
            break
        if time.monotonic() >= deadline:
            # The deadline passed. This is a *bookkeeping* failure, not
            # necessarily a workflow failure (issue #435) -- if the child
            # is still alive and its dashboard is still reachable, the
            # workflow itself is healthy and only the discovery record
            # write failed. Downgrade to a warning and let the launch
            # proceed rather than killing a working run. Only when the
            # child is dead or its port has gone unreachable is this
            # still fatal.
            if proc.poll() is None and _wait_for_server(web_port, timeout=1.0, proc=proc):
                # One more read before declaring the record missing: the
                # child may have written it in the instant between the
                # deadline check above and this re-probe, and re-reading
                # here is essentially free next to the 15s already spent
                # waiting.
                record = _read_record_or_fail(
                    run_id, proc, web_port=web_port, stderr_log=stderr_log
                )
                pid_from_record = _confirmed_pid_from_record(record, proc, web_port, launched_at)
                if pid_from_record is not None:
                    confirmed_child_pid = pid_from_record
                    break
                logger.warning(
                    "Background process (run_id=%s) did not report a run record within "
                    "15 seconds, but is still running and its dashboard is still "
                    "reachable. It will not appear in `conductor status` / `fleet list` "
                    "and cannot be stopped with `conductor stop`. See child stderr log: %s",
                    run_id,
                    stderr_log,
                )
                run_record_written = False
                break
            outcome = _terminate_child(proc)
            raise RuntimeError(
                f"Background process did not report a run record within 15 seconds "
                f"(run_id={run_id}). {_termination_note(outcome, web_port=web_port)}"
                f"See child stderr log: {stderr_log}{_tail_log(stderr_log)}"
            )
        time.sleep(0.2)

    start_timeout = _resolve_start_timeout()
    if start_timeout == 0:
        return _GateOutcome(workflow_started=True, run_record_written=run_record_written)

    last_seen_info: dict[str, Any] = {}
    probe = _wait_for_workflow_start(
        web_port,
        proc,
        timeout=start_timeout,
        expected_run_id=run_id,
        confirmed_child_pid=confirmed_child_pid,
        last_seen_info=last_seen_info,
    )

    if probe is StartProbe.STARTED:
        return _GateOutcome(workflow_started=True, run_record_written=run_record_written)

    if probe is StartProbe.TIMED_OUT:
        logger.info(
            "Workflow on port %s has not reported starting after %.0fs; "
            "leaving it running. Set %s to tune this wait.",
            web_port,
            start_timeout,
            _START_TIMEOUT_ENV,
        )
        return _GateOutcome(workflow_started=False, run_record_written=run_record_written)

    if probe is StartProbe.CHILD_EXITED:
        retcode = proc.poll()
        if retcode == 0:
            # Completed inside the window; the child already removed its
            # own run record.
            return _GateOutcome(workflow_started=True, run_record_written=run_record_written)
        # The outer ``proc`` is provably dead, but under a trampoline that
        # only tells us the *outer* process exited -- the inner process
        # ``confirmed_child_pid`` names can outlive it, orphaned, and keep
        # running the workflow (issue #447). ``_terminate_child`` is still
        # worth calling here even though ``proc`` itself is already dead:
        # its final identity-checked sweep independently targets
        # ``confirmed_child_pid``. The record is only removed once that
        # pid is confirmed dead too, so a live orphan's record -- the only
        # handle `conductor stop` has on it -- is never deleted out from
        # under it.
        outcome = _terminate_child(proc, confirmed_child_pid=confirmed_child_pid)
        dead_pid = confirmed_child_pid if confirmed_child_pid is not None else proc.pid
        _cleanup_record_after_termination(run_id, outcome, dead_pid)
        raise RuntimeError(
            "Background process exited before the workflow started "
            f"(code {retcode}). {_termination_note(outcome, web_port=web_port)}"
            f"See child stderr log: "
            f"{stderr_log}{_tail_log(stderr_log)}"
        )

    # probe is StartProbe.PORT_CONFLICT, which _wait_for_workflow_start only
    # returns when identity was confirmed (confirmed_child_pid is not None)
    # -- see its docstring.
    foreign_pid = last_seen_info.get("pid", "unknown")
    outcome = _terminate_child(proc, confirmed_child_pid=confirmed_child_pid)
    # #447 made termination reach the whole process tree (a Windows job
    # object / a POSIX process group), not just ``proc.pid``, so the
    # identity check for cleanup now targets the *confirmed* pid -- the
    # one actually running the workflow under a trampoline
    # ``sys.executable`` -- rather than being deliberately kept on
    # ``proc.pid`` as before. A pid still confirmed alive after every
    # termination rung was tried keeps its run record: that record is
    # `conductor stop`'s only remaining handle on it.
    _cleanup_record_after_termination(run_id, outcome, confirmed_child_pid)
    raise RuntimeError(
        f"Port {web_port} is already in use by another process (PID {foreign_pid}). "
        f"{_termination_note(outcome, web_port=web_port)}"
        "Choose a different port with --web-port."
    )


def _build_bg_env(
    run_id: str,
    web_port: int,
    stderr_log: Path,
    stdout_log: Path,
) -> dict[str, str]:
    """Compose the child's environment with the bg-diagnostics env vars.

    Args:
        run_id: 8-hex-character run id shared with ``EventLogSubscriber`` so
            the events JSONL and bg log files use the same id in filenames
            and ``workflow_started`` system metadata.
        web_port: The TCP port the child should listen on.
        stderr_log: Path to the child's captured stderr log file.
        stdout_log: Path to the child's captured stdout log file.

    Returns:
        The new environment dict for ``subprocess.Popen``.
    """
    env = os.environ.copy()
    env["CONDUCTOR_WEB_BG"] = "1"
    env["CONDUCTOR_WEB_PORT"] = str(web_port)
    env["CONDUCTOR_RUN_ID"] = run_id
    env["CONDUCTOR_BG_STDERR_LOG"] = str(stderr_log)
    env["CONDUCTOR_BG_STDOUT_LOG"] = str(stdout_log)
    return env


def _release_child_handles(proc: _DetachedChild) -> None:
    """Release the Windows process/job handles the launch gate no longer needs.

    A no-op for a POSIX ``subprocess.Popen`` (nothing beyond what ``Popen``
    itself already manages). For a :class:`_WindowsDetachedProcess`, this
    closes the retained process and job handles -- otherwise they would
    leak for the lifetime of whatever process called :func:`launch_background`,
    which is not always a short-lived CLI invocation: the Fleet TUI's New
    Run screen (``fleet/launch.py``) calls it from a long-lived process.
    """
    close = getattr(proc, "close", None)
    if close is not None:
        with contextlib.suppress(Exception):
            close()


def _spawn_bg_child(
    *,
    cmd: list[str],
    web_port: int,
    pid_workflow_ref: Path,
    forced_run_id: str | None = None,
) -> BackgroundLaunch:
    """Open the bg log files, spawn the detached child, and finalize the launch.

    Shared tail of both ``launch_background`` and ``launch_background_resume``.
    Keeping these steps in a single place is what guarantees the two paths
    cannot drift apart on the detachment flags, the stderr/stdout redirect,
    or the env-var contract that ``EventLogSubscriber`` and
    ``WorkflowEngine._build_system_metadata`` depend on. See issue #116.

    Args:
        cmd: Fully assembled subprocess command (already includes ``--silent``,
            the subcommand, ``--web``, ``--no-interactive``, etc.).
        web_port: The TCP port the child should listen on.
        pid_workflow_ref: Workflow or checkpoint path used as the source of
            the bg log filename stem (see ``_open_bg_log_files``).
        forced_run_id: When given, use this run id instead of generating a
            random one — see ``_peek_resume_run_id``. Passed through to
            ``_open_bg_log_files`` so the bg log filenames, the
            ``CONDUCTOR_RUN_ID`` env var, and the run-record poll key in
            ``_finalize_background_launch`` all agree on one id.

    Returns:
        ``BackgroundLaunch`` describing the live launch.

    Raises:
        RuntimeError: If the log files cannot be created, the child fails to
            start, the dashboard doesn't become reachable, or the child is
            found to have exited with a non-zero code in the narrow window
            between ``_finalize_background_launch`` reporting success and
            this function's own final liveness check.
    """
    try:
        run_id, stderr_path, stdout_path, stderr_handle, stdout_handle = _open_bg_log_files(
            pid_workflow_ref, forced_run_id=forced_run_id
        )
    except OSError as exc:
        raise RuntimeError(f"Failed to create background log files: {exc}") from exc

    # Spawn the detached child via the shared helper so the platform-
    # appropriate detachment kwargs — including Windows job breakaway and
    # the access-denied fallback — apply uniformly. Pass the log file
    # handles as stdio overrides so the child's output is captured (see
    # issue #116) instead of dropped on the floor.
    try:
        try:
            # Captured immediately before the spawn, not inside the gate,
            # so "fresh" (see ``_record_is_fresh``) strictly means
            # "written by a process we started" -- capturing any later
            # would let a record written between here and the gate's first
            # poll look stale when it shouldn't.
            launched_at = datetime.now(UTC)
            proc = _spawn_detached(
                cmd,
                _build_bg_env(run_id, web_port, stderr_path, stdout_path),
                stdout=stdout_handle,
                stderr=stderr_handle,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to start background process: {exc}. See child stderr log: {stderr_path}"
            ) from exc

        try:
            gate_outcome = _finalize_background_launch(
                proc, web_port, run_id, stderr_path, launched_at
            )
        except BaseException:
            _release_child_handles(proc)
            raise
    finally:
        # The child has its own duplicated OS handles by now (or never got
        # them, if Popen raised) — either way the parent's Python file
        # objects can be released without affecting the child.
        _close_quietly(stderr_handle, stdout_handle)

    try:
        # ``_finalize_background_launch`` returns a ``workflow_started`` of
        # ``True`` (or ``False`` for ``StartProbe.TIMED_OUT``) from several
        # paths that don't check the child's exit code, because at the moment
        # they returned the child was confirmed either alive or cleanly exited
        # (0) — see the function's own docstring. ``proc`` is still in hand
        # here, so re-poll it directly rather than widening that function's
        # return type further: this is what lets ``BackgroundLaunch
        # .still_running`` distinguish "genuinely running" from "already
        # exited" without callers printing a live dashboard URL for an
        # already-exited process (#410).
        retcode = proc.poll()
        still_running = retcode is None
        if not still_running and retcode != 0:
            # The child crashed in the narrow window between
            # _finalize_background_launch's last liveness check (STARTED,
            # TIMED_OUT, and the CONDUCTOR_WEB_BG_START_TIMEOUT=0 escape hatch
            # all return without re-checking the exit code, since the child was
            # confirmed alive or the check was skipped entirely moments before)
            # and this re-poll. Reporting this as a clean "Workflow completed"
            # success would be exactly the false-success bug class issue #410
            # exists to close, so raise instead of returning a BackgroundLaunch
            # — the same treatment _finalize_background_launch already gives a
            # non-zero exit it catches directly (StartProbe.CHILD_EXITED above).
            raise RuntimeError(
                f"Background process exited unexpectedly (code {retcode}) while "
                f"the launcher was finishing up. See child stderr log: "
                f"{stderr_path}{_tail_log(stderr_path)}"
            )

        return BackgroundLaunch(
            url=f"http://127.0.0.1:{web_port}",
            stderr_log=stderr_path,
            stdout_log=stdout_path,
            run_id=run_id,
            workflow_started=gate_outcome.workflow_started,
            still_running=still_running,
            run_record_written=gate_outcome.run_record_written,
        )
    finally:
        # Release the Windows process/job handles once the gate no longer
        # needs them (no-op on POSIX). This matters because
        # ``launch_background`` is not always immediately followed by
        # process exit -- ``fleet/launch.py`` calls it from a long-lived
        # TUI process rather than a CLI invocation that exits right after.
        _release_child_handles(proc)


def launch_background(
    *,
    workflow_path: Path,
    inputs: dict[str, Any],
    provider_override: str | None = None,
    skip_gates: bool = False,
    log_file: Path | None = None,
    no_interactive: bool = True,
    web_port: int = 0,
    metadata: dict[str, str] | None = None,
    workspace_instructions: bool = False,
    cli_instructions: list[str] | None = None,
    print_loaded_instructions: bool = False,
) -> BackgroundLaunch:
    """Fork a detached child process running the workflow with a web dashboard.

    The child executes ``conductor run <workflow> --web --web-port <port>``
    with all the caller-supplied options. The parent waits briefly for the
    web server to become reachable, then returns the dashboard URL and the
    path to the child's captured stderr log.

    Args:
        workflow_path: Path to the workflow YAML file.
        inputs: Workflow input key=value pairs.
        provider_override: Optional provider name override.
        skip_gates: Whether to auto-select first option at human gates.
        log_file: Optional log file path.
        no_interactive: Whether to disable interactive mode (always True for bg).
        web_port: Desired port (0 = auto-select).
        metadata: Optional CLI metadata key=value pairs.
        workspace_instructions: Whether to auto-discover workspace instruction files.
        cli_instructions: Optional list of instruction file paths.
        print_loaded_instructions: Whether to forward ``--print-loaded-instructions``
            to the background child. Output goes to the child's captured stderr
            log, not to the parent's TTY.

    Returns:
        A ``BackgroundLaunch`` describing the launch (dashboard URL,
        captured stderr/stdout log paths, run id).

    Raises:
        RuntimeError: If the child process fails to start or the server
            doesn't become reachable within the timeout.
    """
    # Resolve port early so we know what URL to return
    if web_port == 0:
        web_port = _find_free_port()

    # Build the subprocess command. Console output is already redirected to
    # DEVNULL via the Popen ``stdout``/``stderr`` kwargs below, so the child
    # runs at default verbosity. This keeps ``verbose_log()`` and provider
    # SDK event logging active so ``--log-file`` captures a real trace when
    # enabled (see issue #196).
    cmd: list[str] = [
        sys.executable,
        "-m",
        "conductor",
        "run",
        str(workflow_path),
        "--web",
        "--web-port",
        str(web_port),
        "--no-interactive",
    ]

    # Forward inputs -- via the hidden, strictly-typed --input-json flag
    # (not --input's public, ambiguous heuristic), since these values are
    # already declared-type-coerced and must round-trip verbatim (see
    # ``_serialize_input_value``/``cli/run.py::coerce_typed_value``).
    for key, value in inputs.items():
        cmd.extend(["--input-json", f"{key}={_serialize_input_value(value)}"])

    # Forward metadata
    if metadata:
        for key, value in metadata.items():
            cmd.extend(["--metadata", f"{key}={_serialize_value(value)}"])

    if provider_override:
        cmd.extend(["--provider", provider_override])

    if skip_gates:
        cmd.append("--skip-gates")

    if log_file:
        cmd.extend(["--log-file", str(log_file)])

    if workspace_instructions:
        cmd.append("--workspace-instructions")

    if cli_instructions:
        for instr_path in cli_instructions:
            cmd.extend(["--instructions", instr_path])

    if print_loaded_instructions:
        cmd.append("--print-loaded-instructions")

    return _spawn_bg_child(cmd=cmd, web_port=web_port, pid_workflow_ref=workflow_path)


def _peek_resume_run_id(workflow_path: Path | None, checkpoint_path: Path | None) -> str | None:
    """Best-effort peek at the checkpoint the child will resume, to predict its ``run_id``.

    ``resume_workflow_async`` reuses the checkpoint's original ``run_id``
    (rather than generating a fresh one, or honoring ``CONDUCTOR_RUN_ID``)
    whenever the checkpoint's ``event_log_path`` still points at a real
    file — see ``EventLogSubscriber.__init__``'s
    ``existing_path``/``existing_run_id`` branch. The parent must mirror
    that exact same decision so ``_finalize_background_launch`` polls
    ``read_run_record`` for the id the child will actually write under
    (D2); polling a freshly generated id here would time out and
    terminate a perfectly healthy resumed run whenever the original log
    file survived.

    This function and ``EventLogSubscriber``'s ``existing_run_id`` branch
    both defer to the single shared rule in :mod:`conductor.run_id`
    (issue #435) -- before that, this module kept its own broad
    (path-safe) copy while ``EventLogSubscriber``'s *other* branch (the
    ``CONDUCTOR_RUN_ID`` env-var fallback taken when the peeked log has
    vanished between this peek and child start) enforced a narrower
    hex-only rule and lowercased the result, so a checkpoint id this
    function accepted here could still be rejected (and folded to a
    different value) by the child, silently polling for a key the child
    never wrote. With one shared rule, that divergence cannot recur.

    Best-effort: any failure to locate or parse the checkpoint returns
    ``None`` (falling back to a freshly generated id, the pre-existing
    behavior) rather than raising — the child's own checkpoint resolution
    is authoritative and reports a real error if something is actually
    wrong; this peek only affects which id the *parent* polls for.

    Args:
        workflow_path: Optional workflow YAML path (mirrors the resume
            command's positional arg).
        checkpoint_path: Optional explicit checkpoint path (mirrors
            ``--from``).

    Returns:
        The checkpoint's ``run_id`` if the child will reuse it (and it is
        a valid fleet run id -- see :func:`conductor.run_id.is_valid_run_id`,
        the same contract the child's own ``write_run_record`` call
        enforces -- and hence also satisfies ``BackgroundLaunch``'s
        invariant), else ``None``.
    """
    from conductor.engine.checkpoint import CheckpointManager

    try:
        if checkpoint_path is not None:
            cp = CheckpointManager.load_checkpoint(checkpoint_path)
        elif workflow_path is not None:
            latest = CheckpointManager.find_latest_checkpoint(workflow_path)
            if latest is None:
                return None
            cp = CheckpointManager.load_checkpoint(latest)
        else:
            return None
    except Exception:
        return None

    if not cp.run_id or not cp.event_log_path:
        return None
    if not is_valid_run_id(cp.run_id):
        return None
    candidate = Path(cp.event_log_path)
    if candidate.exists() and candidate.is_file():
        return cp.run_id
    return None


def launch_background_resume(
    *,
    workflow_path: Path | None,
    checkpoint_path: Path | None,
    provider_override: str | None = None,
    skip_gates: bool = False,
    log_file: Path | None = None,
    web_port: int = 0,
    metadata: dict[str, str] | None = None,
    guidance: list[str] | None = None,
) -> BackgroundLaunch:
    """Fork a detached child process resuming the workflow with a web dashboard.

    The child executes ``conductor resume <workflow|--from path> --web ...``
    with all the caller-supplied options. ``--no-interactive`` is always
    appended since the detached child has no TTY. The parent waits briefly
    for the web server to become reachable, then returns the dashboard URL
    and the path to the child's captured stderr log.

    Either ``workflow_path`` or ``checkpoint_path`` (or both) must be
    provided — at least one is required by the resume command.

    Args:
        workflow_path: Optional path to the workflow YAML file. Used to find
            the latest checkpoint when ``checkpoint_path`` is not given.
        checkpoint_path: Optional explicit path to a checkpoint file.
        provider_override: Optional provider name override.
        skip_gates: Whether to auto-select first option at human gates.
        log_file: Optional log file path.
        web_port: Desired port (0 = auto-select).
        metadata: Optional CLI metadata key=value pairs.
        guidance: Optional mid-run guidance text(s) to apply before the
            resumed agent runs. Forwarded as repeated ``--guidance`` flags.

    Returns:
        A ``BackgroundLaunch`` describing the launch (dashboard URL,
        captured stderr/stdout log paths, run id).

    Raises:
        ValueError: If neither ``workflow_path`` nor ``checkpoint_path`` is
            provided.
        RuntimeError: If the child process fails to start or the server
            doesn't become reachable within the timeout.
    """
    if workflow_path is None and checkpoint_path is None:
        raise ValueError(
            "launch_background_resume requires either workflow_path or checkpoint_path"
        )

    # Resolve port early so we know what URL to return
    if web_port == 0:
        web_port = _find_free_port()

    # Build the subprocess command. Console output is already redirected to
    # DEVNULL via the Popen ``stdout``/``stderr`` kwargs below, so the child
    # runs at default verbosity. This keeps ``verbose_log()`` and provider
    # SDK event logging active so ``--log-file`` captures a real trace when
    # enabled (see issue #196).
    cmd: list[str] = [
        sys.executable,
        "-m",
        "conductor",
        "resume",
    ]

    if workflow_path is not None:
        cmd.append(str(workflow_path))

    if checkpoint_path is not None:
        cmd.extend(["--from", str(checkpoint_path)])

    cmd.extend(
        [
            "--web",
            "--web-port",
            str(web_port),
            "--no-interactive",
        ]
    )

    # Forward metadata
    if metadata:
        for key, value in metadata.items():
            cmd.extend(["--metadata", f"{key}={_serialize_value(value)}"])

    # Forward guidance
    if guidance:
        for text in guidance:
            cmd.extend(["--guidance", text])

    if provider_override:
        cmd.extend(["--provider", provider_override])

    if skip_gates:
        cmd.append("--skip-gates")

    if log_file:
        cmd.extend(["--log-file", str(log_file)])

    # Use workflow_path if available, otherwise fall back to checkpoint_path
    # for the bg log filename stem. The early guard at the top of this
    # function already rejected the case where both are None; the ``or``
    # here picks the first non-None.
    pid_workflow_ref: Path = workflow_path or checkpoint_path  # type: ignore[assignment]

    # Peek the checkpoint the child is about to resume so the parent polls
    # for the *same* run_id the child will actually write its run record
    # under (see ``_peek_resume_run_id``) — a resumed run whose original
    # event log survived reuses that log's run_id rather than a freshly
    # generated one, and generating our own here would poll the wrong key.
    forced_run_id = _peek_resume_run_id(workflow_path, checkpoint_path)

    return _spawn_bg_child(
        cmd=cmd,
        web_port=web_port,
        pid_workflow_ref=pid_workflow_ref,
        forced_run_id=forced_run_id,
    )


def _serialize_value(value: Any) -> str:
    """Serialize a value for passing as a CLI --metadata argument.

    Args:
        value: The value to serialize.

    Returns:
        String representation suitable for ``key=value`` CLI format.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _serialize_input_value(value: Any) -> str:
    """Serialize an already-typed input value for the ``--input-json`` boundary.

    Unlike :func:`_serialize_value`, this **always** JSON-encodes -- including
    plain strings -- and is decoded on the other side by
    ``cli/run.py::coerce_typed_value`` (a strict ``json.loads``) via the
    hidden ``--input-json`` flag.

    The pairing matters because values reaching here are already coerced to
    their **declared** types (``fleet/launch.py::_coerce_input`` maps the
    New Run form's fields onto the workflow's ``input:`` schema, returning a
    ``string``-typed value verbatim), and nothing downstream restores that
    typing: the engine's ``_apply_input_defaults`` only fills in *missing*
    inputs, so whatever the child's CLI parse produces is final. Routing
    these through the public ``--input`` flag instead would hand them to
    ``coerce_value``, whose command-line heuristic re-guesses a bare
    ``true``/``42``/``[1,2]`` into a bool/int/list -- silently discarding the
    declared ``string`` type the form was careful to preserve.

    Args:
        value: The already-coerced value to serialize.

    Returns:
        A JSON-encoded string representation suitable for ``key=value`` CLI
        format.
    """
    return json.dumps(value)
