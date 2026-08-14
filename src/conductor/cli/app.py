"""Typer application definition for Conductor CLI.

This module defines the main Typer app and global options.
"""

from __future__ import annotations

import contextvars
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from conductor import __version__
from conductor.console import make_console, styled
from conductor.exceptions import WorkflowTerminated

if TYPE_CHECKING:
    # Typing-only: ``stop()`` imports ``conductor.cli.self_run`` lazily at
    # runtime, matching the existing lazy import of ``conductor.cli.pid``.
    from conductor.cli.self_run import OwnRunPartition
    from conductor.fleet.records import RunRecord

logger = logging.getLogger(__name__)


class ConsoleVerbosity(str, Enum):
    """Console output verbosity level."""

    FULL = "full"  # Default: everything, untruncated
    MINIMAL = "minimal"  # Agent lifecycle + routing + timing only
    SILENT = "silent"  # No progress output at all


# Create the main Typer app
app = typer.Typer(
    name="conductor",
    help="Conductor - Orchestrate multi-agent workflows defined in YAML.",
    add_completion=False,
    no_args_is_help=True,
)

# Register subcommand groups
from conductor.cli.checkpoint import checkpoint_app  # noqa: E402
from conductor.cli.fleet import fleet_app  # noqa: E402
from conductor.cli.gate import gate_app  # noqa: E402
from conductor.cli.plugin import plugin_app  # noqa: E402
from conductor.cli.registry import registry_app  # noqa: E402

app.add_typer(registry_app, rich_help_panel="Environment")
app.add_typer(plugin_app, rich_help_panel="Environment")
app.add_typer(gate_app, rich_help_panel="Interact")
app.add_typer(checkpoint_app, rich_help_panel="State")
app.add_typer(fleet_app, rich_help_panel="Run & Recover")

# Rich console for formatted output
console = make_console(stderr=True)
output_console = make_console()

# Stop-ladder timings (issue #344). A stop request is only an acknowledgement,
# so each rung is followed by a bounded wait before escalating. The graceful
# rung gets the longest budget because it is the only one that lets the run
# flush a resume checkpoint. Mirrors the child-termination timings already used
# at launch in ``bg_runner._terminate_child`` (5s polite, 2s forceful).
_GRACEFUL_TIMEOUT = 5.0
_SIGNAL_TIMEOUT = 5.0
_TERMINATE_TIMEOUT = 2.0
# Localhost HTTP calls to the run's own dashboard; matches ``cli/gate.py``.
_IDENTITY_TIMEOUT = 5.0

# Context variable for verbose mode (default True - show progress output)
verbose_mode: contextvars.ContextVar[bool] = contextvars.ContextVar("verbose_mode", default=True)

# Context variable for full verbose mode (default True - show full details)
full_mode: contextvars.ContextVar[bool] = contextvars.ContextVar("full_mode", default=True)

# Context variable for console verbosity level
console_verbosity: contextvars.ContextVar[ConsoleVerbosity] = contextvars.ContextVar(
    "console_verbosity", default=ConsoleVerbosity.FULL
)


def is_verbose() -> bool:
    """Check if verbose mode is enabled (default True)."""
    return verbose_mode.get()


def is_full() -> bool:
    """Check if full verbose mode is enabled.

    Full mode is the default. When enabled, prompts are shown untruncated and
    additional details like tool arguments and reasoning are displayed.
    Use --quiet to disable full mode while keeping progress output.
    """
    return full_mode.get()


def format_error(error: Exception) -> Panel:
    """Format an exception for Rich console display.

    Creates a styled Panel with error type, message, location (if available),
    and suggestion (if available).

    Args:
        error: The exception to format.

    Returns:
        Rich Panel with formatted error content.
    """
    from conductor.exceptions import ConductorError

    # Build error content
    content = Text()

    # Error message (red)
    error_message = str(error).split("\n")[0]  # First line only for main message
    content.append(error_message, style="bold red")

    # Add location info if available
    if isinstance(error, ConductorError):
        if error.file_path or error.line_number:
            content.append("\n\n")
            content.append("📍 Location: ", style="yellow")
            if error.file_path:
                content.append(error.file_path, style="cyan")
            if error.line_number:
                if error.file_path:
                    content.append(":", style="yellow")
                content.append(f"line {error.line_number}", style="cyan")

        # Add field path for configuration errors
        if hasattr(error, "field_path") and error.field_path:
            content.append("\n")
            content.append("📋 Field: ", style="yellow")
            content.append(str(error.field_path), style="cyan")

        # Add suggestion if available
        if error.suggestion:
            content.append("\n\n")
            content.append("💡 Suggestion: ", style="green")
            content.append(error.suggestion, style="white")

    # Get error type name for the panel title
    error_type = type(error).__name__
    if isinstance(error, ConductorError) and hasattr(error, "error_type"):
        error_type = error.error_type

    return Panel(
        content,
        title=styled("[bold red]❌ {}[/bold red]", error_type),
        border_style="red",
        padding=(1, 2),
    )


def print_error(error: Exception) -> None:
    """Print a formatted error to stderr.

    Args:
        error: The exception to print.
    """
    from conductor.exceptions import ConductorError

    if isinstance(error, ConductorError):
        console.print(format_error(error))
    else:
        # For non-Conductor errors, still format nicely
        content = Text()
        content.append(str(error), style="red")
        panel = Panel(
            content,
            title=styled("[bold red]❌ {}[/bold red]", type(error).__name__),
            border_style="red",
            padding=(1, 2),
        )
        console.print(panel)


_INTERACTIVE_STEP_TYPES = ("human_gate", "questions")
"""Step types that park the workflow waiting on a human."""


def _bg_capture_logs(record: RunRecord) -> tuple[str | None, str | None]:
    """Locate a background run's captured stderr/stdout logs.

    These paths were dropped from the discovery schema when ``stop``/``status``
    moved onto :class:`RunRecord` (which carries the authoritative
    ``event_log_path`` and, by design, exactly nine fields). They are still
    worth reporting -- they are where a silently-crashed ``--web-bg`` child
    leaves its traceback -- so they are located rather than stored.

    Located by globbing on ``run_id`` rather than by rewriting
    ``event_log_path``'s suffix: the parent stamps the capture-log filenames
    at launch and the child stamps its own events log a moment later, so the
    two share a ``run_id`` but **not** a timestamp, and a stem swap silently
    yields a path that does not exist.

    Args:
        record: The run whose capture logs to locate.

    Returns:
        ``(stderr_log, stdout_log)``, each ``None`` when absent -- which is
        the normal case for a foreground run, since only ``--web-bg``
        redirects a child's streams to files.
    """
    if not record.run_id or not record.event_log_path:
        return None, None
    try:
        log_dir = Path(record.event_log_path).parent
        stderr = next(iter(sorted(log_dir.glob(f"*-{record.run_id}.bg.stderr.log"))), None)
        stdout = next(iter(sorted(log_dir.glob(f"*-{record.run_id}.bg.stdout.log"))), None)
    except OSError:
        # A listing failure must not take down `status`, whose entire promise
        # is to observe without disturbing anything.
        return None, None
    return (str(stderr) if stderr else None, str(stdout) if stdout else None)


def _optional_str(value: object) -> str | None:
    """Coerce a PID-file field to ``str | None`` for JSON output.

    A PID file written before ``run_id``/``stderr_log``/``stdout_log``
    existed has the key absent; ``write_pid_file`` with no explicit value
    writes an empty string. Both should surface as JSON ``null`` rather than
    ``""``, so a scripted reader can distinguish "no id recorded" from an
    id that happens to be the empty string — which never legitimately
    occurs, but collapsing it to ``""`` would make it indistinguishable
    from "field absent" if it ever did.

    Args:
        value: The raw value read from the PID file JSON.

    Returns:
        ``value`` if it is a non-empty string, otherwise ``None``.
    """
    return value if isinstance(value, str) and value else None


def _workflow_has_human_gate(workflow_path: Path) -> bool:
    """Return True if the workflow defines any step that waits on a human.

    Used to decide whether to print the ``--web-bg`` gate-resolution notice
    after forking the background child (issue #286). Config-load failures
    return ``False`` so the normal run path surfaces the real error instead
    of this best-effort probe.

    Covers ``questions`` as well as ``human_gate`` — both park the run on the
    dashboard, so omitting either would leave a ``--web-bg`` user with a
    silently stalled workflow and no notice explaining why.
    """
    try:
        from conductor.config.loader import load_config

        config = load_config(workflow_path)
    except Exception:  # noqa: BLE001 — defer real validation to the loader path
        logger.debug("Best-effort human_gate probe failed to load %s", workflow_path, exc_info=True)
        return False
    return any(getattr(a, "type", None) in _INTERACTIVE_STEP_TYPES for a in config.agents) or any(
        getattr(getattr(fe, "agent", None), "type", None) in _INTERACTIVE_STEP_TYPES
        for fe in config.for_each
    )


def _print_web_bg_human_gate_notice(url: str) -> None:
    """Tell the user how to resolve human gates in a ``--web-bg`` run.

    Background human gates used to abort the launch (the detached child has
    no stdin to prompt on). They are now resolvable from the dashboard or the
    ``conductor gate respond`` CLI (issue #286), so instead of blocking we
    point at both so a parked run doesn't look stuck. Printed only in verbose
    mode — ``--silent`` suppresses all bg output, including the dashboard URL
    on the line above this notice.
    """
    from urllib.parse import urlparse

    # ``url`` is always a live, bound ``http://127.0.0.1:<port>`` by the time
    # this runs — ``_finalize_background_launch`` in bg_runner.py confirms the
    # child is listening on that exact port before returning it — so ``.port``
    # is always a valid 1-65535 int and this can't raise. Fall back to a
    # placeholder anyway in case that invariant is ever relaxed.
    port = urlparse(url).port
    port_hint = str(port) if port is not None else "<port>"
    console.print(
        styled(
            "[yellow]This workflow contains steps that wait for "
            "you[/yellow] (human_gate / questions). Resolve them from "
            "the dashboard above, or run [bold]conductor gate respond "
            "--port {} --choice <value>[/bold].",
            port_hint,
        )
    )


def version_callback(value: bool) -> None:
    """Display version information and exit."""
    if value:
        output_console.print(f"Conductor v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-v",
            help="Show version and exit.",
            callback=version_callback,
            is_eager=True,
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            "-q",
            help="Minimal output: agent lifecycle and routing only.",
        ),
    ] = False,
    silent: Annotated[
        bool,
        typer.Option(
            "--silent",
            "-s",
            help="No progress output. Only JSON result on stdout.",
        ),
    ] = False,
) -> None:
    """Conductor - Orchestrate multi-agent workflows defined in YAML."""
    if quiet and silent:
        raise typer.BadParameter("--quiet and --silent are mutually exclusive")
    if silent:
        verbosity = ConsoleVerbosity.SILENT
    elif quiet:
        verbosity = ConsoleVerbosity.MINIMAL
    else:
        verbosity = ConsoleVerbosity.FULL
    console_verbosity.set(verbosity)
    verbose_mode.set(verbosity != ConsoleVerbosity.SILENT)
    full_mode.set(verbosity == ConsoleVerbosity.FULL)

    # Show update hint (deferred import to avoid startup overhead)
    if console.is_terminal and verbosity != ConsoleVerbosity.SILENT:
        import sys

        # Skip when the subcommand is 'update' or 'doctor' — both surface
        # update status in their own output (doctor in its env section), so
        # the startup hint would be redundant noise.
        args = sys.argv[1:]
        subcommand = next((a for a in args if not a.startswith("-")), None)
        if subcommand not in ("update", "doctor"):
            from conductor.cli.update import check_for_update_hint

            check_for_update_hint(console)


@app.command(rich_help_panel="Run & Recover")
def run(
    workflow: Annotated[
        str,
        typer.Argument(
            # Typer renders help through rich (``rich_markup_mode="rich"``),
            # so this string *is* markup-parsed and the console convention
            # does not reach it. ``[@registry]`` starts with ``@``, which rich
            # reads as a tag and deletes -- the syntax this line documents was
            # missing from ``--help`` entirely. Escaped rather than wrapped in
            # ``Text``: typer takes a ``str`` here (#406).
            help=r"Workflow file path or registry reference (name\[@registry]\[@version]).",
        ),
    ],
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            "-p",
            help="Override the provider specified in the workflow (e.g., 'copilot').",
        ),
    ] = None,
    raw_inputs: Annotated[
        list[str] | None,
        typer.Option(
            "--input",
            "-i",
            help="Workflow inputs in name=value format. Can be repeated.",
        ),
    ] = None,
    raw_inputs_json: Annotated[
        list[str] | None,
        typer.Option(
            "--input-json",
            hidden=True,
            help=(
                "Internal: workflow inputs in name=<json> format, strictly "
                "JSON-decoded. Used by the background launcher to round-trip "
                "already-typed values."
            ),
        ),
    ] = None,
    raw_metadata: Annotated[
        list[str] | None,
        typer.Option(
            "--metadata",
            "-m",
            help=(
                "Workflow metadata in key=value format. "
                "Merged on top of YAML metadata. Can be repeated."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show execution plan without running the workflow.",
        ),
    ] = False,
    skip_gates: Annotated[
        bool,
        typer.Option(
            "--skip-gates",
            help="Auto-select first option at human gates (for automation).",
        ),
    ] = False,
    log_file: Annotated[
        str | None,
        typer.Option(
            "--log-file",
            "-l",
            help=(
                "Write full debug output to a file. "
                "Pass a file path or 'auto' for auto-generated temp file."
            ),
        ),
    ] = None,
    no_interactive: Annotated[
        bool,
        typer.Option(
            "--no-interactive",
            help="Disable interactive interrupt capability (Esc to pause).",
        ),
    ] = False,
    web: Annotated[
        bool,
        typer.Option(
            "--web",
            help="Start a real-time web dashboard for workflow visualization.",
        ),
    ] = False,
    web_port: Annotated[
        int,
        typer.Option(
            "--web-port",
            help="Port for the web dashboard (0 = auto-select).",
        ),
    ] = 0,
    web_bg: Annotated[
        bool,
        typer.Option(
            "--web-bg",
            help=(
                "Run workflow + dashboard in a background process. "
                "Prints the dashboard URL and exits immediately. "
                "Does not require --web."
            ),
        ),
    ] = False,
    workspace_instructions: Annotated[
        bool,
        typer.Option(
            "--workspace-instructions",
            help=(
                "Auto-discover workspace instruction files and prepend them to "
                "all agent prompts. Discovers AGENTS.md, CLAUDE.md, "
                ".github/copilot-instructions.md, and "
                ".github/instructions/**/*.instructions.md (recursive; only "
                "files marked 'applyTo: \"**\"' in YAML frontmatter are "
                "included)."
            ),
        ),
    ] = False,
    raw_instructions: Annotated[
        list[str] | None,
        typer.Option(
            "--instructions",
            help="Path to instruction file(s) to prepend to all agent prompts. Can be repeated.",
        ),
    ] = None,
    print_loaded_instructions: Annotated[
        bool,
        typer.Option(
            "--print-loaded-instructions",
            help=(
                "Print the resolved list of workspace instruction files (with "
                "their scope and reason for inclusion) to stderr before running "
                "the workflow. Useful for debugging why an instruction file is "
                "or isn't being picked up by --workspace-instructions. Has no "
                "effect unless --workspace-instructions is also set."
            ),
        ),
    ] = False,
) -> None:
    """Run a workflow from a YAML file.

    Execute a multi-agent workflow defined in the specified YAML file.
    Workflow inputs can be provided using --input flags.
    Metadata can be provided using --metadata flags (merged on top of YAML metadata).

    \b
    Examples:
        conductor run workflow.yaml
        conductor run workflow.yaml --input question="What is Python?"
        conductor run workflow.yaml -i question="Hello" -i context="Programming"
        conductor run workflow.yaml --metadata tracker=ado -m work_item_id=1814
        conductor run workflow.yaml --provider copilot
        conductor run workflow.yaml --dry-run
        conductor run workflow.yaml --skip-gates
        conductor run workflow.yaml --log-file auto
        conductor run workflow.yaml --log-file debug.log
        conductor --silent run workflow.yaml --log-file auto
        conductor run workflow.yaml --no-interactive
        conductor run workflow.yaml --web
        conductor run workflow.yaml --web --web-port 8080
        conductor run workflow.yaml --web-bg
        conductor run workflow.yaml --workspace-instructions
        conductor run workflow.yaml --instructions AGENTS.md
    """
    import asyncio
    import json

    from conductor.registry.cache import resolve_and_fetch
    from conductor.registry.errors import RegistryError
    from conductor.registry.resolver import resolve_ref

    try:
        workflow_path = resolve_and_fetch(resolve_ref(workflow))
    except RegistryError as e:
        print_error(e)
        raise typer.Exit(code=1) from None

    # Import here to avoid circular imports and defer heavy imports
    from conductor.cli.run import (
        InputCollector,
        build_dry_run_plan,
        display_execution_plan,
        generate_log_path,
        parse_input_flags,
        parse_input_json_flags,
        parse_metadata_flags,
        run_workflow_async,
    )

    # Handle dry-run mode
    if dry_run:
        try:
            plan = build_dry_run_plan(workflow_path)
            display_execution_plan(plan, output_console)
            return
        except Exception as e:
            print_error(e)
            raise typer.Exit(code=1) from None

    # Validate mutually exclusive flags
    if web and web_bg:
        raise typer.BadParameter("--web and --web-bg are mutually exclusive")

    # Collect inputs from both --input and --input.* patterns
    inputs: dict[str, Any] = {}

    # Parse --input name=value style
    if raw_inputs:
        inputs.update(parse_input_flags(raw_inputs))

    # Parse the hidden --input-json name=<json> style. Applied after
    # --input so a value the background launcher round-tripped verbatim
    # wins over the public flag's type-guessing heuristic.
    if raw_inputs_json:
        inputs.update(parse_input_json_flags(raw_inputs_json))

    # Also parse --input.name=value style from sys.argv
    inputs.update(InputCollector.extract_from_args())

    # Parse --metadata key=value flags (no type coercion — values stay as strings)
    cli_metadata: dict[str, str] = {}
    if raw_metadata:
        cli_metadata.update(parse_metadata_flags(raw_metadata))

    # Resolve log file path
    resolved_log_file: Path | None = None
    if log_file is not None:
        if log_file.lower() == "auto":
            resolved_log_file = generate_log_path(workflow_path.stem)
        else:
            resolved_log_file = Path(log_file)

    # Handle --web-bg: fork a background process and exit immediately
    if web_bg:
        # Background human gates are now resolvable from the dashboard /
        # ``conductor gate respond`` (issue #286), so we no longer abort the
        # launch — we just note how to resolve them once the URL is known.
        notify_gate = not skip_gates and _workflow_has_human_gate(workflow_path)
        from conductor.cli.bg_runner import launch_background

        try:
            launch = launch_background(
                workflow_path=workflow_path,
                inputs=inputs,
                provider_override=provider,
                skip_gates=skip_gates,
                log_file=resolved_log_file,
                no_interactive=True,  # Always non-interactive in background
                web_port=web_port,
                metadata=cli_metadata,
                workspace_instructions=workspace_instructions,
                cli_instructions=raw_instructions,
                print_loaded_instructions=print_loaded_instructions,
            )
            if is_verbose():
                console.print(styled("[bold cyan]Dashboard:[/bold cyan] {}", launch.url))
                console.print(styled("[dim]Child stderr log: {}[/dim]", launch.stderr_log))
                console.print(
                    Text.from_markup(
                        "[dim]Workflow running in background. Dashboard auto-shuts down after "
                        "workflow completes and all clients disconnect.[/dim]"
                    )
                )
                if notify_gate:
                    _print_web_bg_human_gate_notice(launch.url)
        except Exception as e:
            print_error(e)
            raise typer.Exit(code=1) from None
        return

    try:
        # Run the workflow
        result = asyncio.run(
            run_workflow_async(
                workflow_path,
                inputs,
                provider,
                skip_gates,
                resolved_log_file,
                no_interactive,
                web=web,
                web_port=web_port,
                web_bg=web_bg,
                metadata=cli_metadata,
                workspace_instructions=workspace_instructions,
                cli_instructions=raw_instructions,
                print_loaded_instructions=print_loaded_instructions,
            )
        )

        # Output as JSON to stdout
        output_console.print_json(json.dumps(result))

    except WorkflowTerminated as e:
        # Explicit `type: terminate` with `status: failed`. Print the
        # rendered final output so downstream tooling can read it, surface
        # the reason (and optional suggestion) as a user-facing message,
        # then exit non-zero. `default=str` keeps the JSON dump robust
        # against any output value that isn't directly JSON-serialisable —
        # today everything goes through `_maybe_parse_json` so it round-
        # trips, but a future custom Jinja filter or output_template
        # transform could produce a non-trivial Python object that would
        # otherwise crash the CLI here and lose the termination message.
        try:
            output_console.print_json(json.dumps(e.output, default=str))
        except (TypeError, ValueError) as json_exc:
            logger.exception("Failed to serialise terminate output")
            console.print(
                styled(
                    "[yellow]Warning:[/yellow] could not serialise terminate output: {}", json_exc
                )
            )
        console.print(
            styled("[red]Workflow terminated[/red] at '{}': {}", e.terminated_by, e.reason)
        )
        if e.suggestion:
            console.print(styled("[dim]Suggestion: {}[/dim]", e.suggestion))
        raise typer.Exit(code=1) from None
    except Exception as e:
        print_error(e)
        raise typer.Exit(code=1) from None


@app.command(rich_help_panel="Author & Inspect")
def validate(
    workflow: Annotated[
        str,
        typer.Argument(
            # Typer renders help through rich (``rich_markup_mode="rich"``),
            # so this string *is* markup-parsed and the console convention
            # does not reach it. ``[@registry]`` starts with ``@``, which rich
            # reads as a tag and deletes -- the syntax this line documents was
            # missing from ``--help`` entirely. Escaped rather than wrapped in
            # ``Text``: typer takes a ``str`` here (#406).
            help=r"Workflow file path or registry reference (name\[@registry]\[@version]).",
        ),
    ],
) -> None:
    """Validate a workflow YAML file without executing it.

    Checks the workflow file for:
    - Valid YAML syntax
    - Valid schema structure
    - Valid agent references
    - Valid route targets

    \b
    Examples:
        conductor validate workflow.yaml
        conductor validate ./examples/my-workflow.yaml
        conductor validate qa-bot@team@1.0.0
    """
    from conductor.registry.cache import resolve_and_fetch
    from conductor.registry.errors import RegistryError
    from conductor.registry.resolver import resolve_ref

    try:
        workflow_path = resolve_and_fetch(resolve_ref(workflow))
    except RegistryError as e:
        print_error(e)
        raise typer.Exit(code=1) from None

    from conductor.cli.validate import (
        display_validation_success,
        validate_workflow,
    )

    is_valid, config = validate_workflow(workflow_path, output_console)

    if is_valid and config is not None:
        display_validation_success(config, workflow_path, output_console)
    else:
        raise typer.Exit(code=1)


@app.command(rich_help_panel="Author & Inspect")
def show(
    workflow: Annotated[
        str,
        typer.Argument(
            # Typer renders help through rich (``rich_markup_mode="rich"``),
            # so this string *is* markup-parsed and the console convention
            # does not reach it. ``[@registry]`` starts with ``@``, which rich
            # reads as a tag and deletes -- the syntax this line documents was
            # missing from ``--help`` entirely. Escaped rather than wrapped in
            # ``Text``: typer takes a ``str`` here (#406).
            help=r"Workflow file path or registry reference (name\[@registry]\[@version]).",
        ),
    ],
) -> None:
    """Show details and inputs for a workflow.

    Accepts a local file path or a registry reference. Displays the workflow
    name, description, and a table of input parameters.

    \b
    Examples:
        conductor show ./my-workflow.yaml
        conductor show qa-bot
        conductor show qa-bot@my-registry@1.0.0
    """
    from conductor.registry.cache import resolve_and_fetch
    from conductor.registry.errors import RegistryError
    from conductor.registry.resolver import resolve_ref

    try:
        ref = resolve_ref(workflow)
        if ref.kind == "file":
            assert ref.path is not None
            workflow_path = ref.path
            if not workflow_path.exists():
                console.print(
                    styled("[bold red]Error:[/bold red] Workflow file not found: {}", workflow)
                )
                raise typer.Exit(code=1)
        else:
            workflow_path = resolve_and_fetch(ref)
    except RegistryError as e:
        print_error(e)
        raise typer.Exit(code=1) from None

    try:
        from conductor.config.loader import load_config as load_workflow_config

        config = load_workflow_config(workflow_path)
    except Exception as e:
        console.print(styled("[bold red]Error:[/bold red] Failed to parse workflow: {}", e))
        raise typer.Exit(code=1) from None

    wf = config.workflow
    output_console.print(styled("[bold]Name:[/bold]        {}", wf.name))
    if wf.description:
        output_console.print(styled("[bold]Description:[/bold] {}", wf.description))
    output_console.print(styled("[bold]Entry point:[/bold] {}", wf.entry_point))
    output_console.print(styled("[bold]Source:[/bold]      {}", workflow_path))

    if ref.kind == "registry":
        output_console.print(styled("[bold]Registry:[/bold]    {}", ref.registry_name))
        if ref.ref:
            output_console.print(styled("[bold]Version:[/bold]     {}", ref.ref))

    from rich.table import Table

    # --- Inputs ---
    inputs = wf.input
    if inputs:
        output_console.print()
        table = Table(title="Inputs")
        table.add_column("Name", style="cyan")
        table.add_column("Type", style="green")
        table.add_column("Required", justify="center")
        table.add_column("Default")
        table.add_column("Description")

        for name, input_def in inputs.items():
            required = "✓" if input_def.required else ""
            default = str(input_def.default) if input_def.default is not None else "-"
            table.add_row(name, input_def.type, required, default, input_def.description or "-")

        output_console.print(table)

    # --- Agents ---
    output_console.print()
    agent_table = Table(title="Agents")
    agent_table.add_column("Name", style="cyan")
    agent_table.add_column("Type", style="green")
    agent_table.add_column("Description")
    agent_table.add_column("Routes")

    for agent in config.agents:
        agent_type = agent.type or "agent"
        routes = ", ".join(r.to + (f" (when {r.when})" if r.when else "") for r in agent.routes)
        agent_table.add_row(agent.name, agent_type, agent.description or "-", routes or "-")

    # Include parallel groups
    for pg in config.parallel:
        members = ", ".join(pg.agents)
        agent_table.add_row(pg.name, "parallel", members, "-")

    # Include for-each groups
    for fe in config.for_each:
        agent_table.add_row(fe.name, "for_each", fe.source or "-", "-")

    output_console.print(agent_table)

    # --- Outputs ---
    if config.output:
        output_console.print()
        out_table = Table(title="Outputs")
        out_table.add_column("Field", style="cyan")
        out_table.add_column("Template")

        for field, template in config.output.items():
            # Truncate long templates
            display = template if len(template) <= 60 else template[:57] + "..."
            out_table.add_row(field, display)

        output_console.print(out_table)

    # Show example run command
    ref_str = workflow if ref.kind == "registry" else str(workflow_path)
    if inputs:
        input_args = " ".join(f'--input {name}="..."' for name in inputs)
        output_console.print(styled("\n[dim]conductor run {} {}[/dim]", ref_str, input_args))
    else:
        output_console.print(styled("\n[dim]conductor run {}[/dim]", ref_str))


@app.command(rich_help_panel="Run & Recover")
def resume(
    workflow: Annotated[
        str | None,
        typer.Argument(
            # Escaped, not wrapped: typer renders help through rich, so an
            # unescaped ``[@registry]`` is parsed as a tag and deleted (#406).
            help=(
                r"Workflow file path or registry reference (name\[@registry]\[@version]). "
                "Finds the latest checkpoint for this workflow."
            ),
        ),
    ] = None,
    from_checkpoint: Annotated[
        Path | None,
        typer.Option(
            "--from",
            help="Path to a specific checkpoint file to resume from.",
        ),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            "-p",
            help="Override the provider specified in the workflow (e.g., 'copilot').",
        ),
    ] = None,
    raw_metadata: Annotated[
        list[str] | None,
        typer.Option(
            "--metadata",
            "-m",
            help=(
                "Workflow metadata in key=value format. "
                "Merged on top of YAML metadata. Can be repeated."
            ),
        ),
    ] = None,
    skip_gates: Annotated[
        bool,
        typer.Option(
            "--skip-gates",
            help="Auto-select first option at human gates (for automation).",
        ),
    ] = False,
    log_file: Annotated[
        str | None,
        typer.Option(
            "--log-file",
            "-l",
            help=(
                "Write full debug output to a file. "
                "Pass a file path or 'auto' for auto-generated temp file."
            ),
        ),
    ] = None,
    no_interactive: Annotated[
        bool,
        typer.Option(
            "--no-interactive",
            help="Disable interactive interrupt capability (Esc to pause).",
        ),
    ] = False,
    web: Annotated[
        bool,
        typer.Option(
            "--web",
            help="Start a real-time web dashboard for workflow visualization.",
        ),
    ] = False,
    web_port: Annotated[
        int,
        typer.Option(
            "--web-port",
            help="Port for the web dashboard (0 = auto-select).",
        ),
    ] = 0,
    web_bg: Annotated[
        bool,
        typer.Option(
            "--web-bg",
            help=(
                "Run resumed workflow + dashboard in a background process. "
                "Prints the dashboard URL and exits immediately. "
                "Does not require --web."
            ),
        ),
    ] = False,
    guidance: Annotated[
        list[str] | None,
        typer.Option(
            "--guidance",
            help=("Mid-run guidance text to apply before the resumed agent runs. Can be repeated."),
        ),
    ] = None,
) -> None:
    """Resume a workflow from a checkpoint after failure.

    Loads a previously saved checkpoint and resumes execution from
    the agent that failed. The checkpoint contains all prior agent
    outputs so execution continues seamlessly.

    Either provide a workflow file (to find the latest checkpoint) or
    use --from to specify a checkpoint file directly.

    Note: when running with --web or --web-bg, the dashboard only shows
    events from the resumed agent forward. Agent runs that completed
    before the checkpoint were emitted in the original process and are
    not replayed.

    \b
    Examples:
        conductor resume workflow.yaml
        conductor resume --from /tmp/conductor/checkpoints/my-workflow-20260224-153000.json
        conductor resume workflow.yaml --skip-gates
        conductor resume workflow.yaml --log-file auto
        conductor resume workflow.yaml --no-interactive
        conductor resume workflow.yaml --provider copilot
        conductor resume workflow.yaml --metadata tracker=ado -m work_item_id=1814
        conductor resume workflow.yaml --web
        conductor resume workflow.yaml --web --web-port 8080
        conductor resume workflow.yaml --web-bg
        conductor resume workflow.yaml --guidance "Skip the benchmark step"
    """
    import asyncio
    import json

    from conductor.cli.run import (
        generate_log_path,
        parse_guidance_flags,
        parse_metadata_flags,
        resume_workflow_async,
    )

    # Validate arguments
    if workflow is None and from_checkpoint is None:
        console.print(
            Text.from_markup(
                "[bold red]Error:[/bold red] "
                "Provide a workflow file or use --from to specify a checkpoint."
            )
        )
        console.print(
            Text.from_markup(
                "[dim]Usage: conductor resume workflow.yaml "
                "or conductor resume --from <checkpoint.json>[/dim]"
            )
        )
        raise typer.Exit(code=1)

    # Validate mutually exclusive flags
    if web and web_bg:
        raise typer.BadParameter("--web and --web-bg are mutually exclusive")

    # Resolve workflow ref if provided
    resolved_workflow: Path | None = None
    if workflow is not None:
        from conductor.registry.cache import resolve_and_fetch
        from conductor.registry.errors import RegistryError
        from conductor.registry.resolver import resolve_ref

        try:
            ref = resolve_ref(workflow)
            if ref.kind == "file":
                assert ref.path is not None
                resolved_workflow = ref.path.resolve()
                if not resolved_workflow.exists():
                    console.print(
                        styled("[bold red]Error:[/bold red] Workflow file not found: {}", workflow)
                    )
                    raise typer.Exit(code=1)
            else:
                resolved_workflow = resolve_and_fetch(ref)
        except RegistryError as e:
            print_error(e)
            raise typer.Exit(code=1) from None

    # Resolve checkpoint path if provided
    resolved_checkpoint: Path | None = None
    if from_checkpoint is not None:
        resolved_checkpoint = from_checkpoint.resolve()
        if not resolved_checkpoint.exists():
            console.print(
                styled("[bold red]Error:[/bold red] Checkpoint file not found: {}", from_checkpoint)
            )
            raise typer.Exit(code=1)

    # Parse --metadata key=value flags (no type coercion)
    cli_metadata: dict[str, str] = {}
    if raw_metadata:
        cli_metadata.update(parse_metadata_flags(raw_metadata))

    # Validate --guidance flags up front (empty/oversized entries rejected
    # the same way POST /api/guidance rejects them), before any checkpoint
    # restore or --web-bg fork.
    if guidance:
        guidance = parse_guidance_flags(guidance)

    # Resolve log file path
    resolved_log_file: Path | None = None
    if log_file is not None:
        if log_file.lower() == "auto":
            name = resolved_workflow.stem if resolved_workflow else "resume"
            resolved_log_file = generate_log_path(name)
        else:
            resolved_log_file = Path(log_file)

    # Handle --web-bg: fork a background process and exit immediately
    if web_bg:
        # When the user resumes via --from <checkpoint> alone (no workflow
        # argument), resolved_workflow is None but the checkpoint records the
        # original workflow path. Read it so the human_gate notice can still
        # fire for the detached child (issue #286).
        gate_check_workflow: Path | None = resolved_workflow
        if gate_check_workflow is None and resolved_checkpoint is not None:
            try:
                ckpt_data = json.loads(resolved_checkpoint.read_text(encoding="utf-8"))
                ckpt_workflow = ckpt_data.get("workflow_path")
                if isinstance(ckpt_workflow, str):
                    candidate = Path(ckpt_workflow)
                    if candidate.exists():
                        gate_check_workflow = candidate
            except (OSError, json.JSONDecodeError):
                # Checkpoint unreadable — let the normal resume path surface it.
                pass
        # Background human gates are now resolvable from the dashboard /
        # ``conductor gate respond`` (issue #286); compute the notice flag
        # here instead of aborting.
        notify_gate = (
            not skip_gates
            and gate_check_workflow is not None
            and _workflow_has_human_gate(gate_check_workflow)
        )
        from conductor.cli.bg_runner import launch_background_resume

        try:
            launch = launch_background_resume(
                workflow_path=resolved_workflow,
                checkpoint_path=resolved_checkpoint,
                provider_override=provider,
                skip_gates=skip_gates,
                log_file=resolved_log_file,
                web_port=web_port,
                metadata=cli_metadata,
                guidance=guidance,
            )
            if is_verbose():
                console.print(styled("[bold cyan]Dashboard:[/bold cyan] {}", launch.url))
                console.print(styled("[dim]Child stderr log: {}[/dim]", launch.stderr_log))
                console.print(
                    Text.from_markup(
                        "[dim]Resumed workflow running in background. Dashboard auto-shuts down "
                        "after workflow completes and all clients disconnect.[/dim]"
                    )
                )
                if notify_gate:
                    _print_web_bg_human_gate_notice(launch.url)
        except Exception as e:
            print_error(e)
            raise typer.Exit(code=1) from None
        return

    try:
        result = asyncio.run(
            resume_workflow_async(
                workflow_path=resolved_workflow,
                checkpoint_path=resolved_checkpoint,
                provider_override=provider,
                skip_gates=skip_gates,
                log_file=resolved_log_file,
                no_interactive=no_interactive,
                web=web,
                web_port=web_port,
                web_bg=web_bg,
                metadata=cli_metadata,
                guidance=guidance,
            )
        )

        # Output as JSON to stdout
        output_console.print_json(json.dumps(result))

    except WorkflowTerminated as e:
        # Mirror of the `run` handler — see commentary there for the
        # `default=str` and `try/except` rationale.
        try:
            output_console.print_json(json.dumps(e.output, default=str))
        except (TypeError, ValueError) as json_exc:
            logger.exception("Failed to serialise terminate output")
            console.print(
                styled(
                    "[yellow]Warning:[/yellow] could not serialise terminate output: {}", json_exc
                )
            )
        console.print(
            styled("[red]Workflow terminated[/red] at '{}': {}", e.terminated_by, e.reason)
        )
        if e.suggestion:
            console.print(styled("[dim]Suggestion: {}[/dim]", e.suggestion))
        raise typer.Exit(code=1) from None
    except Exception as e:
        print_error(e)
        raise typer.Exit(code=1) from None


@app.command(rich_help_panel="Interact")
def guide(
    text: Annotated[
        str,
        typer.Option(
            "--text",
            "-t",
            help="Guidance text to send to the running workflow.",
        ),
    ],
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            "-p",
            help="Dashboard port of the running workflow (auto-discovered if omitted).",
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
    """Send mid-run guidance to a workflow running with --web or --web-bg.

    The guidance is applied at the next step boundary, or immediately if an
    agent is currently paused (dashboard Stop, or an Esc/Ctrl+G interrupt)
    — in which case the agent resumes with the guidance applied.

    \b
    Examples:
        conductor guide --text "Prefer Python 3.12 examples"
        conductor guide --port 8080 --text "Skip the benchmark step"
        conductor guide --text "Use the staging endpoint" --token secret123
    """
    from conductor.cli.guide import guide_impl

    guide_impl(text, port, token)


@app.command(hidden=True)
def checkpoints(
    workflow: Annotated[
        Path | None,
        typer.Argument(
            help="Path to a workflow YAML file. Filters checkpoints to this workflow only.",
        ),
    ] = None,
) -> None:
    """Deprecated alias for 'conductor checkpoint list'."""
    console.print(
        Text.from_markup(
            "[yellow]Warning:[/yellow] 'conductor checkpoints' is deprecated and will "
            "be removed in a future release. Use 'conductor checkpoint list' instead."
        )
    )
    from conductor.cli.checkpoint import _list_checkpoints_impl

    _list_checkpoints_impl(workflow)


@app.command(rich_help_panel="Run & Recover")
def replay(
    log_file: Annotated[
        Path,
        typer.Argument(
            help="Path to a JSON or JSONL event log file.",
            exists=True,
            readable=True,
        ),
    ],
    web_port: Annotated[
        int,
        typer.Option(
            "--web-port",
            help="Port for the replay dashboard (0 = auto-select).",
        ),
    ] = 0,
) -> None:
    """Replay a recorded workflow from a JSON/JSONL event log.

    Opens the web dashboard in replay mode with a timeline slider
    for scrubbing through the workflow history.

    The log file can be:
    - A JSON array downloaded from the dashboard (GET /api/logs)
    - A JSONL file written by the EventLogSubscriber

    Example:
        conductor replay conductor-logs.json
        conductor replay /tmp/conductor/conductor-my-workflow-20260101-120000.events.jsonl
    """
    import asyncio

    async def _run_replay() -> None:
        from conductor.web.replay import ReplayDashboard

        try:
            dashboard = ReplayDashboard(
                log_file.resolve(),
                host="127.0.0.1",
                port=web_port,
            )
        except ValueError as exc:
            print_error(exc)
            raise typer.Exit(1) from exc

        await dashboard.start()
        if is_verbose():
            console.print(styled("\n[bold green]▶ Replay dashboard:[/] {}\n", dashboard.url))
            console.print(Text.from_markup("[dim]Press Ctrl+C to exit[/dim]\n"))

        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await dashboard.stop()

    try:
        asyncio.run(_run_replay())
    except KeyboardInterrupt:
        if is_verbose():
            console.print(Text.from_markup("\n[dim]Replay stopped.[/dim]"))


@app.command(rich_help_panel="Run & Recover")
def status(
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit machine-readable output instead of a table.",
        ),
    ] = False,
) -> None:
    """List background workflows without stopping any of them.

    \b
    `conductor stop` also lists running workflows, but it stops one when
    exactly one is running -- so the natural "what's running?" reflex is
    destructive precisely when there is a single run to lose. This command is
    read-only and always safe.

    The dashboard URL is included because there is otherwise no supported way
    to recover it once the launching terminal is gone.

    \b
    Exit codes:
        0  listed successfully (including when nothing is running)

    \b
    Examples:
        conductor status
        conductor status --json
    """
    import json

    from conductor.fleet.records import scan_run_records

    # Deliberately not ``read_run_records``: that one prunes as it reads, which
    # would make the read-only command destructive — the exact trap this
    # command exists to give people an alternative to.
    running = scan_run_records()

    if json_output:
        payload = []
        for e in running:
            stderr_log, stdout_log = _bg_capture_logs(e)
            payload.append(
                {
                    "pid": e.pid,
                    "port": e.port,
                    "workflow": str(e.workflow_path or ""),
                    "run_id": _optional_str(e.run_id),
                    "started_at": e.started_at or "",
                    "mode": e.mode,
                    "event_log": _optional_str(e.event_log_path),
                    "stderr_log": stderr_log,
                    "stdout_log": stdout_log,
                    "url": (f"http://127.0.0.1:{e.port}" if e.port is not None else None),
                }
            )
        output_console.print_json(json.dumps({"running": payload}), ensure_ascii=True)
        return

    if not running:
        console.print(Text.from_markup("[dim]No background workflows are currently running.[/dim]"))
        return

    _print_running_list(running, console, show_url=True)
    console.print(
        styled(
            "\n[dim]{} running. Use 'conductor stop --port <PORT>' to stop one.[/dim]", len(running)
        )
    )


def _discover_running_records() -> list[RunRecord]:
    """Discover every currently-running workflow via Fleet Manager run records.

    As of Fleet Manager E3, ``conductor stop`` sources directly from
    :func:`conductor.fleet.records.read_run_records`, which itself merges
    the new ``run_id``-keyed records (every mode: ``fg``, ``fg-web``,
    ``bg``) with legacy port-keyed ``.pid`` files (surfaced with
    ``mode="bg"``, per D1, so they never trigger the foreground-stop
    confirmation). This closes the design's blocking problem --
    ``conductor stop``'s blindness to foreground runs -- and gives
    ``--all`` a meaningful (not bg-only) scope.

    Stale (dead-``pid``) and corrupt/unparseable records are pruned from
    disk as a side effect of ``read_run_records()``; this function never
    raises for a corrupt, vanished, unreadable, or legacy-shaped file.

    Returns:
        List of :class:`conductor.fleet.records.RunRecord` for every run
        whose process is confirmed alive.
    """
    from conductor.fleet.records import read_run_records

    return read_run_records()


def _remove_stopped_record(record: RunRecord) -> None:
    """Remove the backing run record (or legacy PID file) for a stopped run.

    Removal is keyed by ``run_id`` (Fleet Manager E3-T6), falling back to the
    legacy port-keyed removal for a pre-upgrade ``.pid`` file.

    The fallback is deliberately **not** gated on ``run_id`` being empty. A
    legacy ``.pid`` file records a ``run_id`` too (issue #411 added it), so
    "has a run_id" does not imply "is a JSON run record" -- gating on that
    would leave every pre-upgrade file with a recorded id undeletable, and
    ``stop`` would report success while the entry stayed on disk forever.
    Instead, the port-keyed removal is attempted whenever the record-keyed
    one removed nothing. That fallback is identity-checked on the record's
    ``pid``: a port-only match would delete whatever file currently holds the
    port, which after the original run exits can be a *different, live* run
    that has since bound it (issue #344). Since ``remove_run_record`` also
    returns False on the normal cooperative path (the child removed its own
    record on exit), this fallback fires routinely rather than only for
    pre-upgrade files, so it has to be safe rather than merely rare.

    Args:
        record: The :class:`RunRecord` that was just confirmed stopped.
    """
    removed = False
    if record.run_id:
        from conductor.fleet.records import remove_run_record

        removed = remove_run_record(record.run_id)

    if not removed and record.port is not None:
        from conductor.cli.pid import remove_pid_file_for_pid

        remove_pid_file_for_pid(record.port, record.pid)


def _run_has_checkpoints(record: RunRecord) -> bool:
    """Return True if a periodic checkpoint exists for this run (E3-T5).

    Used by the D1 confirmation prompt to tell the user whether stopping a
    foreground run is fully lossy or not: looks for
    ``{workflow_name}-*.json`` files under the record's ``checkpoint_dir``
    (the same, global, ``$TMPDIR``-rooted directory for every run -- see
    ``RunRecord.checkpoint_dir``'s docstring) and checks whether *any* of
    them actually belongs to this run (``run_id`` match) and was written
    by the periodic-checkpoint path (``trigger == "periodic"``) rather
    than an unrelated failure checkpoint from a previous crash of the same
    ``run_id``. Merely checking the directory's existence would say
    nothing about this specific run, since the directory is shared by
    every run on the machine.

    Best-effort: any error while listing/loading checkpoint files is
    treated as "no checkpoints found" rather than raised -- this is
    advisory text on a confirmation prompt, not a correctness-critical
    check.

    Args:
        record: The run record to check.

    Returns:
        True if at least one periodic checkpoint file matches this run.
    """
    if not record.checkpoint_dir or not record.workflow_name or not record.run_id:
        return False

    from conductor.engine.checkpoint import CheckpointManager
    from conductor.exceptions import CheckpointError

    try:
        candidates = list(Path(record.checkpoint_dir).glob(f"{record.workflow_name}-*.json"))
    except OSError:
        return False

    for f in candidates:
        try:
            checkpoint = CheckpointManager.load_checkpoint(f)
        except CheckpointError:
            continue
        if checkpoint.run_id == record.run_id and checkpoint.trigger == "periodic":
            return True
    return False


def _stdin_is_interactive() -> bool:
    """Return True if stdin is a real interactive terminal.

    Factored out (rather than calling ``sys.stdin.isatty()`` inline) so
    tests can simulate an interactive/non-interactive stdin directly --
    Typer/Click's ``CliRunner`` always substitutes a non-tty stream for
    ``sys.stdin`` for the duration of ``invoke()``, so patching the
    ``sys.stdin`` object *before* invoking has no effect on the object the
    command actually sees.
    """
    return sys.stdin.isatty()


def _foreground_targets(targets: list[RunRecord]) -> list[RunRecord]:
    """Return the subset of ``targets`` with ``mode in {"fg", "fg-web"}``.

    Extracted (Fleet Manager E8-T1) so both :func:`_confirm_stop_targets`
    (CLI) and the TUI's kill-confirmation message builder
    (``conductor.fleet.tui.actions.build_kill_confirmation_message``) apply
    the exact same "which of these targets is foreground" rule.
    """
    return [r for r in targets if r.mode in {"fg", "fg-web"}]


def _foreground_stop_warning_lines(foreground: list[RunRecord]) -> list[str]:
    """Build the per-run checkpoint-status warning lines for a foreground stop.

    Extracted from :func:`_confirm_foreground_stop` (Fleet Manager E8-T1)
    so the CLI's ``rich.prompt.Confirm`` prompt and the TUI's
    kill-confirmation modal (``conductor.fleet.tui.actions``) show the
    exact same per-run text -- one policy (what to say about a foreground
    run's checkpoint-recoverability), two presentations (how each UI asks
    the user to confirm). Each returned line is plain text (no Rich markup)
    so either caller can style/wrap it however its own UI requires.

    Args:
        foreground: The subset of stop targets with ``mode in {"fg",
            "fg-web"}``.

    Returns:
        One line per foreground run, in the same order given, reading
        ``"<workflow> (PID <pid>): <checkpoint note>"``.
    """
    lines = []
    for r in foreground:
        workflow_name = Path(r.workflow_path or "unknown").stem
        if _run_has_checkpoints(r):
            note = "periodic checkpoints found -- resumable after stopping"
        else:
            note = "no periodic checkpoints found -- progress will be lost"
        lines.append(f"{workflow_name} (PID {r.pid}): {note}")
    return lines


def _confirm_foreground_stop(foreground: list[RunRecord], con: Console) -> bool:
    """Print the D1 confirmation prompt and return whether the user agreed.

    Only called when at least one target has ``mode in {"fg", "fg-web"}``
    (Fleet Manager E3-T3) -- the caller is responsible for the ``--yes``
    bypass and the non-TTY refusal (E3-T4), since those two cases must not
    reach ``rich.prompt.Confirm`` at all. Names every foreground run in
    scope and states, per Open Question 1's working assumption, that
    in-flight progress is lost unless periodic checkpoints are enabled for
    that run (E3-T5).

    Args:
        foreground: The subset of stop targets with ``mode in {"fg",
            "fg-web"}``. Never empty.
        con: Rich Console to print to and prompt on.

    Returns:
        True if the user confirmed, False if they declined.
    """
    from rich.prompt import Confirm

    names = ", ".join(
        f"'{Path(r.workflow_path or 'unknown').stem}' (PID {r.pid})" for r in foreground
    )
    con.print(
        styled(
            "[bold yellow]Warning:[/bold yellow] this will stop {} foreground workflow run(s): {}.",
            len(foreground),
            names,
        )
    )
    con.print(
        Text.from_markup(
            "[dim]In-flight progress will be lost unless periodic checkpoints are "
            "enabled for the run:[/dim]"
        )
    )
    for line in _foreground_stop_warning_lines(foreground):
        con.print(styled("[dim]  - {}[/dim]", line))

    return Confirm.ask("Continue?", console=con, default=False)


def _confirm_stop_targets(
    targets: list[RunRecord], yes: bool, con: Console, *, non_interactive: bool = False
) -> bool:
    """Gate the D1 confirmation, including the ``--yes`` bypass and non-TTY refusal.

    Legacy ``.pid`` records and ``mode="bg"`` records never trigger a
    prompt (D1) -- today's behavior for a bg-only fleet is byte-for-byte
    preserved. When at least one target is a foreground run:

    - ``--yes``/``-y`` bypasses the prompt unconditionally (E3-T4).
    - A non-interactive ``stdin`` (``sys.stdin.isatty()`` is False)
      without ``--yes`` refuses to proceed -- a non-TTY cannot confirm,
      and defaulting to yes would reinstate the exact hazard D1 closes.
      This case exits non-zero (``typer.Exit(1)``) having signalled
      nothing, distinct from an interactive decline below.
    - Otherwise, prompts once via :func:`_confirm_foreground_stop`, naming
      every foreground run in scope (so ``--all`` over a mixed fleet
      prompts exactly once).

    Args:
        targets: The full set of run records ``stop`` is about to act on.
        yes: Whether ``--yes``/``-y`` was passed.
        con: Rich Console for output/prompting.
        non_interactive: Force the non-TTY branch regardless of what
            ``stdin`` reports. Set for ``--json``, which cannot prompt --
            being unable to ask is the same condition the non-TTY branch
            treats as grounds to *refuse*, so skipping the gate entirely
            would let ``stop --all --json`` kill a developer's foreground
            run with no prompt, no ``--yes`` and no refusal.

    Returns:
        True if the caller should proceed to stop every target. False if
        the user interactively declined the prompt -- the caller should
        exit 0 having stopped nothing.

    Raises:
        typer.Exit: With code 1 when a foreground target requires
            confirmation but confirmation is impossible (``stdin`` is not a
            terminal, or ``non_interactive``) and ``--yes`` was not passed.
    """
    foreground = _foreground_targets(targets)
    if not foreground:
        return True
    if yes:
        return True
    if non_interactive or not _stdin_is_interactive():
        con.print(
            Text.from_markup(
                "[bold red]Error:[/bold red] stopping a foreground workflow run requires "
                "confirmation, but stdin is not a terminal. Re-run with --yes/-y to "
                "confirm non-interactively."
            )
        )
        con.print(Text.from_markup("[dim]Would stop:[/dim]"))
        _print_running_list(targets, con)
        # A non-TTY cannot confirm, and defaulting to yes would reinstate
        # the hazard D1 closes -- exit non-zero having signalled nothing,
        # distinct from the exit-0 "user declined" path below.
        raise typer.Exit(code=1)
    return _confirm_foreground_stop(foreground, con)


@dataclass
class StopOutcome:
    """Outcome of a :func:`stop_records` call.

    Attributes:
        declined: True if the injected ``confirm`` callback returned
            False, meaning nothing was attempted -- distinct from an
            empty :attr:`stopped` caused by every target failing to
            actually stop (permission denied, escalation failure, etc.),
            which reports ``declined=False`` with an empty list.
        stopped: Records confirmed stopped (:func:`_stop_process` polled
            them dead, or found them already gone) and whose run record
            was removed.
        failed: ``(record, outcome)`` for every target *not* confirmed
            stopped -- ``"survived"``, ``"unconfirmed"``, or a refused
            identity mismatch. Carried explicitly because a caller that
            renders only :attr:`stopped` reports a success it did not
            achieve; that matters most for the TUI, whose console is a
            discarded buffer, so this is the only channel its user has.
    """

    declined: bool
    stopped: list[RunRecord]
    failed: list[tuple[RunRecord, str]] = field(default_factory=list)


def stop_records(
    targets: list[RunRecord],
    con: Console,
    *,
    confirm: Callable[[list[RunRecord]], bool] | None = None,
) -> StopOutcome:
    """Stop every record in ``targets`` -- the one kill implementation shared
    by ``conductor stop`` and the Fleet Manager TUI's kill/kill-all actions
    (Fleet Manager E8-T1).

    If ``confirm`` is given, it is called once with the full ``targets``
    list before anything is touched; if it returns False, nothing is
    stopped (``declined=True``). Passing ``confirm=None`` skips this gate
    entirely -- for a caller (the TUI) that has already resolved its own
    confirmation via an async modal before calling this function, since a
    synchronous callback slot cannot itself ``await`` a Textual screen.
    ``confirm`` may raise instead of returning (the CLI's non-interactive
    refusal path raises ``typer.Exit``); this function does not catch
    that, so it propagates unchanged to the caller.

    Killing is deliberately **not** ``conductor fleet kill`` (per the
    design) -- both callers funnel through this exact function rather than
    each re-implementing "signal, verify, remove record".

    Reuses :func:`_stop_process`'s verify-then-report contract (E3-T10): a
    record is only removed, and only counted in the returned ``stopped``
    list, once the process is actually confirmed gone (or was already
    gone) -- never on signal-send alone. This is the guarantee the TUI
    must inherit rather than reintroduce the fire-and-forget behavior
    E3-T10 removed from the CLI.

    Args:
        targets: The run records to stop.
        con: Rich Console for progress output. Also safe to pass a Console
            writing to a non-terminal stream (e.g. the TUI's silent
            console), since this function never assumes stdout ownership.
        confirm: Optional gate called once with ``targets`` before
            stopping anything.

    Returns:
        A :class:`StopOutcome` describing whether the caller declined,
        which records were actually confirmed stopped, and which were not.
    """
    if confirm is not None and not confirm(targets):
        return StopOutcome(declined=True, stopped=[])

    stopped: list[RunRecord] = []
    failed: list[tuple[RunRecord, str]] = []
    for record in targets:
        # ``_stop_process`` reports an outcome rather than a bool: only the
        # two outcomes that mean "this process is definitively gone" may
        # remove the record. ``survived``/``unconfirmed`` must not, or a
        # still-running run becomes untracked.
        result = _stop_process(record, con)
        if result["outcome"] in ("stopped", "already-exited"):
            _remove_stopped_record(record)
            stopped.append(record)
        else:
            failed.append((record, str(result["outcome"])))
    return StopOutcome(declined=False, stopped=stopped, failed=failed)


@app.command(rich_help_panel="Run & Recover")
def stop(
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            help="Stop the workflow whose dashboard is on this port.",
        ),
    ] = None,
    run_id: Annotated[
        str | None,
        typer.Option(
            "--run-id",
            help=(
                "Stop the workflow with this run ID. The only selector that can "
                "name a foreground run, which has no dashboard port."
            ),
        ),
    ] = None,
    all_workflows: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Stop all running conductor workflows, foreground and background.",
        ),
    ] = False,
    allow_self: Annotated[
        bool,
        typer.Option(
            "--allow-self",
            help="Include the run this command is executing inside (refused by default).",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Force-terminate even when the run's identity cannot be confirmed. "
                "Dangerous: the recorded PID may have been recycled onto another process. "
                "Does not override a confirmed mismatch, which blocks every rung. "
                "Also clears the run record of a run whose liveness cannot be probed at "
                "all -- if that process is still alive it becomes untracked and must be "
                "stopped by hand."
            ),
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip the confirmation prompt shown before stopping a foreground run.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit a machine-readable result per workflow instead of prose.",
        ),
    ] = False,
) -> None:
    """Stop running workflow processes.

    With no arguments, lists every running workflow -- foreground and
    background alike. If exactly one is found, stops it automatically. If
    multiple are found, prints the list and asks you to select one with
    --run-id (or --port, which can only name a run that has a dashboard).

    Each workflow is stopped by escalating until it is confirmed gone: a
    graceful cancel via the dashboard (which lets the run checkpoint), then a
    platform signal, then forceful termination. A run record is only removed
    once its process is confirmed dead, so a workflow that survives stays
    discoverable instead of becoming an untracked orphan.

    Forceful termination requires confirming the run's identity against its
    dashboard, because a recorded PID may since have been recycled onto an
    unrelated process. Use --force to override that check.

    \b
    By default, `stop` never targets the run it is executing inside --
    an agent smoke-testing this command must not terminate its own
    workflow (issue #399). That run is identified by `CONDUCTOR_RUN_ID`,
    the legacy `CONDUCTOR_WEB_BG`/`CONDUCTOR_WEB_PORT` pair, or process
    ancestry, and is excluded from `--all` and the no-flag auto-stop; a
    `--port` naming it is refused outright. Pass `--allow-self` to
    include it anyway.

    \b
    Exit codes:
        0  every targeted workflow is confirmed stopped (or was already
           gone), including a self-only refusal
        1  --port matched no running workflow, the target was ambiguous,
           or --port matched only your own run
        2  at least one workflow survived or could not be confirmed stopped

    \b
    Examples:
        conductor stop
        conductor stop --port 8080
        conductor stop --all
        conductor stop --all --json
        conductor stop --allow-self --port 8080
    """
    import json

    from conductor.cli.self_run import partition_own_run

    # Sources run records rather than PID files since the Fleet Manager, so
    # foreground runs are stoppable too -- previously `stop` could only see
    # `--web-bg` runs, and a plain `conductor run` was invisible to it.
    running = _discover_running_records()

    if not running:
        if json_output:
            output_console.print_json(json.dumps({"stopped": [], "failed": []}), ensure_ascii=True)
        else:
            console.print(Text.from_markup("[dim]No workflows are currently running.[/dim]"))
        return

    partition = partition_own_run(running)
    targetable = running if allow_self else partition.others
    auto_detected_single = False

    if all_workflows:
        if not allow_self and not targetable:
            if json_output:
                output_console.print_json(
                    json.dumps({"stopped": [], "failed": []}), ensure_ascii=True
                )
            else:
                _print_self_exclusion(partition, console, blocking=True)
            return
        targets = targetable
        if not allow_self and partition.own and not json_output:
            _print_self_exclusion(partition, console, blocking=False)
    elif run_id is not None:
        # The only selector that can name a foreground run: `fg` records have
        # no dashboard port, so --port cannot reach them.
        targets = [e for e in targetable if e.run_id == run_id]
        if not targets:
            if not allow_self:
                own_match = [e for e in partition.own if e.run_id == run_id]
                if own_match:
                    if json_output:
                        output_console.print_json(
                            json.dumps(
                                {
                                    "error": (
                                        f"run {run_id} is the run this command is executing "
                                        "inside; pass --allow-self to include it"
                                    )
                                }
                            ),
                            ensure_ascii=True,
                        )
                    else:
                        _print_self_refusal_line(own_match[0], console)
                        _print_allow_self_hint(console)
                    raise typer.Exit(code=1)
            if json_output:
                output_console.print_json(
                    json.dumps({"error": f"no running workflow with run id {run_id}"}),
                    ensure_ascii=True,
                )
            else:
                console.print(
                    styled(
                        "[bold red]Error:[/bold red] No running workflow found with run ID {}.",
                        run_id,
                    )
                )
                console.print(Text.from_markup("[dim]Running workflows:[/dim]"))
                _print_running_list(targetable, console)
            raise typer.Exit(code=1)
    elif port is not None:
        targets = [e for e in targetable if e.port == port]
        if not targets:
            if not allow_self:
                own_match = [e for e in partition.own if e.port == port]
                if own_match:
                    if json_output:
                        output_console.print_json(
                            json.dumps(
                                {
                                    "error": (
                                        f"port {port} is the run this command is executing "
                                        "inside; pass --allow-self to include it"
                                    )
                                }
                            ),
                            ensure_ascii=True,
                        )
                    else:
                        _print_self_refusal_line(own_match[0], console)
                        _print_allow_self_hint(console)
                    raise typer.Exit(code=1)
            if json_output:
                output_console.print_json(
                    json.dumps({"error": f"no running workflow on port {port}"}),
                    ensure_ascii=True,
                )
            else:
                console.print(
                    styled(
                        "[bold red]Error:[/bold red] No running workflow found on port {}. "
                        "(A foreground run without a dashboard has no port to match.)",
                        port,
                    )
                )
                if not allow_self and not targetable and partition.own:
                    _print_self_exclusion(partition, console, blocking=False)
                else:
                    console.print(Text.from_markup("[dim]Running workflows:[/dim]"))
                    _print_running_list(targetable, console)
            raise typer.Exit(code=1)
    elif len(targetable) == 0:
        if json_output:
            output_console.print_json(json.dumps({"stopped": [], "failed": []}), ensure_ascii=True)
        else:
            _print_self_exclusion(partition, console, blocking=True)
        return
    elif len(targetable) == 1:
        targets = targetable
        auto_detected_single = True
    else:
        # Ambiguous: list rather than guess which run the user meant. This is
        # a failure to act, so it must not report success to automation.
        if json_output:
            output_console.print_json(
                json.dumps({"error": "multiple workflows running; specify --port or --all"}),
                ensure_ascii=True,
            )
        else:
            console.print(
                styled(
                    "[bold yellow]Multiple workflows running ({}).[/bold yellow]",
                    len(targetable),
                )
            )
            console.print(
                Text.from_markup(
                    "[dim]Specify --port to stop a specific one, or --all to stop all.[/dim]\n"
                )
            )
            _print_running_list(targetable, console)
            if not allow_self and partition.own:
                _print_self_exclusion(partition, console, blocking=False)
        raise typer.Exit(code=1)

    # Prose goes to ``console`` (stderr); JSON goes to ``output_console``
    # (stdout). They cannot corrupt each other, so diagnostics stay visible
    # even in --json mode.
    # Stopping a foreground run is newly possible (and more disruptive: there
    # is no dashboard to resume from), so it is gated behind a confirmation
    # unless --yes. --json cannot prompt, so it takes the same refusal branch
    # a non-TTY does rather than skipping the gate: "cannot ask" is the
    # condition D1 exists to refuse on, not a licence to proceed.
    if not _confirm_stop_targets(targets, yes, console, non_interactive=json_output):
        console.print(Text.from_markup("[dim]Aborted: no workflows were stopped.[/dim]"))
        return

    results = []
    for entry in targets:
        if allow_self:
            _maybe_warn_stopping_self(entry, partition, console)
        results.append(_stop_process(entry, console, force=force))

    for entry, result in zip(targets, results, strict=True):
        if result["outcome"] in ("stopped", "already-exited"):
            # Identity-checked: only remove the record if it still describes
            # the process we just stopped, never merely "whatever holds this
            # port" -- see _remove_stopped_record.
            _remove_stopped_record(entry)
        elif force and result["outcome"] == "unconfirmed" and result["rung"] == "terminate":
            # The #166 escape hatch. Reaching here means the liveness probe
            # itself failed, so we genuinely cannot say whether the process
            # died. Left in place the entry is permanent: bare ``stop`` stays
            # ambiguous and ``stop --all`` exits 2 for good, so a CI teardown
            # never recovers. ``--force`` is the operator accepting that risk,
            # so honour it — loudly, because the process may still be alive.
            #
            # Deliberately narrow. It does not fire for ``refused`` (we chose
            # not to act), for ``mismatched`` (the PID is someone else's), or
            # for ``survived`` (the process is demonstrably alive, and removing
            # its file would orphan it).
            console.print(
                styled(
                    "[bold yellow]Warning:[/bold yellow] removing the PID file for workflow "
                    "[cyan]'{}'[/cyan] (PID {}, port {}) without confirming it stopped, "
                    "because --force was given and its liveness could not be probed. If "
                    "that process is still alive it is now untracked and must be stopped "
                    "by hand.",
                    result["workflow"],
                    result["pid"],
                    result["port"],
                )
            )
            _remove_stopped_record(entry)

    if json_output:
        payload = {
            "stopped": [r for r in results if r["outcome"] in ("stopped", "already-exited")],
            "failed": [r for r in results if r["outcome"] not in ("stopped", "already-exited")],
        }
        output_console.print_json(json.dumps(payload), ensure_ascii=True)
    elif auto_detected_single and not allow_self and partition.own:
        # Single-target auto-stop: the exclusion note comes after the stop
        # so the user sees "Stopped <other>" before being told their own run
        # was left out of consideration, matching the --all branch's note.
        _print_self_exclusion(partition, console, blocking=False)

    if any(r["outcome"] not in ("stopped", "already-exited") for r in results):
        raise typer.Exit(code=2)


def _print_self_refusal_line(entry: RunRecord, con: Console) -> None:
    """Print the red refusal line naming the run this command is executing inside.

    Args:
        entry: The PID-file dict identified as this process's own run.
        con: Rich Console for output.
    """
    from conductor.cli.self_run import describe_own_run

    con.print(
        styled(
            "[bold red]Refusing[/bold red] to stop run {} — it is the run this "
            "command is executing inside.",
            describe_own_run(entry),
        )
    )


def _print_allow_self_hint(con: Console) -> None:
    """Print the dim hint pointing at the ``--allow-self`` escape hatch."""
    con.print(Text.from_markup("[dim]Use --allow-self to include it.[/dim]"))


def _print_self_exclusion(partition: OwnRunPartition, con: Console, *, blocking: bool) -> None:
    """Print the message explaining that this run was excluded from targeting.

    Args:
        partition: The result of ``partition_own_run``. ``partition.own``
            must be non-empty.
        con: Rich Console for output.
        blocking: True when there is nothing left to stop (prints the red
            refusal line plus "No other workflows are running."); False when
            other runs were still targeted (prints a yellow exclusion note).
    """
    entry = partition.own[0]
    if blocking:
        _print_self_refusal_line(entry, con)
        con.print(Text.from_markup("[dim]No other workflows are running.[/dim]"))
    else:
        from conductor.cli.self_run import describe_own_run

        con.print(
            styled(
                "[yellow]Excluded[/yellow] run {} — it is the run this command is "
                "executing inside.",
                describe_own_run(entry),
            )
        )
    _print_allow_self_hint(con)


def _maybe_warn_stopping_self(entry: RunRecord, partition: OwnRunPartition, con: Console) -> None:
    """Print a yellow warning when about to signal the caller's own run.

    Only reachable via ``--allow-self`` -- without that flag, an entry
    identified as this process's own run is never present in the
    targetable list in the first place.

    Args:
        entry: The PID-file dict about to be stopped.
        partition: The result of ``partition_own_run``.
        con: Rich Console for output.
    """
    from conductor.cli.self_run import describe_own_run

    if any(o.pid == entry.pid for o in partition.own):
        con.print(
            styled(
                "[yellow]Warning:[/yellow] stopping run {} — this is the run "
                "executing this command.",
                describe_own_run(entry),
            )
        )


class Identity(str, Enum):
    """Result of checking that a PID file describes the process on its port.

    The distinction between :attr:`UNCONFIRMED` and :attr:`MISMATCHED` is
    load-bearing. ``UNCONFIRMED`` means "no evidence either way" (an older PID
    file, or a dashboard that isn't answering) — the polite signal is still
    reasonable, since that is all the previous implementation ever did.
    ``MISMATCHED`` means "positive evidence this PID belongs to someone else",
    which must block *every* PID-directed action, not just the forceful one.
    """

    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    MISMATCHED = "mismatched"


def _confirm_identity(entry: RunRecord, con: Console) -> Identity:
    """Check that the process on ``entry['port']`` is the one ``entry`` describes.

    Between a PID file being written and ``conductor stop`` reading it, the
    process may have exited and the OS may have recycled its PID onto something
    unrelated — at which point terminating that PID kills an innocent process.
    Asking the dashboard who it is closes that gap, because the answer comes
    from the running process itself.

    ``pid`` is the primary signal: the dashboard runs in the same process as
    the workflow, so a matching ``os.getpid()`` is direct proof. It is also
    available immediately, whereas ``run_id`` is empty until the workflow
    emits ``workflow_started``, and legitimately *differs* from the launcher's
    id on resume (the child reuses the checkpoint's run id). ``run_id`` is
    kept as a secondary signal so a dashboard from an older conductor, which
    does not report ``pid``, can still be identified.

    Args:
        entry: A PID-file dict.
        con: Rich Console for output.

    Returns:
        :class:`Identity`.
    """
    import httpx

    port = entry.port
    if port is None:
        # A `mode="fg"` record has no dashboard, so there is nothing to ask.
        # Probing anyway builds `http://127.0.0.1:None/api/info`, which httpx
        # rejects into the blanket handler below -- reaching the right answer
        # by accident, via a debug log, after a wasted request.
        logger.debug("No dashboard port on run %s; identity cannot be confirmed", entry.run_id)
        return Identity.UNCONFIRMED

    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/api/info", timeout=_IDENTITY_TIMEOUT)
        resp.raise_for_status()
        info = resp.json()
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot confirm"
        logger.debug("Identity probe on port %s failed: %s", port, exc)
        return Identity.UNCONFIRMED

    if not isinstance(info, dict):
        return Identity.UNCONFIRMED

    reported_pid = info.get("pid")
    if isinstance(reported_pid, int):
        if reported_pid == entry.pid:
            return Identity.CONFIRMED
        con.print(
            styled(
                "[bold yellow]Warning:[/bold yellow] the dashboard on port {} is PID "
                "{}, but the PID file records {}. Refusing to act on it.",
                port,
                reported_pid,
                entry.pid,
            )
        )
        return Identity.MISMATCHED

    # Older dashboard: fall back to run_id when both sides have one.
    expected = str(entry.run_id or "")
    actual = str(info.get("run_id") or "")
    if not expected or not actual:
        return Identity.UNCONFIRMED
    return Identity.CONFIRMED if actual == expected else Identity.MISMATCHED


def _request_graceful_kill(port: int) -> bool:
    """Ask the dashboard to cancel its workflow via ``POST /api/kill``.

    Returns:
        True if the request was accepted. This is an **acknowledgement, not a
        death certificate** — the endpoint sets an asyncio event and returns
        immediately, and the drain that follows it is unbounded, so the caller
        must still confirm the process actually exited.
    """
    import httpx

    try:
        resp = httpx.post(f"http://127.0.0.1:{port}/api/kill", timeout=_IDENTITY_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - fall through to the next rung
        logger.debug("POST /api/kill on port %s failed: %s", port, exc)
        return False
    return True


def _signal_process(pid: int) -> None:
    """Send the platform's polite termination signal, ignoring failures.

    Neither platform's signal is reliable for conductor: on Windows
    ``CTRL_BREAK_EVENT`` requires a shared console, which a separate
    ``conductor stop`` invocation does not have; on POSIX the background child
    runs ``--no-interactive`` and installs no SIGTERM handler. This rung is
    therefore best-effort — it costs nothing and occasionally works.
    """
    import signal
    import sys

    try:
        if sys.platform == "win32":
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError) as exc:
        logger.debug("Polite signal to PID %s failed: %s", pid, exc)


def _stop_process(entry: RunRecord, con: Console, force: bool = False) -> dict:
    """Stop one background workflow, escalating until it is confirmed dead.

    The ladder is graceful → polite signal → forceful, with a bounded wait
    after each rung. It never reports success on the strength of a request
    having been *accepted*: every rung is followed by a liveness check, and
    the caller only removes the PID file when the process is confirmed gone.

    Args:
        entry: A PID-file dict with ``pid``, ``port``, ``workflow``, and
            ideally ``run_id`` keys.
        con: Rich Console for output.
        force: Permit forceful termination even when identity could not be
            confirmed. Dangerous — the PID may have been recycled.

    Returns:
        A result dict with ``pid``, ``port``, ``workflow``, ``run_id``,
        ``outcome`` and ``rung`` keys. ``outcome`` is one of ``stopped``,
        ``already-exited``, ``survived`` or ``unconfirmed``.
    """
    from conductor.cli.pid import Liveness, process_liveness, terminate_process, wait_for_exit

    pid = entry.pid
    port = entry.port
    workflow = entry.workflow_name or Path(str(entry.workflow_path or "unknown")).stem

    def _result(outcome: str, rung: str) -> dict:
        return {
            "pid": pid,
            "port": port,
            "workflow": workflow,
            "run_id": entry.run_id,
            "outcome": outcome,
            "rung": rung,
        }

    if process_liveness(pid) is Liveness.DEAD:
        con.print(
            styled(
                "[dim]Process already exited:[/dim] workflow '{}' (PID {}, port {})",
                workflow,
                pid,
                port,
            )
        )
        return _result("already-exited", "none")

    identity = _confirm_identity(entry, con)

    # Rung 1 — ask the workflow to cancel itself. This is the only rung that
    # lets the run write a resume checkpoint, so it is always tried first, and
    # only when we are sure we are talking to the right run.
    # ``port is not None`` is load-bearing, not a type appeasement: a
    # foreground run has no dashboard, so there is nothing to ask and this
    # rung is skipped straight to the signal below.
    if (
        identity is Identity.CONFIRMED
        and port is not None
        and _request_graceful_kill(port)
        and wait_for_exit(pid, _GRACEFUL_TIMEOUT) is Liveness.DEAD
    ):
        con.print(
            styled(
                "[green]Stopped[/green] workflow [cyan]'{}'[/cyan] (PID {}, port {})",
                workflow,
                pid,
                port,
            )
        )
        return _result("stopped", "api-kill")

    # Rung 2 — polite signal. Best-effort on both platforms. Skipped on a
    # positive mismatch, and ``--force`` does not lift that: ``--force``
    # overrides *uncertainty*, never positive evidence that this PID belongs to
    # someone else. An unconfirmable identity is not evidence of anything, and
    # refusing to signal there would be a regression for PID files written by
    # older versions, where a signal is all the previous code ever sent.
    if identity is Identity.MISMATCHED:
        con.print(
            styled(
                "[bold red]Could not stop[/bold red] workflow [cyan]'{}'[/cyan] "
                "(PID {}, port {}): the process on that port is a different run, "
                "so nothing was signalled.",
                workflow,
                pid,
                port,
            )
        )
        con.print(Text.from_markup("[dim]The PID file has been left in place.[/dim]"))
        return _result("mismatched", "refused")

    _signal_process(pid)
    if wait_for_exit(pid, _SIGNAL_TIMEOUT) is Liveness.DEAD:
        con.print(
            styled(
                "[green]Stopped[/green] workflow [cyan]'{}'[/cyan] (PID {}, port {})",
                workflow,
                pid,
                port,
            )
        )
        return _result("stopped", "signal")

    # Rung 3 — forceful, and irreversible. Re-confirm identity immediately
    # beforehand, *including* under ``--force``: several seconds of waiting have
    # elapsed since the first check, and if the target died in that window its
    # PID could now belong to an unrelated process. That window is precisely
    # what ``--force`` must not paper over, because this is the rung that cannot
    # be taken back.
    identity = _confirm_identity(entry, con)
    if identity is Identity.MISMATCHED:
        con.print(
            styled(
                "[bold red]Could not stop[/bold red] workflow [cyan]'{}'[/cyan] "
                "(PID {}, port {}): the process on that port is a different run, "
                "so it was not force-terminated.",
                workflow,
                pid,
                port,
            )
        )
        con.print(Text.from_markup("[dim]The PID file has been left in place.[/dim]"))
        return _result("mismatched", "refused")
    if not (identity is Identity.CONFIRMED or force):
        con.print(
            styled(
                "[bold red]Could not stop[/bold red] workflow [cyan]'{}'[/cyan] "
                "(PID {}, port {}): it is still running, and its identity could not be "
                "confirmed, so it was not force-terminated.",
                workflow,
                pid,
                port,
            )
        )
        con.print(
            Text.from_markup(
                "[dim]Re-run with --force if you are certain this PID is the workflow. "
                "The PID file has been left in place.[/dim]"
            )
        )
        return _result("unconfirmed", "refused")

    state = terminate_process(pid, _TERMINATE_TIMEOUT)
    if state is Liveness.DEAD:
        con.print(
            styled(
                "[green]Stopped[/green] workflow [cyan]'{}'[/cyan] "
                "(PID {}, port {}) [dim]— required forceful termination[/dim]",
                workflow,
                pid,
                port,
            )
        )
        return _result("stopped", "terminate")

    if state is Liveness.ALIVE:
        con.print(
            styled(
                "[bold red]Could not stop[/bold red] workflow [cyan]'{}'[/cyan] "
                "(PID {}, port {}): the process survived forceful termination.",
                workflow,
                pid,
                port,
            )
        )
        con.print(
            Text.from_markup(
                "[dim]The PID file has been left in place so the run stays discoverable.[/dim]"
            )
        )
        return _result("survived", "terminate")

    # Liveness.UNKNOWN — the probe itself failed, so we genuinely do not know
    # whether it died. Reporting "survived" here would assert more than we know.
    con.print(
        styled(
            "[bold yellow]Could not confirm[/bold yellow] whether workflow "
            "[cyan]'{}'[/cyan] (PID {}, port {}) stopped: the liveness probe failed.",
            workflow,
            pid,
            port,
        )
    )
    con.print(
        Text.from_markup(
            "[dim]The PID file has been left in place so the run stays discoverable.[/dim]"
        )
    )
    return _result("unconfirmed", "terminate")


def _format_started_at(value: object) -> str:
    """Render a PID file's ``started_at`` for the running-list table.

    ``write_pid_file`` records a full microsecond-precision ISO timestamp
    (32 characters), which crowds out the ``Dashboard`` column at a default
    80-column terminal width. This trims it to minute precision in UTC —
    the table is a glance-at listing, not an audit log; ``--json`` continues
    to report the exact recorded value untouched.

    Args:
        value: The raw ``started_at`` value read from the PID file JSON.

    Returns:
        ``"%Y-%m-%d %H:%MZ"`` in UTC, the raw string unchanged if it cannot
        be parsed as an ISO timestamp or normalized to UTC, or ``"?"`` if
        missing/empty/non-string.
    """
    if not isinstance(value, str) or not value:
        return "?"
    try:
        parsed = datetime.fromisoformat(value)
        # write_pid_file always writes a tz-aware UTC value; a naive one
        # implies an externally-written or hand-edited file. Treat it as
        # UTC rather than guessing the local timezone.
        parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        return parsed.strftime("%Y-%m-%d %H:%MZ")
    except (ValueError, OverflowError):
        # ValueError: not a parseable ISO timestamp. OverflowError: parsed
        # fine but astimezone() pushed a near-datetime.min/max value out of
        # range. Either way, one malformed entry must not crash the whole
        # listing (see pid.py's scan_pid_files for the same principle) —
        # fall back to the raw value rather than raise.
        logger.warning("Could not render started_at value %r as UTC; showing it as-is", value)
        return value


def _print_running_list(entries: list[RunRecord], con: Console, show_url: bool = False) -> None:
    """Print a table of running workflows.

    ``Started`` is rendered to minute precision (``_format_started_at``)
    regardless of ``show_url`` — ``conductor stop`` shares this function and
    gets the shorter timestamp too.

    Sources :class:`RunRecord` rather than PID-file dicts since the Fleet
    Manager, so foreground runs appear here too. That is why ``Port`` renders
    ``—`` for a portless ``fg`` run instead of indexing a key that isn't
    there, and why a ``Mode`` column exists at all: with foreground runs in
    the listing, "no port" and "no dashboard" need to be distinguishable.

    Args:
        entries: List of run records.
        con: Rich Console for output.
        show_url: Append a Dashboard URL column. Defaults to False;
            ``conductor status`` passes True, since discovery is its whole
            purpose and the URL is otherwise unrecoverable once the launching
            terminal is gone. That column folds rather than crops (see
            below), so a long workflow stem plus this column can wrap the
            row onto two lines rather than lose part of the URL.
    """
    from rich.table import Table

    table = Table(show_lines=False)
    table.add_column("Port", style="cyan")
    table.add_column("PID", style="yellow")
    if not show_url:
        # Mode and Run ID are omitted whenever the Dashboard column is shown:
        # together they push the table past 80 columns, which folds the URL
        # mid-string -- the exact defect issues #405/#413 fixed. Run ID earns
        # its place in `stop`'s listing because --run-id is the only selector
        # that can name a foreground run; `conductor status` exposes both
        # fields via --json instead, and a portless run already shows "—".
        table.add_column("Mode", style="magenta")
        table.add_column("Run ID", style="green")
    table.add_column("Workflow", style="white")
    # Folds rather than crops: _format_started_at's happy path is a fixed
    # 17 characters, but its fallback for an unparseable/out-of-range value
    # returns the raw string unbounded, which could otherwise reproduce the
    # exact cropping bug this PR fixes for Dashboard, one column over.
    table.add_column("Started", style="dim", overflow="fold")
    if show_url:
        # Folds onto a second line instead of cropping. A cropped URL is
        # unrecoverable from the output — the one thing this column exists
        # to surface — whereas a folded one is complete, just wrapped.
        table.add_column("Dashboard", style="blue", overflow="fold")

    for e in entries:
        row = [
            str(e.port) if e.port is not None else "—",
            str(e.pid),
        ]
        if not show_url:
            row += [e.mode, e.run_id or "—"]
        row += [
            e.workflow_name or Path(str(e.workflow_path or "unknown")).stem,
            _format_started_at(e.started_at),
        ]
        if show_url:
            row.append(f"http://127.0.0.1:{e.port}" if e.port is not None else "—")
        table.add_row(*row)

    con.print(table)


@app.command(name="gate-respond", hidden=True)
def gate_respond(
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
    """Deprecated alias for 'conductor gate respond'."""
    console.print(
        Text.from_markup(
            "[yellow]Warning:[/yellow] 'conductor gate-respond' is deprecated and will "
            "be removed in a future release. Use 'conductor gate respond' instead."
        )
    )
    from conductor.cli.gate import _gate_respond_impl

    _gate_respond_impl(port, choice, agent, input_text, token)


@app.command(rich_help_panel="Environment")
def update(
    force: bool = typer.Option(
        False,
        "--force",
        help="Accepted for backward compatibility; currently a no-op.",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Launch the install script automatically. Conductor will exit so "
            "file locks release; on Windows the installer opens in a new "
            "console window."
        ),
    ),
) -> None:
    """Check for and install the latest version of Conductor.

    By default, prints the OS-appropriate one-liner you can paste into a
    fresh shell. With ``--apply``, spawns the install script as a fully
    detached process and exits the current ``conductor`` so its file locks
    release — required for upgrade-while-running to succeed on Windows.

    \b
    Examples:
        conductor update           # check + print install command
        conductor update --apply   # check + launch installer, then exit
    """
    from conductor.cli.update import run_update

    try:
        run_update(console, force=force, apply=apply)
    except Exception as e:
        print_error(e)
        raise typer.Exit(code=1) from None


@app.command(rich_help_panel="Environment")
def doctor(
    section: Annotated[
        str | None,
        typer.Argument(
            help="Section to show: providers | registries | env. Default: all sections.",
        ),
    ] = None,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Instantiate providers and test their connections (network).",
        ),
    ] = False,
    models: Annotated[
        bool,
        typer.Option(
            "--models",
            help="List available models for each provider (implies --check).",
        ),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            "-p",
            help="Scope the providers section to a single provider.",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit machine-readable JSON instead of tables.",
        ),
    ] = False,
) -> None:
    """Report provider & environment diagnostics.

    A safe, read-only health check for your Conductor setup: which providers
    are installed, their stability tier, which credential environment
    variables are detected (presence only — values are never printed), plus
    Conductor version / update status and configured registries.

    Offline by default — no providers are instantiated and no credentials are
    required. (The default env section does a cache-first GitHub update check;
    set CONDUCTOR_NO_UPDATE_CHECK to disable it.) Use --check to actually test
    provider connections, and --models to list each provider's available
    models.

    \b
    Examples:
        conductor doctor                     # all sections
        conductor doctor providers           # providers section only
        conductor doctor --check             # test provider connections
        conductor doctor --models -p claude  # list Claude's models
        conductor doctor --json              # machine-readable output
    """
    from conductor.cli.doctor import run_doctor

    try:
        exit_code = run_doctor(
            section=section,
            provider=provider,
            check=check,
            models=models,
            as_json=as_json,
            console=output_console,
            err_console=console,
        )
    except Exception as e:
        print_error(e)
        raise typer.Exit(code=1) from None

    if exit_code != 0:
        raise typer.Exit(code=exit_code)
