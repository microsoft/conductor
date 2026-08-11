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

import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from conductor.fleet.retention import event_log_root

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
# (`time.strftime("%Y%m%d-%H%M%S")`, always 8 digits-dash-6 digits) and
# hex run-id segments anchor the parse from the *right* end of the
# filename, leaving whatever remains (including any hyphens) as the name.
_FILENAME_PATTERN = re.compile(
    r"^conductor-(?P<name>.+)-(?P<ts>\d{8}-\d{6})-(?P<run_id>[0-9a-fA-F]{1,32})\.events\.jsonl$"
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
    """Unix timestamp of the ``workflow_started`` event, or ``None`` if
    the log has no such event at all (an unrecognized/garbled file, or
    one that hasn't recorded it yet) -- the full log is always read
    (:func:`_read_full_log`), so this is never unknown merely because
    the event was far from the end of the file."""

    ended_at: float | None
    """Unix timestamp of the terminal event, or ``None`` if there is none."""

    duration_seconds: float | None
    """The engine-reported ``elapsed`` field from ``workflow_completed``
    when present (``workflow_failed`` carries no such field), else
    ``ended_at - started_at`` when both are known, else ``None`` -- never
    guessed from anything else."""

    total_tokens: int
    """Sum of ``tokens`` across every completed agent seen in the tail --
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


def _scan_history_events(events: list[dict[str, Any]]) -> _ScanResult:
    """Single pass over a log's full event stream, mirroring
    ``conductor.fleet.summary._scan_events``'s terminal-event and
    token/cost accounting -- narrowed to just what History needs (no
    current-step/gate tracking, since a completed/failed/unknown log has
    no "current" step to highlight).

    Events are assumed oldest-first (the natural order of a JSONL log and
    of :func:`_read_full_log`'s output).

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
    event is seen (E14 review round 1).
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
    """Raised by :func:`_read_full_log` for a non-empty log with no
    parseable events at all -- distinguishes "genuinely corrupt content"
    from "genuinely empty file" (E14 review round 2), so
    :func:`build_history_entries`'s per-file guard can skip the former
    (E14-T4) while still returning a normal ``"unknown"`` entry for the
    latter (a legitimately empty log is not corruption)."""


def _read_full_log(path: Path) -> list[dict[str, Any]]:
    """Stream-parse every line of a retained event log, oldest first.

    Unlike ``fleet.summary``'s bounded tail/head readers -- built for a
    *live* run's cheap, repeated ~2s poll, or a bounded detail view --
    History only ever reads a log once, after the run is already done, so
    there is no reason to accept a bounded reader's truncation trade-off
    here. A byte-capped read can silently omit an early token/cost event
    or discard an oversized terminal event that falls outside the window,
    presenting a genuinely completed run as ``"unknown"`` with an
    incomplete total (E14 review round 1). Reading the file unboundedly,
    streamed line-by-line via file iteration (never loaded into memory as
    a single blob) avoids that without paying for a whole-file read at
    once.

    Unlike ``read_event_log_tail``'s never-raise contract, a read failure
    here (the file cannot be opened at all -- permission denied, vanished
    mid-scan, or any other ``OSError``) is deliberately **not**
    swallowed: it propagates so :func:`build_history_entries`'s
    per-file guard can tell "genuinely no events" apart from "couldn't
    read this file at all" and skip the latter, rather than presenting a
    fabricated ``"unknown"`` entry for a log it never actually read
    (E14-T4 / E14 review round 1).

    A malformed individual line (bad JSON, a truncated write caught
    mid-flush) is tolerated the same way the bounded readers already do
    -- skipped rather than aborting the whole read. But a **non-empty**
    file (at least one non-blank line) that yields **zero** parseable
    events is corrupt, not "legitimately empty" -- E14-T4 requires a
    corrupt log to be skipped, not shown as an ordinary ``"unknown"``
    entry (E14 review round 2). Raises :class:`_CorruptEventLogError` in
    that case; a genuinely empty file (no non-blank lines at all) still
    returns an empty list, which :func:`_build_entry` legitimately turns
    into an ``"unknown"`` entry.
    """
    events: list[dict[str, Any]] = []
    saw_nonblank_line = False
    with open(path, "rb") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            saw_nonblank_line = True
            try:
                obj = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                events.append(obj)
    if saw_nonblank_line and not events:
        raise _CorruptEventLogError(f"{path}: non-empty log with no parseable events")
    return events


def _build_entry(path: Path) -> HistoryEntry:
    """Build one :class:`HistoryEntry` for ``path``.

    Uses :func:`_read_full_log`'s unbounded, streamed read rather than a
    byte-capped tail/head read -- see that function's docstring for why a
    bounded read is unsuitable for a retrospective, read-once history
    entry (E14 review round 1).

    Raises on a read failure (propagated from :func:`_read_full_log`,
    including a :class:`_CorruptEventLogError` for a non-empty log with
    no parseable events) so :func:`build_history_entries`'s per-file
    guard can skip a genuinely unreadable/corrupt log rather than
    presenting it as a fabricated ``"unknown"`` entry with zero totals
    (E14-T4 / E14 review round 1 and 2). A genuinely **empty** log (no
    non-blank lines at all) still produces a normal ``"unknown"`` entry
    -- that is legitimate data, not corruption.
    """
    workflow_name, run_id = _parse_filename(path)
    events = _read_full_log(path)
    scan = _scan_history_events(events)

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
        except Exception:
            logger.warning("Failed to build history entry for %s; skipping", path, exc_info=True)
            continue
    return entries
