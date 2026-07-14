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
from conductor.cli.registry import registry_app  # noqa: E402

app.add_typer(registry_app)

# Rich console for formatted output
console = Console(stderr=True)
output_console = Console()

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


def _abort_web_bg_if_human_gate(workflow_path: Path, *, skip_gates: bool) -> None:
    """Reject ``--web-bg`` when the workflow has a ``human_gate`` agent.

    Without this check, ``--web-bg`` forks a detached child whose stdin is
    redirected to ``DEVNULL``; ``Prompt.ask`` then raises ``EOFError`` and
    the parent only reports ``"Background process exited immediately"``,
    which never mentions ``human_gate`` or ``--skip-gates``. Failing fast
    in the parent process produces a single visible error on the user's
    terminal. ``--skip-gates`` is a documented escape hatch and is honored.
    """
    if skip_gates:
        return
    try:
        from conductor.config.loader import load_config

        config = load_config(workflow_path)
    except Exception:  # noqa: BLE001 — defer real validation to the loader path
        # If config fails to load, let the normal run path surface the error.
        return
    has_gate = any(getattr(a, "type", None) == "human_gate" for a in config.agents) or any(
        getattr(getattr(fe, "agent", None), "type", None) == "human_gate" for fe in config.for_each
    )
    if not has_gate:
        return
    # Emit via plain typer.echo (not typer.BadParameter) so the message renders
    # verbatim — BadParameter is rendered as a Rich panel whose text wrapping
    # can split long flag names like ``--skip-gates`` across border lines in
    # narrow terminals (e.g. CI runners), hiding the remediation hint.
    message = (
        "Error: --web-bg is incompatible with workflows that contain human_gate "
        "steps because the detached process has no stdin to prompt on.\n"
        "\n"
        "Options:\n"
        "  1. Use --web (foreground) instead of --web-bg\n"
        "  2. Add --skip-gates to auto-accept the first option\n"
        "  3. Remove human_gate steps from the workflow\n"
        "  4. Use `conductor gate-respond --port <port> --choice <value>` to resolve from CLI"
    )
    typer.echo(message, err=True)
    raise typer.Exit(code=2)


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


@app.command()
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
        _abort_web_bg_if_human_gate(workflow_path, skip_gates=skip_gates)
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


@app.command()
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


@app.command()
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


@app.command()
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
        # original workflow path. Read it so the human_gate guard still fires
        # and the user gets a single visible error instead of a silent crash
        # in the detached child.
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
        if gate_check_workflow is not None:
            _abort_web_bg_if_human_gate(gate_check_workflow, skip_gates=skip_gates)
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


@app.command()
def checkpoints(
    workflow: Annotated[
        Path | None,
        typer.Argument(
            help="Path to a workflow YAML file. Filters checkpoints to this workflow only.",
        ),
    ] = None,
) -> None:
    """List available workflow checkpoints.

    Shows all checkpoint files with metadata including workflow name,
    timestamp, failed agent, and error type. Optionally filter by
    workflow file.

    \b
    Examples:
        conductor checkpoints
        conductor checkpoints workflow.yaml
    """
    from rich.table import Table

    from conductor.engine.checkpoint import CheckpointManager

    # Resolve workflow path for filtering
    resolved_workflow: Path | None = None
    if workflow is not None:
        resolved_workflow = workflow.resolve()
        if not resolved_workflow.exists():
            console.print(f"[bold red]Error:[/bold red] Workflow file not found: {workflow}")
            raise typer.Exit(code=1)

    checkpoint_list = CheckpointManager.list_checkpoints(resolved_workflow)

    if not checkpoint_list:
        if resolved_workflow:
            output_console.print(
                f"[dim]No checkpoints found for workflow: {resolved_workflow.name}[/dim]"
            )
        else:
            output_console.print("[dim]No checkpoints found.[/dim]")
        return

    table = Table(title="Workflow Checkpoints", show_lines=True)
    table.add_column("Workflow", style="cyan")
    table.add_column("Timestamp", style="green")
    table.add_column("Trigger", style="magenta")
    table.add_column("Agent", style="yellow")
    table.add_column("Error Type", style="red", no_wrap=True, min_width=13)
    # File path absorbs truncation so the triage columns stay readable.
    table.add_column("File", style="dim", overflow="ellipsis")

    for cp in checkpoint_list:
        workflow_name = Path(cp.workflow_path).stem
        timestamp = cp.created_at
        trigger = cp.trigger
        # For failure checkpoints this is the failed agent; for periodic
        # checkpoints it is the step that was about to run.
        agent = cp.failure.get("agent") or "unknown"
        # Periodic checkpoints have no error; show an em dash.
        error_type = cp.failure.get("error_type") or "—"
        file_path = str(cp.file_path)

        table.add_row(workflow_name, timestamp, trigger, agent, error_type, file_path)

    output_console.print(table)
    output_console.print(f"\n[dim]Total: {len(checkpoint_list)} checkpoint(s)[/dim]")


@app.command()
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


@app.command()
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
) -> None:
    """Stop background workflow processes launched with --web-bg.

    With no arguments, lists running background workflows. If exactly one
    is found, stops it automatically. If multiple are found, prints the
    list and asks you to specify --port.

    \b
    Examples:
        conductor stop
        conductor stop --port 8080
        conductor stop --all
    """
    from conductor.cli.pid import read_pid_files, remove_pid_file

    running = read_pid_files()

    if not running:
        console.print("[dim]No background workflows are currently running.[/dim]")
        return

    if all_workflows:
        for entry in running:
            _stop_process(entry, console)
            remove_pid_file(entry["port"])
        return

    if port is not None:
        # Find the entry for the specified port
        match = [e for e in running if e["port"] == port]
        if not match:
            console.print(
                f"[bold red]Error:[/bold red] No background workflow found on port {port}."
            )
            console.print("[dim]Running workflows:[/dim]")
            _print_running_list(running, console)
            raise typer.Exit(code=1)
        _stop_process(match[0], console)
        remove_pid_file(port)
        return

    # No flags: auto-stop if exactly one, otherwise list
    if len(running) == 1:
        entry = running[0]
        _stop_process(entry, console)
        remove_pid_file(entry["port"])
    else:
        console.print(
            f"[bold yellow]Multiple background workflows running ({len(running)}).[/bold yellow]"
        )
        console.print("[dim]Specify --port to stop a specific one, or --all to stop all.[/dim]\n")
        _print_running_list(running, console)


def _stop_process(entry: dict, con: Console) -> None:
    """Send SIGTERM (or equivalent) to a background workflow process.

    Args:
        entry: A PID-file dict with ``pid``, ``port``, ``workflow`` keys.
        con: Rich Console for output.
    """
    import signal
    import sys

    pid = entry["pid"]
    port = entry["port"]
    workflow = Path(entry.get("workflow", "unknown")).stem

    try:
        if sys.platform == "win32":
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.kill(pid, signal.SIGTERM)
        con.print(
            f"[green]Stopped[/green] workflow [cyan]'{workflow}'[/cyan] (PID {pid}, port {port})"
        )
    except ProcessLookupError:
        con.print(
            f"[dim]Process already exited:[/dim] workflow '{workflow}' (PID {pid}, port {port})"
        )
    except PermissionError:
        con.print(
            f"[bold red]Permission denied:[/bold red] could not stop PID {pid}. "
            f"Try running with elevated privileges."
        )
    except OSError as exc:
        # Defensive catch (companion to the fix for issue #166): on Windows,
        # ``os.kill`` can raise OSError subclasses for edge cases such as the
        # target not being a console process group leader, or a probe-failing
        # PID that the "assume alive" fallback in ``_is_process_alive_windows``
        # let through.  Treating these as "already exited" lets ``conductor
        # stop`` continue and clean up the PID file rather than crash.
        logger.warning(
            "Unexpected OSError stopping PID %s; treating as already exited", pid, exc_info=True
        )
        con.print(
            f"[yellow]Could not signal PID {pid} ({exc}); "
            f"removing PID file for workflow '{workflow}' anyway.[/yellow]"
        )


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


@app.command(name="gate-respond")
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
    """Resolve a parked human gate from the command line.

    Sends a gate response to a running workflow's web dashboard via HTTP.
    Use this when the dashboard UI is unreachable (e.g. SSH session).

    \b
    Examples:
        conductor gate-respond --port 8080 --choice approve
        conductor gate-respond -p 8080 -c reject --agent review-gate
        conductor gate-respond -p 8080 -c approve --token secret123
        conductor gate-respond -p 8080 -c approve --input "Looks good"
    """
    import httpx

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


@app.command()
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


@app.command()
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
