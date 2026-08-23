"""Wire the frozen catalogue onto the low-level MCP ``Server`` and run it
over stdio (FR1, FR10, DD3, DD9, E8-T4, E8-T5).

``build_server`` registers exactly one handler — ``list_tools`` — that
always returns the catalogue built once at startup. That is what makes the
server DD3-compliant: there is no code path here that rebuilds, filters, or
otherwise varies the tool list per call or per connection. ``Catalogue.tools()``
itself hands back a fresh deep copy on every call (so neither this module nor
the SDK's own tool cache can mutate the catalogue's canonical data), but the
*content* returned is always byte-identical.

``serve_stdio`` is the whole runtime: print the FR10 startup summary, then run
the wired server over ``stdio_server()`` until the host closes the connection.
Stdout is the JSON-RPC transport (DD9) — nothing in this module, or anything
it calls, may write to it; the startup summary and every anomaly it reports go
through a dedicated stderr console (``conductor.console.make_console(stderr=True)``,
same convention as ``cli/checkpoint.py`` / ``cli/gate.py``).
"""

from __future__ import annotations

import logging

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool

from conductor import __version__
from conductor.console import make_console, styled
from conductor.mcp.serve.catalogue import Catalogue
from conductor.mcp.serve.options import ServeOptions

logger = logging.getLogger(__name__)

console = make_console(stderr=True)

# The name reported in `initialize`'s server info. Kept as a module constant
# rather than threading it through `ServeOptions` -- there is exactly one
# Conductor MCP server implementation, so there is nothing for an operator to
# choose here.
SERVER_NAME = "conductor"


def build_server(catalogue: Catalogue) -> Server:
    """Wire a frozen :class:`Catalogue` onto a low-level ``Server`` (E8-T4).

    The returned server answers ``tools/list`` with exactly the catalogue's
    tools, in the catalogue's own stable order, on every call — the same
    list for the lifetime of the server process, satisfying MCP
    ``2026-07-28``'s "MUST NOT vary per-connection or as a side effect of
    other requests on the connection" (DD3).

    Args:
        catalogue: The catalogue built once at startup by
            :func:`conductor.mcp.serve.catalogue.build_catalogue`.

    Returns:
        A ``Server`` ready to run over any transport (``server.run(...)``).
    """
    server = Server(SERVER_NAME, version=__version__)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return list(catalogue.tools())

    return server


async def serve_stdio(catalogue: Catalogue, options: ServeOptions) -> None:
    """Run the MCP server over stdio until the host disconnects (DD9).

    Prints the FR10 startup summary before entering the read loop -- this is
    the only channel a stdio server has, and hosts surface it in their MCP
    logs.

    Args:
        catalogue: The frozen catalogue to publish.
        options: The startup options that produced it, used only for the
            startup summary (e.g. the ``--max-direct-tools`` value that
            explains a ``discovery``-mode catalogue).
    """
    log_startup_summary(catalogue, options)
    server = build_server(catalogue)
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
