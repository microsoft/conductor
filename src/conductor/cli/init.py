"""Implementation of the 'conductor init' and 'conductor templates' commands.

This module provides functionality to initialize new workflow files
from templates and list available templates.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table


@dataclass
class TemplateInfo:
    """Information about a workflow template."""

    name: str
    description: str
    filename: str
    features: list[str]


# Register available templates
TEMPLATES: dict[str, TemplateInfo] = {
    "simple": TemplateInfo(
        name="simple",
        description="A minimal linear workflow with a single agent",
        filename="simple.yaml",
        features=["single agent", "linear flow"],
    ),
    "loop": TemplateInfo(
        name="loop",
        description="A workflow with loop-back pattern for iterative refinement",
        filename="loop.yaml",
        features=["multiple agents", "loop-back", "conditional routing"],
    ),
    "human-gate": TemplateInfo(
        name="human-gate",
        description="A workflow with human-in-the-loop approval gate",
        filename="human-gate.yaml",
        features=["human gate", "interactive", "approval workflow"],
    ),
}


def get_template_dir() -> Path:
    """Get the path to the templates directory."""
    return Path(__file__).parent.parent / "templates"


def list_templates() -> list[TemplateInfo]:
    """Get a list of all available templates.

    Returns:
        List of TemplateInfo objects for each template.
    """
    return list(TEMPLATES.values())


def get_template(name: str) -> TemplateInfo | None:
    """Get a template by name.

    Args:
        name: The template name.

    Returns:
        TemplateInfo if found, None otherwise.
    """
    return TEMPLATES.get(name)


def render_template(template_name: str, workflow_name: str) -> str:
    """Render a template with the given workflow name.

    Args:
        template_name: Name of the template to use.
        workflow_name: Name for the new workflow.

    Returns:
        The rendered template content.

    Raises:
        ValueError: If the template is not found.
    """
    template_info = get_template(template_name)
    if template_info is None:
        raise ValueError(f"Template '{template_name}' not found")

    template_path = get_template_dir() / template_info.filename
    if not template_path.exists():
        raise ValueError(f"Template file not found: {template_path}")

    content = template_path.read_text(encoding="utf-8")

    # Simple template substitution (replacing {{ name }} with workflow_name)
    # We use a simple string replacement to avoid conflicts with Jinja2
    # syntax that may be in the template
    content = content.replace("{{ name }}", workflow_name)

    return content


def display_templates(console: Console | None = None) -> None:
    """Display available templates with Rich formatting.

    Args:
        console: Optional Rich console for output.
    """
    output_console = console if console is not None else Console()

    table = Table(title="Available Workflow Templates", show_lines=True)
    table.add_column("Name", style="cyan", width=12)
    table.add_column("Description", width=50)
    table.add_column("Features")

    for template in list_templates():
        features_str = ", ".join(template.features)
        table.add_row(template.name, template.description, features_str)

    output_console.print(table)
    output_console.print()
    output_console.print(
        "[dim]Use 'conductor init <workflow-name> --template <name>' "
        "to create a new workflow from a template.[/dim]"
    )


def create_workflow_file(
    workflow_name: str,
    template_name: str = "simple",
    output_path: Path | None = None,
    console: Console | None = None,
) -> Path:
    """Create a new workflow file from a template.

    Args:
        workflow_name: Name for the new workflow.
        template_name: Template to use (default: "simple").
        output_path: Optional output path. Defaults to <workflow_name>.yaml.
        console: Optional Rich console for output.

    Returns:
        Path to the created file.

    Raises:
        ValueError: If the template is not found.
        FileExistsError: If the output file already exists.
    """
    output_console = console if console is not None else Console()

    # Determine output path
    if output_path is None:
        # Sanitize workflow name for filename
        safe_name = workflow_name.replace(" ", "-").lower()
        output_path = Path(f"{safe_name}.yaml")

    # Check if file already exists
    if output_path.exists():
        raise FileExistsError(f"File already exists: {output_path}")

    # Render and write template
    content = render_template(template_name, workflow_name)
    output_path.write_text(content, encoding="utf-8")

    output_console.print(f"[green]Created workflow file:[/green] {output_path}")
    output_console.print(f"[dim]Template used:[/dim] {template_name}")

    return output_path
