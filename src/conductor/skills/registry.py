"""Built-in skill registry for Conductor.

Phase 1 ships a single built-in skill — ``conductor`` — that points at
the existing ``plugins/conductor/skills/conductor/`` directory inside the
wheel. The skill directory follows the Copilot/Claude-Code skill format:
``SKILL.md`` plus an optional ``references/`` subdirectory of supporting
docs.

The plugins directory is bundled as wheel package data via the
``[tool.hatch.build.targets.wheel.force-include]`` entries in
``pyproject.toml``. Resolution prefers a package-relative location so
installed wheels work; it falls back to a source-checkout location for
editable installs and tests.

Entries written in ``skills:`` are resolved here: a bare name must be a
registered built-in, while anything path-shaped is resolved against the
workflow file's directory. Discovering skills already installed in the
user's environment (``~/.copilot/skills``, ``.github/skills``, plugin
roots) is deliberately out of scope — discovery locations differ per
provider, so it is tracked separately in issue #362.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


class SkillNotFoundError(ValueError):
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
      hatchling's ``artifacts`` entry and lands alongside the
      ``conductor/`` package directory inside ``site-packages``.

    We probe both. The first hit wins.
    """
    here = Path(__file__).resolve()

    # Editable install / source checkout.
    repo_root = here.parents[3]
    if (repo_root / "plugins" / "conductor" / "skills").is_dir():
        return repo_root

    # Wheel install: artifacts land alongside the package itself
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
            "'./team-skills/my-skill'); paths are recognised by a leading "
            "'./', '~/', or an embedded '/'."
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


def resolve_skill_directories(skills: list[str], base_dir: Path | None = None) -> list[Path]:
    """Resolve a list of skill entries to their on-disk directories.

    Thin wrapper over :func:`resolve_skills` for callers that only need
    the directories. Note the result is not index-parallel with
    ``skills``: a ``skills/`` root entry expands to one path per child.

    Args:
        skills: Built-in skill names and/or filesystem paths.
        base_dir: Directory relative path entries resolve against.

    Returns:
        List of absolute paths, duplicates removed.

    Raises:
        SkillNotFoundError: If any entry cannot be resolved.
        SkillManifestError: If a resolved skill's ``SKILL.md`` is
            missing, unparseable, or incomplete.
    """
    return [resolved.directory for resolved in resolve_skills(skills, base_dir)]


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
    """The ``skills:`` entry this was resolved from, verbatim.

    A ``skills/`` root expands to several :class:`ResolvedSkill` objects
    that share one ``source``, so error messages can name what the user
    actually wrote.
    """


def _is_path_entry(entry: str) -> bool:
    """Whether a ``skills:`` entry denotes a filesystem path.

    The test is purely syntactic — no disk access — so classification
    never depends on what happens to exist locally, and a bare name like
    ``conductor`` can never be shadowed by a same-named directory.
    """
    return entry.startswith(("~", ".")) or "/" in entry or "\\" in entry or os.path.isabs(entry)


def _resolve_path_entry(entry: str, base_dir: Path | None) -> tuple[Path, list[Path]]:
    """Expand a path entry to the skill directories it denotes.

    Accepts either granularity: a single skill directory (one holding
    ``SKILL.md``) or a root containing several such directories.

    Args:
        entry: The raw path as written in ``skills:``.
        base_dir: Directory a relative entry resolves against. Falls back
            to the process working directory.

    Returns:
        A ``(resolved_root, skill_dirs)`` tuple. ``skill_dirs`` holds one
        path for a single skill directory, or every child holding a
        ``SKILL.md`` (sorted by name) for a root.

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

    try:
        if (resolved / "SKILL.md").is_file():
            return resolved, [resolved]
        entries = list(resolved.iterdir())
        children = sorted(
            (child for child in entries if (child / "SKILL.md").is_file()),
            key=lambda child: child.name,
        )
    except OSError as exc:
        # A stat-able but unreadable directory. Python re-raises EACCES from
        # both ``is_file`` and ``iterdir``, so without this the caller sees a
        # bare PermissionError traceback instead of a message naming the entry.
        raise SkillNotFoundError(
            f"Skill path {entry!r} resolved to {resolved!s}, which could not be read: {exc}"
        ) from exc

    if children:
        return resolved, children

    raise SkillNotFoundError(
        f"Skill path {entry!r} resolved to {resolved!s}, which contains neither a "
        "SKILL.md nor any subdirectory containing one. A skill directory holds "
        "SKILL.md directly; a skills root holds one subdirectory per skill."
    )


def resolve_skills(skills: Sequence[str], base_dir: Path | None = None) -> list[ResolvedSkill]:
    """Resolve ``skills:`` entries to named, on-disk skill directories.

    Each entry is either a **registered built-in name** (``conductor``)
    or a **filesystem path**. Classification is syntactic — see
    :func:`_is_path_entry` — so a bare name is never shadowed by a
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

    Returns:
        One :class:`ResolvedSkill` per skill directory, in entry order,
        with duplicate directories removed (first occurrence wins).

    Raises:
        SkillNotFoundError: If an entry is an unknown built-in name, or a
            path that does not resolve to any skill directory.
        SkillManifestError: If a resolved skill's ``SKILL.md`` is
            missing, unparseable, or omits ``name`` / ``description``.
    """
    from conductor.skills.frontmatter import read_skill_frontmatter

    seen: set[Path] = set()
    out: list[ResolvedSkill] = []
    for entry in skills:
        if _is_path_entry(entry):
            _, directories = _resolve_path_entry(entry, base_dir)
        else:
            directories = [get_skill_directory(entry)]
        for directory in directories:
            if directory in seen:
                continue
            seen.add(directory)
            read_skill_frontmatter(directory)
            out.append(ResolvedSkill(name=directory.name, directory=directory, source=entry))
    return out


# Marker file identifying a Claude Code plugin root. A skill directory
# that lives under one can be registered natively with the
# claude-agent-sdk via ``--plugin-dir``.
_PLUGIN_MANIFEST: Path = Path(".claude-plugin") / "plugin.json"

# Directory a plugin keeps its skills in, relative to the plugin root.
_PLUGIN_SKILLS_DIR: str = "skills"

# How far above a skill directory to look for the plugin manifest. The
# layout is ``<root>/skills/<skill>/``, so two levels is the exact
# distance; a third allows one level of grouping under ``skills/``.
_PLUGIN_SEARCH_DEPTH: int = 3

# Characters allowed in a plugin or skill name. The two are joined with
# ``:`` into a qualified name, which the SDK expands to ``Skill(<name>)``
# and joins with ``,`` into a single ``--allowedTools`` value — so a name
# containing either delimiter would split into extra permission rules.
_SAFE_NAME = re.compile(r"\A[A-Za-z0-9_.-]+\Z")


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

    Args:
        manifest: Path to an existing ``.claude-plugin/plugin.json``.

    Returns:
        The declared plugin name.

    Raises:
        SkillPluginError: If the file cannot be read, is not a JSON
            object, or declares no usable ``name``.
    """
    try:
        parsed = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SkillPluginError(f"Plugin manifest at {manifest} could not be read: {exc}") from exc
    # Anything that parses to something other than an object (a bare
    # array, string, or null) is as unusable as a parse failure.
    name = parsed.get("name") if isinstance(parsed, dict) else None
    if not isinstance(name, str) or not name:
        raise SkillPluginError(f"Plugin manifest at {manifest} declares no usable 'name'.")
    if not _SAFE_NAME.match(name):
        raise SkillPluginError(
            f"Plugin manifest at {manifest} declares name {name!r}, which contains "
            f"characters outside {_SAFE_NAME.pattern}. The name is joined into the "
            "CLI's delimiter-separated tool list, so it must not contain ':' or ','."
        )
    return name


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
        manifest = candidate / _PLUGIN_MANIFEST
        if not manifest.is_file():
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
