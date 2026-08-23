"""Pin every exposed workflow to an immutable identity (DD6, E7-T5).

At catalogue-build time, each exposed workflow is resolved to an identity
that cannot silently change out from under a caller mid-session:

* **GitHub registries** — the already-resolved commit SHA
  (``materialize_to_sha``), offline-resolvable through the E5-T3 ref
  pointer (``registry/cache.py``) when network access is not permitted.
* **Path registries and ``--workflow-dir``** — a content hash of the YAML
  file's bytes. ``version_resolver.materialize_to_sha`` raises for a path
  registry (there is no ref/SHA concept there at all), so a hash is the
  only identity available.

That pin is included in every invocation result and the terminal record
(future epics), and is re-checked on an interval by the functions in this
module — but re-checking **never mutates the live catalogue** (DD6): the
spec forbids a server's tool list from varying within a connection (DD3),
so drift is reported, not applied. A caller decides what to do with a
drifted pin (log it loudly, surface it in a status tool, etc.); this
module only detects it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from conductor.registry.config import RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError
from conductor.registry.version_resolver import materialize_to_sha, resolve_ref


@dataclass(frozen=True)
class Pin:
    """An immutable identity for one exposed workflow.

    ``kind="sha"`` for GitHub registries; ``kind="hash"`` for path
    registries and ``--workflow-dir``. Two pins are equal (and therefore
    "no drift") only when both ``kind`` and ``value`` match — a SHA and a
    content hash are never comparable, so a registry migrating backend
    types always reports as drift rather than a false match.
    """

    kind: Literal["sha", "hash"]
    value: str

    def as_str(self) -> str:
        """Render as ``"sha:<value>"`` / ``"hash:<value>"`` — the form the
        design's API contract shows in a run handle's ``workflow.pinned``
        field."""
        return f"{self.kind}:{self.value}"


def pin_github_registry(
    registry_name: str,
    entry: RegistryEntry,
    ref: str | None = None,
    *,
    allow_network: bool = True,
) -> Pin:
    """Resolve a GitHub-registry workflow's pin: its immutable commit SHA.

    Args:
        registry_name: The configured registry name (used to look up the
            E5-T3 ref pointer when offline).
        entry: The registry's ``RegistryEntry`` (must be ``github``).
        ref: The ref to resolve (``None`` = the registry's default
            branch).
        allow_network: When ``False``, resolves entirely through the
            E5-T3 ref pointer (``registry/cache.py``'s offline SHA
            resolver) — no GitHub API call, satisfying NFR1. When
            ``True``, resolves exactly as a normal fetch would.

    Raises:
        ValueError: if ``entry`` is not a GitHub registry.
        RegistryError: if the ref cannot be resolved (e.g. offline with no
            recorded pointer).
    """
    if entry.type != RegistryType.github:
        raise ValueError(f"pin_github_registry called with a non-GitHub entry: {entry.type!r}")

    if allow_network:
        resolved_ref = resolve_ref(entry, ref)
        sha = materialize_to_sha(entry, resolved_ref)
    else:
        # Reuses the E5-T3 offline SHA resolver rather than duplicating its
        # ref-pointer lookup here -- see registry/cache.py's module
        # docstring, which names the catalogue builder as its intended
        # consumer.
        from conductor.registry import cache as _cache

        sha = _cache._resolve_sha_offline(registry_name, ref)

    return Pin(kind="sha", value=sha)


def pin_content(data: bytes) -> Pin:
    """Content hash (SHA-256) of arbitrary bytes."""
    return Pin(kind="hash", value=hashlib.sha256(data).hexdigest())


def pin_content_file(path: Path) -> Pin:
    """Content hash of a YAML file's bytes — the identity for path
    registries and ``--workflow-dir``.

    Raises:
        OSError: if the file cannot be read. A caller building a catalogue
            entry for a workflow whose file is unreadable (an
            environmental failure like any other schema-resolution miss,
            NFR2) should catch this and substitute a deterministic
            fallback (e.g. a hash of a placeholder string identifying the
            failure) rather than aborting the whole catalogue build.
    """
    return pin_content(path.read_bytes())


@dataclass(frozen=True)
class DriftReport:
    """The result of re-checking one exposed workflow's pin (DD6).

    ``current`` is ``None`` when the re-check itself failed (e.g. the
    registry became unreachable) — that is reported as an error, not as
    drift, since a re-check that could not complete says nothing about
    whether the workflow actually changed.
    """

    registry: str
    workflow: str
    original: Pin
    current: Pin | None
    drifted: bool
    error: str | None = None


def recheck_github_pin(
    registry_name: str,
    workflow: str,
    entry: RegistryEntry,
    ref: str | None,
    original: Pin,
    *,
    allow_network: bool = True,
) -> DriftReport:
    """Re-resolve a GitHub-registry workflow's pin and report drift.

    Never mutates any live catalogue — this only computes and compares;
    the caller decides what, if anything, to do with the result (DD6,
    DD3).
    """
    try:
        current = pin_github_registry(registry_name, entry, ref, allow_network=allow_network)
    except RegistryError as exc:
        return DriftReport(
            registry=registry_name,
            workflow=workflow,
            original=original,
            current=None,
            drifted=False,
            error=str(exc),
        )
    return DriftReport(
        registry=registry_name,
        workflow=workflow,
        original=original,
        current=current,
        drifted=current != original,
    )


def recheck_content_pin(
    registry: str,
    workflow: str,
    path: Path,
    original: Pin,
) -> DriftReport:
    """Re-hash a path-registry / ``--workflow-dir`` workflow and report
    drift. Never mutates any live catalogue (DD6, DD3)."""
    try:
        current = pin_content_file(path)
    except OSError as exc:
        return DriftReport(
            registry=registry,
            workflow=workflow,
            original=original,
            current=None,
            drifted=False,
            error=str(exc),
        )
    return DriftReport(
        registry=registry,
        workflow=workflow,
        original=original,
        current=current,
        drifted=current != original,
    )
