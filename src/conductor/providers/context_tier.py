"""Shared context-tier helpers for providers.

This module centralizes Conductor's provider-agnostic notion of a model's
context-window tier and how it resolves between the workflow-wide default
and a per-agent override.

- The **Copilot SDK** accepts a ``context_tier`` literal on ``create_session``
  (``ContextTier = Literal["default", "long_context"]``) to select a model's
  long-context (e.g. 1M-token) window.
- Other providers do not currently expose an equivalent knob, so they ignore
  the resolved value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef

ContextTier = Literal["default", "long_context"]


def resolve_context_tier(
    agent: AgentDef,
    runtime_default: ContextTier | None,
) -> ContextTier | None:
    """Resolve the effective context tier for an agent.

    Per-agent ``context_tier`` wins over the workflow-wide
    ``runtime.default_context_tier``. Returns ``None`` when neither is set,
    signalling that no context-tier parameter should be sent to the SDK.

    Args:
        agent: The agent whose effective context tier is being resolved.
        runtime_default: Workflow-wide default, or ``None``.

    Returns:
        The resolved ``ContextTier``, or ``None`` to send no value.
    """
    if agent.context_tier is not None:
        # #262: AgentDef widens ``context_tier`` to ``ContextTier | str`` so a
        # ``{{ ... }}`` template survives schema validation. By the time this
        # resolver runs (provider execute, after AgentExecutor renders +
        # validates the field) the value is a concrete, validated literal, so
        # narrowing back to ContextTier is sound.
        return cast(ContextTier, agent.context_tier)
    return runtime_default
