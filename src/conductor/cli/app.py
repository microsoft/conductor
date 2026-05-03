"""Typer application definition for Conductor CLI.

This module defines the main Typer app and global options.
"""

from __future__ import annotations

import contextvars
import os
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from conductor import __version__


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

        # Skip when the subcommand is 'update'
        args = sys.argv[1:]
        subcommand = next((a for a in args if not a.startswith("-")), None)
        if subcommand != "update":
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
        conductor run workflow.yaml --silent --log-file auto
        conductor run workflow.yaml --no-interactive
        conductor run workflow.yaml --web
        conductor run workflow.yaml --web --web-port 8080
        conductor run workflow.yaml --web-bg
    """
    import asyncio
    import json

    from conductor.registry.cache import fetch_workflow as fetch_registry_workflow
    from conductor.registry.errors import RegistryError
    from conductor.registry.resolver import resolve_ref

    try:
        ref = resolve_ref(workflow)
        if ref.kind == "file":
            assert ref.path is not None
            workflow_path = ref.path
        else:
            assert ref.registry_name is not None
            assert ref.registry_entry is not None
            assert ref.workflow is not None
            workflow_path = fetch_registry_workflow(
                registry_name=ref.registry_name,
                registry_entry=ref.registry_entry,
                workflow_name=ref.workflow,
                version=ref.version,
            )
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
        from conductor.cli.bg_runner import launch_background

        try:
            url = launch_background(
                workflow_path=workflow_path,
                inputs=inputs,
                provider_override=provider,
                skip_gates=skip_gates,
                log_file=resolved_log_file,
                no_interactive=True,  # Always non-interactive in background
                web_port=web_port,
                metadata=cli_metadata,
            )
            console.print(f"[bold cyan]Dashboard:[/bold cyan] {url}")
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
            )
        )

        # Output as JSON to stdout
        output_console.print_json(json.dumps(result))

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
    from conductor.registry.cache import fetch_workflow as fetch_registry_workflow
    from conductor.registry.errors import RegistryError
    from conductor.registry.resolver import resolve_ref

    try:
        ref = resolve_ref(workflow)
        if ref.kind == "file":
            assert ref.path is not None
            workflow_path = ref.path
        else:
            assert ref.registry_name is not None
            assert ref.registry_entry is not None
            assert ref.workflow is not None
            workflow_path = fetch_registry_workflow(
                registry_name=ref.registry_name,
                registry_entry=ref.registry_entry,
                workflow_name=ref.workflow,
                version=ref.version,
            )
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
    from conductor.registry.cache import fetch_workflow as fetch_registry_workflow
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
            assert ref.registry_name is not None
            assert ref.registry_entry is not None
            assert ref.workflow is not None
            workflow_path = fetch_registry_workflow(
                registry_name=ref.registry_name,
                registry_entry=ref.registry_entry,
                workflow_name=ref.workflow,
                version=ref.version,
            )
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
        if ref.version:
            output_console.print(f"[bold]Version:[/bold]     {ref.version}")

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
) -> None:
    """Resume a workflow from a checkpoint after failure.

    Loads a previously saved checkpoint and resumes execution from
    the agent that failed. The checkpoint contains all prior agent
    outputs so execution continues seamlessly.

    Either provide a workflow file (to find the latest checkpoint) or
    use --from to specify a checkpoint file directly.

    \b
    Examples:
        conductor resume workflow.yaml
        conductor resume --from /tmp/conductor/checkpoints/my-workflow-20260224-153000.json
        conductor resume workflow.yaml --skip-gates
        conductor resume workflow.yaml --log-file auto
        conductor resume workflow.yaml --no-interactive
    """
    import asyncio
    import json

    from conductor.cli.run import generate_log_path, resume_workflow_async

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

    # Resolve workflow ref if provided
    resolved_workflow: Path | None = None
    if workflow is not None:
        from conductor.registry.cache import fetch_workflow as fetch_registry_workflow
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
                assert ref.registry_name is not None
                assert ref.registry_entry is not None
                assert ref.workflow is not None
                resolved_workflow = fetch_registry_workflow(
                    registry_name=ref.registry_name,
                    registry_entry=ref.registry_entry,
                    workflow_name=ref.workflow,
                    version=ref.version,
                )
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

    # Resolve log file path
    resolved_log_file: Path | None = None
    if log_file is not None:
        if log_file.lower() == "auto":
            name = resolved_workflow.stem if resolved_workflow else "resume"
            resolved_log_file = generate_log_path(name)
        else:
            resolved_log_file = Path(log_file)

    try:
        result = asyncio.run(
            resume_workflow_async(
                workflow_path=resolved_workflow,
                checkpoint_path=resolved_checkpoint,
                skip_gates=skip_gates,
                log_file=resolved_log_file,
                no_interactive=no_interactive,
            )
        )

        # Output as JSON to stdout
        output_console.print_json(json.dumps(result))

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
    table.add_column("Failed Agent", style="yellow")
    table.add_column("Error Type", style="red")
    table.add_column("File", style="dim")

    for cp in checkpoint_list:
        workflow_name = Path(cp.workflow_path).stem
        timestamp = cp.created_at
        failed_agent = cp.failure.get("agent", "unknown")
        error_type = cp.failure.get("error_type", "unknown")
        file_path = str(cp.file_path)

        table.add_row(workflow_name, timestamp, failed_agent, error_type, file_path)

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


@app.command()
def update() -> None:
    """Check for and install the latest version of Conductor.

    Fetches the latest release from GitHub and upgrades using
    ``uv tool install --locked --force git+https://github.com/microsoft/conductor.git@v{version}``.

    \b
    Examples:
        conductor update
    """
    from conductor.cli.update import run_update

    try:
        run_update(console)
    except Exception as e:
        print_error(e)
        raise typer.Exit(code=1) from None
