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
    * **Claude Agent SDK** — the Claude Code plugin owning the skill is
      registered on the session (``--plugin-dir``) and the skill enabled
      by its ``<plugin>:<skill>`` name, also progressive. Skills the
      workflow did not declare are filtered out of the model's listing
      rather than inherited from the machine.
    * **Claude** and **Hermes** — eager preamble injection of
      ``SKILL.md`` plus ``references/*.md`` into the agent's rendered
      prompt. Neither has a native skill surface: the Anthropic API
      offers none without adopting the container/code-execution beta,
      and hermes runs its own internal toolsets. Injected size is
      bounded by ``runtime.skill_injection``, since the whole body is
      re-sent on every call and every retry.

A ``skills:`` entry is either a **built-in name** or a **filesystem
path** — see :func:`conductor.skills.registry.resolve_skills`. Conductor
ships one built-in skill, ``conductor``, sourced from
``plugins/conductor/skills/conductor/``. Discovering skills already
installed in the user's environment is tracked separately in issue #362;
discovery locations differ per provider, so a single switch would hand
different skill sets to different agents inside one run.
"""

from conductor.skills.errors import SkillError
from conductor.skills.frontmatter import (
    SkillFrontmatter,
    SkillManifestError,
    read_skill_frontmatter,
)
from conductor.skills.loader import BYTES_PER_TOKEN_ESTIMATE, load_skill_content
from conductor.skills.registry import (
    ResolvedSkill,
    SkillNotFoundError,
    SkillPlugin,
    SkillPluginError,
    WarningSink,
    get_skill_directory,
    is_path_entry,
    list_builtin_skills,
    resolve_skill_plugin,
    resolve_skills,
)

__all__ = [
    "BYTES_PER_TOKEN_ESTIMATE",
    "ResolvedSkill",
    "SkillError",
    "SkillFrontmatter",
    "SkillManifestError",
    "SkillNotFoundError",
    "SkillPlugin",
    "SkillPluginError",
    "WarningSink",
    "get_skill_directory",
    "is_path_entry",
    "list_builtin_skills",
    "load_skill_content",
    "read_skill_frontmatter",
    "resolve_skill_plugin",
    "resolve_skills",
]
