"""Typer subcommand group for inspecting pricing overrides."""

from __future__ import annotations

import typer

from conductor.config.user_pricing import USER_PRICING_ENV_VAR, get_user_pricing_path

pricing_app = typer.Typer(
    name="pricing",
    help="Inspect machine-wide pricing overrides.",
    no_args_is_help=True,
)


@pricing_app.command("path")
def path() -> None:
    """Print the path conductor would read for user-level pricing.

    Honors the ``CONDUCTOR_PRICING_FILE`` environment variable.
    """
    target = get_user_pricing_path()
    typer.echo(str(target))
    if not target.exists():
        typer.echo(
            f"(File does not exist; create it or set {USER_PRICING_ENV_VAR}.)",
            err=True,
        )
