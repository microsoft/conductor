"""Build the frozen tool catalogue (design's *Key Components -> 1 and 2*,
FR2, FR3, NFR1, NFR2, DD4, DD6, DD10, E7-T6).

``build_catalogue(...)`` turns configuration into an immutable list of
generated ``mcp.types.Tool`` objects at startup. No protocol is spoken
here and no workflow is ever launched — this module is pure functions
over the registry index / cache and the local filesystem.

Pipeline, in order:

1. Enumerate the registries selected by ``--registry`` (one level above
   the exposure ladder — a registry outside this set is never a
   candidate, full stop).
2. For each registry, load its index — for a GitHub registry, through the
   E5 offline-capable ref-pointer + SHA-keyed cache path, so a warm cache
   never touches the network (NFR1).
3. Filter each workflow through the four-rung exposure ladder (``--deny``
   > ``--allow`` > ``mcp.expose`` > default-on, DD4).
4. Resolve each surviving workflow's schema through the three-tier ladder
   (index-provided -> SHA-keyed parse cache -> fetch-and-parse), under a
   startup deadline. A tier-3 failure — including the ``${VAR}``-missing
   and unresolvable-``!file`` cases the design calls out — degrades to a
   permissive ``{"type": "object"}`` schema and an explanatory
   description rather than dropping the workflow (NFR2).
5. Pin each surviving workflow to an immutable identity (DD6).
6. Reject any workflow whose ``input:`` collides with the reserved
   ``_wait_seconds`` parameter, logging why (FR10) — this is an
   authoring conflict, not an environmental failure, so it is excluded
   deliberately rather than degraded.
7. Sanitize descriptions (NFR4), qualify name collisions (DD10), and
   assemble the final ``Tool`` objects.
8. Decide direct-tools vs. discovery mode (FR9).

The result is immutable for the server process's lifetime (DD3).
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from mcp.types import Tool

from conductor.config.loader import load_config
from conductor.config.schema import InputDef, McpConfig, WorkflowConfig
from conductor.config.validator import MCP_RESERVED_WAIT_SECONDS_INPUT
from conductor.exceptions import ConfigurationError
from conductor.mcp.serve.naming import NameCollision, ToolIdentity, build_tool_names
from conductor.mcp.serve.options import ServeOptions
from conductor.mcp.serve.pinning import Pin, pin_content, pin_content_file
from conductor.mcp.serve.sanitize import sanitize_description
from conductor.mcp.serve.toolgen import build_tool
from conductor.registry import cache as registry_cache
from conductor.registry.cache import (
    ParsedToolInfo,
    fetch_workflow,
    load_parsed_tools,
    save_parsed_tools,
)
from conductor.registry.config import RegistriesConfig, RegistryEntry, RegistryType
from conductor.registry.config import load_config as load_registries_config
from conductor.registry.errors import RegistryError
from conductor.registry.index import RegistryIndex, WorkflowInfo, load_index
from conductor.registry.version_resolver import materialize_to_sha, resolve_ref

logger = logging.getLogger(__name__)

# NFR1: cold-start to first `tools/list` response <= 2s with a warm cache.
# This bounds tier-3 (fetch-and-parse) attempts specifically -- a tier-1 or
# tier-2 hit never reaches this check. A workflow whose tier-3 resolution
# would blow the deadline degrades (NFR2) rather than stalling startup.
DEFAULT_SCHEMA_RESOLUTION_DEADLINE_SECONDS = 2.0

# Non-recursive: only files directly under a `--workflow-dir` are exposed,
# mirroring the "expand a root into its immediate children" convention used
# elsewhere in the codebase (e.g. skills/registry.py::expand_skills_root).
_WORKFLOW_DIR_EXTENSIONS = (".yaml", ".yml")

ResolutionTier = Literal["index", "cache", "parsed", "degraded"]


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogueEntry:
    """One workflow published as an MCP tool."""

    tool_name: str
    registry: str
    workflow: str
    tool: Tool
    pin: Pin
    resolution_tier: ResolutionTier
    source: str = ""
    """The candidate's own disambiguator (``_Candidate.source`` / ``ToolIdentity.source``)
    -- e.g. a ``--workflow-dir`` file's resolved path. ``registry``/``workflow`` alone
    (what ``Catalogue.reverse`` exposes) can be shared by two distinct directories with
    the same basename and a same-named file; a path-resolution caller that already has
    the exact entry for a tool name (rather than only its reduced ``(registry, workflow)``
    pair) must use this to resolve the file that was actually scanned, not re-derive one
    that happens to match by name."""


@dataclass(frozen=True)
class RejectedWorkflow:
    """A workflow the catalogue builder considered but did not expose, and
    why. Always logged (FR10) — never a silent drop."""

    registry: str
    workflow: str
    reason: str


@dataclass(frozen=True)
class FailedRegistry:
    """A whole registry whose index could not be resolved and was
    skipped. Always logged -- never a silent drop (mirrors
    :class:`RejectedWorkflow`, one rung up: a whole registry rather than
    one workflow within it) -- and captured here so a caller that cannot
    rely on log output (e.g. ``conductor doctor``) still learns about it."""

    registry: str
    reason: str


@dataclass(frozen=True)
class Catalogue:
    """The frozen result of :func:`build_catalogue` (DD3): immutable for
    the server process's lifetime.

    ``reverse`` is a read-only mapping (a mutation attempt raises
    ``TypeError``). The canonical entries are stored privately
    (``_entries``); the public :attr:`entries` property and :meth:`tools`
    each return a deep copy of every generated ``Tool`` on every access —
    ``Tool`` is a third-party pydantic model whose ``inputSchema`` is an
    ordinary mutable dict, so a caller that mutated an entry's ``tool``
    (or a tool ``tools()`` returned) would otherwise corrupt the
    catalogue's own canonical data for every subsequent caller.
    """

    _entries: tuple[CatalogueEntry, ...]
    reverse: MappingProxyType[str, tuple[str, str]]
    """``tool name -> (registry, workflow)`` — what the invocation layer
    needs to know what to launch (DD10)."""

    collisions: tuple[NameCollision, ...]
    rejected: tuple[RejectedWorkflow, ...]
    failed_registries: tuple[FailedRegistry, ...]
    """Whole registries whose index could not be resolved and were
    skipped entirely (e.g. no warm cache under ``allow_network=False``, or
    an unreachable remote). Distinct from :attr:`rejected`, which covers
    individual workflows within a registry that *did* resolve."""
    mode: Literal["direct", "discovery"]
    """Whether per-workflow tools are served directly, or the
    ``discovery`` toolset pair replaces them because the exposed count
    exceeds ``--max-direct-tools`` (FR9). Recorded here; acted on by the
    server (E8/E12)."""

    @property
    def entries(self) -> tuple[CatalogueEntry, ...]:
        """The published entries, each carrying a defensive deep copy of
        its ``tool`` so a caller cannot mutate the catalogue's canonical
        ``Tool`` (and its nested ``inputSchema``) through this attribute.
        """
        return tuple(
            CatalogueEntry(
                tool_name=entry.tool_name,
                registry=entry.registry,
                workflow=entry.workflow,
                tool=entry.tool.model_copy(deep=True),
                pin=entry.pin,
                resolution_tier=entry.resolution_tier,
                source=entry.source,
            )
            for entry in self._entries
        )

    def tools(self) -> tuple[Tool, ...]:
        """The generated ``Tool`` objects, in a stable order.

        Returns a deep copy of each ``Tool`` (not the catalogue's own
        stored object) so a caller mutating a returned tool's
        ``inputSchema`` cannot corrupt the immutable catalogue.
        """
        return tuple(entry.tool.model_copy(deep=True) for entry in self._entries)


# ---------------------------------------------------------------------------
# Internal working types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """A workflow that survived the exposure ladder, with its schema
    resolved and its identity pinned — everything needed to name and
    generate its tool."""

    registry: str
    workflow: str
    description: str
    input: dict[str, InputDef]
    mcp: McpConfig
    tier: ResolutionTier
    pin: Pin
    source: str = ""
    """A unique-per-candidate discriminator independent of ``registry``/
    ``workflow`` (its *display* qualifier) — e.g. a ``--workflow-dir``
    file's resolved path. Two candidates with the same display registry
    and workflow key (two directories sharing a basename and a filename)
    must still be distinct identities, or one silently overwrites the
    other."""
    name: str | None = None
    """The workflow's own declared ``WorkflowDef.name``, when actually
    parsed (tier ``parsed``, or a ``--workflow-dir`` entry, which always
    parses) — ``None`` for a tier that never reads the workflow file
    (``index``, ``cache``) or a ``degraded`` entry with no schema at all.
    FR3 requires the published tool name be derived from this declared
    name so it agrees with ``conductor validate``; the registry key /
    filename stem is used only when the declared name is unknown."""


@dataclass(frozen=True)
class _ResolvedSchema:
    description: str
    input: dict[str, InputDef]
    mcp: McpConfig
    tier: ResolutionTier
    name: str | None = None


def _identity_for(candidate: _Candidate) -> ToolIdentity:
    """Build the :class:`ToolIdentity` for one candidate.

    ``source`` disambiguates two candidates that share the same display
    ``registry``/``workflow`` (e.g. two ``--workflow-dir`` directories with
    the same basename and a same-named file in each). ``display`` carries
    the workflow's declared ``WorkflowDef.name`` (FR3), when known, so
    naming derives from it rather than the registry key/filename stem.
    """
    return ToolIdentity(
        registry=candidate.registry,
        workflow=candidate.workflow,
        source=candidate.source,
        display=candidate.name,
    )


# ---------------------------------------------------------------------------
# build_catalogue
# ---------------------------------------------------------------------------


def build_catalogue(
    options: ServeOptions,
    *,
    registries_config: RegistriesConfig | None = None,
    allow_network: bool = True,
    schema_resolution_deadline: float = DEFAULT_SCHEMA_RESOLUTION_DEADLINE_SECONDS,
) -> Catalogue:
    """Build the frozen tool catalogue.

    Args:
        options: The frozen startup arguments (E7-T1).
        registries_config: The configured registries. Defaults to
            ``registry.config.load_config()`` (``~/.conductor/registries.toml``);
            overridable so tests and embedders can supply a fixture set
            without touching disk-backed configuration.
        allow_network: When ``False``, every registry resolution is
            answered entirely from the local cache — no GitHub API calls
            — matching NFR1. A cache miss under this constraint degrades
            that workflow (or, for a whole unreachable registry, skips
            it) rather than raising.
        schema_resolution_deadline: Wall-clock seconds, from this call's
            start, budgeted for tier-3 (fetch-and-parse) schema
            resolution. Exceeding it degrades the remaining unresolved
            workflows (NFR2) rather than stalling startup (NFR1).

    Returns:
        An immutable :class:`Catalogue`.
    """
    if registries_config is None:
        registries_config = load_registries_config()

    deadline = time.monotonic() + schema_resolution_deadline

    candidates: list[_Candidate] = []
    rejected: list[RejectedWorkflow] = []
    failed_registries: list[FailedRegistry] = []

    for registry_name in sorted(_select_registries(options, registries_config)):
        entry = registries_config.registries[registry_name]
        _collect_registry_candidates(
            registry_name,
            entry,
            options,
            allow_network=allow_network,
            deadline=deadline,
            candidates=candidates,
            failed_registries=failed_registries,
        )

    for workflow_dir in options.workflow_dirs:
        _collect_workflow_dir_candidates(
            workflow_dir, options, deadline=deadline, candidates=candidates
        )

    # Reject a workflow whose input collides with the reserved
    # `_wait_seconds` parameter before naming/tool generation ever sees it
    # (Tool generator note; FR10). This is an authoring conflict, not an
    # environmental failure -- it is excluded deliberately, not degraded.
    accepted: list[_Candidate] = []
    for candidate in candidates:
        if MCP_RESERVED_WAIT_SECONDS_INPUT in candidate.input:
            reason = (
                f"input {MCP_RESERVED_WAIT_SECONDS_INPUT!r} collides with the reserved "
                "parameter the tool generator injects into every tool's schema (FR5)"
            )
            rejected.append(RejectedWorkflow(candidate.registry, candidate.workflow, reason))
            logger.warning(
                "Excluding %s/%s from the MCP catalogue: %s",
                candidate.registry,
                candidate.workflow,
                reason,
            )
            continue
        accepted.append(candidate)

    identities = [_identity_for(c) for c in accepted]
    naming_result = build_tool_names(identities, tool_prefix=options.tool_prefix)

    for identity, reason in naming_result.rejected.items():
        rejected.append(RejectedWorkflow(identity.registry, identity.workflow, reason))
        logger.warning(
            "Excluding %s/%s from the MCP catalogue: %s",
            identity.registry,
            identity.workflow,
            reason,
        )

    for collision in naming_result.collisions:
        logger.warning(
            "Tool name collision on %r across %s -- qualified as %s",
            collision.base_slug,
            ", ".join(f"{i.registry}/{i.workflow}" for i in collision.identities),
            ", ".join(collision.qualified_names),
        )

    by_identity = {_identity_for(c): c for c in accepted}
    entries: list[CatalogueEntry] = []
    for identity, tool_name in naming_result.names.items():
        candidate = by_identity[identity]
        sanitized_description = sanitize_description(candidate.description)
        tool = build_tool(
            tool_name,
            description=sanitized_description,
            inputs=candidate.input,
            mcp=candidate.mcp,
        )
        entries.append(
            CatalogueEntry(
                tool_name=tool_name,
                registry=candidate.registry,
                workflow=candidate.workflow,
                tool=tool,
                pin=candidate.pin,
                resolution_tier=candidate.tier,
                source=candidate.source,
            )
        )
    entries.sort(key=lambda entry: entry.tool_name)

    mode: Literal["direct", "discovery"] = "direct"
    if len(entries) > options.max_direct_tools:
        mode = "discovery"
        logger.warning(
            "%d exposed workflows exceeds --max-direct-tools=%d; serving the discovery "
            "toolset instead of per-workflow tools (FR9)",
            len(entries),
            options.max_direct_tools,
        )

    return Catalogue(
        _entries=tuple(entries),
        reverse=MappingProxyType(
            {
                name: (identity.registry, identity.workflow)
                for name, identity in naming_result.reverse.items()
            }
        ),
        collisions=naming_result.collisions,
        rejected=tuple(rejected),
        failed_registries=tuple(failed_registries),
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Registry enumeration and the exposure ladder
# ---------------------------------------------------------------------------


def _select_registries(
    options: ServeOptions, registries_config: RegistriesConfig
) -> dict[str, RegistryEntry]:
    """Rung above the exposure ladder: which registries are enumerated at
    all (FR2). ``--registry`` is glob-capable; a registry outside the
    selected set is never a candidate, regardless of any other flag."""
    if options.registries is None:
        return dict(registries_config.registries)
    return {
        name: entry
        for name, entry in registries_config.registries.items()
        if any(fnmatch.fnmatch(name, pattern) for pattern in options.registries)
    }


def _passes_deny_allow(workflow_key: str, options: ServeOptions) -> tuple[bool, bool]:
    """Cheaply evaluate exposure-ladder rungs 1-2 (``--deny`` / ``--allow``),
    which depend only on the workflow's identifier -- never its schema.

    Returns ``(survives, allow_matched)``. ``allow_matched`` is ``True``
    only when an ``--allow`` pattern matched, which (per DD4) overrides
    the workflow's own ``mcp.expose: false`` once its schema is resolved.
    """
    if any(fnmatch.fnmatch(workflow_key, pattern) for pattern in options.deny):
        return False, False
    if options.allow:
        matched = any(fnmatch.fnmatch(workflow_key, pattern) for pattern in options.allow)
        return matched, matched
    return True, False


# ---------------------------------------------------------------------------
# Registry-backed candidates
# ---------------------------------------------------------------------------


def _collect_registry_candidates(
    registry_name: str,
    entry: RegistryEntry,
    options: ServeOptions,
    *,
    allow_network: bool,
    deadline: float,
    candidates: list[_Candidate],
    failed_registries: list[FailedRegistry],
) -> None:
    try:
        index, sha = _resolve_registry_index(registry_name, entry, allow_network=allow_network)
    except RegistryError as exc:
        # A whole registry being unreachable is an environmental failure
        # like any single workflow's parse failure -- log and skip rather
        # than aborting the entire catalogue build. Captured structurally
        # (not just logged) so a caller like `conductor doctor` still
        # learns about it even with logging suppressed.
        logger.warning("Skipping registry %r: could not resolve its index: %s", registry_name, exc)
        failed_registries.append(FailedRegistry(registry_name, str(exc)))
        return

    parsed_cache: dict[str, ParsedToolInfo] = {}
    if sha is not None:
        parsed_cache = load_parsed_tools(registry_name, sha) or {}
    cache_dirty = False

    for workflow_key in sorted(index.workflows):
        info = index.workflows[workflow_key]
        survives, allow_matched = _passes_deny_allow(workflow_key, options)
        if not survives:
            continue

        resolved = _resolve_registry_workflow_schema(
            registry_name,
            entry,
            workflow_key,
            info,
            sha,
            parsed_cache=parsed_cache,
            allow_network=allow_network,
            deadline=deadline,
        )
        if resolved.tier == "parsed" and sha is not None and workflow_key not in parsed_cache:
            parsed_cache[workflow_key] = ParsedToolInfo(
                description=resolved.description,
                input=resolved.input,
                mcp=resolved.mcp,
                name=resolved.name,
            )
            cache_dirty = True

        # Rung 3/4: `mcp.expose`, unless `--allow` already overrode it.
        if not allow_matched and not resolved.mcp.expose:
            continue

        pin = _pin_for_registry_workflow(registry_name, entry, workflow_key, info, sha)
        candidates.append(
            _Candidate(
                registry=registry_name,
                workflow=workflow_key,
                description=resolved.description,
                input=resolved.input,
                mcp=resolved.mcp,
                tier=resolved.tier,
                pin=pin,
                name=resolved.name,
            )
        )

    if cache_dirty and sha is not None:
        try:
            save_parsed_tools(registry_name, sha, parsed_cache)
        except OSError as exc:
            logger.debug(
                "Could not persist parse cache for %s@%s: %s", registry_name, sha[:12], exc
            )


def _validated_cached_index(
    meta: Path, registry_name: str, entry: RegistryEntry, sha: str
) -> RegistryIndex | None:
    """Load a cached registry index only if its ``source.json`` metadata
    still matches the current registry (source, type, cache layout) and
    SHA. Repointing a registry name at a different repository must not
    keep serving the previous repository's cached catalogue (NFR1's warm
    cache is only safe when the cache is verified to be *this* source's).
    """
    metadata = registry_cache._read_source_metadata(meta)
    if not registry_cache._metadata_matches(metadata, entry, sha):
        if metadata is not None:
            logger.warning(
                "Cached index for registry %r at %s does not match the configured source; "
                "ignoring the stale cache entry",
                registry_name,
                sha[:12],
            )
        return None
    return registry_cache._load_cached_index(meta)


def _resolve_registry_index(
    registry_name: str, entry: RegistryEntry, *, allow_network: bool
) -> tuple[RegistryIndex, str | None]:
    """Load a registry's index, honoring the E5 offline/warm-cache path
    for GitHub registries. Returns ``(index, sha)`` — ``sha`` is ``None``
    for path registries, which have no ref/SHA concept.

    Reuses ``registry/cache.py``'s E5 primitives directly (its own module
    docstring names the MCP catalogue builder as their intended consumer)
    rather than duplicating the ref-pointer / SHA-keyed cache logic here.
    """
    if entry.type == RegistryType.path:
        return load_index(entry), None

    # NFR1: consult the warm cache first -- network is used only on a cold
    # miss (no recorded ref pointer yet, or the index isn't cached for the
    # pointer's SHA), even when allow_network=True. Resolving the ref
    # online unconditionally would touch the network on every build, warm
    # cache or not.
    if allow_network:
        try:
            offline_sha = registry_cache._resolve_sha_offline(registry_name, None)
        except RegistryError:
            offline_sha = None
        if offline_sha is not None:
            meta = registry_cache._meta_dir(registry_name, offline_sha)
            cached_index = _validated_cached_index(meta, registry_name, entry, offline_sha)
            if cached_index is not None:
                return cached_index, offline_sha

        resolved_ref = resolve_ref(entry, None)
        sha = materialize_to_sha(entry, resolved_ref)
        registry_cache._write_ref_pointer(registry_name, None, sha)
    else:
        sha = registry_cache._resolve_sha_offline(registry_name, None)

    meta = registry_cache._meta_dir(registry_name, sha)
    cached_index = _validated_cached_index(meta, registry_name, entry, sha)
    if cached_index is not None:
        return cached_index, sha

    if not allow_network:
        raise RegistryError(
            f"Registry {registry_name!r} index at {sha[:12]} is not available in the "
            "local cache and network access is not permitted.",
            suggestion=(
                f"Run 'conductor mcp serve' (or 'conductor validate') against registry "
                f"'{registry_name}' with network access once to prime the cache."
            ),
        )

    index = load_index(entry, ref=sha)
    meta.mkdir(parents=True, exist_ok=True)
    registry_cache._write_source_metadata(meta, entry, sha)
    registry_cache._save_cached_index(meta, registry_cache._index_to_yaml(index))
    return index, sha


def _resolve_registry_workflow_schema(
    registry_name: str,
    entry: RegistryEntry,
    workflow_key: str,
    info: WorkflowInfo,
    sha: str | None,
    *,
    parsed_cache: dict[str, ParsedToolInfo],
    allow_network: bool,
    deadline: float,
) -> _ResolvedSchema:
    """Resolve one registry workflow's schema through the three-tier
    ladder. Never raises for an environmental failure (NFR2) — a tier-3
    failure degrades to a permissive schema instead of dropping the
    workflow.

    ``input`` and ``mcp`` are each resolved independently (FR2/DD4): an
    index that declares one but not the other must not let the missing
    field silently default (``mcp`` -> ``expose=True``) or silently
    discard the one it did declare -- only a field the index leaves
    unset falls through to the lower tiers.
    """
    if info.input is not None and info.mcp is not None:
        return _ResolvedSchema(
            description=info.description, input=info.input, mcp=info.mcp, tier="index"
        )

    lower = _resolve_lower_tier_schema(
        registry_name,
        entry,
        workflow_key,
        sha,
        parsed_cache=parsed_cache,
        allow_network=allow_network,
        deadline=deadline,
    )
    return _ResolvedSchema(
        description=info.description if info.input is not None else lower.description,
        input=info.input if info.input is not None else lower.input,
        mcp=info.mcp if info.mcp is not None else lower.mcp,
        tier=lower.tier,
        name=lower.name,
    )


_DEGRADED_DEADLINE_DESCRIPTION = (
    "This workflow's parameters could not be resolved: the catalogue "
    "build's startup deadline was reached before it could be fetched."
)
_DEGRADED_BUDGET_EXCEEDED_DESCRIPTION = (
    "This workflow's parameters could not be resolved: fetching and parsing it "
    "would have exceeded the catalogue build's remaining startup budget."
)


def _run_within_budget[T](func: Callable[[], T], remaining_seconds: float) -> T:
    """Run ``func`` bounded by ``remaining_seconds`` of the startup deadline.

    Fetching a workflow (network I/O) and parsing it (YAML + Jinja
    rendering) are both synchronous, so checking the deadline only
    *before* this call — as the pre-existing "already expired" check
    does — cannot stop a single slow fetch or parse from running past it.
    Running the call on a worker thread and bounding it with
    ``Future.result(timeout=...)`` lets the caller give up, and degrade,
    at the remaining budget instead of waiting for the call to finish.
    The worker thread itself is not forcibly killed (Python has no safe
    way to do that for arbitrary code) and is left to finish in the
    background; the executor is shut down without waiting for it.

    Raises:
        concurrent.futures.TimeoutError: if ``func`` does not complete
            within ``remaining_seconds``.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(func)
    try:
        return future.result(timeout=max(remaining_seconds, 0.0))
    finally:
        executor.shutdown(wait=False)


def _resolve_lower_tier_schema(
    registry_name: str,
    entry: RegistryEntry,
    workflow_key: str,
    sha: str | None,
    *,
    parsed_cache: dict[str, ParsedToolInfo],
    allow_network: bool,
    deadline: float,
) -> _ResolvedSchema:
    """Resolve a workflow's full schema through tiers 2-3, for whichever
    field(s) the index did not declare."""
    # Tier 2: SHA-keyed parse cache. Path registries have no immutable SHA
    # to key a cache entry on, so they never participate in this tier.
    if sha is not None and workflow_key in parsed_cache:
        parsed = parsed_cache[workflow_key]
        return _ResolvedSchema(
            description=parsed.description,
            input=parsed.input,
            mcp=parsed.mcp,
            tier="cache",
            name=parsed.name,
        )

    # Tier 3: fetch and parse, under the startup deadline.
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _ResolvedSchema(
            description=_DEGRADED_DEADLINE_DESCRIPTION,
            input={},
            mcp=McpConfig(),
            tier="degraded",
        )

    def _fetch_and_parse() -> WorkflowConfig:
        ref = sha if entry.type == RegistryType.github else None
        path = fetch_workflow(
            registry_name, entry, workflow_key, ref=ref, allow_network=allow_network
        )
        return load_config(path)

    try:
        config = _run_within_budget(_fetch_and_parse, remaining)
    except concurrent.futures.TimeoutError:
        return _ResolvedSchema(
            description=_DEGRADED_BUDGET_EXCEEDED_DESCRIPTION,
            input={},
            mcp=McpConfig(),
            tier="degraded",
        )
    except (RegistryError, ConfigurationError) as exc:
        return _ResolvedSchema(
            description=f"This workflow's parameters could not be resolved: {exc}",
            input={},
            mcp=McpConfig(),
            tier="degraded",
        )

    return _ResolvedSchema(
        description=config.workflow.description or "",
        input=config.workflow.input,
        mcp=config.workflow.mcp,
        tier="parsed",
        name=config.workflow.name,
    )


def _pin_for_registry_workflow(
    registry_name: str,
    entry: RegistryEntry,
    workflow_key: str,
    info: WorkflowInfo,
    sha: str | None,
) -> Pin:
    """Pin a registry workflow to its immutable identity (DD6): the
    already-resolved commit SHA for GitHub, or a content hash of the YAML
    for a path registry (``version_resolver`` raises for a ref on a path
    registry, so a hash is the only identity available there)."""
    if entry.type == RegistryType.github:
        assert sha is not None, "GitHub registries always resolve a SHA"
        return Pin(kind="sha", value=sha)

    try:
        path = _path_registry_workflow_path(entry, info)
        return pin_content_file(path)
    except (RegistryError, OSError) as exc:
        # The pin exists to detect drift, not to gate exposure (NFR2) -- a
        # workflow whose file cannot even be hashed still gets a
        # deterministic (if unhelpful) pin rather than blocking exposure.
        return pin_content(f"unavailable:{registry_name}/{workflow_key}:{exc}".encode())


def _path_registry_workflow_path(entry: RegistryEntry, info: WorkflowInfo) -> Path:
    """Resolve a path-registry workflow's on-disk path, reusing
    ``registry/cache.py``'s own path-containment helpers so a workflow
    path cannot escape the registry root here either."""
    from conductor.registry.cache import _resolve_within, _safe_repo_path

    root = Path(entry.source)
    safe = _safe_repo_path(info.path)
    return _resolve_within(root, safe)


# ---------------------------------------------------------------------------
# --workflow-dir candidates
# ---------------------------------------------------------------------------


def _collect_workflow_dir_candidates(
    directory: Path, options: ServeOptions, *, deadline: float, candidates: list[_Candidate]
) -> None:
    """Expose every workflow file directly under a ``--workflow-dir``
    (FR2), non-recursively. There is no index or cache for an ad-hoc
    directory, so every file always resolves via tier 3 (fetch-and-parse
    degrades to a plain parse) — a parse failure still exposes the
    workflow with a degraded schema (NFR2), keyed by filename stem since
    that survives even a total parse failure. A successful parse instead
    uses the workflow's own declared ``name:`` (FR3), so the published
    tool name agrees with what ``conductor validate`` reports for the
    same file; the filename stem is the fallback only for a ``degraded``
    entry, where no declared name was ever read.

    The registry label for these is ``dir:<directory name>``, so a
    cross-``--workflow-dir`` (or dir-vs-registry) name collision qualifies
    exactly like a cross-registry one (DD10). ``source`` is set to each
    file's own resolved path so two different directories sharing a
    basename (and a same-named file within them) still produce distinct
    identities instead of one candidate silently overwriting the other.

    Bounded by ``deadline`` (NFR1) — a file reached after the startup
    deadline degrades without being parsed, rather than adding unbounded
    parse time on top of an already-exhausted budget.
    """
    if not directory.is_dir():
        logger.warning("--workflow-dir %s is not a directory; skipping", directory)
        return

    registry_label = f"dir:{directory.name}"
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _WORKFLOW_DIR_EXTENSIONS:
            continue

        workflow_key = path.stem
        survives, allow_matched = _passes_deny_allow(workflow_key, options)
        if not survives:
            continue

        name: str | None = None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            description = (
                "This workflow's parameters could not be resolved: the catalogue "
                "build's startup deadline was reached before it could be parsed."
            )
            input_defs: dict[str, InputDef] = {}
            mcp = McpConfig()
            tier: ResolutionTier = "degraded"
        else:
            try:
                config = _run_within_budget(lambda path=path: load_config(path), remaining)
                description = config.workflow.description or ""
                input_defs = config.workflow.input
                mcp = config.workflow.mcp
                name = config.workflow.name
                tier = "parsed"
            except concurrent.futures.TimeoutError:
                description = (
                    "This workflow's parameters could not be resolved: parsing it would "
                    "have exceeded the catalogue build's remaining startup budget."
                )
                input_defs = {}
                mcp = McpConfig()
                tier = "degraded"
            except ConfigurationError as exc:
                description = f"This workflow's parameters could not be resolved: {exc}"
                input_defs = {}
                mcp = McpConfig()
                tier = "degraded"

        if not allow_matched and not mcp.expose:
            continue

        try:
            pin = pin_content_file(path)
        except OSError as exc:
            pin = pin_content(f"unavailable:{registry_label}/{workflow_key}:{exc}".encode())

        candidates.append(
            _Candidate(
                registry=registry_label,
                workflow=workflow_key,
                description=description,
                input=input_defs,
                mcp=mcp,
                tier=tier,
                pin=pin,
                source=str(path),
                name=name,
            )
        )
