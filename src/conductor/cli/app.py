"""Typer application definition for Conductor CLI.

This module defines the main Typer app and global options.
"""

from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from conductor import __version__

# Create the main Typer app
app = typer.Typer(
    name="conductor",
    help="Conductor - Orchestrate multi-agent workflows defined in YAML.",
    add_completion=False,
    no_args_is_help=True,
)

# Rich console for formatted output
console = Console(stderr=True)
output_console = Console()

# Context variable for verbose mode (default True - show progress output)
verbose_mode: contextvars.ContextVar[bool] = contextvars.ContextVar("verbose_mode", default=True)

# Context variable for full verbose mode (--verbose flag - show full details)
full_mode: contextvars.ContextVar[bool] = contextvars.ContextVar("full_mode", default=False)


def is_verbose() -> bool:
    """Check if verbose mode is enabled (default True)."""
    return verbose_mode.get()


def is_full() -> bool:
    """Check if full verbose mode is enabled (--verbose flag).

    When full mode is enabled, prompts are shown untruncated and
    additional details like tool arguments and reasoning are displayed.
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
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-V",
            help="Show full prompts and detailed tool call information.",
        ),
    ] = False,
) -> None:
    """Conductor - Orchestrate multi-agent workflows defined in YAML."""
    full_mode.set(verbose)


@app.command()
def run(
    workflow: Annotated[
        Path,
        typer.Argument(
            help="Path to the workflow YAML file.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
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
) -> None:
    """Run a workflow from a YAML file.

    Execute a multi-agent workflow defined in the specified YAML file.
    Workflow inputs can be provided using --input flags.

    \b
    Examples:
        conductor run workflow.yaml
        conductor run workflow.yaml --input question="What is Python?"
        conductor run workflow.yaml -i question="Hello" -i context="Programming"
        conductor run workflow.yaml --provider copilot
        conductor run workflow.yaml --dry-run
        conductor run workflow.yaml --skip-gates
    """
    import asyncio
    import json

    # Import here to avoid circular imports and defer heavy imports
    from conductor.cli.run import (
        InputCollector,
        build_dry_run_plan,
        display_execution_plan,
        parse_input_flags,
        run_workflow_async,
    )

    # Handle dry-run mode
    if dry_run:
        try:
            plan = build_dry_run_plan(workflow)
            display_execution_plan(plan, output_console)
            return
        except Exception as e:
            print_error(e)
            raise typer.Exit(code=1) from None

    # Collect inputs from both --input and --input.* patterns
    inputs: dict[str, Any] = {}

    # Parse --input name=value style
    if raw_inputs:
        inputs.update(parse_input_flags(raw_inputs))

    # Also parse --input.name=value style from sys.argv
    inputs.update(InputCollector.extract_from_args())

    try:
        # Run the workflow
        result = asyncio.run(run_workflow_async(workflow, inputs, provider, skip_gates))

        # Output as JSON to stdout
        output_console.print_json(json.dumps(result))

    except Exception as e:
        print_error(e)
        raise typer.Exit(code=1) from None


@app.command()
def validate(
    workflow: Annotated[
        Path,
        typer.Argument(
            help="Path to the workflow YAML file to validate.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
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
    """
    from conductor.cli.validate import (
        display_validation_success,
        validate_workflow,
    )

    is_valid, config = validate_workflow(workflow, output_console)

    if is_valid and config is not None:
        display_validation_success(config, workflow, output_console)
    else:
        raise typer.Exit(code=1)


@app.command()
def init(
    name: Annotated[
        str,
        typer.Argument(
            help="Name for the new workflow.",
        ),
    ],
    template: Annotated[
        str,
        typer.Option(
            "--template",
            "-t",
            help="Template to use (see 'conductor templates' for options).",
        ),
    ] = "simple",
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Output file path. Defaults to <name>.yaml in current directory.",
        ),
    ] = None,
) -> None:
    """Initialize a new workflow file from a template.

    Creates a new workflow YAML file based on the specified template.
    Use 'conductor templates' to see available templates.

    \b
    Examples:
        conductor init my-workflow
        conductor init my-workflow --template loop
        conductor init my-workflow -t human-gate -o ./workflows/my-workflow.yaml
    """
    from conductor.cli.init import create_workflow_file, get_template

    # Check if template exists
    template_info = get_template(template)
    if template_info is None:
        console.print(f"[bold red]Error:[/bold red] Template '{template}' not found.")
        console.print("[dim]Use 'conductor templates' to see available templates.[/dim]")
        raise typer.Exit(code=1)

    try:
        create_workflow_file(name, template, output, output_console)
    except FileExistsError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        console.print("[dim]Use --output to specify a different path.[/dim]")
        raise typer.Exit(code=1) from None
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1) from None


@app.command()
def templates() -> None:
    """List available workflow templates.

    Shows all templates that can be used with 'conductor init'.

    \b
    Examples:
        conductor templates
    """
    from conductor.cli.init import display_templates

    display_templates(output_console)
