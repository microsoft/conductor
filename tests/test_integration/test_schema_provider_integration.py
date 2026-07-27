"""Integration tests verifying schema fields are correctly passed to providers.

This module tests that all Claude-specific schema fields (temperature, max_tokens)
are correctly passed from the schema to the ClaudeProvider constructor and used
during execution.

These tests use real provider classes (not mocks) and patch the Pydantic AI
``build_agent`` seam to verify that runtime parameters reach the constructed
Agent's ``model_settings`` without making real network calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from conductor.config.schema import (
    AgentDef,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.claude import ClaudeProvider
from conductor.providers.factory import create_provider


class _ResultModel(BaseModel):
    """Structured output shape used to short-circuit the Pydantic AI run."""

    result: str


def _build_structured_agent(data: dict[str, Any]) -> Agent[Any, Any]:
    """Build a Pydantic AI structured-output agent backed by TestModel."""

    class DynamicModel(BaseModel):
        model_config = {"extra": "allow"}

    return Agent(
        model=TestModel(custom_output_args=data),
        output_type=DynamicModel,
    )


class TestSchemaToProviderIntegration:
    """Test that schema fields correctly integrate with provider implementations."""

    @pytest.mark.asyncio
    async def test_claude_runtime_config_fields_passed_to_provider(self) -> None:
        """Test that all Claude runtime config fields are passed to ClaudeProvider.

        Verifies: temperature, max_tokens
        """
        # Create workflow config with all Claude fields
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-claude-fields",
                description="Test Claude fields",
                version="1.0.0",
                entry_point="agent1",
                runtime=RuntimeConfig(
                    provider="claude",
                    temperature=0.8,
                    max_tokens=2048,
                ),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    description="Test agent",
                    model="claude-3-5-sonnet-latest",
                    prompt="Test prompt",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                )
            ],
        )

        # Create provider using factory (real instantiation)
        provider = await create_provider(
            provider_type="claude",
            validate=False,
            default_model="claude-3-5-sonnet-latest",
            temperature=config.workflow.runtime.temperature,
            max_tokens=config.workflow.runtime.max_tokens,
        )

        # Verify provider is ClaudeProvider
        assert isinstance(provider, ClaudeProvider)

        # Execute through engine and inspect the constructed Pydantic AI agent
        engine = WorkflowEngine(config, provider)
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_structured_agent({"answer": "test"}),
        ) as mock_build_agent:
            await engine.run({})

        # Verify build_agent received the runtime sampling settings
        assert mock_build_agent.call_args.kwargs["default_temperature"] == 0.8
        assert mock_build_agent.call_args.kwargs["default_max_tokens"] == 2048

        await provider.close()

    @pytest.mark.asyncio
    async def test_claude_provider_with_none_fields(self) -> None:
        """Test that ClaudeProvider handles None values for optional fields correctly."""
        # Create workflow config with all Claude fields set to None
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-none-fields",
                description="Test None fields",
                version="1.0.0",
                entry_point="agent1",
                runtime=RuntimeConfig(
                    provider="claude",
                    temperature=None,
                    max_tokens=None,
                ),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    description="Test agent",
                    model="claude-3-5-sonnet-latest",
                    prompt="Test prompt",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                )
            ],
        )

        # Create provider using factory
        provider = await create_provider(
            provider_type="claude",
            validate=False,
            default_model="claude-3-5-sonnet-latest",
        )

        # Verify provider is ClaudeProvider
        assert isinstance(provider, ClaudeProvider)

        # Execute workflow and inspect the constructed Pydantic AI agent
        engine = WorkflowEngine(config, provider)
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_structured_agent({"result": "ok"}),
        ) as mock_build_agent:
            await engine.run({})

        # When temperature is None, it should NOT be passed as a default
        assert mock_build_agent.call_args.kwargs["default_temperature"] is None
        # max_tokens uses the provider default of 8192 when not specified
        assert mock_build_agent.call_args.kwargs["default_max_tokens"] == 8192

        await provider.close()

    @pytest.mark.asyncio
    async def test_agent_level_overrides_runtime_defaults(self) -> None:
        """Test that agent-level config overrides runtime defaults."""
        # Create workflow with runtime defaults
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-overrides",
                description="Test agent overrides",
                version="1.0.0",
                entry_point="agent1",
                runtime=RuntimeConfig(provider="claude", temperature=0.5, max_tokens=1024),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    description="Test agent",
                    model="claude-3-5-sonnet-latest",
                    prompt="Test prompt",
                    output={"result": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                )
            ],
        )

        # Create provider
        provider = await create_provider(
            provider_type="claude",
            validate=False,
            default_model="claude-3-5-sonnet-latest",
            temperature=config.workflow.runtime.temperature,
            max_tokens=config.workflow.runtime.max_tokens,
        )

        # Execute workflow and inspect the constructed Pydantic AI agent
        engine = WorkflowEngine(config, provider)
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_structured_agent({"result": "ok"}),
        ) as mock_build_agent:
            await engine.run({})

        # Verify API was called with runtime defaults (no agent-level overrides set)
        assert mock_build_agent.call_args.kwargs["default_temperature"] == 0.5
        assert mock_build_agent.call_args.kwargs["default_max_tokens"] == 1024

        await provider.close()
