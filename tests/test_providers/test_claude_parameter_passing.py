"""End-to-end parameter passing tests for Claude provider.

Verifies that common parameters are forwarded from the factory to the
provider and from the provider into the constructed Pydantic AI Agent. The
deleted legacy tests inspected raw SDK keyword arguments; the Pydantic AI
rewrite maps those parameters to ``Agent.model_settings`` via
``build_agent``, so these tests assert the new seam.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from conductor.config.schema import AgentDef
from conductor.providers.claude import ClaudeProvider
from conductor.providers.factory import create_provider


class SimpleModel(BaseModel):
    """Structured output shape used to short-circuit the Pydantic AI run."""

    result: str


def _build_text_agent(text: str) -> Agent[Any, str]:
    """Build a Pydantic AI text agent backed by TestModel."""
    return Agent(model=TestModel(custom_output_text=text), output_type=str)


def _build_structured_agent(data: dict[str, Any]) -> Agent[Any, Any]:
    """Build a Pydantic AI structured-output agent backed by TestModel."""
    return Agent(
        model=TestModel(custom_output_args=data),
        output_type=SimpleModel,
    )


class TestClaudeParameterPassing:
    """Tests for end-to-end parameter passing through factory and provider."""

    @pytest.mark.asyncio
    @patch("conductor.providers.factory.ClaudeProvider")
    async def test_common_parameters_passed_from_factory(self, mock_claude_class: Mock) -> None:
        """Test that factory passes common parameters to provider."""
        mock_instance = Mock()
        mock_instance.validate_connection = AsyncMock(return_value=True)
        mock_claude_class.return_value = mock_instance

        await create_provider(
            provider_type="claude",
            default_model="claude-3-opus-20240229",
            temperature=0.7,
            max_tokens=4096,
            timeout=120.0,
        )

        mock_claude_class.assert_called_once_with(
            model="claude-3-opus-20240229",
            temperature=0.7,
            max_tokens=4096,
            timeout=120.0,
            mcp_servers=None,
            max_agent_iterations=None,
            max_session_seconds=None,
            default_reasoning_effort=None,
            auth_token=None,
            base_url=None,
            tool_output=None,
        )

    @pytest.mark.asyncio
    async def test_common_parameters_passed_to_pydantic_agent(self) -> None:
        """Provider passes common parameters into build_agent defaults."""
        provider = ClaudeProvider(
            api_key="test-key",
            model="claude-3-opus-20240229",
            temperature=0.7,
            max_tokens=4096,
        )

        with patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None), patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_text_agent("Test response"),
        ) as mock_build_agent:
            agent = AgentDef(
                name="test_agent",
                prompt="Test prompt",
                model="claude-3-sonnet-20240229",
            )
            await provider.execute(agent, {}, "Test prompt")

        kwargs = mock_build_agent.call_args.kwargs
        assert kwargs["agent"].model == "claude-3-sonnet-20240229"
        assert kwargs["default_temperature"] == 0.7
        assert kwargs["default_max_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_optional_parameters_not_passed_when_none(self) -> None:
        """When temperature is None, build_agent defaults to None."""
        provider = ClaudeProvider(api_key="test-key")

        with patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None), patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_text_agent("Test response"),
        ) as mock_build_agent:
            agent = AgentDef(name="test_agent", prompt="Test prompt")
            await provider.execute(agent, {}, "Test prompt")

        kwargs = mock_build_agent.call_args.kwargs
        assert kwargs["default_temperature"] is None
        assert kwargs["default_max_tokens"] == 8192

    @pytest.mark.asyncio
    async def test_agent_model_overrides_provider_model(self) -> None:
        """Agent-level model overrides the provider default in build_agent."""
        provider = ClaudeProvider(
            api_key="test-key",
            model="claude-3-5-sonnet-latest",
        )

        with patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None), patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_text_agent("Test response"),
        ) as mock_build_agent:
            agent = AgentDef(
                name="test_agent",
                prompt="Test prompt",
                model="claude-3-opus-20240229",
            )
            await provider.execute(agent, {}, "Test prompt")

        assert mock_build_agent.call_args.kwargs["agent"].model == "claude-3-opus-20240229"
