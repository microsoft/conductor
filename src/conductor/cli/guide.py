"""``conductor guide`` — submit mid-run guidance to a running workflow.

Port auto-discovery via ``scan_pid_files()`` mirrors ``app.py::status``'s
reasoning; error-response mapping (403/409/422/connect-error) mirrors
``cli/gate.py::_gate_respond_impl`` — ``cli/gate.py``'s own ``--port`` is a
required option with no auto-discovery, so only the latter half of this
command is actually modeled on it.
"""

from __future__ import annotations

import httpx
import typer
from rich.text import Text

from conductor.console import make_console, styled
from conductor.web.auth import resolve_cli_token

console = make_console(stderr=True)


def guide_impl(text: str, port: int | None, token: str | None) -> None:
    """Send guidance text to a running dashboard over HTTP.

    Shared implementation behind the ``conductor guide`` command.
    """
    resolved_port = port
    if resolved_port is None:
        from conductor.cli.pid import scan_pid_files

        # scan_pid_files (not read_pid_files): this is a read, not a
        # maintenance operation — matching the reasoning in
        # ``app.py::status``.
        running = scan_pid_files()
        if not running:
            console.print(
                Text.from_markup(
                    "[bold red]Error:[/bold red] No background workflows are currently running."
                )
            )
            raise typer.Exit(code=1)
        if len(running) > 1:
            console.print(
                styled(
                    "[bold yellow]Multiple background workflows running ({}).[/bold yellow]",
                    len(running),
                )
            )
            console.print(Text.from_markup("[dim]Specify --port to target one:[/dim]"))
            for entry in running:
                console.print(
                    styled(
                        "  port {} — {}",
                        entry.get("port"),
                        entry.get("workflow", "unknown"),
                    )
                )
            raise typer.Exit(code=1)
        resolved_port = running[0]["port"]

    base_url = f"http://127.0.0.1:{resolved_port}"
    # Resolve token: flag > CONDUCTOR_GATE_TOKEN env var > the per-run token
    # file written by WebDashboard.start() (issue #397).
    resolved_token = resolve_cli_token(resolved_port, token)

    headers: dict[str, str] = {}
    if resolved_token is not None:
        headers["Authorization"] = f"Bearer {resolved_token}"

    try:
        resp = httpx.post(
            f"{base_url}/api/guidance", json={"text": text}, headers=headers, timeout=10
        )
    except httpx.ConnectError:
        console.print(
            styled(
                "[bold red]Error:[/bold red] Cannot connect to dashboard on "
                "port {}. Is the workflow running with --web or --web-bg?",
                resolved_port,
            )
        )
        raise typer.Exit(code=1) from None
    except httpx.HTTPError as exc:
        console.print(styled("[bold red]Error:[/bold red] Request failed: {}", exc))
        raise typer.Exit(code=1) from None

    if resp.status_code == 403:
        console.print(
            Text.from_markup(
                "[bold red]Error:[/bold red] Authentication failed. "
                "Provide a valid token with --token, CONDUCTOR_GATE_TOKEN env var, "
                "or the auto-discovered token file in ~/.conductor/runs/."
            )
        )
        raise typer.Exit(code=1)
    if resp.status_code == 409:
        detail = resp.json().get("error", "Workflow has already completed")
        console.print(styled("[bold red]Error:[/bold red] {}", detail))
        raise typer.Exit(code=1)
    if resp.status_code == 422:
        detail = resp.json().get("error", "Validation error")
        console.print(styled("[bold red]Error:[/bold red] {}", detail))
        raise typer.Exit(code=1)
    if resp.status_code != 200:
        console.print(
            styled(
                "[bold red]Error:[/bold red] Unexpected response ({}): {}",
                resp.status_code,
                resp.text,
            )
        )
        raise typer.Exit(code=1)

    body = resp.json()
    if body.get("paused"):
        console.print(
            Text.from_markup(
                "[green]Guidance sent[/green] — the paused agent will resume with it applied."
            )
        )
    else:
        console.print(
            Text.from_markup(
                "[green]Guidance sent[/green] — it will apply at the next step boundary."
            )
        )
