"""Correlate History rows to resumable checkpoints (issue #460).

The fleet layer's first checkpoint consumer. :mod:`conductor.fleet.history`
enumerates completed runs from retained event logs; this module joins that
list against :func:`conductor.engine.checkpoint.CheckpointManager.
list_checkpoints`'s on-disk checkpoints so the History screen can offer
``r``/Resume on a row that has one.

Kept out of ``fleet/history.py`` deliberately: that module's docstring is
emphatic that History is derived *from event logs*, and
``build_history_entries`` is the symbol several existing tests patch by
name -- widening its contract to also touch checkpoints would blur the
module and break that patching.

**The join key is the event log path, not ``run_id``.** Both a
:class:`~conductor.fleet.history.HistoryEntry` and a
:class:`~conductor.engine.checkpoint.CheckpointData` ultimately derive
their path from ``tempfile.gettempdir()`` (``engine/event_log.py`` and
``engine/checkpoint.py::CheckpointManager.get_checkpoints_dir``), so raw
string equality would nearly always hold; normalizing with
``os.path.realpath`` additionally makes it hold across a symlinked
``$TMPDIR``. ``run_id`` is used only as a **documented fallback**, taken
only when ``event_log_path`` was never recorded (predates that field, or
the log was unavailable at checkpoint time) or the recorded path no
longer exists on disk -- never merely because the primary lookup missed,
since a checkpoint's own log can be pruned while a *different*, still
extant log happens to carry the same ``run_id`` (a nested ``conductor``
invocation inherits ``CONDUCTOR_RUN_ID`` from its parent), which would
otherwise correlate the checkpoint to the wrong row. The fallback is also
refused whenever the candidate ``run_id`` appears on more than one
scanned :class:`~conductor.fleet.history.HistoryEntry`, for the same
sharing reason -- picking either would be a guess, not a join.

**``outcome`` is never inspected.** Whether a row resumes is decided
entirely by "does a checkpoint exist for it, and is it structurally
usable" -- never by whether the row's own event log ended in
``workflow_completed``, ``workflow_failed``, or nothing at all. This cuts
both ways on purpose: an ``unknown`` row (a crash with no terminal event)
is exactly the case a periodic checkpoint exists *for*, so it must offer
Resume; a ``failed`` row from an explicit ``type: terminate`` writes no
checkpoint by design (``engine/workflow.py``'s terminate-step handling),
so it is excluded not by an outcome check but simply because no checkpoint
correlates to it. A ``completed`` row can also correlate to a stale
checkpoint a crashed cleanup left behind -- offering Resume there really
does re-execute already-finished, possibly billable work, but hiding the
key based on outcome would be a second, silent policy diverging from "a
checkpoint exists" and was explicitly rejected in favor of keeping
provenance (the checkpoint's ``created_at``/``current_agent``) visible
in the UI instead.

**A currently-live run is excluded outright, before any of the above
applies** (issue #460 review). ``build_history_entries`` cannot tell an
active run's log from a finished one -- a live run's log has no terminal
event and therefore renders as ``unknown``, exactly like a genuine crash
-- so without this check a workflow with periodic checkpoints enabled (or
one already resumed via the CLI after a failure) would offer ``r`` while
still running. Pressing it would make the new child adopt the *original*
``run_id`` (``cli/bg_runner.py::_peek_resume_run_id``), overwriting the
live run's own record (``fleet/records.py``) and rendering it unstoppable
by ``conductor stop``, while its resumed ``EventLogSubscriber`` appends
into the same JSONL log the still-running process is writing to -- and
the same billable workflow now executes twice concurrently.
:func:`correlate_checkpoints` therefore excludes every entry whose
event-log path is currently referenced by a live run record
(:func:`conductor.fleet.retention._live_event_log_paths`, built from
:func:`conductor.fleet.records.read_run_records`, which itself filters by
:func:`conductor.cli.pid.is_process_alive`) before joining anything. When
liveness cannot be determined at all, this fails **closed** -- it returns
no resumable checkpoints for the whole scan, matching
``_live_event_log_paths``'s own "skip the sweep entirely" posture --
rather than risk offering Resume on a run that might still be live.

**Retention is asymmetric in both directions**, so a row can outlive its
checkpoint and vice versa: ``conductor.fleet.retention.prune_event_logs``
never descends into ``checkpoints/`` (its own docstring), and checkpoint
rotation (``CheckpointManager.rotate_periodic_checkpoints`` /
``cleanup_periodic_for_run``) is entirely independent of event-log
retention. Neither side of the join is authoritative over the other's
lifetime.

:func:`correlate_checkpoints` calls ``list_checkpoints(None)``, which
JSON-parses every file under ``$TMPDIR/conductor/checkpoints/`` (~495 on
the issue author's machine) -- this must therefore only ever be invoked
off the event loop, exactly as ``build_history_entries`` already is.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from conductor.engine.checkpoint import CheckpointManager, CheckpointTrigger
from conductor.fleet.retention import _live_event_log_paths

if TYPE_CHECKING:
    from conductor.fleet.history import HistoryEntry


@dataclass(frozen=True, slots=True)
class ResumableCheckpoint:
    """A checkpoint correlated to one :class:`~conductor.fleet.history.HistoryEntry`."""

    checkpoint_path: Path
    """What ``--from``/``launch_resume`` receives -- ``CheckpointData.file_path``."""

    workflow_path: Path
    """``CheckpointData.workflow_path``, already existence-checked (see module
    docstring's validity filter)."""

    created_at: str
    """ISO-8601 timestamp the checkpoint was written -- shown for provenance
    since Resume is offered regardless of the row's ``outcome``."""

    run_id: str
    """The checkpoint's own recorded run id (``CheckpointData.run_id``), may
    be empty for a checkpoint written before that field existed."""

    current_agent: str
    """The step that was executing (failure) or about to run (periodic)
    when the checkpoint was taken -- shown for provenance."""

    trigger: CheckpointTrigger
    """``"failure"`` or ``"periodic"`` -- ``CheckpointData.trigger``."""

    matched_by: Literal["event_log_path", "run_id"]
    """Which join key produced this correlation -- recorded so a support
    question about a surprising match is answerable."""


def _normalize_log_path(raw: str | Path) -> str:
    """Normalize an event-log path for cross-side comparison.

    ``os.path.realpath`` resolves both relative segments and symlinks (so a
    symlinked ``$TMPDIR`` still joins correctly), and ``os.path.normcase``
    additionally folds case on a case-insensitive filesystem (a no-op on
    POSIX). An empty string normalizes to ``""`` rather than the cwd --
    ``CheckpointData.event_log_path`` uses ``""`` to mean "not recorded",
    and resolving that to a real (and misleading) path would let it falsely
    match an entry whose own log path happens to be the process's cwd.
    """
    text = str(raw)
    if not text:
        return ""
    return os.path.normcase(os.path.realpath(text))


def correlate_checkpoints(entries: Sequence[HistoryEntry]) -> dict[Path, ResumableCheckpoint]:
    """Join retained History rows against on-disk checkpoints.

    Args:
        entries: The current History screen's rows (from
            :func:`conductor.fleet.history.build_history_entries`).

    Returns:
        A mapping from each resumable :class:`~conductor.fleet.history.
        HistoryEntry.path` to the :class:`ResumableCheckpoint` that
        correlates to it. An entry absent from the mapping simply has no
        usable checkpoint -- never inspect ``outcome`` to decide this (see
        module docstring). A currently-live entry is never present in the
        mapping regardless of what checkpoint would otherwise correlate to
        it (see the module docstring's liveness paragraph); when liveness
        can't be determined at all, this returns ``{}`` for the whole scan.

    Raises:
        Exception: Whatever :func:`CheckpointManager.list_checkpoints`
            raises on a directory-level failure (e.g. the checkpoints
            directory cannot be listed at all). Per-file corruption is
            already swallowed inside ``list_checkpoints`` itself. Callers
            must catch this separately from the History load so a
            checkpoint-scan failure never blanks an otherwise-successful
            history listing.
    """
    if not entries:
        return {}

    live_log_paths = _live_event_log_paths()
    if live_log_paths is None:
        # Liveness could not be determined at all -- fail closed for the
        # whole scan rather than risk offering Resume on a run that might
        # still be live. Mirrors `_live_event_log_paths`'s own contract,
        # which asks its callers to skip the operation entirely on `None`.
        return {}
    live_normalized = {_normalize_log_path(p) for p in live_log_paths}
    entries = [entry for entry in entries if _normalize_log_path(entry.path) not in live_normalized]
    if not entries:
        return {}

    entries_by_log_path: dict[str, Path] = {}
    run_id_counts: dict[str, int] = {}
    for entry in entries:
        if entry.run_id:
            run_id_counts[entry.run_id] = run_id_counts.get(entry.run_id, 0) + 1
        normalized = _normalize_log_path(entry.path)
        # First-seen wins on a (pathological) duplicate normalized path --
        # entries are otherwise one distinct file each.
        entries_by_log_path.setdefault(normalized, entry.path)

    ambiguous_run_ids = {run_id for run_id, count in run_id_counts.items() if count > 1}
    entries_by_run_id: dict[str, Path] = {}
    for entry in entries:
        if entry.run_id and entry.run_id not in ambiguous_run_ids:
            entries_by_run_id.setdefault(entry.run_id, entry.path)

    # `list_checkpoints` already returns newest-`created_at`-first, but that
    # ordering is another module's implementation detail and the "newest
    # wins" rule just below depends entirely on it -- a defensive re-sort
    # here makes that dependency explicit and local rather than an
    # undeclared cross-module assumption (issue #460 review).
    checkpoints = sorted(
        CheckpointManager.list_checkpoints(None), key=lambda c: c.created_at, reverse=True
    )

    result: dict[Path, ResumableCheckpoint] = {}

    for cp in checkpoints:
        matched_entry_path: Path | None = None
        matched_by: Literal["event_log_path", "run_id"] | None = None

        if cp.event_log_path:
            candidate = entries_by_log_path.get(_normalize_log_path(cp.event_log_path))
            if candidate is not None:
                matched_entry_path = candidate
                matched_by = "event_log_path"

        if matched_entry_path is None and cp.run_id:
            # The fallback is only justified in the two cases the module
            # docstring actually describes -- the primary key was never
            # recorded, or the log it names has since moved/vanished --
            # not whenever the primary lookup simply missed. Falling back
            # unconditionally let a checkpoint whose *own* log was pruned
            # (while a distinct, surviving log happened to inherit the
            # same run_id via `CONDUCTOR_RUN_ID`) silently join to that
            # unrelated row (issue #460 review).
            event_log_path_absent_or_gone = (
                not cp.event_log_path or not Path(cp.event_log_path).exists()
            )
            if event_log_path_absent_or_gone:
                candidate = entries_by_run_id.get(cp.run_id)
                if candidate is not None:
                    matched_entry_path = candidate
                    matched_by = "run_id"

        if matched_entry_path is None:
            continue
        assert matched_by is not None  # noqa: S101 - matched_entry_path is only set alongside matched_by
        if matched_entry_path in result:
            # A newer checkpoint (checkpoints are sorted newest-first)
            # already claimed this entry -- newest wins. This is also what
            # keeps "one checkpoint, one entry" true: every `cp.file_path`
            # here is distinct (`list_checkpoints` globs `checkpoints_dir`,
            # so each is visited exactly once), so the only way an entry
            # could be claimed twice is by two *different* checkpoints,
            # which this check already resolves.
            continue

        # Validity filter: the workflow file the checkpoint would resume
        # must still exist (mirrors resume_workflow_async's own hard
        # failure), and the checkpoint file itself must still be on disk
        # (it may have been deleted -- e.g. by rotation -- between the
        # `list_checkpoints` scan and this check).
        if not Path(cp.workflow_path).exists():
            continue
        if not cp.file_path.exists():
            continue

        result[matched_entry_path] = ResumableCheckpoint(
            checkpoint_path=cp.file_path,
            workflow_path=Path(cp.workflow_path),
            created_at=cp.created_at,
            run_id=cp.run_id,
            current_agent=cp.current_agent,
            trigger=cp.trigger,
            matched_by=matched_by,
        )

    return result
