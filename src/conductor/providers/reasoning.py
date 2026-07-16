"""Shared reasoning / extended-thinking helpers for providers.

This module centralizes the provider-agnostic mapping between Conductor's
discrete ``reasoning.effort`` levels and each SDK's native parameter shape:

- Copilot SDK uses a discrete ``reasoning_effort`` literal.
- Anthropic SDK uses a token budget passed via ``thinking={"type":"enabled",
  "budget_tokens": N}`` and is only valid on extended-thinking-capable models.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final, Literal, cast, get_args

from conductor.templating import is_jinja_template

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef

ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]

CLAUDE_EXTENDED_THINKING_OUTPUT_CAP: Final[int] = 64_000
"""Per-request output-token cap when Anthropic extended thinking is enabled.

Extended-thinking-capable Claude models accept up to this many total output
tokens (thinking + visible response) per request. Shared with
:meth:`ClaudeProvider._coerce_for_thinking`, which clamps ``max_tokens`` to
this value, and :meth:`ClaudeProvider.execute_dialog_turn`, which reuses
that same clamping helper for dialog turns.
"""

CLAUDE_ANSWER_HEADROOM_TOKENS: Final[int] = 4096
"""Minimum visible-answer headroom reserved above the thinking budget.

The Anthropic API requires ``max_tokens > budget_tokens``; this is the
margin :meth:`ClaudeProvider._coerce_for_thinking` adds on top of the
resolved thinking budget when computing the effective ``max_tokens``.
"""

EFFORT_TO_BUDGET_TOKENS: Final[Mapping[ReasoningEffort, int]] = {
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 32768,
    "max": CLAUDE_EXTENDED_THINKING_OUTPUT_CAP - CLAUDE_ANSWER_HEADROOM_TOKENS,
}
"""Mapping from Conductor effort level to Claude ``budget_tokens`` value.

The minimum supported by the Anthropic API is 1024; all values above sit
comfortably above that floor. ``max`` is derived from the two constants
above (``64000 - 4096 = 59904``): the largest budget that still leaves the
default answer headroom under the extended-thinking output cap enforced by
:meth:`ClaudeProvider._coerce_for_thinking` (and, via that same helper, by
:meth:`ClaudeProvider.execute_dialog_turn`).
"""

assert set(EFFORT_TO_BUDGET_TOKENS) == set(get_args(ReasoningEffort)), (
    "EFFORT_TO_BUDGET_TOKENS is missing an entry for a ReasoningEffort level; "
    "add a budget for every level in the Literal."
)

_CLAUDE_THINKING_MODEL_PREFIXES: tuple[str, ...] = (
    "claude-3-7-",
    "claude-opus-4",
    "claude-sonnet-4",
    "claude-haiku-4",
)
"""Model id prefixes that support extended thinking.

Anthropic introduced extended thinking with Claude 3.7 and continued it on
the Claude 4.x family. Older 3.5 / 3 / instant models are not supported.
"""


def effort_to_budget_tokens(effort: ReasoningEffort) -> int:
    """Translate a Conductor effort level into a Claude ``budget_tokens`` value.

    Args:
        effort: One of ``low``, ``medium``, ``high``, ``xhigh``, ``max``.

    Returns:
        The number of thinking-budget tokens to allocate.

    Raises:
        ValueError: If ``effort`` is not a recognized level.
    """
    try:
        return EFFORT_TO_BUDGET_TOKENS[effort]
    except KeyError as exc:
        raise ValueError(
            f"Unknown reasoning effort {effort!r}; expected one of "
            f"{sorted(EFFORT_TO_BUDGET_TOKENS)}"
        ) from exc


def is_claude_thinking_model(model_id: str) -> bool:
    """Return ``True`` when ``model_id`` supports Anthropic extended thinking.

    Matching is prefix-based to handle dated suffixes (e.g.
    ``claude-opus-4-20250514``) and ``-latest`` aliases.
    """
    if not model_id:
        return False
    lowered = model_id.lower()
    return any(lowered.startswith(prefix) for prefix in _CLAUDE_THINKING_MODEL_PREFIXES)


def resolve_reasoning_effort(
    agent: AgentDef,
    runtime_default: ReasoningEffort | None,
) -> ReasoningEffort | None:
    """Resolve the effective reasoning effort for an agent.

    Per-agent ``reasoning.effort`` wins over the workflow-wide
    ``runtime.default_reasoning_effort``. Returns ``None`` when neither is
    set, signalling that no reasoning parameter should be sent to the SDK.
    """
    if agent.reasoning is not None:
        # #262: ``ReasoningConfig`` widens ``effort`` to ``ReasoningEffort | str``
        # so a ``{{ ... }}`` / ``{% ... %}`` template survives schema validation.
        # By the time this resolver runs (provider execute, after AgentExecutor
        # renders + validates the field) the value is a concrete, validated
        # literal. Guard the invariant so an unrendered template raises here
        # rather than being cast straight to the SDK.
        effort = agent.reasoning.effort
        if is_jinja_template(effort):
            raise ValueError(f"reasoning.effort reached the provider unresolved: {effort!r}")
        return cast(ReasoningEffort, effort)
    return runtime_default
