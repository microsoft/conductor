"""Identify whether a PID-file entry is the run this process executes inside.

Issue #399: an agent smoke-testing ``conductor stop`` sent it against the
background workflow that was, itself, executing the agent — the process
printed a "Stopped" message and was killed by the very thing it printed. This
module answers one question — *is this PID-file entry the run I am currently
running inside?* — so ``cli/app.py::stop`` can exclude that entry from
targeting by default.

Three independent signals are tried, in order (first match wins per entry):

1. **``CONDUCTOR_RUN_ID`` env var** matches the PID file's ``run_id``
   (case-insensitively, since a manually-exported env var could differ in
   case from the minted lowercase id). ``cli/bg_runner.py::_build_bg_env``
   sets this on the detached background child, so every descendant of it —
   including an agent's ``bash`` tool and the ``conductor stop`` it spawns —
   inherits it, and ``_finalize_background_launch`` writes the same id into
   the PID file's ``run_id`` key (added in issue #411).
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

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conductor.fleet.records import RunRecord

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
        # errors="replace": the kernel-sourced ``Name:`` line can be
        # arbitrary, non-UTF-8 bytes (any unprivileged process can set its
        # own via ``prctl(PR_SET_NAME, ...)``), but only ``PPid:`` and the
        # digits after it are ever parsed below, so corruption elsewhere in
        # the file is harmless — raising here would needlessly crash the
        # whole ancestry walk (and the ``os.getsid(0)`` fallback after it)
        # over a process with an unrelated garbled name.
        status = Path(f"/proc/{pid}/status").read_text(errors="replace")
    except OSError as exc:
        logger.debug("Could not read /proc/%s/status: %s", pid, exc)
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
    ``PPid:`` links upward (bounded by :data:`_MAX_ANCESTRY_HOPS`; a cyclic
    chain terminates immediately, before the hop cap, because each new
    parent is checked against every pid collected so far), then adds
    ``os.getsid(0)`` when available.

    Deliberately not memoized: a ``stop`` invocation calls this once, and
    caching would force every test to remember to clear it.

    Returns:
        A frozen set of PIDs considered to be "this run" for the purpose of
        excluding a PID-file entry from ``conductor stop`` targeting.
    """
    pids: set[int] = {os.getpid()}

    if Path("/proc/self/status").exists():
        current = os.getpid()
        for _ in range(_MAX_ANCESTRY_HOPS):
            parent = _read_ppid(current)
            if parent is None or parent in pids:
                break
            pids.add(parent)
            current = parent

    if hasattr(os, "getsid"):
        try:
            pids.add(os.getsid(0))
        except OSError as exc:
            logger.debug("os.getsid(0) failed: %s", exc)

    return frozenset(pids)


@dataclass(frozen=True, slots=True)
class OwnRunPartition:
    """The result of classifying run records against this process's identity."""

    others: list[RunRecord]
    """Records that are not this run — the only ones ``stop`` should target by default."""

    own: list[RunRecord]
    """Records identified as this run."""

    reasons: dict[int, str]
    """Why a record (keyed by PID) was classified as own: ``"run id"``,
    ``"dashboard port"``, or ``"process ancestry"``. Keyed by PID rather than
    port because a foreground run has no port at all (Fleet Manager: ``fg``
    records carry ``port=None``), so port would silently collapse every
    portless record onto a single ``None`` key. Logged at debug so a false
    positive is diagnosable without being printed to the user."""

    def __post_init__(self) -> None:
        """Guard the one invariant that matters for safety: no record is in both lists.

        ``others``/``own`` are both plain ``list[RunRecord]`` — nothing in the
        type system stops a future edit to :func:`partition_own_run` from
        swapping them, which would silently invert exactly the safety property
        this module exists to provide. Catching it here, at construction, turns
        that mistake into an immediate ``ValueError`` instead of a quiet
        misclassification.
        """
        own_pids = {r.pid for r in self.own}
        other_pids = {r.pid for r in self.others}
        overlap = own_pids & other_pids
        if overlap:
            raise ValueError(
                f"OwnRunPartition: record{'' if len(overlap) == 1 else 's'} with "
                f"PID(s) {sorted(overlap)} classified as both own and other"
            )


def partition_own_run(records: list[RunRecord]) -> OwnRunPartition:
    """Split run records into "others" and "own" (this process's run).

    Computes this process's identity (:func:`own_run_pids` plus the bg-launch
    env vars) once, then classifies each record against it in order:
    ``run_id`` match, then the legacy dashboard-port compatibility signal
    (only for records with no recorded ``run_id``), then process ancestry.

    Args:
        records: Run records, as returned by
            :func:`conductor.fleet.records.read_run_records` — which also
            surfaces legacy port-keyed ``.pid`` files in this same shape, so
            this function never needs to handle raw PID-file dicts.

    Returns:
        An :class:`OwnRunPartition` with ``others``/``own`` preserving the
        input order, and a ``reasons`` map (keyed by PID) for diagnostics.
    """
    my_pids = own_run_pids()
    my_run_id = os.environ.get(RUN_ID_ENV, "")
    web_bg = os.environ.get(WEB_BG_ENV) == "1"
    my_web_port = os.environ.get(WEB_PORT_ENV, "")

    others: list[RunRecord] = []
    own: list[RunRecord] = []
    reasons: dict[int, str] = {}

    for record in records:
        port = record.port
        record_run_id = record.run_id or ""
        reason: str | None = None

        if my_run_id and record_run_id and record_run_id.lower() == my_run_id.lower():
            reason = "run id"
        elif not record_run_id and web_bg and my_web_port and str(port) == my_web_port:
            reason = "dashboard port"
        elif record.pid in my_pids:
            reason = "process ancestry"

        if reason is not None:
            own.append(record)
            reasons[record.pid] = reason
            logger.debug(
                "Run record (PID %s, port %s) identified as own run (%s)",
                record.pid,
                port,
                reason,
            )
        else:
            others.append(record)

    return OwnRunPartition(others=others, own=own, reasons=reasons)


def describe_own_run(record: RunRecord) -> str:
    """Return the identity fragment used to name the caller's own run in messages.

    Args:
        record: The run record identified as this process's own run.

    Returns:
        The ``run_id`` when the record has one, otherwise
        ``"<workflow-stem> (port N)"`` — or ``"<workflow-stem> (PID N)"`` for
        a foreground run, which has no port to name it by. Always a plain
        ``str`` — never a ``Text`` — since there's no styling to preserve
        here, and keeping it a plain string forecloses a future f-string
        interpolation mistake (markup guard rule F) regardless of which
        mechanism a caller uses to print it.
    """
    if record.run_id:
        return record.run_id
    workflow = record.workflow_name or Path(str(record.workflow_path or "unknown")).stem
    if record.port is None:
        return f"{workflow} (PID {record.pid})"
    return f"{workflow} (port {record.port})"
