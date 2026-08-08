"""Typer application definition for Conductor CLI.

This module defines the main Typer app and global options.
"""

from __future__ import annotations

import contextvars
import logging
import os
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from conductor import __version__
from conductor.exceptions import WorkflowTerminated

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
from conductor.cli.gate import gate_app  # noqa: E402
from conductor.cli.registry import registry_app  # noqa: E402

app.add_typer(registry_app, rich_help_panel="Environment")
app.add_typer(gate_app, rich_help_panel="Interact")
app.add_typer(checkpoint_app, rich_help_panel="State")

# Rich console for formatted output
console = Console(stderr=True)
output_console = Console()

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
        title=f"[bold red]❌ {error_type}[/bold red]",
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
            title=f"[bold red]❌ {type(error).__name__}[/bold red]",
            border_style="red",
            padding=(1, 2),
        )
        console.print(panel)


_INTERACTIVE_STEP_TYPES = ("human_gate", "questions")
"""Step types that park the workflow waiting on a human."""


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
        "[yellow]This workflow contains steps that wait for you[/yellow] "
        "(human_gate / questions). Resolve them from "
        "the dashboard above, or run "
        f"[bold]conductor gate respond --port {port_hint} --choice <value>[/bold]."
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
            help="Workflow file path or registry reference (name[@registry][@version]).",
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
                console.print(f"[bold cyan]Dashboard:[/bold cyan] {launch.url}")
                console.print(f"[dim]Child stderr log: {launch.stderr_log}[/dim]")
                console.print(
                    "[dim]Workflow running in background. Dashboard auto-shuts down after "
                    "workflow completes and all clients disconnect.[/dim]"
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
                f"[yellow]Warning:[/yellow] could not serialise terminate output: {json_exc}"
            )
        console.print(f"[red]Workflow terminated[/red] at '{e.terminated_by}': {e.reason}")
        if e.suggestion:
            console.print(f"[dim]Suggestion: {e.suggestion}[/dim]")
        raise typer.Exit(code=1) from None
    except Exception as e:
        print_error(e)
        raise typer.Exit(code=1) from None


@app.command(rich_help_panel="Author & Inspect")
def validate(
    workflow: Annotated[
        str,
        typer.Argument(
            help="Workflow file path or registry reference (name[@registry][@version]).",
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
            help="Workflow file path or registry reference (name[@registry][@version]).",
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
                console.print(f"[bold red]Error:[/bold red] Workflow file not found: {workflow}")
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
        console.print(f"[bold red]Error:[/bold red] Failed to parse workflow: {e}")
        raise typer.Exit(code=1) from None

    wf = config.workflow
    output_console.print(f"[bold]Name:[/bold]        {wf.name}")
    if wf.description:
        output_console.print(f"[bold]Description:[/bold] {wf.description}")
    output_console.print(f"[bold]Entry point:[/bold] {wf.entry_point}")
    output_console.print(f"[bold]Source:[/bold]      {workflow_path}")

    if ref.kind == "registry":
        output_console.print(f"[bold]Registry:[/bold]    {ref.registry_name}")
        if ref.ref:
            output_console.print(f"[bold]Version:[/bold]     {ref.ref}")

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
        output_console.print(f"\n[dim]conductor run {ref_str} {input_args}[/dim]")
    else:
        output_console.print(f"\n[dim]conductor run {ref_str}[/dim]")


@app.command(rich_help_panel="Run & Recover")
def resume(
    workflow: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Workflow file path or registry reference (name[@registry][@version]). "
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
    """
    import asyncio
    import json

    from conductor.cli.run import (
        generate_log_path,
        parse_metadata_flags,
        resume_workflow_async,
    )

    # Validate arguments
    if workflow is None and from_checkpoint is None:
        console.print(
            "[bold red]Error:[/bold red] "
            "Provide a workflow file or use --from to specify a checkpoint."
        )
        console.print(
            "[dim]Usage: conductor resume workflow.yaml "
            "or conductor resume --from <checkpoint.json>[/dim]"
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
                        f"[bold red]Error:[/bold red] Workflow file not found: {workflow}"
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
                f"[bold red]Error:[/bold red] Checkpoint file not found: {from_checkpoint}"
            )
            raise typer.Exit(code=1)

    # Parse --metadata key=value flags (no type coercion)
    cli_metadata: dict[str, str] = {}
    if raw_metadata:
        cli_metadata.update(parse_metadata_flags(raw_metadata))

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
            )
            if is_verbose():
                console.print(f"[bold cyan]Dashboard:[/bold cyan] {launch.url}")
                console.print(f"[dim]Child stderr log: {launch.stderr_log}[/dim]")
                console.print(
                    "[dim]Resumed workflow running in background. Dashboard auto-shuts down "
                    "after workflow completes and all clients disconnect.[/dim]"
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
                f"[yellow]Warning:[/yellow] could not serialise terminate output: {json_exc}"
            )
        console.print(f"[red]Workflow terminated[/red] at '{e.terminated_by}': {e.reason}")
        if e.suggestion:
            console.print(f"[dim]Suggestion: {e.suggestion}[/dim]")
        raise typer.Exit(code=1) from None
    except Exception as e:
        print_error(e)
        raise typer.Exit(code=1) from None


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
        "[yellow]Warning:[/yellow] 'conductor checkpoints' is deprecated and will "
        "be removed in a future release. Use 'conductor checkpoint list' instead."
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
            console.print(f"\n[bold green]▶ Replay dashboard:[/] {dashboard.url}\n")
            console.print("[dim]Press Ctrl+C to exit[/dim]\n")

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
            console.print("\n[dim]Replay stopped.[/dim]")


@app.command(rich_help_panel="Run & Recover")
def stop(
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            help="Stop the background workflow running on this port.",
        ),
    ] = None,
    all_workflows: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Stop all background conductor workflows.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Force-terminate even when the run's identity cannot be confirmed. "
                "Dangerous: the recorded PID may have been recycled onto another process."
            ),
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
    """Stop background workflow processes launched with --web-bg.

    With no arguments, lists running background workflows. If exactly one
    is found, stops it automatically. If multiple are found, prints the
    list and asks you to specify --port.

    Each workflow is stopped by escalating until it is confirmed gone: a
    graceful cancel via the dashboard (which lets the run checkpoint), then a
    platform signal, then forceful termination. A PID file is only removed
    once its process is confirmed dead, so a workflow that survives stays
    discoverable instead of becoming an untracked orphan.

    Forceful termination requires confirming the run's identity against its
    dashboard, because a recorded PID may since have been recycled onto an
    unrelated process. Use --force to override that check.

    \b
    Exit codes:
        0  every targeted workflow is confirmed stopped (or was already gone)
        1  --port matched no running workflow, or the target was ambiguous
        2  at least one workflow survived or could not be confirmed stopped

    \b
    Examples:
        conductor stop
        conductor stop --port 8080
        conductor stop --all
        conductor stop --all --json
    """
    import json

    from conductor.cli.pid import read_pid_files, remove_pid_file_at

    running = read_pid_files()

    if not running:
        if json_output:
            output_console.print_json(json.dumps({"stopped": [], "failed": []}), ensure_ascii=True)
        else:
            console.print("[dim]No background workflows are currently running.[/dim]")
        return

    if all_workflows:
        targets = running
    elif port is not None:
        targets = [e for e in running if e["port"] == port]
        if not targets:
            if json_output:
                output_console.print_json(
                    json.dumps({"error": f"no background workflow on port {port}"}),
                    ensure_ascii=True,
                )
            else:
                console.print(
                    f"[bold red]Error:[/bold red] No background workflow found on port {port}."
                )
                console.print("[dim]Running workflows:[/dim]")
                _print_running_list(running, console)
            raise typer.Exit(code=1)
    elif len(running) == 1:
        targets = running
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
                f"[bold yellow]Multiple background workflows running "
                f"({len(running)}).[/bold yellow]"
            )
            console.print(
                "[dim]Specify --port to stop a specific one, or --all to stop all.[/dim]\n"
            )
            _print_running_list(running, console)
        raise typer.Exit(code=1)

    # Prose goes to ``console`` (stderr); JSON goes to ``output_console``
    # (stdout). They cannot corrupt each other, so diagnostics stay visible
    # even in --json mode.
    results = [_stop_process(entry, console, force=force) for entry in targets]

    for entry, result in zip(targets, results, strict=True):
        if result["outcome"] in ("stopped", "already-exited"):
            # Identity-checked: only remove the file if it still describes the
            # process we just stopped, never merely "the file for this port".
            remove_pid_file_at(entry["file"], entry["pid"])

    if json_output:
        payload = {
            "stopped": [r for r in results if r["outcome"] in ("stopped", "already-exited")],
            "failed": [r for r in results if r["outcome"] not in ("stopped", "already-exited")],
        }
        output_console.print_json(json.dumps(payload), ensure_ascii=True)

    if any(r["outcome"] not in ("stopped", "already-exited") for r in results):
        raise typer.Exit(code=2)


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


def _confirm_identity(entry: dict, con: Console) -> Identity:
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

    port = entry["port"]
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
        if reported_pid == entry["pid"]:
            return Identity.CONFIRMED
        con.print(
            f"[bold yellow]Warning:[/bold yellow] the dashboard on port {port} is PID "
            f"{reported_pid}, but the PID file records {entry['pid']}. Refusing to act on it."
        )
        return Identity.MISMATCHED

    # Older dashboard: fall back to run_id when both sides have one.
    expected = str(entry.get("run_id") or "")
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


def _stop_process(entry: dict, con: Console, force: bool = False) -> dict:
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

    pid = entry["pid"]
    port = entry["port"]
    workflow = Path(entry.get("workflow", "unknown")).stem

    def _result(outcome: str, rung: str) -> dict:
        return {
            "pid": pid,
            "port": port,
            "workflow": workflow,
            "run_id": entry.get("run_id", ""),
            "outcome": outcome,
            "rung": rung,
        }

    if process_liveness(pid) is Liveness.DEAD:
        con.print(
            f"[dim]Process already exited:[/dim] workflow '{workflow}' (PID {pid}, port {port})"
        )
        return _result("already-exited", "none")

    identity = _confirm_identity(entry, con)

    # Rung 1 — ask the workflow to cancel itself. This is the only rung that
    # lets the run write a resume checkpoint, so it is always tried first, and
    # only when we are sure we are talking to the right run.
    if (
        identity is Identity.CONFIRMED
        and _request_graceful_kill(port)
        and wait_for_exit(pid, _GRACEFUL_TIMEOUT) is Liveness.DEAD
    ):
        con.print(
            f"[green]Stopped[/green] workflow [cyan]'{workflow}'[/cyan] (PID {pid}, port {port})"
        )
        return _result("stopped", "api-kill")

    # Rung 2 — polite signal. Best-effort on both platforms. Skipped only on a
    # positive mismatch: an unconfirmable identity is not evidence of anything,
    # and refusing to signal there would be a regression for PID files written
    # by older versions, where a signal is all the previous code ever sent.
    if identity is Identity.MISMATCHED and not force:
        con.print(
            f"[bold red]Could not stop[/bold red] workflow [cyan]'{workflow}'[/cyan] "
            f"(PID {pid}, port {port}): the process on that port is a different run, "
            f"so nothing was signalled."
        )
        con.print("[dim]The PID file has been left in place.[/dim]")
        return _result("unconfirmed", "refused")

    _signal_process(pid)
    if wait_for_exit(pid, _SIGNAL_TIMEOUT) is Liveness.DEAD:
        con.print(
            f"[green]Stopped[/green] workflow [cyan]'{workflow}'[/cyan] (PID {pid}, port {port})"
        )
        return _result("stopped", "signal")

    # Rung 3 — forceful, and irreversible. Re-confirm identity immediately
    # beforehand: several seconds of waiting have elapsed since the first
    # check, and if the target died in that window its PID could now belong to
    # an unrelated process.
    if not force:
        identity = _confirm_identity(entry, con)
    if not (identity is Identity.CONFIRMED or force):
        con.print(
            f"[bold red]Could not stop[/bold red] workflow [cyan]'{workflow}'[/cyan] "
            f"(PID {pid}, port {port}): it is still running, and its identity could not be "
            f"confirmed, so it was not force-terminated."
        )
        con.print(
            "[dim]Re-run with --force if you are certain this PID is the workflow. "
            "The PID file has been left in place.[/dim]"
        )
        return _result("unconfirmed", "refused")

    state = terminate_process(pid, _TERMINATE_TIMEOUT)
    if state is Liveness.DEAD:
        con.print(
            f"[green]Stopped[/green] workflow [cyan]'{workflow}'[/cyan] "
            f"(PID {pid}, port {port}) [dim]— required forceful termination[/dim]"
        )
        return _result("stopped", "terminate")

    if state is Liveness.ALIVE:
        con.print(
            f"[bold red]Could not stop[/bold red] workflow [cyan]'{workflow}'[/cyan] "
            f"(PID {pid}, port {port}): the process survived forceful termination."
        )
        con.print("[dim]The PID file has been left in place so the run stays discoverable.[/dim]")
        return _result("survived", "terminate")

    # Liveness.UNKNOWN — the probe itself failed, so we genuinely do not know
    # whether it died. Reporting "survived" here would assert more than we know.
    con.print(
        f"[bold yellow]Could not confirm[/bold yellow] whether workflow "
        f"[cyan]'{workflow}'[/cyan] (PID {pid}, port {port}) stopped: the liveness probe failed."
    )
    con.print("[dim]The PID file has been left in place so the run stays discoverable.[/dim]")
    return _result("unconfirmed", "terminate")


def _print_running_list(entries: list[dict], con: Console) -> None:
    """Print a table of running background workflows.

    Args:
        entries: List of PID-file dicts.
        con: Rich Console for output.
    """
    from rich.table import Table

    table = Table(show_lines=False)
    table.add_column("Port", style="cyan")
    table.add_column("PID", style="yellow")
    table.add_column("Workflow", style="white")
    table.add_column("Started", style="dim")

    for e in entries:
        table.add_row(
            str(e["port"]),
            str(e["pid"]),
            Path(e.get("workflow", "unknown")).stem,
            e.get("started_at", "?"),
        )

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
        "[yellow]Warning:[/yellow] 'conductor gate-respond' is deprecated and will "
        "be removed in a future release. Use 'conductor gate respond' instead."
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
