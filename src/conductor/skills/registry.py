"""Built-in skill registry for Conductor.

Phase 1 ships a single built-in skill — ``conductor`` — that points at
the existing ``plugins/conductor/skills/conductor/`` directory inside the
wheel. The skill directory follows the Copilot/Claude-Code skill format:
``SKILL.md`` plus an optional ``references/`` subdirectory of supporting
docs.

The plugins directory is bundled as wheel package data via the
``[tool.hatch.build.targets.wheel] artifacts`` entry in
``pyproject.toml``. Resolution prefers a package-relative location so
installed wheels work; it falls back to a source-checkout location for
editable installs and tests.

Follow-up issues will add user-defined skill directories with a trust /
allowlist model; for now the registry only accepts built-in skill names.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


class SkillNotFoundError(ValueError):
    """Raised when a skill name is not found in the registry."""


# Built-in skills. Maps the user-facing skill name (the string that
# appears in ``skills: [...]``) to a relative path from the repository
# root / wheel root where the skill directory lives.
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
    """Resolve a built-in skill name to its on-disk directory.

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
            "User-defined skill directories are not yet supported."
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


def resolve_skill_directories(skills: list[str]) -> list[Path]:
    """Resolve a list of skill names to their on-disk directories.

    Args:
        skills: List of built-in skill names.

    Returns:
        List of absolute paths in the same order, with duplicates removed
        (preserving first occurrence).

    Raises:
        SkillNotFoundError: If any skill name is unknown.
    """
    seen: set[Path] = set()
    out: list[Path] = []
    for name in skills:
        path = get_skill_directory(name)
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out
