"""Load skill content for eager preamble injection (Claude-path mechanism).

On providers that lack a native skill surface (Claude, today), Conductor
loads the full ``SKILL.md`` plus every ``references/*.md`` file from
each enabled skill's directory and prepends them to the agent's rendered
prompt, wrapped in ``<skill name="...">`` tags. On providers with native
support (Copilot's ``skill_directories``), eager injection is skipped
and the SDK handles discovery natively — the model loads skill content
only when relevant, which is more token-efficient.

The loader is the *content* side of the skill abstraction. The
:mod:`conductor.skills.registry` module is the *resolution* side.
Results are cached per-directory for the lifetime of the process — skill
content is bundled and immutable.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_HEADER = (
    "The following content describes skills available to this agent. "
    "Each skill provides reusable knowledge or capabilities — consult "
    "the relevant skill when its description matches the task at hand."
)


def _read_skill_dir(skill_dir: Path) -> str:
    """Read ``SKILL.md`` plus all ``references/*.md`` files in order.

    Returns the concatenated text, with each file preceded by a heading
    divider. Returns an empty string if the directory has no readable
    content.
    """
    sections: list[str] = []

    skill_md = skill_dir / "SKILL.md"
    if skill_md.is_file():
        try:
            text = skill_md.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Failed to read SKILL.md at %s: %s", skill_md, exc)
            text = ""
        if text:
            sections.append(f"# SKILL.md\n\n{text}")

    references_dir = skill_dir / "references"
    if references_dir.is_dir():
        for ref in sorted(references_dir.glob("*.md")):
            try:
                text = ref.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("Failed to read %s: %s", ref, exc)
                continue
            if text:
                sections.append(f"# references/{ref.name}\n\n{text}")

    return "\n\n---\n\n".join(sections)


@functools.lru_cache(maxsize=32)
def _cached_skill_payload(skill_dir_str: str, name: str) -> str:
    skill_dir = Path(skill_dir_str)
    body = _read_skill_dir(skill_dir)
    if not body:
        return ""
    size_kb = len(body.encode("utf-8")) / 1024
    logger.info("Loaded skill %r from %s (%.1fKB)", name, skill_dir, size_kb)
    return f'<skill name="{name}">\n{body}\n</skill>'


def load_skill_content(skills: list[tuple[str, Path]]) -> str:
    """Load and concatenate skill content for eager preamble injection.

    Args:
        skills: List of ``(skill_name, skill_dir)`` tuples in
            presentation order.

    Returns:
        A single string containing every skill's ``SKILL.md`` plus
        ``references/*.md`` content wrapped in ``<skill name="...">``
        tags and prefaced with a header describing the section. Returns
        an empty string when no skills produce any content.
    """
    payloads = [
        payload
        for name, skill_dir in skills
        if (payload := _cached_skill_payload(str(skill_dir), name))
    ]
    if not payloads:
        return ""
    body = "\n\n".join(payloads)
    return f"<skills>\n{_HEADER}\n\n{body}\n</skills>\n\n"
