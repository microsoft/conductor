"""Wire the frozen catalogue onto the low-level MCP ``Server`` and run it
over stdio (FR1, FR8, FR9, FR10, DD3, DD9, E8-T4, E8-T5, E11-T1, E12-T2).

``build_server`` registers two handlers: ``list_tools`` and ``call_tool``.
In **direct** mode (``catalogue.mode == "direct"``), ``list_tools`` returns
the catalogue's workflow tools built once at startup. In **discovery** mode
(``catalogue.mode == "discovery"`` -- the exposed count exceeded
``--max-direct-tools``, FR9), the catalogue's per-workflow tools are replaced
outright by the fixed two-tool ``discovery.py`` pair -- never both at once,
and never re-decided after startup, since ``catalogue.mode`` was itself fixed
once by the catalogue builder (E7/E12-T2). Either way, ``list_tools`` also
returns (E11-T1) whichever of the ``introspect``/``diagnose`` toolset's tools
``options.toolsets`` enables -- decided once, here, from the frozen
``ServeOptions``, never re-evaluated per call or per connection. That is what
makes the server DD3-compliant: there is no code path here that rebuilds,
filters, or otherwise varies the tool list per call or per connection.
``Catalogue.tools()`` itself hands back a fresh deep copy on every call (so
neither this module nor the SDK's own tool cache can mutate the catalogue's
canonical data), but the *content* returned is always byte-identical.
``call_tool`` dispatches an ``introspect``/``diagnose`` tool name to its
adapter in ``introspect.py``/``diagnose.py`` when its toolset is enabled, and
(only in discovery mode) a ``conductor_find_workflow``/``conductor_run_workflow``
name to ``discovery.py`` -- these adapters are otherwise unreachable through
the protocol, regardless of what ``tools/list`` reports, which is what keeps
the gate real rather than cosmetic.

**Scope note.** Dispatching a *direct-mode* generated workflow tool name
(``invoke.py::invoke_workflow_tool``, E9) or a run-lifecycle tool name
(``runs.py``, E10) through ``call_tool`` remains out of scope here -- see
those modules' own docstrings for why that wiring was deliberately left to
"a later epic" once those tools' own ``Tool`` definitions exist. E12's own
discovery pair is the one exception: ``conductor_run_workflow`` must itself
be callable to satisfy this epic's acceptance criteria, so this module wires
it (and ``conductor_find_workflow``) directly, sharing the same
:class:`~conductor.mcp.serve.invoke.LaunchTracker` a later epic's direct-mode
wiring will also need (R3).

``serve_stdio`` is the whole runtime: print the FR10 startup summary, then run
the wired server over ``stdio_server()`` until the host closes the connection.
Stdout is the JSON-RPC transport (DD9) — nothing in this module, or anything
it calls, may write to it; the startup summary and every anomaly it reports go
through a dedicated stderr console (``conductor.console.make_console(stderr=True)``,
same convention as ``cli/checkpoint.py`` / ``cli/gate.py``).
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import ResourceLink, TextContent, Tool

from conductor import __version__
from conductor.console import make_console, styled
from conductor.mcp.serve.catalogue import Catalogue
from conductor.mcp.serve.diagnose import conductor_doctor, conductor_run_logs
from conductor.mcp.serve.diagnose import conductor_validate_workflow as _validate_workflow
from conductor.mcp.serve.discovery import conductor_find_workflow as _find_workflow
from conductor.mcp.serve.discovery import conductor_run_workflow as _run_workflow
from conductor.mcp.serve.introspect import DEFAULT_EVENTS_LIMIT
from conductor.mcp.serve.introspect import conductor_node_detail as _node_detail
from conductor.mcp.serve.introspect import conductor_plan_tree as _plan_tree
from conductor.mcp.serve.introspect import conductor_run_events as _run_events
from conductor.mcp.serve.invoke import LaunchTracker
from conductor.mcp.serve.options import ServeOptions, is_toolset_enabled

logger = logging.getLogger(__name__)

console = make_console(stderr=True)

# The name reported in `initialize`'s server info. Kept as a module constant
# rather than threading it through `ServeOptions` -- there is exactly one
# Conductor MCP server implementation, so there is nothing for an operator to
# choose here.
SERVER_NAME = "conductor"

_RUN_ID_PROPERTY = {"type": "string", "description": "The run identifier to look up."}

# E11-T1: the `introspect` toolset's own `Tool` definitions -- separate from
# the catalogue's generated workflow tools, since these three describe fixed
# server-side query parameters (never a workflow's own `input:` schema).
_INTROSPECT_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="conductor_run_events",
        description=(
            "Query a run's event log, optionally filtered by event type and bounded "
            "by limit. A tool call's arguments/result are redacted by default (R4) "
            "unless the server was started with --introspect-full."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": _RUN_ID_PROPERTY,
                "event_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only return events whose type is in this list.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of events to return.",
                    "default": DEFAULT_EVENTS_LIMIT,
                },
            },
            "required": ["run_id"],
        },
    ),
    Tool(
        name="conductor_node_detail",
        description="One step's prompt, output, and activity stream for a run.",
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": _RUN_ID_PROPERTY,
                "agent": {"type": "string", "description": "The step (agent) name."},
            },
            "required": ["run_id", "agent"],
        },
    ),
    Tool(
        name="conductor_plan_tree",
        description=(
            "The parsed structure of a published workflow: its entry point, nodes, and routes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The catalogue tool name."},
            },
            "required": ["name"],
        },
    ),
)

# E11-T1: the `diagnose` toolset's own `Tool` definitions.
_DIAGNOSE_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="conductor_doctor",
        description="Run Conductor's own environment/provider/registry diagnostics.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="conductor_validate_workflow",
        description="Validate a published workflow the same way `conductor validate` does.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The catalogue tool name."},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="conductor_run_logs",
        description="Locate a run's log files as resource links, never their contents (DD12).",
        inputSchema={
            "type": "object",
            "properties": {"run_id": _RUN_ID_PROPERTY},
            "required": ["run_id"],
        },
    ),
)

# E12-T2: the `discovery` toolset's own `Tool` definitions -- published only
# when `catalogue.mode == "discovery"` (FR9), replacing the catalogue's
# per-workflow tools outright rather than joining them. Never
# operator-selectable via `--toolsets` (see `options.py::ALL_TOOLSETS`), so
# there is no `is_toolset_enabled` gate for this pair -- `catalogue.mode` is
# the only switch, and it was decided once, at startup, by the catalogue
# builder (DD3).
_DISCOVERY_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="conductor_find_workflow",
        description=(
            "Search the published workflow catalogue by name, description, or registry. "
            "This server is running in discovery mode: its exposed workflow count exceeds "
            "--max-direct-tools, so individual workflow tools are not published directly -- "
            "use this tool (and conductor_run_workflow) instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Case-insensitive substring to match against a workflow's name, "
                        "description, or registry. Omit or pass an empty string to list "
                        "every published workflow."
                    ),
                },
            },
        },
    ),
    Tool(
        name="conductor_run_workflow",
        description=(
            "Launch a published workflow found via conductor_find_workflow. `name` must be "
            "the exact catalogue tool name conductor_find_workflow reported -- never a "
            "filesystem path, URL, or registry source (NFR3)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The catalogue tool name, from conductor_find_workflow.",
                },
                "inputs": {
                    "type": "object",
                    "description": "The workflow's own declared input parameters.",
                },
                "_wait_seconds": {
                    "type": "number",
                    "description": (
                        "0 = return immediately; >0 = wait up to N seconds for a terminal "
                        "run state (capped by the server's --max-wait-seconds ceiling "
                        "regardless of the value requested); omitted defers to this "
                        "workflow's declared mcp.mode."
                    ),
                },
            },
            "required": ["name"],
        },
    ),
)


def _extra_tools_for(options: ServeOptions) -> tuple[Tool, ...]:
    """Every non-catalogue tool ``options.toolsets`` enables (E11-T1),
    decided once from the frozen startup options."""
    tools: list[Tool] = []
    if is_toolset_enabled(options, "introspect"):
        tools.extend(_INTROSPECT_TOOLS)
    if is_toolset_enabled(options, "diagnose"):
        tools.extend(_DIAGNOSE_TOOLS)
    return tuple(tools)


async def _dispatch_extra_tool(
    name: str, arguments: dict[str, Any], *, catalogue: Catalogue, options: ServeOptions
) -> dict[str, Any] | tuple[list[ResourceLink], dict[str, Any]]:
    """Dispatch one ``tools/call`` for an ``introspect``/``diagnose`` tool
    (E11-T1). Only reachable for a name whose toolset is enabled -- a
    disabled toolset's tool name is refused here even if a caller invokes
    it directly, without going through ``tools/list`` first, so the gate
    holds regardless of what a client already cached.

    Raises:
        ValueError: If ``name`` names no known/enabled tool.
    """
    if is_toolset_enabled(options, "introspect"):
        if name == "conductor_run_events":
            event_types = arguments.get("event_types")
            return _run_events(
                arguments["run_id"],
                event_types=tuple(event_types) if event_types is not None else None,
                limit=arguments.get("limit", DEFAULT_EVENTS_LIMIT),
                introspect_full=options.introspect_full,
            )
        if name == "conductor_node_detail":
            return _node_detail(arguments["run_id"], arguments["agent"])
        if name == "conductor_plan_tree":
            return _plan_tree(arguments["name"], catalogue=catalogue, options=options)

    if is_toolset_enabled(options, "diagnose"):
        if name == "conductor_doctor":
            return await conductor_doctor()
        if name == "conductor_validate_workflow":
            return _validate_workflow(arguments["name"], catalogue=catalogue, options=options)
        if name == "conductor_run_logs":
            return conductor_run_logs(arguments["run_id"])

    raise ValueError(f"Unknown tool: {name!r}.")


# E12-T2: the discovery pair's own tool names -- checked against `name`
# by `_call_tool` before falling back to `_dispatch_extra_tool`, and never
# just against `catalogue.mode` alone, so a stray call for one of these two
# names cannot be misrouted to `_dispatch_extra_tool`'s `Unknown tool` path
# in discovery mode.
_DISCOVERY_TOOL_NAMES: frozenset[str] = frozenset(tool.name for tool in _DISCOVERY_TOOLS)


async def _dispatch_discovery_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    catalogue: Catalogue,
    options: ServeOptions,
    tracker: LaunchTracker,
) -> dict[str, Any] | tuple[list[TextContent | ResourceLink], dict[str, Any]]:
    """Dispatch one ``tools/call`` for the ``discovery`` pair (E12-T2).

    Only ever invoked when ``catalogue.mode == "discovery"`` -- see
    ``_call_tool``'s own gate -- since the pair is never published, and
    therefore never callable, in direct mode.

    Raises:
        ValueError: If ``name`` names neither discovery tool.
    """
    if name == "conductor_find_workflow":
        return _find_workflow(arguments.get("query", ""), catalogue=catalogue)
    if name == "conductor_run_workflow":
        return await _run_workflow(
            arguments["name"],
            arguments.get("inputs"),
            arguments.get("_wait_seconds"),
            catalogue=catalogue,
            options=options,
            tracker=tracker,
        )
    raise ValueError(f"Unknown tool: {name!r}.")


def build_server(catalogue: Catalogue, options: ServeOptions) -> Server:
    """Wire a frozen :class:`Catalogue` onto a low-level ``Server`` (E8-T4,
    E11-T1, E12-T2).

    In direct mode, the returned server answers ``tools/list`` with the
    catalogue's own workflow tools; in discovery mode (FR9), it answers with
    the fixed ``conductor_find_workflow``/``conductor_run_workflow`` pair
    instead -- never both -- since ``catalogue.mode`` was decided once, at
    startup, by the catalogue builder and is never re-evaluated here. Either
    way, ``list_tools`` also returns whichever ``introspect``/``diagnose``
    tools ``options.toolsets`` enables, in a stable order, on every call —
    the same list for the lifetime of the server process, satisfying MCP
    ``2026-07-28``'s "MUST NOT vary per-connection or as a side effect of
    other requests on the connection" (DD3). ``tools/call`` for an
    ``introspect``/``diagnose`` tool is dispatched to its adapter only when
    its toolset is enabled; a discovery-pair name is dispatched only when
    ``catalogue.mode == "discovery"``.

    Args:
        catalogue: The catalogue built once at startup by
            :func:`conductor.mcp.serve.catalogue.build_catalogue`.
        options: The frozen startup options -- read for ``toolsets`` (which
            extra tools are exposed and callable) and ``introspect_full``
            (whether ``conductor_run_events`` restores tool payloads).

    Returns:
        A ``Server`` ready to run over any transport (``server.run(...)``).

    Raises:
        ValueError: If an enabled ``introspect``/``diagnose`` tool name, or
            (in discovery mode) a discovery-pair tool name, collides with a
            published workflow tool name (e.g. a workflow named
            ``conductor_run_events`` in direct mode, or ``conductor_find_workflow``
            in discovery mode) -- publishing both would leave ``call_tool``
            always resolving to the fixed tool's adapter, silently shadowing
            the workflow tool.
    """
    server = Server(SERVER_NAME, version=__version__)
    extra_tools = _extra_tools_for(options)
    discovery_mode = catalogue.mode == "discovery"
    discovery_tools = _DISCOVERY_TOOLS if discovery_mode else ()
    # One tracker per server process (R3) -- shared by every
    # `conductor_run_workflow` call this server dispatches, mirroring how a
    # later epoch's direct-mode wiring will share the same tracker across
    # every generated workflow tool's own invocations.
    tracker = LaunchTracker()

    reserved_names = {tool.name for tool in extra_tools} | {tool.name for tool in discovery_tools}
    colliding = sorted(reserved_names & set(catalogue.reverse))
    if colliding:
        raise ValueError(
            f"Tool name(s) {', '.join(colliding)} are reserved by the "
            "introspect/diagnose/discovery toolsets and collide with a published workflow "
            "tool of the same name; rename the workflow or disable the "
            "conflicting toolset."
        )

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        if discovery_mode:
            return [*discovery_tools, *extra_tools]
        return [*catalogue.tools(), *extra_tools]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]):
        if discovery_mode and name in _DISCOVERY_TOOL_NAMES:
            return await _dispatch_discovery_tool(
                name, arguments, catalogue=catalogue, options=options, tracker=tracker
            )
        return await _dispatch_extra_tool(name, arguments, catalogue=catalogue, options=options)

    return server


async def serve_stdio(catalogue: Catalogue, options: ServeOptions) -> None:
    """Run the MCP server over stdio until the host disconnects (DD9).

    Prints the FR10 startup summary before entering the read loop -- this is
    the only channel a stdio server has, and hosts surface it in their MCP
    logs.

    Args:
        catalogue: The frozen catalogue to publish.
        options: The startup options that produced it -- also read for
            ``toolsets``/``introspect_full`` (E11-T1) in addition to the
            startup summary (e.g. the ``--max-direct-tools`` value that
            explains a ``discovery``-mode catalogue).
    """
    log_startup_summary(catalogue, options)
    server = build_server(catalogue, options)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def log_startup_summary(catalogue: Catalogue, options: ServeOptions) -> None:
    """Print the FR10 startup summary to stderr (E8-T5).

    Reports, in order: the exposed count and direct-vs-discovery mode; every
    published tool's name, source registry and pinned identity; every
    workflow exposed with a degraded schema, and why; and every tool-name
    collision the catalogue qualified, naming both registries. The last two
    are also logged through the standard ``logging`` module at warning level
    so they are captured wherever conductor's other startup warnings are
    (e.g. a host's own log aggregation), not only in this printed summary.

    Never touches stdout (DD9) -- every line goes through this module's own
    stderr-bound console.
    """
    mode_label = "direct" if catalogue.mode == "direct" else "discovery"
    console.print(
        styled(
            "[bold]conductor mcp serve[/bold]: exposing {} workflow(s) in {} mode "
            "(--max-direct-tools={}).",
            len(catalogue.entries),
            mode_label,
            options.max_direct_tools,
        )
    )
    # E11-T1: toolset membership is decided once, here, at startup -- never
    # re-evaluated per connection or per request (DD3). Reporting it in the
    # one channel a stdio server has makes that decision visible the same
    # way the exposed-count/mode line above already is.
    console.print(styled("Toolsets enabled: {}.", ", ".join(sorted(options.toolsets)) or "(none)"))

    for entry in catalogue.entries:
        console.print(
            styled(
                "  [cyan]{}[/cyan] <- {}/{} (pin: {})",
                entry.tool_name,
                entry.registry,
                entry.workflow,
                entry.pin.as_str(),
            )
        )
        if entry.resolution_tier == "degraded":
            reason = entry.tool.description
            console.print(styled("    [yellow]degraded schema:[/yellow] {}", reason))
            logger.warning(
                "%s (%s/%s) is exposed with a degraded schema: %s",
                entry.tool_name,
                entry.registry,
                entry.workflow,
                reason,
            )

    for collision in catalogue.collisions:
        sources = {f"{identity.registry}/{identity.workflow}" for identity in collision.identities}
        registries = ", ".join(sorted(sources))
        qualified = ", ".join(collision.qualified_names)
        console.print(
            styled(
                "[yellow]Name collision[/yellow] on {!r} across {} -- qualified as {}.",
                collision.base_slug,
                registries,
                qualified,
            )
        )
        logger.warning(
            "Tool name collision on %r across %s -- qualified as %s",
            collision.base_slug,
            registries,
            qualified,
        )
