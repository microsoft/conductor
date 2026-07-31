"""Edge case tests for ClaudeProvider.

Tests cover:
- Temperature validation edge cases
- Empty/unusual responses
- Retry history exposure

Note: Tests for stop_sequences, metadata, top_p, and top_k have been removed
as these were Claude-specific parameters not supported by both providers.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.run import AgentRunResult

from conductor.config.schema import AgentDef
from conductor.exceptions import ValidationError
from conductor.providers._pydantic_ai.interrupt import RunOutcome
from conductor.providers.claude import ClaudeProvider


def _build_text_agent(text: str) -> Agent[Any, str]:
    """Build a Pydantic AI text agent backed by TestModel."""
    return Agent(model=TestModel(custom_output_text=text), output_type=str)


@pytest.fixture(autouse=True)
def _ensure_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a dummy API key so ClaudeProvider construction succeeds."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


class TestClaudeEdgeCases:
    """Tests for edge cases in Claude provider."""

    def test_temperature_validation_edge_cases(self) -> None:
        """Test temperature validation at boundaries."""
        # Valid boundaries
        provider = ClaudeProvider(api_key="test-key", temperature=0.0)
        assert provider._default_temperature == 0.0

        provider = ClaudeProvider(api_key="test-key", temperature=1.0)
        assert provider._default_temperature == 1.0

        # Invalid - below range
        with pytest.raises(ValidationError, match="Temperature must be between 0.0 and 1.0"):
            ClaudeProvider(api_key="test-key", temperature=-0.1)

        # Invalid - above range
        with pytest.raises(ValidationError, match="Temperature must be between 0.0 and 1.0"):
            ClaudeProvider(api_key="test-key", temperature=1.1)

    @pytest.mark.asyncio
    async def test_empty_response_handling(self) -> None:
        """Test handling of empty text response via the TestModel seam."""
        provider = ClaudeProvider(api_key="test-key")

        agent = AgentDef(name="test_agent", prompt="Test prompt")
        context: dict[str, Any] = {}
        rendered_prompt = "Test prompt"

        with (
            patch(
                "conductor.providers._pydantic_ai.agent_builder.build_agent",
                return_value=_build_text_agent(""),
            ),
            patch(
                "conductor.providers._pydantic_ai.interrupt.run_with_interrupt",
                return_value=RunOutcome(result=AgentRunResult(output="")),
            ),
        ):
            result = await provider.execute(agent, context, rendered_prompt)

        assert result.content == {"result": ""}

    def test_retry_history_exposure(self) -> None:
        """Test that retry history can be accessed for debugging."""
        provider = ClaudeProvider(api_key="test-key")

        # Initially empty
        history = provider.get_retry_history()
        assert history == []
        assert isinstance(history, list)

        # Ensure it returns a copy (not the internal list)
        history.append({"test": "data"})
        assert provider.get_retry_history() == []
