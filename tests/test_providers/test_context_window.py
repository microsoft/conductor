"""Tests for context window model lookup tables."""

from __future__ import annotations

import re

from conductor.providers.claude import _CLAUDE_CONTEXT_WINDOWS
from conductor.providers.copilot import _COPILOT_CONTEXT_WINDOWS


def _lookup_context_window(model: str) -> int | None:
    """Replicate the lookup logic from WorkflowEngine._get_context_window_for_agent."""
    for table in (_CLAUDE_CONTEXT_WINDOWS, _COPILOT_CONTEXT_WINDOWS):
        if model in table:
            return table[model]

    stripped = re.sub(r"-(\d{8}|latest|preview)$", "", model)
    for table in (_CLAUDE_CONTEXT_WINDOWS, _COPILOT_CONTEXT_WINDOWS):
        if stripped in table:
            return table[stripped]

    for table in (_CLAUDE_CONTEXT_WINDOWS, _COPILOT_CONTEXT_WINDOWS):
        for key, size in table.items():
            if stripped.startswith(key) or key.startswith(stripped):
                return size

    return None


class TestClaudeContextWindow:
    """Tests for Claude model context window lookups."""

    def test_exact_match(self) -> None:
        assert _lookup_context_window("claude-sonnet-4") == 200_000
        assert _lookup_context_window("claude-opus-4") == 200_000
        assert _lookup_context_window("claude-haiku-4.5") == 200_000

    def test_suffix_stripped(self) -> None:
        assert _lookup_context_window("claude-sonnet-4-20250514") == 200_000
        assert _lookup_context_window("claude-opus-4-20250514") == 200_000

    def test_prefix_match(self) -> None:
        assert _lookup_context_window("claude-sonnet-4-extended") == 200_000

    def test_unknown_model(self) -> None:
        assert _lookup_context_window("some-random-model") is None

    def test_latest_suffix(self) -> None:
        assert _lookup_context_window("claude-3-5-sonnet-latest") == 200_000


class TestCopilotContextWindow:
    """Tests for Copilot/OpenAI model context window lookups."""

    def test_exact_match(self) -> None:
        assert _lookup_context_window("gpt-4o") == 128_000
        assert _lookup_context_window("gpt-4o-mini") == 128_000

    def test_gpt4_legacy(self) -> None:
        assert _lookup_context_window("gpt-4") == 8_192

    def test_unknown_model(self) -> None:
        assert _lookup_context_window("totally-unknown") is None

    def test_claude_via_copilot_sdk(self) -> None:
        """Claude models routed through Copilot SDK resolve via Claude table."""
        assert _lookup_context_window("claude-haiku-4.5") == 200_000

    def test_dot_and_dash_notation(self) -> None:
        """Both claude-3.5 and claude-3-5 resolve correctly."""
        assert _lookup_context_window("claude-3.5-sonnet") == 200_000
        assert _lookup_context_window("claude-3-5-sonnet") == 200_000


class TestTableConsistency:
    """Cross-table consistency checks."""

    def test_all_claude_values_are_positive(self) -> None:
        for model, size in _CLAUDE_CONTEXT_WINDOWS.items():
            assert size > 0, f"{model} has non-positive context window"

    def test_all_copilot_values_are_positive(self) -> None:
        for model, size in _COPILOT_CONTEXT_WINDOWS.items():
            assert size > 0, f"{model} has non-positive context window"
