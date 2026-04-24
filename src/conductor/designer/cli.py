"""CLI helper for ``conductor designer``.

Launches the visual workflow designer server and opens a browser.
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from pathlib import Path

from rich.console import Console

logger = logging.getLogger(__name__)
console = Console(stderr=True)


def run_designer(
    *,
    workflow_path: str | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    no_open: bool = False,
) -> None:
    """Start the designer server and block until Ctrl+C."""
    resolved = Path(workflow_path).resolve() if workflow_path else None

    if resolved and not resolved.exists():
        console.print(f"[yellow]⚠[/yellow]  File not found: {resolved}")
        console.print("  Starting with a blank workflow. Save will create the file.")

    async def _run() -> None:
        from conductor.designer.server import DesignerServer

        server = DesignerServer(
            workflow_path=resolved,
            host=host,
            port=port,
        )
        await server.start()

        url = server.url
        console.print(
            f"\n[bold cyan]🎨 Conductor Designer[/bold cyan]  →  [link={url}]{url}[/link]"
        )
        if resolved:
            console.print(f"   Editing: [dim]{resolved}[/dim]")
        console.print("   Press [bold]Ctrl+C[/bold] to stop.\n")

        if not no_open:
            webbrowser.open(url)

        try:
            if server._serve_task:
                await server._serve_task
        except asyncio.CancelledError:
            pass
        finally:
            await server.stop()

    asyncio.run(_run())
