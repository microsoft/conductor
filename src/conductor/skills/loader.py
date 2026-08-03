"""Load skill content for eager preamble injection (Claude-path mechanism).

On providers that lack a native skill surface, Conductor loads the full
``SKILL.md`` plus every ``references/*.md`` file from each enabled
skill's directory and prepends them to the agent's rendered prompt,
wrapped in ``<skill name="...">`` tags. On providers with native support
(Copilot's ``skill_directories``, claude-agent-sdk's plugin-scoped
``skills`` option), eager injection is skipped and the SDK handles
discovery natively — the model loads skill content only when relevant,
which is more token-efficient.

The loader is the *content* side of the skill abstraction. The
:mod:`conductor.skills.registry` module is the *resolution* side.

Results are cached per-directory for the lifetime of the process. The
cache key is the directory and name only — no mtime — so a skill edited
mid-process keeps serving its first-read content. That is intentional
for a single ``conductor run`` (one process, one workflow, consistent
prompts across retries) but it is *not* the "bundled and immutable"
guarantee this once relied on: since issue #350 a skill may be any
directory the user points at. Tests that rewrite a skill at a path they
have already loaded must call ``_cached_skill_payload.cache_clear()`` —
``tests/test_skills/conftest.py`` does this automatically for tests under
that package.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Rough bytes-per-token ratio, used only to annotate size messages with an
# approximate token count. English prose sits near 4; the exact figure is
# model- and tokenizer-specific, so every message derived from it marks the
# value as approximate — a leading ``~``, or the ``approx_tokens`` key. Lives
# here because this module produces the string those messages measure.
BYTES_PER_TOKEN_ESTIMATE = 4

_HEADER = (
    "The following content describes skills available to this agent. "
    "Each skill provides reusable knowledge or capabilities — consult "
    "the relevant skill when its description matches the task at hand."
)


def _read_file(path: Path, label: str) -> str:
    """Read one file of a skill's declared content, or fail loudly.

    A read failure is **not** skipped. Every file under a skill directory is
    content the workflow asked the agent to have, and dropping one silently
    is the precise defect this package exists to prevent — the upstream CLIs
    skip an unreadable skill without a word, which is what issue #350 was
    filed about. Doing the same one directory deeper would be no better, and
    is easy to hit now that a skill may be any path the user points at
    (a `chmod`, a non-UTF-8 byte, a flaky network mount).

    Raising also keeps the failure out of :func:`_cached_skill_payload`'s
    cache: ``lru_cache`` never memoizes a call that raised, so a transient
    error is retried rather than frozen in for the rest of the run.

    Args:
        path: File to read.
        label: How to name it in an error message.

    Returns:
        The file's text, stripped.

    Raises:
        SkillManifestError: If the file cannot be read or decoded.
    """
    from conductor.skills.frontmatter import SkillManifestError

    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillManifestError(
            f"Skill {label} at {path} could not be read: {exc}. It is part of the "
            "skill's declared content, so loading the skill without it would hand "
            "the agent less than the workflow asked for. Fix the file's permissions "
            "or encoding, or remove it from the skill directory."
        ) from exc


def _read_skill_dir(skill_dir: Path) -> str:
    """Read ``SKILL.md`` plus all ``references/*.md`` files in order.

    Returns the concatenated text, with each file preceded by a heading
    divider. Returns an empty string only when the directory genuinely has
    no content — an unreadable file raises rather than being skipped.

    Raises:
        SkillManifestError: If any file in the skill cannot be read.
    """
    sections: list[str] = []

    skill_md = skill_dir / "SKILL.md"
    if skill_md.is_file():
        text = _read_file(skill_md, "manifest")
        if text:
            sections.append(f"# SKILL.md\n\n{text}")

    references_dir = skill_dir / "references"
    if references_dir.is_dir():
        for ref in sorted(references_dir.glob("*.md")):
            text = _read_file(ref, "reference")
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
