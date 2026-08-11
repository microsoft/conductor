"""Identify whether a PID-file entry is the run this process executes inside.

Issue #399: an agent smoke-testing ``conductor stop`` sent it against the
background workflow that was, itself, executing the agent — the process
printed a "Stopped" message and was killed by the very thing it printed. This
module answers one question — *is this PID-file entry the run I am currently
running inside?* — so ``cli/app.py::stop`` can exclude that entry from
targeting by default.

Three independent signals are tried, in order (first match wins per entry):

1. **``CONDUCTOR_RUN_ID`` env var** matches the PID file's ``run_id``.
   ``cli/bg_runner.py::_build_bg_env`` sets this on the detached background
   child, so every descendant of it — including an agent's ``bash`` tool and
   the ``conductor stop`` it spawns — inherits it, and
   ``_finalize_background_launch`` writes the same id into the PID file's
   ``run_id`` key (added in issue #411).
2. **``CONDUCTOR_WEB_BG=1`` + ``CONDUCTOR_WEB_PORT``** matching the entry's
   ``port``, used *only* when the entry records no ``run_id`` (i.e. ``""``).
   This is the compatibility path for PID files written before #411. Limiting
   it to entries with no recorded id means an entry whose id is present and
   *different* from ours is never misidentified as self.
3. **Process ancestry.** A Linux ``/proc/<pid>/status`` ``PPid:`` walk
   upward from ``os.getpid()``, plus a POSIX ``os.getsid(0)`` check. The
   session check is precise for background runs specifically because
   ``bg_runner._detachment_kwargs()`` passes ``start_new_session=True``,
   making the bg child a session leader whose session id equals its own pid
   — so any descendant, even one re-parented away from the direct ancestry
   chain, still resolves to it.

``os.getpid()`` itself is always in the identity set: a PID file naming the
very process running ``stop`` is definitionally self, and SIGTERM-ing
yourself is never the right behaviour.

**Windows caveat**: signal 3 (process ancestry) is POSIX-only. On Windows,
self-identification relies solely on signals 1 and 2 (the env vars). This is
a deliberate limitation rather than an implementation gap — a
``CreateToolhelp32Snapshot``-based ancestry walk would be unexercised by
conductor's ubuntu-only CI, so it is documented here instead: an agent on
Windows whose tool runner strips ``CONDUCTOR_*`` env vars before spawning
its shell is still exposed to this issue.
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Mirrors the names ``cli/bg_runner.py::_build_bg_env`` writes into the
# background child's environment.
RUN_ID_ENV = "CONDUCTOR_RUN_ID"
WEB_BG_ENV = "CONDUCTOR_WEB_BG"
WEB_PORT_ENV = "CONDUCTOR_WEB_PORT"

# Bounds the ``/proc`` ancestry walk so a malformed or cyclic ``PPid`` chain
# cannot loop indefinitely.
_MAX_ANCESTRY_HOPS = 64


def _read_ppid(pid: int) -> int | None:
    """Return the parent PID of *pid* by reading ``/proc/<pid>/status``.

    This is the one seam tests monkeypatch to drive :func:`own_run_pids`'
    ancestry walk deterministically, independent of the real process tree.

    Args:
        pid: The process ID to look up.

    Returns:
        The parent PID, or ``None`` if it cannot be determined (the file
        does not exist, is unreadable, or has no parseable ``PPid:`` line).
    """
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return None

    for line in status.splitlines():
        if line.startswith("PPid:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def own_run_pids() -> frozenset[int]:
    """Return the set of PIDs that identify "this process" for self-exclusion.

    Includes ``os.getpid()`` unconditionally, then walks ``/proc/<pid>/status``
    ``PPid:`` links upward (bounded by :data:`_MAX_ANCESTRY_HOPS`, with a
    ``seen`` set so a malformed cyclic chain also terminates), then adds
    ``os.getsid(0)`` when available.

    Deliberately not memoized: a ``stop`` invocation calls this once, and
    caching would force every test to remember to clear it.

    Returns:
        A frozen set of PIDs considered to be "this run" for the purpose of
        excluding a PID-file entry from ``conductor stop`` targeting.
    """
    pids: set[int] = {os.getpid()}

    if Path("/proc/self/status").exists():
        seen: set[int] = set()
        current = os.getpid()
        for _ in range(_MAX_ANCESTRY_HOPS):
            if current in seen:
                break
            seen.add(current)
            parent = _read_ppid(current)
            if parent is None or parent in pids:
                break
            pids.add(parent)
            current = parent

    if hasattr(os, "getsid"):
        with contextlib.suppress(OSError):
            pids.add(os.getsid(0))

    return frozenset(pids)


@dataclass(frozen=True)
class OwnRunPartition:
    """The result of classifying PID-file entries against this process's identity."""

    others: list[dict]
    """Entries that are not this run — the only ones ``stop`` should target by default."""

    own: list[dict]
    """Entries identified as this run."""

    reasons: dict[int, str]
    """Why an entry (keyed by port) was classified as own: ``"run id"``,
    ``"dashboard port"``, or ``"process ancestry"``. Logged at debug so a
    false positive is diagnosable without being printed to the user."""


def partition_own_run(entries: list[dict]) -> OwnRunPartition:
    """Split PID-file entries into "others" and "own" (this process's run).

    Computes this process's identity (:func:`own_run_pids` plus the bg-launch
    env vars) once, then classifies each entry against it in order:
    ``run_id`` match, then the legacy dashboard-port compatibility signal
    (only for entries with no recorded ``run_id``), then process ancestry.

    Args:
        entries: PID-file dicts, each with at least ``pid`` and ``port``
            (and typically ``run_id``, ``workflow``).

    Returns:
        An :class:`OwnRunPartition` with ``others``/``own`` preserving the
        input order, and a ``reasons`` map for diagnostics.
    """
    my_pids = own_run_pids()
    my_run_id = os.environ.get(RUN_ID_ENV, "")
    web_bg = os.environ.get(WEB_BG_ENV) == "1"
    my_web_port = os.environ.get(WEB_PORT_ENV, "")

    others: list[dict] = []
    own: list[dict] = []
    reasons: dict[int, str] = {}

    for entry in entries:
        port = entry.get("port")
        entry_run_id = entry.get("run_id") or ""
        reason: str | None = None

        if my_run_id and entry_run_id and entry_run_id.lower() == my_run_id.lower():
            reason = "run id"
        elif not entry_run_id and web_bg and my_web_port and str(port) == my_web_port:
            reason = "dashboard port"
        elif entry.get("pid") in my_pids:
            reason = "process ancestry"

        if reason is not None:
            own.append(entry)
            if isinstance(port, int):
                reasons[port] = reason
            logger.debug("PID-file entry on port %s identified as own run (%s)", port, reason)
        else:
            others.append(entry)

    return OwnRunPartition(others=others, own=own, reasons=reasons)


def describe_own_run(entry: dict) -> str:
    """Return the identity fragment used to name the caller's own run in messages.

    Args:
        entry: The PID-file dict identified as this process's own run.

    Returns:
        The ``run_id`` when the PID file records one, otherwise
        ``"<workflow-stem> (port N)"``. Always a plain ``str`` — never a
        ``Text`` — since callers interpolate it through ``styled()`` (markup
        guard rule F).
    """
    run_id = entry.get("run_id")
    if run_id:
        return str(run_id)
    workflow = Path(entry.get("workflow", "unknown")).stem
    return f"{workflow} (port {entry.get('port')})"
