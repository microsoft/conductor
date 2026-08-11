"""``RunSummary`` derivation from a run record plus its event-log tail.

Fleet Manager E6 (see ``docs/projects/fleet-manager/fleet-manager.design.md``,
*Implementation* → ``summary.py``): given a live :class:`~conductor.fleet.records.RunRecord`
(as returned by :func:`conductor.fleet.records.read_run_records`), derive a
:class:`RunSummary` — the status vocabulary, current step, elapsed-on-step,
token/cost totals, and any open human gate — from a **bounded tail** of the
run's JSONL event log rather than the whole file, so this can be called once
per row on a ~2-second poll loop (E7's Runs screen) without the cost of a
whole-file load growing with the run's lifetime (contrast
``web/replay.py::_load_events``, which loads the entire file and is the wrong
tool for this).

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
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from conductor.fleet.records import RunRecord

logger = logging.getLogger(__name__)

RunStatus = Literal["running", "at-gate", "paused", "completed", "failed"]

AgentDetailStatus = Literal["pending", "running", "completed", "failed"]

# Bounded read windows. Neither grows with the file's size, satisfying the
# "bounded, not whole-file" requirement even for a very long-running
# workflow's log. 512 KB comfortably covers a typical run's entire event
# history (events are small JSON lines), so token/cost totals and current-step
# tracking are correct in the common case; for an unusually long run whose
# log has grown past this window, older completed-agent totals age out of the
# tail and are undercounted — an accepted, documented limitation consistent
# with this project's other "known data gaps" (see the module docstring's
# liveness note and D5 in the plan).
_DEFAULT_TAIL_BYTES = 512 * 1024

# Bound for the run-detail screen's *full*-log read (E9-T3). Unlike the tail
# reader above, this reads from the start of the file so every agent's
# history (not just the most recent window) is available for the per-agent
# rows the detail screen renders. 8 MB is comfortably larger than any
# realistic single-run event log (the design's own directory-wide
# measurement was 12 MB across 1522 files) while still bounding memory use
# against a pathological log; a log that exceeds this bound has its
# *trailing* history (including, in the worst case, the run's own
# `workflow_completed`/`workflow_failed`) silently truncated rather than
# read in full -- an accepted, documented limitation, not a crash.
_DEFAULT_FULL_LOG_MAX_BYTES = 8 * 1024 * 1024


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
    the full log. ``"running"`` -- currently the open step. ``"completed"``
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
    bounded **full** log read (:func:`read_event_log_full`) rather than the
    tail window :class:`RunSummary` uses -- the list screen's ~2s poll stays
    on the cheaper tail path; only opening the detail screen pays for a
    fuller read, and only once per open (not on every poll tick)."""

    run_id: str
    workflow_name: str
    topology: RunTopology | None
    """``None`` when the log is missing/unreadable, empty, or its
    ``workflow_started`` event fell outside the (bounded) read window --
    the detail screen renders a placeholder in this case rather than an
    empty table (E9-T5)."""

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
    started without a matching completion event seen in the tail. ``None``
    when nothing is open (e.g. a log with no events yet, or a terminal
    status)."""

    current_step_type: Literal["agent", "parallel", "for_each"] | None
    current_step_started_at: float | None
    """Unix timestamp of the current step's start event, or ``None``."""

    total_tokens: int
    """Sum of ``tokens`` across every ``agent_completed`` event seen in the
    tail. Per D5, this is **completed-agent tokens only** — there is no
    mid-flight usage event, so the agent currently running never
    contributes here until it finishes."""

    total_cost_usd: float | None
    """Sum of ``cost_usd`` across priced ``agent_completed`` events in the
    tail, or ``None`` if none were priced. Mirrors
    ``WorkflowUsage.total_cost_usd``: never a confident total when
    :attr:`has_unpriced` is true — see that attribute."""

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
    """Run topology from ``workflow_started``, if that event happened to
    fall within the read window (E6-T5). ``workflow_started`` is always
    the first line of the log, so this is populated for a just-started run;
    for an older run whose log has grown past the tail window, this is
    ``None`` until a dedicated full-log read is added (E9-T3) for the
    run-detail screen."""

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
# Bounded JSONL reading (E6-T1)
# ---------------------------------------------------------------------------


def _parse_jsonl_bytes(raw: bytes) -> list[dict[str, Any]]:
    """Parse ``raw`` as newline-delimited JSON objects, tolerating bad lines.

    Each line is decoded and parsed independently, so a single malformed
    line (a truncated write caught mid-flush, invalid UTF-8, or a line cut
    off by a bounded read's window boundary) is silently skipped rather
    than aborting the whole parse — this is what lets the tail/head readers
    tolerate a file being appended to concurrently.
    """
    events: list[dict[str, Any]] = []
    for raw_line in raw.split(b"\n"):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def read_event_log_tail(
    path: Path, *, tail_bytes: int = _DEFAULT_TAIL_BYTES
) -> list[dict[str, Any]]:
    """Read the last ``tail_bytes`` of a JSONL event log, parsed into event dicts.

    Seeks from the end of the file rather than loading it whole (E6-T1):
    when the file is larger than ``tail_bytes``, seeks to
    ``size - tail_bytes`` and discards the (likely partial) leading line
    before parsing the rest, so a line straddling the seek boundary never
    surfaces as a corrupt/garbled event. A trailing line cut short by a
    concurrent in-progress write is tolerated the same way every other
    malformed line is — it fails to parse and is silently skipped (see
    :func:`_parse_jsonl_bytes`).

    Never raises: a missing file, a permission error, or any other
    ``OSError`` while reading yields an empty event list rather than
    propagating — a diagnostic read must not be able to crash a poll loop.

    Args:
        path: Path to the JSONL event log.
        tail_bytes: Maximum number of bytes to read from the end of the
            file, regardless of the file's actual size.

    Returns:
        Parsed event dicts, oldest first, from (at most) the last
        ``tail_bytes`` of the file.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # Discard the partial line at the seek boundary.
            else:
                f.seek(0)
            raw = f.read()
    except OSError:
        return []
    return _parse_jsonl_bytes(raw)


def read_event_log_full(
    path: Path, *, max_bytes: int = _DEFAULT_FULL_LOG_MAX_BYTES
) -> list[dict[str, Any]]:
    """Read up to ``max_bytes`` from the *start* of a JSONL event log (E9-T3).

    Unlike :func:`read_event_log_tail` (which the Runs list screen's ~2s
    poll uses so per-row reads stay cheap and bounded regardless of the
    run's age), this is the **full**-log read used only by the run-detail
    screen: it needs every agent's complete history -- including agents
    that finished and aged out of a tail window -- not just the most
    recent events. It is still bounded (not a literal whole-file read) so
    a pathologically large log can't exhaust memory when a user opens the
    detail screen; see :data:`_DEFAULT_FULL_LOG_MAX_BYTES` for the bound
    and its trade-off.

    Never raises: mirrors :func:`read_event_log_tail`'s contract -- a
    missing file, a permission error, or any other ``OSError`` yields an
    empty event list rather than propagating.

    Args:
        path: Path to the JSONL event log.
        max_bytes: Maximum number of bytes to read from the start of the
            file, regardless of the file's actual size.

    Returns:
        Parsed event dicts, oldest first, from (at most) the first
        ``max_bytes`` of the file.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(max_bytes)
    except OSError:
        return []
    return _parse_jsonl_bytes(raw)


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


def _scan_events(events: list[dict[str, Any]]) -> _ScanResult:
    """Single pass over event dicts deriving status, gate, current step, and totals.

    Events are assumed oldest-first (the natural order of a JSONL log and
    of :func:`read_event_log_tail`'s output), so later events in the list
    override earlier ones for state that only has one current value
    (``status``, ``gate``).
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
        # anything outside the root context.
        if data.get("subworkflow_path"):
            continue
        ts = evt.get("timestamp")

        if etype == "workflow_started" and result.topology is None:
            result.topology = _extract_topology(data)

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

        elif etype in _AGENT_CLOSE_EVENT_TYPES:
            name = data.get("agent_name")
            if name is not None:
                _close_step(result.open_steps, "agent", str(name))
            if etype == "gate_resolved":
                result.status = "running"
                result.gate = None
            elif etype in ("agent_completed", "parallel_agent_completed"):
                tokens = data.get("tokens")
                if isinstance(tokens, int | float):
                    result.total_tokens += int(tokens)
                cost = data.get("cost_usd")
                if isinstance(cost, int | float):
                    result.total_cost_usd = (result.total_cost_usd or 0.0) + float(cost)
                elif isinstance(tokens, int | float) and tokens > 0:
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
    events: list[dict[str, Any]], topology: RunTopology | None
) -> tuple[list[AgentDetail], str | None]:
    """Single pass over **full**-log events building per-agent detail rows (E9-T3).

    Unlike :func:`_scan_events` (which only tracks aggregate totals for the
    Runs list), this tracks each agent's own completion payload
    (``elapsed``/``tokens``/``cost_usd``) individually, keyed by
    ``agent_name`` -- the per-agent history the detail screen needs that the
    list screen's aggregate totals do not carry.

    Args:
        events: Full-log event dicts, oldest first (see
            :func:`read_event_log_full`).
        topology: The run's topology (agent order/definitions), or ``None``
            if unavailable -- in which case there is nothing to build rows
            for.

    Returns:
        A ``(agents, current_step)`` tuple: one :class:`AgentDetail` per
        ``topology.agents`` entry (same order), and the name of the
        currently open step (agent, parallel group, or for_each group), or
        ``None`` if nothing is open.
    """
    if topology is None:
        return [], None

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

        if etype in ("agent_started", "parallel_agent_started"):
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

        elif etype in _AGENT_CLOSE_EVENT_TYPES:
            name = data.get("agent_name")
            if name is not None:
                name = str(name)
                _close_step(open_steps, "agent", name)
                elapsed = data.get("elapsed")
                if etype in ("agent_completed", "parallel_agent_completed"):
                    tokens = data.get("tokens")
                    cost = data.get("cost_usd")
                    if isinstance(tokens, int | float):
                        cumulative_tokens[name] = cumulative_tokens.get(name, 0) + int(tokens)
                    if isinstance(cost, int | float):
                        cumulative_cost[name] = (cumulative_cost.get(name) or 0.0) + float(cost)
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
                    status="running",
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

    return agent_details, current_step


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def derive_run_summary(
    record: RunRecord,
    *,
    now: float | None = None,
    tail_bytes: int = _DEFAULT_TAIL_BYTES,
) -> RunSummary:
    """Derive a :class:`RunSummary` for ``record`` from its event log's tail.

    ``record`` is assumed to already be known-live (e.g. sourced from
    :func:`conductor.fleet.records.read_run_records`) — this function does
    not re-check liveness; see the module docstring for why. When
    ``record.event_log_path`` is empty or unreadable, this still returns a
    usable summary (``status="running"``, no current step, zero totals)
    rather than raising, so a run whose log hasn't been created yet (or
    whose path is stale) never crashes the caller's poll loop.

    Args:
        record: The run record to summarize.
        now: Reference time for elapsed calculations (defaults to
            ``time.time()``); exposed for deterministic testing.
        tail_bytes: Passed through to :func:`read_event_log_tail`.

    Returns:
        A :class:`RunSummary` reflecting the run's state as of the last
        event visible within the tail window.
    """
    events: list[dict[str, Any]] = []
    if record.event_log_path:
        events = read_event_log_tail(Path(record.event_log_path), tail_bytes=tail_bytes)

    scan = _scan_events(events)

    if scan.open_steps:
        current_step_type, current_step, current_step_started_at = scan.open_steps[-1]
    else:
        current_step_type, current_step, current_step_started_at = None, None, None

    return RunSummary(
        run_id=record.run_id,
        workflow_name=record.workflow_name,
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
    )


def derive_run_detail(
    record: RunRecord, *, max_bytes: int = _DEFAULT_FULL_LOG_MAX_BYTES
) -> RunDetail:
    """Derive a :class:`RunDetail` for ``record`` from its event log's **full**
    (bounded) contents -- the run-detail screen's data source (E9-T3),
    distinct from :func:`derive_run_summary`'s tail-based read the polled
    Runs list uses.

    When ``record.event_log_path`` is empty or unreadable, or the log has
    no (or no yet-visible) ``workflow_started`` event, this returns a
    :class:`RunDetail` with ``topology=None`` and an empty ``agents`` list
    rather than raising -- the detail screen renders a placeholder for this
    case (E9-T5) instead of crashing or showing an empty table.

    Args:
        record: The run record to derive detail for.
        max_bytes: Passed through to :func:`read_event_log_full`.

    Returns:
        A :class:`RunDetail` with one row per topology agent (in the
        topology's own order) and the name of the currently open step.
    """
    events: list[dict[str, Any]] = []
    if record.event_log_path:
        events = read_event_log_full(Path(record.event_log_path), max_bytes=max_bytes)

    topology: RunTopology | None = None
    for evt in events:
        if evt.get("type") == "workflow_started":
            data = evt.get("data")
            if not isinstance(data, dict):
                continue
            # Skip a nested sub-workflow's own workflow_started (stamped
            # with subworkflow_path) -- only the root run's topology
            # belongs on this screen.
            if data.get("subworkflow_path"):
                continue
            topology = _extract_topology(data)
            break

    agents, current_step = _scan_agent_details(events, topology)

    return RunDetail(
        run_id=record.run_id,
        workflow_name=record.workflow_name,
        topology=topology,
        agents=agents,
        current_step=current_step,
    )
