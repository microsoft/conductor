"""Launch-directory resolution for ``conductor mcp serve`` (issue #544).

MCP tools carry no filesystem-path parameter (NFR3), so a workflow launched
by a generated tool has always inherited the server process's own current
working directory -- typically the directory a Copilot *plugin* was
installed into, not the repository the operator is actually working in.
This module fixes the default without changing that contract: it captures
the Copilot host process's cwd once, at server startup, and that snapshot
-- not a historical or per-invocation directory -- is what every launch
uses for the rest of the connection.

Resolution order, decided once at :class:`~conductor.mcp.serve.options.ServeOptions`
construction and never re-evaluated per call:

1. An explicit ``--launch-dir`` override (:func:`resolve_launch_dir`'s
   ``explicit`` argument) -- bypasses ancestor detection entirely.
2. The nearest ancestor process positively identified as the Copilot host
   (:func:`detect_copilot_ancestor_cwd`), walking through any intervening
   launch wrappers.
3. The server's own startup cwd, unchanged from today's behavior.

Detection uses ``psutil`` for portable process inspection (Linux, macOS,
Windows) rather than reading ``/proc`` directly the way
``cli/self_run.py::_read_ppid`` does -- that helper only reads a PID's
*parent id*, not another process's name or cwd, and is Linux-only besides.
Only the direct ancestor chain of this process is ever inspected; nothing
here enumerates unrelated processes on the machine.

Current Copilot npm packages launch a native executable named exactly
``copilot`` (POSIX) or ``copilot.exe`` (Windows) -- see
:data:`_COPILOT_EXECUTABLE_BASENAMES`. Detection matches on the basename
of ``psutil.Process.exe()`` -- the actual executable on disk -- rather
than ``Process.name()``, which is process-manager-reported and need not
match the executable at all: a real Copilot host process has been
observed reporting a ``name()`` of ``MainThread`` while its ``exe()``
remained ``.../copilot``. Matching by executable basename also means
detection does not search command-line arguments for the substring
"copilot", which could match an unrelated tool.

Nothing here ever calls ``os.chdir()``. The resolved directory only ever
reaches :func:`conductor.cli.bg_runner.launch_background`'s ``cwd``
keyword for the detached workflow child; ``--workflow-dir`` discovery and
registry resolution keep their existing, independent meaning.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import psutil

from conductor.exceptions import ConductorError

logger = logging.getLogger(__name__)

# Current Copilot npm packages (`@github/copilot`) launch a native
# executable under exactly one of these basenames. Matched against
# `psutil.Process.exe()` -- the actual executable path -- never against
# `Process.name()`, which is process-manager-reported and has been
# observed to diverge from the executable (a real Copilot host reporting
# `name() == "MainThread"`). Compared case-folded so a Windows path's
# casing (e.g. `Copilot.EXE`) still matches.
_COPILOT_EXECUTABLE_BASENAMES = frozenset({"copilot", "copilot.exe"})

# Bounds the ancestor walk exactly like `cli/self_run.py::_MAX_ANCESTRY_HOPS`
# -- a malformed or cyclic parent chain must terminate rather than loop
# indefinitely, and a chain this deep could never be a real launch wrapper
# stack.
_MAX_ANCESTRY_HOPS = 64

_LAUNCH_DIR_REMEDY = "Pass --launch-dir <path> to specify the execution directory explicitly."


class LaunchDirectoryError(ConductorError):
    """Raised when the effective launch directory cannot be established.

    Always a startup-time failure: it is only ever raised while resolving
    :class:`~conductor.mcp.serve.options.ServeOptions`, before the
    catalogue is built or stdio is opened, so a misconfigured or
    unreachable directory never reaches a launched workflow.
    """


@dataclass(frozen=True, slots=True)
class _ProcessInfo:
    """The minimal process descriptor detection needs, decoupled from
    ``psutil.Process`` so tests can substitute a synthetic ancestry without
    spawning real processes -- mirroring ``cli/self_run.py::_read_ppid``'s
    role for PPid-only ancestry."""

    pid: int
    name: str
    exe_basename: str | None
    """The case-folded basename of the process's executable
    (``psutil.Process.exe()``), or ``None`` when that could not be read
    (e.g. an OS permission error scoped to that one attribute). This --
    not :attr:`name`, which is process-manager-reported and need not
    match the executable -- is what host detection matches against."""
    cwd: Path | None
    """``None`` when the name was readable but the cwd was not (e.g. an
    OS permission error scoped to that one attribute)."""


def _parent_process_info(pid: int) -> _ProcessInfo | None:
    """Return info about *pid*'s immediate parent process, or ``None``.

    ``None`` covers every *ordinary* end of an ancestry walk: *pid* itself
    no longer exists, has no parent (the root of the tree), or the parent
    exited in the moment between being found and being inspected. None of
    these indicate an inspection *limitation* -- they are exactly what an
    ancestry walk reaching its top looks like.

    This is the one seam tests monkeypatch to drive ancestor detection
    deterministically, independent of the real process tree.

    Raises:
        psutil.AccessDenied: If the OS refuses to identify *pid*'s parent
            at all (as opposed to refusing only its cwd or exe, which are
            each reported via a ``None`` field on :class:`_ProcessInfo`
            instead). This is deliberately allowed to propagate: an
            access-denied parent might have been the Copilot host, so the
            walk cannot silently treat it as "no more ancestors" the way
            it does a vanished process.
    """
    try:
        proc = psutil.Process(pid)
        parent = proc.parent()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    if parent is None:
        return None
    try:
        name = parent.name()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None

    exe_basename: str | None
    try:
        exe_basename = os.path.basename(parent.exe()).casefold()
    except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
        exe_basename = None

    cwd: Path | None
    try:
        cwd = Path(parent.cwd())
    except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
        cwd = None

    return _ProcessInfo(pid=parent.pid, name=name, exe_basename=exe_basename, cwd=cwd)


@dataclass(frozen=True, slots=True)
class _AncestorScan:
    """The outcome of walking a process's ancestry for a Copilot host."""

    cwd: Path | None
    """The nearest Copilot ancestor's cwd, or ``None`` if none was found."""

    limited: bool
    """Whether the walk could not be completed cleanly (a permission error
    mid-walk, or exhausting :data:`_MAX_ANCESTRY_HOPS` without reaching
    the top of the tree) rather than simply never crossing a
    Copilot-named process. Only this case warrants a startup warning --
    an ordinary walk that never finds a Copilot ancestor (the common case:
    a developer running the server by hand) is expected and silent."""


def _scan_for_copilot_ancestor(start_pid: int) -> _AncestorScan:
    """Walk ``start_pid``'s ancestors looking for a Copilot host process.

    Raises:
        LaunchDirectoryError: If a Copilot host is positively identified
            but its cwd cannot be read -- launching in the plugin
            directory (this process's own inherited cwd) would be worse
            than failing loudly with an actionable remedy.
    """
    seen = {start_pid}
    current = start_pid
    for _ in range(_MAX_ANCESTRY_HOPS):
        try:
            info = _parent_process_info(current)
        except psutil.AccessDenied:
            return _AncestorScan(cwd=None, limited=True)

        if info is None:
            return _AncestorScan(cwd=None, limited=False)
        if info.pid in seen:
            # A cyclic ancestry chain -- treat it the same as reaching the
            # top: it cannot contain a Copilot ancestor we have not
            # already ruled out.
            return _AncestorScan(cwd=None, limited=False)
        seen.add(info.pid)

        if info.exe_basename in _COPILOT_EXECUTABLE_BASENAMES:
            if info.cwd is None:
                raise LaunchDirectoryError(
                    f"Detected a Copilot host process (pid {info.pid}) but could not "
                    "read its working directory.",
                    suggestion=_LAUNCH_DIR_REMEDY,
                )
            return _AncestorScan(cwd=info.cwd, limited=False)

        current = info.pid

    # The bound was exhausted without reaching the top of the tree or
    # finding a match -- a real Copilot ancestor could still be further up,
    # so this is a limitation, not a clean "no host" result.
    return _AncestorScan(cwd=None, limited=True)


def detect_copilot_ancestor_cwd(pid: int | None = None) -> Path | None:
    """Return the nearest Copilot-host ancestor's cwd, or ``None``.

    Logs a warning (never raises for this outcome) when the walk could
    not be completed cleanly, since that is the one case where "no host
    found" does not necessarily mean there isn't one.

    Args:
        pid: The process to walk ancestors from. Defaults to this
            process's own pid.

    Raises:
        LaunchDirectoryError: See :func:`_scan_for_copilot_ancestor`.
    """
    scan = _scan_for_copilot_ancestor(os.getpid() if pid is None else pid)
    if scan.cwd is not None:
        return scan.cwd
    if scan.limited:
        logger.warning(
            "Could not fully inspect this process's ancestry to detect a Copilot host "
            "process (a permission error or an unusually deep launch-wrapper chain); "
            "falling back to this server's own startup directory. %s",
            _LAUNCH_DIR_REMEDY,
        )
    return None


def normalize_launch_dir(path: Path) -> Path:
    """Expand ``~`` and make *path* absolute without resolving symlinks.

    Mirrors this repo's existing "normpath, not resolve" convention
    (``fleet/launch.py::resolve_workflow``, ``_resolve_agent_working_dir``)
    so a symlinked project directory stays the alias the operator typed
    rather than being collapsed to its real path.
    """
    return Path(os.path.abspath(path.expanduser()))


def validate_launch_dir(path: Path) -> None:
    """Confirm *path* is a directory this process can read.

    Raises:
        LaunchDirectoryError: If *path* does not exist, is not a
            directory, or cannot be inspected (e.g. a permission error).
    """
    try:
        is_dir = path.is_dir()
    except OSError as exc:
        raise LaunchDirectoryError(
            f"Could not access launch directory {path}: {exc}.",
            suggestion=_LAUNCH_DIR_REMEDY,
        ) from exc
    if is_dir:
        return
    if path.exists():
        raise LaunchDirectoryError(
            f"Launch directory {path} is not a directory.",
            suggestion=_LAUNCH_DIR_REMEDY,
        )
    raise LaunchDirectoryError(
        f"Launch directory {path} does not exist.",
        suggestion=_LAUNCH_DIR_REMEDY,
    )


def resolve_launch_dir(explicit: Path | None) -> Path:
    """Resolve the effective launch directory (issue #544).

    Precedence: *explicit* (bypasses ancestor detection entirely); the
    nearest Copilot-host ancestor's cwd; this server's own startup cwd.

    Deliberately returns the **raw** resolved path, neither normalized nor
    validated: :class:`~conductor.mcp.serve.options.ServeOptions.__post_init__`
    applies :func:`normalize_launch_dir` and :func:`validate_launch_dir` to
    whatever this function returns, so an explicit and a detected value
    receive the exact same treatment exactly once, regardless of which
    branch below produced it.

    Args:
        explicit: An operator-supplied ``--launch-dir`` value, or
            ``None`` to detect a default.

    Returns:
        The resolved launch directory, not yet normalized or validated.
    """
    if explicit is not None:
        return explicit

    detected = detect_copilot_ancestor_cwd()
    if detected is not None:
        return detected

    return Path.cwd()
