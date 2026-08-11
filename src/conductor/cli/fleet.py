"""Typer subcommand group for the Conductor fleet manager.

``conductor fleet list`` is the non-interactive half of the design's *CLI
surface* (see ``docs/projects/fleet-manager/fleet-manager.design.md``): a
Rich table over every live run record, requiring no optional dependency.

``conductor fleet`` (bare, no subcommand) launches the interactive Textual
TUI (Fleet Manager E7) — this is the *One deliberate deviation* the design
calls out: the other three sub-apps (``checkpoint`` / ``registry`` /
``gate``) set ``no_args_is_help=True`` since they have no sensible default
action, whereas the TUI *is* the feature here and is the hot path. The TUI
requires the optional ``tui`` extra (``pip install 'conductor-cli[tui]'``);
when ``textual`` isn't installed, the bare invocation prints an install
hint and exits non-zero rather than raising an ``ImportError`` traceback —
mirroring the established availability-flag pattern used for other optional
SDK dependencies (see ``providers/aca.py``'s ``AZURE_IDENTITY_AVAILABLE``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table
from rich.text import Text

from conductor.console import make_console, styled

# `textual` is an optional dependency (the `tui` extra) — this module is
# imported unconditionally at every `conductor` invocation (via
# `cli/app.py`), so it must never itself require `textual` to import
# successfully. Only the bare-invocation callback below actually needs it,
# and only there is the availability flag consulted.
try:
    import textual  # noqa: F401

    TEXTUAL_AVAILABLE = True
except ImportError:
    TEXTUAL_AVAILABLE = False

fleet_app = typer.Typer(
    name="fleet",
    help="Monitor and manage running Conductor workflows.",
    invoke_without_command=True,
)

console = make_console(stderr=True)
output_console = make_console()


@fleet_app.callback()
def fleet_main(ctx: typer.Context) -> None:
    """Manage the fleet of running Conductor workflows.

    With no subcommand, this launches the interactive TUI. Requires the
    `tui` extra (`pip install 'conductor-cli[tui]'`); without it, prints an
    install hint and exits rather than launching.

    \b
    Examples:
        conductor fleet
        conductor fleet list
    """
    if ctx.invoked_subcommand is None:
        if not TEXTUAL_AVAILABLE:
            console.print(
                Text.from_markup(
                    "[bold red]Error:[/bold red] the interactive fleet manager requires "
                    "the 'tui' extra.\n"
                    "Install with: [cyan]pip install 'conductor-cli\\[tui]'[/cyan]"
                )
            )
            raise typer.Exit(code=1)

        from conductor.fleet.tui.app import FleetApp

        FleetApp().run()


@fleet_app.command("list")
def list_runs() -> None:
    """List every live Conductor run.

    Shows each run's workflow, mode (foreground, foreground+web, or
    background), status, PID, dashboard port, and start time. Discovers
    runs the same way `conductor stop` does — via the Fleet Manager run
    record (`~/.conductor/runs/`) — so foreground runs show up here too,
    not just `--web-bg` ones.

    \b
    Examples:
        conductor fleet list
    """
    from conductor.fleet.records import read_run_records

    records = read_run_records()

    if not records:
        output_console.print(Text.from_markup("[dim]No runs found.[/dim]"))
        return

    table = Table(title="Fleet")
    table.add_column("Workflow", style="cyan")
    table.add_column("Mode", style="magenta")
    table.add_column("Status", style="green")
    table.add_column("PID", style="yellow")
    table.add_column("Port", style="blue")
    table.add_column("Started", style="dim")

    for record in records:
        table.add_row(
            Path(record.workflow_path or "unknown").stem or "unknown",
            record.mode,
            # All records returned by read_run_records() are live (it
            # filters to processes that pass is_process_alive), but a live
            # run may not be plainly "running" -- e.g. it can be blocked
            # at a human gate. This non-interactive table intentionally
            # stays coarse-grained ("running") rather than deriving the
            # richer gate/status vocabulary `derive_run_summary` (E6)
            # computes for the TUI's Runs screen, which also requires a
            # bounded event-log tail read per row; use `conductor fleet`'s
            # TUI (or `--web`) for the finer-grained status.
            "running",
            str(record.pid),
            str(record.port) if record.port is not None else "—",
            record.started_at or "?",
        )

    output_console.print(table)


@fleet_app.command("prune")
def prune(
    keep_last: Annotated[
        int | None,
        typer.Option(
            "--keep-last",
            help=(
                "Number of most-recent event logs to retain, overriding "
                r"\[fleet.retention].keep_last from ~/.conductor/config.toml."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="List what would be pruned without deleting anything.",
        ),
    ] = False,
) -> None:
    """Prune old event logs under $TMPDIR/conductor/.

    This is the explicit manual entry point for event-log retention (E5 —
    see docs/projects/fleet-manager/fleet-manager.design.md's *Second-order
    cleanup*), and runs regardless of whether the opportunistic startup
    sweep is enabled via [fleet.retention].enabled in
    ~/.conductor/config.toml. Never deletes checkpoints or a live run's
    event log. Pruning an event log makes that run unavailable to
    `conductor replay`.

    \b
    Examples:
        conductor fleet prune
        conductor fleet prune --dry-run
        conductor fleet prune --keep-last 50
    """
    from conductor.exceptions import ConductorError
    from conductor.fleet.retention import prune_event_logs
    from conductor.settings import load_settings

    resolved_keep_last = keep_last
    if resolved_keep_last is None:
        try:
            settings = load_settings()
        except ConductorError as e:
            console.print(styled("[bold red]Error:[/bold red] {}", e))
            raise typer.Exit(code=1) from None
        resolved_keep_last = settings.fleet.retention.keep_last

    result = prune_event_logs(keep_last=resolved_keep_last, dry_run=dry_run)

    if not result.deleted:
        output_console.print(Text.from_markup("[dim]Nothing to prune.[/dim]"))
        return

    verb = "Would delete" if dry_run else "Deleted"
    output_console.print(styled("{} {} file(s):", verb, len(result.deleted)))
    for f in result.deleted:
        output_console.print(styled("  [dim]{}[/dim]", f))

    if result.skipped_live:
        output_console.print(
            styled(
                "[dim]Skipped {} file(s) still referenced by a live run.[/dim]",
                len(result.skipped_live),
            )
        )
