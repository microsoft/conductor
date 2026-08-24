"""``RunSummary`` derivation from a run record plus its event log.

Fleet Manager E6 (see ``docs/projects/fleet-manager/fleet-manager.design.md``,
*Implementation* → ``summary.py``): given a live :class:`~conductor.fleet.records.RunRecord`
(as returned by :func:`conductor.fleet.records.read_run_records`), derive a
:class:`RunSummary` — the status vocabulary, current step, elapsed-on-step,
token/cost totals, and any open human gate — from the run's JSONL event log,
so this can be called once per row on a ~2-second poll loop (E7's Runs
screen) without silently dropping state a long or resumed run has already
outgrown a bounded read window (issue #485).

This module used to bound its reads to a 512 KiB tail (list screen), a
512 KiB head (topology recovery for a run whose ``workflow_started`` had
aged out of that tail), and an 8 MiB cap (run-detail/step-detail screens).
Every one of those windows was sized against logs that reality then
outgrew: a run long enough to be interesting silently lost its current
step, its token/cost totals, and (on a resumed run) reported a stale
*prior* generation's terminal status. :func:`stream_event_log` replaces
all three with one **streaming, uncapped** reader — the same choice
:mod:`conductor.fleet.history`'s ``_read_full_log`` already made for the
same reason (an accepted-then-exceeded cap silently truncating live data
is worse than an unbounded read that costs proportionally more CPU).
Memory is bounded by the longest single line, not by the file's size or
event count; a 12.5 ms scan of a real 9.72 MB / 20,361-line log (with the
prefilter below) is comfortably inside the Runs screen's worker-thread
poll budget.

**Liveness is not re-derived here.** The design's own measurement found
inferring "is this run still running" from the event stream alone unreliable
(228 false positives, 0 true positives) — so this module trusts that the
``RunRecord`` it is given already passed
:func:`conductor.cli.pid.is_process_alive` (as every record from
``read_run_records()`` does) and derives only the *finer-grained* status
(``at-gate`` / ``paused`` / terminal) from explicit event markers, defaulting
to ``"running"`` when the log shows nothing more specific. A narrow race
exists where a workflow's ``workflow_completed``/``workflow_failed`` event has
been written but the process has not yet exited and removed its own record —
in that window this module correctly reports ``"completed"``/``"failed"``
even though the record is technically still live.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from conductor.fleet.records import RunRecord

logger = logging.getLogger(__name__)

RunStatus = Literal["running", "at-gate", "paused", "completed", "failed"]

AgentDetailStatus = Literal["pending", "running", "at-gate", "completed", "failed"]

_STEP_ACTIVITY_LIMIT = 200
"""How many activity lines the step drill-down keeps. A long agentic loop
emits hundreds of tool calls; this is a drill-down, not a log viewer."""


def _finite_float(value: Any) -> float | None:
    """Return ``value`` as a ``float`` iff it is a finite ``int``/``float``.

    ``NaN``/``Infinity``/``-Infinity`` are valid JSON values (Python's
    ``json`` module accepts them by default) but are not legitimate token
    counts or costs -- letting one through would silently poison a sum or
    crash downstream formatting (``int(nan)``/``int(inf)`` both raise).
    Rejected the same way a wrong-shaped value already is: silently
    ignored, not raised. Shared with :mod:`conductor.fleet.history`, which
    already imports this module and enforces the identical rule over the
    same engine-written event payloads.
    """
    if not isinstance(value, int | float):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class GateInfo:
    """The open human gate carried onto a :class:`RunSummary`.

    Populated from the most recent unresolved ``gate_presented`` event
    (E6-T6) — cleared once a matching ``gate_resolved`` is seen.
    """

    agent_name: str
    prompt: str
    options: list[str]
    option_details: list[dict[str, Any]]


@dataclass(frozen=True)
class TopologyAgent:
    """A single agent's static definition, for the E9 run-detail screen."""

    name: str
    type: str
    model: str | None
    provider_name: str | None


@dataclass(frozen=True)
class RunTopology:
    """Run topology extracted from the ``workflow_started`` event (E6-T5)."""

    entry_point: str | None
    agents: list[TopologyAgent]


@dataclass(frozen=True)
class AgentDetail:
    """One agent's row on the run-detail screen (E9-T2): status, elapsed,
    tokens, cost -- deliberately *not* a DAG node, an agent message, or tool
    output (all explicitly out of scope per the design's *Non-goals*)."""

    name: str
    type: str
    model: str | None
    provider_name: str | None

    status: AgentDetailStatus
    """``"pending"`` -- never seen an ``agent_started`` for this agent in
    the full log. ``"running"`` -- currently the open step. ``"at-gate"`` --
    the open step, with a presented gate nobody has answered yet.
    ``"completed"``
    -- closed via ``agent_completed`` (or another step type's own
    ``*_completed``/``gate_resolved``). ``"failed"`` -- closed via
    ``agent_failed`` or ``parallel_agent_failed``."""

    started_at: float | None
    """Unix timestamp of this agent's most recent (re-)start, or ``None``
    if it never started. Used to compute a live elapsed time while
    :attr:`status` is ``"running"``."""

    reported_elapsed_seconds: float | None
    """The ``elapsed`` value the engine itself reported on the closing
    event, or ``None`` if the agent hasn't closed (yet)."""

    tokens: int | None
    """Tokens from this agent's own ``agent_completed`` event, or ``None``
    if it never completed (pending/running/failed carry no token count --
    per D5, there is no mid-flight or failure-path usage event)."""

    cost_usd: float | None
    """Cost from this agent's own ``agent_completed`` event, or ``None`` if
    it never completed or the model was unpriced."""

    def elapsed_seconds(self, now: float | None = None) -> float | None:
        """Elapsed time for this row: live (``now - started_at``) while
        ``"running"``, the engine-reported value once ``"completed"`` or
        ``"failed"``, or ``None`` while ``"pending"``."""
        if self.status == "running":
            return _elapsed_since(self.started_at, now)
        if self.status in ("completed", "failed"):
            return self.reported_elapsed_seconds
        return None


@dataclass(frozen=True)
class RunDetail:
    """Full per-agent detail for the run-detail screen (E9), derived from a
    streamed, uncapped read of the run's event log (:func:`stream_event_log`)
    -- the same reader :class:`RunSummary` uses, over every event rather
    than a tail window. The distinction from :class:`RunSummary` is not
    frequency but filtering: :func:`derive_run_summary` passes
    ``keep_types=_SUMMARY_EVENT_TYPES`` (12.5 ms measured against a real
    9.72 MB / 20,361-line log) while this class's unfiltered scan costs
    roughly 5x that (65 ms on the same log) because it also needs
    per-agent event types the Runs screen's aggregate totals do not. The
    scan runs on every ~2s poll tick for as long as the run-detail screen
    is open, not once per open -- kept off the event loop in a worker for
    that reason."""

    run_id: str
    workflow_name: str
    topology: RunTopology | None

    """``None`` when the log is missing/unreadable, empty, or has not
    (yet) written a ``workflow_started`` event -- the detail screen
    renders a placeholder in this case rather than an empty table (E9-T5)."""

    agents: list[AgentDetail]
    """One row per :attr:`topology`'s agent, in the same (declared) order.
    Empty whenever :attr:`topology` is ``None``."""

    current_step: str | None
    """Name of the currently open step (agent, parallel group, or for_each
    group) -- may name something other than an :attr:`agents` row (e.g. a
    parallel/for_each group name), in which case no agent row is
    highlighted as running."""


@dataclass(frozen=True)
class RunSummary:
    """A point-in-time summary of one live run, for the fleet Runs screen (E7)
    and run-detail screen (E9)."""

    run_id: str
    workflow_name: str
    mode: str
    port: int | None
    started_at: str

    status: RunStatus

    current_step: str | None
    """Name of the agent, parallel group, or for_each group most recently
    started without a matching completion event seen in the log. ``None``
    when nothing is open (e.g. a log with no events yet, or a terminal
    status)."""

    current_step_type: Literal["agent", "parallel", "for_each"] | None
    current_step_started_at: float | None
    """Unix timestamp of the current step's start event, or ``None``."""

    total_tokens: int
    """Sum of ``tokens`` across every ``agent_completed`` event in the log.
    Per D5, this is **completed-agent tokens only** — there is no
    mid-flight usage event, so the agent currently running never
    contributes here until it finishes. A **lifetime total across every
    generation** in the log (issue #485): a resumed run's totals include
    whatever the prior, terminated generation(s) already accumulated, not
    just the current one -- only the *status*/*current step*/*gate* reset
    at a resume boundary, not the usage totals (:attr:`total_tokens`,
    :attr:`total_cost_usd`, and :attr:`unpriced_agent_count` alike -- all
    three accumulate through the same event-handling code path)."""

    total_cost_usd: float | None
    """Sum of ``cost_usd`` across priced ``agent_completed`` events in the
    log (every generation -- see :attr:`total_tokens`), or ``None`` if none
    were priced. Mirrors ``WorkflowUsage.total_cost_usd``: never a
    confident total when :attr:`has_unpriced` is true — see that
    attribute."""

    unpriced_agent_count: int
    """Count of completed agents that consumed tokens but had no cost data
    (``cost_usd`` was null despite ``tokens > 0``). Reuses the
    ``unpriced_agents``/``has_unpriced`` convention from
    ``engine.usage.WorkflowUsage`` (issue #265) rather than silently summing
    a null into a confident-looking total: a caller rendering
    :attr:`total_cost_usd` must also surface this count, e.g.
    ``~$X (N unpriced)``."""

    gate: GateInfo | None
    gate_resolvable: bool

    """True when this run's gate (if any) can be resolved remotely via
    ``conductor gate respond`` (D4): true whenever the record has a
    dashboard port (``fg-web`` or ``bg``), false for a plain foreground
    run (``mode == "fg"``), which has no HTTP channel and whose gate can
    only be resolved at the terminal that owns it. Computed once, here,
    from the record's ``port`` — not re-derived per-screen."""

    topology: RunTopology | None
    """Run topology from the log's **latest** root ``workflow_started``
    event (E6-T5). ``workflow_started`` is always the first line of a
    fresh log, and a resume always writes a second one into the same file
    (the engine's own re-emit, or the dashboard-seeding path's synthesized
    copy) -- so on a resumed run this is the *current* generation's
    topology, not a stale earlier one (issue #485). ``None`` only when the
    log has no root ``workflow_started`` at all (missing/unreadable log,
    or one that hasn't written it yet)."""

    cwd: str | None = None
    """Directory conductor was launched from (``workflow_started``'s
    ``system`` block). Not on the run record, and what distinguishes two
    runs of the same workflow started from different checkouts. Like
    :attr:`topology`, taken from the latest generation."""

    inputs: dict[str, Any] | None = None
    """The values this run was launched with, when the log records them.
    Like :attr:`topology`, taken from the latest generation."""

    @property
    def has_unpriced(self) -> bool:
        """``True`` when at least one completed agent had no cost data."""
        return self.unpriced_agent_count > 0

    def elapsed_on_step_seconds(self, now: float | None = None) -> float | None:
        """Seconds since :attr:`current_step_started_at`, or ``None`` if no step is open."""
        return _elapsed_since(self.current_step_started_at, now)

    def total_elapsed_seconds(self, now: float | None = None) -> float | None:
        """Seconds since :attr:`started_at`, or ``None`` if it can't be parsed."""
        return _elapsed_since(_parse_iso_timestamp(self.started_at), now)


def _elapsed_since(timestamp: float | None, now: float | None = None) -> float | None:
    """Return ``now - timestamp`` (never negative), or ``None`` if ``timestamp`` is unknown."""
    if timestamp is None:
        return None
    resolved_now = now if now is not None else time.time()
    return max(0.0, resolved_now - timestamp)


def _parse_iso_timestamp(value: str) -> float | None:
    """Best-effort parse of an ISO 8601 timestamp (as written by ``RunRecord.started_at``)
    into a Unix epoch float. Returns ``None`` for an empty, missing, or malformed value
    (e.g. the ``"?"`` placeholder some legacy code paths use) rather than raising."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Streaming JSONL reading (issue #485)
# ---------------------------------------------------------------------------

# Whitespace-tolerant, anchored match of a JSONL event line's leading
# `"type"` key -- lets `stream_event_log`'s prefilter skip an uninteresting
# line without JSON-parsing it. Anchored (`.match`, not `.search`) so a
# `"type"` key appearing later in the object (e.g. inside `data`) can never
# be mistaken for the event's own type. Whitespace-tolerant because
# production log lines are written with `json.dumps(..., separators=(",",
# ":"))` (`engine/event_log.py`) -- no spaces at all -- while test fixtures
# typically use plain `json.dumps`, which inserts a space after each colon;
# a regex anchored to one exact spacing would make the fast path silently
# dead in tests while live in production.
_TYPE_PREFIX_RE = re.compile(rb'\{\s*"type"\s*:\s*"([A-Za-z0-9_]+)"')


def stream_event_log(
    path: Path, *, keep_types: frozenset[str] | None = None
) -> Iterator[dict[str, Any]]:
    """Stream-parse a JSONL event log, oldest first, yielding one parsed
    event dict at a time.

    Replaces this module's former bounded tail/head/full-log readers
    (issue #485): none of the three windows they used was actually safe
    against a run outliving it, and every one of them did, in production,
    silently and repeatedly. Memory here is bounded by the longest single
    line in the file, not by the file's size or event count -- retained
    state beyond that one line/dict is nothing. This mirrors
    :mod:`conductor.fleet.history`'s ``_read_full_log``, which made the
    same choice first, for the same reason: a byte-capped read can drop an
    early token/cost event or discard an oversized terminal event outside
    its window, silently corrupting the very state callers rely on.

    With ``keep_types``, a whitespace-tolerant anchored regex
    (:data:`_TYPE_PREFIX_RE`) reads the ``type`` key off the raw bytes and
    skips a line whose type is not in the set *without* JSON-decoding it --
    this is what keeps a per-row scan on the Runs screen's ~2s poll cheap
    even against a very large log (12.5 ms measured against a real 9.72 MB
    / 20,361-line log, versus 65 ms unfiltered). A line whose ``type`` key
    is not first (so the regex fails to match) is always parsed rather
    than skipped -- the prefilter is only ever an optimization, never a
    second opinion about what a line means, so it can never silently drop
    an event the writer happens to serialize with a different key order.

    This function is a generator: it holds the file handle open (inside
    its own ``with``) only while being consumed, and none of its side
    effects -- including ``open()`` and any ``OSError`` it raises (a
    missing file, a permission error, or any other read failure) -- happen
    until the returned iterator's first ``next()``. Unlike this module's
    former bounded readers, this does **not** swallow ``OSError``; it
    propagates, so a caller that wants the old "never raise" behavior must
    wrap the *consumption* (e.g. inside :func:`_scan_events`'s ``for``
    loop), not just the call to this function -- see :func:`derive_run_summary`.

    A malformed individual line (bad JSON, a truncated write caught
    mid-flush, invalid UTF-8) is tolerated the same way a concurrent
    in-progress write always has been here: skipped rather than aborting
    the whole read.

    Args:
        path: Path to the JSONL event log.
        keep_types: A best-effort prefilter, not an exact one: a line
            whose leading ``type`` key matches :data:`_TYPE_PREFIX_RE` and
            is not in this set is skipped without being JSON-parsed. A
            line the regex cannot read (e.g. ``type`` is not the first
            key) is always parsed and yielded regardless of this set --
            callers must still branch on ``type`` rather than assume the
            filter was exact. ``None`` parses every line.

    Yields:
        Parsed event dicts, oldest first.

    Raises:
        OSError: On the returned iterator's first ``next()`` (not at call
            time) if ``path`` cannot be opened or read -- see above.
    """
    with open(path, "rb") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            if keep_types is not None:
                match = _TYPE_PREFIX_RE.match(line)
                if match is not None and match.group(1).decode("ascii") not in keep_types:
                    continue
            try:
                obj = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                continue
            if isinstance(obj, dict):
                yield obj


# ---------------------------------------------------------------------------
# Event-stream derivation (E6-T2, E6-T3, E6-T4, E6-T5)
# ---------------------------------------------------------------------------

# Step-tracking (E6-T3): every step type's "started" event opens an entry;
# a matching "closing" event for the same (family, name) removes it. The
# most recently-opened, still-open entry is the "current step".
#
# Every step type — not just a plain LLM agent — emits `agent_started`
# unconditionally before its type-specific handling
# (`engine/workflow.py:3410`), but only a plain agent later emits
# `agent_completed`; script/wait/set/human_gate/sub-workflow steps each
# close out via their own distinct `*_completed` (or `gate_resolved`)
# event instead, and a parallel-group member closes via
# `parallel_agent_completed` rather than `agent_completed` (it never gets
# a plain `agent_started`/`agent_completed` at all -- see
# `parallel_agent_started` below). All of these close an "agent"-family
# entry keyed by `agent_name` so a non-LLM step (or a parallel member)
# never appears stuck "open" forever once it finishes.
_AGENT_CLOSE_EVENT_TYPES = frozenset(
    {
        "agent_completed",
        "script_completed",
        "wait_completed",
        "set_completed",
        "gate_resolved",
        "subworkflow_completed",
        "parallel_agent_completed",
        # A `questions` node closes with its own event rather than
        # `agent_completed`. Without it here the node stayed "running" for
        # the rest of the run -- visibly so, since the workflow had already
        # moved on to the next step.
        "questions_completed",
    }
)

# Closing events that mark a step "failed" rather than "completed". Every
# non-LLM step type emits its own `*_failed` on an unhandled exception
# (`script_failed`/`wait_failed`/`set_failed`/`subworkflow_failed`), and a
# parallel-group member fails via `parallel_agent_failed`. `agent_failed`
# is emitted only along the terminate-step failure path. A regular
# (non-parallel) agent's own unhandled exception has no per-agent failure
# event of its own -- it propagates straight to `workflow_failed`, whose
# `agent_name` field is handled separately in the scan loops below.
_AGENT_FAILED_EVENT_TYPES = frozenset(
    {
        "agent_failed",
        "parallel_agent_failed",
        "script_failed",
        "wait_failed",
        "set_failed",
        "subworkflow_failed",
    }
)

# Every event type `_scan_events` actually branches on -- the `keep_types`
# prefilter passed to `stream_event_log` for the Runs screen's per-row scan
# (issue #485). Derived from the two frozensets above (not restated) so
# adding a new close/failure event type to either one keeps this prefilter
# correct automatically, without a second edit anyone could forget. This
# set and `_scan_events` move together: a type the scanner branches on but
# this set omits would be silently dropped from every derived summary --
# the exact failure class issue #485 is about -- so `TestStreamEventLog`'s
# prefilter-equivalence test compares a filtered scan against an unfiltered
# one over a log exercising every branch, to catch that drift here rather
# than in production.
_SUMMARY_EVENT_TYPES: frozenset[str] = (
    _AGENT_CLOSE_EVENT_TYPES
    | _AGENT_FAILED_EVENT_TYPES
    | frozenset(
        {
            "workflow_started",
            "workflow_completed",
            "workflow_failed",
            "agent_started",
            "parallel_agent_started",
            "parallel_started",
            "parallel_completed",
            "for_each_started",
            "for_each_completed",
            "gate_presented",
            "agent_paused",
        }
    )
)


@dataclass
class _ScanResult:
    """Internal accumulator for a single pass over event dicts."""

    status: RunStatus = "running"
    gate: GateInfo | None = None
    open_steps: list[tuple[str, str, float]] = field(default_factory=list)
    total_tokens: int = 0
    total_cost_usd: float | None = None
    unpriced_agent_count: int = 0
    topology: RunTopology | None = None
    workflow_name: str | None = None
    cwd: str | None = None
    inputs: dict[str, Any] | None = None


def _close_step(open_steps: list[tuple[str, str, float]], step_type: str, name: str) -> None:
    """Remove the most recent open step matching ``(step_type, name)``, if any."""
    for i in range(len(open_steps) - 1, -1, -1):
        if open_steps[i][0] == step_type and open_steps[i][1] == name:
            del open_steps[i]
            return


def _extract_topology(data: dict[str, Any]) -> RunTopology:
    """Build a :class:`RunTopology` from a ``workflow_started`` event's ``data`` payload."""
    agents = [
        TopologyAgent(
            name=str(a.get("name", "")),
            type=str(a.get("type") or "agent"),
            model=a.get("model"),
            provider_name=a.get("provider_name"),
        )
        for a in (data.get("agents") or [])
        if isinstance(a, dict)
    ]
    return RunTopology(entry_point=data.get("entry_point"), agents=agents)


def _scan_events(events: Iterable[dict[str, Any]]) -> _ScanResult:
    """Single pass over event dicts deriving status, gate, current step, and totals.

    Events are assumed oldest-first (the natural order of a JSONL log and
    of :func:`stream_event_log`'s output), so later events override earlier
    ones for state that only has one current value (``status``, ``gate``,
    ``topology``, ``workflow_name``, ``cwd``, ``inputs``).

    Accepts any one-shot iterable and makes exactly one forward pass over
    it -- no indexing, no ``len()``, no re-iteration (mirrors
    ``fleet.history._scan_history_events``'s identical issue-#436
    constraint) -- which is what lets :func:`derive_run_summary` stream
    straight from :func:`stream_event_log` instead of materializing a list.

    **Generation-aware (issue #485):** every *root* ``workflow_started``
    marks the start of a new execution generation of this run -- a resumed
    run always writes a second one into the same log, whether or not a
    dashboard was attached (see the module docstring). Reaching one resets
    the per-generation state a fresh run starts with -- ``status`` back to
    ``"running"``, any open gate cleared, every open step closed -- and
    *overwrites* (not merges) ``topology``/``workflow_name``/``cwd``/
    ``inputs`` with this generation's own, so a resumed run's current step
    and topology reflect what is actually running now, not a stale earlier
    generation. Token/cost totals are the one exception: they are **not**
    reset here, and keep accumulating across every generation in the log --
    a resumed run's lifetime usage, not just its latest attempt's.
    """
    result = _ScanResult()

    for evt in events:
        etype = evt.get("type")
        data = evt.get("data")
        if not isinstance(data, dict):
            data = {}
        # `_dashboard_context_path`-stamped events (`engine/workflow.py`)
        # originate from a nested sub-workflow engine, not the root run.
        # A nested agent can share a root agent's name, so scanning these
        # would corrupt the root agent's status/timing/usage -- skip
        # anything outside the root context. This is also what keeps a
        # nested sub-workflow's own `workflow_started` from being mistaken
        # for a resume boundary.
        if data.get("subworkflow_path"):
            continue
        ts = evt.get("timestamp")

        if etype == "workflow_started":
            # The workflow's *declared* name (`workflow.name`), which is not
            # the file stem the run record carries: a repo that stores each
            # workflow as `<name>/workflow.yaml` makes every run show up as
            # "workflow". History reads this same declared name out of the
            # log filename, which is why the two screens disagreed.
            declared = data.get("name")
            result.workflow_name = declared if isinstance(declared, str) and declared else None

            # Where conductor was launched from, and what it was launched
            # with -- neither is on the run record, and both are what tells
            # two runs of the same workflow apart.
            system = data.get("system")
            cwd = system.get("cwd") if isinstance(system, dict) else None
            result.cwd = cwd if isinstance(cwd, str) and cwd else None
            inputs = data.get("inputs")
            result.inputs = inputs if isinstance(inputs, dict) else None

            result.topology = _extract_topology(data)

            # A new generation starts clean: nothing from a dead prior
            # attempt (its terminal status, an unresolved gate, a step
            # that never got its own closing event before the process
            # exited) may leak into how this generation reads. Totals are
            # deliberately untouched -- see the docstring.
            result.status = "running"
            result.gate = None
            result.open_steps = []
            continue

        elif etype in ("agent_started", "parallel_agent_started"):
            # A plain agent opens via `agent_started`; a parallel-group
            # member instead opens via its own `parallel_agent_started`
            # (it never gets a plain `agent_started` -- see
            # `_AGENT_CLOSE_EVENT_TYPES` above).
            name = data.get("agent_name")
            if name is not None and isinstance(ts, int | float):
                result.open_steps.append(("agent", str(name), float(ts)))
            if result.status == "paused":
                result.status = "running"
            # A step starting means the run is executing, not waiting on a
            # human -- so any gate still on record is stale. Needed because
            # `gate_resolved` is not guaranteed: a `questions` node emitted
            # only `gate_presented` until the engine was fixed to pair them,
            # and every log written before that fix still has the unpaired
            # events in it. Without this, such a run shows "at gate" forever.
            if result.gate is not None and str(name) != result.gate.agent_name:
                result.gate = None
                if result.status == "at-gate":
                    result.status = "running"

        elif etype in _AGENT_CLOSE_EVENT_TYPES:
            name = data.get("agent_name")
            if name is not None:
                _close_step(result.open_steps, "agent", str(name))
            if etype == "gate_resolved":
                result.status = "running"
                result.gate = None
            elif (
                result.gate is not None and name is not None and str(name) == result.gate.agent_name
            ):
                # The agent that presented the gate has finished, so the gate
                # went with it even if no `gate_resolved` was ever written
                # (see the note in the `agent_started` branch above).
                result.gate = None
                if result.status == "at-gate":
                    result.status = "running"
            elif etype in ("agent_completed", "parallel_agent_completed"):
                tokens = _finite_float(data.get("tokens"))
                if tokens is not None:
                    result.total_tokens += int(tokens)
                cost = _finite_float(data.get("cost_usd"))
                if cost is not None:
                    result.total_cost_usd = (result.total_cost_usd or 0.0) + cost
                elif tokens is not None and tokens > 0:
                    result.unpriced_agent_count += 1

        elif etype in _AGENT_FAILED_EVENT_TYPES:
            # A failed step also closes its open "agent" entry -- otherwise
            # a script/wait/set/sub-workflow/parallel-member failure would
            # leave `current_step` stuck "open" after the run has already
            # moved on (or terminated).
            name = data.get("agent_name")
            if name is not None:
                _close_step(result.open_steps, "agent", str(name))

        elif etype == "parallel_started":
            name = data.get("group_name")
            if name is not None and isinstance(ts, int | float):
                result.open_steps.append(("parallel", str(name), float(ts)))

        elif etype == "parallel_completed":
            name = data.get("group_name")
            if name is not None:
                _close_step(result.open_steps, "parallel", str(name))

        elif etype == "for_each_started":
            name = data.get("group_name")
            if name is not None and isinstance(ts, int | float):
                result.open_steps.append(("for_each", str(name), float(ts)))

        elif etype == "for_each_completed":
            name = data.get("group_name")
            if name is not None:
                _close_step(result.open_steps, "for_each", str(name))

        elif etype == "gate_presented":
            result.status = "at-gate"
            result.gate = GateInfo(
                agent_name=str(data.get("agent_name", "")),
                prompt=str(data.get("prompt", "")),
                options=[str(o) for o in (data.get("options") or [])],
                option_details=[
                    o for o in (data.get("option_details") or []) if isinstance(o, dict)
                ],
            )

        elif etype == "agent_paused":
            result.status = "paused"

        elif etype == "workflow_completed":
            result.status = "completed"
            result.gate = None

        elif etype == "workflow_failed":
            result.status = "failed"
            result.gate = None
            # A plain agent's unhandled exception has no per-agent failure
            # event of its own -- it propagates straight here. Close the
            # matching open step (if any) via `workflow_failed`'s own
            # `agent_name` so `current_step` doesn't report a step that has
            # actually terminated.
            name = data.get("agent_name")
            if name is not None:
                _close_step(result.open_steps, "agent", str(name))

    return result


def _scan_agent_details(
    events: Iterable[dict[str, Any]],
) -> tuple[RunTopology | None, str | None, list[AgentDetail], str | None]:
    """Single pass over the full event stream building per-agent detail rows (E9-T3).

    Unlike :func:`_scan_events` (which only tracks aggregate totals for the
    Runs list), this tracks each agent's own completion payload
    (``elapsed``/``tokens``/``cost_usd``) individually, keyed by
    ``agent_name`` -- the per-agent history the detail screen needs that the
    list screen's aggregate totals do not carry.

    Topology (and the run's declared workflow name) extraction is folded
    into this same pass -- required once the log is read as a one-shot
    stream (issue #485): :func:`derive_run_detail` used to iterate the
    event list twice, once to find the topology and once here, which a
    one-shot iterator cannot support. The **latest** root
    ``workflow_started`` wins (last generation wins), so a resumed run's
    detail screen reflects the current generation's topology, not a stale
    earlier one. That ``workflow_started`` branch also resets ``open_steps``,
    ``gated`` and ``started_at_by_name`` -- the same resume-boundary reset
    :func:`_scan_events` does -- so a generation killed mid-step (its open
    steps and any unresolved gate never getting a closing event) cannot
    leak into how the next generation reads. Per-agent status/usage
    tracking is otherwise unchanged: an agent's cumulative tokens/cost keep
    accumulating across every restart they already did (a loop-back or a
    resume look the same to this loop -- a fresh ``agent_started`` always
    means "running now", regardless of why the process is executing it
    again), consistent with :func:`_scan_events`'s Q1
    totals-accumulate-across-generations rule.

    Args:
        events: Event dicts, oldest first (see :func:`stream_event_log`).

    Returns:
        A ``(topology, workflow_name, agents, current_step)`` tuple: the
        run's topology from its latest ``workflow_started`` (``None`` if
        the log never had one), that event's declared workflow name
        (``None`` if undeclared or no such event), one :class:`AgentDetail`
        per ``topology.agents`` entry (same order, empty when ``topology``
        is ``None``), and the name of the currently open step (agent,
        parallel group, or for_each group), or ``None`` if nothing is open.
    """
    topology: RunTopology | None = None
    workflow_name: str | None = None

    open_steps: list[tuple[str, str, float]] = []
    # agent_name -> (status, started_at, reported_elapsed) for the latest
    # attempt only -- a loop-back restart must show the *current* attempt's
    # status, not a stale completion from an earlier pass.
    closed: dict[str, tuple[AgentDetailStatus, float | None, float | None]] = {}
    # agent_name -> cumulative tokens/cost across *every* completed attempt
    # (tracked separately from `closed` so a loop-back restart's later
    # completion adds to, rather than overwrites, an earlier attempt's
    # usage -- the row represents the agent's complete history, not just
    # its most recent run).
    cumulative_tokens: dict[str, int] = {}
    cumulative_cost: dict[str, float | None] = {}
    started_at_by_name: dict[str, float] = {}
    # Steps with a presented-but-unresolved gate. An open step that is
    # waiting on a person is not the same as one that is working, and the
    # run-detail screen has no other way to say so now that it no longer
    # repeats the Runs screen's gate panel.
    gated: set[str] = set()

    for evt in events:
        etype = evt.get("type")
        data = evt.get("data")
        if not isinstance(data, dict):
            data = {}
        # Skip nested sub-workflow events (see `_scan_events`'s identical
        # guard) -- a nested agent sharing a root agent's name must not
        # corrupt the root row's status, timing, or usage.
        if data.get("subworkflow_path"):
            continue
        ts = evt.get("timestamp")

        if etype == "workflow_started":
            topology = _extract_topology(data)
            declared = data.get("name")
            workflow_name = declared if isinstance(declared, str) and declared else None
            # A new generation starts clean: an open step or unresolved
            # gate from a dead prior attempt (which got no closing event
            # before the process exited) must not leak into this
            # generation's reading -- the same reset `_scan_events` does at
            # its own `workflow_started` branch. `closed`,
            # `cumulative_tokens` and `cumulative_cost` are deliberately
            # left untouched -- see the docstring.
            open_steps.clear()
            gated.clear()
            started_at_by_name.clear()
            continue

        elif etype in ("agent_started", "parallel_agent_started"):
            # A plain agent opens via `agent_started`; a parallel-group
            # member instead opens via its own `parallel_agent_started` --
            # it never gets a plain `agent_started` at all.
            name = data.get("agent_name")
            if name is not None:
                name = str(name)
                if isinstance(ts, int | float):
                    open_steps.append(("agent", name, float(ts)))
                    started_at_by_name[name] = float(ts)
                # A restarted agent (e.g. a for_each iteration reusing the
                # same inline agent name) is open again -- drop the stale
                # prior *status* so it reflects the latest attempt. Its
                # cumulative usage (tracked separately) is untouched.
                closed.pop(name, None)
                gated.discard(name)

        elif etype == "gate_presented":
            name = data.get("agent_name")
            if name is not None:
                gated.add(str(name))

        elif etype in _AGENT_CLOSE_EVENT_TYPES:
            name = data.get("agent_name")
            if name is not None:
                name = str(name)
                _close_step(open_steps, "agent", name)
                gated.discard(name)
                elapsed = data.get("elapsed")
                if etype in ("agent_completed", "parallel_agent_completed"):
                    tokens = _finite_float(data.get("tokens"))
                    cost = _finite_float(data.get("cost_usd"))
                    if tokens is not None:
                        cumulative_tokens[name] = cumulative_tokens.get(name, 0) + int(tokens)
                    if cost is not None:
                        cumulative_cost[name] = (cumulative_cost.get(name) or 0.0) + cost
                    closed[name] = (
                        "completed",
                        started_at_by_name.get(name),
                        float(elapsed) if isinstance(elapsed, int | float) else None,
                    )
                else:
                    # script_completed / wait_completed / set_completed /
                    # gate_resolved / subworkflow_completed: these non-LLM
                    # step types have no tokens/cost (only a plain agent's
                    # own agent_completed/parallel_agent_completed carries
                    # usage data), but the step did complete -- record that
                    # so the row doesn't appear stuck "pending" forever
                    # once its own closing event fires (mirrors
                    # _scan_events's current-step closing behavior for the
                    # same event set).
                    closed[name] = (
                        "completed",
                        started_at_by_name.get(name),
                        float(elapsed) if isinstance(elapsed, int | float) else None,
                    )

        elif etype in _AGENT_FAILED_EVENT_TYPES:
            name = data.get("agent_name")
            if name is not None:
                name = str(name)
                _close_step(open_steps, "agent", name)
                elapsed = data.get("elapsed")
                closed[name] = (
                    "failed",
                    started_at_by_name.get(name),
                    float(elapsed) if isinstance(elapsed, int | float) else None,
                )

        elif etype == "parallel_started":
            name = data.get("group_name")
            if name is not None and isinstance(ts, int | float):
                open_steps.append(("parallel", str(name), float(ts)))

        elif etype == "parallel_completed":
            name = data.get("group_name")
            if name is not None:
                _close_step(open_steps, "parallel", str(name))

        elif etype == "for_each_started":
            name = data.get("group_name")
            if name is not None and isinstance(ts, int | float):
                open_steps.append(("for_each", str(name), float(ts)))

        elif etype == "for_each_completed":
            name = data.get("group_name")
            if name is not None:
                _close_step(open_steps, "for_each", str(name))

        elif etype == "workflow_failed":
            # A plain agent's unhandled exception has no per-agent failure
            # event of its own -- it propagates straight to
            # `workflow_failed`. Use its `agent_name` to mark that agent's
            # row failed instead of leaving it stuck "running". But real
            # script/wait/set/subworkflow failures emit their own specific
            # *_failed event first (recording elapsed), followed by
            # workflow_failed -- don't clobber that existing record.
            name = data.get("agent_name")
            if name is not None:
                name = str(name)
                _close_step(open_steps, "agent", name)
                existing = closed.get(name)
                if existing is None or existing[0] != "failed":
                    closed[name] = ("failed", started_at_by_name.get(name), None)

    open_agent_names = {name for (kind, name, _ts) in open_steps if kind == "agent"}
    current_step = open_steps[-1][1] if open_steps else None

    if topology is None:
        return None, workflow_name, [], None

    agent_details: list[AgentDetail] = []
    for ta in topology.agents:
        cum_tokens = cumulative_tokens.get(ta.name)
        cum_cost = cumulative_cost.get(ta.name)

        if ta.name in open_agent_names:
            agent_details.append(
                AgentDetail(
                    name=ta.name,
                    type=ta.type,
                    model=ta.model,
                    provider_name=ta.provider_name,
                    status="at-gate" if ta.name in gated else "running",
                    started_at=started_at_by_name.get(ta.name),
                    reported_elapsed_seconds=None,
                    tokens=cum_tokens,
                    cost_usd=cum_cost,
                )
            )
            continue

        info = closed.get(ta.name)
        if info is None:
            agent_details.append(
                AgentDetail(
                    name=ta.name,
                    type=ta.type,
                    model=ta.model,
                    provider_name=ta.provider_name,
                    status="pending",
                    started_at=None,
                    reported_elapsed_seconds=None,
                    tokens=None,
                    cost_usd=None,
                )
            )
            continue

        status, started_at, reported_elapsed = info
        agent_details.append(
            AgentDetail(
                name=ta.name,
                type=ta.type,
                model=ta.model,
                provider_name=ta.provider_name,
                status=status,
                started_at=started_at,
                reported_elapsed_seconds=reported_elapsed,
                tokens=cum_tokens,
                cost_usd=cum_cost,
            )
        )

    return topology, workflow_name, agent_details, current_step


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def derive_run_summary(record: RunRecord) -> RunSummary:
    """Derive a :class:`RunSummary` for ``record`` from its event log.

    ``record`` is assumed to already be known-live (e.g. sourced from
    :func:`conductor.fleet.records.read_run_records`) — this function does
    not re-check liveness; see the module docstring for why. When
    ``record.event_log_path`` is empty, unreadable, or any other
    ``OSError`` occurs while streaming it, this still returns a usable
    summary (``status="running"``, no current step, zero totals) rather
    than raising, so a run whose log hasn't been created yet (or whose
    path is stale) never crashes the caller's poll loop -- the
    :func:`stream_event_log` generator raises on its first ``next()``, not
    at construction, so the ``try``/``except`` here wraps the *consumption*
    (:func:`_scan_events`'s draining loop), not just the call.

    Args:
        record: The run record to summarize.

    Returns:
        A :class:`RunSummary` reflecting the run's state as of the last
        event in the log (see :attr:`RunSummary.total_tokens` for how a
        resumed run's multiple generations are combined).
    """
    scan = _ScanResult()
    if record.event_log_path:
        log_path = Path(record.event_log_path)
        try:
            scan = _scan_events(stream_event_log(log_path, keep_types=_SUMMARY_EVENT_TYPES))
        except OSError:
            logger.debug("Could not read event log %s", log_path, exc_info=True)
            scan = _ScanResult()

    if scan.open_steps:
        current_step_type, current_step, current_step_started_at = scan.open_steps[-1]
    else:
        current_step_type, current_step, current_step_started_at = None, None, None

    return RunSummary(
        run_id=record.run_id,
        # Declared name when the log carries one, else the record's file stem.
        workflow_name=scan.workflow_name or record.workflow_name,
        mode=record.mode,
        port=record.port,
        started_at=record.started_at,
        status=scan.status,
        current_step=current_step,
        current_step_type=current_step_type,  # type: ignore[arg-type]
        current_step_started_at=current_step_started_at,
        total_tokens=scan.total_tokens,
        total_cost_usd=scan.total_cost_usd,
        unpriced_agent_count=scan.unpriced_agent_count,
        gate=scan.gate,
        gate_resolvable=record.port is not None,
        topology=scan.topology,
        cwd=scan.cwd,
        inputs=scan.inputs,
    )


@dataclass(frozen=True, slots=True)
class ActivityLine:
    """One line of a step's activity stream (a message, a tool call, …)."""

    kind: str
    """``message`` / ``reasoning`` / ``tool`` / ``tool_result`` / ``turn``."""

    text: str


@dataclass(frozen=True, slots=True)
class StepDetail:
    """One step's input, output and activity -- the step drill-down (enter on
    a row of the run-detail screen).

    Answers "what did this step actually do", which neither the Runs table
    (state) nor the run-detail table (per-step status/usage) can: the prompt
    that went in, the structured output that came out, and -- while it is
    still running, when there is no output yet -- what it has been doing.
    """

    agent_name: str
    status: str
    prompt: str | None
    output: Any | None
    activity: list[ActivityLine]
    workflow_name: str
    """The run's declared name, not the workflow file's stem."""


def _scan_step_events(
    events: Iterable[dict[str, Any]], agent_name: str
) -> tuple[str, str | None, Any | None, deque[ActivityLine], str | None]:
    """Single pass over the full event stream extracting one step's detail.

    Returns ``(status, prompt, output, activity, workflow_name)`` only
    after the stream drains -- callers must assign the whole tuple in one
    statement inside their own ``try``/``except OSError`` so a mid-stream
    read failure (propagating out of the ``events`` iterator) can never
    leave a caller holding a partial scan: an exception here means the
    assignment in :func:`derive_step_detail` never happens at all, and its
    pristine defaults are returned instead.

    Args:
        events: Event dicts, oldest first (see :func:`stream_event_log`).
        agent_name: The step whose prompt/output/activity to extract.

    Returns:
        The five-tuple described above. ``status`` defaults to
        ``"pending"``; ``prompt``/``output``/``workflow_name`` default to
        ``None``; ``activity`` defaults to an empty, capacity-bounded
        deque.
    """
    prompt: str | None = None
    output: Any | None = None
    status = "pending"
    activity: deque[ActivityLine] = deque(maxlen=_STEP_ACTIVITY_LIMIT)
    # The run's declared workflow name, captured from the same pass (last
    # generation wins) rather than a second scan over the same log -- see
    # `_scan_agent_details`'s identical reasoning.
    workflow_name: str | None = None

    for evt in events:
        data = evt.get("data")
        if not isinstance(data, dict) or data.get("subworkflow_path"):
            continue
        etype = evt.get("type")

        if etype == "workflow_started":
            declared = data.get("name")
            workflow_name = declared if isinstance(declared, str) and declared else None
            # A new generation starts clean: an agent that was mid-flight
            # when a prior generation died (and never got a closing event)
            # cannot still be "running" across a resume boundary -- but a
            # genuine `completed`/`failed` status is real history and is
            # left alone.
            if status == "running":
                status = "pending"
            continue

        if data.get("agent_name") != agent_name:
            continue

        if etype in ("agent_started", "parallel_agent_started"):
            status = "running"
            # A re-run (loop-back) supersedes the previous attempt's result.
            prompt = None
            output = None
            activity.clear()
        elif etype == "agent_prompt_rendered":
            rendered = data.get("rendered_prompt")
            if isinstance(rendered, str):
                prompt = rendered
        elif etype in ("agent_completed", "parallel_agent_completed"):
            status = "completed"
            output = data.get("output")
        elif etype == "questions_completed" or etype in _AGENT_CLOSE_EVENT_TYPES:
            status = "completed"
        elif etype in _AGENT_FAILED_EVENT_TYPES:
            status = "failed"

        if etype == "agent_message":
            content = data.get("content")
            if isinstance(content, str) and content.strip():
                activity.append(ActivityLine("message", content.strip()))
        elif etype == "agent_reasoning":
            content = data.get("content")
            if isinstance(content, str) and content.strip():
                activity.append(ActivityLine("reasoning", content.strip()))
        elif etype == "agent_tool_start":
            activity.append(ActivityLine("tool", str(data.get("tool_name") or "tool")))
        elif etype == "agent_tool_complete":
            activity.append(ActivityLine("tool_result", str(data.get("tool_name") or "tool")))

    return status, prompt, output, activity, workflow_name


def derive_step_detail(record: RunRecord, agent_name: str) -> StepDetail:
    """Extract one step's input/output/activity from a run's event log.

    Streams the whole (uncapped) log -- a step's prompt is emitted once,
    when it started, which on a long run the old bounded tail window
    always missed. When ``record.event_log_path`` is empty, or any
    ``OSError`` occurs while streaming it, this still returns a usable
    (``status="pending"``, no prompt/output/activity) :class:`StepDetail`
    rather than raising, mirroring :func:`derive_run_summary` /
    :func:`derive_run_detail`'s never-raise contract. The scan itself lives
    in :func:`_scan_step_events` and is assigned here in one statement so a
    mid-stream ``OSError`` can never leave this function holding (and
    returning) a partial scan as if it were authoritative.

    Activity is bounded to the most recent :data:`_STEP_ACTIVITY_LIMIT`
    entries -- a long agentic loop emits hundreds of tool calls, and this is
    a drill-down, not a log viewer.

    Args:
        record: The run record whose event log to read.
        agent_name: The step (agent, parallel-group member, or non-LLM
            step type) to extract input/output/activity for.

    Returns:
        A :class:`StepDetail` for ``agent_name``.
    """
    prompt: str | None = None
    output: Any | None = None
    status = "pending"
    activity: deque[ActivityLine] = deque(maxlen=_STEP_ACTIVITY_LIMIT)
    workflow_name: str | None = None

    if record.event_log_path:
        log_path = Path(record.event_log_path)
        try:
            status, prompt, output, activity, workflow_name = _scan_step_events(
                stream_event_log(log_path), agent_name
            )
        except OSError:
            logger.debug("Could not read event log %s", log_path, exc_info=True)

    return StepDetail(
        agent_name=agent_name,
        status=status,
        prompt=prompt,
        output=output,
        activity=list(activity),
        workflow_name=workflow_name or record.workflow_name,
    )


def derive_run_detail(record: RunRecord) -> RunDetail:
    """Derive a :class:`RunDetail` for ``record`` from its event log's full
    (streamed) contents -- the run-detail screen's data source (E9-T3),
    distinct from :func:`derive_run_summary`'s aggregate-only scan the
    polled Runs list uses.

    When ``record.event_log_path`` is empty, unreadable, or the log has no
    ``workflow_started`` event, this returns a :class:`RunDetail` with
    ``topology=None`` and an empty ``agents`` list rather than raising --
    the detail screen renders a placeholder for this case (E9-T5) instead
    of crashing or showing an empty table.

    Returns:
        A :class:`RunDetail` with one row per topology agent (in the
        topology's own order) and the name of the currently open step.
    """
    topology: RunTopology | None = None
    workflow_name: str | None = None
    agents: list[AgentDetail] = []
    current_step: str | None = None

    if record.event_log_path:
        log_path = Path(record.event_log_path)
        try:
            topology, workflow_name, agents, current_step = _scan_agent_details(
                stream_event_log(log_path, keep_types=_SUMMARY_EVENT_TYPES)
            )
        except OSError:
            logger.debug("Could not read event log %s", log_path, exc_info=True)

    return RunDetail(
        run_id=record.run_id,
        workflow_name=workflow_name or record.workflow_name,
        topology=topology,
        agents=agents,
        current_step=current_step,
    )
