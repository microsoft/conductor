"""``$TMPDIR/conductor/`` event-log retention (Fleet Manager E5 — D3).

Bounds the unbounded event-log directory (1522 files / 12 MB observed per
the design's *Second-order cleanup*) using the same ``keep_last``
vocabulary already established by
``CheckpointManager.rotate_periodic_checkpoints``
(``engine/checkpoint.py:530``), rather than inventing a second policy
language.

Two constraints hold regardless of ``keep_last``:

- Never descend into the ``checkpoints/`` subdirectory
  (``engine/checkpoint.py:158`` places it inside this same
  ``$TMPDIR/conductor/`` directory). This falls out naturally from using a
  non-recursive glob rooted at ``event_log_root()`` — ``Path.glob`` with a
  plain (non-``**``) pattern never descends into child directories.
- Never delete an event log a live run record still points to — a
  ``resume`` may be actively appending to it
  (``engine/event_log.py:113``). Liveness is sourced from
  ``conductor.fleet.records.read_run_records()``, the same liveness
  primitive the rest of the Fleet Manager uses.

A retained (or live) events log's ``.bg.stderr.log`` / ``.bg.stdout.log``
companions (written by ``cli/bg_runner.py`` for a ``--web-bg`` launch) are
pruned or kept together with it, so the three artefacts of one run are
never split apart. The companions and the events log are matched by the
shared ``run_id`` embedded in each filename, not by a full filename
prefix: the events log's ``ts`` segment (written by the workflow child,
``engine/event_log.py``) and the bg log files' ``ts`` segment (written
independently by the parent, ``cli/bg_runner.py::_open_bg_log_files``)
can differ by a second or more when the two processes cross a clock
tick, so only ``run_id`` is guaranteed to match.
"""

from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_EVENT_LOG_GLOB = "conductor-*.events.jsonl"
_BG_STDERR_SUFFIX = ".bg.stderr.log"
_BG_STDOUT_SUFFIX = ".bg.stdout.log"
_RUN_ID_FROM_EVENT_LOG = re.compile(r"-([0-9a-fA-F]{1,32})\.events\.jsonl$")


def event_log_root() -> Path:
    """Return ``$TMPDIR/conductor/``, creating it if needed.

    The same directory ``conductor.engine.event_log.EventLogSubscriber``
    writes event logs to, and that also holds the ``checkpoints/``
    subdirectory this module must never touch.

    Raises:
        RuntimeError: If ``$TMPDIR/conductor`` already exists as a
            symlink. ``$TMPDIR`` is a world-writable shared directory, so
            an attacker (or a stale/malicious leftover) could replace this
            path with a symlink pointing anywhere on the filesystem; since
            this module deletes files under this root, following such a
            symlink would make it possible to delete files outside the
            intended directory. Refusing outright is safer than silently
            resolving through it.
    """
    path = Path(tempfile.gettempdir()) / "conductor"
    if path.is_symlink():
        raise RuntimeError(
            f"Refusing to use {path} for event-log retention: it is a symlink, "
            "not a real directory. This could indicate tampering with a "
            "shared temporary directory."
        )
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class PruneResult:
    """Outcome of a single :func:`prune_event_logs` sweep."""

    deleted: list[Path] = field(default_factory=list)
    """Files actually removed (or, for ``dry_run=True``, that *would* have
    been removed). Includes any ``.bg.stderr.log`` / ``.bg.stdout.log``
    companions of a deleted events log."""

    skipped_live: list[Path] = field(default_factory=list)
    """Event logs that were past ``keep_last`` by age but were retained
    anyway because a live run record still references them."""

    failed: list[tuple[Path, str]] = field(default_factory=list)
    """``(path, reason)`` for each file this sweep tried and failed to
    delete. Populated only for a real ``OSError`` — a file that was already
    gone is not a failure, since "absent" is the outcome the caller wanted.
    A caller that renders only ``deleted`` reports a success it did not
    achieve: a read-only or root-owned log directory fails *every* deletion,
    forever, and looks identical to having nothing to prune."""

    error: str | None = None
    """Non-None when the sweep aborted before completing, leaving every file
    in place. :func:`prune_event_logs` still never raises — the opportunistic
    startup sweep depends on that — so this is how a caller that is an
    *explicit* user request tells "the sweep failed" apart from "there was
    nothing to do"."""


def _companion_paths(event_log: Path) -> list[Path]:
    """Return the ``.bg.stderr.log`` / ``.bg.stdout.log`` companions of ``event_log``.

    Bg log companions share the events log's ``run_id`` (see
    ``cli/bg_runner.py::_open_bg_log_files`` and
    ``engine/event_log.py``'s default-path construction) but **not**
    necessarily its full filename prefix: the ``ts`` segment is generated
    independently by the parent (bg log files) and the child (events log)
    processes, and a clock tick between the two calls makes them differ.
    Matching is therefore done by extracting ``run_id`` from the events
    log's filename and globbing for companions ending in that same
    ``run_id``. Only paths that actually exist are returned — a
    foreground or foreground+web run has no such companions.
    """
    match = _RUN_ID_FROM_EVENT_LOG.search(event_log.name)
    if match is None:
        return []
    run_id = match.group(1)
    parent = event_log.parent
    patterns = [
        f"conductor-*-{run_id}{_BG_STDERR_SUFFIX}",
        f"conductor-*-{run_id}{_BG_STDOUT_SUFFIX}",
    ]
    companions: list[Path] = []
    for pattern in patterns:
        companions.extend(p for p in parent.glob(pattern) if p.is_file())
    return companions


def _live_event_log_paths() -> set[Path] | None:
    """Return every ``event_log_path`` referenced by a currently-live run record.

    Returns ``None`` (rather than an empty set) when liveness can't be
    determined, so the caller fails **closed**: an empty set would be
    read as "nothing is live," which would let the sweep delete an
    actively-appended event log the moment run-record discovery hiccups.
    ``None`` instead tells the caller to skip pruning entirely for this
    sweep. :func:`conductor.fleet.records.read_run_records` is itself
    documented never to raise, but this is wrapped defensively anyway.
    """
    from conductor.fleet.records import read_run_records

    try:
        records = read_run_records()
    except Exception:
        logger.warning("Failed to read run records for retention liveness check", exc_info=True)
        return None
    return {Path(r.event_log_path) for r in records if r.event_log_path}


def _safe_mtime(path: Path) -> float:
    """Return ``path``'s mtime, or ``-inf`` if it can't be stat'd.

    A file that vanishes mid-scan (or is otherwise unreadable) sorts as
    "oldest" rather than aborting the whole sweep or crashing the sort.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return float("-inf")


def _safe_unlink(path: Path) -> tuple[bool, str | None]:
    """Best-effort delete of ``path``, never raising.

    Returns:
        ``(removed, failure_reason)``. ``removed`` is True only when this
        call's ``unlink()`` actually removed the file. ``failure_reason`` is
        non-None only when an ``OSError`` prevented removal; a file that was
        already absent is neither removed nor a failure, because "gone" is
        the outcome the caller asked for. The two are distinct so the caller
        can report a refused deletion instead of silently omitting it.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        logger.warning("Could not delete event log file: %s", path, exc_info=True)
        return False, f"{type(exc).__name__}: {exc}"
    return True, None


def prune_event_logs(*, keep_last: int, dry_run: bool = False) -> PruneResult:
    """Delete event logs under ``event_log_root()`` beyond the newest ``keep_last``.

    Sorted newest-first by mtime, mirroring
    ``CheckpointManager.rotate_periodic_checkpoints``'s semantics
    (including its ``keep_last < 1`` guard: a non-positive ``keep_last``
    means "prune nothing", not "delete everything" — without this guard, a
    negative ``keep_last`` would produce a negative slice that *retains*
    exactly the files it was meant to delete). An event log still
    referenced by a live run record is never deleted regardless of age,
    and a retained/live events log's ``.bg.stderr.log`` /
    ``.bg.stdout.log`` companions are always kept alongside it.

    Best-effort: this function never raises. Any unexpected failure
    during the sweep is logged and treated as "nothing was pruned" —
    callers on the opportunistic startup path
    (``cli/run.py``) rely on this so a retention bug can never break
    ``conductor run``.

    Args:
        keep_last: Number of most-recent event logs to retain.
        dry_run: When True, compute what would be deleted without
            actually deleting anything.

    Returns:
        A :class:`PruneResult` describing what was (or would be) deleted,
        which normally-prunable logs were skipped because a live run still
        references them, which deletions were refused, and — via
        :attr:`PruneResult.error` — whether the sweep aborted wholesale.
    """
    try:
        return _prune_event_logs_impl(keep_last=keep_last, dry_run=dry_run)
    except Exception as exc:
        logger.warning("Event-log retention sweep failed; leaving files in place", exc_info=True)
        return PruneResult(error=f"{type(exc).__name__}: {exc}")


def _prune_event_logs_impl(*, keep_last: int, dry_run: bool) -> PruneResult:
    """The actual sweep, allowed to raise -- wrapped by :func:`prune_event_logs`."""
    # Mirrors CheckpointManager.rotate_periodic_checkpoints's guard: without
    # it, `candidates[keep_last:]` with a negative keep_last would retain
    # (not delete) exactly the files meant to be pruned.
    if keep_last < 1:
        return PruneResult()

    root = event_log_root()
    live_paths = _live_event_log_paths()
    if live_paths is None:
        # Fail closed: liveness could not be determined, so treat this
        # sweep as "nothing is safely prunable" rather than risk deleting
        # an event log a live run is actively appending to.
        return PruneResult()

    # Non-recursive glob: never descends into the checkpoints/ subdirectory
    # that also lives under `root`. Filter to files only -- a directory
    # that happens to match the glob pattern must not consume a
    # `keep_last` slot meant for real event log files.
    candidates = sorted(
        (p for p in root.glob(_EVENT_LOG_GLOB) if p.is_file()), key=_safe_mtime, reverse=True
    )

    prune_candidates = candidates[keep_last:]

    deleted: list[Path] = []
    skipped_live: list[Path] = []
    failed: list[tuple[Path, str]] = []

    for event_log in prune_candidates:
        if event_log in live_paths:
            skipped_live.append(event_log)
            continue

        companions = _companion_paths(event_log)
        if dry_run:
            deleted.append(event_log)
            deleted.extend(companions)
            continue

        for target in (event_log, *companions):
            removed, reason = _safe_unlink(target)
            if removed:
                deleted.append(target)
            elif reason is not None:
                failed.append((target, reason))

    return PruneResult(deleted=deleted, skipped_live=skipped_live, failed=failed)


def maybe_prune_event_logs() -> PruneResult | None:
    """Run the settings-driven retention sweep, if enabled.

    The single entry point ``cli/run.py``'s opportunistic startup sweep
    calls (E5-T4): reads ``~/.conductor/config.toml`` via
    :func:`conductor.settings.load_settings` (which can raise
    ``ConductorError`` on malformed TOML) and, only when
    ``[fleet.retention].enabled`` is true, delegates to
    :func:`prune_event_logs` (which never raises on its own). Combining
    both under one try/except here gives every caller — the startup sweep
    included — a single "never raises" surface for the whole opt-in
    feature, per D3's requirement that a machine-wide settings file must
    never break ``conductor run``.

    Returns:
        The :class:`PruneResult` from the sweep, or ``None`` when
        retention is disabled, the settings file could not be loaded, or
        any other unexpected error occurred — distinguishing "did nothing
        because it isn't configured / is broken" from a real
        :class:`PruneResult` reporting nothing to prune.
    """
    try:
        from conductor.settings import load_settings

        settings = load_settings()
    except Exception:
        logger.warning(
            "Failed to load Conductor settings for the retention sweep; skipping",
            exc_info=True,
        )
        return None

    if not settings.fleet.retention.enabled:
        return None

    # Inside the try, not after it: `prune_event_logs` builds its error
    # string with `str(exc)`, which is not itself guaranteed safe, and this
    # function's contract is a single never-raises surface for the whole
    # opt-in feature -- a machine-wide settings file must never break
    # `conductor run` (D3).
    try:
        return prune_event_logs(keep_last=settings.fleet.retention.keep_last)
    except Exception:
        logger.warning("Event-log retention sweep failed unexpectedly", exc_info=True)
        return None
