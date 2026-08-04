"""Discover skills already installed in the user's environment.

``skills:`` entries name skills one at a time. Discovery is the opt-in
alternative: point at *categories* of well-known location and pick up
whatever is installed there.

Discovery locations are provider-specific — the Copilot CLI keeps skills
under ``~/.copilot``, Claude Code under ``~/.claude`` — so the obvious
design (one flag, each provider asked to discover its own) would surface
**different skill sets to different agents inside a single run**,
depending on which provider each agent happens to resolve to. That is
non-determinism within one run, and it is why issue #362 exists
separately from #215 and #350.

Conductor therefore does the scanning itself, over the *union* of both
CLIs' locations, and feeds the result through the same
:func:`~conductor.skills.registry.resolve_skills` pipeline that explicit
entries use. Every agent sees the identical discovered set whatever its
provider. The providers' own discovery stays off — ``copilot`` is never
given ``enable_config_discovery`` (it would also auto-load MCP servers
from ``.mcp.json``) and ``claude-agent-sdk`` keeps ``setting_sources=[]``.

Every mapped location is a *skills root*, so all three categories expand
by :func:`~conductor.skills.registry.expand_skills_root` — discovery adds
no second opinion about what a skill directory looks like.

**Discovered content is treated more leniently than content the user
wrote.** A broken ``SKILL.md``, a name already taken, or a directory that
cannot be read is an error for an explicit ``skills:`` entry and a
warning-plus-skip for a discovered one. The author asked for the former
by name; the latter merely happened to be installed, and failing a
workflow over a stray directory in ``~/.copilot/skills`` would make
discovery unusable.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from conductor.skills.errors import SkillError
from conductor.skills.registry import (
    ResolvedSkill,
    WarningSink,
    expand_skills_root,
    resolve_skills,
)

logger = logging.getLogger(__name__)

DiscoverySource = Literal["personal", "project", "plugins"]
"""A category of well-known location to scan for installed skills.

* ``personal`` — skills the user installed for themselves, in their home
  directory.
* ``project`` — skills committed alongside the code, found by walking up
  from the workflow file to the repository root.
* ``plugins`` — skills shipped by installed plugins.

Deliberately *not* provider names. The categories are what the user
recognises ("my skills", "this repo's skills", "my plugins"); which CLI
happens to own each concrete path is an implementation detail that
Conductor unions over.
"""

# Canonical scan order, most specific to most ambient. Used instead of
# the order written in YAML so that reordering ``sources:`` can never
# change which of two same-named skills wins, and so a discovered set is
# reproducible from the filesystem alone.
_SOURCE_ORDER: tuple[DiscoverySource, ...] = ("project", "personal", "plugins")

# Home-relative roots holding standalone skill directories.
_PERSONAL_ROOTS: tuple[str, ...] = (
    ".copilot/skills",
    ".claude/skills",
)

# Directory names searched at each level of the project walk. Both CLIs
# look for their own; Conductor looks for both so one workflow behaves
# the same whichever the repo was set up for.
_PROJECT_ROOTS: tuple[str, ...] = (
    ".github/skills",
    ".claude/skills",
)

# Home-relative glob patterns matching a plugin's ``skills/`` directory.
# The ``*/*`` is load-bearing: plugins are stored two levels deep, as
# ``<marketplace>/<plugin>/``, and globbing one level finds nothing.
_PLUGIN_ROOT_GLOBS: tuple[str, ...] = (
    ".copilot/installed-plugins/*/*/skills",
    ".claude/plugins/*/*/skills",
)

# Marker identifying a repository root, where the project walk stops.
# Without a stop condition the walk would climb into the home directory
# and start picking up unrelated checkouts.
_REPO_MARKER = ".git"


@dataclass(frozen=True)
class DiscoveredSkill:
    """A skill found by scanning, rather than named in ``skills:``."""

    name: str
    """Skill name — the directory's basename, as for an explicit entry."""

    directory: Path
    """Absolute path to the directory holding ``SKILL.md``."""

    source: DiscoverySource
    """The category whose locations this was found under."""

    root: Path
    """The scanned directory it was found in.

    Reported to the user verbatim. An ambient set is only defensible if
    the author can see where each piece came from, so every message
    about a discovered skill names this.
    """


def _project_roots(base_dir: Path) -> list[Path]:
    """Candidate project skill roots, from ``base_dir`` up to the repo root.

    Mirrors what both CLIs do from their working directory, anchored on
    the workflow file instead so that ``conductor validate`` and
    ``conductor run`` agree no matter where either was invoked from.

    The walk stops at the first ancestor containing ``.git``. When there
    is no repository at all, only ``base_dir`` itself is considered —
    climbing to the filesystem root on an unversioned tree would sweep in
    whatever happens to sit above it.

    Args:
        base_dir: The workflow file's directory.

    Returns:
        Candidate roots, nearest directory first, so a repo-root skill is
        shadowed by a same-named one closer to the workflow.
    """
    ancestors: list[Path] = []
    for directory in (base_dir, *base_dir.parents):
        ancestors.append(directory)
        if _has_repo_marker(directory):
            break
    else:
        ancestors = [base_dir]

    return [ancestor / relative for ancestor in ancestors for relative in _PROJECT_ROOTS]


def _has_repo_marker(directory: Path) -> bool:
    """Whether ``directory`` looks like a repository root.

    An unreadable directory reads as "not the root" so the walk climbs
    past it rather than stopping early on a permissions quirk.
    """
    try:
        return (directory / _REPO_MARKER).exists()
    except OSError:
        return False


def _glob_roots(home: Path, patterns: Iterable[str]) -> list[Path]:
    """Expand home-relative glob patterns to existing directories.

    Args:
        home: The home directory to resolve patterns against.
        patterns: Home-relative glob patterns.

    Returns:
        Matching directories, sorted, with duplicates removed.
    """
    found: list[Path] = []
    for pattern in patterns:
        try:
            matches = sorted(home.glob(pattern))
        except OSError as exc:
            # A glob crossing an unreadable directory. Nothing here is
            # user-authored, so drop the pattern rather than fail a run.
            logger.debug("Skill discovery glob %r under %s failed: %s", pattern, home, exc)
            continue
        found.extend(match for match in matches if match.is_dir())
    return _unique(found)


def _unique(paths: Iterable[Path]) -> list[Path]:
    """Deduplicate paths preserving order."""
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _roots_for_source(source: DiscoverySource, base_dir: Path | None, home: Path) -> list[Path]:
    """Map one category onto the concrete directories to scan.

    Args:
        source: The category.
        base_dir: The workflow file's directory, for ``project``. When
            ``None`` the project walk is skipped — there is no anchor,
            and the process working directory is not one (it is wherever
            the user happened to invoke the CLI from).
        home: The home directory ``personal`` and ``plugins`` resolve
            against.

    Returns:
        Candidate roots, which may or may not exist.
    """
    if source == "personal":
        return [home / relative for relative in _PERSONAL_ROOTS]
    if source == "project":
        return [] if base_dir is None else _project_roots(base_dir)
    return _glob_roots(home, _PLUGIN_ROOT_GLOBS)


def discover_skills(
    sources: Sequence[DiscoverySource],
    *,
    base_dir: Path | None = None,
    home: Path | None = None,
    exclude: Sequence[str] = (),
    on_warning: WarningSink | None = None,
) -> list[DiscoveredSkill]:
    """Scan the enabled categories for installed skills.

    Categories are scanned in a fixed canonical order — ``project``,
    ``personal``, ``plugins`` — regardless of the order they were written
    in, so that reordering ``sources:`` cannot change which of two
    same-named skills wins.

    A location that does not exist is skipped in silence: most users have
    only one or two of these directories, and that is the ordinary case
    rather than a misconfiguration. A category that yields *nothing at
    all* does warn, because that config did nothing and the author almost
    certainly expected otherwise.

    Args:
        sources: The enabled categories. Empty means discovery is off.
        base_dir: The workflow file's directory, used to anchor the
            ``project`` walk. ``project`` is skipped when ``None``.
        home: Home directory to resolve ``personal`` and ``plugins``
            against. Defaults to the real one. Always pass an explicit
            path in tests — otherwise results depend on what the machine
            running them happens to have installed.
        exclude: Skill names to drop from the result.
        on_warning: Sink for non-fatal diagnostics.

    Returns:
        The discovered skills in canonical order, with duplicates by
        directory removed and the first of any two same-named skills
        kept.
    """
    if not sources:
        return []

    unknown = [source for source in sources if source not in get_args(DiscoverySource)]
    if unknown:  # pragma: no cover - the schema rejects these first
        raise SkillError(f"Unknown skill discovery source(s): {sorted(unknown)!r}")

    home = Path(home) if home is not None else Path.home()
    excluded = set(exclude)
    enabled: list[DiscoverySource] = [source for source in _SOURCE_ORDER if source in sources]

    by_name: dict[str, DiscoveredSkill] = {}
    out: list[DiscoveredSkill] = []
    for source in enabled:
        found_here = 0
        for root in _roots_for_source(source, base_dir, home):
            for directory in _scan_root(root, source, on_warning):
                found_here += 1
                name = directory.name
                if name in excluded:
                    continue
                seen = by_name.get(name)
                if seen is not None:
                    if seen.directory != directory:
                        _warn(
                            on_warning,
                            f"Skill discovery found two skills named {name!r}: keeping "
                            f"{seen.directory} (from {seen.root}) and ignoring "
                            f"{directory} (from {root}). Exclude one by name with "
                            "runtime.skill_discovery.exclude, or narrow 'sources'.",
                        )
                    continue
                discovered = DiscoveredSkill(
                    name=name, directory=directory, source=source, root=root
                )
                by_name[name] = discovered
                out.append(discovered)
        if found_here == 0:
            _warn(
                on_warning,
                f"Skill discovery source {source!r} found no skills. Searched: "
                f"{[str(root) for root in _roots_for_source(source, base_dir, home)]!r}. "
                "Remove it from runtime.skill_discovery.sources, or install skills there.",
            )
    return out


def _scan_root(root: Path, source: DiscoverySource, on_warning: WarningSink | None) -> list[Path]:
    """List the skill directories inside one discovery location.

    Args:
        root: A candidate location, which may not exist.
        source: The category it came from, for warning text.
        on_warning: Sink for non-fatal diagnostics.

    Returns:
        Skill directories, or an empty list when the root is absent,
        is not a directory, or cannot be read.
    """
    try:
        if not root.is_dir():
            return []
        children, skipped = expand_skills_root(root)
    except OSError as exc:
        # Unlike a path the user wrote, an unreadable ambient location is
        # not worth failing a run over — but it is worth saying, since the
        # skills the author expected from it are silently absent.
        _warn(
            on_warning,
            f"Skill discovery could not read {root} (source {source!r}): {exc}. "
            "Any skills installed there were skipped.",
        )
        return []
    if skipped:
        _warn(
            on_warning,
            f"Skill discovery skipped {len(skipped)} subdirector"
            f"{'y' if len(skipped) == 1 else 'ies'} of {root} with no SKILL.md: "
            f"{skipped!r}.",
        )
    return children


def _warn(on_warning: WarningSink | None, message: str) -> None:
    """Report a diagnostic exactly once.

    The sink is the user-facing channel when there is one — ``conductor
    validate`` prints it, the executor logs it verbosely — so also calling
    ``logger.warning`` would print every discovery diagnostic twice, since
    Conductor installs no logging handlers and ``logging.lastResort``
    writes straight to stderr. With no sink the logger is the only place
    left for it to go. Matches ``registry._resolve_path_entry``, which
    reports through its sink alone.
    """
    if on_warning is not None:
        on_warning(message)
        logger.debug("%s", message)
    else:
        logger.warning("%s", message)


def resolve_effective_skills(
    entries: Sequence[str],
    *,
    sources: Sequence[DiscoverySource] = (),
    exclude: Sequence[str] = (),
    base_dir: Path | None = None,
    home: Path | None = None,
    on_warning: WarningSink | None = None,
) -> list[ResolvedSkill]:
    """Resolve explicit ``skills:`` entries plus any discovered skills.

    The single composition point for the two ways a skill can be enabled,
    used by both :class:`~conductor.executor.agent.AgentExecutor` and
    ``conductor validate`` so a run and its validation cannot disagree
    about which skills an agent has.

    Explicit entries resolve first and strictly — an unknown name, an
    unresolvable path or broken frontmatter raises, exactly as when
    discovery is off. Discovered skills are then appended if their name is
    still free, and any that cannot be used are dropped with a warning
    rather than failing the workflow.

    Args:
        entries: The ``skills:`` entries, explicit and authoritative.
        sources: Enabled discovery categories. Empty disables discovery,
            making this equivalent to
            :func:`~conductor.skills.registry.resolve_skills`.
        exclude: Skill names to drop from the discovered set. Does not
            apply to explicit entries — removing one of those is a matter
            of deleting the line.
        base_dir: Directory relative entries and the ``project`` walk
            resolve against, normally the workflow file's.
        home: Home directory for ``personal`` and ``plugins``.
        on_warning: Sink for non-fatal diagnostics.

    Returns:
        Explicit skills in entry order, then discovered skills in
        canonical order.

    Raises:
        SkillNotFoundError: If an explicit entry does not resolve, or two
            explicit entries claim one name.
        SkillManifestError: If an explicit entry's ``SKILL.md`` is
            missing, unparseable, or incomplete.
    """
    resolved = resolve_skills(entries, base_dir=base_dir, on_warning=on_warning)
    if not sources:
        return resolved

    claimed = {skill.name: skill for skill in resolved}
    for candidate in discover_skills(
        sources, base_dir=base_dir, home=home, exclude=exclude, on_warning=on_warning
    ):
        owner = claimed.get(candidate.name)
        if owner is not None:
            if owner.directory != candidate.directory:
                _warn(
                    on_warning,
                    f"Skill {candidate.name!r} was declared explicitly as "
                    f"{owner.source!r} and also discovered at {candidate.directory} "
                    f"(from {candidate.root}). Keeping the declared one.",
                )
            continue
        skill = _to_resolved(candidate, on_warning)
        if skill is None:
            continue
        claimed[skill.name] = skill
        resolved.append(skill)
    return resolved


def _to_resolved(
    candidate: DiscoveredSkill, on_warning: WarningSink | None
) -> ResolvedSkill | None:
    """Validate a discovered skill and convert it to a :class:`ResolvedSkill`.

    Args:
        candidate: The discovered skill.
        on_warning: Sink for non-fatal diagnostics.

    Returns:
        The converted skill, or ``None`` when its manifest is unusable —
        which is a warning here rather than the error an explicit entry
        would get, because the author did not ask for this skill by name.
    """
    from conductor.skills.frontmatter import read_skill_frontmatter

    try:
        read_skill_frontmatter(candidate.directory)
    except SkillError as exc:
        _warn(
            on_warning,
            f"Skill discovery skipped {candidate.name!r} at {candidate.directory} "
            f"(from {candidate.root}): {exc}",
        )
        return None
    return ResolvedSkill(
        name=candidate.name,
        directory=candidate.directory,
        source=str(candidate.root),
        discovered=True,
    )
