"""Invocation layer: launch, bounded wait, and result shaping (FR4, FR5, G3,
G4, DD2, R3, E9).

The single most important property of this module (design's *Key
Components -> 3*): **a workflow tool call never executes a workflow inside
this server process.** ``invoke_workflow_tool`` always calls
:func:`conductor.cli.bg_runner.launch_background`, exactly the same
detached-spawn primitive ``conductor run --web-bg`` and the Fleet Manager's
New Run screen use (``fleet/launch.py``). The reserved ``_wait_seconds``
parameter (FR5) changes only whether *this call* waits for the detached
child before returning -- the run itself is detached either way (DD2), so a
dashboard URL is available unconditionally (G4) and every MCP-launched run
is gate-resolvable by construction.

Flow, in order (data flows A/B in the design doc):

1. Map the tool name back to ``(registry, workflow)`` through the
   catalogue's reverse map (E9-T1); reject an unknown name outright.
2. Enforce ``--max-concurrent-runs`` (R3, E9-T7) *before* anything is
   forked.
3. Resolve the workflow's on-disk path at its catalogue-pinned identity
   (DD6) and its declared ``input:``/``mcp:`` blocks, reusing the same
   registry primitives ``catalogue.py`` used to build the catalogue rather
   than a second resolution path.
4. Validate/fill typed inputs via
   :func:`conductor.fleet.launch.build_typed_launch_inputs` (E9-T2).
5. Launch, unconditionally detached, with ``skip_gates=False`` (DD11) and
   an MCP-launched metadata stamp (E9-T3).
6. Resolve ``_wait_seconds`` (FR5, E9-T4) and either return the run handle
   immediately, or poll -- emitting ``notifications/progress`` when a
   token was supplied (E9-T5) -- until a terminal status, ``at-gate``, or
   the bounded deadline (E9-T4/T5).
7. Shape the result: the rendered ``output:`` dict as structured content on
   completion, bounded in size (NFR6, E9-T6); a run handle in every other
   case.

Argument validation against a tool's ``inputSchema`` is the SDK's own job
(``@server.call_tool(validate_input=True)``, the default) -- this module
does not duplicate it.
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.types import ResourceLink, TextContent
from pydantic import AnyUrl

from conductor.cli.bg_runner import BackgroundLaunch, launch_background
from conductor.cli.pid import is_process_alive
from conductor.config.loader import load_config
from conductor.exceptions import ConductorError
from conductor.fleet.launch import build_typed_launch_inputs
from conductor.fleet.records import TerminalRunRecord, read_run_record, read_terminal_record
from conductor.fleet.summary import RunSummary, derive_run_summary
from conductor.mcp.serve.catalogue import Catalogue, CatalogueEntry
from conductor.mcp.serve.options import ServeOptions
from conductor.mcp.serve.pinning import Pin
from conductor.mcp.serve.toolgen import WAIT_SECONDS_PARAM
from conductor.registry.cache import fetch_workflow
from conductor.registry.config import RegistriesConfig
from conductor.registry.config import load_config as load_registries_config

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UnknownToolError(ConductorError):
    """Raised when ``call_tool`` names a tool this catalogue does not publish (E9-T1)."""


class ConcurrentRunLimitError(ConductorError):
    """Raised when launching a run would exceed ``--max-concurrent-runs`` (R3)."""


# ---------------------------------------------------------------------------
# R3: the in-process concurrent-run tracker
# ---------------------------------------------------------------------------


@dataclass
class LaunchTracker:
    """In-process record of ``run_id``s this server has launched, for R3's
    ``--max-concurrent-runs`` cap.

    Deliberately in-process only -- nothing here is persisted to disk, so a
    restarted server starts this count at zero. That is a consequence of
    the design's "the MCP server owns no execution state" principle, not a
    lapse from it: restarting the process already forgets everything else
    this server tracked (the catalogue, any open wait loops), and R3 bounds
    what *one running process* can accumulate, not a durable global ledger
    of every MCP-launched run that ever existed.

    This is also *why* the tracker exists at all rather than counting from
    ``fleet/records.py`` directly: ``RunRecord`` deliberately carries no
    metadata field (nine fields, no more), so the E9-T3 metadata stamp
    reaches the event log but never the run record -- counting by that
    stamp would cost a bounded event-log head read per live run on every
    single launch. And counting *every* live ``RunRecord`` regardless of
    origin would charge an unrelated ``conductor run`` a human started by
    hand, in the same ``$CONDUCTOR_HOME``, against this server's cap. An
    in-process set of the ``run_id``s this process itself minted avoids
    both costs.
    """

    launched_run_ids: set[str] = field(default_factory=set)

    def register(self, run_id: str) -> None:
        """Record that this process just launched ``run_id``."""
        self.launched_run_ids.add(run_id)

    def live_count(self) -> int:
        """Count tracked ``run_id``s that are still live.

        A tracked ``run_id`` is "live" when its :class:`RunRecord` still
        exists *and* its process passes :func:`is_process_alive` -- the
        same liveness test ``fleet/records.py::read_run_records`` applies
        to every record it surfaces. A ``run_id`` that no longer satisfies
        that (the run finished, crashed, or its record was pruned) is
        dropped from the tracked set as a side effect of this call, so a
        completed run frees its slot rather than counting against the cap
        for the rest of the server's lifetime.
        """
        alive: set[str] = set()
        for run_id in self.launched_run_ids:
            record = read_run_record(run_id)
            if record is not None and is_process_alive(record.pid):
                alive.add(run_id)
        self.launched_run_ids = alive
        return len(alive)


def _enforce_concurrency_cap(tracker: LaunchTracker, options: ServeOptions) -> None:
    """Reject a launch once the tracked live count is at the configured cap (R3).

    ``0`` (the default) is unbounded, so this is a no-op unless an operator
    opted in. Never queues the request -- the design bounds concurrency at
    startup rather than adding a runtime scheduler, so a caller at the cap
    is pointed at what is already running instead of being made to wait.

    Raises:
        ConcurrentRunLimitError: If the cap is set and already reached.
    """
    if options.max_concurrent_runs <= 0:
        return
    if tracker.live_count() >= options.max_concurrent_runs:
        raise ConcurrentRunLimitError(
            f"This server is already running its --max-concurrent-runs="
            f"{options.max_concurrent_runs} limit of MCP-launched workflow(s). "
            "Call conductor_list_runs to see what is running, and "
            "conductor_cancel_run to free a slot before retrying."
        )


# ---------------------------------------------------------------------------
# _wait_seconds resolution (FR5, E9-T4)
# ---------------------------------------------------------------------------


def resolve_wait_seconds(requested: float | None, *, mcp_mode: str, max_wait_seconds: int) -> float:
    """Resolve the effective bounded-wait duration for one invocation (FR5).

    - ``requested == 0`` (or negative) -- return immediately: ``0.0``.
    - ``requested > 0`` -- wait up to that many seconds, capped at
      ``max_wait_seconds`` regardless of what the caller asked for. The
      ceiling applies to *every* blocking path (E9-T4).
    - ``requested is None`` (the parameter was omitted) -- defer to the
      workflow's declared ``mcp.mode``: ``"sync"`` resolves to the same
      ``max_wait_seconds`` ceiling rather than an unbounded wait; the
      design's FR5 text names only this case as blocking. ``"async"``
      returns immediately. ``"auto"`` is not otherwise defined by the
      design and is treated the same as ``"async"`` here -- the safer,
      non-blocking default -- since nothing at invocation time (no
      knowledge of what else the caller is doing) could make a dynamic
      per-call choice trustworthy.

    Args:
        requested: The caller-supplied ``_wait_seconds`` value, or ``None``
            if the parameter was omitted.
        mcp_mode: The workflow's declared ``mcp.mode`` (``"async"`` /
            ``"sync"`` / ``"auto"``).
        max_wait_seconds: The server's ``--max-wait-seconds`` ceiling.

    Returns:
        The number of seconds this call should block for, ``0.0`` meaning
        "return immediately".
    """
    if requested is not None:
        if requested <= 0:
            return 0.0
        return min(float(requested), float(max_wait_seconds))
    if mcp_mode == "sync":
        return float(max_wait_seconds)
    return 0.0


# ---------------------------------------------------------------------------
# Workflow-path resolution
# ---------------------------------------------------------------------------

_WORKFLOW_DIR_PREFIX = "dir:"
_WORKFLOW_DIR_EXTENSIONS = (".yaml", ".yml")


def _resolve_workflow_dir_path(
    registry: str, workflow: str, options: ServeOptions, *, source: str = ""
) -> Path:
    """Resolve a ``--workflow-dir`` candidate's on-disk path.

    ``source`` -- the exact file path recorded on the entry's
    :class:`~conductor.mcp.serve.catalogue.CatalogueEntry` at catalogue-build
    time -- is tried first and, when it still exists, returned directly.
    This is what makes the resolution unambiguous: ``registry``/``workflow``
    alone (a ``"dir:<directory name>"`` label plus a filename stem) can be
    shared by two distinct ``--workflow-dir`` directories with the same
    basename and a same-named file in each, and a directory-name rescan
    below has no way to tell them apart -- it would always resolve to
    whichever configured directory happens to come first, regardless of
    which one the published tool name actually names.

    Falls back to the rescan (mirroring ``catalogue.py``'s own
    ``"dir:<directory name>"`` labeling convention (DD10) in reverse: find
    the configured directory whose name matches, then the workflow file
    directly under it) only when ``source`` is absent -- e.g. a legacy
    entry built before this field existed. When ``source`` is present but
    no longer on disk, the rescan is refused outright rather than
    attempted: two distinct ``--workflow-dir`` directories can share a
    basename and a same-named file, so a rescan in that case would
    silently resolve the tool to the *other* directory's workflow instead
    of reporting that the one it actually names is gone.

    Raises:
        UnknownToolError: If ``source`` is present but no longer on disk,
            or (when ``source`` is absent) no configured
            ``--workflow-dir``/file matches any more -- e.g. it was moved
            or removed since the catalogue was built at startup.
    """
    if source:
        pinned = Path(source)
        if pinned.is_file():
            return pinned
        raise UnknownToolError(
            f"Workflow {workflow!r} under {registry!r} was resolved to {source!r} at "
            "startup, but that file no longer exists; refusing to guess which other "
            "directory it might mean."
        )

    dir_name = registry[len(_WORKFLOW_DIR_PREFIX) :]
    for directory in options.workflow_dirs:
        if directory.name != dir_name:
            continue
        for ext in _WORKFLOW_DIR_EXTENSIONS:
            candidate = directory / f"{workflow}{ext}"
            if candidate.is_file():
                return candidate
    raise UnknownToolError(
        f"Could not resolve --workflow-dir workflow {workflow!r} under {registry!r}; "
        "the file may have been moved or removed since the catalogue was built at startup."
    )


def _resolve_workflow_path(
    registry: str,
    workflow: str,
    pin: Pin,
    *,
    options: ServeOptions,
    registries_config: RegistriesConfig,
    source: str = "",
) -> Path:
    """Resolve ``(registry, workflow)`` to a local workflow file.

    Reuses the same registry primitive (:func:`conductor.registry.cache.fetch_workflow`)
    ``catalogue.py`` used to build the catalogue, rather than a second
    resolution path -- it already handles both GitHub and path registries.
    A GitHub-registry workflow is fetched at its catalogue-pinned commit
    SHA (DD6), so the content actually launched is guaranteed to match the
    content whose schema was scanned and published at startup.

    ``source`` -- the entry's own recorded file path -- disambiguates a
    ``--workflow-dir`` candidate the same ``(registry, workflow)`` pair
    alone cannot (see :func:`_resolve_workflow_dir_path`); it has no effect
    on a registry-index workflow, which is already uniquely resolved by
    ``registry`` + ``workflow`` + the pinned ref.

    Raises:
        UnknownToolError: If ``registry`` is no longer configured (e.g. a
            ``--registry`` change since startup would require a restart to
            take effect, per DD3's "fixed at startup").
        RegistryError: On a fetch failure (network, missing workflow, etc.).
    """
    if registry.startswith(_WORKFLOW_DIR_PREFIX):
        return _resolve_workflow_dir_path(registry, workflow, options, source=source)

    entry = registries_config.registries.get(registry)
    if entry is None:
        raise UnknownToolError(
            f"Registry {registry!r} (for workflow {workflow!r}) is no longer configured; "
            "restart the server to rebuild the catalogue against the current configuration."
        )
    ref = pin.value if pin.kind == "sha" else None
    return fetch_workflow(registry, entry, workflow, ref=ref, allow_network=True)


def _entry_for_tool(catalogue: Catalogue, tool_name: str) -> CatalogueEntry:
    """Find the :class:`CatalogueEntry` for an already-validated tool name."""
    for entry in catalogue.entries:
        if entry.tool_name == tool_name:
            return entry
    raise UnknownToolError(f"Unknown tool: {tool_name!r}")


# ---------------------------------------------------------------------------
# DD11 / metadata stamp
# ---------------------------------------------------------------------------

# DD11 — never True. A human gate is a control the workflow author
# deliberately placed in the path; the server always parks at it and
# returns an approval URL rather than auto-selecting an option on the
# caller's behalf. This is a one-parameter regression away from silently
# rubber-stamping every gate, which is why it is a named constant with an
# assertion below rather than a bare literal at the call site.
_NEVER_SKIP_GATES = False


def _mcp_launch_metadata(tool_name: str) -> dict[str, str]:
    """CLI metadata stamped on every MCP-launched run (E9-T3).

    Forwarded by ``launch_background`` as ``--metadata`` and included
    verbatim in the engine's ``workflow_started`` event, so a human reading
    the dashboard or event log can tell the run was launched by
    ``conductor mcp serve`` and which tool triggered it.
    """
    return {"conductor_mcp_server": "true", "conductor_mcp_tool": tool_name}


# ---------------------------------------------------------------------------
# Bounded wait / polling (E9-T4, E9-T5)
# ---------------------------------------------------------------------------

_POLL_INTERVAL_SECONDS = 2.0
"""Cadence of status polls / progress notifications during a bounded wait.
Matches the Fleet Manager Runs screen's own ~2s poll cadence
(``fleet/summary.py``'s module docstring) -- there is no reason this
server's own polling needs to be finer-grained than the UI that already
polls the same event log."""

_ProgressSender = Callable[[str | int, float, float | None, str | None], Awaitable[None]]


async def _await_terminal_or_gate(
    run_id: str,
    *,
    deadline: float,
    progress_token: str | int | None,
    send_progress: _ProgressSender | None,
) -> tuple[RunSummary | None, TerminalRunRecord | None]:
    """Poll ``run_id`` until a terminal state or ``at-gate`` is derived, or
    ``deadline`` (a ``time.monotonic()`` timestamp) passes (E9-T4).

    Emits progress via ``send_progress`` once per poll iteration when both
    it and ``progress_token`` were supplied (E9-T5); silently skipped
    otherwise -- DD2's caveat that this liveness channel only works when
    the caller opted into a ``progressToken``.

    Returns ``(summary, terminal)``. At most one is non-``None``: a live
    run yields ``summary`` (whose ``status`` may be ``"running"``,
    ``"at-gate"``, or -- the narrow race ``fleet/summary.py`` documents --
    already ``"completed"``/``"failed"`` before the process removed its own
    record; a run whose live record is already gone yields ``terminal``
    when the tombstone has been written. Both are ``None`` only in the
    narrow startup race where neither has been written yet; the caller
    should treat that the same as "still running".
    """
    summary: RunSummary | None = None
    progress = 0.0
    while True:
        record = read_run_record(run_id)
        if record is not None:
            summary = derive_run_summary(record)
            if summary.status in ("completed", "failed", "at-gate"):
                return summary, None
        else:
            terminal = read_terminal_record(run_id)
            if terminal is not None:
                return None, terminal

        now = time.monotonic()
        if now >= deadline:
            return summary, None

        if progress_token is not None and send_progress is not None:
            progress += 1.0
            status_label = summary.status if summary is not None else "starting"
            await send_progress(
                progress_token, progress, None, f"Waiting for run {run_id} ({status_label})..."
            )

        remaining = max(deadline - now, 0.0)
        await _sleep(min(_POLL_INTERVAL_SECONDS, remaining))


async def _sleep(seconds: float) -> None:
    """Thin wrapper around ``asyncio.sleep`` so tests can patch this one
    name (``conductor.mcp.serve.invoke._sleep``) instead of the global
    ``asyncio.sleep``, which every other concurrently-running test would
    also observe."""
    import asyncio

    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# Result shaping (E9-T3, E9-T6)
# ---------------------------------------------------------------------------


def _run_handle(
    *,
    run_id: str,
    url: str,
    entry: CatalogueEntry,
    started_at: str,
    status: str,
    next_action: str,
) -> dict[str, Any]:
    """Build the run handle every invocation returns (API Contracts:
    "Run handle (every invocation, every mode)"). A dashboard URL is always
    present (G4)."""
    return {
        "run_id": run_id,
        "status": status,
        "url": url,
        "workflow": {
            "name": entry.workflow,
            "registry": entry.registry,
            "pinned": entry.pin.as_str(),
        },
        "started_at": started_at,
        "next": next_action,
    }


_MAX_INLINE_RESULT_BYTES = 50_000
"""Bound on a completed run's serialized ``output:`` dict before it spills
to a file and is returned as a ``resource_link`` instead of inline JSON
(NFR6). Matches ``runtime.tool_output``'s own ``max_chars`` default
(``config/schema.py::ToolOutputConfig``), which bounds an individual MCP
*tool-call* result the same way -- this is the analogous bound for a
workflow's own rendered result."""


def _spill_output_dir() -> Path:
    """Where an oversized result payload is spilled (NFR6): a
    ``conductor/mcp-output/`` subdirectory of the OS temp directory,
    mirroring ``runtime.tool_output``'s own unconfigured default
    (``/conductor/tool-output``, ``config/schema.py::ToolOutputConfig.spill_dir``)."""
    spill_dir = Path(tempfile.gettempdir()) / "conductor" / "mcp-output"
    spill_dir.mkdir(parents=True, exist_ok=True)
    return spill_dir


def _shape_output_result(
    run_id: str, output: dict[str, Any]
) -> tuple[list[TextContent | ResourceLink], dict[str, Any]]:
    """Shape a finished run's rendered ``output:`` dict into MCP result
    content (E9-T6): the dict as structured content plus a human-readable
    text block, bounded per NFR6. No ``outputSchema`` is ever published for
    a workflow tool (DD5), so there is nothing to validate this shape
    against.

    A payload whose serialized size exceeds :data:`_MAX_INLINE_RESULT_BYTES`
    is written to a file instead and returned as a ``resource_link`` plus a
    bounded note -- never embedded inline -- following the same
    metadata-not-bytes principle DD12's log tool established for a
    different payload (a third-party log file rather than the workflow's
    own output), even though the mechanism here is necessarily different
    since there is no pre-existing file to link to.
    """
    serialized = json.dumps(output, indent=2, default=str)
    if len(serialized.encode("utf-8")) <= _MAX_INLINE_RESULT_BYTES:
        text = TextContent(type="text", text=f"Run {run_id} completed.\n\n{serialized}")
        return [text], dict(output)

    path = _spill_output_dir() / f"{run_id}.json"
    path.write_text(serialized, encoding="utf-8")
    size = path.stat().st_size
    link = ResourceLink(
        type="resource_link",
        uri=AnyUrl(path.resolve().as_uri()),
        name=f"{run_id}-output",
        mimeType="application/json",
        size=size,
    )
    text = TextContent(
        type="text",
        text=f"Run {run_id} completed; its output is {size} bytes and was written to {path}.",
    )
    structured = {
        "run_id": run_id,
        "status": "completed",
        "note": (
            f"Output exceeded {_MAX_INLINE_RESULT_BYTES} bytes and was written to the "
            "linked file rather than returned inline (NFR6)."
        ),
    }
    return [text, link], structured


def _immediate_result(
    launch: BackgroundLaunch, entry: CatalogueEntry, started_at: str
) -> tuple[list[TextContent | ResourceLink], dict[str, Any]]:
    """Data flow A: shape the result for an immediate (``_wait_seconds: 0``
    or omitted-async) return. The run is already detached and running
    regardless of this choice (DD2) -- this only decides whether the tool
    call itself waits for it."""
    handle = _run_handle(
        run_id=launch.run_id,
        url=launch.url,
        entry=entry,
        started_at=started_at,
        status="running",
        next_action=(
            f"Call conductor_await_run(run_id={launch.run_id!r}) to wait for a terminal "
            f"state, or open {launch.url} to watch it live."
        ),
    )
    if not launch.workflow_started:
        # issue #410 / cli/app.py's own treatment: the launch gate's deadline
        # passed before the child reported `workflow_started`. Not a
        # failure -- surface it as a note (E9-T3).
        handle["note"] = (
            "The workflow has not reported starting yet. It may still be initializing "
            "(plugin fetch, MCP server startup, provider connection) -- check the "
            "dashboard URL above."
        )
    text = TextContent(
        type="text",
        text=f"Started run {launch.run_id} for {entry.tool_name!r}. Dashboard: {launch.url}",
    )
    return [text], handle


def _final_result(
    launch: BackgroundLaunch,
    entry: CatalogueEntry,
    started_at: str,
    summary: RunSummary | None,
    terminal: TerminalRunRecord | None,
) -> tuple[list[TextContent | ResourceLink], dict[str, Any]]:
    """Shape the result once a bounded wait has ended: on a terminal
    outcome, an ``at-gate`` park, or the deadline (E9-T4/T5/T6)."""
    if terminal is not None:
        if terminal.status == "success":
            return _shape_output_result(launch.run_id, terminal.output)
        handle = _run_handle(
            run_id=launch.run_id,
            url=launch.url,
            entry=entry,
            started_at=started_at,
            status="failed",
            next_action=(
                f"Run failed. Call conductor_run_status(run_id={launch.run_id!r}) for detail."
            ),
        )
        handle["error"] = {"type": terminal.error_type, "message": terminal.error_message}
        text = TextContent(
            type="text",
            text=(
                f"Run {launch.run_id} failed: "
                f"{terminal.error_message or terminal.error_type or 'unknown error'}"
            ),
        )
        return [text], handle

    if summary is not None and summary.status == "at-gate":
        handle = _run_handle(
            run_id=launch.run_id,
            url=launch.url,
            entry=entry,
            started_at=started_at,
            status="at-gate",
            next_action=(
                f"Run is waiting at a human gate. Respond at {launch.url}, or call "
                f"conductor_await_run(run_id={launch.run_id!r}) again once it is resolved."
            ),
        )
        if summary.gate is not None:
            handle["gate"] = {
                "agent_name": summary.gate.agent_name,
                "prompt": summary.gate.prompt,
                "options": summary.gate.options,
                "option_details": summary.gate.option_details,
            }
        text = TextContent(
            type="text",
            text=f"Run {launch.run_id} is waiting at a human gate. Respond at {launch.url}.",
        )
        return [text], handle

    if summary is not None and summary.status in ("completed", "failed"):
        # The narrow race `fleet/summary.py` documents: the event log
        # already shows terminal but the process hasn't removed its own
        # live record (and written the terminal record) yet. Report the
        # status now rather than waiting out the rest of the deadline for
        # a tombstone that is only moments away.
        handle = _run_handle(
            run_id=launch.run_id,
            url=launch.url,
            entry=entry,
            started_at=started_at,
            status=summary.status,
            next_action=(f"Call conductor_run_status(run_id={launch.run_id!r}) for full detail."),
        )
        text = TextContent(
            type="text",
            text=f"Run {launch.run_id} reported {summary.status}; awaiting its final record.",
        )
        return [text], handle

    # Deadline reached with the run neither terminal nor at-gate (E9-T4).
    handle = _run_handle(
        run_id=launch.run_id,
        url=launch.url,
        entry=entry,
        started_at=started_at,
        status=summary.status if summary is not None else "running",
        next_action=(
            f"Still running after the bounded wait. Call conductor_await_run(run_id="
            f"{launch.run_id!r}) again, or open {launch.url}."
        ),
    )
    if summary is not None:
        handle["current_step"] = summary.current_step
    text = TextContent(
        type="text", text=f"Run {launch.run_id} is still running; the wait deadline was reached."
    )
    return [text], handle


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def invoke_workflow_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    catalogue: Catalogue,
    options: ServeOptions,
    tracker: LaunchTracker,
    registries_config: RegistriesConfig | None = None,
    progress_token: str | int | None = None,
    send_progress: _ProgressSender | None = None,
) -> tuple[list[TextContent | ResourceLink], dict[str, Any]]:
    """Handle one ``tools/call`` for a generated workflow tool (E9).

    Returns a ``(content, structuredContent)`` pair -- the exact shape the
    pinned SDK's ``@server.call_tool()`` decorator accepts as
    ``CombinationContent`` -- so a future caller wiring this onto the live
    ``Server`` (e.g. a later epic's toolset dispatch) can simply forward the
    return value.

    Args:
        tool_name: The tool name from the ``tools/call`` request.
        arguments: The (already schema-validated, per the SDK) tool
            arguments, including the reserved ``_wait_seconds`` if the
            caller supplied it.
        catalogue: The frozen catalogue built at startup.
        options: The frozen startup options.
        tracker: This server process's :class:`LaunchTracker` (R3).
        registries_config: The configured registries; defaults to
            ``registry.config.load_config()``, overridable for tests.
        progress_token: The caller-supplied MCP progress token, if any
            (usually read from ``request_context.meta.progressToken``).
        send_progress: An async callable matching
            ``ServerSession.send_progress_notification``'s signature, used
            to emit ``notifications/progress`` during a bounded wait
            (E9-T5). ``None`` (the default) silently disables progress
            regardless of ``progress_token``.

    Raises:
        UnknownToolError: If ``tool_name`` isn't in the catalogue, or its
            workflow can no longer be resolved (E9-T1).
        ConcurrentRunLimitError: If ``--max-concurrent-runs`` is set and
            already reached (R3).
        LaunchError: If a required input is missing, or the underlying
            ``launch_background()`` call itself fails.
    """
    identity = catalogue.reverse.get(tool_name)
    if identity is None:
        raise UnknownToolError(
            f"Unknown tool: {tool_name!r}. This server does not publish a workflow "
            "under that name -- call tools/list to see the current catalogue."
        )
    registry, workflow = identity
    entry = _entry_for_tool(catalogue, tool_name)

    # R3 -- reject before anything is resolved or forked.
    _enforce_concurrency_cap(tracker, options)

    values = {k: v for k, v in arguments.items() if k != WAIT_SECONDS_PARAM}
    raw_wait = arguments.get(WAIT_SECONDS_PARAM)
    requested_wait = None if raw_wait is None else float(raw_wait)

    if registries_config is None:
        registries_config = load_registries_config()

    workflow_path = _resolve_workflow_path(
        registry,
        workflow,
        entry.pin,
        options=options,
        registries_config=registries_config,
        source=entry.source,
    )
    config = load_config(workflow_path)
    inputs = build_typed_launch_inputs(values, config.workflow.input)

    # DD11 -- a gate always parks; the server never auto-skips one on the
    # caller's behalf. See `_NEVER_SKIP_GATES`'s own comment.
    assert _NEVER_SKIP_GATES is False, (
        "DD11: the MCP server must never pass skip_gates=True to launch_background()"
    )
    launch = launch_background(
        workflow_path=workflow_path,
        inputs=inputs,
        provider_override=None,
        skip_gates=_NEVER_SKIP_GATES,
        web_port=0,
        metadata=_mcp_launch_metadata(tool_name),
    )
    tracker.register(launch.run_id)

    record = read_run_record(launch.run_id)
    started_at = record.started_at if record is not None else ""

    wait_seconds = resolve_wait_seconds(
        requested_wait, mcp_mode=config.workflow.mcp.mode, max_wait_seconds=options.max_wait_seconds
    )

    if wait_seconds <= 0:
        return _immediate_result(launch, entry, started_at)

    deadline = time.monotonic() + wait_seconds
    summary, terminal = await _await_terminal_or_gate(
        launch.run_id,
        deadline=deadline,
        progress_token=progress_token,
        send_progress=send_progress,
    )
    return _final_result(launch, entry, started_at, summary, terminal)
