"""Split a leading YAML frontmatter block from a Markdown document.

Shared by the two Markdown formats Conductor consumes from the wider
agent ecosystem — a skill's ``SKILL.md``
(:mod:`conductor.skills.frontmatter`) and a plugin's
``agents/<name>.agent.md`` (:mod:`conductor.plugins.agents`). Both are
``---`` fenced YAML followed by a prose body, and both are **silently
skipped** by the downstream CLIs when that block fails to parse, so
Conductor parses it itself and reports the failure.

A leaf module with no Conductor imports, following
:mod:`conductor.duration`: it raises plain ``ValueError`` subclasses and
leaves the surrounding message to callers, because "skill manifest at X"
and "agent definition at Y" want different phrasing for the same defect.

Parsing uses ``ruamel.yaml`` — the project's YAML library. PyYAML is not
a dependency.
"""

from __future__ import annotations

import io
import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

# The leading ``---`` fenced block. Anchored at the start of the file:
# a ``---`` further down is a thematic break in the body, not metadata.
FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)

# Appended to parse failures by callers: the block-scalar form sidesteps
# the colon-in-a-plain-scalar trap entirely, which is by far the most
# common way one of these files becomes invalid YAML.
BLOCK_SCALAR_HINT = (
    "A ':' followed by a space inside an unquoted value (e.g. "
    "'description: Does things. Triggers: a, b') is invalid YAML. Use a "
    "block scalar instead:\n"
    "    description: |\n"
    "      Does things. Triggers: a, b"
)


class FrontmatterError(ValueError):
    """Base for frontmatter parse failures."""


class FrontmatterSyntaxError(FrontmatterError):
    """The frontmatter block is not valid YAML."""


class FrontmatterShapeError(FrontmatterError):
    """The frontmatter block parsed, but not to a mapping."""


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Split ``text`` into its frontmatter mapping and its body.

    Args:
        text: Full file contents.

    Returns:
        A ``(mapping, body)`` tuple. ``mapping`` is ``None`` when the
        document has no leading ``---`` block at all, in which case
        ``body`` is ``text`` unchanged — callers decide whether a missing
        block is fatal, because it is for a skill and for an agent but
        the wording differs.

    Raises:
        FrontmatterSyntaxError: If the block is not valid YAML.
        FrontmatterShapeError: If the block parses to something other
            than a mapping.
    """
    match = FRONTMATTER.match(text)
    if match is None:
        return None, text

    try:
        parsed = YAML(typ="safe").load(io.StringIO(match.group(1)))
    except YAMLError as exc:
        raise FrontmatterSyntaxError(f"invalid YAML frontmatter: {exc}") from exc

    if not isinstance(parsed, dict):
        raise FrontmatterShapeError(
            f"frontmatter that is not a YAML mapping (parsed as {type(parsed).__name__})"
        )

    return parsed, text[match.end() :]
