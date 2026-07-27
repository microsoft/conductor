"""Tests for Claude provider error conditions and edge cases.

Note: Tests for top_p, top_k, stop_sequences, and metadata have been removed
as these were Claude-specific parameters not supported by both providers.
Tests for parse recovery and retryable error classification have been removed
because the legacy multi-turn recovery loop and SDK retry plumbing were
deleted in the Pydantic AI rewrite; equivalent behavior is covered by the
``test_pydantic_ai_*`` suite.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from conductor.config.schema import AgentDef, OutputField
from conductor.providers.claude import ClaudeProvider


class AnswerModel(BaseModel):
    """Structured output shape used by the edge-case tests."""

    answer: str


def _build_structured_agent(data: dict[str, Any]) -> Agent[Any, Any]:
    """Build a Pydantic AI structured-output agent backed by TestModel."""
    return Agent(
        model=TestModel(custom_output_args=data),
        output_type=AnswerModel,
    )

@pytest.fixture

def provider(monkeypatch: pytest.MonkeyPatch) -> ClaudeProvider:
    """Return a fresh ClaudeProvider instance using a dummy API key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return ClaudeProvider(api_key="test-key")


class TestClaudeEdgeCases:
    """Test edge cases and boundary conditions for the public execute contract."""

    @pytest.mark.asyncio
    async def test_empty_prompt(self) -> None:
        """Test handling of empty prompt."""
        provider = ClaudeProvider()

        agent = AgentDef(
            name="test",
            prompt="",  # Empty prompt
            output={"answer": OutputField(type="string")},
        )

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_structured_agent({"answer": "default"}),
        ) as mock_build_agent:
            result = await provider.execute(agent, {}, "")

        assert result.content["answer"] == "default"
        assert mock_build_agent.call_args.kwargs["rendered_prompt"] == ""

    @pytest.mark.asyncio
    async def test_special_characters_in_prompt(self) -> None:
        """Test handling of special characters in prompts."""
        provider = ClaudeProvider()

        special_prompt = "Test with special chars: \n\t\"'<>&{}[]\u0000"

        agent = AgentDef(
            name="test",
            prompt=special_prompt,
            output={"answer": OutputField(type="string")},
        )

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_structured_agent({"answer": "processed"}),
        ) as mock_build_agent:
            result = await provider.execute(agent, {}, special_prompt)

        assert result.content["answer"] == "processed"
        assert mock_build_agent.call_args.kwargs["rendered_prompt"] == special_prompt

    @pytest.mark.asyncio
    async def test_null_context(self) -> None:
        """Test handling of null/empty context."""
        provider = ClaudeProvider()

        agent = AgentDef(
            name="test",
            prompt="Test",
            output={"answer": OutputField(type="string")},
        )

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_structured_agent({"answer": "works"}),
        ):
            result = await provider.execute(agent, {}, "Test")

        assert result.content["answer"] == "works"
