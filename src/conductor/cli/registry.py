"""Remote template registry for discovering and sharing community workflows.

This module provides functionality to:
- Fetch and list community workflow templates from a remote GitHub-based registry
- Download and scaffold projects from remote templates
- Validate and publish workflows to the registry

The registry is backed by a GitHub repository containing YAML workflow files
with metadata (similar to Homebrew formulae).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)

# Registry configuration
REGISTRY_OWNER = "microsoft"
REGISTRY_REPO = "conductor-workflows"
REGISTRY_BRANCH = "main"
REGISTRY_INDEX_PATH = "registry/index.json"
REGISTRY_TEMPLATES_DIR = "registry/templates"
_FETCH_TIMEOUT_SECONDS = 10

# GitHub raw content URL template
_RAW_URL = "https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
# GitHub API URL template for contents
_API_CONTENTS_URL = "https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"


@dataclass
class RegistryTemplate:
    """Metadata for a remote registry template."""

    name: str
    description: str
    author: str
    tags: list[str] = field(default_factory=list)
    conductor_version: str = ""
    filename: str = ""
    url: str = ""


class RegistryError(Exception):
    """Error communicating with the template registry."""


def _build_raw_url(path: str) -> str:
    """Build a raw GitHub content URL for the registry.

    Args:
        path: Path within the registry repository.

    Returns:
        Full URL to the raw content.
    """
    return _RAW_URL.format(
        owner=REGISTRY_OWNER,
        repo=REGISTRY_REPO,
        branch=REGISTRY_BRANCH,
        path=path,
    )


def _build_api_url(path: str) -> str:
    """Build a GitHub API contents URL for the registry.

    Args:
        path: Path within the registry repository.

    Returns:
        Full URL to the API endpoint.
    """
    return _API_CONTENTS_URL.format(
        owner=REGISTRY_OWNER,
        repo=REGISTRY_REPO,
        branch=REGISTRY_BRANCH,
        path=path,
    )


def _fetch_url(url: str, timeout: int = _FETCH_TIMEOUT_SECONDS) -> bytes:
    """Fetch content from a URL.

    Args:
        url: URL to fetch.
        timeout: Request timeout in seconds.

    Returns:
        Response body as bytes.

    Raises:
        RegistryError: If the request fails.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "conductor-cli",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RegistryError(f"Registry resource not found: {url}") from e
        raise RegistryError(f"Registry request failed (HTTP {e.code}): {url}") from e
    except urllib.error.URLError as e:
        raise RegistryError(f"Could not connect to registry: {e.reason}") from e
    except Exception as e:
        raise RegistryError(f"Failed to fetch from registry: {e}") from e


def fetch_registry_index() -> list[RegistryTemplate]:
    """Fetch the template index from the remote registry.

    The index is a JSON file listing all available community templates
    with their metadata.

    Returns:
        List of RegistryTemplate objects.

    Raises:
        RegistryError: If the index cannot be fetched or parsed.
    """
    url = _build_raw_url(REGISTRY_INDEX_PATH)
    try:
        data = _fetch_url(url)
        index = json.loads(data.decode("utf-8"))
    except RegistryError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RegistryError(f"Invalid registry index format: {e}") from e

    if not isinstance(index, dict) or "templates" not in index:
        raise RegistryError("Invalid registry index: missing 'templates' key")

    templates = []
    for entry in index["templates"]:
        if not isinstance(entry, dict) or "name" not in entry:
            continue
        templates.append(
            RegistryTemplate(
                name=entry["name"],
                description=entry.get("description", ""),
                author=entry.get("author", ""),
                tags=entry.get("tags", []),
                conductor_version=entry.get("conductor_version", ""),
                filename=entry.get("filename", f"{entry['name']}.yaml"),
                url=entry.get("url", ""),
            )
        )

    return templates


def fetch_remote_template(template_name: str) -> str:
    """Fetch a specific template's YAML content from the registry.

    Args:
        template_name: Name of the template to fetch.

    Returns:
        The YAML content of the template.

    Raises:
        RegistryError: If the template cannot be fetched.
    """
    # First, try to find the template in the index for its filename
    try:
        templates = fetch_registry_index()
    except RegistryError:
        # If index fetch fails, try direct path convention
        templates = []

    filename = f"{template_name}.yaml"
    for t in templates:
        if t.name == template_name:
            filename = t.filename or f"{template_name}.yaml"
            break

    path = f"{REGISTRY_TEMPLATES_DIR}/{filename}"
    url = _build_raw_url(path)

    try:
        data = _fetch_url(url)
        return data.decode("utf-8")
    except RegistryError as e:
        raise RegistryError(f"Template '{template_name}' not found in registry: {e}") from e


def display_remote_templates(console: Console | None = None) -> None:
    """Display remote registry templates with Rich formatting.

    Args:
        console: Optional Rich console for output.
    """
    output_console = console if console is not None else Console()

    try:
        templates = fetch_registry_index()
    except RegistryError as e:
        output_console.print(f"[bold red]Error:[/bold red] Could not fetch remote templates: {e}")
        output_console.print("[dim]Check your internet connection and try again.[/dim]")
        return

    if not templates:
        output_console.print("[yellow]No community templates available yet.[/yellow]")
        output_console.print("[dim]Use 'conductor publish' to share your workflows![/dim]")
        return

    table = Table(title="Community Workflow Templates (Registry)", show_lines=True)
    table.add_column("Name", style="cyan", width=20)
    table.add_column("Description", width=40)
    table.add_column("Author", style="green", width=15)
    table.add_column("Tags", width=20)

    for template in templates:
        tags_str = ", ".join(template.tags) if template.tags else ""
        table.add_row(
            template.name,
            template.description,
            template.author,
            tags_str,
        )

    output_console.print(table)
    output_console.print()
    output_console.print(
        "[dim]Use 'conductor init <name> --template registry:<template-name>' "
        "to create a workflow from a community template.[/dim]"
    )


def render_remote_template(template_name: str, workflow_name: str) -> str:
    """Fetch and render a remote template with the given workflow name.

    Args:
        template_name: Name of the remote template (without 'registry:' prefix).
        workflow_name: Name for the new workflow.

    Returns:
        The rendered template content.

    Raises:
        RegistryError: If the template cannot be fetched.
    """
    content = fetch_remote_template(template_name)

    # Apply the same name substitution as local templates
    content = content.replace("{{ name }}", workflow_name)

    return content


# ---------------------------------------------------------------------------
# Publish support
# ---------------------------------------------------------------------------

# Patterns that suggest potentially unsafe content
_SUSPICIOUS_PATTERNS = [
    re.compile(r"rm\s+-rf\s+/", re.IGNORECASE),
    re.compile(r"curl\s+.*\|\s*(?:ba)?sh", re.IGNORECASE),
    re.compile(r"wget\s+.*\|\s*(?:ba)?sh", re.IGNORECASE),
    re.compile(r"eval\s*\(", re.IGNORECASE),
    re.compile(r"exec\s*\(", re.IGNORECASE),
    re.compile(r"__import__\s*\(", re.IGNORECASE),
    re.compile(r"os\.system\s*\(", re.IGNORECASE),
    re.compile(r"subprocess\.", re.IGNORECASE),
]


@dataclass
class PublishValidationResult:
    """Result of publish validation."""

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


def validate_for_publish(workflow_path: Path) -> PublishValidationResult:
    """Validate a workflow file for publishing to the registry.

    Checks for:
    - Valid YAML syntax and schema
    - Required metadata (name, description)
    - No suspicious patterns
    - No hardcoded secrets or credentials

    Args:
        workflow_path: Path to the workflow YAML file.

    Returns:
        PublishValidationResult with validation status and details.
    """
    from conductor.config.loader import load_config
    from conductor.exceptions import ConductorError

    errors: list[str] = []
    warnings: list[str] = []
    metadata: dict[str, str] = {}

    # Check file exists
    if not workflow_path.exists():
        return PublishValidationResult(
            is_valid=False,
            errors=[f"File not found: {workflow_path}"],
        )

    # Read raw content for security checks
    try:
        content = workflow_path.read_text(encoding="utf-8")
    except OSError as e:
        return PublishValidationResult(
            is_valid=False,
            errors=[f"Cannot read file: {e}"],
        )

    # Check for suspicious patterns
    for pattern in _SUSPICIOUS_PATTERNS:
        if pattern.search(content):
            errors.append(
                f"Suspicious pattern detected: '{pattern.pattern}'. "
                "Workflows with potentially unsafe commands cannot be published."
            )

    # Check for hardcoded secrets (common patterns)
    secret_patterns = [
        re.compile(
            r"(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}['\"]", re.IGNORECASE
        ),
        re.compile(r"sk-[a-zA-Z0-9]{20,}"),  # OpenAI-style API keys
        re.compile(r"ghp_[a-zA-Z0-9]{36}"),  # GitHub PATs
    ]
    for pattern in secret_patterns:
        if pattern.search(content):
            errors.append(
                "Possible hardcoded secret detected. "
                "Use environment variables (${VAR}) instead of hardcoded credentials."
            )
            break

    # Validate workflow schema
    try:
        config = load_config(workflow_path)
        metadata["name"] = config.workflow.name
        if config.workflow.description:
            metadata["description"] = config.workflow.description
        else:
            warnings.append(
                "Workflow has no description. "
                "Adding a description helps users discover your workflow."
            )
    except ConductorError as e:
        errors.append(f"Workflow validation failed: {e}")
        return PublishValidationResult(
            is_valid=False,
            errors=errors,
            warnings=warnings,
            metadata=metadata,
        )

    return PublishValidationResult(
        is_valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        metadata=metadata,
    )


def display_publish_result(
    result: PublishValidationResult,
    workflow_path: Path,
    console: Console | None = None,
) -> None:
    """Display the publish validation result with Rich formatting.

    Args:
        result: The validation result to display.
        workflow_path: Path to the validated workflow file.
        console: Optional Rich console for output.
    """
    output_console = console if console is not None else Console()

    if result.is_valid:
        output_console.print(
            f"\n[bold green]✓ Workflow is ready for publishing:[/bold green] {workflow_path}"
        )
        if result.metadata:
            output_console.print(f"  Name: [cyan]{result.metadata.get('name', 'N/A')}[/cyan]")
            if "description" in result.metadata:
                output_console.print(f"  Description: {result.metadata['description']}")
        if result.warnings:
            output_console.print("\n[yellow]Warnings:[/yellow]")
            for warning in result.warnings:
                output_console.print(f"  ⚠ {warning}")
        output_console.print()
        output_console.print(
            "[dim]To share this workflow, submit a pull request to the "
            f"'{REGISTRY_OWNER}/{REGISTRY_REPO}' repository\n"
            f"adding your workflow to the '{REGISTRY_TEMPLATES_DIR}/' directory "
            "and updating the index.[/dim]"
        )
    else:
        output_console.print(
            f"\n[bold red]✗ Workflow cannot be published:[/bold red] {workflow_path}"
        )
        for error in result.errors:
            output_console.print(f"  [red]✗[/red] {error}")
        if result.warnings:
            output_console.print("\n[yellow]Warnings:[/yellow]")
            for warning in result.warnings:
                output_console.print(f"  ⚠ {warning}")
