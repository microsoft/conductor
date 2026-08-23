"""Frozen startup options for ``conductor mcp serve`` (E7-T1).

:class:`ServeOptions` is the single artifact holding every value that can
influence what the server exposes or how it behaves at runtime. That is a
deliberate design property, not an implementation convenience: NFR3 ("no
tool accepts a filesystem path, URL, or registry source as a parameter")
is only checkable at all because there is exactly one place an operator's
input can enter the system. Any value a generated tool's ``inputSchema``
carries did not come from here; any value the catalogue builder consulted
did.

The dataclass is frozen for the same reason the catalogue it feeds is
immutable (DD3): the spec forbids a server's tool list — and, by
extension, the configuration that produced it — from varying within a
connection. There is no in-place mutation path for a running server to
accidentally take.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Toolsets on by default (design's Key Components -> 5. Toolsets table):
# every generated workflow tool, plus the four run-lifecycle tools.
# `introspect` and `diagnose` are off by default (DD3); `discovery` is not
# operator-selectable at all -- it is decided by the catalogue builder once
# the exposed count crosses `max_direct_tools` (FR9), so it deliberately
# does not appear here.
DEFAULT_TOOLSETS: tuple[str, ...] = ("workflows", "runs")

# Mirrors the CLI defaults named in the plan's E8-T1 (`cli/mcp.py`) so a
# directly-constructed `ServeOptions` (as tests and any future embedding do)
# behaves identically to the CLI's own defaults.
DEFAULT_MAX_DIRECT_TOOLS = 25
DEFAULT_MAX_WAIT_SECONDS = 300
DEFAULT_MAX_CONCURRENT_RUNS = 0  # R3: unbounded until an operator opts in.


@dataclass(frozen=True)
class ServeOptions:
    """Every startup argument for ``conductor mcp serve``.

    Attributes:
        registries: Glob patterns selecting which configured registries are
            enumerated at all (FR2). ``None`` means "every registry in
            ``RegistriesConfig.registries``" — the FR1 default. An empty
            tuple is different from ``None``: it means "no configured
            registries selected", which is only useful in combination with
            ``workflow_dirs``.
        workflow_dirs: Local directories whose workflow files are exposed
            in addition to (or instead of) any registry (FR2). Each is a
            startup argument the operator typed, never a value a tool
            accepts (NFR3).
        allow: Glob patterns for the allow-list rung of the exposure ladder
            (DD4, rung 2). A non-empty tuple switches the ladder into
            allow-list mode: only matching workflows are candidates, and a
            match overrides a workflow's own ``mcp.expose: false``.
        deny: Glob patterns for the deny rung (DD4, rung 1) — the highest
            precedence rung; a match here excludes a workflow
            unconditionally, even one an ``allow`` pattern also matches.
        toolsets: Which named toolsets are enabled (DD3, *Key Components ->
            5*). Fixed at startup; never re-evaluated per connection.
        max_direct_tools: Above this many exposed workflows, the catalogue
            builder serves the two-tool discovery pair instead of one tool
            per workflow (FR9).
        max_wait_seconds: The hard ceiling every generated tool's
            ``_wait_seconds`` parameter is capped at, regardless of the
            value a caller requests, and the resolution a ``mode: sync``
            workflow uses when the caller omits the parameter (FR5).
        tool_prefix: Optional operator-chosen prefix prepended to every
            *generated* workflow tool name (not the static lifecycle
            tools, which already carry a meaningful ``conductor_`` prefix)
            (DD10).
        max_concurrent_runs: Bounds how many runs launched by *this server
            process* may be live at once; ``0`` (default) is unbounded so
            behavior is unchanged unless an operator opts in (R3). Tracked
            in-process only (``mcp/serve/invoke.py::LaunchTracker``), so
            restarting the server resets the count to zero -- a consequence
            of the design's "the MCP server owns no execution state"
            principle, not a lapse from it.
        introspect_full: Restores full tool-call arguments/results on
            ``conductor_run_events`` instead of the default
            ``{name, status, byte_size}`` reduction (R4).
    """

    registries: tuple[str, ...] | None = None
    workflow_dirs: tuple[Path, ...] = ()
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    toolsets: tuple[str, ...] = field(default_factory=lambda: DEFAULT_TOOLSETS)
    max_direct_tools: int = DEFAULT_MAX_DIRECT_TOOLS
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS
    tool_prefix: str | None = None
    max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS
    introspect_full: bool = False
