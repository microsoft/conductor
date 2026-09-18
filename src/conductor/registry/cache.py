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
    <base>/<registry>/_meta/<sha[:12]>/workflows/<workflow>.complete  # readiness sentinel
    <base>/<registry>/_meta/<sha[:12]>/tools.json       # SHA-keyed parse cache
    <base>/<registry>/_meta/<sha[:12]>/tools.complete   # parse-cache sentinel
    <base>/<registry>/_meta/_refs/<ref-slug>.json       # last SHA a floating ref had
    <base>/<registry>/_meta/<sha[:12]>/.lock            # cross-process fetch lock

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
file exists **and** carries a payload naming the current
:data:`CACHE_LAYOUT_VERSION` (written **last** during a fetch, after every
file in the workflow's containing directory has been promoted and the
registry metadata/index have been persisted). This prevents readers from
observing a partially populated workflow during a concurrent fetch, and
also means an empty or version-mismatched marker left over from an older
Conductor is treated as a cache miss rather than a stale hit — see
"Recursive asset acquisition" below for why the marker's *content*, not
merely its presence, has to be checked.

Recursive asset acquisition and the failure contract
=====================================================

A GitHub-registry fetch acquires **every regular file** beneath the
workflow's containing directory (recursively, via the Git Trees API — see
:func:`conductor.registry.github.list_files_recursive`), not just the
workflow file and its immediate siblings. This is what makes a workflow
whose ``prompt: !file`` (or a ``type: script`` step's data file) lives a
few directories below the workflow YAML resolve correctly once cached,
without conductor having to parse the workflow first to discover which
nested files it actually references — a workflow can reach a data file via
a script, a Jinja include, or any other indirection the loader never sees.
Symlinks and submodules are not followed.

The fetch is **strictly all-or-nothing**: a failure listing any
subdirectory, downloading any regular file (including one the workflow
never actually uses), staging into the temporary directory, or promoting a
staged file into the shared SHA root aborts the whole fetch with a
:class:`~conductor.registry.errors.RegistryError` naming the failing
repository, directory, or file. Readiness is only published — the
sentinel written — once the *entire* selected subtree has been acquired.
There is no best-effort partial fetch and no silently-skipped sibling.

Because a root-level workflow's "containing directory" is the repository
root, recursion for such a workflow acquires the entire repository tree.
Dedicated workflow directories keep acquisition scoped and fast.

Offline resolution and the parse cache
=======================================

Two additions make the catalogue answerable from a warm cache with zero
network I/O (MCP server plan E5 — see
``docs/projects/mcp-server/conductor-mcp.plan.md``):

* ``_meta/_refs/<ref-slug>.json`` records the SHA a floating ref (``latest``,
  a branch, a tag) last resolved to, written on every successful online
  resolution. It is modelled directly on ``plugins/fetch.py``'s own
  ``_refs/<slug>.json`` pointer, which exists for exactly this reason: without
  a record of what a floating ref last meant, an offline caller has no
  checkout to choose. ``fetch_workflow(..., allow_network=False)`` reads this
  pointer instead of calling the GitHub API, and raises :class:`RegistryError`
  when no pointer has been recorded yet.
* ``_meta/<sha>/tools.json`` (guarded by the ``tools.complete`` sentinel,
  written last, same as every other sentinel here) caches a normalized,
  already-resolved tool definition per workflow — its description, resolved
  input schema, and ``mcp:`` block — via :func:`save_parsed_tools` /
  :func:`load_parsed_tools`. A SHA-keyed entry is immutable, which is what
  makes reuse safe without re-verification.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from conductor.config.schema import InputDef, McpConfig
from conductor.registry.config import RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError
from conductor.registry.github import fetch_file, list_files_recursive, parse_github_source
from conductor.registry.index import RegistryIndex, WorkflowInfo, load_index, parse_index_text
from conductor.registry.version_resolver import materialize_to_sha, resolve_ref

if TYPE_CHECKING:
    from conductor.registry.resolver import ResolvedRef

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    import msvcrt

    fcntl = None
else:
    import fcntl

    msvcrt = None

# Reserved cache namespaces. Cannot collide with named registries because
# configured registry names are not allowed to contain '/' and these names
# start with '_'.
_ADHOC_NAMESPACE = "_adhoc"
_META_NAMESPACE = "_meta"
_REFS_NAMESPACE = "_refs"

# Per-workflow readiness markers live in their own subdirectory of the meta
# dir rather than directly alongside registry-level metadata files
# (tools.json/tools.complete, source.json, index.yaml). Workflow names are
# arbitrary registry-index keys an author controls, so a workflow literally
# named "tools" would otherwise collide with the SHA-keyed parse cache's own
# "tools.complete" sentinel — save_parsed_tools() would silently overwrite
# that workflow's readiness marker with an empty-string payload, or vice
# versa, and _is_workflow_ready()/load_parsed_tools() would each misread the
# other's marker.
_WORKFLOWS_META_NAMESPACE = "workflows"

# Current on-disk cache layout version. Bumping this invalidates all existing
# caches (their source.json will fail validation and the entries are re-fetched).
# v3: ParsedToolInfo (tools.json) gained a `name` field carrying the workflow's
# declared WorkflowDef.name, so a tier-2 cache hit doesn't lose FR3 naming.
# v4: per-workflow fetches acquire every regular file recursively beneath the
# workflow's containing directory (issue #530), not just the workflow file and
# its immediate siblings. A version-3 cache only ever populated the shallow
# sibling set, so it must not be mistaken for a fully-acquired subtree —
# see `_workflow_readiness_marker` / `_is_workflow_ready` below, which check
# the per-workflow `.complete` marker's *content* (not merely its presence)
# for exactly this reason: `mcp/serve/catalogue.py::_resolve_registry_index`
# can refresh a registry's `source.json`/`index.yaml` (bumping the recorded
# layout version) without ever calling `fetch_workflow`, so relying on
# `source.json` alone could resurrect a stale version-3 marker as "ready".
CACHE_LAYOUT_VERSION = 4

_SHA_DIR_RE = re.compile(r"^[0-9a-f]{12}$")
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# Characters kept when turning a ref into a filename. A ref may contain '/'
# (e.g. "release/1.x"), which would otherwise create directories inside
# _refs. Mirrors plugins/fetch.py's _REF_SLUG_UNSAFE.
_REF_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


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


def _cache_lock_path(registry_name: str, sha: str) -> Path:
    """Return the lock file path guarding cache mutations for a registry+SHA."""
    return _meta_dir(registry_name, sha) / ".lock"


@contextlib.contextmanager
def _cache_write_lock(registry_name: str, sha: str) -> Iterator[None]:
    """Cross-process advisory lock guarding cache mutations for one registry+SHA.

    Held from stale-metadata invalidation through readiness-marker
    publication so two concurrent fetchers for the same registry+SHA — even
    in separate processes — cannot interleave. Without this, a slower
    fetcher that misses the cache can invalidate or clobber a faster
    sibling's just-published, fully-valid readiness marker (or the metadata
    it depends on) before observing that the sibling already finished; a
    subsequent failure in the slower fetcher then leaves no valid cache
    entry at all, even though one briefly existed.

    Uses an OS-level advisory lock (``fcntl.flock`` on POSIX,
    ``msvcrt.locking`` on Windows) on a dedicated lock file rather than a
    lock-directory/PID-file scheme, so the lock is automatically released
    by the OS if the holding process dies or is killed — a crash can never
    leave a stale lock blocking every future fetch for that registry+SHA.
    """
    lock_path = _cache_lock_path(registry_name, sha)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        if msvcrt is not None:
            # msvcrt.locking() locks byte ranges of the CRT-level file, which
            # requires at least one byte to exist; LK_LOCK blocks internally
            # (raising and retrying) until the lock is acquired.
            with contextlib.suppress(OSError):
                os.write(fd, b"\0")
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                with contextlib.suppress(OSError):
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _sentinel_path(registry_name: str, sha: str, workflow_name: str) -> Path:
    """Return the readiness sentinel for a single workflow within a SHA dir.

    Lives under a dedicated ``workflows/`` subdirectory of the meta dir so a
    workflow named e.g. ``tools`` can never collide with the SHA-keyed parse
    cache's own ``tools.complete`` sentinel (or any other registry-level
    metadata file that lives directly in the meta dir).
    """
    # workflow_name is a registry index key; sanitize for filesystem use.
    safe = workflow_name.replace("/", "_").replace("\\", "_")
    return _meta_dir(registry_name, sha) / _WORKFLOWS_META_NAMESPACE / f"{safe}.complete"


def _readiness_marker_payload() -> str:
    """Return the versioned payload written to a per-workflow ``.complete`` sentinel."""
    return json.dumps({"cache_layout_version": CACHE_LAYOUT_VERSION}, sort_keys=True)


def _write_readiness_marker(sentinel: Path) -> None:
    """Atomically write the versioned readiness marker for a workflow.

    Unlike the pre-v4 marker (an empty file whose mere *presence* meant
    "ready"), the payload names the :data:`CACHE_LAYOUT_VERSION` that
    produced it. This lets :func:`_is_workflow_ready` distinguish a fully
    recursive v4 fetch from an older Conductor's shallow-siblings fetch —
    which left an empty marker behind — even when a metadata-only refresh
    (``mcp/serve/catalogue.py::_resolve_registry_index``) has already
    bumped ``source.json`` to the current layout version without ever
    re-running :func:`_fetch_github`.
    """
    _atomic_write_text(sentinel, _readiness_marker_payload())


def _is_workflow_ready(sentinel: Path) -> bool:
    """Return ``True`` only if *sentinel* exists and names the current layout version.

    An empty marker (pre-v4), one from a different layout version, or one
    that fails to parse is treated as a cache miss rather than a stale hit.
    """
    try:
        text = sentinel.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and data.get("cache_layout_version") == CACHE_LAYOUT_VERSION


def _invalidate_readiness_marker(sentinel: Path) -> None:
    """Remove a workflow's readiness marker before rebuilding its cache entry.

    Best-effort: a marker that fails to delete (already absent, or a
    transient filesystem error) does not block the fetch that is about to
    overwrite the files it used to certify. Called *before* staging or
    promotion begins so a concurrent reader can never observe a stale
    marker (from an older layout version, or from this same workflow's
    previous fetch) while this fetch is overwriting the mirrored files it
    describes in place.
    """
    with contextlib.suppress(OSError):
        sentinel.unlink()


def _tools_cache_path(registry_name: str, sha: str) -> Path:
    """Return the SHA-keyed parse-cache file for a registry."""
    return _meta_dir(registry_name, sha) / "tools.json"


def _tools_cache_sentinel(registry_name: str, sha: str) -> Path:
    """Return the readiness sentinel for the SHA-keyed parse cache."""
    return _meta_dir(registry_name, sha) / "tools.complete"


def _refs_dir(registry_name: str) -> Path:
    """Return the directory holding ref→SHA pointers for a registry."""
    return _registry_root(registry_name) / _META_NAMESPACE / _REFS_NAMESPACE


def _ref_slug(ref: str | None) -> str:
    """Turn a ref into a filename-safe, collision-free slug.

    Mirrors ``plugins/fetch.py``'s ``_ref_slug``: the sanitised name alone is
    lossy (``release/1.x`` and ``release_1.x`` would collide, and a branch
    literally named ``_default`` would collide with the no-ref case), so a
    short hash of the original is appended.

    ``None`` and ``"latest"`` (case-insensitively) both mean "the registry's
    default branch" per :func:`~conductor.registry.version_resolver.resolve_ref`,
    so they are normalized to the same slug — a single pointer file serves
    both spellings rather than two files that could disagree.
    """
    normalized = None if ref is None or ref.lower() == "latest" else ref
    if not normalized:
        return "_default"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{_REF_SLUG_UNSAFE.sub('_', normalized)}-{digest}"


def _ref_pointer_path(registry_name: str, ref: str | None) -> Path:
    """Return the file recording the last SHA a floating ref resolved to."""
    return _refs_dir(registry_name) / f"{_ref_slug(ref)}.json"


def _read_ref_pointer(registry_name: str, ref: str | None) -> str | None:
    """Return the SHA a floating ref last resolved to, if a pointer exists.

    Returns ``None`` when there is no pointer, it cannot be parsed, or its
    recorded SHA is not a well-formed 40-character hex string.
    """
    pointer = _ref_pointer_path(registry_name, ref)
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError, OSError):
        return None
    sha = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(sha, str) or not _FULL_SHA_RE.match(sha):
        return None
    return sha.lower()


def _write_ref_pointer(registry_name: str, ref: str | None, sha: str) -> None:
    """Record which SHA a ref resolved to, for the offline fallback.

    Best-effort: an unwritable cache costs a future offline run its fallback,
    which is not worth failing a working online fetch over. Uses the same
    atomic temp-file-plus-rename convention as the rest of this module so a
    concurrent reader never observes a half-written pointer.
    """
    pointer = _ref_pointer_path(registry_name, ref)
    try:
        payload = json.dumps({"ref": ref, "sha": sha}, sort_keys=True)
        _atomic_write_text(pointer, payload)
    except OSError as exc:
        logger.debug("Could not record ref pointer %s: %s", pointer, exc)


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


# ---------------------------------------------------------------------------
# SHA-keyed parse cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedToolInfo:
    """A normalized, already-resolved tool definition, cached per SHA.

    Captures exactly what the MCP catalogue builder needs to publish a
    workflow as a tool without re-parsing its YAML: its description,
    resolved input schema, ``mcp:`` block, and declared ``WorkflowDef.name``
    (FR3 — without it, a tier-2 cache hit would fall back to the registry
    index key/workflow filename stem for naming, silently disagreeing with
    the tier-3 parse that first populated the cache). Keyed by workflow name
    and persisted under ``_meta/<sha[:12]>/tools.json`` (schema-ladder tier
    2; see ``docs/projects/mcp-server/conductor-mcp.design.md``, Key
    Components → 1). Immutable because the SHA it is cached under is
    immutable — that is what makes reuse safe without re-verification.
    """

    description: str
    input: dict[str, InputDef]
    mcp: McpConfig
    name: str | None = None


def save_parsed_tools(registry_name: str, sha: str, tools: dict[str, ParsedToolInfo]) -> None:
    """Atomically persist the SHA-keyed parse cache for a registry.

    Writes ``tools.json`` and then the ``tools.complete`` sentinel **last**,
    matching the readiness-sentinel convention used everywhere else in this
    module, so a concurrent reader never observes a half-written cache.

    Args:
        registry_name: Name of the registry the SHA belongs to.
        sha: Full immutable commit SHA (only the first 12 chars are used as
            the on-disk directory name, same as elsewhere in this module).
        tools: Parsed tool definitions, keyed by workflow name.
    """
    meta_dir = _meta_dir(registry_name, sha)
    meta_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "cache_layout_version": CACHE_LAYOUT_VERSION,
        "tools": {
            name: {
                "description": info.description,
                "input": {key: value.model_dump(mode="json") for key, value in info.input.items()},
                "mcp": info.mcp.model_dump(mode="json"),
                "name": info.name,
            }
            for name, info in tools.items()
        },
    }
    _atomic_write_text(_tools_cache_path(registry_name, sha), json.dumps(payload, sort_keys=True))
    _atomic_write_text(_tools_cache_sentinel(registry_name, sha), "")


def load_parsed_tools(registry_name: str, sha: str) -> dict[str, ParsedToolInfo] | None:
    """Load the SHA-keyed parse cache for a registry.

    Returns ``None`` when the readiness sentinel is missing (a write is in
    progress, was interrupted, or never happened), the cache layout version
    does not match :data:`CACHE_LAYOUT_VERSION`, or the file cannot be
    parsed — callers fall back to fetching and parsing the workflow file
    directly (schema-ladder tier 3).

    A single malformed entry is skipped rather than discarding the whole
    cache, so one bad row does not cost every other workflow its warm-cache
    hit.
    """
    if not _tools_cache_sentinel(registry_name, sha).is_file():
        return None
    try:
        raw_text = _tools_cache_path(registry_name, sha).read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or data.get("cache_layout_version") != CACHE_LAYOUT_VERSION:
        return None
    raw_tools = data.get("tools")
    if not isinstance(raw_tools, dict):
        return None

    result: dict[str, ParsedToolInfo] = {}
    for name, raw in raw_tools.items():
        try:
            raw_name = raw.get("name")
            result[name] = ParsedToolInfo(
                description=str(raw["description"]),
                input={key: InputDef.model_validate(value) for key, value in raw["input"].items()},
                mcp=McpConfig.model_validate(raw["mcp"]),
                name=str(raw_name) if raw_name is not None else None,
            )
        except Exception as exc:
            logger.debug("Skipping malformed parse-cache entry %r for %s: %s", name, sha, exc)
            continue
    return result


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
       (``_meta/<sha>/<workflow_name>.complete``) exists **and** names the
       current :data:`CACHE_LAYOUT_VERSION` (see :func:`_is_workflow_ready`)
       — an empty or version-mismatched marker left over from an older
       Conductor is a miss, not a stale hit.
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
    if not _is_workflow_ready(sentinel):
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


def _resolve_sha_offline(registry_name: str, ref: str | None) -> str:
    """Resolve a github ref to a SHA without touching the network.

    An already-fully-qualified 40-character SHA is normalized to lowercase
    and used directly — it is already immutable, so there is nothing to
    look up. Anything else (``None``, ``"latest"``, a branch, or a tag) is a
    floating ref and can only be resolved via a pointer
    :func:`_write_ref_pointer` recorded during a prior online call.

    Raises:
        RegistryError: If ``ref`` is not already a SHA and no ref pointer
            has been recorded for it.
    """
    if ref is not None and _FULL_SHA_RE.match(ref):
        return ref.lower()

    cached_sha = _read_ref_pointer(registry_name, ref)
    if cached_sha is not None:
        return cached_sha

    ref_label = ref if ref else "the default branch"
    raise RegistryError(
        f"Ref {ref_label!r} for registry '{registry_name}' has not been resolved on this "
        "machine and network access is not permitted.",
        suggestion=(
            "Fetch this workflow once with network access (e.g. via 'conductor run' or "
            "'conductor validate') to record the ref, or pin to an explicit commit SHA."
        ),
    )


def _try_cached_workflow_hit(
    registry_name: str, registry_entry: RegistryEntry, workflow_name: str, sha: str
) -> Path | None:
    """Return the cached workflow path if a valid, ready cache entry exists.

    Read-only — requires matching source metadata, a cached index naming
    *workflow_name*, and a readiness marker naming the current cache layout
    version (see :func:`get_cached_workflow_path`). Used both for the fast
    uncontended check in :func:`fetch_workflow` and, after acquiring
    :func:`_cache_write_lock`, to detect that a concurrent fetcher already
    published the exact entry this call needs — so a slower caller reuses
    it instead of re-fetching or invalidating it.
    """
    meta = _meta_dir(registry_name, sha)
    metadata = _read_source_metadata(meta)
    if not _metadata_matches(metadata, registry_entry, sha):
        return None
    index = _load_cached_index(meta)
    if index is None or workflow_name not in index.workflows:
        return None
    return get_cached_workflow_path(
        registry_name,
        workflow_name,
        sha,
        workflow_repo_path=index.workflows[workflow_name].path,
    )


def fetch_workflow(
    registry_name: str,
    registry_entry: RegistryEntry,
    workflow_name: str,
    ref: str | None = None,
    *,
    allow_network: bool = True,
) -> Path:
    """Fetch a workflow from a registry and cache it locally.

    For **path** registries, reads directly from the source directory (no
    caching). The ``ref`` argument must be ``None`` — :func:`resolve_ref`
    raises if a ref is provided for a path registry.

    For **github** registries:

    1. Resolve ``ref`` (or "latest") to a concrete git ref name and
       materialize to an immutable commit SHA.
    2. If the source metadata already matches and the per-workflow readiness
       marker names the current cache layout version, return the cached
       path.
    3. Otherwise, load the index pinned to the SHA (preferring the cached
       copy under ``_meta/<sha>/index.yaml``), recursively fetch every
       regular file beneath the workflow's containing directory into a
       staging dir, atomically promote each file into the shared SHA root,
       and finally write the readiness marker.

    Args:
        registry_name: Configured registry name.
        registry_entry: The registry definition (type + source).
        workflow_name: Workflow key as listed in the registry index.
        ref: Explicit git ref (tag, branch, or SHA), or ``None`` for the
            registry's default (default branch HEAD).
        allow_network: When ``False``, resolve entirely from the local
            cache — no GitHub API calls. A floating ``ref`` (``None``,
            ``"latest"``, a branch, or a tag) resolves through the ref
            pointer recorded by a previous online call
            (:func:`_write_ref_pointer`); an already-fully-qualified 40-char
            SHA is used verbatim. A cache miss at any point (no recorded
            pointer, or the workflow is not fully cached at the resolved
            SHA) is a typed :class:`RegistryError` naming the fetch path,
            rather than a silent HTTP call. This is the same posture
            ``plugins/resolution.py`` takes between ``conductor run`` and
            ``conductor validate``. Ignored for path registries, which
            never touch the network.

    Returns:
        Path to the cached workflow YAML file.

    Raises:
        RegistryError: On fetch failure, missing workflow, cache miss while
            ``allow_network=False``, or an I/O error. A failure listing or
            downloading *any* regular file beneath the workflow's
            containing directory — including one the workflow does not
            itself reference — aborts the whole fetch; there is no
            best-effort partial acquisition.
    """
    # Path registries: read directly from source. resolve_ref raises if a
    # ref was supplied, propagating a clear error to the caller. No network
    # is ever involved, so allow_network is a no-op here.
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
    if allow_network:
        resolved_ref = resolve_ref(registry_entry, ref)
        sha = materialize_to_sha(registry_entry, resolved_ref)
        # Record what this ref resolved to, so a later cache-only caller can
        # resolve the same floating ref without touching the network.
        _write_ref_pointer(registry_name, ref, sha)
    else:
        sha = _resolve_sha_offline(registry_name, ref)

    # Fast, uncontended check: no lock needed for a pure read.
    cached = _try_cached_workflow_hit(registry_name, registry_entry, workflow_name, sha)
    if cached is not None:
        return cached

    if not allow_network:
        raise RegistryError(
            f"Workflow '{workflow_name}' in registry '{registry_name}' at {sha[:12]} is not "
            "available in the local cache and network access is not permitted.",
            suggestion=(
                f"Run 'conductor run'/'conductor validate' against registry '{registry_name}' "
                "with network access once to prime the cache for this workflow."
            ),
        )

    # Every step from here on mutates shared cache state (metadata
    # invalidation, the workflow's readiness marker, the SHA-rooted file
    # mirror), so it is serialized per registry+SHA: two concurrent
    # fetchers for the same workflow must never interleave, and a slower
    # one must never invalidate or clobber what a faster one already
    # published.
    with _cache_write_lock(registry_name, sha):
        # Re-check now that we hold the lock: another process may have
        # completed this exact fetch (or refreshed stale metadata) while we
        # were waiting for it, in which case we reuse that result instead of
        # racing it or invalidating what it just published.
        cached = _try_cached_workflow_hit(registry_name, registry_entry, workflow_name, sha)
        if cached is not None:
            return cached

        meta = _meta_dir(registry_name, sha)
        metadata = _read_source_metadata(meta)
        matches = _metadata_matches(metadata, registry_entry, sha)
        if not matches and metadata is not None:
            # Stale or missing metadata — clear the meta dir to avoid serving
            # an inconsistent index/source on a subsequent miss. Don't touch
            # the SHA mirror itself; new fetches will overwrite
            # content-addressed files.
            shutil.rmtree(meta, ignore_errors=True)

        # Fetch the index from the upstream registry (pinned to SHA) and persist it.
        index = load_index(registry_entry, ref=sha)
        if workflow_name not in index.workflows:
            raise RegistryError(
                f"Workflow '{workflow_name}' not found in registry '{registry_name}'",
                suggestion=(
                    f"Run 'conductor registry list {registry_name}' to see available workflows."
                ),
            )
        workflow_info = index.workflows[workflow_name]
        safe_path = _safe_repo_path(workflow_info.path)

        sentinel = _sentinel_path(registry_name, sha, workflow_name)
        # Invalidate any existing readiness marker *before* rebuilding — a
        # concurrent reader must never observe an older-version (or otherwise
        # stale) marker while this fetch is overwriting the mirrored files it
        # describes in place. Safe under the lock: any marker still present
        # here was not published by a sibling we already deferred to above.
        _invalidate_readiness_marker(sentinel)

        sha_root = _sha_dir(registry_name, sha)
        sha_root.mkdir(parents=True, exist_ok=True)

        # Stage everything in a temp dir under the meta dir (intra-filesystem,
        # so per-file os.replace into sha_root is atomic).
        meta.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix=f".tmp-{workflow_name}-", dir=meta))
        try:
            _fetch_github(registry_entry, str(safe_path), sha, tmp_dir)
            _promote_staged_files(tmp_dir, sha_root)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        # Persist metadata + cached index, then write the readiness marker
        # **last** so concurrent readers never observe a partial fetch.
        _write_source_metadata(meta, registry_entry, sha)
        _save_cached_index(meta, _index_to_yaml(index))

        workflow_path = _resolve_within(sha_root, safe_path)
        if not workflow_path.is_file():
            raise RegistryError(
                f"Workflow file '{safe_path}' not found in cache after fetch",
                suggestion="The registry index may reference a file that does not exist.",
            )

        _write_readiness_marker(sentinel)

        return workflow_path


def fetch_workflow_adhoc(
    owner: str,
    repo: str,
    workflow_name: str,
    ref: str | None = None,
    *,
    allow_network: bool = True,
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
        allow_network: When ``False``, resolve entirely from the local
            cache. See :func:`fetch_workflow` for the full contract.

    Returns:
        Path to the cached workflow YAML file.

    Raises:
        RegistryError: On fetch failure, missing workflow, cache miss while
            ``allow_network=False``, or I/O errors.
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
        allow_network=allow_network,
    )


def resolve_and_fetch(resolved: ResolvedRef, *, allow_network: bool = True) -> Path:
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
        allow_network: When ``False``, resolve entirely from the local
            cache for ``registry``/``adhoc`` kinds — see
            :func:`fetch_workflow` for the full contract. Has no effect on
            the ``file`` kind, which never touches the network.

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
            allow_network=allow_network,
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
            allow_network=allow_network,
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
    """Recursively fetch every regular file beneath the workflow's containing
    directory from a GitHub registry into a staging dir.

    Files are written into ``dest_dir`` preserving the full repository-
    relative layout. For example, fetching ``sdd-plan/plan.yaml`` (with a
    nested ``prompts/`` and ``scripts/`` beneath ``sdd-plan/``) writes::

        <dest_dir>/sdd-plan/plan.yaml
        <dest_dir>/sdd-plan/prompts/plan.md
        <dest_dir>/sdd-plan/scripts/run.sh

    This is strictly all-or-nothing (issue #530): listing the directory
    tree or downloading any regular file within it — including one the
    workflow itself never references — raises :class:`RegistryError` and
    aborts the whole fetch. There is no best-effort partial acquisition,
    and unlisted/malformed entries are never silently skipped.

    Args:
        registry_entry: Registry entry with ``source`` as ``owner/repo``.
        workflow_path: Validated repo-relative path to the workflow YAML.
        sha: Immutable commit SHA to fetch at.
        dest_dir: Local staging directory to write files into.

    Raises:
        RegistryError: If the containing directory cannot be listed, the
            workflow itself is not a regular file in the enumerated
            subtree, or any regular file fails to download.
    """
    owner, repo = parse_github_source(registry_entry.source)
    workflow_p = PurePosixPath(workflow_path)
    parent_dir = workflow_p.parent  # PurePosixPath('.') for repo-root workflows
    parent_dir_str = str(parent_dir) if str(parent_dir) != "." else "."
    repo_label = f"{owner}/{repo}"

    try:
        repo_paths = list_files_recursive(owner, repo, parent_dir_str, ref=sha)
    except RegistryError as exc:
        raise RegistryError(
            f"Failed to list files under '{parent_dir_str}' in {repo_label} at {sha[:12]}: {exc}"
        ) from exc

    if workflow_path not in repo_paths:
        raise RegistryError(
            f"Workflow file '{workflow_path}' was not found as a regular file in "
            f"{repo_label} at {sha[:12]} (it may be a symlink, submodule, or missing).",
            suggestion="Check the 'path' field in the registry index points at a real file.",
        )

    for repo_path in repo_paths:
        # Defense-in-depth: list_files_recursive is already scoped to
        # parent_dir_str, but re-validate shape and subtree membership
        # before trusting a path enough to write to disk.
        safe = _safe_repo_path(repo_path)
        if parent_dir_str != "." and not _is_within_directory(safe, parent_dir_str):
            raise RegistryError(
                f"Listing for {repo_label} at {sha[:12]} returned a path outside the "
                f"workflow's containing directory: {repo_path!r}",
                suggestion="This indicates a bug in the registry listing; please report it.",
            )

        try:
            content = fetch_file(owner, repo, repo_path, ref=sha)
        except RegistryError as exc:
            raise RegistryError(
                f"Failed to download '{repo_path}' from {repo_label} at {sha[:12]}: {exc}"
            ) from exc

        target = _resolve_within(dest_dir, safe)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _is_within_directory(repo_path: PurePosixPath, directory: str) -> bool:
    """Return True if *repo_path* lives at or under *directory* (repo-relative)."""
    try:
        repo_path.relative_to(PurePosixPath(directory))
    except ValueError:
        return False
    return True


def _promote_staged_files(tmp_dir: Path, sha_root: Path) -> None:
    """Move every file from ``tmp_dir`` into ``sha_root`` atomically.

    Preserves the relative directory layout of ``tmp_dir``. Each file is
    moved with ``os.replace()`` so concurrent readers see either the old or
    the new file, never a half-written one. Files in ``sha_root`` are
    content-addressed by the immutable SHA so overwriting an existing entry
    with the same content is safe and idempotent.

    Raises:
        RegistryError: If any staged path is unsafe or would escape
            ``sha_root`` — this aborts promotion rather than silently
            omitting the offending file, matching the strict completeness
            contract of :func:`_fetch_github`.
    """
    for src in sorted(tmp_dir.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(tmp_dir)
        # Defense-in-depth: re-validate that the staged path stays inside
        # sha_root (catches any unexpected absolute component). Unlike the
        # pre-#530 best-effort loop, a bad path here is fatal — a staged
        # file that can't be safely promoted must fail the whole fetch,
        # not be dropped in silence.
        safe = _safe_repo_path(str(rel))
        dest = _resolve_within(sha_root, safe)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dest)


def _index_to_yaml(index: RegistryIndex) -> str:
    """Render a RegistryIndex back to YAML for persistence in the meta dir.

    Round-trips the optional ``input``/``mcp`` fields (E5-T1) too — omitting
    them here would silently drop a registry's tier-1 schema declarations
    the moment the index is served from the cached copy on a subsequent
    warm run.
    """
    from io import StringIO

    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.default_flow_style = False
    data = {
        "workflows": {name: _workflow_info_to_dict(info) for name, info in index.workflows.items()}
    }
    buf = StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()


def _workflow_info_to_dict(info: WorkflowInfo) -> dict:
    """Render a single ``WorkflowInfo`` to a plain, YAML-safe dict.

    Omits ``input``/``mcp`` entirely when unset (``None``) rather than
    writing an explicit ``null``, so a cached index for a workflow that
    never declared them round-trips byte-for-byte in shape to one that
    never had the fields at all.
    """
    result: dict = {"description": info.description, "path": info.path}
    if info.input is not None:
        result["input"] = {
            key: value.model_dump(mode="json", exclude_none=True)
            for key, value in info.input.items()
        }
    if info.mcp is not None:
        result["mcp"] = info.mcp.model_dump(mode="json")
    return result


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
