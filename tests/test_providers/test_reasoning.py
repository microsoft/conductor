"""Tests for the shared reasoning-effort helpers (#299)."""

from __future__ import annotations

from typing import get_args

import pytest

from conductor.providers.reasoning import (
    CLAUDE_ANSWER_HEADROOM_TOKENS,
    CLAUDE_EXTENDED_THINKING_OUTPUT_CAP,
    EFFORT_TO_BUDGET_TOKENS,
    ReasoningEffort,
    effort_to_budget_tokens,
    is_claude_thinking_model,
)


class TestEffortToBudgetTokensExhaustiveness:
    """The dict's completeness against the Literal isn't enforced by ``ty``
    (a ``Mapping[K, V]`` type hint only constrains *which* keys are legal,
    not how many must be present) — these tests are the load-bearing check.
    """

    def test_every_literal_member_has_a_budget_entry(self) -> None:
        assert set(EFFORT_TO_BUDGET_TOKENS) == set(get_args(ReasoningEffort))

    @pytest.mark.parametrize("effort", get_args(ReasoningEffort))
    def test_effort_to_budget_tokens_resolves_every_level(self, effort: ReasoningEffort) -> None:
        # Would raise ValueError (via the try/except in effort_to_budget_tokens)
        # if a level in the Literal were ever missing from the dict.
        budget = effort_to_budget_tokens(effort)
        assert isinstance(budget, int)
        assert budget > 0

    def test_effort_to_budget_tokens_rejects_unknown_level(self) -> None:
        with pytest.raises(ValueError, match="Unknown reasoning effort"):
            effort_to_budget_tokens("ultra")  # type: ignore[arg-type]


class TestMaxBudgetDerivation:
    """#299: ``max`` is deliberately pinned to the largest budget that keeps
    the default answer headroom under the extended-thinking output cap."""

    def test_max_budget_equals_cap_minus_headroom(self) -> None:
        assert (
            EFFORT_TO_BUDGET_TOKENS["max"]
            == CLAUDE_EXTENDED_THINKING_OUTPUT_CAP - CLAUDE_ANSWER_HEADROOM_TOKENS
        )
        assert EFFORT_TO_BUDGET_TOKENS["max"] == 59904

    def test_budget_ladder_is_monotonically_increasing(self) -> None:
        ladder = [EFFORT_TO_BUDGET_TOKENS[e] for e in ("low", "medium", "high", "xhigh", "max")]
        assert ladder == sorted(ladder)
        assert len(set(ladder)) == len(ladder)


class TestIsClaudeThinkingModel:
    def test_empty_model_id_is_false(self) -> None:
        assert is_claude_thinking_model("") is False

    def test_thinking_capable_prefixes_recognized(self) -> None:
        assert is_claude_thinking_model("claude-opus-4-20250514") is True
        assert is_claude_thinking_model("claude-sonnet-4-20250514") is True

    def test_non_thinking_model_rejected(self) -> None:
        assert is_claude_thinking_model("claude-3-5-sonnet-latest") is False
