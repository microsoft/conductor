"""Frozen models for run bundles: the manifest, its entries, and the
ephemeral build/validate descriptor.

Two distinct artifacts are defined here:

* **Content zone** — :class:`BundleManifest` plus :class:`BundleEntry`.
  This is the immutable, content-addressed artifact stored in the bundle
  store (``bundle.json``). Its digest (:func:`compute_bundle_digest`)
  covers only entries and the skills/plugins topologies: the same content
  bundled at a different git HEAD, from a different registry resolution
  time, or on a different machine must produce the *same* digest.

* **Provenance and link fields** — :class:`BundleProvenance` and
  :class:`BundleDescriptor`. These describe *how* the bundle was reached
  (git HEAD, registry refs, plugin origins) and *what it links to* (run
  manifest, workflow file, environment identity). They are an ephemeral
  build/validate report and are **never** stored in the content-addressed
  store: reusing someone else's provenance would break the audit link.

All models are frozen (``frozen=True``) and reject unknown keys
(``extra="forbid"``) so a schema drift fails loudly at the boundary instead
of serializing silently into the store.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from conductor.digest import canonical_json, canonical_json_digest

_DIGEST_VERSION = 1
"""Schema version of the bundle digest inputs. ``Literal[1]`` on the models."""


class BundleEntry(BaseModel):
    """One file or symlink inside the bundle, addressed by logical path.

    ``logical_path`` is always in POSIX notation, relative to the bundle
    root, and is never a host-absolute path — bundles are relocatable
    artifacts. ``origin_detail`` is a *logical* identifier of the source
    (e.g. ``include:prompts/review.md`` relative to the bundle root,
    ``plugin:prs``, ``skill:conductor``, ``asset:scripts/x.sh``), never a
    host path: host paths would make the descriptor machine-dependent.

    For a symlink entry, ``digest`` is the SHA-256 of the normalized
    ``link_target`` (UTF-8), ``size`` is ``0``, and ``executable`` is
    ``False`` — a symlink's content is its target string, not file bytes.
    Use :meth:`for_symlink` to construct one; the invariant is enforced
    there and by the model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_path: str
    """POSIX path relative to the bundle root, e.g. ``tree/main/workflow.yaml``."""

    kind: Literal["file", "symlink"]
    """Whether this entry is a regular file or a symbolic link."""

    digest: str
    """``sha256:<hex>`` of the file bytes, or of the symlink's link target."""

    size: int
    """File size in bytes; always ``0`` for symlinks."""

    executable: bool
    """Whether the executable bit is set; always ``False`` for symlinks."""

    link_target: str | None
    """Normalized POSIX linkname, set only for symlinks."""

    origin_kind: Literal[
        "workflow",
        "include",
        "jinja_include",
        "subworkflow",
        "subworkflow_registry",
        "skill",
        "plugin",
        "asset",
    ]
    """Which dependency class this entry was collected from."""

    origin_detail: str
    """Logical identifier of the source — never a host path."""

    @field_validator("logical_path")
    @classmethod
    def _logical_path_must_be_safe_relative(cls, value: str) -> str:
        """Reject anything but a normalized relative POSIX path.

        Bundles are relocatable, so a logical path must stay inside the
        bundle root on every host: no POSIX- or Windows-absolute spelling
        (``/x``, ``C:\\x``, UNC ``\\\\server\\x``), no parent traversal
        (``..``), and no backslashes (logical paths are POSIX-only
        notation). Paths are additionally required to be normalized — no
        empty segments (``a//b``, ``a/``, ``""``) and no dot segments
        (``a/./b``, ``.``) — matching what
        :meth:`pathlib.PurePath.as_posix` produces for relative paths.
        """
        if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
            raise ValueError(
                f"bundle logical_path must be relative to the bundle root, "
                f"got absolute path {value!r}"
            )
        if "\\" in value:
            raise ValueError(
                f"bundle logical_path must use POSIX notation, got backslash in {value!r}"
            )
        if any(segment in ("", ".", "..") for segment in value.split("/")):
            raise ValueError(
                f"bundle logical_path must be a normalized relative POSIX path, got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _link_target_matches_kind(self) -> BundleEntry:
        """``link_target`` is set exactly for symlink entries."""
        if self.kind == "symlink" and self.link_target is None:
            raise ValueError(f"symlink entry {self.logical_path!r} requires a link_target")
        if self.kind == "file" and self.link_target is not None:
            raise ValueError(
                f"file entry {self.logical_path!r} must not carry a link_target "
                f"({self.link_target!r})"
            )
        return self

    @classmethod
    def for_symlink(
        cls,
        logical_path: str,
        link_target: str,
        *,
        origin_kind: Literal[
            "workflow",
            "include",
            "jinja_include",
            "subworkflow",
            "subworkflow_registry",
            "skill",
            "plugin",
            "asset",
        ],
        origin_detail: str,
    ) -> BundleEntry:
        """Construct a symlink entry, enforcing the symlink digest rule.

        A symlink's ``digest`` is ``sha256:<hex>`` of the normalized
        ``link_target`` (UTF-8), its ``size`` is ``0``, and its
        ``executable`` flag is ``False`` — the entry's content is the
        target string, not file bytes.
        """
        return cls(
            logical_path=logical_path,
            kind="symlink",
            digest=f"sha256:{hashlib.sha256(link_target.encode('utf-8')).hexdigest()}",
            size=0,
            executable=False,
            link_target=link_target,
            origin_kind=origin_kind,
            origin_detail=origin_detail,
        )


class GitProvenance(BaseModel):
    """Git state of the checkout the bundle was collected from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    head_sha: str | None
    """Commit the checkout was at, or ``None`` outside a git work tree."""

    remote: str | None
    """URL of ``origin``, or ``None`` when no remote is configured."""

    dirty: list[str]
    """Logical paths that are modified in the work tree **and** bundled —
    dirty files that are not part of the bundle do not matter to it."""


class RegistryProvenance(BaseModel):
    """One workflow registry reference the bundle resolved through."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str
    """The authored reference (``name@registry#version`` form or shorthand)."""

    resolved_sha: str
    """Commit SHA the reference resolved to.

    Deliberately carries no ``resolved_at``: a timestamp would make the
    descriptor (and any serialized copy of it) drift with wall clock."""


class PluginProvenance(BaseModel):
    """One plugin whose content is bundled."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    """Plugin name as authored in ``plugins:``."""

    flavor: Literal["copilot", "claude"]
    """Which plugin manifest convention the plugin was parsed as."""

    origin: Literal["cache", "installed", "path"]
    """Where the plugin content came from: the git-backed source cache, an
    installed CLI marketplace tree, or an explicit local path."""

    sha: str | None
    """Commit SHA for ``cache``-origin plugins, else ``None``."""


class BundleProvenance(BaseModel):
    """How the bundle was reached — never an input to the bundle digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    git: GitProvenance | None
    """Git state of the root checkout, or ``None`` outside a work tree."""

    registry: list[RegistryProvenance]
    """Registry references resolved during collection."""

    plugins: list[PluginProvenance]
    """Plugins bundled during collection."""


class BundleManifest(BaseModel):
    """The immutable, content-addressed bundle artifact (content zone only).

    This is what the bundle store persists as ``bundle.json``. It carries
    no provenance, no link fields, no timestamps, and no Conductor
    version: the manifest must digest identically for identical content
    regardless of when, where, or by which Conductor build it was
    collected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    """Manifest schema version."""

    bundle_digest: str
    """``sha256:<hex>`` over the canonical digest inputs — see
    :func:`compute_bundle_digest`."""

    entries: tuple[BundleEntry, ...]
    """Bundled files and symlinks, keyed by ``logical_path``."""

    skills_topology: dict[str, str]
    """Skill name → logical path of its ``SKILL.md`` inside the bundle."""

    plugins_topology: dict[str, str]
    """Plugin name → logical path of its manifest inside the bundle."""


class BundleEnvironmentLink(BaseModel):
    """Identity link to the execution environment the bundle was built for.

    Mirrors the identity/digest shape of
    ``config.environment.ResolvedEnvironment`` (name, source, digest of the
    resolved document) without linking the parsed document itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    """Environment name: file stem, or ``"local/default"`` for the built-in."""

    source: str
    """Which level produced the document (``builtin``/``path``/``project``/``user``)."""

    digest: str
    """``sha256:<hex>`` of the canonical JSON serialization of the document."""


class BundleDescriptor(BaseModel):
    """Ephemeral build/validate report for one bundle — never stored in CAS.

    Carries the digest plus the *link* fields (run manifest, workflow
    file, environment identity) and the provenance of how the bundle was
    reached. None of these fields are inputs to ``bundle_digest``: two
    descriptors over identical content with different git HEADs or
    different link targets agree on the digest and disagree only here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_digest: str
    """Content digest of the bundle — identical inputs give identical digests."""

    run_manifest_digest: str | None
    """Digest of the compiled run manifest, when one was compiled."""

    workflow_digest: str | None
    """Digest of the root workflow file's bytes, when there is one on disk."""

    environment: BundleEnvironmentLink | None
    """Identity of the execution environment, when one was resolved."""

    provenance: BundleProvenance
    """How the bundle was reached — not part of the content digest."""

    incomplete: list[str]
    """Logical names of dependencies that could not be fully bundled
    (e.g. unfetched plugin sources) — the digest must not be reported for
    an incomplete closure."""

    warnings: list[str]
    """Non-fatal diagnostics collected during the build."""


def compute_bundle_digest(
    entries: Iterable[BundleEntry],
    skills_topology: Mapping[str, str],
    plugins_topology: Mapping[str, str],
) -> str:
    """Content digest of a bundle: version, entries, and topologies only.

    Inputs: ``{version: 1, entries: [...] sorted by ``logical_path``,
    skills_topology sorted, plugins_topology sorted}``. Provenance
    (git/registry/plugin origins) and descriptor link fields are
    deliberately **not** inputs — the invariant is that identical content
    digests identically no matter when, where, or how it was collected.
    """
    ordered_entries = sorted(entries, key=lambda entry: entry.logical_path)
    payload = {
        "version": _DIGEST_VERSION,
        "entries": [entry.model_dump(mode="json") for entry in ordered_entries],
        "skills_topology": dict(sorted(skills_topology.items())),
        "plugins_topology": dict(sorted(plugins_topology.items())),
    }
    return canonical_json_digest(payload)


def serialize_manifest(manifest: BundleManifest) -> str:
    """Serialize a manifest as canonical JSON — the bytes stored as ``bundle.json``.

    Delegates to :func:`conductor.digest.canonical_json`, the single
    canonical serializer, so the stored manifest and every digest over it
    agree byte-for-byte; re-serializing an equal manifest yields identical
    text on every host.
    """
    return canonical_json(manifest.model_dump(mode="json"))
