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

Parsing uses ``ruamel.yaml`` — the project's YAML library. PyYAML is
not a dependency.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from conductor.skills.errors import SkillError

# The leading ``---`` fenced block. Anchored at the start of the file:
# a ``---`` further down is a thematic break in the body, not metadata.
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)

# Appended to every parse failure: the block-scalar form sidesteps the
# colon-in-a-plain-scalar trap entirely.
_BLOCK_SCALAR_HINT = (
    "A ':' followed by a space inside an unquoted value (e.g. "
    "'description: Does things. Triggers: a, b') is invalid YAML. Use a "
    "block scalar instead:\n"
    "    description: |\n"
    "      Does things. Triggers: a, b"
)


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

    match = _FRONTMATTER.match(text)
    if match is None:
        raise SkillManifestError(
            f"Skill manifest at {skill_md} has no YAML frontmatter. It must begin "
            "with a '---' line, followed by 'name' and 'description', followed by "
            "a closing '---' line."
        )

    try:
        parsed = YAML(typ="safe").load(io.StringIO(match.group(1)))
    except YAMLError as exc:
        raise SkillManifestError(
            f"Skill manifest at {skill_md} has invalid YAML frontmatter: {exc}\n\n"
            f"{_BLOCK_SCALAR_HINT}"
        ) from exc

    if not isinstance(parsed, dict):
        raise SkillManifestError(
            f"Skill manifest at {skill_md} has frontmatter that is not a YAML "
            f"mapping (parsed as {type(parsed).__name__}). It must declare 'name' "
            "and 'description' as keys."
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
