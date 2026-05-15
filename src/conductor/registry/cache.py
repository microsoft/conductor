"""Local workflow cache management.

Manages the on-disk cache at ``~/.conductor/cache/registries/`` (or under
``$CONDUCTOR_HOME``).  Workflows fetched from GitHub registries are stored
here so that subsequent runs can resolve to a stable filesystem path — a
requirement for ``!file`` tag resolution and checkpoint identity.

Path registries are read directly from the source directory (no caching)
so that local edits are reflected immediately.

Cache layout
============

Workflows from the same registry+SHA share a per-SHA root that mirrors
the source repository's directory structure. This lets workflows reference
sibling workflows via repo-relative paths (e.g. ``../other/workflow.yaml``)
just as they do at the source.

::

    <base>/<registry>/<sha[:12]>/<repo_path>            # mirrored repo files
    <base>/<registry>/_meta/<sha[:12]>/source.json      # cache metadata
    <base>/<registry>/_meta/<sha[:12]>/index.yaml       # cached registry index
    <base>/<registry>/_meta/<sha[:12]>/<workflow>.complete  # readiness sentinel

For ad-hoc references (``workflow@owner/repo#ref``) the registry namespace
is ``_adhoc/<owner>/<repo>`` so adhoc caches are isolated from named
registry caches and cannot collide with any user-configured registry name
(named registries reject names containing ``/``)::

    <base>/_adhoc/<owner>/<repo>/<sha[:12]>/<repo_path>
    <base>/_adhoc/<owner>/<repo>/_meta/<sha[:12]>/...

The ``_meta`` directory lives **outside** the SHA-rooted mirror so it can
never collide with a real ``.conductor/`` (or any other) directory in the
source repo.

A workflow is considered "fully cached" only when its readiness sentinel
file exists (written **last** during a fetch). This prevents readers from
observing a partially populated workflow during a concurrent fetch.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from conductor.registry.config import RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError
from conductor.registry.github import fetch_file, list_directory, parse_github_source
from conductor.registry.index import RegistryIndex, load_index, parse_index_text
from conductor.registry.version_resolver import materialize_to_sha, resolve_ref

if TYPE_CHECKING:
    from conductor.registry.resolver import ResolvedRef

# Reserved cache namespaces. Cannot collide with named registries because
# configured registry names are not allowed to contain '/' and these names
# start with '_'.
_ADHOC_NAMESPACE = "_adhoc"
_META_NAMESPACE = "_meta"

# Current on-disk cache layout version. Bumping this invalidates all existing
# caches (their source.json will fail validation and the entries are re-fetched).
CACHE_LAYOUT_VERSION = 2

_SHA_DIR_RE = re.compile(r"^[0-9a-f]{12}$")


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


def _registry_root(registry_name: str) -> Path:
    """Return ``<base>/<registry_name>`` (joined per-segment for adhoc names)."""
    base = get_cache_base()
    # registry_name may contain '/' for adhoc (e.g. "_adhoc/owner/repo")
    # which Path naturally splits into segments.
    return base / registry_name


def _sha_dir(registry_name: str, sha: str) -> Path:
    """Return the per-SHA root for a registry."""
    return _registry_root(registry_name) / sha[:12]


def _meta_dir(registry_name: str, sha: str) -> Path:
    """Return the per-SHA metadata directory."""
    return _registry_root(registry_name) / _META_NAMESPACE / sha[:12]


def _sentinel_path(registry_name: str, sha: str, workflow_name: str) -> Path:
    """Return the readiness sentinel for a single workflow within a SHA dir."""
    # workflow_name is a registry index key; sanitize for filesystem use.
    safe = workflow_name.replace("/", "_").replace("\\", "_")
    return _meta_dir(registry_name, sha) / f"{safe}.complete"


def _safe_repo_path(repo_path: str) -> PurePosixPath:
    """Validate a repo-relative path and return it as a normalized PurePosixPath.

    Rejects:
    - empty paths
    - absolute paths (POSIX or Windows-style with drive)
    - paths containing ``..`` segments
    - paths containing NUL bytes

    Returns a :class:`PurePosixPath` suitable for joining with a local cache
    root via ``Path / posix_path``.

    Raises:
        RegistryError: If *repo_path* is unsafe.
    """
    if not repo_path or repo_path in (".", "./"):
        raise RegistryError(
            "Workflow path is empty",
            suggestion="Set 'path' to a non-empty repo-relative file path in the index.",
        )
    if "\x00" in repo_path:
        raise RegistryError(
            f"Workflow path contains a NUL byte: {repo_path!r}",
            suggestion="Remove invalid characters from the index path.",
        )
    # Reject Windows drive letters and UNC anchors, plus POSIX-absolute paths.
    if repo_path.startswith(("/", "\\")) or (
        len(repo_path) >= 2 and repo_path[1] == ":" and repo_path[0].isalpha()
    ):
        raise RegistryError(
            f"Workflow path must be repo-relative, got absolute path: {repo_path!r}",
            suggestion="Use a path like 'workflows/foo.yaml' relative to the registry root.",
        )

    # Normalize separators and split.
    posix = PurePosixPath(repo_path.replace("\\", "/"))
    parts = posix.parts
    for part in parts:
        if part in ("..", ""):
            raise RegistryError(
                f"Workflow path must not contain '..' segments: {repo_path!r}",
                suggestion="Use a path that stays within the registry root.",
            )
    return posix


def _resolve_within(root: Path, relative: PurePosixPath) -> Path:
    """Join ``relative`` onto ``root`` and verify the result stays under ``root``.

    Defense-in-depth on top of :func:`_safe_repo_path` — catches any escape via
    symlinks or oddly-cased paths after disk resolution.
    """
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise RegistryError(
            f"Resolved cache path {candidate} escapes registry SHA root {root_resolved}",
            suggestion="Check the workflow path in the registry index for unsafe components.",
        ) from exc
    return candidate


# ---------------------------------------------------------------------------
# Source metadata + cached index
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SourceMetadata:
    """Identity of a cache directory, persisted to source.json for validation."""

    cache_layout_version: int
    registry_type: str
    source: str
    full_sha: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "cache_layout_version": self.cache_layout_version,
                "registry_type": self.registry_type,
                "source": self.source,
                "full_sha": self.full_sha,
            },
            indent=2,
            sort_keys=True,
        )


def _read_source_metadata(meta_dir: Path) -> _SourceMetadata | None:
    """Read source.json from a meta dir, returning ``None`` if missing/invalid."""
    path = meta_dir / "source.json"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    try:
        return _SourceMetadata(
            cache_layout_version=int(data["cache_layout_version"]),
            registry_type=str(data["registry_type"]),
            source=str(data["source"]),
            full_sha=str(data["full_sha"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _write_source_metadata(meta_dir: Path, entry: RegistryEntry, full_sha: str) -> None:
    """Atomically write source.json for the cache directory."""
    meta_dir.mkdir(parents=True, exist_ok=True)
    metadata = _SourceMetadata(
        cache_layout_version=CACHE_LAYOUT_VERSION,
        registry_type=entry.type.value,
        source=entry.source,
        full_sha=full_sha,
    )
    target = meta_dir / "source.json"
    _atomic_write_text(target, metadata.to_json())


def _metadata_matches(meta: _SourceMetadata | None, entry: RegistryEntry, full_sha: str) -> bool:
    """Return True if cached metadata matches the current registry+SHA."""
    if meta is None:
        return False
    return (
        meta.cache_layout_version == CACHE_LAYOUT_VERSION
        and meta.registry_type == entry.type.value
        and meta.source == entry.source
        and meta.full_sha == full_sha
    )


def _load_cached_index(meta_dir: Path) -> RegistryIndex | None:
    """Load the cached registry index from ``<meta_dir>/index.yaml``.

    Returns ``None`` when the file is missing or unparseable. Callers fall
    back to fetching the index from the upstream registry on ``None``.
    """
    path = meta_dir / "index.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        return parse_index_text(text, "yaml", str(path))
    except RegistryError:
        return None


def _save_cached_index(meta_dir: Path, raw_yaml_text: str) -> None:
    """Atomically write the registry index to the meta dir."""
    meta_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(meta_dir / "index.yaml", raw_yaml_text)


def _atomic_write_text(target: Path, text: str) -> None:
    """Atomically write text to ``target`` via tempfile + ``os.replace``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".tmp-{target.name}-",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, target)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Atomically write bytes to ``target`` via tempfile + ``os.replace``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".tmp-{target.name}-",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_name, target)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


# ---------------------------------------------------------------------------
# Cache hit detection
# ---------------------------------------------------------------------------


def get_cached_workflow_path(
    registry_name: str,
    workflow_name: str,
    sha: str,
    *,
    workflow_repo_path: str | None = None,
) -> Path | None:
    """Return the cached workflow YAML path if fully cached, else ``None``.

    A workflow is considered fully cached when:

    1. The per-workflow readiness sentinel
       (``_meta/<sha>/<workflow_name>.complete``) exists.
    2. The workflow file itself exists at the expected mirrored path.

    When ``workflow_repo_path`` is omitted, this falls back to looking up the
    path in the cached registry index (``_meta/<sha>/index.yaml``). If
    neither is available, returns ``None``.

    Args:
        registry_name: Name of the registry.
        workflow_name: Workflow key as listed in the registry index.
        sha: Full immutable commit SHA. Only the first 12 chars are used as
            the on-disk directory name.
        workflow_repo_path: Optional repo-relative path of the workflow
            (e.g. ``"sdd-plan/plan.yaml"``). When omitted, the cached index
            is consulted.

    Returns:
        ``Path`` to the cached workflow YAML, or ``None`` when not cached.
    """
    sentinel = _sentinel_path(registry_name, sha, workflow_name)
    if not sentinel.is_file():
        return None

    repo_path = workflow_repo_path
    if repo_path is None:
        meta = _meta_dir(registry_name, sha)
        index = _load_cached_index(meta)
        if index is None or workflow_name not in index.workflows:
            return None
        repo_path = index.workflows[workflow_name].path

    try:
        safe = _safe_repo_path(repo_path)
    except RegistryError:
        return None

    workflow_path = _sha_dir(registry_name, sha) / safe
    if not workflow_path.is_file():
        return None
    return workflow_path


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

    1. Resolve ``ref`` (or "latest") to a concrete git ref name and
       materialize to an immutable commit SHA.
    2. If the source metadata already matches and the per-workflow sentinel
       is present, return the cached path.
    3. Otherwise, load the index pinned to the SHA (preferring the cached
       copy under ``_meta/<sha>/index.yaml``), fetch the workflow + sibling
       files into a staging dir, atomically promote each file into the
       shared SHA root, and finally write the readiness sentinel.

    Args:
        registry_name: Configured registry name.
        registry_entry: The registry definition (type + source).
        workflow_name: Workflow key as listed in the registry index.
        ref: Explicit git ref (tag, branch, or SHA), or ``None`` for the
            registry's default (default branch HEAD).

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
        # Validate the path before joining, even for path registries — keeps
        # the safety contract uniform regardless of backend.
        safe = _safe_repo_path(workflow_info.path)
        source_path = Path(registry_entry.source) / safe
        if not source_path.exists():
            raise RegistryError(
                f"Workflow file not found at '{source_path}'",
                suggestion="Verify the 'path' field in the registry's index.yaml.",
                file_path=str(source_path),
            )
        return source_path

    # GitHub registry: resolve ref → SHA, then attempt cache hit.
    resolved_ref = resolve_ref(registry_entry, ref)
    sha = materialize_to_sha(registry_entry, resolved_ref)

    meta = _meta_dir(registry_name, sha)
    metadata = _read_source_metadata(meta)
    matches = _metadata_matches(metadata, registry_entry, sha)

    # Try the cache first: requires matching metadata, sentinel present, and
    # the workflow file present at its mirrored path.
    if matches:
        index = _load_cached_index(meta)
        if index is not None and workflow_name in index.workflows:
            cached = get_cached_workflow_path(
                registry_name,
                workflow_name,
                sha,
                workflow_repo_path=index.workflows[workflow_name].path,
            )
            if cached is not None:
                return cached
    else:
        # Stale or missing metadata — clear the meta dir to avoid serving an
        # inconsistent index/source on a subsequent miss. Don't touch the SHA
        # mirror itself; new fetches will overwrite content-addressed files.
        if metadata is not None:
            shutil.rmtree(meta, ignore_errors=True)

    # Fetch the index from the upstream registry (pinned to SHA) and persist it.
    index = load_index(registry_entry, ref=sha)
    if workflow_name not in index.workflows:
        raise RegistryError(
            f"Workflow '{workflow_name}' not found in registry '{registry_name}'",
            suggestion=f"Run 'conductor registry list {registry_name}' to see available workflows.",
        )
    workflow_info = index.workflows[workflow_name]
    safe_path = _safe_repo_path(workflow_info.path)

    sha_root = _sha_dir(registry_name, sha)
    sha_root.mkdir(parents=True, exist_ok=True)

    # Stage everything in a temp dir under the meta dir (intra-filesystem, so
    # per-file os.replace into sha_root is atomic).
    meta.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".tmp-{workflow_name}-", dir=meta))
    try:
        _fetch_github(registry_entry, str(safe_path), sha, tmp_dir)
        _promote_staged_files(tmp_dir, sha_root)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Persist metadata + cached index, then write the readiness sentinel
    # **last** so concurrent readers never observe a partial fetch.
    _write_source_metadata(meta, registry_entry, sha)
    _save_cached_index(meta, _index_to_yaml(index))

    workflow_path = _resolve_within(sha_root, safe_path)
    if not workflow_path.is_file():
        raise RegistryError(
            f"Workflow file '{safe_path}' not found in cache after fetch",
            suggestion="The registry index may reference a file that does not exist.",
        )

    sentinel = _sentinel_path(registry_name, sha, workflow_name)
    _atomic_write_text(sentinel, "")

    return workflow_path


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
# Auto-fetch sub-workflows from the same registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CacheLocation:
    """Identity of a path inside the registry cache."""

    registry_name: str
    sha: str
    sha_root: Path


def find_registry_cache_location(path: Path) -> _CacheLocation | None:
    """Detect whether ``path`` lives inside a registry SHA-mirrored cache.

    Recognizes both named-registry and ad-hoc layouts and returns the
    parsed registry name, 12-char SHA prefix, and the SHA root. Returns
    ``None`` when ``path`` does not match either layout (or when the SHA
    segment is not a 12-char hex string).
    """
    base = get_cache_base()
    try:
        rel = path.resolve().relative_to(base.resolve())
    except (FileNotFoundError, ValueError):
        return None

    parts = rel.parts
    if not parts:
        return None

    # Adhoc: _adhoc/<owner>/<repo>/<sha>/...
    if parts[0] == _ADHOC_NAMESPACE:
        if len(parts) < 4:
            return None
        owner, repo, sha = parts[1], parts[2], parts[3]
        if not _SHA_DIR_RE.match(sha):
            return None
        registry_name = f"{_ADHOC_NAMESPACE}/{owner}/{repo}"
        sha_root = base / _ADHOC_NAMESPACE / owner / repo / sha
        return _CacheLocation(registry_name=registry_name, sha=sha, sha_root=sha_root)

    # Named: <registry>/<sha>/...
    if len(parts) < 2:
        return None
    registry_name, sha = parts[0], parts[1]
    # The reserved "_meta" subdirectory is not a SHA root — skip it.
    if registry_name == _META_NAMESPACE or sha == _META_NAMESPACE:
        return None
    if not _SHA_DIR_RE.match(sha):
        return None
    sha_root = base / registry_name / sha
    return _CacheLocation(registry_name=registry_name, sha=sha, sha_root=sha_root)


def auto_fetch_relative_workflow(absolute_path: Path) -> Path | None:
    """Try to populate the cache for a missing workflow file resolved by
    relative path against another cached workflow.

    Used by :class:`~conductor.engine.workflow.WorkflowEngine` when a
    sub-workflow reference like ``../document-review/workflow.yaml``
    resolves to a path that does not exist on disk. If the referenced file
    sits inside the same registry+SHA cache as the parent workflow and is
    listed in that registry's cached index, fetch it.

    Returns ``absolute_path`` (now populated) on success, or ``None`` when
    the path is outside any registry cache, the index has no workflow at
    that repo-relative path, or the cached metadata is missing/stale.

    Failures during fetch propagate as :class:`RegistryError` so callers
    can produce a clear error message.
    """
    location = find_registry_cache_location(absolute_path)
    if location is None:
        return None

    # Compute the repo-relative path of the requested workflow.
    try:
        rel_in_repo = PurePosixPath(
            absolute_path.resolve().relative_to(location.sha_root.resolve()).as_posix()
        )
    except ValueError:
        return None

    meta = _meta_dir(location.registry_name, location.sha)
    metadata = _read_source_metadata(meta)
    if metadata is None:
        return None

    # Validate the cached metadata before trusting it for re-fetch:
    # - layout version must match (avoids using older/newer cache shapes)
    # - registry type must be github (path registries don't share this cache)
    # - the SHA in metadata must agree with the on-disk SHA dir name
    # - the source must look like a valid github source
    if metadata.cache_layout_version != CACHE_LAYOUT_VERSION:
        return None
    if metadata.registry_type != RegistryType.github.value:
        return None
    if not metadata.full_sha.startswith(location.sha):
        return None
    try:
        parse_github_source(metadata.source)
    except RegistryError:
        return None

    index = _load_cached_index(meta)
    if index is None:
        return None

    # Find a workflow whose path matches the requested repo-relative path.
    matching_name: str | None = None
    for name, info in index.workflows.items():
        try:
            info_rel = _safe_repo_path(info.path)
        except RegistryError:
            continue
        if info_rel == rel_in_repo:
            matching_name = name
            break

    if matching_name is None:
        return None

    # Reconstruct the registry entry from the cached metadata.
    try:
        entry = RegistryEntry(type=RegistryType(metadata.registry_type), source=metadata.source)
    except (ValueError, RegistryError):
        return None

    fetched = fetch_workflow(
        registry_name=location.registry_name,
        registry_entry=entry,
        workflow_name=matching_name,
        ref=metadata.full_sha,
    )
    return fetched


# ---------------------------------------------------------------------------
# GitHub fetch + staging
# ---------------------------------------------------------------------------


def _fetch_github(
    registry_entry: RegistryEntry,
    workflow_path: str,
    sha: str,
    dest_dir: Path,
) -> None:
    """Fetch a workflow and its sibling files from a GitHub registry into a staging dir.

    Files are written into ``dest_dir`` preserving the workflow's repo
    parent directory. For example, fetching ``sdd-plan/plan.yaml`` writes::

        <dest_dir>/sdd-plan/plan.yaml
        <dest_dir>/sdd-plan/<sibling files>

    Args:
        registry_entry: Registry entry with ``source`` as ``owner/repo``.
        workflow_path: Validated repo-relative path to the workflow YAML.
        sha: Immutable commit SHA to fetch at.
        dest_dir: Local staging directory to write files into.
    """
    owner, repo = parse_github_source(registry_entry.source)
    workflow_p = PurePosixPath(workflow_path)
    parent_dir = workflow_p.parent  # PurePosixPath('.') for repo-root workflows
    workflow_filename = workflow_p.name

    # Fetch the workflow file itself (returns bytes)
    content = fetch_file(owner, repo, workflow_path, ref=sha)
    target_dir = dest_dir if str(parent_dir) == "." else dest_dir / parent_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / workflow_filename).write_bytes(content)

    # Fetch sibling files — list_directory returns filenames (not full paths)
    parent_dir_str = str(parent_dir) if str(parent_dir) != "." else "."
    try:
        sibling_names = list_directory(owner, repo, parent_dir_str, ref=sha)
    except Exception:
        return

    for name in sibling_names:
        if name == workflow_filename:
            continue  # already fetched
        sibling_repo_path = f"{parent_dir_str}/{name}" if parent_dir_str != "." else name
        # Best-effort: skip names with unsafe characters rather than fail
        # the whole fetch over a single malformed sibling entry.
        try:
            _safe_repo_path(sibling_repo_path)
        except RegistryError:
            continue
        try:
            sibling_content = fetch_file(owner, repo, sibling_repo_path, ref=sha)
            (target_dir / name).write_bytes(sibling_content)
        except Exception:
            # Best-effort for siblings — don't fail the whole fetch
            pass


def _promote_staged_files(tmp_dir: Path, sha_root: Path) -> None:
    """Move every file from ``tmp_dir`` into ``sha_root`` atomically.

    Preserves the relative directory layout of ``tmp_dir``. Each file is
    moved with ``os.replace()`` so concurrent readers see either the old or
    the new file, never a half-written one. Files in ``sha_root`` are
    content-addressed by the immutable SHA so overwriting an existing entry
    with the same content is safe and idempotent.
    """
    for src in sorted(tmp_dir.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(tmp_dir)
        # Defense-in-depth: re-validate that the staged path stays inside
        # sha_root (catches any unexpected absolute component).
        try:
            _safe_repo_path(str(rel))
        except RegistryError:
            continue
        dest = _resolve_within(sha_root, PurePosixPath(rel.as_posix()))
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dest)


def _index_to_yaml(index: RegistryIndex) -> str:
    """Render a RegistryIndex back to YAML for persistence in the meta dir."""
    from io import StringIO

    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.default_flow_style = False
    data = {
        "workflows": {
            name: {"description": info.description, "path": info.path}
            for name, info in index.workflows.items()
        }
    }
    buf = StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()


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
    directories under each registry's ``_meta/<sha>/`` directory. If a
    process is killed mid-write, these orphans never get cleaned up. This
    helper walks the cache and removes any directory whose name starts with
    ``.tmp-``.

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
        for tmp in reg_root.rglob(".tmp-*"):
            if tmp.is_dir():
                shutil.rmtree(tmp, ignore_errors=True)
                if not tmp.exists():
                    removed += 1
    return removed
