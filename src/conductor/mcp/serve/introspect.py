"""``introspect`` toolset: event query, per-step detail, and plan tree
(FR8, DD3, E11-T2). Off by default -- see ``options.py::ALL_TOOLSETS`` and
``server.py``'s startup summary (E11-T1).

Every function here is a thin adapter over machinery the Fleet Manager
already built: :func:`conductor.mcp.serve.runs.read_event_log_events` for
:func:`conductor_run_events`, :func:`conductor.fleet.summary.derive_step_detail`
for :func:`conductor_node_detail`, and a parsed
:class:`conductor.config.schema.WorkflowConfig` for :func:`conductor_plan_tree`.
None of this module re-derives run status, activity, or topology -- see
``runs.py``'s own docstring for why re-implementation is the thing to avoid.

**R4.** ``derive_step_detail`` already discards a tool call's ``arguments``/
``result`` when it builds its activity stream (``fleet/summary.py:1066``,
``:1068`` build an ``ActivityLine`` from ``tool_name`` only), so
:func:`conductor_node_detail` satisfies R4's "withhold tool payloads by
default" posture by construction -- ``tests/test_mcp/test_serve_introspect.py``
asserts this directly (rather than assuming it) so a future change to
``ActivityLine`` cannot silently reopen the exposure. The payloads *do* still
live in the raw event records :func:`conductor.mcp.serve.runs.read_event_log_events`
returns (``agent_tool_start.arguments`` / ``agent_tool_complete.result``,
``providers/copilot.py:2066``, ``:2079``), so :func:`conductor_run_events` is
where the reduction is actually applied, gated by ``--introspect-full``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from conductor.config.loader import load_config
from conductor.fleet.records import RunRecord
from conductor.fleet.summary import derive_step_detail
from conductor.mcp.serve.runs import RunLookup, read_event_log_events, resolve_run

if TYPE_CHECKING:
    from conductor.config.schema import WorkflowConfig
    from conductor.mcp.serve.catalogue import Catalogue
    from conductor.mcp.serve.options import ServeOptions
    from conductor.registry.config import RegistriesConfig

# ---------------------------------------------------------------------------
# Shared: catalogue tool name -> on-disk workflow path (NFR3)
# ---------------------------------------------------------------------------


def resolve_catalogue_workflow_path(
    name: str,
    *,
    catalogue: Catalogue,
    options: ServeOptions,
    registries_config: RegistriesConfig | None = None,
) -> Path:
    """Resolve a **catalogue tool name** to its on-disk workflow file.

    Reuses the exact resolution ``mcp/serve/invoke.py`` performs for a real
    ``tools/call`` (``_entry_for_tool`` + ``_resolve_workflow_path``) rather
    than a second lookup path, so introspection always describes the same
    file a workflow invocation would actually launch -- fetched at the
    catalogue's pinned identity (DD6) for a registry workflow.

    ``name`` must be a name this server's catalogue actually publishes.
    NFR3 ("no tool accepts a filesystem path, URL, or registry source as a
    parameter") holds here for free: a path-shaped string is never a key in
    ``catalogue.reverse``, so it is rejected the same way an unknown name
    is -- there is no separate "looks like a path" check to bypass.

    Raises:
        UnknownToolError: If ``name`` is not a tool name this catalogue
            publishes (imported lazily from ``invoke.py`` to avoid a
            module-level cycle).
    """
    # Lazy + private-name import: `invoke.py` doesn't publish these as a
    # public resolution API of its own, but this module needs the exact
    # same lookup a real invocation uses, not a second copy of it -- the
    # same cross-module reuse `runs.py` already does for
    # `fleet/summary.py::_scan_events`.
    from conductor.mcp.serve.invoke import _entry_for_tool, _resolve_workflow_path

    entry = _entry_for_tool(catalogue, name)  # raises UnknownToolError
    registry, workflow = catalogue.reverse[name]

    if registries_config is None:
        from conductor.registry.config import load_config as load_registries_config

        registries_config = load_registries_config()

    return _resolve_workflow_path(
        registry,
        workflow,
        entry.pin,
        options=options,
        registries_config=registries_config,
        source=entry.source,
    )


# ---------------------------------------------------------------------------
# E11-T2: conductor_run_events -- R4's reduction lives here
# ---------------------------------------------------------------------------

DEFAULT_EVENTS_LIMIT = 200
"""Default cap on how many events :func:`conductor_run_events` returns.
Matches ``derive_step_detail``'s own bounded-drill-down posture -- this is a
query tool, not a log viewer."""

MAX_EVENTS_LIMIT = 1000
"""Hard ceiling on ``limit``, regardless of what the caller requests --
mirrors ``invoke.py::resolve_wait_seconds``'s "requested value is capped,
never trusted outright" shape."""

_TOOL_PAYLOAD_FIELDS = {
    "agent_tool_start": "arguments",
    "agent_tool_complete": "result",
}
"""Which field of which event type R4 reduces. Matches
``providers/copilot.py:2066``/``:2079`` exactly -- these are the only two
event types that ever carry a raw tool-call payload."""


def _byte_size(value: Any) -> int:
    """Size, in UTF-8 bytes, of ``value`` serialized the same way the event
    log itself would encode it -- what R4's reduction reports so the
    caller "learns the size it is not being shown" even when it cannot see
    the content.

    Matches ``engine/event_log.py``'s own ``json.dumps(..., separators=(",", ":"))``
    exactly (default ``ensure_ascii=True``, no added whitespace) -- the
    previous ``ensure_ascii=False`` plus default separators reported a
    different number of bytes than what the log file itself actually
    contains for any structured or non-ASCII payload.
    """
    try:
        serialized = json.dumps(value, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = str(value)
    return len(serialized.encode("utf-8"))


MAX_FIELD_BYTES = 50_000
"""Ceiling on a single event's (or a node detail's ``prompt``/``output``
field's) own serialized size (NFR6). Bounding a result by item *count*
(``limit``) does not bound its size when one event, prompt, or output can
itself be arbitrarily large -- this applies independently of ``limit`` and
independently of R4's tool-payload reduction. Matches ``invoke.py``'s own
``_MAX_INLINE_RESULT_BYTES`` -- the analogous per-field bound for a query
tool rather than a completed run's own result."""


def _bound_size(value: Any, *, max_bytes: int = MAX_FIELD_BYTES) -> Any:
    """Replace ``value`` with a bounded placeholder when its own serialized
    size exceeds ``max_bytes`` (NFR6); returns ``value`` unchanged otherwise."""
    size = _byte_size(value)
    if size <= max_bytes:
        return value
    return {
        "truncated": True,
        "byte_size": size,
        "note": f"Exceeded {max_bytes} bytes and was withheld (NFR6).",
    }


def _bound_event(evt: dict[str, Any], *, max_bytes: int = MAX_FIELD_BYTES) -> dict[str, Any]:
    """Cap one event's own serialized size (NFR6), preserving ``type`` and
    ``timestamp`` so a truncated entry is still identifiable. R4's
    reduction of a tool-call payload can still leave a huge
    ``agent_message`` or prompt-render event behind; bounding the result
    by ``limit`` alone does not catch that."""
    size = _byte_size(evt)
    if size <= max_bytes:
        return evt
    return {
        "type": evt.get("type"),
        "timestamp": evt.get("timestamp"),
        "truncated": True,
        "byte_size": size,
        "note": f"Event exceeded {max_bytes} bytes and was withheld (NFR6).",
    }


def _reduce_tool_payload_field(data: dict[str, Any], field: str, *, full: bool) -> dict[str, Any]:
    """Return a copy of ``data`` with ``field`` reduced per R4.

    The original value's byte size is always reported (as a sibling
    ``"<field>_byte_size"`` key), in *both* modes -- restoring the payload
    under ``--introspect-full`` does not remove the size annotation,
    since a caller comparing runs benefits from it regardless of whether
    the content itself is visible. Only the field's own value differs:
    the original, verbatim, when ``full`` is set; otherwise a
    ``{"name", "status", "byte_size"}`` placeholder that never carries the
    original content.
    """
    if field not in data:
        return data

    original = data[field]
    size = _byte_size(original)
    new_data = dict(data)
    new_data[f"{field}_byte_size"] = size
    if not full:
        new_data[field] = {
            "name": data.get("tool_name") or field,
            "status": "redacted",
            "byte_size": size,
        }
    return new_data


def _reduce_event(evt: dict[str, Any], *, full: bool) -> dict[str, Any]:
    """Apply R4's reduction to one parsed event, never mutating the
    original (``read_event_log_events``'s result may be read again by
    another caller)."""
    data = evt.get("data")
    if not isinstance(data, dict):
        return dict(evt)

    field = _TOOL_PAYLOAD_FIELDS.get(evt.get("type"))
    if field is None:
        return dict(evt)

    new_event = dict(evt)
    new_event["data"] = _reduce_tool_payload_field(data, field, full=full)
    return new_event


def _event_log_path_for(lookup: RunLookup) -> Path | None:
    """The event log path to read for ``lookup``, across all three
    resolvable sources; ``None`` when there is nothing to read (no log
    path recorded, or the run is entirely unknown)."""
    if lookup.source == "live":
        assert lookup.record is not None
        return Path(lookup.record.event_log_path) if lookup.record.event_log_path else None
    if lookup.source == "terminal":
        assert lookup.terminal is not None
        return Path(lookup.terminal.event_log_path) if lookup.terminal.event_log_path else None
    if lookup.source == "event_log":
        return lookup.event_log_path
    return None


def conductor_run_events(
    run_id: str,
    *,
    event_types: tuple[str, ...] | None = None,
    limit: int = DEFAULT_EVENTS_LIMIT,
    introspect_full: bool = False,
) -> dict[str, Any]:
    """``conductor_run_events(run_id, event_types?, limit=200)`` (E11-T2, FR8).

    Reads ``run_id``'s event log (resolved through the same three-source
    ladder ``runs.py::resolve_run`` uses, so a live, cleanly-finished, or
    crashed run are all queryable) and returns its parsed events, oldest
    first, optionally filtered to ``event_types`` and always bounded by
    ``limit`` (itself capped at :data:`MAX_EVENTS_LIMIT`).

    **R4:** a tool-call event's payload (``agent_tool_start.arguments`` /
    ``agent_tool_complete.result``) is replaced with a
    ``{"name", "status": "redacted", "byte_size"}`` placeholder unless
    ``introspect_full`` is set (the server's ``--introspect-full`` flag).
    ``byte_size`` (as ``"<field>_byte_size"``) is reported regardless of
    ``introspect_full``, so the caller always learns the size of what it
    is -- or is not -- being shown.

    Args:
        run_id: The run identifier to query.
        event_types: If given, only events whose ``type`` is in this set.
        limit: Maximum number of (post-filter) events to return, capped at
            :data:`MAX_EVENTS_LIMIT` regardless of what is requested.
        introspect_full: Restore full tool-call payloads (the server's
            ``--introspect-full`` startup flag).

    Returns:
        ``{"run_id", "source", "events", "returned", "total", "truncated"}``,
        or the same shape with an ``"error"`` key and empty ``"events"``
        when ``run_id`` has no resolvable event log at all.
    """
    lookup = resolve_run(run_id)
    path = _event_log_path_for(lookup)
    if path is None:
        return {
            "run_id": run_id,
            "source": lookup.source,
            "events": [],
            "returned": 0,
            "total": 0,
            "truncated": False,
            "error": f"No event log is known for run_id {run_id!r}.",
        }

    events = read_event_log_events(path)
    if event_types is not None:
        wanted = set(event_types)
        events = [evt for evt in events if evt.get("type") in wanted]

    total = len(events)
    bounded_limit = max(0, min(limit, MAX_EVENTS_LIMIT))
    selected = events[:bounded_limit]

    return {
        "run_id": run_id,
        "source": lookup.source,
        "events": [_bound_event(_reduce_event(evt, full=introspect_full)) for evt in selected],
        "returned": len(selected),
        "total": total,
        "truncated": total > len(selected),
    }


# ---------------------------------------------------------------------------
# E11-T2: conductor_node_detail
# ---------------------------------------------------------------------------


def _record_for_step_detail(lookup: RunLookup, run_id: str) -> RunRecord | None:
    """Build (or reuse) the :class:`RunRecord` ``derive_step_detail`` needs,
    across all three resolvable sources.

    ``derive_step_detail`` only ever reads ``record.event_log_path`` and
    ``record.workflow_name`` (as the fallback when the log has no declared
    name) -- every other field is a placeholder for the ``terminal`` and
    ``event_log`` sources, which have no live process, port, or checkpoint
    directory to report.
    """
    if lookup.source == "live":
        return lookup.record
    if lookup.source == "terminal":
        terminal = lookup.terminal
        assert terminal is not None
        return RunRecord(
            run_id=terminal.run_id or run_id,
            pid=0,
            workflow_path=terminal.workflow_path,
            workflow_name=terminal.workflow_name,
            started_at=terminal.started_at,
            event_log_path=terminal.event_log_path,
            port=None,
            mode="bg",
            checkpoint_dir=None,
        )
    if lookup.source == "event_log":
        assert lookup.event_log_path is not None
        return RunRecord(
            run_id=run_id,
            pid=0,
            workflow_path="",
            workflow_name="",
            started_at="",
            event_log_path=str(lookup.event_log_path),
            port=None,
            mode="bg",
            checkpoint_dir=None,
        )
    return None


def conductor_node_detail(run_id: str, agent: str) -> dict[str, Any]:
    """``conductor_node_detail(run_id, agent)`` (E11-T2, FR8).

    One step's prompt, output and activity stream -- what
    ``derive_step_detail`` already derives for the Fleet TUI's step
    drill-down, over any of the three resolvable run sources.

    Returns prompt and output **in full** (R4 -- these are Conductor's own
    structured fields, not a third-party payload DD12 governs). No tool
    call's ``arguments``/``result`` are ever present here, regardless of
    ``--introspect-full``: ``derive_step_detail`` never reads them in the
    first place (see the module docstring) -- there is no reduction
    applied here because there is nothing to reduce.

    Args:
        run_id: The run identifier to query.
        agent: The step (agent, script, parallel-group member, ...) name.

    Returns:
        ``{"run_id", "agent_name", "status", "prompt", "output",
        "activity", "workflow_name"}``, or an ``"error"``-carrying dict
        when ``run_id`` is not resolvable at all.
    """
    lookup = resolve_run(run_id)
    record = _record_for_step_detail(lookup, run_id)
    if record is None:
        return {
            "run_id": run_id,
            "agent_name": agent,
            "status": "unknown",
            "error": f"No run found for run_id {run_id!r}.",
        }

    detail = derive_step_detail(record, agent)
    return {
        "run_id": run_id,
        "agent_name": detail.agent_name,
        "status": detail.status,
        "prompt": _bound_size(detail.prompt),
        "output": _bound_size(detail.output),
        "activity": [{"kind": line.kind, "text": line.text} for line in detail.activity],
        "workflow_name": detail.workflow_name,
    }


# ---------------------------------------------------------------------------
# E11-T2: conductor_plan_tree
# ---------------------------------------------------------------------------


def _route_dicts(routes: Any) -> list[dict[str, Any]]:
    return [{"to": route.to, "when": route.when} for route in routes]


def _build_plan_tree(config: WorkflowConfig) -> dict[str, Any]:
    """Flatten a parsed :class:`WorkflowConfig` into the plan tree
    :func:`conductor_plan_tree` returns: every agent/parallel-group/
    for-each node, its type, and its routes, plus the declared entry
    point -- everything ``conductor validate`` and the Fleet TUI's own
    topology view already derive from the same model.
    """
    nodes: list[dict[str, Any]] = []
    for agent in config.agents:
        nodes.append(
            {
                "name": agent.name,
                "type": agent.type or "agent",
                "routes": _route_dicts(agent.routes),
            }
        )
    for group in config.parallel:
        nodes.append(
            {
                "name": group.name,
                "type": "parallel",
                "agents": list(group.agents),
                "routes": _route_dicts(group.routes),
            }
        )
    for group in config.for_each:
        nodes.append(
            {
                "name": group.name,
                "type": "for_each",
                "agent": group.agent.name,
                "routes": _route_dicts(group.routes),
            }
        )
    return {
        "workflow_name": config.workflow.name,
        "entry_point": config.workflow.entry_point,
        "nodes": nodes,
    }


def conductor_plan_tree(
    name: str,
    *,
    catalogue: Catalogue,
    options: ServeOptions,
    registries_config: RegistriesConfig | None = None,
) -> dict[str, Any]:
    """``conductor_plan_tree(name)`` (E11-T2, FR8).

    The parsed structure of a published workflow: its declared entry
    point plus every agent, parallel group and for-each group with its
    routes -- built from the same :class:`WorkflowConfig` ``conductor
    validate`` parses, not a second representation.

    ``name`` is a **catalogue tool name**, never a path (NFR3) -- resolved
    via :func:`resolve_catalogue_workflow_path`, the same lookup a real
    invocation of that tool would use.

    Args:
        name: The catalogue tool name to describe.
        catalogue: The frozen catalogue built at startup.
        options: The frozen startup options.
        registries_config: The configured registries; defaults to
            ``registry.config.load_config()``.

    Returns:
        ``{"workflow_name", "entry_point", "nodes"}``.

    Raises:
        UnknownToolError: If ``name`` is not a tool name this catalogue
            publishes.
    """
    path = resolve_catalogue_workflow_path(
        name, catalogue=catalogue, options=options, registries_config=registries_config
    )
    config = load_config(path)
    return _build_plan_tree(config)
