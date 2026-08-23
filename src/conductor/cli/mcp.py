"""Typer subcommand group for ``conductor mcp serve`` (E8-T1, E8-T3).

Nothing under ``conductor.mcp`` -- not even ``conductor.mcp.serve.options``,
which has no SDK dependency of its own -- is imported at this module's top
level. ``conductor/cli/app.py`` imports every ``cli/*.py`` sub-app module
(this one included) on *every* ``conductor`` invocation, and
``conductor/mcp/__init__.py`` (the parent package, not
``conductor.mcp.serve``) eagerly imports ``MCPManager``, which pulls in the
full ``mcp`` SDK -- including its server-side surface -- as a side effect of
its own ``__init__.py``. So merely importing ``conductor.mcp.serve.options``
at load time would already pay that cost for every ``conductor`` command,
not just ``conductor mcp serve``. Every reference to ``conductor.mcp.*`` in
this module is therefore a lazy import inside a function body.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from conductor.console import make_console, styled

if TYPE_CHECKING:
    from conductor.mcp.serve.options import ServeOptions

console = make_console(stderr=True)

mcp_app = typer.Typer(
    name="mcp",
    help="Expose Conductor workflows as MCP tools over stdio.",
    no_args_is_help=True,
)

# Mirrors `conductor.mcp.serve.options`'s own defaults (E7-T1) so `--help`
# shows the same numbers `ServeOptions` would default to, without importing
# that module (and, transitively, the `mcp` SDK) at CLI startup. `serve()`
# is the single place that actually applies them, via a lazy import.
_DEFAULT_MAX_DIRECT_TOOLS = 25
_DEFAULT_MAX_WAIT_SECONDS = 300
_DEFAULT_MAX_CONCURRENT_RUNS = 0


@mcp_app.command("serve")
def serve(
    registry: Annotated[
        list[str] | None,
        typer.Option(
            "--registry",
            help=(
                "Glob pattern selecting which configured registries to expose "
                "(repeatable). Default: every registry in registries.toml."
            ),
        ),
    ] = None,
    allow: Annotated[
        list[str] | None,
        typer.Option(
            "--allow",
            help=(
                "Glob pattern for the allow-list rung of the exposure ladder "
                "(repeatable). A match overrides a workflow's own mcp.expose=false."
            ),
        ),
    ] = None,
    deny: Annotated[
        list[str] | None,
        typer.Option(
            "--deny",
            help=(
                "Glob pattern for the deny rung of the exposure ladder (repeatable) "
                "-- the highest-precedence rung; a match excludes a workflow "
                "unconditionally."
            ),
        ),
    ] = None,
    workflow_dir: Annotated[
        list[Path] | None,
        typer.Option(
            "--workflow-dir",
            help=(
                "Local directory whose workflow files are exposed in addition to "
                "(or instead of) any registry (repeatable, non-recursive)."
            ),
        ),
    ] = None,
    toolsets: Annotated[
        list[str] | None,
        typer.Option(
            "--toolsets",
            help=(
                "Toolset names to enable, e.g. workflows, runs, introspect, "
                "diagnose (repeatable). Default: workflows, runs."
            ),
        ),
    ] = None,
    max_direct_tools: Annotated[
        int,
        typer.Option(
            "--max-direct-tools",
            help=(
                "Above this many exposed workflows, serve the two-tool discovery "
                "pair instead of one tool per workflow."
            ),
        ),
    ] = _DEFAULT_MAX_DIRECT_TOOLS,
    max_wait_seconds: Annotated[
        int,
        typer.Option(
            "--max-wait-seconds",
            help=(
                "Hard ceiling, in seconds, on how long a blocking tool call may "
                "wait for a terminal run state."
            ),
        ),
    ] = _DEFAULT_MAX_WAIT_SECONDS,
    tool_prefix: Annotated[
        str | None,
        typer.Option(
            "--tool-prefix",
            help="Optional prefix prepended to every generated workflow tool name.",
        ),
    ] = None,
    max_concurrent_runs: Annotated[
        int,
        typer.Option(
            "--max-concurrent-runs",
            help=(
                "Bound how many runs launched by this server process may be live "
                "at once. 0 (default) is unbounded."
            ),
        ),
    ] = _DEFAULT_MAX_CONCURRENT_RUNS,
    introspect_full: Annotated[
        bool,
        typer.Option(
            "--introspect-full",
            help=(
                "Restore full tool-call arguments and results on "
                "conductor_run_events instead of the default reduced form."
            ),
        ),
    ] = False,
) -> None:
    """Start an MCP server over stdio, publishing a frozen tool catalogue.

    By default, every registry configured in registries.toml is exposed,
    one tool per workflow unless the exposed count exceeds
    --max-direct-tools, in which case a two-tool discovery pair is served
    instead. The tool list is fixed at startup and never varies across
    calls or connections.

    \b
    Examples:
        conductor mcp serve
        conductor mcp serve --registry official --allow release-*
        conductor mcp serve --workflow-dir ./workflows --max-direct-tools 10
    """
    from conductor.mcp.serve.options import DEFAULT_TOOLSETS, ServeOptions

    options = ServeOptions(
        registries=tuple(registry) if registry is not None else None,
        workflow_dirs=tuple(workflow_dir) if workflow_dir else (),
        allow=tuple(allow) if allow else (),
        deny=tuple(deny) if deny else (),
        toolsets=tuple(toolsets) if toolsets else DEFAULT_TOOLSETS,
        max_direct_tools=max_direct_tools,
        max_wait_seconds=max_wait_seconds,
        tool_prefix=tool_prefix,
        max_concurrent_runs=max_concurrent_runs,
        introspect_full=introspect_full,
    )
    _serve_impl(options)


def _serve_impl(options: ServeOptions) -> None:
    """Build the catalogue and run the MCP server over stdio.

    Split out from :func:`serve` so tests can monkeypatch this exact
    function (or the ``serve_stdio``/``build_catalogue`` it calls) without
    binding a real stdio transport to the test process's own stdin/stdout
    (E8-T7).
    """
    import asyncio

    from conductor.mcp.serve.catalogue import build_catalogue
    from conductor.mcp.serve.server import serve_stdio
    from conductor.registry.errors import RegistryError

    try:
        catalogue = build_catalogue(options)
    except RegistryError as exc:
        console.print(styled("[bold red]Error:[/bold red] {}", exc))
        raise typer.Exit(code=1) from None

    asyncio.run(serve_stdio(catalogue, options))
