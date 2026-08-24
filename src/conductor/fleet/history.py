"""Enumerate completed runs from retained event logs (Fleet Manager E14).

The design's *Screens* section describes History as "completed runs,
subject to retention" -- this module is that enumeration, built directly
over ``$TMPDIR/conductor/*.events.jsonl`` (the same directory and glob
:mod:`conductor.fleet.retention` prunes) rather than the run-record
subsystem, since a completed run's record has already been removed by the
time this screen would show it (:func:`conductor.fleet.records.
remove_run_record_for_current_process` runs unconditionally in
``cli/run.py``'s ``finally``).

**A non-terminal log is not evidence of a live run.** The design's own
liveness measurement (see :mod:`conductor.fleet.summary`'s module
docstring: 228 false positives, 0 true positives inferring "running" from
the event stream alone) applies here too, with an even sharper
consequence: unlike ``summary.py`` (which is only ever handed a record
:mod:`conductor.fleet.records` has already confirmed live), this module
has **no** run-record to fall back on -- every file it enumerates might be
an active run's log, a crashed run's log, or a genuinely completed run's
log, and it cannot tell those apart from timing alone. So a log with no
``workflow_completed``/``workflow_failed`` terminal event classifies as
``"unknown"`` ("ended, outcome unknown") -- never as ``"running"``, which
would misrepresent a guess as a known fact.

Bounded by the same ``[fleet.retention].keep_last`` setting
:func:`conductor.fleet.retention.prune_event_logs` uses (E5), applied here
independently of the sweep's own ``enabled`` toggle: retention's
``enabled`` flag only controls whether files are actively *deleted*, but
this screen's list must stay bounded regardless of whether that sweep is
turned on, per *What single-user removes*: no long-horizon audit history,
no pagination.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from conductor.fleet import summary
from conductor.fleet.retention import event_log_root
from conductor.run_id import RUN_ID_PATTERN_SOURCE

logger = logging.getLogger(__name__)

HistoryOutcome = Literal["completed", "failed", "unknown"]

# Matches `conductor.fleet.retention`'s own glob -- duplicated (not
# imported) since that constant is private to that module; the two must
# be kept in sync if the filename convention ever changes.
_EVENT_LOG_GLOB = "conductor-*.events.jsonl"

# Recovers (workflow_name, run_id) from `EventLogSubscriber`'s filename
# convention (`engine/event_log.py`): `conductor-<name>-<ts>-<run_id>.events.jsonl`.
# `<name>` itself may contain hyphens (a common workflow-file-stem
# convention, e.g. "simple-qa-bot"), so it cannot be split on hyphens
# positionally -- instead the fixed-format timestamp
# (`time.strftime("%Y%m%d-%H%M%S")`, always 8 digits-dash-6 digits) anchors
# the parse from the *right* end of the filename, leaving whatever remains
# before it (including any hyphens) as the name. The run-id segment is
# built from the shared `conductor.run_id.RUN_ID_PATTERN_SOURCE` (not a
# hand-rolled hex-only charset) so a run id containing `-`/`_` still
# round-trips -- without this, such a run silently loses its `run_id` on
# the History screen (`_parse_filename` would fall back to `run_id=None`).
_FILENAME_PATTERN = re.compile(
    rf"^conductor-(?P<name>.+)-(?P<ts>\d{{8}}-\d{{6}})-(?P<run_id>{RUN_ID_PATTERN_SOURCE})"
    r"\.events\.jsonl$"
)

# Mirrors `conductor.settings.FleetRetentionSettings.keep_last`'s own
# default -- used only when settings can't be loaded at all (E14-T4).
_DEFAULT_KEEP_LAST = 200

# A hard, independent display cap: `keep_last < 1` means "unbounded" to
# the *pruning* sweep (retention disabled, not zero), but the History
# screen itself must never grow without limit regardless of that setting
# -- the design's own "no long-horizon audit history, no pagination"
# constraint applies to the display, not just to disk retention (E14
# review round 1). Matches `_DEFAULT_KEEP_LAST` so a default-configured
# machine sees no behavioral change from this cap.
_MAX_HISTORY_ENTRIES = 200


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One retained run's history-screen row (E14-T1)."""

    path: Path
    """The event log this entry was derived from."""

    workflow_name: str
    """Recovered from the filename (see :data:`_FILENAME_PATTERN`) -- the
    same ``workflow.name`` passed to ``EventLogSubscriber`` at run start
    (``cli/run.py``), so this matches what the Runs screen showed for
    this run while it was live."""

    run_id: str | None
    """Recovered from the filename, or ``None`` if the filename doesn't
    match the expected shape (e.g. a hand-crafted or otherwise
    unrecognized file that happened to match the glob)."""

    outcome: HistoryOutcome
    """``"completed"``/``"failed"`` from the log's terminal event, or
    ``"unknown"`` when no terminal event is present -- **never**
    ``"running"``, regardless of whether the underlying process happens
    to still be alive (see the module docstring)."""

    started_at: float | None
    """Unix timestamp of the **latest** ``workflow_started`` event, or
    ``None`` if the log has no such event at all (an unrecognized/garbled
    file, or one that hasn't recorded it yet) -- the full log is always
    read (:func:`_read_full_log`), so this is never unknown merely because
    the event was far from the end of the file. On a resumed run this is
    the current attempt's start time, not the original one (issue #485,
    Q2): since this field isn't itself a displayed column, the effect is
    on :attr:`duration_seconds`'s fallback, which stops spanning the idle
    gap between a resume's generations."""

    ended_at: float | None
    """Unix timestamp of the terminal event, or ``None`` if there is none."""

    duration_seconds: float | None
    """The engine-reported ``elapsed`` field from ``workflow_completed``
    when present (``workflow_failed`` carries no such field), else
    ``ended_at - started_at`` when both are known, else ``None`` -- never
    guessed from anything else."""

    total_tokens: int
    """Sum of ``tokens`` across every completed agent seen in the log --
    mirrors ``RunSummary.total_tokens``'s own accounting exactly (D5:
    completed-agent tokens only)."""

    total_cost_usd: float | None
    """Sum of ``cost_usd`` across priced completed agents, or ``None`` if
    none were priced -- mirrors ``RunSummary.total_cost_usd``."""

    unpriced_agent_count: int
    """Count of completed agents that consumed tokens but had no cost
    data -- mirrors ``RunSummary.unpriced_agent_count`` / issue #265's
    convention rather than silently summing a null into a confident total."""

    @property
    def has_unpriced(self) -> bool:
        """``True`` when at least one completed agent had no cost data."""
        return self.unpriced_agent_count > 0


def _parse_filename(path: Path) -> tuple[str, str | None]:
    """Return ``(workflow_name, run_id)`` recovered from an events-log filename.

    Falls back to the file's stem (with ``run_id=None``) when the
    filename doesn't match :data:`_FILENAME_PATTERN` -- an unrecognized
    file still produces a usable, displayable entry rather than being
    dropped outright.
    """
    match = _FILENAME_PATTERN.match(path.name)
    if match is None:
        return path.stem, None
    return match.group("name"), match.group("run_id")


def _finite_float(value: Any) -> float | None:
    """Return ``value`` as a ``float`` iff it is a finite ``int``/``float``.

    ``NaN``/``Infinity``/``-Infinity`` are valid JSON values (Python's
    ``json`` module accepts them by default) but are not legitimate
    timestamps, durations, token counts, or costs -- letting one through
    would silently poison a sum or crash the History screen's duration
    formatting (``int(nan)``/``int(inf)`` both raise) downstream (E14
    review round 1). Rejected the same way a wrong-shaped value already
    is: silently ignored, not raised.
    """
    if not isinstance(value, int | float):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


@dataclass
class _ScanResult:
    """Internal accumulator for a single pass over one log's events."""

    outcome: HistoryOutcome = "unknown"
    started_at: float | None = None
    ended_at: float | None = None
    reported_elapsed: float | None = None
    total_tokens: int = 0
    total_cost_usd: float | None = None
    unpriced_agent_count: int = 0


def _scan_history_events(events: Iterable[dict[str, Any]]) -> _ScanResult:
    """Single pass over a log's full event stream, mirroring
    ``conductor.fleet.summary._scan_events``'s terminal-event and
    token/cost accounting -- narrowed to just what History needs (no
    current-step/gate tracking, since a completed/failed/unknown log has
    no "current" step to highlight).

    Events are assumed oldest-first (the natural order of a JSONL log and
    of :func:`_read_full_log`'s yield order). This function makes exactly
    one forward pass over ``events`` (a single ``for evt in events:``, no
    indexing, no ``len()``, no re-iteration), so it accepts a one-shot
    iterator -- this is precisely the property that lets
    :func:`_read_full_log` stream rather than materialise a list. A
    future edit must not break this by, say, iterating ``events`` twice.

    A **resumed** run appends a fresh ``workflow_started`` after an
    earlier terminal event without a dashboard attached (the engine only
    suppresses that re-emit when seeding a dashboard on resume --
    ``cli/run.py``), so a log can legitimately contain
    ``workflow_started`` -> ... -> ``workflow_failed`` -> a *second*
    ``workflow_started`` -> more activity. Any ``workflow_started`` past
    the first is therefore treated as the start of a new root execution
    generation: the previously recorded terminal ``outcome``/``ended_at``/
    ``reported_elapsed`` are reset back to their "no terminal event yet"
    defaults, so an in-progress resumed attempt reads as ``"unknown"``
    (never the stale prior attempt's outcome) until *its own* terminal
    event is seen (E14 review round 1). ``started_at`` is likewise taken
    from the **latest** ``workflow_started`` (issue #485, Q2): a resumed
    run's :attr:`HistoryEntry.duration_seconds` fallback (``ended_at -
    started_at``, used when no engine-reported ``elapsed`` is available --
    e.g. a resumed-then-failed run) should measure the current attempt,
    not span the idle gap since the very first one.
    """
    result = _ScanResult()
    seen_workflow_started = False

    for evt in events:
        etype = evt.get("type")
        data = evt.get("data")
        if not isinstance(data, dict):
            data = {}
        # A nested sub-workflow's own events share the root event
        # vocabulary but must never be mistaken for the root run's own
        # terminal event or token/cost totals -- mirrors
        # `fleet.summary._scan_events`'s identical guard.
        if data.get("subworkflow_path"):
            continue
        ts = _finite_float(evt.get("timestamp"))

        if etype == "workflow_started":
            if seen_workflow_started:
                # A later root-level workflow_started (a resume) -- the
                # prior terminal state no longer describes this log's
                # current, still-open execution attempt.
                result.outcome = "unknown"
                result.ended_at = None
                result.reported_elapsed = None
            else:
                seen_workflow_started = True
            if ts is not None:
                result.started_at = ts

        elif etype == "workflow_completed":
            result.outcome = "completed"
            if ts is not None:
                result.ended_at = ts
            elapsed = _finite_float(data.get("elapsed"))
            if elapsed is not None:
                result.reported_elapsed = elapsed

        elif etype == "workflow_failed":
            result.outcome = "failed"
            if ts is not None:
                result.ended_at = ts

        elif etype in ("agent_completed", "parallel_agent_completed"):
            tokens = _finite_float(data.get("tokens"))
            if tokens is not None:
                result.total_tokens += int(tokens)
            cost = _finite_float(data.get("cost_usd"))
            if cost is not None:
                result.total_cost_usd = (result.total_cost_usd or 0.0) + cost
            elif tokens is not None and tokens > 0:
                result.unpriced_agent_count += 1

    return result


class _CorruptEventLogError(Exception):
    """Raised from :func:`_read_full_log`'s generator body when the
    stream is exhausted, for a non-empty log with no parseable events at
    all -- distinguishes "genuinely corrupt content" from "genuinely
    empty file" (E14 review round 2), so :func:`build_history_entries`'s
    per-file guard can skip the former (E14-T4) while still returning a
    normal ``"unknown"`` entry for the latter (a legitimately empty log
    is not corruption). For a corrupt log the generator never suspends at
    a ``yield`` (no event ever parses), so this is raised on the very
    first ``next()`` -- there is no drain-dependent timing to it, in
    practice or otherwise."""


def _read_full_log(path: Path) -> Iterator[dict[str, Any]]:
    """Stream-parse every line of a retained event log, oldest first,
    yielding one parsed event dict at a time.

    Delegates the actual line-reading and JSON-parsing to
    :func:`conductor.fleet.summary.stream_event_log` (issue #485), which
    made this exact choice -- an uncapped, streamed read bounded by the
    longest single line rather than by file size or event count -- for
    History first and generalized it for the Runs/run-detail screens'
    former bounded tail/head/full-log readers to share. History still
    reads a log once, after the run is already done, so there is no
    reason to accept a bounded reader's truncation trade-off: a byte-capped
    read can silently omit an early token/cost event or discard an
    oversized terminal event outside its window, presenting a genuinely
    completed run as ``"unknown"`` with an incomplete total (E14 review
    round 1).

    The generator holds the file handle open (inside ``stream_event_log``'s
    own ``with``) until it is exhausted, closed, or garbage collected --
    that ``with`` block, not any particular caller, owns the handle's
    lifetime. The sole consumer in production, :func:`_build_entry`, drains
    it to completion via :func:`_scan_history_events`; tests may consume it
    directly and abandon it early, which is exactly why the handle's
    release does not depend on caller behavior.

    Because this is a generator, none of its side effects happen when
    ``_read_full_log(path)`` is called -- they happen while the returned
    iterator is being **consumed**. In particular, ``open()`` (and any
    ``OSError`` it raises: permission denied, the file vanishing mid-scan,
    or any other read failure) surfaces on the first ``next()``, not at
    call time -- the delegated-to ``stream_event_log`` is always the
    *first* thing this generator's body does, before any size check, so a
    failure opening the file is never masked by an empty-file fast path.
    Unlike ``fleet.summary``'s former bounded readers' never-raise
    contract, such a failure is deliberately **not** swallowed: it
    propagates so :func:`build_history_entries`'s per-file guard can tell
    "genuinely no events" apart from "couldn't read this file at all" and
    skip the latter, rather than presenting a fabricated ``"unknown"``
    entry for a log it never actually read (E14-T4 / E14 review round 1).

    A malformed individual line (bad JSON, a truncated write caught
    mid-flush) is tolerated by the shared reader the same way it always
    has been here -- skipped rather than aborting the whole read. But a
    **non-empty** file that yields **zero** parseable events is corrupt,
    not "legitimately empty" -- E14-T4 requires a corrupt log to be
    skipped, not shown as an ordinary ``"unknown"`` entry (E14 review
    round 2). Raises :class:`_CorruptEventLogError` in that case, once the
    stream is exhausted; a genuinely empty (zero-byte) file still yields
    nothing and raises nothing, which :func:`_build_entry` legitimately
    turns into an ``"unknown"`` entry. Distinguishing the two now checks
    ``path.stat().st_size`` **after** the read completes with nothing
    yielded (rather than "at least one non-blank raw line", this
    function's own former signal) -- deliberately after, not before,
    because checking size first would let a permission error on a
    zero-byte file skip the read (and the ``OSError`` it should raise)
    entirely. The shared reader has no reason to track skipped-line
    counts for a live run's cheap, repeated poll, so this function no
    longer can either. A file containing only blank/whitespace lines --
    untested, and not known to occur in practice, since every write here
    is a single JSON object per line -- would now read as "corrupt" rather
    than "empty"; a zero-byte file (the only shape a genuinely fresh or
    truncated-at-creation log actually takes) is unaffected.
    """
    yielded_any = False
    for evt in summary.stream_event_log(path):
        yielded_any = True
        yield evt
    if not yielded_any and path.stat().st_size > 0:
        raise _CorruptEventLogError(f"{path}: non-empty log with no parseable events")


def _build_entry(path: Path) -> HistoryEntry:
    """Build one :class:`HistoryEntry` for ``path``.

    Uses :func:`_read_full_log`'s unbounded, streamed read rather than a
    byte-capped tail/head read -- see that function's docstring for why a
    bounded read is unsuitable for a retrospective, read-once history
    entry (E14 review round 1).

    Raises on a read failure -- an ``OSError`` opening the file, or a
    :class:`_CorruptEventLogError` for a non-empty log with no parseable
    events -- surfacing while :func:`_read_full_log`'s generator is
    consumed here (via :func:`_scan_history_events`'s draining loop),
    not when it is constructed. Either way, :func:`build_history_entries`'s
    per-file guard can skip a genuinely unreadable/corrupt log rather than
    presenting it as a fabricated ``"unknown"`` entry with zero totals
    (E14-T4 / E14 review round 1 and 2). A genuinely **empty** log (no
    non-blank lines at all) still produces a normal ``"unknown"`` entry
    -- that is legitimate data, not corruption.
    """
    workflow_name, run_id = _parse_filename(path)
    scan = _scan_history_events(_read_full_log(path))

    duration = scan.reported_elapsed
    if duration is None and scan.started_at is not None and scan.ended_at is not None:
        duration = max(0.0, scan.ended_at - scan.started_at)

    return HistoryEntry(
        path=path,
        workflow_name=workflow_name,
        run_id=run_id,
        outcome=scan.outcome,
        started_at=scan.started_at,
        ended_at=scan.ended_at,
        duration_seconds=duration,
        total_tokens=scan.total_tokens,
        total_cost_usd=scan.total_cost_usd,
        unpriced_agent_count=scan.unpriced_agent_count,
    )


def _safe_mtime(path: Path) -> float:
    """Return ``path``'s mtime, or ``-inf`` if it can't be stat'd.

    Mirrors ``conductor.fleet.retention._safe_mtime`` exactly: a file that
    vanishes mid-scan sorts as "oldest" rather than aborting the sort.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return float("-inf")


def _resolve_keep_last() -> int:
    """Return the configured retention ``keep_last`` bound.

    Falls back to :data:`_DEFAULT_KEEP_LAST` (mirroring
    ``FleetRetentionSettings.keep_last``'s own default) when settings
    can't be loaded at all (malformed ``~/.conductor/config.toml``) --
    mirrors ``conductor.fleet.retention.maybe_prune_event_logs``'s own
    never-break-on-bad-settings contract: a machine-wide settings file
    must never break this screen either.
    """
    try:
        from conductor.settings import load_settings

        return load_settings().fleet.retention.keep_last
    except Exception:
        logger.warning(
            "Failed to load Conductor settings for history's retention bound; using default",
            exc_info=True,
        )
        return _DEFAULT_KEEP_LAST


def build_history_entries(*, keep_last: int | None = None) -> list[HistoryEntry]:
    """Enumerate completed runs from retained event logs (E14-T1).

    Args:
        keep_last: Explicit override for the retention bound (primarily
            for testing); defaults to :func:`_resolve_keep_last`'s
            settings-driven value. A value less than 1 means "unbounded"
            -- mirrors ``prune_event_logs``'s own ``keep_last < 1`` guard
            (retention *disabled* is not the same as *zero*).

    Returns:
        :class:`HistoryEntry` rows, sorted newest-first by mtime, bounded
        to the newest ``keep_last`` logs -- and, independently, never more
        than :data:`_MAX_HISTORY_ENTRIES` even when ``keep_last`` is
        configured below 1 ("unbounded" for the *pruning* sweep, not for
        this display -- E14 review round 1). Never raises: a failure
        resolving the event-log root, listing its contents, or building
        any individual entry is logged and that file (or the whole list,
        for a root-level failure) is simply omitted rather than crashing
        the History screen (E14-T4).
    """
    if keep_last is None:
        keep_last = _resolve_keep_last()

    try:
        root = event_log_root()
        candidates = sorted(
            (p for p in root.glob(_EVENT_LOG_GLOB) if p.is_file()),
            key=_safe_mtime,
            reverse=True,
        )
    except Exception:
        logger.warning("Failed to enumerate event logs for history", exc_info=True)
        return []

    display_cap = min(keep_last, _MAX_HISTORY_ENTRIES) if keep_last >= 1 else _MAX_HISTORY_ENTRIES
    candidates = candidates[:display_cap]

    entries: list[HistoryEntry] = []
    for path in candidates:
        try:
            entries.append(_build_entry(path))
        except (OSError, _CorruptEventLogError):
            logger.warning("Skipping unreadable/corrupt event log %s", path, exc_info=True)
            continue
        except Exception:
            logger.error(
                "Unexpected failure building history entry for %s; skipping", path, exc_info=True
            )
            continue
    return entries
