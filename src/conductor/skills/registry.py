"""Built-in skill registry for Conductor.

Conductor ships one built-in skill — ``conductor`` — pointing at the
``plugins/conductor/skills/conductor/`` directory inside the wheel. The skill
directory follows the Copilot/Claude-Code skill format: ``SKILL.md`` plus an
optional ``references/`` subdirectory of supporting docs.

The plugins directory is bundled as wheel package data via the
``[tool.hatch.build.targets.wheel.force-include]`` entries in
``pyproject.toml``. Resolution prefers a package-relative location so
installed wheels work; it falls back to a source-checkout location for
editable installs and tests.

Entries written in ``skills:`` are resolved here: a bare name must be a
registered built-in, while anything path-shaped is resolved against the
workflow file's directory. Picking up skills already installed in the
user's environment (``~/.copilot/skills``, ``.github/skills``, plugin
roots) is opt-in and lives in :mod:`conductor.skills.discovery`, which
reuses :func:`expand_skills_root` from here so a discovered directory and
a written path are judged by the same rules.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from conductor.plugins.manifest import (
    PLUGIN_SKILLS_DIR,
    SAFE_NAME,
    find_manifest,
    read_manifest_name,
)
from conductor.skills.errors import SkillError

logger = logging.getLogger(__name__)

# Sink for non-fatal diagnostics raised during resolution. Callers decide
# where these land, because the right destination differs: warnings from
# ``conductor validate`` are printed, warnings at run time are verbose-only.
WarningSink = Callable[[str], None]


class SkillNotFoundError(SkillError):
    """Raised when a skill name is not found in the registry."""


class SkillPluginError(SkillNotFoundError):
    """Raised when a skill's owning plugin is present but unusable.

    Distinct from "this skill has no plugin at all", which
    :func:`resolve_skill_plugin` reports by returning ``None``. Keeping
    the two apart is what lets callers tell a user whose manifest is
    broken from a user whose skill simply isn't packaged as a plugin.
    """


# Built-in skills. Maps the user-facing skill name (the string that
# appears in ``skills: [...]``) to a relative path from the repository
# root / wheel root where the skill directory lives.
#
# The final path segment must equal the key: the claude-agent-sdk
# provider re-derives the skill name from the directory basename, so a
# divergence here would silently rename the skill. Pinned by
# ``test_builtin_skill_names_match_their_directory_basenames``.
_BUILTIN_SKILLS: dict[str, str] = {
    "conductor": "plugins/conductor/skills/conductor",
}


@lru_cache(maxsize=1)
def _repo_or_wheel_root() -> Path:
    """Locate the directory that contains the ``plugins/`` tree.

    Two layouts are supported:

    * **Editable install / source checkout** — ``plugins/`` lives at the
      repository root, three directories above this file
      (``src/conductor/skills/registry.py``).
    * **Wheel install** — ``plugins/`` is bundled as package data via
      hatchling's ``force-include`` entries and lands alongside the
      ``conductor/`` package directory inside ``site-packages``.

    We probe both. The first hit wins.
    """
    here = Path(__file__).resolve()

    # Editable install / source checkout.
    repo_root = here.parents[3]
    if (repo_root / "plugins" / "conductor" / "skills").is_dir():
        return repo_root

    # Wheel install: force-included files land alongside the package itself
    # (site-packages/plugins next to site-packages/conductor).
    wheel_root = here.parents[2]
    if (wheel_root / "plugins" / "conductor" / "skills").is_dir():
        return wheel_root

    # Fall back to repo_root so an eventual SkillNotFoundError surfaces a
    # sensible path; callers will raise when the dir doesn't exist.
    return repo_root


def list_builtin_skills() -> list[str]:
    """Return the names of every built-in skill known to the registry."""
    return sorted(_BUILTIN_SKILLS.keys())


def get_skill_directory(skill: str) -> Path:
    """Resolve a built-in skill *name* to its on-disk directory.

    Only handles registered built-in names. Path entries are handled by
    :func:`resolve_skills`.

    Args:
        skill: The skill name as it appears in ``skills: [...]`` (e.g.
            ``"conductor"``).

    Returns:
        Absolute path to the skill directory.

    Raises:
        SkillNotFoundError: If the skill name is not a known built-in
            or the resolved directory does not exist on disk.
    """
    rel = _BUILTIN_SKILLS.get(skill)
    if rel is None:
        available = ", ".join(list_builtin_skills()) or "(none)"
        raise SkillNotFoundError(
            f"Unknown skill {skill!r}. Available built-in skills: {available}. "
            "To use a skill of your own, give a path instead (e.g. "
            "'./team-skills/my-skill'); an entry counts as a path when it "
            "starts with '.' or '~', or contains a path separator."
        )
    path = (_repo_or_wheel_root() / rel).resolve()
    if not path.is_dir():
        raise SkillNotFoundError(
            f"Built-in skill {skill!r} resolved to {path!s}, which does not "
            "exist. This usually indicates a broken install; try reinstalling "
            "conductor. If running from a source checkout, ensure the "
            "plugins/ directory is present."
        )
    return path


@dataclass(frozen=True)
class ResolvedSkill:
    """A single skill resolved from one ``skills:`` entry."""

    name: str
    """Skill name — always the directory's basename.

    Deliberately *not* the ``SKILL.md`` frontmatter name: the built-in
    registry keys, the eager-injection ``<skill name="...">`` tag, and
    claude-agent-sdk's ``<plugin>:<skill>`` qualified name are all
    basename-derived. :func:`resolve_skill_plugin` enforces that the
    frontmatter agrees for the provider that needs it.
    """

    directory: Path
    """Absolute path to the directory holding ``SKILL.md``."""

    source: str
    """Where this skill came from, for use in messages.

    For an explicitly declared skill, the ``skills:`` entry verbatim — a
    ``skills/`` root expands to several :class:`ResolvedSkill` objects
    that share one ``source``, so errors can name what the user actually
    wrote. For a discovered skill (:attr:`discovered`), the **scanned
    root** it was found under — not its own directory, which is
    :attr:`directory`.
    """

    discovered: bool = False
    """Whether this skill was found by scanning rather than declared.

    Callers treat the two differently on purpose: a problem with a skill
    the author named is an error, while the same problem with one that
    merely happened to be installed is a warning and a skip. See
    :mod:`conductor.skills.discovery`.
    """

    def __post_init__(self) -> None:
        """Assert the two invariants the docstrings above claim.

        Both are established by :func:`resolve_skills` today, but this is
        an exported public constructor and ``name`` is interpolated
        unescaped into ``<skill name="...">`` by the loader. Same
        reasoning as :class:`SkillPlugin`, which checks its own names for
        the same reason.

        Deliberately no ``is_dir()`` probe: filesystem state in a
        constructor is a TOCTOU illusion, and establishing it is
        correctly the producer's job.

        Raises:
            SkillNotFoundError: If ``name`` is not ``directory``'s
                basename, or ``directory`` is not absolute.
        """
        if self.name != self.directory.name:
            raise SkillNotFoundError(
                f"ResolvedSkill.name {self.name!r} must equal its directory "
                f"basename {self.directory.name!r}"
            )
        if not self.directory.is_absolute():
            raise SkillNotFoundError(
                f"ResolvedSkill.directory must be absolute, got {self.directory}"
            )


def is_path_entry(entry: str) -> bool:
    """Whether a ``skills:`` entry denotes a filesystem path.

    The test is purely syntactic — no disk access — so classification
    never depends on what happens to exist locally, and a bare name like
    ``conductor`` can never be shadowed by a same-named directory.

    No ``os.path.isabs`` check is needed: every absolute form on either
    POSIX or Windows (``/x``, ``C:\\x``, ``C:/x``, ``\\\\server\\share``)
    already contains a separator.
    """
    return entry.startswith(("~", ".")) or "/" in entry or "\\" in entry


def expand_skills_root(root: Path) -> tuple[list[Path], list[str], list[str]]:
    """Split a skills root into the skill directories it holds.

    The one definition of "what counts as a skill directory", shared by
    ``skills:`` path entries and by
    :mod:`conductor.skills.discovery` — the discovery locations are all
    skills roots, so they expand by exactly these rules.

    A child that cannot be read is reported rather than allowed to
    propagate. Letting it escape would discard every *readable* sibling
    over one stray directory, and the resulting message would name the
    root, which the user can list perfectly well.

    Deliberately makes no judgement about an empty result: a path entry
    naming an empty root is a user error, while a discovery location that
    happens to hold nothing is the ordinary case. Each caller decides —
    a path entry naming an empty root raises, while discovery defers the
    judgement to :func:`~conductor.skills.discovery.discover_skills`,
    which warns only when a whole *source* came up empty.

    Args:
        root: An existing directory to look inside.

    Returns:
        A ``(children, skipped, unreadable)`` tuple. ``children`` is every
        immediate subdirectory holding a ``SKILL.md``, sorted by name;
        ``skipped`` is the names of the subdirectories that do not, so
        callers can report near-misses instead of silently yielding one
        fewer skill; ``unreadable`` is the names of those that could not
        be inspected at all.

    Raises:
        OSError: If ``root`` itself cannot be listed. Callers translate
            this into their own message — there is no single phrasing
            that suits both a user-written path and an ambient location.
    """
    children: list[Path] = []
    skipped: list[str] = []
    unreadable: list[str] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        try:
            if (child / "SKILL.md").is_file():
                children.append(child)
            # Files (a README, a LICENSE) are not reported as near-misses —
            # only a directory can plausibly have been *meant* as a skill.
            # Without this, pointing at a root turns the loud "no SKILL.md"
            # error you would get from naming the directory directly into
            # silence: a mis-cased ``Skill.md`` or a file someone forgot to
            # commit simply yields one fewer skill.
            elif child.is_dir():
                skipped.append(child.name)
        except OSError:
            unreadable.append(child.name)
    return children, skipped, unreadable


def _resolve_path_entry(
    entry: str, base_dir: Path | None, on_warning: WarningSink | None = None
) -> list[Path]:
    """Expand a path entry to the skill directories it denotes.

    Accepts either granularity: a single skill directory (one holding
    ``SKILL.md``) or a root containing several such directories.

    Args:
        entry: The raw path as written in ``skills:``.
        base_dir: Directory a relative entry resolves against. Falls back
            to the process working directory.
        on_warning: Optional sink for non-fatal diagnostics — currently a
            skills root that skipped subdirectories lacking a ``SKILL.md``.

    Returns:
        The entry itself for a single skill directory, or every child
        holding a ``SKILL.md`` (sorted by name) for a root.

    Raises:
        SkillNotFoundError: If the path does not exist, is not a
            directory, cannot be read, or is a directory holding neither
            a ``SKILL.md`` nor any child that does.
    """
    path = Path(entry).expanduser()
    if not path.is_absolute():
        path = (base_dir if base_dir is not None else Path.cwd()) / path
    # normpath, not resolve(): symlink aliases stay distinct, matching
    # WorkflowEngine._resolve_agent_working_dir.
    resolved = Path(os.path.normpath(path))

    try:
        if not resolved.exists():
            raise SkillNotFoundError(
                f"Skill path {entry!r} resolved to {resolved!s}, which does not exist. "
                "Relative skill paths resolve against the workflow file's directory."
            )
        if not resolved.is_dir():
            raise SkillNotFoundError(
                f"Skill path {entry!r} resolved to {resolved!s}, which is not a "
                "directory. Point it at a skill directory (one containing SKILL.md) "
                "or at a directory of them."
            )
        if (resolved / "SKILL.md").is_file():
            return [resolved]
        children, skipped, unreadable = expand_skills_root(resolved)
    except OSError as exc:
        # A path Conductor can name but not inspect — an unreadable directory,
        # or one whose parent is unreadable. Python re-raises EACCES from
        # ``exists``, ``is_dir``, ``is_file`` and ``iterdir`` alike, so without
        # this the caller sees a bare PermissionError traceback instead of a
        # message naming the entry.
        raise SkillNotFoundError(
            f"Skill path {entry!r} resolved to {resolved!s}, which could not be read: {exc}"
        ) from exc

    if unreadable:
        # Strict for a path the user wrote, matching every other failure
        # here — discovery is the caller that downgrades this to a warning.
        raise SkillNotFoundError(
            f"Skill path {entry!r} resolved to {resolved!s}, whose "
            f"subdirector{'y' if len(unreadable) == 1 else 'ies'} {unreadable!r} "
            "could not be read. Fix the permissions, or remove them."
        )

    if children:
        if skipped and on_warning is not None:
            on_warning(
                f"Skills root {entry!r} expanded to {len(children)} skill(s) but "
                f"skipped {len(skipped)} subdirector{'y' if len(skipped) == 1 else 'ies'} "
                f"with no SKILL.md: {skipped!r}. Add a SKILL.md to each, or remove them."
            )
        return children

    raise SkillNotFoundError(
        f"Skill path {entry!r} resolved to {resolved!s}, which contains neither a "
        "SKILL.md nor any subdirectory containing one. A skill directory holds "
        "SKILL.md directly; a skills root holds one subdirectory per skill."
    )


def resolve_skills(
    skills: Sequence[str],
    base_dir: Path | None = None,
    on_warning: WarningSink | None = None,
) -> list[ResolvedSkill]:
    """Resolve ``skills:`` entries to named, on-disk skill directories.

    Each entry is either a **registered built-in name** (``conductor``)
    or a **filesystem path**. Classification is syntactic — see
    :func:`is_path_entry` — so a bare name is never shadowed by a
    same-named local directory.

    Every resolved directory's ``SKILL.md`` frontmatter is parsed and
    checked here rather than only at ``conductor validate`` time,
    because ``conductor run`` does not run the static validator and both
    downstream CLIs skip an unparseable skill in silence.

    Args:
        skills: The entries as written in ``skills:``.
        base_dir: Directory relative path entries resolve against
            (normally the workflow file's directory). Falls back to the
            process working directory.
        on_warning: Optional sink for non-fatal diagnostics. ``conductor
            validate`` routes these into its warning list; the executor
            routes them to verbose logging.

    Returns:
        One :class:`ResolvedSkill` per skill directory, in entry order,
        with duplicate directories removed (first occurrence wins).

    Raises:
        SkillNotFoundError: If an entry is an unknown built-in name, a
            path that does not resolve to any skill directory, or two
            different directories that would claim the same skill name.
        SkillManifestError: If a resolved skill's ``SKILL.md`` is
            missing, unparseable, or omits ``name`` / ``description``.
    """
    from conductor.skills.frontmatter import read_skill_frontmatter

    # A skill's name is its directory basename, so this doubles as the
    # seen-directories index: one name maps to exactly one directory.
    by_name: dict[str, ResolvedSkill] = {}
    out: list[ResolvedSkill] = []
    for entry in skills:
        if is_path_entry(entry):
            directories = _resolve_path_entry(entry, base_dir, on_warning)
        else:
            directories = [get_skill_directory(entry)]
        for directory in directories:
            name = directory.name
            seen = by_name.get(name)
            if seen is not None:
                if seen.directory == directory:
                    # The same directory reached twice — a name and its
                    # equivalent path, or two overlapping roots. First wins.
                    continue
                # Two different directories, one name. Every downstream
                # consumer is name-keyed — the eager preamble emits
                # ``<skill name="...">`` per skill and the native CLIs
                # resolve by name — so one of the two would be shadowed
                # with no indication which. Refusing is the same call
                # ``resolve_skill_plugin`` makes for a qualified-name clash.
                raise SkillNotFoundError(
                    f"Skills {seen.source!r} and {entry!r} both resolve to a skill "
                    f"named {name!r} ({seen.directory} and {directory}). Skill names "
                    "must be unique — one would silently shadow the other. Rename one "
                    "of the directories, or enable only one of them."
                )
            read_skill_frontmatter(directory)
            resolved = ResolvedSkill(name=name, directory=directory, source=entry)
            by_name[name] = resolved
            out.append(resolved)
    return out


# What counts as a plugin root, and what its parts are called, is defined
# once in :mod:`conductor.plugins.manifest` and imported here. A second
# copy would strand Copilot-convention plugins: recognising only
# ``.claude-plugin/plugin.json`` leaves a skill inside a plugin that uses
# ``.github/plugin/plugin.json`` with no reachable plugin root, and
# claude-agent-sdk refuses it — 12 of 13 plugins on an ordinary machine.
_PLUGIN_SKILLS_DIR: str = PLUGIN_SKILLS_DIR

# How far above a skill directory to look for the plugin manifest. The
# layout is ``<root>/skills/<skill>/``, so two levels is the exact
# distance; a third allows one level of grouping under ``skills/``.
_PLUGIN_SEARCH_DEPTH: int = 3

# Characters allowed in a plugin or skill name. The two are joined with
# ``:`` into a qualified name, which the SDK expands to ``Skill(<name>)``
# and joins with ``,`` into a single ``--allowedTools`` value — so a name
# containing either delimiter would split into extra permission rules.
_SAFE_NAME = SAFE_NAME


@dataclass(frozen=True)
class SkillPlugin:
    """A skill directory together with the plugin that owns it.

    Providers whose SDK loads skills through the Claude Code *plugin*
    surface (``claude-agent-sdk``) need the plugin root and the
    plugin-qualified skill name, not just the skill directory.

    Invariants (enforced in ``__post_init__``):

    * ``skill_name`` and ``plugin_name`` are non-empty and match
      :data:`_SAFE_NAME`, so ``qualified_name`` cannot inject extra
      entries into the SDK's delimiter-joined tool list.
    * ``plugin_root`` is absolute.

    Violations raise :class:`SkillPluginError` — a ``ValueError`` subclass,
    so it is still the exception a value object is expected to raise, but
    one callers can catch alongside the resolver's own failures.
    """

    skill_name: str
    """Skill directory name.

    :func:`resolve_skill_plugin` checks this against the ``name`` in the
    skill's ``SKILL.md`` frontmatter, because the CLI resolves the
    enabled-skill list against that name — a divergence would hide the
    skill rather than fail.
    """

    plugin_name: str
    """Plugin name as declared in ``.claude-plugin/plugin.json``."""

    plugin_root: Path
    """Absolute path to the plugin root (the directory holding
    ``.claude-plugin/``), suitable for ``--plugin-dir``.

    Broader than :attr:`skill_name`: registering a root exposes every
    command, agent, skill, and hook the plugin ships. Callers must pair
    it with :attr:`qualified_name` in the SDK's ``skills`` filter to
    narrow back down to the declared skill.
    """

    def __post_init__(self) -> None:
        # SkillPluginError (a ValueError subclass) rather than a bare
        # ValueError: the provider catches that class to report the real
        # reason, so a name the producer failed to reject must not escape
        # as an unhandled exception.
        for label, value in (
            ("skill_name", self.skill_name),
            ("plugin_name", self.plugin_name),
        ):
            if not _SAFE_NAME.match(value):
                raise SkillPluginError(
                    f"SkillPlugin.{label} must match {_SAFE_NAME.pattern} "
                    f"(it is joined into a delimited tool list), got {value!r}"
                )
        if not self.plugin_root.is_absolute():
            raise SkillPluginError(
                f"SkillPlugin.plugin_root must be absolute, got {self.plugin_root!s}"
            )

    @property
    def qualified_name(self) -> str:
        """``<plugin>:<skill>`` — how the SDK names a plugin's skill."""
        return f"{self.plugin_name}:{self.skill_name}"


def _read_plugin_name(manifest: Path) -> str:
    """Read the ``name`` a plugin manifest declares.

    Delegates to :func:`conductor.plugins.manifest.read_manifest_name` so
    both manifest conventions are recognised here exactly as they are
    when resolving a ``plugins:`` entry, and re-raises as
    :class:`SkillPluginError` because that is the class this module's
    callers catch.

    Args:
        manifest: Path to an existing plugin manifest.

    Returns:
        The declared plugin name.

    Raises:
        SkillPluginError: If the file cannot be read, is not a JSON
            object, or declares no usable ``name``.
    """
    from conductor.plugins.errors import PluginManifestError

    try:
        return read_manifest_name(manifest)
    except PluginManifestError as exc:
        raise SkillPluginError(str(exc)) from exc


def _declared_skill_name(skill_dir: Path) -> str:
    """Read the ``name`` a skill's ``SKILL.md`` frontmatter declares.

    Delegates to :func:`~conductor.skills.frontmatter.read_skill_frontmatter`
    so a broken manifest is reported the same way here as at
    ``conductor validate`` time, and re-raises as
    :class:`SkillPluginError` because that is the class
    :func:`resolve_skill_plugin`'s callers catch.

    Args:
        skill_dir: Directory expected to contain ``SKILL.md``.

    Returns:
        The declared skill name.

    Raises:
        SkillPluginError: If ``SKILL.md`` is missing, unreadable, has
            unparseable frontmatter, or omits ``name`` / ``description``.
    """
    from conductor.skills.frontmatter import SkillManifestError, read_skill_frontmatter

    try:
        return read_skill_frontmatter(skill_dir).name
    except SkillManifestError as exc:
        raise SkillPluginError(str(exc)) from exc


def resolve_skill_plugin(skill_dir: Path) -> SkillPlugin | None:
    """Find the Claude Code plugin that owns a skill directory.

    Walks up from ``skill_dir`` through its nearest
    :data:`_PLUGIN_SEARCH_DEPTH` ancestors looking for a
    ``.claude-plugin/plugin.json`` manifest. An ancestor only counts as
    the owner when ``skill_dir`` also sits under its ``skills/``
    directory, so an unrelated plugin further up the tree cannot adopt a
    skill it does not ship.

    Args:
        skill_dir: Path to a skill directory (the one holding
            ``SKILL.md``). Resolved to an absolute path.

    Returns:
        The resolved :class:`SkillPlugin`, or ``None`` when no owning
        plugin root is found. Callers decide whether that is fatal —
        providers that can only load skills via plugins should refuse
        loudly rather than drop the skill silently.

    Raises:
        SkillPluginError: If an owning plugin is found but cannot be
            used: an unreadable or nameless manifest, a missing
            ``SKILL.md``, or a frontmatter ``name`` that disagrees with
            the directory name. Each of these would otherwise leave the
            agent running without the skill it declared, with nothing to
            diagnose it by.
    """
    skill_dir = skill_dir.resolve()
    for candidate in skill_dir.parents[:_PLUGIN_SEARCH_DEPTH]:
        manifest = find_manifest(candidate)
        if manifest is None:
            continue
        if not skill_dir.is_relative_to(candidate / _PLUGIN_SKILLS_DIR):
            # A plugin root that does not ship this skill. Keep walking:
            # adopting it would register an unrelated plugin and ask the
            # CLI for a name it cannot resolve.
            continue
        plugin_name = _read_plugin_name(manifest)
        declared = _declared_skill_name(skill_dir)
        if declared != skill_dir.name:
            raise SkillPluginError(
                f"Skill at {skill_dir} declares 'name: {declared}' in SKILL.md but lives "
                f"in a directory named {skill_dir.name!r}. The CLI would be asked for "
                f"'{plugin_name}:{skill_dir.name}', which matches nothing."
            )
        return SkillPlugin(
            skill_name=skill_dir.name,
            plugin_name=plugin_name,
            plugin_root=candidate,
        )
    return None
