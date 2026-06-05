"""Conductor Skills — generalized opt-in capabilities for Conductor agents.

The skills system lets agents opt into bundled, reusable knowledge or
capabilities (e.g. the Conductor knowledge base, code-review rules,
domain-specific guidance) via the ``skills:`` field on ``AgentDef`` or
``RuntimeConfig``.

Skills follow the cross-cutting skill format used by GitHub Copilot CLI
and Anthropic Claude Code (a directory containing ``SKILL.md`` plus
optional ``references/*.md``). Conductor is a *consumer* of that format
alongside the Copilot CLI plugin — there is one canonical source of
truth per skill (no duplicated docs).

Provider-parity contract:
    *"The agent has access to the named skill."* Mechanism differs by
    provider (same pattern as MCP):

    * **Copilot** — native ``skill_directories`` on the SDK session.
      Skill becomes discoverable; the model loads it as relevant
      (progressive disclosure, token-efficient).
    * **Claude** — eager preamble injection of ``SKILL.md`` plus
      ``references/*.md`` into the agent's rendered prompt. The
      Anthropic API has no server-side skill surface without adopting
      the container/code-execution beta.

Phase 1 ships one built-in skill: ``conductor``, sourced from
``plugins/conductor/skills/conductor/``. Future phases will add
user-defined skill directories, executable skill resources, and
progressive disclosure via MCP.
"""

from conductor.skills.loader import load_skill_content
from conductor.skills.registry import (
    SkillNotFoundError,
    get_skill_directory,
    list_builtin_skills,
    resolve_skill_directories,
)

__all__ = [
    "SkillNotFoundError",
    "get_skill_directory",
    "list_builtin_skills",
    "load_skill_content",
    "resolve_skill_directories",
]
