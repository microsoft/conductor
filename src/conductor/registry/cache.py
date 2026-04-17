"""Local workflow cache management.

Manages the on-disk cache at ``~/.conductor/cache/registries/`` (or under
``$CONDUCTOR_HOME``).  Workflows fetched from remote registries are stored
here so that subsequent runs can resolve to a stable filesystem path — a
requirement for ``!file`` tag resolution and checkpoint identity.

Cache layout::

    <base>/cache/registries/<registry>/<workflow>/<version>/
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from conductor.registry.config import RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError
from conductor.registry.github import fetch_file, list_directory, parse_github_source
from conductor.registry.index import load_index, resolve_latest

# fetch_file returns bytes; list_directory returns filenames (not full paths)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_cache_base() -> Path:
    """Return the base cache directory.

    Uses ``$CONDUCTOR_HOME/cache/registries/`` or
    ``~/.conductor/cache/registries/``.
    """
    home = os.environ.get("CONDUCTOR_HOME")
    base = Path(home) if home else Path.home() / ".conductor"
    return base / "cache" / "registries"


def get_cached_workflow_path(
    registry_name: str,
    workflow_name: str,
    version: str,
) -> Path | None:
    """Return the cached workflow YAML path if it exists, else ``None``.

    Looks for the workflow at::

        <cache_base>/<registry_name>/<workflow_name>/<version>/

    The filename is derived from a glob of ``*.yaml`` / ``*.yml`` files in the
    version directory.  Returns the first YAML file found (there should be
    exactly one workflow file per cached version directory).

    Args:
        registry_name: Name of the registry.
        workflow_name: Name of the workflow.
        version: Resolved version string.

    Returns:
        ``Path`` to the cached workflow YAML, or ``None`` when not cached.
    """
    version_dir = get_cache_base() / registry_name / workflow_name / version
    if not version_dir.is_dir():
        return None

    for ext in ("*.yaml", "*.yml"):
        matches = list(version_dir.glob(ext))
        if matches:
            return matches[0]
    return None


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------


def fetch_workflow(
    registry_name: str,
    registry_entry: RegistryEntry,
    workflow_name: str,
    version: str | None = None,
) -> Path:
    """Fetch a workflow from a registry and cache it locally.

    Steps:

    1. Load the registry index.
    2. Resolve version (if ``None``, resolve ``latest``).
    3. Check cache — return cached path if explicit version already present.
    4. Fetch the workflow file **and** sibling files in the same directory.
    5. Write everything to the cache directory.
    6. Return the ``Path`` to the cached workflow YAML.

    For **GitHub** registries files are fetched at the git tag matching the
    version via :func:`~conductor.registry.github.fetch_file` and siblings
    are enumerated with :func:`~conductor.registry.github.list_directory`.

    For **path** registries files are copied from the source directory to
    guarantee a stable snapshot even when the source changes.

    Args:
        registry_name: Configured registry name.
        registry_entry: The registry definition (type + source).
        workflow_name: Workflow key as listed in the registry index.
        version: Explicit version string, or ``None`` for ``latest``.

    Returns:
        Path to the cached workflow YAML file.

    Raises:
        RegistryError: On fetch failure, missing workflow, or I/O errors.
    """
    # 1. Load the index
    index = load_index(registry_entry)

    # 2. Resolve version
    if version is None:
        version = resolve_latest(index, workflow_name)

    # 3. Look up workflow metadata from the index
    if workflow_name not in index.workflows:
        raise RegistryError(
            f"Workflow '{workflow_name}' not found in registry '{registry_name}'",
            suggestion=f"Run 'conductor registry list {registry_name}' to see available workflows.",
        )
    workflow_info = index.workflows[workflow_name]

    # 4. Check cache (explicit versions are immutable)
    cached = get_cached_workflow_path(registry_name, workflow_name, version)
    if cached is not None:
        return cached

    # 5. Prepare cache directory
    version_dir = get_cache_base() / registry_name / workflow_name / version
    version_dir.mkdir(parents=True, exist_ok=True)

    # 6. Fetch based on registry type
    try:
        if registry_entry.type == RegistryType.github:
            _fetch_github(registry_entry, workflow_info.path, version, version_dir)
        else:
            _fetch_path(registry_entry, workflow_info.path, version_dir)
    except RegistryError:
        raise
    except Exception as exc:
        raise RegistryError(
            f"Failed to fetch workflow '{workflow_name}' from registry '{registry_name}': {exc}",
            suggestion="Check your network connection and registry configuration.",
        ) from exc

    # 7. Return the cached workflow path
    workflow_filename = Path(workflow_info.path).name
    result = version_dir / workflow_filename
    if not result.exists():
        raise RegistryError(
            f"Workflow file '{workflow_filename}' not found in cache after fetch",
            suggestion="The registry index may reference a file that does not exist.",
        )
    return result


# ---------------------------------------------------------------------------
# GitHub fetch
# ---------------------------------------------------------------------------


def _fetch_github(
    registry_entry: RegistryEntry,
    workflow_path: str,
    version: str,
    dest_dir: Path,
) -> None:
    """Fetch a workflow and its sibling files from a GitHub registry.

    Args:
        registry_entry: Registry entry with ``source`` as ``owner/repo``.
        workflow_path: Relative path to the workflow YAML in the repo.
        version: Git ref (tag) to fetch at.
        dest_dir: Local directory to write files into.
    """
    owner, repo = parse_github_source(registry_entry.source)
    workflow_p = Path(workflow_path)
    parent_dir = str(workflow_p.parent)
    workflow_filename = workflow_p.name

    # Fetch the workflow file itself (returns bytes)
    content = fetch_file(owner, repo, workflow_path, ref=version)
    (dest_dir / workflow_filename).write_bytes(content)

    # Fetch sibling files — list_directory returns filenames (not full paths)
    try:
        sibling_names = list_directory(owner, repo, parent_dir, ref=version)
    except Exception:
        # If listing fails, we already have the workflow file
        return

    for name in sibling_names:
        if name == workflow_filename:
            continue  # already fetched
        sibling_repo_path = f"{parent_dir}/{name}" if parent_dir != "." else name
        try:
            sibling_content = fetch_file(owner, repo, sibling_repo_path, ref=version)
            (dest_dir / name).write_bytes(sibling_content)
        except Exception:
            # Best-effort for siblings — don't fail the whole fetch
            pass


# ---------------------------------------------------------------------------
# Path (local) fetch
# ---------------------------------------------------------------------------


def _fetch_path(
    registry_entry: RegistryEntry,
    workflow_path: str,
    dest_dir: Path,
) -> None:
    """Copy a workflow and its sibling files from a local path registry.

    Args:
        registry_entry: Registry entry with ``source`` as a filesystem path.
        workflow_path: Relative path to the workflow YAML within the source.
        dest_dir: Local directory to write files into.
    """
    source_root = Path(registry_entry.source)
    source_file = source_root / workflow_path

    if not source_file.exists():
        raise RegistryError(
            f"Workflow file not found at '{source_file}'",
            suggestion="Verify the 'path' field in the registry's index.yaml.",
            file_path=str(source_file),
        )

    source_dir = source_file.parent

    # Copy all files in the workflow's directory
    for item in source_dir.iterdir():
        if item.is_file():
            shutil.copy2(item, dest_dir / item.name)


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def clear_cache(registry_name: str | None = None) -> None:
    """Clear cached workflows.

    If *registry_name* is provided only that registry's cache is removed.
    Otherwise **all** cached registries are deleted.

    Args:
        registry_name: Optional registry name to scope the clear.
    """
    base = get_cache_base()

    if registry_name is not None:
        target = base / registry_name
        if target.exists():
            shutil.rmtree(target)
    else:
        if base.exists():
            shutil.rmtree(base)
