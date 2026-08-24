"""Run lifecycle tools: status, bounded await, cancel, and list (FR6, FR7,
DD11, E10).

Data flows C and D from the design (*API Contracts*, *Key Components -> 4*):
a `run_id` must be answerable **before**, **during**, **at a gate**, and
**after** the run it names -- whether or not this server's own process is
still the one that launched it. The single :func:`resolve_run` (E10-T1) is
the one place that decides which of three sources answers:

```
read_run_record(run_id)  --found & alive-->  derive_run_summary(record)  (live)
        |
        +--not found / dead-->  read_terminal_record(run_id)  (finished cleanly)
                    |
                    +--not found-->  find_event_log_for_run(...)  (crashed)
```

Every public function in this module builds its result from
:func:`resolve_run`'s output rather than re-deriving status itself, so this
file never re-implements the state machine `fleet/summary.py::derive_run_summary`
(and, for a crash, `fleet/summary.py::_scan_events`) already owns -- see the
epic's own acceptance criterion: "Nothing here re-implements the stop ladder
or the status derivation."

**Note on `GET /api/info` enrichment.** The design's ASCII diagram above
(*Key Components -> 4*) shows the live branch as "enriched from ``GET
/api/info``". This module deliberately does **not** add a live HTTP probe:
`derive_run_summary` already yields everything `conductor_run_status` needs
(`status`, `current_step`, totals, `gate: GateInfo`, `gate_resolvable`) from
the event log alone -- the same event-log-first choice `/api/gate-status`'s
own docstring calls "strictly richer" than a live probe. What the resolver
*does* need from a live check is **liveness**, not `/api/info`'s payload: a
stale `RunRecord` whose process has already exited (but which has not yet
been pruned) must fall through to the terminal/event-log branches rather
than being reported as still `"running"` -- that is what
:func:`conductor.cli.pid.is_process_alive` is for below, matching the same
liveness gate :func:`conductor.fleet.records.read_run_records` already
applies to every record it surfaces.
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from conductor.cli.pid import is_process_alive
from conductor.console import make_console
from conductor.fleet.records import (
    RunRecord,
    TerminalRunRecord,
    find_event_log_for_run,
    is_valid_run_id,
    read_run_record,
    read_terminal_record,
    read_terminal_records,
    scan_run_records,
)
from conductor.fleet.summary import (
    GateInfo,
    RunSummary,
    _scan_events,  # crash fallback -- mirrors fleet/history.py's own cross-module reuse
    derive_run_summary,
    read_event_log_full,
)
from conductor.mcp.serve.options import DEFAULT_MAX_WAIT_SECONDS

logger = logging.getLogger(__name__)

# ``ServerSession.send_progress_notification``'s signature (mirrors
# ``mcp/serve/invoke.py``'s identical alias).
_ProgressSender = Callable[[str | int, float, float | None, str | None], Awaitable[None]]

RunSource = Literal["live", "terminal", "event_log", "not_found"]


# ---------------------------------------------------------------------------
# E10-T1: the three-source resolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunLookup:
    """The uniform shape :func:`resolve_run` returns from any of its three
    sources (E10-T1).

    Exactly one of :attr:`summary`, :attr:`terminal`, :attr:`event_log_path`
    is non-``None``, matching :attr:`source`. Carrying :attr:`source`
    explicitly is the point of this dataclass: a caller must be able to
    tell "completed cleanly" (``source == "terminal"``) from "process
    vanished" (``source == "event_log"``) rather than inferring it from
    which optional field happens to be set.
    """

    run_id: str
    source: RunSource
    record: RunRecord | None = None
    """The live :class:`RunRecord`, only when ``source == "live"``. Kept
    alongside :attr:`summary` (rather than requiring a second lookup) so
    :func:`conductor_cancel_run` can hand it straight to ``stop_records``."""
    summary: RunSummary | None = None
    terminal: TerminalRunRecord | None = None
    event_log_path: Path | None = None


def resolve_run(run_id: str) -> RunLookup:
    """Resolve ``run_id`` through the three-source ladder (E10-T1).

    Args:
        run_id: The run identifier to look up.

    Returns:
        A :class:`RunLookup` naming which source answered:

        - ``"live"`` -- a run record exists and its process is confirmed
          alive (:func:`conductor.cli.pid.is_process_alive`); the run may
          be ``running``, ``at-gate``, ``paused``, or (the narrow race
          ``fleet/summary.py`` documents) already terminal in the event
          log while the process is still exiting.
        - ``"terminal"`` -- no live record (never existed, or its process
          has already exited), but a :class:`TerminalRunRecord` tombstone
          was written by that run's own ``finally`` block: it ended
          cleanly, one way or the other.
        - ``"event_log"`` -- neither a live nor a terminal record exists,
          but its event log can still be located by filename
          (:func:`conductor.fleet.records.find_event_log_for_run`): the
          process almost certainly crashed or was ``kill -9``'d before its
          ``finally`` block ran (see that module's *Key Components -> 4*
          ⚠️ note -- this is the one case a terminal record can never
          cover).
        - ``"not_found"`` -- none of the three sources knows this
          ``run_id`` at all, including a ``run_id`` that is not
          path-safe (e.g. ``"*"`` or ``"../x"``) -- rejected before any
          lookup is attempted so it can never be interpolated into a
          glob pattern and match another run's files.
    """
    if not is_valid_run_id(run_id):
        return RunLookup(run_id=run_id, source="not_found")

    record = read_run_record(run_id)
    if record is not None and is_process_alive(record.pid):
        return RunLookup(
            run_id=run_id, source="live", record=record, summary=derive_run_summary(record)
        )

    terminal = read_terminal_record(run_id)
    if terminal is not None:
        return RunLookup(run_id=run_id, source="terminal", terminal=terminal)

    started_at = record.started_at if record is not None else None
    log_path = find_event_log_for_run(run_id, started_at)
    if log_path is not None:
        return RunLookup(run_id=run_id, source="event_log", event_log_path=log_path)

    return RunLookup(run_id=run_id, source="not_found")


# ---------------------------------------------------------------------------
# Shared result shaping
# ---------------------------------------------------------------------------


def _dashboard_url(port: int | None) -> str | None:
    return f"http://127.0.0.1:{port}" if port is not None else None


def _gate_payload(gate: GateInfo) -> dict[str, Any]:
    return {
        "agent_name": gate.agent_name,
        "prompt": gate.prompt,
        "options": gate.options,
        "option_details": gate.option_details,
    }


def _live_status_payload(lookup: RunLookup) -> dict[str, Any]:
    """Shape a ``source == "live"`` lookup (E10-T2)."""
    summary = lookup.summary
    assert summary is not None, "resolve_run only sets source='live' alongside a summary"
    url = _dashboard_url(summary.port)
    payload: dict[str, Any] = {
        "run_id": lookup.run_id,
        "source": "live",
        "status": summary.status,
        "workflow_name": summary.workflow_name,
        "current_step": summary.current_step,
        "started_at": summary.started_at,
        "total_tokens": summary.total_tokens,
        "total_cost_usd": summary.total_cost_usd,
        "unpriced_agent_count": summary.unpriced_agent_count,
        "url": url,
    }
    if summary.status == "at-gate" and summary.gate is not None:
        payload["gate"] = _gate_payload(summary.gate)
        # `summary.gate_resolvable` (not a hardcoded `True`): every run
        # *this server* launches gets `web_port=0` (mcp/serve/invoke.py),
        # so it always has a dashboard port and is therefore always
        # gate-resolvable in practice (DD2) -- but `conductor_run_status`
        # can be asked about any run_id, including one a plain foreground
        # `conductor run` produced, which genuinely has no port to resolve
        # a gate through. Passing the derived value through (rather than
        # a literal `True`) keeps that case honest while still satisfying
        # DD2's guarantee for every MCP-launched run in practice (E10-T2).
        payload["gate_resolvable"] = summary.gate_resolvable
        payload["approval_url"] = url
        payload["next"] = (
            f"Run is waiting at a human gate. Respond at {url}, or call "
            f"conductor_await_run(run_id={lookup.run_id!r}) again once it is resolved."
        )
    return payload


def _terminal_status_payload(lookup: RunLookup) -> dict[str, Any]:
    """Shape a ``source == "terminal"`` lookup: a cleanly-finished run, with
    no process, port, or event log needed (E10-T2)."""
    from conductor.cli.app import _format_terminal_status, _terminal_duration_seconds

    terminal = lookup.terminal
    assert terminal is not None, "resolve_run only sets source='terminal' alongside a record"
    return {
        "run_id": lookup.run_id,
        "source": "terminal",
        "status": _format_terminal_status(terminal.status),
        "workflow_name": terminal.workflow_name,
        "started_at": terminal.started_at,
        "ended_at": terminal.ended_at,
        "duration_seconds": _terminal_duration_seconds(terminal),
        "output": terminal.output,
        "error_type": terminal.error_type,
        "error_message": terminal.error_message,
        "total_tokens": terminal.total_tokens,
        "total_cost_usd": terminal.total_cost_usd,
        "unpriced_agent_count": terminal.unpriced_agent_count,
    }


def _event_log_status_payload(lookup: RunLookup) -> dict[str, Any]:
    """Shape a ``source == "event_log"`` lookup: the crash-fallback rung
    (E10-T2). Reuses ``fleet/summary.py``'s own single-pass event scan
    rather than re-deriving status from the raw events here."""
    path = lookup.event_log_path
    assert path is not None, "resolve_run only sets source='event_log' alongside a path"
    events = read_event_log_full(path)
    scan = _scan_events(events)
    # The process is confirmed gone (no live record) and never wrote a
    # terminal record either, so any non-terminal status the scan reports
    # (`"running"`/`"paused"`/`"at-gate"`) describes a moment before the
    # crash, not the run's current state -- there is no "current state"
    # left to report. Only a genuine terminal event in the log (the
    # tombstone write itself failed for some other reason, after the run
    # otherwise finished) is trusted as-is.
    status = scan.status if scan.status in ("completed", "failed") else "unknown"
    return {
        "run_id": lookup.run_id,
        "source": "event_log",
        "status": status,
        "workflow_name": scan.workflow_name or "",
        "total_tokens": scan.total_tokens,
        "total_cost_usd": scan.total_cost_usd,
        "unpriced_agent_count": scan.unpriced_agent_count,
        "event_log_path": str(path),
        "note": (
            "No live or terminal run record exists for this run_id -- its process most "
            "likely exited before writing one (a crash or a kill -9). This status was "
            "recovered directly from its event log, which may be missing its final events."
        ),
    }


def _not_found_payload(lookup: RunLookup) -> dict[str, Any]:
    return {
        "run_id": lookup.run_id,
        "source": "not_found",
        "status": "unknown",
        "error": (
            f"No run found for run_id {lookup.run_id!r}: no live run record, terminal "
            "record, or event log could be located."
        ),
    }


def _status_payload(lookup: RunLookup) -> dict[str, Any]:
    """Shape any :class:`RunLookup` into the uniform status dict every
    lifecycle tool below returns."""
    if lookup.source == "live":
        return _live_status_payload(lookup)
    if lookup.source == "terminal":
        return _terminal_status_payload(lookup)
    if lookup.source == "event_log":
        return _event_log_status_payload(lookup)
    return _not_found_payload(lookup)


# ---------------------------------------------------------------------------
# E10-T2: conductor_run_status
# ---------------------------------------------------------------------------


def conductor_run_status(run_id: str) -> dict[str, Any]:
    """``conductor_run_status(run_id)`` (E10-T2, FR6, FR7).

    Live or finished. At a gate, includes the gate's ``prompt``,
    ``options``, ``option_details`` and the dashboard ``approval_url``
    (FR7).

    Args:
        run_id: The run identifier to look up.

    Returns:
        A status dict; see :func:`_status_payload` for the per-source shape.
    """
    return _status_payload(resolve_run(run_id))


# ---------------------------------------------------------------------------
# E10-T3: conductor_await_run
# ---------------------------------------------------------------------------

_POLL_INTERVAL_SECONDS = 2.0
"""Matches ``mcp/serve/invoke.py``'s identical poll cadence, itself matching
the Fleet Manager Runs screen's own ~2s poll."""


async def _sleep(seconds: float) -> None:
    """Thin wrapper around ``asyncio.sleep`` so a test can patch this one
    name (``conductor.mcp.serve.runs._sleep``) without affecting every
    other concurrently-running test (mirrors ``mcp/serve/invoke.py``'s
    identical helper)."""
    import asyncio

    await asyncio.sleep(seconds)


async def conductor_await_run(
    run_id: str,
    *,
    wait_seconds: float = 60.0,
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
    progress_token: str | int | None = None,
    send_progress: _ProgressSender | None = None,
) -> dict[str, Any]:
    """``conductor_await_run(run_id, wait_seconds=60)`` (E10-T3, FR6).

    Bounded by ``max_wait_seconds`` (the server's ``--max-wait-seconds``
    ceiling) regardless of what the caller requests, emitting
    ``notifications/progress`` once per poll when both ``progress_token``
    and ``send_progress`` are supplied. Returns as soon as the run reaches
    a terminal status **or** ``at-gate`` -- reaching a gate ends the wait
    rather than burning the rest of the budget waiting for a human to
    resolve it (DD11). On a genuine timeout, the returned status still
    names the next action to take; when the run is waiting at a gate, that
    "next" text (already set by :func:`_live_status_payload`) names the
    dashboard approval URL (DD11's second bullet).

    Args:
        run_id: The run identifier to await.
        wait_seconds: How long to wait, capped at ``max_wait_seconds``.
            Zero or negative returns (almost) immediately, after a single
            status check.
        max_wait_seconds: The server's ``--max-wait-seconds`` ceiling.
        progress_token: The caller-supplied MCP progress token, if any.
        send_progress: An async callable matching
            ``ServerSession.send_progress_notification``'s signature.
            ``None`` (the default) silently disables progress regardless
            of ``progress_token``.

    Returns:
        A status dict; see :func:`_status_payload`. On a timeout, an
        additional ``"next"`` key names the next action to take.
    """
    bounded = min(max(float(wait_seconds), 0.0), float(max_wait_seconds))
    deadline = time.monotonic() + bounded
    progress = 0.0

    while True:
        lookup = resolve_run(run_id)

        if lookup.source != "live" or (
            lookup.summary is not None
            and lookup.summary.status in ("completed", "failed", "at-gate")
        ):
            return _status_payload(lookup)

        now = time.monotonic()
        if now >= deadline:
            payload = _status_payload(lookup)
            payload["next"] = (
                f"Still running after the bounded wait. Call conductor_await_run(run_id="
                f"{run_id!r}) again, or call conductor_run_status(run_id={run_id!r})."
            )
            return payload

        if progress_token is not None and send_progress is not None:
            progress += 1.0
            status_label = lookup.summary.status if lookup.summary is not None else "starting"
            await send_progress(
                progress_token, progress, None, f"Waiting for run {run_id} ({status_label})..."
            )

        remaining = max(deadline - now, 0.0)
        await _sleep(min(_POLL_INTERVAL_SECONDS, remaining))


# ---------------------------------------------------------------------------
# E10-T4: conductor_cancel_run
# ---------------------------------------------------------------------------


def _silent_console() -> Any:
    """A Rich console that discards its output, for ``stop_records``'s CLI-
    style progress prints -- mirrors ``fleet/tui/actions.py::_silent_console``
    exactly, reimplemented here (rather than imported) because that module
    is only importable with the optional ``tui`` extra installed
    (``textual``), which this module must not require."""
    return make_console(file=io.StringIO(), width=200)


def conductor_cancel_run(run_id: str, *, force: bool = False) -> dict[str, Any]:
    """``conductor_cancel_run(run_id, force=False)`` (E10-T4, FR6).

    Reuses :func:`conductor.cli.app.stop_records` with ``confirm=None`` (the
    mode built for a non-CLI caller, already used by the Fleet TUI's kill
    action) and a silent console, so the graceful ``POST /api/stop`` rung --
    the only one that writes a resume checkpoint, via
    ``WorkflowEngine.handle_dashboard_stop`` -- is tried first, and the
    verify-then-report contract (a record is only reported stopped once its
    process is actually confirmed gone) is inherited rather than
    reimplemented here.

    Args:
        run_id: The run identifier to cancel.
        force: Accepted for signature parity with the design's lifecycle
            table, but **not** threaded into ``stop_records``'s escalation
            ladder: ``stop_records`` (unlike ``conductor stop --force``'s
            own direct call to ``_stop_process``) has no forceful-override
            parameter, and reimplementing that ladder here to add one would
            violate this epic's own acceptance criterion ("Nothing here
            re-implements the stop ladder"). When ``force=True`` is
            requested but the graceful ladder did not confirm the run
            stopped, the result's ``"note"`` says so honestly rather than
            silently claiming a forceful termination that did not happen.

    Returns:
        On an already-terminal run (``source != "live"``): ``{"run_id",
        "status": "already_terminal", "source", "run_status"}`` -- a
        distinct, non-error outcome, not a failure.

        Otherwise: ``{"run_id", "status": "stopped" | "failed", ...}``,
        honestly reflecting whatever :func:`~conductor.cli.app.stop_records`
        confirmed.
    """
    lookup = resolve_run(run_id)
    if lookup.source != "live":
        status_payload = _status_payload(lookup)
        return {
            "run_id": run_id,
            "status": "already_terminal",
            "source": lookup.source,
            "run_status": status_payload.get("status"),
            "note": "This run is no longer live; there is nothing to cancel.",
        }

    assert lookup.record is not None, "resolve_run only sets source='live' alongside a record"

    from conductor.cli.app import stop_records

    outcome = stop_records([lookup.record], _silent_console(), confirm=None)

    result: dict[str, Any]
    if outcome.stopped:
        result = {
            "run_id": run_id,
            "status": "stopped",
            "workflow_name": lookup.record.workflow_name,
        }
    elif outcome.failed:
        _, reason = outcome.failed[0]
        result = {
            "run_id": run_id,
            "status": "failed",
            "reason": reason,
            "workflow_name": lookup.record.workflow_name,
        }
    else:  # pragma: no cover -- confirm=None means `declined` can never happen here
        result = {"run_id": run_id, "status": "failed", "reason": "declined"}

    if force:
        result["note"] = (
            "force=True was requested, but conductor_cancel_run always routes through the "
            "same graceful stop ladder `conductor stop` uses (POST /api/stop, then a "
            "platform signal) and never escalates to forceful termination on its own. "
            "If the run survived, use `conductor stop --force --run-id <run_id>` from the "
            "CLI to force-terminate it."
        )
    return result


# ---------------------------------------------------------------------------
# E10-T5: conductor_list_runs
# ---------------------------------------------------------------------------


def conductor_list_runs(
    *,
    status: str | None = None,
    workflow: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """``conductor_list_runs(status?, workflow?, limit=20)`` (E10-T5, FR6).

    Enumerates live records union terminal records, deduplicated by
    ``run_id`` with the live record winning -- the same run can appear in
    both sources in the narrow window between a `workflow_completed`/
    `workflow_failed` event and the process removing its own live record
    and writing its tombstone (the same race ``fleet/summary.py``
    documents). ``status="at-gate"`` is the query that surfaces every
    parked run (DD11's third bullet).

    Args:
        status: If given, only entries whose derived ``status`` equals
            this value (e.g. ``"at-gate"``, ``"running"``, ``"completed"``,
            ``"failed"``).
        workflow: If given, only entries whose ``workflow_name`` equals
            this value exactly.
        limit: Maximum number of entries to return, applied after
            filtering. Live entries are listed first (most actionable),
            then terminal entries (already newest-first, per
            :func:`~conductor.fleet.records.read_terminal_records`).

    Returns:
        A list of status-shaped dicts (a subset of :func:`_status_payload`'s
        fields -- ``run_id``, ``source``, ``status``, ``workflow_name``,
        ``started_at``, plus ``url`` for a live entry or ``ended_at`` for a
        terminal one).
    """
    from conductor.cli.app import _format_terminal_status

    live: dict[str, dict[str, Any]] = {}
    # scan_run_records() (read-only), not read_run_records() (prunes stale
    # entries as a side effect): this is a query, and a diagnostic listing
    # must not have the side effect of deleting a run record -- the exact
    # reasoning `cli/app.py::status` already applies to itself.
    for record in scan_run_records():
        if not record.run_id:
            # A legacy port-keyed `.pid` file has no run_id at all, so it
            # can never be looked up again by conductor_run_status/
            # conductor_cancel_run -- excluded rather than surfaced as an
            # entry no other tool in this toolset can act on.
            continue
        summary = derive_run_summary(record)
        live[record.run_id] = {
            "run_id": record.run_id,
            "source": "live",
            "status": summary.status,
            "workflow_name": summary.workflow_name,
            "started_at": summary.started_at,
            "url": _dashboard_url(summary.port),
        }

    terminal: list[dict[str, Any]] = []
    for record in read_terminal_records():
        if record.run_id in live:
            # Live wins: the narrow race window mentioned in the docstring
            # above, where the same run_id briefly exists in both sources.
            continue
        terminal.append(
            {
                "run_id": record.run_id,
                "source": "terminal",
                "status": _format_terminal_status(record.status),
                "workflow_name": record.workflow_name,
                "started_at": record.started_at,
                "ended_at": record.ended_at,
            }
        )

    combined = list(live.values()) + terminal

    if status is not None:
        combined = [entry for entry in combined if entry["status"] == status]
    if workflow is not None:
        combined = [entry for entry in combined if entry["workflow_name"] == workflow]

    return combined[:limit]
