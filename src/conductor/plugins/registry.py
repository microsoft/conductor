"""Resolve ``plugins:`` entries to on-disk plugins and their components.

An entry is either an **installed plugin name** or a **filesystem path**,
classified syntactically by
:func:`~conductor.skills.registry.is_path_entry` — the same rule
``skills:`` uses, so a bare name is never shadowed by a same-named local
directory and classification never depends on what happens to exist.

Resolving a name reads the same installed-plugin roots that skill
discovery used to scan, but the two are opposites and the distinction is
the point of issue #378. Discovery is *ambient*: whatever is installed
enters the run, and a missing plugin is silently less capability.
Resolution is *named*: the author wrote it down, nothing enters unasked,
and a missing plugin is a hard error at ``conductor validate``.

Each resolved plugin is split into the components Conductor can register
individually — skills, subagents, MCP servers — so the per-component
tri-state on ``plugins:`` means something. See :mod:`conductor.plugins`
for why handing the SDK a plugin root instead would not.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from conductor.plugins.agents import AGENT_SUFFIX, PluginAgent, read_plugin_agents
from conductor.plugins.errors import (
    PluginManifestError,
    PluginNotFoundError,
)
from conductor.plugins.manifest import (
    PLUGIN_AGENTS_DIR,
    PLUGIN_DROPPED_DIRS,
    PLUGIN_MANIFESTS,
    PLUGIN_SKILLS_DIR,
    find_manifest,
    read_plugin_manifest,
)
from conductor.skills.registry import (
    ResolvedSkill,
    WarningSink,
    expand_skills_root,
    is_path_entry,
)

logger = logging.getLogger(__name__)

# Home-relative glob patterns matching an installed plugin root. The
# ``*`` is the marketplace directory — plugins live at
# ``<marketplace>/<plugin>/``, so globbing one level fewer finds nothing.
#
# Both CLIs' locations are searched, for the same reason discovery unions
# them: a workflow must resolve the same plugin whichever provider each
# of its agents happens to use, or one run would see two different sets.
_INSTALLED_PLUGIN_GLOBS: tuple[str, ...] = (
    ".copilot/installed-plugins/*/{name}",
    ".claude/plugins/*/{name}",
)


class PluginEntry(Protocol):
    """The shape :func:`resolve_plugins` needs from a ``plugins:`` entry.

    A structural type rather than a hard dependency on
    :class:`~conductor.config.schema.PluginDef`, so this package stays
    below the config layer — but a *typed* one rather than ``Any``.
    ``getattr(entry, "name", entry)`` looked equivalent and was not:
    :class:`pathlib.Path` has a ``.name`` attribute, so passing one
    silently reduced a path entry to its basename and reclassified it as
    an installed-plugin name, resolving a different plugin on disk
    without a word. A ``Path`` fails this protocol instead.
    """

    name: str
    skills: bool
    agents: bool
    mcp: bool


@dataclass(frozen=True)
class ResolvedPlugin:
    """A plugin resolved from one ``plugins:`` entry, split by component."""

    name: str
    """Plugin name as declared in its manifest."""

    root: Path
    """Absolute path to the plugin root."""

    source: str
    """The ``plugins:`` entry verbatim, so messages can name what the
    author actually wrote rather than a path they never typed."""

    skills: tuple[ResolvedSkill, ...] = ()
    """Skills the plugin ships, already expanded and frontmatter-checked.

    Empty when the plugin ships none, or when the entry disabled them.
    These join the same ``skill_directories`` channel that ``skills:``
    entries use, so a plugin skill and a declared skill are indistinguishable
    downstream.
    """

    agents: tuple[PluginAgent, ...] = ()
    """Subagents the plugin ships, empty when none or when disabled."""

    mcp_servers: dict[str, Any] = field(default_factory=dict)
    """MCP servers the plugin declares, empty when none or when disabled.

    Keyed by the plugin's own server names — see
    :attr:`~conductor.plugins.manifest.PluginManifest.mcp_servers` for
    why they are not rewritten.
    """

    dropped: tuple[str, ...] = ()
    """Component directories present in the plugin that Conductor does
    not load — ``hooks`` and ``commands``.

    Reported rather than ignored. Hooks are arbitrary shell run on tool
    events, neither SDK exposes a per-hook filter, and nothing in
    Conductor's model corresponds to them; dropping them silently would
    reproduce, for the most dangerous component, exactly the invisible
    divergence this feature exists to remove.
    """

    disabled: tuple[str, ...] = ()
    """Components the plugin ships that the entry switched off, for
    reporting at ``conductor validate`` time."""

    def __post_init__(self) -> None:
        """Enforce that ``root`` is absolute.

        Without this the invariant held only by accident, via
        :class:`~conductor.skills.registry.ResolvedSkill`'s own check —
        so a relative plugin root surfaced as a *skill* error in a class
        the plugin layer's callers are not required to catch, and with
        ``skills: false`` produced no error at all.

        Raises:
            PluginManifestError: If ``root`` is not absolute.
        """
        if not self.root.is_absolute():
            raise PluginManifestError(f"ResolvedPlugin.root must be absolute, got {self.root!s}")


def _installed_roots(name: str, home: Path) -> list[Path]:
    """Find installed plugin roots matching ``name``.

    Args:
        name: Bare plugin name as written in ``plugins:``.
        home: Home directory to resolve the globs against.

    Returns:
        Every matching directory that holds a recognised plugin manifest,
        sorted and deduplicated. A match without a manifest is not a
        plugin and is skipped.
    """
    found: list[Path] = []
    seen: set[Path] = set()
    for pattern in _INSTALLED_PLUGIN_GLOBS:
        try:
            matches = sorted(home.glob(pattern.format(name=name)))
        except OSError as exc:
            logger.debug("Plugin glob %r under %s failed: %s", pattern, home, exc)
            continue
        for match in matches:
            try:
                if not match.is_dir() or find_manifest(match) is None:
                    continue
            except OSError:
                continue
            if match not in seen:
                seen.add(match)
                found.append(match)
    return found


def _search_locations(home: Path) -> str:
    """Human-readable list of where a bare name is looked for."""
    return ", ".join(
        str(home / pattern.format(name="<name>")) for pattern in _INSTALLED_PLUGIN_GLOBS
    )


def _resolve_name_entry(entry: str, home: Path) -> Path:
    """Resolve a bare plugin name to exactly one installed root.

    Raises:
        PluginNotFoundError: If no installed plugin has that name, or if
            more than one does. Ambiguity is refused rather than resolved
            by an arbitrary rule: two marketplaces shipping a ``git``
            plugin are genuinely different plugins, and picking one
            silently is how a workflow behaves differently per machine.
    """
    roots = _installed_roots(entry, home)
    if not roots:
        raise PluginNotFoundError(
            f"Plugin {entry!r} is not installed. Looked in {_search_locations(home)}. "
            "Install it with your agent CLI, or point at it with a path "
            "(e.g. './tools/my-plugin')."
        )
    if len(roots) > 1:
        listed = ", ".join(str(root) for root in roots)
        raise PluginNotFoundError(
            f"Plugin {entry!r} is ambiguous — {len(roots)} installed plugins share "
            f"that name: {listed}. Reference the one you want by path."
        )
    return roots[0]


def _resolve_path_entry(entry: str, base_dir: Path | None) -> Path:
    """Resolve a path entry to a plugin root.

    Relative paths resolve against the workflow file's directory,
    mirroring ``skills:`` and ``working_dir``. ``normpath`` rather than
    ``resolve()``, so symlink aliases stay distinct.

    Raises:
        PluginNotFoundError: If the path does not exist, is not a
            directory, or cannot be read.
        PluginManifestError: If it exists but holds no plugin manifest.
    """
    path = Path(entry).expanduser()
    if not path.is_absolute():
        path = (base_dir if base_dir is not None else Path.cwd()) / path
    resolved = Path(os.path.normpath(path))

    try:
        if not resolved.exists():
            raise PluginNotFoundError(
                f"Plugin path {entry!r} resolved to {resolved!s}, which does not "
                "exist. Relative plugin paths resolve against the workflow file's "
                "directory."
            )
        if not resolved.is_dir():
            raise PluginNotFoundError(
                f"Plugin path {entry!r} resolved to {resolved!s}, which is not a "
                "directory. Point it at a plugin root."
            )
    except OSError as exc:
        raise PluginNotFoundError(
            f"Plugin path {entry!r} resolved to {resolved!s}, which could not be read: {exc}"
        ) from exc

    if find_manifest(resolved) is None:
        conventions = ", ".join(str(candidate) for candidate in PLUGIN_MANIFESTS)
        raise PluginManifestError(
            f"Plugin path {entry!r} resolved to {resolved!s}, which is not a plugin: "
            f"it contains none of {conventions}."
        )
    return resolved


def _plugin_skills(root: Path, source: str, on_warning: WarningSink | None) -> list[ResolvedSkill]:
    """Expand a plugin's ``skills/`` directory.

    Reuses :func:`~conductor.skills.registry.expand_skills_root` so a
    plugin skill and a path-declared skill are judged by identical rules.
    Each is frontmatter-checked here rather than only at validate time,
    because ``conductor run`` never calls the static validator and both
    downstream CLIs skip an unparseable skill in silence.

    Raises:
        PluginManifestError: If ``skills/`` cannot be listed.
        SkillManifestError: If a skill's ``SKILL.md`` is missing,
            unparseable, or incomplete.
    """
    from conductor.skills.frontmatter import read_skill_frontmatter

    skills_dir = root / PLUGIN_SKILLS_DIR
    try:
        if not skills_dir.is_dir():
            return []
        children, skipped, unreadable = expand_skills_root(skills_dir)
    except OSError as exc:
        raise PluginManifestError(
            f"Plugin skills directory {skills_dir} could not be read: {exc}"
        ) from exc

    if unreadable:
        raise PluginManifestError(
            f"Plugin skills directory {skills_dir} has "
            f"subdirector{'y' if len(unreadable) == 1 else 'ies'} {unreadable!r} that "
            "could not be read. Fix the permissions, or remove them."
        )
    if skipped and on_warning is not None:
        on_warning(
            f"Plugin {source!r} skipped {len(skipped)} "
            f"subdirector{'y' if len(skipped) == 1 else 'ies'} of {skills_dir} with "
            f"no SKILL.md: {skipped!r}."
        )

    resolved: list[ResolvedSkill] = []
    for directory in children:
        read_skill_frontmatter(directory)
        resolved.append(ResolvedSkill(name=directory.name, directory=directory, source=source))
    return resolved


def _has_agent_definitions(root: Path) -> bool:
    """Whether a plugin ships any ``*.agent.md``, without parsing them.

    Used only to decide whether switching agents off was a real omission
    worth reporting. Deliberately does not call
    :func:`~conductor.plugins.agents.read_plugin_agents`: that raises on a
    malformed definition, which would make ``agents: false`` fail over the
    very files it opted out of.
    """
    agents_dir = root / PLUGIN_AGENTS_DIR
    try:
        if not agents_dir.is_dir():
            return False
        return any(entry.name.endswith(AGENT_SUFFIX) for entry in agents_dir.iterdir())
    except OSError:
        return False


def _dropped_components(root: Path) -> tuple[str, ...]:
    """Which unsupported component directories the plugin ships."""
    present: list[str] = []
    for name in PLUGIN_DROPPED_DIRS:
        try:
            if (root / name).is_dir():
                present.append(name)
        except OSError:
            continue
    return tuple(present)


def resolve_plugin(
    entry: str,
    *,
    want_skills: bool = True,
    want_agents: bool = True,
    want_mcp: bool = True,
    base_dir: Path | None = None,
    home: Path | None = None,
    on_warning: WarningSink | None = None,
) -> ResolvedPlugin:
    """Resolve one ``plugins:`` entry to a plugin and its components.

    Args:
        entry: A bare installed-plugin name or a filesystem path.
        want_skills: Whether to load the plugin's ``skills/``.
        want_agents: Whether to load the plugin's ``agents/``.
        want_mcp: Whether to load the plugin's MCP declarations.
        base_dir: Directory a relative path entry resolves against
            (normally the workflow file's directory).
        home: Home directory installed names resolve against. A
            parameter rather than a lookup so tests never read the
            developer's real ``~``.
        on_warning: Optional sink for non-fatal diagnostics.

    Returns:
        The resolved plugin, with disabled components left empty and
        recorded in :attr:`ResolvedPlugin.disabled`.

    Raises:
        PluginNotFoundError: If the entry names nothing resolvable, or is
            ambiguous.
        PluginManifestError: If the plugin is present but unusable.
        SkillManifestError: If one of its skills has broken frontmatter.
    """
    home = Path.home() if home is None else home
    root = (
        _resolve_path_entry(entry, base_dir)
        if is_path_entry(entry)
        else _resolve_name_entry(entry, home)
    )
    manifest = read_plugin_manifest(root)

    skills = _plugin_skills(root, entry, on_warning) if want_skills else []
    agents = read_plugin_agents(root, manifest.name) if want_agents else []
    mcp_servers = dict(manifest.mcp_servers) if want_mcp else {}

    # Record what was switched off *and actually present*, so validate
    # reports a real omission rather than every default that happened to
    # be flipped on a plugin that ships nothing of that kind.
    disabled: list[str] = []
    if not want_skills and (root / PLUGIN_SKILLS_DIR).is_dir():
        disabled.append("skills")
    if not want_agents and _has_agent_definitions(root):
        disabled.append("agents")
    if not want_mcp and manifest.mcp_servers:
        disabled.append("mcp")

    return ResolvedPlugin(
        name=manifest.name,
        root=root,
        source=entry,
        skills=tuple(skills),
        agents=tuple(agents),
        mcp_servers=mcp_servers,
        dropped=_dropped_components(root),
        disabled=tuple(disabled),
    )


def describe_dropped_components(plugin: ResolvedPlugin, *, root_is_registered: bool) -> str | None:
    """Phrase what a plugin ships that Conductor does not load.

    One builder so ``conductor validate`` and ``conductor run`` cannot
    describe the same plugin differently — the paired checks exist
    precisely so the two commands agree, and that is worth nothing if the
    strings drift.

    The wording turns on the *mechanism*, not on which component it is.
    Where a provider can only reach a plugin's skills by registering the
    whole plugin root, that registration carries ``hooks/`` and
    ``commands/`` along with it — so saying "not loaded" would be exactly
    backwards, and most dangerously so for hooks, which are arbitrary
    shell run on tool events.

    Args:
        plugin: The resolved plugin.
        root_is_registered: Whether this provider will register the
            plugin root (true only when the provider has no bare
            skill-directory surface *and* this plugin's skills are on).

    Returns:
        A sentence for the user, or ``None`` when the plugin ships
        nothing Conductor drops.
    """
    if not plugin.dropped:
        return None
    listed = " and ".join(f"{name}/" for name in plugin.dropped)
    if root_is_registered:
        danger = (
            " (hooks are arbitrary shell run on tool events)" if "hooks" in plugin.dropped else ""
        )
        return (
            f"plugin {plugin.source!r} ships {listed}, and reaching its skills on this "
            f"provider requires registering the plugin root — so {listed} are exposed "
            f"to the CLI rather than dropped{danger}. Set 'skills: false' on the plugin "
            f"to avoid registering the root, or run this agent on 'copilot', which "
            f"loads each component individually."
        )
    return (
        f"plugin {plugin.source!r} ships {listed}, which Conductor does not load. "
        f"The plugin behaves differently here than it does in the CLI."
    )


def resolve_plugins(
    entries: Sequence[PluginEntry],
    *,
    base_dir: Path | None = None,
    home: Path | None = None,
    on_warning: WarningSink | None = None,
) -> list[ResolvedPlugin]:
    """Resolve a whole ``plugins:`` list.

    Args:
        entries: :class:`~conductor.config.schema.PluginDef` objects, or
            anything satisfying :class:`PluginEntry`.
        base_dir: Directory relative path entries resolve against.
        home: Home directory installed names resolve against.
        on_warning: Optional sink for non-fatal diagnostics.

    Returns:
        One :class:`ResolvedPlugin` per entry, in order, with duplicate
        roots removed (first occurrence wins).

    Raises:
        PluginError: If an entry cannot be resolved, two entries resolve
            to different plugins claiming one name, or two plugins
            declare the same MCP server name. A server-name clash is
            refused rather than merged because the name determines the
            tool names the model sees, so one plugin's tools would appear
            under the other's configuration.
        SkillManifestError: If one of a plugin's skills has broken
            frontmatter. A :class:`~conductor.skills.errors.SkillError`
            subclass, *not* a :class:`PluginError` — catch both, as
            ``config/validator.py`` does.
    """
    resolved: list[ResolvedPlugin] = []
    by_root: dict[Path, ResolvedPlugin] = {}
    by_name: dict[str, ResolvedPlugin] = {}
    servers: dict[str, ResolvedPlugin] = {}
    skill_names: dict[str, ResolvedPlugin] = {}

    switches: dict[Path, tuple[bool, bool, bool]] = {}
    for entry in entries:
        wanted = (bool(entry.skills), bool(entry.agents), bool(entry.mcp))
        plugin = resolve_plugin(
            entry.name,
            want_skills=wanted[0],
            want_agents=wanted[1],
            want_mcp=wanted[2],
            base_dir=base_dir,
            home=home,
            on_warning=on_warning,
        )
        if plugin.root in by_root:
            # The same plugin reached twice — a name and its equivalent path,
            # or two spellings of one path. Identical switches are a harmless
            # repeat; differing ones have no correct merge, and keeping the
            # first silently would grant a component the second entry
            # declined. ``_validate_plugin_entries`` refuses the same thing
            # by name; this is the by-root half it cannot see.
            if switches[plugin.root] != wanted:
                raise PluginNotFoundError(
                    f"Plugin entries {by_root[plugin.root].source!r} and "
                    f"{entry.name!r} both resolve to {plugin.root}, but enable "
                    f"different components. List the plugin once with the "
                    f"components you want."
                )
            continue
        seen = by_name.get(plugin.name)
        if seen is not None:
            raise PluginNotFoundError(
                f"Plugins {seen.source!r} and {plugin.source!r} both resolve to a "
                f"plugin named {plugin.name!r} ({seen.root} and {plugin.root}). "
                "Plugin names namespace their skills and agents, so one would "
                "silently shadow the other."
            )
        for server in plugin.mcp_servers:
            owner = servers.get(server)
            if owner is not None:
                raise PluginManifestError(
                    f"Plugins {owner.source!r} and {plugin.source!r} both declare an "
                    f"MCP server named {server!r}. The server name prefixes the tool "
                    "names the model sees, so it must be unique. Disable it on one of "
                    "them with 'mcp: false'."
                )
            servers[server] = plugin
        for skill in plugin.skills:
            claimant = skill_names.get(skill.name)
            if claimant is not None:
                # Skills reach the provider as one flat, name-keyed list, so
                # one of these would be dropped. Refusing matches
                # ``resolve_skills``, which errors when two directories claim
                # one skill name for exactly the same reason.
                raise PluginManifestError(
                    f"Plugins {claimant.source!r} and {plugin.source!r} both ship a "
                    f"skill named {skill.name!r}. Skill names are resolved without the "
                    "plugin prefix, so one would silently shadow the other. Disable it "
                    "on one of them with 'skills: false'."
                )
            skill_names[skill.name] = plugin
        by_root[plugin.root] = plugin
        switches[plugin.root] = wanted
        by_name[plugin.name] = plugin
        resolved.append(plugin)

    return resolved


__all__ = [
    "PluginEntry",
    "ResolvedPlugin",
    "describe_dropped_components",
    "resolve_plugin",
    "resolve_plugins",
]
