"""Typer subcommand group for the Conductor fleet manager.

``conductor fleet list`` is the non-interactive half of the design's *CLI
surface* (see ``docs/projects/fleet-manager/fleet-manager.design.md``): a
Rich table over every live run record, requiring no optional dependency.

``conductor fleet`` (bare, no subcommand) launches the interactive Textual
TUI (Fleet Manager E7) — this is the *One deliberate deviation* the design
calls out: the other three sub-apps (``checkpoint`` / ``registry`` /
``gate``) set ``no_args_is_help=True`` since they have no sensible default
action, whereas the TUI *is* the feature here and is the hot path. The TUI
requires the optional ``tui`` extra; when ``textual`` isn't installed, the
bare invocation prints an install hint and exits non-zero rather than
raising an ``ImportError`` traceback — mirroring the established
availability-flag pattern used for other optional SDK dependencies (see
``providers/aca.py``'s ``AZURE_IDENTITY_AVAILABLE``).

That hint is resolved from the detected install context
(:func:`conductor.install_hint.install_command`) rather than hardcoded: a
uv tool venv is not pip-managed and ``conductor-cli`` is not on PyPI, so a
hardcoded ``pip install`` string cannot work on the documented install
path (issue #441).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table
from rich.text import Text

from conductor.console import make_console, styled
from conductor.install_hint import install_command

logger = logging.getLogger(__name__)

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
    r"""Manage the fleet of running Conductor workflows.

    With no subcommand, this launches the interactive TUI. Requires the
    `tui` extra; without it, prints the install command for how this
    Conductor was installed and exits rather than launching.

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
                    "the 'tui' extra."
                )
            )
            # soft_wrap so rich never inserts a hard newline mid-command: the
            # whole point of this line is that it can be copied and pasted,
            # and the resolved uv spec is longer than a default terminal.
            console.print(
                styled("Install with: [cyan]{}[/cyan]", install_command("tui")),
                soft_wrap=True,
            )
            raise typer.Exit(code=1)

        from conductor.fleet.tui.app import FleetApp

        FleetApp().run()


@fleet_app.command("list")
def list_runs(
    live_only: Annotated[
        bool,
        typer.Option(
            "--live",
            help=(
                "List only currently-running workflows, reproducing this command's "
                "pre-completed-runs scope exactly."
            ),
        ),
    ] = False,
) -> None:
    r"""List every live Conductor run, plus recently-completed ones.

    Shows each run's workflow, mode (foreground, foreground+web, or
    background), status, PID, dashboard port, and start time. Discovers
    live runs the same way `conductor stop` does — via the Fleet Manager run
    record (`~/.conductor/runs/`) — so foreground runs show up here too, not
    just `--web-bg` ones.

    Also lists recently-completed runs (`completed`/`failed`, bounded by
    \[fleet.retention].keep_last) — pass `--live` to see only currently-
    running workflows, matching this command's scope before completed runs
    were added.

    \b
    Examples:
        conductor fleet list
        conductor fleet list --live
    """
    from conductor.fleet.records import TerminalRunRecord, read_run_records

    records = read_run_records()

    completed: list[TerminalRunRecord] = []
    if not live_only:
        from conductor.fleet.records import read_terminal_records

        keep_last = _resolve_completed_keep_last()
        completed = read_terminal_records(limit=keep_last if keep_last >= 1 else None)

    if not records and not completed:
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
            # streamed event-log scan per row; use `conductor fleet`'s
            # TUI (or `--web`) for the finer-grained status.
            # A completed row, added below, carries its real terminal
            # status instead.
            "running",
            str(record.pid),
            str(record.port) if record.port is not None else "—",
            record.started_at or "?",
        )

    for terminal in completed:
        table.add_row(
            terminal.workflow_name or Path(terminal.workflow_path or "unknown").stem or "unknown",
            "—",
            _terminal_status_label(terminal.status),
            "—",
            "—",
            terminal.started_at or "?",
        )

    output_console.print(table)


def _resolve_completed_keep_last() -> int:
    """Return the configured retention ``keep_last`` bound for completed rows.

    Mirrors ``conductor.fleet.history._resolve_keep_last``'s own
    never-break-on-bad-settings contract: a malformed
    ``~/.conductor/config.toml`` (or any other failure loading settings)
    must not break `fleet list`, so a failure loading settings falls back
    to ``FleetRetentionSettings.keep_last``'s own default rather than
    raising.
    """
    from conductor.settings import load_settings

    try:
        return load_settings().fleet.retention.keep_last
    except Exception:
        logger.warning(
            "Failed to load Conductor settings for fleet list's retention bound; using default",
            exc_info=True,
        )
        return 200


def _terminal_status_label(status: str) -> str:
    """Map a raw status value to this table's Status cell text.

    ``"running"`` passes through unchanged (every live record's coarse
    status, per `list_runs`'s own comment). A completed row's
    :class:`~conductor.fleet.records.TerminalRunRecord.status` is written as
    ``"success"``/``"failed"`` by ``cli/run.py`` (a forward-compat
    ``"unknown"`` substitutes for an absent field on load) -- this renders
    ``"success"`` as ``"completed"`` to match the vocabulary the rest of the
    Fleet Manager already uses (``fleet/tui/theme.py``, ``fleet/history.py``'s
    ``HistoryOutcome``) rather than introducing a second word for the same
    outcome.
    """
    return "completed" if status == "success" else status


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
    r"""Prune old event logs under $TMPDIR/conductor/.

    This is the explicit manual entry point for event-log retention (E5 —
    see docs/projects/fleet-manager/fleet-manager.design.md's *Second-order
    cleanup*), and runs regardless of whether the opportunistic startup
    sweep is enabled via \[fleet.retention].enabled in
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

    # A failed sweep must not render as "nothing to do". `prune_event_logs`
    # never raises (the opportunistic startup sweep depends on that), so the
    # explicit CLI reader is the layer that has to tell the two apart --
    # otherwise `conductor fleet prune || alert` never fires while the disk
    # fills, and the symlink-tamper refusal is reported as an empty sweep.
    if result.error is not None:
        console.print(
            styled(
                "[bold red]Error:[/bold red] the event-log sweep did not complete; "
                "some files may not have been deleted: {}",
                result.error,
            )
        )
        raise typer.Exit(code=1)

    if not result.deleted and not result.failed:
        output_console.print(Text.from_markup("[dim]Nothing to prune.[/dim]"))
        return

    if result.deleted:
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

    # Reported last so it is the final thing on screen, and exits non-zero:
    # a systematic cause (a read-only or root-owned log directory) refuses
    # the same files on every run, and listing only the successes above
    # would present that as a working sweep.
    if result.failed:
        console.print(
            styled("[bold red]Failed to delete {} file(s):[/bold red]", len(result.failed))
        )
        for path, reason in result.failed:
            console.print(styled("  {} — {}", path, reason))
        raise typer.Exit(code=1)
