"""Turn ``plugin_sources:`` declarations into resolved marketplaces.

The single composition point between acquisition
(:mod:`conductor.plugins.fetch`) and resolution
(:mod:`conductor.plugins.registry`), analogous to
:func:`conductor.skills.discovery.resolve_effective_skills`. Both
``conductor run`` and ``conductor validate`` come through here, so the
two cannot disagree about what a source resolves to — they differ only in
whether the network is allowed.

The output is a name-keyed table of :class:`~conductor.plugins.marketplace.Marketplace`
objects that ``resolve_plugins`` consults *before* the installed roots.
That ordering is the feature: a declared source shadows an installed
marketplace of the same name, so a workflow that declares its sources
behaves identically on a machine that happens to have the same
marketplace installed at a different version.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from conductor.plugins.errors import PluginSourceError
from conductor.plugins.fetch import fetch_sources
from conductor.plugins.marketplace import Marketplace, read_marketplace
from conductor.plugins.sources import PluginSource, parse_plugin_source

logger = logging.getLogger(__name__)


class SourceEntry(Protocol):
    """The shape :func:`resolve_plugin_sources` needs from a source entry.

    A structural type standing in for
    :class:`~conductor.config.schema.PluginSourceDef`, keeping this
    package below the config layer — the same reason
    :class:`~conductor.plugins.registry.PluginEntry` exists, and a
    ``Protocol`` for the same reason: a plain class would only match by
    inheritance, which the schema model does not have.
    """

    source: str
    path: str | None
    plugin: str | None


@dataclass(frozen=True)
class ResolvedSource:
    """One declared source, resolved to a marketplace on disk."""

    name: str
    """Marketplace name the workflow registered the source under."""

    source: PluginSource
    """The parsed source."""

    marketplace: Marketplace
    """The marketplace read from the resolved checkout."""

    sha: str | None = None
    """Commit SHA of the checkout, or ``None`` for a local source."""

    stale: bool = False
    """Whether a floating ref could not be re-checked and the cached
    checkout was used instead."""

    fetched: bool = False
    """Whether resolving this source performed a clone."""


def _local_root(source: PluginSource, base_dir: Path | None) -> Path:
    """Anchor a local source's path.

    Relative paths resolve against the workflow file's directory,
    matching ``skills:``, ``plugins:`` and ``working_dir``. Normalised
    with ``normpath`` rather than ``resolve()`` so symlink aliases stay
    distinct, for the same reason
    :func:`~conductor.skills.registry.normalize_entry_path` does.

    Raises:
        PluginSourceError: If the path does not exist or is not a
            directory.
    """
    expanded = Path(source.location).expanduser()
    if not expanded.is_absolute():
        anchor = base_dir if base_dir is not None else Path.cwd()
        expanded = anchor / expanded
    resolved = Path(os.path.normpath(expanded))
    try:
        if not resolved.is_dir():
            raise PluginSourceError(
                f"Source {source.display!r} resolved to {resolved}, which is not a "
                "directory. Relative plugin sources resolve against the workflow "
                "file's directory."
            )
    except OSError as exc:
        raise PluginSourceError(
            f"Source {source.display!r} resolved to {resolved}, which could not be read: {exc}"
        ) from exc
    return resolved


def _subdirectory(root: Path, name: str, relative: str | None) -> Path:
    """Apply a source's ``path:`` subdirectory, refusing an escape.

    A ``path`` that climbs out of the checkout would let a source point
    resolution at an arbitrary directory on the machine, so it is
    refused rather than normalised away.

    Raises:
        PluginSourceError: If the subdirectory escapes ``root`` or does
            not exist.
    """
    if not relative:
        return root
    combined = Path(os.path.normpath(root / relative))
    try:
        combined.relative_to(Path(os.path.normpath(root)))
    except ValueError:
        raise PluginSourceError(
            f"Source for marketplace {name!r} sets 'path: {relative}', which escapes "
            "the source directory."
        ) from None
    if not combined.is_dir():
        raise PluginSourceError(
            f"Source for marketplace {name!r} sets 'path: {relative}', which does not "
            f"exist in the source ({combined})."
        )
    return combined


def resolve_plugin_sources(
    sources: Mapping[str, SourceEntry],
    *,
    base_dir: Path | None = None,
    allow_network: bool = True,
    on_warning: Callable[[str], None] | None = None,
) -> dict[str, ResolvedSource]:
    """Resolve every declared source to a marketplace on disk.

    Remote sources are acquired concurrently — a workflow naming three
    marketplaces should pay one ``ls-remote`` round trip in wall-clock
    time, not three.

    Args:
        sources: The ``runtime.plugin_sources`` mapping.
        base_dir: Directory a relative local source resolves against.
        allow_network: When ``False``, resolve from cache only. This is
            what keeps ``conductor validate`` off the network.
        on_warning: Sink for non-fatal diagnostics.

    Returns:
        One :class:`ResolvedSource` per declared name.

    Raises:
        PluginSourceError: If a source is unusable — an unreadable local
            path, a subdirectory that escapes, or a checkout holding
            neither a catalog nor a plugin.
        PluginFetchError: If a remote source cannot be acquired and no
            cached checkout can stand in.
    """
    if not sources:
        return {}

    warn = on_warning if on_warning is not None else (lambda _message: None)
    parsed = {name: parse_plugin_source(entry.source) for name, entry in sources.items()}
    remote = [source for source in parsed.values() if not source.is_local]
    fetched = fetch_sources(remote, allow_network=allow_network, on_warning=warn) if remote else {}

    resolved: dict[str, ResolvedSource] = {}
    for name, entry in sources.items():
        source = parsed[name]
        if source.is_local:
            root, sha, stale, was_fetched = _local_root(source, base_dir), None, False, False
        else:
            result = fetched[source.raw]
            root, sha, stale, was_fetched = result.root, result.sha, result.stale, result.fetched

        marketplace = read_marketplace(
            _subdirectory(root, name, entry.path), name=name, plugin=entry.plugin
        )
        resolved[name] = ResolvedSource(
            name=name,
            source=source,
            marketplace=marketplace,
            sha=sha,
            stale=stale,
            fetched=was_fetched,
        )
    return resolved


def marketplaces_from(resolved: Mapping[str, ResolvedSource]) -> dict[str, Marketplace]:
    """Reduce resolved sources to the table ``resolve_plugins`` consults."""
    return {name: entry.marketplace for name, entry in resolved.items()}


__all__ = [
    "ResolvedSource",
    "SourceEntry",
    "marketplaces_from",
    "resolve_plugin_sources",
]
