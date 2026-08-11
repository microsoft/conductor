"""Parse and validate ``SKILL.md`` YAML frontmatter.

Every skill directory carries a ``SKILL.md`` whose leading ``---``
block declares at minimum a ``name`` and a ``description``. Both the
Copilot CLI and Claude Code resolve skills through those two fields:
``name`` is how an enabled skill is referenced, ``description`` is the
progressive-disclosure summary the model reads before deciding to load
the body.

The reason this module exists is that both CLIs **silently skip** a
skill whose frontmatter fails to parse — no warning, no error, the
skill is simply absent. The trap is ordinary:

.. code-block:: yaml

    ---
    name: acme-widgets
    description: Internal ACME conventions. Triggers: widget, acme widget.
    ---

``Triggers:`` inside an unquoted plain scalar makes that invalid YAML.
Conductor parses the block itself so the failure surfaces at
``conductor validate`` time with the fix spelled out, instead of as an
agent that quietly never received its skill.

The ``---`` block itself is split by :mod:`conductor.frontmatter`, which
a plugin's ``agents/<name>.agent.md`` shares — same format, same silent
downstream skip, so one parser rather than two.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from conductor.frontmatter import (
    BLOCK_SCALAR_HINT as _BLOCK_SCALAR_HINT,
)
from conductor.frontmatter import (
    FrontmatterShapeError,
    FrontmatterSyntaxError,
    split_frontmatter,
)
from conductor.skills.errors import SkillError


class SkillManifestError(SkillError):
    """Raised when a skill's ``SKILL.md`` is missing, unparseable, or incomplete.

    A sibling of :class:`~conductor.skills.registry.SkillNotFoundError`
    rather than a subclass — a broken manifest is not a species of "no
    such skill". Catch :class:`~conductor.skills.errors.SkillError` to
    handle both.
    """


@dataclass(frozen=True)
class SkillFrontmatter:
    """The ``name`` and ``description`` a ``SKILL.md`` declares."""

    name: str
    """Skill name. Both CLIs resolve an enabled skill by this value, not
    by its directory name — :func:`~conductor.skills.registry.resolve_skill_plugin`
    checks the two agree."""

    description: str
    """One-paragraph summary used for progressive disclosure."""


def read_skill_frontmatter(skill_dir: Path) -> SkillFrontmatter:
    """Read and validate the frontmatter of ``skill_dir/SKILL.md``.

    Args:
        skill_dir: Directory expected to contain ``SKILL.md``.

    Returns:
        The parsed :class:`SkillFrontmatter`.

    Raises:
        SkillManifestError: If ``SKILL.md`` is missing or unreadable, has
            no frontmatter block, contains invalid YAML, does not parse
            to a mapping, or omits a non-empty string ``name`` or
            ``description``.
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise SkillManifestError(
            f"Skill directory {skill_dir} has no SKILL.md. A skill directory must "
            "contain a SKILL.md whose YAML frontmatter declares 'name' and "
            "'description'."
        )
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillManifestError(f"Skill manifest at {skill_md} could not be read: {exc}") from exc

    try:
        parsed, _body = split_frontmatter(text)
    except FrontmatterSyntaxError as exc:
        raise SkillManifestError(
            f"Skill manifest at {skill_md} has {exc}\n\n{_BLOCK_SCALAR_HINT}"
        ) from exc
    except FrontmatterShapeError as exc:
        raise SkillManifestError(
            f"Skill manifest at {skill_md} has {exc}. It must declare 'name' "
            "and 'description' as keys."
        ) from exc

    if parsed is None:
        raise SkillManifestError(
            f"Skill manifest at {skill_md} has no YAML frontmatter. It must begin "
            "with a '---' line, followed by 'name' and 'description', followed by "
            "a closing '---' line."
        )

    values: dict[str, str] = {}
    for field in ("name", "description"):
        value = parsed.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SkillManifestError(
                f"Skill manifest at {skill_md} declares no usable {field!r} in its "
                f"YAML frontmatter (got {value!r}). Both CLIs skip a skill whose "
                f"frontmatter is incomplete, so the agent would run without it.\n\n"
                f"{_BLOCK_SCALAR_HINT}"
            )
        values[field] = value.strip()

    return SkillFrontmatter(name=values["name"], description=values["description"])
