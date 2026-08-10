"""Read a marketplace *catalog* and locate the plugins it lists.

A git source is one of two shapes, and both exist in the wild:

* a **catalog** — a repository holding many plugins, with a
  ``marketplace.json`` naming each one and where it lives;
* a **single plugin** — a repository that *is* one plugin, with a
  ``plugin.json`` at its root.

Detection is automatic, with an explicit ``plugin:`` key to settle the
case where a repository is both.

Two catalog conventions are recognised, matching the two plugin-manifest
conventions in :mod:`conductor.plugins.manifest`. They are not
interchangeable, and the difference is not cosmetic — verified against a
real marketplace repository that ships both:

======================================  =============  =====================
Manifest                                ``pluginRoot`` per-plugin ``source``
======================================  =============  =====================
``.claude-plugin/marketplace.json``     ``./dist/claude``  ``./dist/claude/ado``
``.github/plugin/marketplace.json``     ``./dist/copilot`` ``./ado``
======================================  =============  =====================

The first anchors ``source`` at the repository root; the second anchors it
at ``pluginRoot``. Assuming either one strands every plugin published
under the other, so both anchors are tried and whichever holds a plugin
manifest wins.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from conductor.plugins.errors import PluginNotFoundError, PluginSourceError
from conductor.plugins.manifest import find_manifest, is_plugin_root

# Catalog manifest locations, in probe order — the same directories the
# plugin manifests live in, so a repository is examined once.
MARKETPLACE_MANIFESTS: tuple[Path, ...] = (
    Path(".claude-plugin") / "marketplace.json",
    Path(".github") / "plugin" / "marketplace.json",
)


@dataclass(frozen=True)
class Marketplace:
    """A resolved marketplace — a name-keyed table of plugin roots."""

    name: str
    """Marketplace name as declared in the catalog, or the key the
    workflow registered the source under for a single-plugin source."""

    root: Path
    """Directory the marketplace was read from.

    Its identity, and the reason it is carried rather than derived: two
    marketplaces can register the same *name* from different checkouts,
    so ``AgentExecutor``'s plugin cache keys on this alongside the name.
    Keying on names alone would let a differently-sourced ``prs@acme``
    hit another table's cached answer.
    """

    plugins: dict[str, Path]
    """Plugin name to absolute plugin root, for every plugin it lists."""

    is_catalog: bool
    """Whether this came from a catalog manifest rather than a lone plugin.

    Provenance for reporting, not a control-flow flag: nothing branches on
    it to decide behaviour, and a non-catalog marketplace always holds
    exactly one plugin, so it cannot stand in for "is this empty".
    """

    def resolve(self, plugin: str) -> Path:
        """Return the root of ``plugin``, or raise naming what is available.

        Raises:
            PluginNotFoundError: If the marketplace lists no such plugin.
        """
        root = self.plugins.get(plugin)
        if root is not None:
            return root
        available = ", ".join(sorted(self.plugins)) or "none"
        raise PluginNotFoundError(
            f"Marketplace {self.name!r} does not ship a plugin named {plugin!r}. "
            f"It provides: {available}."
        )


def find_marketplace_manifest(root: Path) -> Path | None:
    """Return the catalog manifest inside ``root``, if there is one."""
    for relative in MARKETPLACE_MANIFESTS:
        candidate = root / relative
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _load_json(path: Path) -> dict[str, Any]:
    """Read a manifest file and return its top-level mapping.

    Raises:
        PluginSourceError: If it cannot be read or is not a JSON object.
    """
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PluginSourceError(f"Marketplace manifest at {path} could not be read: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PluginSourceError(
            f"Marketplace manifest at {path} is not a JSON object "
            f"(parsed as {type(parsed).__name__})."
        )
    return parsed


def _contained(root: Path, candidate: Path) -> Path | None:
    """Return ``candidate`` normalised, or ``None`` if it escapes ``root``.

    A catalog manifest is fetched content, not something the workflow
    author wrote, so a ``source`` of ``../../..`` must not be able to
    point resolution at an arbitrary directory on the machine. Normalised
    with ``os.path.normpath`` semantics rather than ``resolve()`` so a
    symlinked checkout is not silently rewritten to its target — the same
    choice :func:`~conductor.skills.registry.normalize_entry_path` makes.
    """
    combined = Path(os.path.normpath(root / candidate))
    try:
        combined.relative_to(Path(os.path.normpath(root)))
    except ValueError:
        return None
    return combined


def _plugin_root_for(root: Path, base: Path, source: str) -> Path | None:
    """Resolve one catalog entry's ``source`` to a plugin root.

    Tries the repository root first, then ``pluginRoot``, and accepts the
    first that actually holds a plugin manifest. Neither anchor is
    "correct" — the two conventions genuinely disagree, so the filesystem
    settles it.
    """
    relative = PurePosixPath(source.strip())
    for anchor in (root, base):
        candidate = _contained(anchor, Path(*relative.parts))
        if candidate is not None and is_plugin_root(candidate):
            return candidate
    return None


def _read_catalog(manifest: Path, root: Path) -> Marketplace:
    """Parse a catalog manifest into a name-keyed table of plugin roots.

    Entries that do not resolve to a plugin root are skipped rather than
    fatal: a marketplace commonly publishes for several CLIs from one
    repository, and one unbuilt variant must not make every other plugin
    in the catalog unreachable. A miss surfaces when the workflow asks
    for that specific plugin, via :meth:`Marketplace.resolve`.

    Raises:
        PluginSourceError: If the manifest is unreadable, nameless, or
            lists no plugins at all.
    """
    parsed = _load_json(manifest)
    name = parsed.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PluginSourceError(f"Marketplace manifest at {manifest} declares no usable 'name'.")

    metadata = parsed.get("metadata")
    plugin_root = metadata.get("pluginRoot") if isinstance(metadata, dict) else None
    base = root
    if isinstance(plugin_root, str) and plugin_root.strip():
        contained = _contained(root, Path(*PurePosixPath(plugin_root.strip()).parts))
        if contained is not None:
            base = contained

    listed = parsed.get("plugins")
    if not isinstance(listed, list):
        raise PluginSourceError(f"Marketplace manifest at {manifest} declares no 'plugins' list.")

    plugins: dict[str, Path] = {}
    for entry in listed:
        if not isinstance(entry, dict):
            continue
        plugin_name = entry.get("name")
        source = entry.get("source")
        if not isinstance(plugin_name, str) or not plugin_name.strip():
            continue
        # A catalog may also express `source` as an object (a nested git
        # remote). Conductor resolves plugins from the checkout it already
        # has, so only the in-repo string form is usable here.
        if not isinstance(source, str) or not source.strip():
            continue
        resolved = _plugin_root_for(root, base, source)
        if resolved is not None:
            plugins[plugin_name.strip()] = resolved

    return Marketplace(name=name.strip(), root=root, plugins=plugins, is_catalog=True)


def read_marketplace(root: Path, *, name: str, plugin: str | None = None) -> Marketplace:
    """Read the marketplace rooted at ``root``.

    Args:
        root: Directory the source was fetched or pointed at.
        name: Name the workflow registered this source under, used when
            the source is a single plugin rather than a catalog.
        plugin: Explicit disambiguator from ``plugin_sources``. Names
            which plugin to use when ``root`` holds both a catalog and a
            plugin manifest, and narrows a catalog to a single entry
            otherwise. Both candidates are consulted, so the key can name
            either the root plugin or any plugin the catalog lists — the
            ambiguity error recommends this key, and a remedy that worked
            in only one of its two directions would be worse than none.

    Returns:
        The resolved marketplace.

    Raises:
        PluginSourceError: If ``root`` holds neither a catalog nor a
            plugin manifest, if it holds both and no ``plugin:`` key
            settles it, or if a catalog manifest is unusable.
    """
    catalog = find_marketplace_manifest(root)
    single = find_manifest(root)

    if catalog is not None and single is not None and plugin is None:
        raise PluginSourceError(
            f"Source for marketplace {name!r} at {root} holds both a marketplace "
            f"manifest ({catalog.name}) and a plugin manifest ({single.name}), so it "
            "is both a catalog and a plugin. Add a 'plugin:' key naming which one to "
            "use, or point 'path:' at the subdirectory you meant."
        )

    if catalog is not None and plugin is None:
        return _read_catalog(catalog, root)

    if single is not None:
        # A single-plugin repository. Its manifest name is the plugin's
        # own; the marketplace is keyed by the workflow's chosen name, so
        # `plugins: [thing@acme]` works whether or not the repository
        # happens to call itself `thing`.
        from conductor.plugins.manifest import read_manifest_name

        declared = read_manifest_name(single)
        if plugin is None or plugin == declared:
            return Marketplace(name=name, root=root, plugins={declared: root}, is_catalog=False)
        if catalog is None:
            raise PluginSourceError(
                f"Source for marketplace {name!r} at {root} sets 'plugin: {plugin}' but "
                f"the plugin there is named {declared!r}."
            )
        # `plugin:` names neither the root plugin nor nothing — fall through
        # to the catalog, which is the other thing this repository is. Without
        # this, the root plugin's name is the only value the key can ever take,
        # and the ambiguity error above sends every other user into a message
        # claiming the catalog does not list a plugin it visibly does.

    if catalog is not None:
        # A catalog, with `plugin:` narrowing it to one entry. Narrowing
        # rather than ignoring the key: it is the disambiguator, and a
        # catalog that no longer ships the named plugin should say so.
        resolved = _read_catalog(catalog, root)
        assert plugin is not None
        return Marketplace(
            name=resolved.name,
            root=root,
            plugins={plugin: resolved.resolve(plugin)},
            is_catalog=True,
        )

    conventions = ", ".join(str(candidate) for candidate in MARKETPLACE_MANIFESTS)
    raise PluginSourceError(
        f"Source for marketplace {name!r} resolved to {root}, which is neither a "
        f"marketplace nor a plugin: it contains none of {conventions}, and no plugin "
        "manifest. Point 'source:' at a marketplace repository, or 'path:' at the "
        "subdirectory holding the plugin."
    )


__all__ = [
    "Marketplace",
    "find_marketplace_manifest",
    "read_marketplace",
]
