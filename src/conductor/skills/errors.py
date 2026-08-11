"""Common base for skill resolution and manifest failures.

Lives in its own module so :mod:`conductor.skills.registry` and
:mod:`conductor.skills.frontmatter` can share it without importing each
other — registry already depends on frontmatter, and the reverse edge
would close a cycle.

Every skill failure descends from this, so a call site that can trigger
more than one kind has a single correct thing to catch. That matters
here: resolution and manifest errors originate in different modules but
reach the same handlers, and enumerating both by name is a step that is
easy to forget (``_check_skill_injection_budget`` did, and an unreadable
``references/*.md`` escaped ``conductor validate`` as a traceback).
"""

from __future__ import annotations


class SkillError(ValueError):
    """Base for every skill resolution or manifest failure.

    A ``ValueError`` subclass so these nest cleanly inside Pydantic field
    validation — ``AgentDef.validate_skills`` surfaces an unknown built-in
    name as an ordinary schema error rather than an opaque crash.
    """
