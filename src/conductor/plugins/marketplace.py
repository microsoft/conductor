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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from conductor.plugins.errors import PluginNotFoundError, PluginSourceError
from conductor.plugins.manifest import PluginFlavor, find_manifest, is_plugin_root

# Catalog manifest locations, paired with the flavor each convention
# identifies — mirroring :data:`conductor.plugins.manifest.MANIFEST_FLAVORS`,
# since a catalog convention and its plugin-manifest convention always
# travel together. In probe order: the same directories the plugin
# manifests live in, so a repository is examined once.
MARKETPLACE_FLAVORS: tuple[tuple[Path, PluginFlavor], ...] = (
    (Path(".claude-plugin") / "marketplace.json", "claude"),
    (Path(".github") / "plugin" / "marketplace.json", "copilot"),
)

MARKETPLACE_MANIFESTS: tuple[Path, ...] = tuple(relative for relative, _ in MARKETPLACE_FLAVORS)


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
    """Plugin name to absolute plugin root, for every plugin it lists.

    Populated from whichever catalog convention is found first (Claude,
    then Copilot) when both exist — unchanged from before flavor
    selection existed, so ``len(marketplace.plugins)`` (``cli/plugin.py``'s
    reported count) does not shift for a repository that ships only one
    convention, which is every one observed except the dual-catalog case
    :attr:`flavored` exists for.
    """

    is_catalog: bool
    """Whether this came from a catalog manifest rather than a lone plugin.

    Provenance for reporting, not a control-flow flag: nothing branches on
    it to decide behaviour, and a non-catalog marketplace always holds
    exactly one plugin, so it cannot stand in for "is this empty".
    """

    flavored: dict[PluginFlavor, dict[str, Path]] = field(default_factory=dict)
    """Per-flavor plugin tables, populated only for the catalog
    convention(s) actually present under :attr:`root`.

    A dual-catalog repository — verified against a real marketplace that
    ships both — holds a genuinely different table per flavor: the same
    plugin name resolves to a different directory (the Claude build vs.
    the Copilot build) depending which catalog is consulted. A
    single-catalog repository populates exactly one key here, identical
    to :attr:`plugins`. Empty for a single-plugin (non-catalog) source,
    where there is no flavor choice to make.
    """

    def resolve(
        self,
        plugin: str,
        *,
        flavor: PluginFlavor | None = None,
        on_warning: Callable[[str], None] | None = None,
    ) -> Path:
        """Return the root of ``plugin``, or raise naming what is available.

        Args:
            plugin: Plugin name to look up.
            flavor: Prefer this flavor's table when this marketplace
                carries more than one (a dual-catalog repository). A
                marketplace with only one build present has nothing to
                choose between, so this only ever changes the answer for
                the repository :attr:`flavored`'s docstring describes.
            on_warning: Sink for a non-fatal notice when this marketplace
                genuinely offers more than one build but ``flavor``'s own
                table does not list ``plugin`` — the unflavored
                :attr:`plugins` table is used instead, and the caller is
                told which flavor it fell back from, since that plugin
                may not actually be the build it asked for. Never fires
                for a single-build marketplace: there, the unflavored
                table already *is* the only build there is, so using it
                is not a fallback.

        Raises:
            PluginNotFoundError: If neither the preferred flavor's table
                nor the unflavored one lists ``plugin``.
        """
        is_multi_flavor = len(self.flavored) > 1
        if flavor is not None:
            flavored_table = self.flavored.get(flavor)
            if flavored_table is not None and plugin in flavored_table:
                return flavored_table[plugin]
            if on_warning is not None and is_multi_flavor:
                on_warning(
                    f"Marketplace {self.name!r} has no {flavor!r}-flavored build of "
                    f"{plugin!r}; resolving it from the default table instead."
                )
        root = self.plugins.get(plugin)
        if root is not None:
            return root
        available = ", ".join(sorted(self.plugins)) or "none"
        raise PluginNotFoundError(
            f"Marketplace {self.name!r} does not ship a plugin named {plugin!r}. "
            f"It provides: {available}."
        )


def _marketplace_probe_order(prefer: PluginFlavor | None) -> tuple[Path, ...]:
    """Order the catalog probe, optionally favouring one flavor.

    Mirrors :func:`conductor.plugins.manifest._probe_order` exactly, for
    the same reason: ``prefer=None`` keeps the historical Claude-first
    order every existing caller relies on.
    """
    if prefer is None:
        return MARKETPLACE_MANIFESTS
    preferred = tuple(relative for relative, flavor in MARKETPLACE_FLAVORS if flavor == prefer)
    rest = tuple(relative for relative, flavor in MARKETPLACE_FLAVORS if flavor != prefer)
    return preferred + rest


def find_marketplace_manifest(root: Path, *, prefer: PluginFlavor | None = None) -> Path | None:
    """Return the catalog manifest inside ``root``, if there is one.

    Args:
        root: Candidate marketplace root.
        prefer: Flavor to probe for first. ``None`` keeps the historical
            Claude-first probe order.
    """
    for relative in _marketplace_probe_order(prefer):
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


def _find_all_marketplace_manifests(root: Path) -> list[tuple[Path, PluginFlavor]]:
    """Every catalog convention actually present under ``root``, in probe order.

    Unlike :func:`find_marketplace_manifest`, which stops at the first
    match, this is what lets :func:`read_marketplace` build a genuinely
    different table per flavor for the dual-catalog case — a repository
    that ships both conventions has two catalogs to read, not one.
    """
    found: list[tuple[Path, PluginFlavor]] = []
    for relative, flavor in MARKETPLACE_FLAVORS:
        candidate = root / relative
        try:
            if candidate.is_file():
                found.append((candidate, flavor))
        except OSError:
            continue
    return found


def _read_all_catalogs(catalogs: list[tuple[Path, PluginFlavor]], root: Path) -> Marketplace:
    """Read every present catalog convention into one :class:`Marketplace`.

    ``plugins`` mirrors pre-flavor behaviour exactly — it is always the
    *first* convention found in probe order (Claude, then Copilot), so a
    caller that never asks for a flavor sees the identical table it did
    before flavor selection existed (``cli/plugin.py``'s reported plugin
    count in particular). ``flavored`` additionally carries one table per
    convention actually present.

    Args:
        catalogs: Non-empty list of ``(manifest_path, flavor)`` pairs, as
            returned by :func:`_find_all_marketplace_manifests`.
        root: The marketplace root the manifests were found under.
    """
    tables: dict[PluginFlavor, Marketplace] = {
        flavor: _read_catalog(manifest, root) for manifest, flavor in catalogs
    }
    primary = tables[catalogs[0][1]]
    return Marketplace(
        name=primary.name,
        root=root,
        plugins=primary.plugins,
        is_catalog=True,
        flavored={flavor: table.plugins for flavor, table in tables.items()},
    )


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
        The resolved marketplace, with :attr:`Marketplace.flavored`
        populated for every catalog convention ``root`` actually holds.

    Raises:
        PluginSourceError: If ``root`` holds neither a catalog nor a
            plugin manifest, if it holds both and no ``plugin:`` key
            settles it, or if a catalog manifest is unusable.
    """
    catalogs = _find_all_marketplace_manifests(root)
    catalog = catalogs[0][0] if catalogs else None
    single = find_manifest(root)

    if catalog is not None and single is not None and plugin is None:
        raise PluginSourceError(
            f"Source for marketplace {name!r} at {root} holds both a marketplace "
            f"manifest ({catalog.name}) and a plugin manifest ({single.name}), so it "
            "is both a catalog and a plugin. Add a 'plugin:' key naming which one to "
            "use, or point 'path:' at the subdirectory you meant."
        )

    if catalog is not None and plugin is None:
        return _read_all_catalogs(catalogs, root)

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
        resolved = _read_all_catalogs(catalogs, root)
        assert plugin is not None
        narrowed_flavored: dict[PluginFlavor, dict[str, Path]] = {
            flavor: {plugin: table[plugin]}
            for flavor, table in resolved.flavored.items()
            if plugin in table
        }
        return Marketplace(
            name=resolved.name,
            root=root,
            plugins={plugin: resolved.resolve(plugin)},
            is_catalog=True,
            flavored=narrowed_flavored,
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
