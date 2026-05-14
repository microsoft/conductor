"""Local workflow cache management.

Manages the on-disk cache at ``~/.conductor/cache/registries/`` (or under
``$CONDUCTOR_HOME``).  Workflows fetched from GitHub registries are stored
here so that subsequent runs can resolve to a stable filesystem path — a
requirement for ``!file`` tag resolution and checkpoint identity.

Path registries are read directly from the source directory (no caching)
so that local edits are reflected immediately.

Cache layout::

    <base>/cache/registries/<registry>/<workflow>/<sha[:12]>/

For ad-hoc references (``workflow@owner/repo#ref``) the registry namespace
is ``_adhoc/<owner>/<repo>`` so adhoc caches are isolated from named
registry caches and cannot collide with any user-configured registry name
(named registries reject names containing ``/``)::

    <base>/cache/registries/_adhoc/<owner>/<repo>/<workflow>/<sha[:12]>/
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from conductor.registry.config import RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError
from conductor.registry.github import fetch_file, list_directory, parse_github_source
from conductor.registry.index import load_index
from conductor.registry.version_resolver import materialize_to_sha, resolve_ref

if TYPE_CHECKING:
    from conductor.registry.resolver import ResolvedRef

# fetch_file returns bytes; list_directory returns filenames (not full paths)

# Reserved cache namespace for ad-hoc references. Cannot collide with a
# named registry because configured registry names cannot contain '/'.
_ADHOC_NAMESPACE = "_adhoc"

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
    sha: str,
) -> Path | None:
    """Return the cached workflow YAML path if it exists, else ``None``.

    Looks for the workflow at::

        <cache_base>/<registry_name>/<workflow_name>/<sha[:12]>/

    The filename is derived from a glob of ``*.yaml`` / ``*.yml`` files in the
    SHA directory.  Returns the first YAML file found (there should be
    exactly one workflow file per cached SHA directory).

    Args:
        registry_name: Name of the registry.
        workflow_name: Name of the workflow.
        sha: Full immutable commit SHA. The first 12 chars are used as the
            on-disk directory name.

    Returns:
        ``Path`` to the cached workflow YAML, or ``None`` when not cached.
    """
    sha_dir = get_cache_base() / registry_name / workflow_name / sha[:12]
    if not sha_dir.is_dir():
        return None

    for ext in ("*.yaml", "*.yml"):
        matches = list(sha_dir.glob(ext))
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
    ref: str | None = None,
) -> Path:
    """Fetch a workflow from a registry and cache it locally.

    For **path** registries, reads directly from the source directory (no
    caching). The ``ref`` argument must be ``None`` — :func:`resolve_ref`
    raises if a ref is provided for a path registry.

    For **github** registries:

    1. Resolve ``ref`` (or "latest") to a concrete git ref name.
    2. Materialize that ref to an immutable commit SHA.
    3. Return the cached workflow path if already present.
    4. Otherwise, load the registry index **at that SHA** (so the index and
       workflow file are guaranteed to come from the same commit), look up
       the workflow path, fetch the workflow + sibling files into a temp
       directory, and atomically rename it into the cache.

    Args:
        registry_name: Configured registry name.
        registry_entry: The registry definition (type + source).
        workflow_name: Workflow key as listed in the registry index.
        ref: Explicit git ref (tag, branch, or SHA), or ``None`` for the
            registry's default (latest tag, falling back to default branch).

    Returns:
        Path to the cached workflow YAML file.

    Raises:
        RegistryError: On fetch failure, missing workflow, or I/O errors.
            Failures fetching sibling files in the same directory are
            silently swallowed (best-effort) — only the workflow file
            itself must succeed.
    """
    # Path registries: read directly from source. resolve_ref raises if a
    # ref was supplied, propagating a clear error to the caller.
    if registry_entry.type == RegistryType.path:
        resolve_ref(registry_entry, ref)
        index = load_index(registry_entry)
        if workflow_name not in index.workflows:
            raise RegistryError(
                f"Workflow '{workflow_name}' not found in registry '{registry_name}'",
                suggestion=(
                    f"Run 'conductor registry list {registry_name}' to see available workflows."
                ),
            )
        workflow_info = index.workflows[workflow_name]
        source_path = Path(registry_entry.source) / workflow_info.path
        if not source_path.exists():
            raise RegistryError(
                f"Workflow file not found at '{source_path}'",
                suggestion="Verify the 'path' field in the registry's index.yaml.",
                file_path=str(source_path),
            )
        return source_path

    # GitHub registry: resolve ref → SHA, then check cache.
    resolved_ref = resolve_ref(registry_entry, ref)
    sha = materialize_to_sha(registry_entry, resolved_ref)

    cached = get_cached_workflow_path(registry_name, workflow_name, sha)
    if cached is not None:
        return cached

    # Load the index pinned to the SHA so the workflow path comes from the
    # exact commit we're about to fetch.
    index = load_index(registry_entry, ref=sha)
    if workflow_name not in index.workflows:
        raise RegistryError(
            f"Workflow '{workflow_name}' not found in registry '{registry_name}'",
            suggestion=f"Run 'conductor registry list {registry_name}' to see available workflows.",
        )
    workflow_info = index.workflows[workflow_name]

    # Atomically write to cache: fetch into a tmp dir under the workflow
    # parent (so the rename is intra-filesystem), then os.replace().
    parent = get_cache_base() / registry_name / workflow_name
    parent.mkdir(parents=True, exist_ok=True)
    final_dir = parent / sha[:12]

    tmp_dir = Path(tempfile.mkdtemp(prefix=".tmp-", dir=parent))
    try:
        _fetch_github(registry_entry, workflow_info.path, sha, tmp_dir)
        try:
            os.replace(tmp_dir, final_dir)
        except OSError:
            # Likely a race: another process populated final_dir first. If it
            # exists, drop our tmp work and use the cached entry. Otherwise
            # re-raise.
            if final_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            else:
                raise
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    workflow_filename = Path(workflow_info.path).name
    result = final_dir / workflow_filename
    if not result.exists():
        raise RegistryError(
            f"Workflow file '{workflow_filename}' not found in cache after fetch",
            suggestion="The registry index may reference a file that does not exist.",
        )
    return result


def fetch_workflow_adhoc(
    owner: str,
    repo: str,
    workflow_name: str,
    ref: str | None = None,
) -> Path:
    """Fetch an ad-hoc workflow from a GitHub repo without registry config.

    Constructs a synthetic ``RegistryEntry`` for ``owner/repo`` and reuses
    the same fetch + cache pipeline as named registries. Cache entries are
    namespaced under ``_adhoc/<owner>/<repo>/`` so they're isolated from
    configured registries.

    Args:
        owner: GitHub repository owner.
        repo: GitHub repository name.
        workflow_name: Workflow key as listed in the repo's ``index.yaml``.
        ref: Optional git ref (tag, branch, or SHA). ``None`` resolves to
            the repository's default branch HEAD.

    Returns:
        Path to the cached workflow YAML file.

    Raises:
        RegistryError: On fetch failure, missing workflow, or I/O errors.
    """
    synthetic_entry = RegistryEntry(
        type=RegistryType.github,
        source=f"{owner}/{repo}",
    )
    synthetic_registry_name = f"{_ADHOC_NAMESPACE}/{owner}/{repo}"
    return fetch_workflow(
        registry_name=synthetic_registry_name,
        registry_entry=synthetic_entry,
        workflow_name=workflow_name,
        ref=ref,
    )


def resolve_and_fetch(resolved: ResolvedRef) -> Path:
    """Return a local filesystem path for any kind of resolved reference.

    Single dispatcher used by the CLI, engine, and validator so each call
    site does not need to switch on ``ResolvedRef.kind``. Behavior by kind:

    * ``file``: returns ``resolved.path`` unchanged. Caller is responsible
      for verifying the path exists.
    * ``registry``: fetches via :func:`fetch_workflow` (cached under the
      configured registry name).
    * ``adhoc``: fetches via :func:`fetch_workflow_adhoc` (cached under the
      ``_adhoc/<owner>/<repo>`` namespace).

    Args:
        resolved: A :class:`~conductor.registry.resolver.ResolvedRef` from
            :func:`~conductor.registry.resolver.resolve_ref`.

    Returns:
        A local ``Path`` to the workflow YAML file.

    Raises:
        RegistryError: When a registry/adhoc fetch fails.
        ValueError: If ``resolved`` has missing required fields for its kind.
    """
    if resolved.kind == "file":
        if resolved.path is None:
            raise ValueError("ResolvedRef(kind='file') must have a non-None path")
        return resolved.path

    if resolved.kind == "registry":
        if (
            resolved.registry_name is None
            or resolved.registry_entry is None
            or resolved.workflow is None
        ):
            raise ValueError(
                "ResolvedRef(kind='registry') must have non-None "
                "registry_name, registry_entry, and workflow"
            )
        return fetch_workflow(
            registry_name=resolved.registry_name,
            registry_entry=resolved.registry_entry,
            workflow_name=resolved.workflow,
            ref=resolved.ref,
        )

    if resolved.kind == "adhoc":
        if resolved.adhoc_owner is None or resolved.adhoc_repo is None or resolved.workflow is None:
            raise ValueError(
                "ResolvedRef(kind='adhoc') must have non-None adhoc_owner, adhoc_repo, and workflow"
            )
        return fetch_workflow_adhoc(
            owner=resolved.adhoc_owner,
            repo=resolved.adhoc_repo,
            workflow_name=resolved.workflow,
            ref=resolved.ref,
        )

    raise ValueError(f"Unknown ResolvedRef kind: {resolved.kind!r}")


# ---------------------------------------------------------------------------
# GitHub fetch
# ---------------------------------------------------------------------------


def _fetch_github(
    registry_entry: RegistryEntry,
    workflow_path: str,
    sha: str,
    dest_dir: Path,
) -> None:
    """Fetch a workflow and its sibling files from a GitHub registry.

    Args:
        registry_entry: Registry entry with ``source`` as ``owner/repo``.
        workflow_path: Relative path to the workflow YAML in the repo.
        sha: Immutable commit SHA to fetch at.
        dest_dir: Local directory to write files into.
    """
    owner, repo = parse_github_source(registry_entry.source)
    workflow_p = Path(workflow_path)
    parent_dir = str(workflow_p.parent)
    workflow_filename = workflow_p.name

    # Fetch the workflow file itself (returns bytes)
    content = fetch_file(owner, repo, workflow_path, ref=sha)
    (dest_dir / workflow_filename).write_bytes(content)

    # Fetch sibling files — list_directory returns filenames (not full paths)
    try:
        sibling_names = list_directory(owner, repo, parent_dir, ref=sha)
    except Exception:
        # If listing fails, we already have the workflow file
        return

    for name in sibling_names:
        if name == workflow_filename:
            continue  # already fetched
        sibling_repo_path = f"{parent_dir}/{name}" if parent_dir != "." else name
        try:
            sibling_content = fetch_file(owner, repo, sibling_repo_path, ref=sha)
            (dest_dir / name).write_bytes(sibling_content)
        except Exception:
            # Best-effort for siblings — don't fail the whole fetch
            pass


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


def prune_temp_dirs(registry_name: str | None = None) -> int:
    """Remove orphaned ``.tmp-*`` directories under the cache.

    The atomic write pattern in :func:`fetch_workflow` creates ``.tmp-XXXX``
    directories alongside each workflow's SHA directory. If a process is
    killed mid-write, these orphans never get cleaned up. This helper walks
    the cache and removes any directory whose name starts with ``.tmp-``.

    Args:
        registry_name: If provided, only that registry's cache is scanned.
            Otherwise all registries under the cache base are scanned.

    Returns:
        Count of directories successfully removed.
    """
    base = get_cache_base()
    if not base.is_dir():
        return 0

    if registry_name is not None:
        registry_roots = [base / registry_name]
    else:
        registry_roots = [p for p in base.iterdir() if p.is_dir()]

    removed = 0
    for reg_root in registry_roots:
        if not reg_root.is_dir():
            continue
        # Layout: <reg>/<workflow>/<sha-or-.tmp-*>/
        for workflow_dir in reg_root.iterdir():
            if not workflow_dir.is_dir():
                continue
            for child in workflow_dir.iterdir():
                if child.is_dir() and child.name.startswith(".tmp-"):
                    shutil.rmtree(child, ignore_errors=True)
                    if not child.exists():
                        removed += 1
    return removed
