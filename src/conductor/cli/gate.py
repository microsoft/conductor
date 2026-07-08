"""Typer subcommand group for interacting with human gates."""

from __future__ import annotations

import os
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console

console = Console(stderr=True)

gate_app = typer.Typer(
    name="gate",
    help="Interact with human gates in running workflows.",
    no_args_is_help=True,
)


@gate_app.command("respond")
def respond(
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help="Dashboard port of the running workflow.",
        ),
    ],
    choice: Annotated[
        str,
        typer.Option(
            "--choice",
            "-c",
            help="Selected gate option value.",
        ),
    ],
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            "-a",
            help="Gate agent name (auto-discovered via /api/gate-status if omitted).",
        ),
    ] = None,
    input_text: Annotated[
        str | None,
        typer.Option(
            "--input",
            help="Additional input text for the gate response.",
        ),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            help="Auth token (also reads from CONDUCTOR_GATE_TOKEN env var).",
        ),
    ] = None,
) -> None:
    """Resolve a parked human gate from the command line.

    Sends a gate response to a running workflow's web dashboard via HTTP.
    Use this when the dashboard UI is unreachable (e.g. SSH session).

    \b
    Examples:
        conductor gate respond --port 8080 --choice approve
        conductor gate respond -p 8080 -c reject --agent review-gate
        conductor gate respond -p 8080 -c approve --token secret123
        conductor gate respond -p 8080 -c approve --input "Looks good"
    """
    _gate_respond_impl(port, choice, agent, input_text, token)


def _gate_respond_impl(
    port: int,
    choice: str,
    agent: str | None,
    input_text: str | None,
    token: str | None,
) -> None:
    """Send a gate response to a running dashboard over HTTP.

    Shared implementation behind ``conductor gate respond`` and the
    deprecated ``conductor gate-respond`` alias, so both stay in lockstep.
    """
    base_url = f"http://127.0.0.1:{port}"

    # Resolve token from flag or environment variable
    resolved_token = token or os.environ.get("CONDUCTOR_GATE_TOKEN")

    # Auto-discover agent name if not provided
    if agent is None:
        try:
            resp = httpx.get(f"{base_url}/api/gate-status", timeout=5)
            resp.raise_for_status()
            status = resp.json()
            if not status.get("waiting"):
                console.print(f"[yellow]No gate is currently waiting on port {port}.[/yellow]")
                raise typer.Exit(code=1)
            agent = status["agent_name"]
        except httpx.ConnectError:
            console.print(
                f"[bold red]Error:[/bold red] Cannot connect to dashboard on port {port}. "
                "Is the workflow running with --web or --web-bg?"
            )
            raise typer.Exit(code=1) from None
        except httpx.HTTPError as exc:
            console.print(f"[bold red]Error:[/bold red] Failed to query gate status: {exc}")
            raise typer.Exit(code=1) from None

    # Build request body
    body: dict[str, Any] = {
        "agent_name": agent,
        "selected_value": choice,
    }
    if input_text is not None:
        body["additional_input"] = input_text

    # Send the token in the Authorization header (not the body) so it is not
    # captured in request-body logs and is compared in constant time server-side.
    headers: dict[str, str] = {}
    if resolved_token is not None:
        headers["Authorization"] = f"Bearer {resolved_token}"

    # Send gate response
    try:
        resp = httpx.post(f"{base_url}/api/gate-respond", json=body, headers=headers, timeout=10)
    except httpx.ConnectError:
        console.print(
            f"[bold red]Error:[/bold red] Cannot connect to dashboard on port {port}. "
            "Is the workflow running with --web or --web-bg?"
        )
        raise typer.Exit(code=1) from None
    except httpx.HTTPError as exc:
        console.print(f"[bold red]Error:[/bold red] Request failed: {exc}")
        raise typer.Exit(code=1) from None

    if resp.status_code == 403:
        console.print(
            "[bold red]Error:[/bold red] Authentication failed. "
            "Provide a valid token with --token or CONDUCTOR_GATE_TOKEN env var."
        )
        raise typer.Exit(code=1)
    if resp.status_code == 409:
        detail = resp.json().get("error", "Gate is not waiting for this response")
        console.print(f"[bold red]Error:[/bold red] {detail}")
        raise typer.Exit(code=1)
    if resp.status_code == 422:
        detail = resp.json().get("error", "Validation error")
        console.print(f"[bold red]Error:[/bold red] {detail}")
        raise typer.Exit(code=1)
    if resp.status_code != 200:
        console.print(
            f"[bold red]Error:[/bold red] Unexpected response ({resp.status_code}): {resp.text}"
        )
        raise typer.Exit(code=1)

    console.print(
        f"[green]Gate resolved:[/green] agent=[cyan]{agent}[/cyan] choice=[cyan]{choice}[/cyan]"
    )
