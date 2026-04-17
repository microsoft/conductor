"""Registry index loading and parsing.

Handles fetching and parsing ``index.yaml`` / ``index.json`` from local
path registries and remote GitHub registries.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from pydantic import BaseModel
from ruamel.yaml import YAML, YAMLError

from conductor.registry.config import RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError

_GITHUB_RAW_BASE = "https://raw.githubusercontent.com"
_GITHUB_BRANCHES = ("main", "master")
_INDEX_FILENAMES = ("index.yaml", "index.json")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class WorkflowInfo(BaseModel):
    """Metadata about a workflow in a registry index."""

    description: str = ""
    path: str
    """Relative path from registry root to the workflow YAML."""
    versions: list[str] = []


class RegistryIndex(BaseModel):
    """Parsed registry index."""

    workflows: dict[str, WorkflowInfo] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_index(entry: RegistryEntry) -> RegistryIndex:
    """Load and parse the index for a registry.

    For path registries: reads ``index.yaml`` or ``index.json`` from the
    source directory.

    For GitHub registries: fetches from ``raw.githubusercontent.com`` on the
    default branch (``main``, falling back to ``master``).

    Args:
        entry: The registry entry describing the backend type and source.

    Returns:
        A parsed ``RegistryIndex``.

    Raises:
        RegistryError: If the index is not found or malformed.
    """
    if entry.type == RegistryType.path:
        return _load_path_index(entry.source)
    return _load_github_index(entry.source)


def resolve_latest(index: RegistryIndex, workflow_name: str) -> str:
    """Resolve 'latest' to the last version in the versions list.

    Args:
        index: The registry index to look up.
        workflow_name: Name of the workflow.

    Returns:
        The last element of the workflow's versions list.

    Raises:
        RegistryError: If the workflow is not found or has no versions listed.
    """
    info = get_workflow_info(index, workflow_name)
    if not info.versions:
        raise RegistryError(
            f"Workflow '{workflow_name}' has no versions listed",
            suggestion="Add at least one version to the workflow entry in the registry index.",
        )
    return info.versions[-1]


def get_workflow_info(index: RegistryIndex, workflow_name: str) -> WorkflowInfo:
    """Get info for a specific workflow.

    Args:
        index: The registry index to look up.
        workflow_name: Name of the workflow.

    Returns:
        The ``WorkflowInfo`` for the workflow.

    Raises:
        RegistryError: If the workflow is not found.
    """
    if workflow_name not in index.workflows:
        available = ", ".join(sorted(index.workflows)) or "(none)"
        raise RegistryError(
            f"Workflow '{workflow_name}' not found in registry index",
            suggestion=f"Available workflows: {available}",
        )
    return index.workflows[workflow_name]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_index_data(raw: dict, source_label: str) -> RegistryIndex:
    """Validate and parse raw dict data into a ``RegistryIndex``."""
    try:
        return RegistryIndex.model_validate(raw)
    except Exception as exc:
        raise RegistryError(
            f"Malformed registry index from {source_label}: {exc}",
            suggestion="Check that the index file matches the expected schema.",
        ) from exc


def _load_path_index(source: str) -> RegistryIndex:
    """Load index from a local filesystem path."""
    root = Path(source)
    if not root.is_dir():
        raise RegistryError(
            f"Registry path does not exist: {source}",
            suggestion="Check that the path is correct and the directory exists.",
        )

    # Try index.yaml first, then index.json
    yaml_path = root / "index.yaml"
    json_path = root / "index.json"

    if yaml_path.is_file():
        return _parse_yaml_file(yaml_path)
    if json_path.is_file():
        return _parse_json_file(json_path)

    raise RegistryError(
        f"No index.yaml or index.json found in {source}",
        suggestion="Create an index.yaml or index.json in the registry root.",
    )


def _parse_yaml_file(path: Path) -> RegistryIndex:
    """Parse a YAML index file."""
    try:
        yaml = YAML(typ="safe")
        with open(path) as f:
            data = yaml.load(f)
    except YAMLError as exc:
        raise RegistryError(
            f"Failed to parse {path}: {exc}",
            suggestion="Check the YAML syntax in the index file.",
            file_path=str(path),
        ) from exc

    if not isinstance(data, dict):
        raise RegistryError(
            f"Malformed registry index from {path}: expected a mapping at the top level",
            suggestion="Check that the index file matches the expected schema.",
            file_path=str(path),
        )
    return _parse_index_data(data, str(path))


def _parse_json_file(path: Path) -> RegistryIndex:
    """Parse a JSON index file."""
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise RegistryError(
            f"Failed to parse {path}: {exc}",
            suggestion="Check the JSON syntax in the index file.",
            file_path=str(path),
        ) from exc

    if not isinstance(data, dict):
        raise RegistryError(
            f"Malformed registry index from {path}: expected a mapping at the top level",
            suggestion="Check that the index file matches the expected schema.",
            file_path=str(path),
        )
    return _parse_index_data(data, str(path))


def _load_github_index(source: str) -> RegistryIndex:
    """Fetch index from a GitHub repository via raw.githubusercontent.com."""
    # source is "owner/repo"
    for filename in _INDEX_FILENAMES:
        for branch in _GITHUB_BRANCHES:
            url = f"{_GITHUB_RAW_BASE}/{source}/{branch}/{filename}"
            try:
                resp = httpx.get(url, timeout=30, follow_redirects=True)
            except httpx.HTTPError as exc:
                raise RegistryError(
                    f"Failed to fetch registry index from {url}: {exc}",
                    suggestion="Check your network connection and the registry source.",
                ) from exc

            if resp.status_code == 200:
                return _parse_github_response(resp.text, filename, url)

    raise RegistryError(
        f"No index.yaml or index.json found in GitHub repo '{source}' "
        f"(tried branches: {', '.join(_GITHUB_BRANCHES)})",
        suggestion="Ensure the repository contains an index.yaml or index.json on main or master.",
    )


def _parse_github_response(text: str, filename: str, url: str) -> RegistryIndex:
    """Parse the content fetched from GitHub."""
    if filename.endswith(".yaml"):
        try:
            yaml = YAML(typ="safe")
            data = yaml.load(text)
        except YAMLError as exc:
            raise RegistryError(
                f"Failed to parse index from {url}: {exc}",
                suggestion="Check the YAML syntax in the remote index file.",
            ) from exc
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RegistryError(
                f"Failed to parse index from {url}: {exc}",
                suggestion="Check the JSON syntax in the remote index file.",
            ) from exc

    if not isinstance(data, dict):
        raise RegistryError(
            f"Malformed registry index from {url}: expected a mapping at the top level",
            suggestion="Check that the index file matches the expected schema.",
        )
    return _parse_index_data(data, url)
