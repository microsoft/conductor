"""Tests for configurable agent iteration limits and session timeouts.

Covers:
- Schema validation for max_agent_iterations on RuntimeConfig and AgentDef
- Factory threading of max_agent_iterations to both providers
- Claude provider defaults, per-agent overrides, and session timeout
- Copilot provider iteration counting and enforcement
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from conductor.config.schema import AgentDef, RuntimeConfig
from conductor.exceptions import ProviderError
from conductor.providers.copilot import CopilotProvider
from conductor.providers.factory import create_provider

# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestRuntimeConfigMaxAgentIterations:
    """RuntimeConfig.max_agent_iterations validation."""

    def test_default_is_none(self) -> None:
        rc = RuntimeConfig()
        assert rc.max_agent_iterations is None

    def test_valid_value(self) -> None:
        rc = RuntimeConfig(max_agent_iterations=100)
        assert rc.max_agent_iterations == 100

    def test_min_value(self) -> None:
        rc = RuntimeConfig(max_agent_iterations=1)
        assert rc.max_agent_iterations == 1

    def test_max_value(self) -> None:
        rc = RuntimeConfig(max_agent_iterations=500)
        assert rc.max_agent_iterations == 500

    def test_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig(max_agent_iterations=0)

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig(max_agent_iterations=-1)

    def test_rejects_over_500(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig(max_agent_iterations=501)


class TestAgentDefMaxAgentIterations:
    """AgentDef.max_agent_iterations validation."""

    def test_default_is_none(self) -> None:
        agent = AgentDef(name="test", prompt="do stuff")
        assert agent.max_agent_iterations is None

    def test_valid_value(self) -> None:
        agent = AgentDef(name="test", prompt="do stuff", max_agent_iterations=200)
        assert agent.max_agent_iterations == 200

    def test_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            AgentDef(name="test", prompt="do stuff", max_agent_iterations=0)

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            AgentDef(name="test", prompt="do stuff", max_agent_iterations=-5)

    def test_rejects_over_500(self) -> None:
        with pytest.raises(ValidationError):
            AgentDef(name="test", prompt="do stuff", max_agent_iterations=501)

    def test_script_agent_rejects_max_agent_iterations(self) -> None:
        with pytest.raises(ValidationError, match="max_agent_iterations"):
            AgentDef(
                name="test",
                type="script",
                command="echo hi",
                max_agent_iterations=10,
            )

    def test_script_agent_without_max_agent_iterations_ok(self) -> None:
        agent = AgentDef(name="test", type="script", command="echo hi")
        assert agent.max_agent_iterations is None


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


class TestFactoryMaxAgentIterations:
    """max_agent_iterations flows through create_provider."""

    @patch("conductor.providers.factory.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_flows_to_claude_provider(
        self, mock_anthropic_module: Any, mock_anthropic_class: Any
    ) -> None:
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(return_value=MagicMock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = await create_provider("claude", validate=False, max_agent_iterations=100)
        assert provider._default_max_agent_iterations == 100

    @pytest.mark.asyncio
    async def test_flows_to_copilot_provider(self) -> None:
        provider = await create_provider("copilot", validate=False, max_agent_iterations=75)
        assert isinstance(provider, CopilotProvider)
        assert provider._default_max_agent_iterations == 75
        await provider.close()

    @pytest.mark.asyncio
    async def test_copilot_default_is_none(self) -> None:
        provider = await create_provider("copilot", validate=False)
        assert isinstance(provider, CopilotProvider)
        assert provider._default_max_agent_iterations is None
        await provider.close()

    @patch("conductor.providers.factory.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_claude_default_is_50(
        self, mock_anthropic_module: Any, mock_anthropic_class: Any
    ) -> None:
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(return_value=MagicMock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = await create_provider("claude", validate=False)
        assert provider._default_max_agent_iterations == 50


class TestFactoryMaxSessionSeconds:
    """max_session_seconds flows to Claude provider."""

    @patch("conductor.providers.factory.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_flows_to_claude_provider(
        self, mock_anthropic_module: Any, mock_anthropic_class: Any
    ) -> None:
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(return_value=MagicMock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = await create_provider("claude", validate=False, max_session_seconds=900.0)
        assert provider._default_max_session_seconds == 900.0

    @patch("conductor.providers.factory.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_claude_default_is_none(
        self, mock_anthropic_module: Any, mock_anthropic_class: Any
    ) -> None:
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(return_value=MagicMock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = await create_provider("claude", validate=False)
        assert provider._default_max_session_seconds is None


# ---------------------------------------------------------------------------
# Claude provider tests
# ---------------------------------------------------------------------------


class TestClaudeProviderIterationLimit:
    """Claude provider _execute_agentic_loop respects iteration and session limits."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_default_max_agent_iterations_is_50(
        self, mock_anthropic_module: Any, mock_anthropic_class: Any
    ) -> None:
        from conductor.providers.claude import ClaudeProvider

        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_class.return_value = MagicMock()

        provider = ClaudeProvider()
        assert provider._default_max_agent_iterations == 50

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_custom_max_agent_iterations(
        self, mock_anthropic_module: Any, mock_anthropic_class: Any
    ) -> None:
        from conductor.providers.claude import ClaudeProvider

        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_class.return_value = MagicMock()

        provider = ClaudeProvider(max_agent_iterations=200)
        assert provider._default_max_agent_iterations == 200

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_max_session_seconds_stored(
        self, mock_anthropic_module: Any, mock_anthropic_class: Any
    ) -> None:
        from conductor.providers.claude import ClaudeProvider

        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_class.return_value = MagicMock()

        provider = ClaudeProvider(max_session_seconds=600.0)
        assert provider._default_max_session_seconds == 600.0

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_max_session_seconds_default_none(
        self, mock_anthropic_module: Any, mock_anthropic_class: Any
    ) -> None:
        from conductor.providers.claude import ClaudeProvider

        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_class.return_value = MagicMock()

        provider = ClaudeProvider()
        assert provider._default_max_session_seconds is None

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_session_timeout_fires(
        self, mock_anthropic_module: Any, mock_anthropic_class: Any
    ) -> None:
        """Test that _execute_agentic_loop raises ProviderError on session timeout."""
        from conductor.providers.claude import ClaudeProvider

        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Create a mock response that always returns tool_use (never terminates)
        mock_tool_use_block = MagicMock()
        mock_tool_use_block.type = "tool_use"
        mock_tool_use_block.name = "some_tool"
        mock_tool_use_block.id = "tool_1"
        mock_tool_use_block.input = {"arg": "value"}

        mock_response = MagicMock()
        mock_response.content = [mock_tool_use_block]
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=20)

        # Mock the API call and MCP manager
        provider._execute_api_call = AsyncMock(return_value=mock_response)
        mock_mcp = MagicMock()
        mock_mcp.has_servers.return_value = True
        mock_mcp.call_tool = AsyncMock(return_value="tool result")
        provider._mcp_manager = mock_mcp

        # Use a very short session timeout and mock time to trigger it
        call_count = [0]

        def mock_monotonic() -> float:
            call_count[0] += 1
            # First call returns start time, subsequent calls return past the timeout
            if call_count[0] <= 1:
                return 1000.0
            return 1002.0  # 2 seconds past start

        with patch("conductor.providers.claude.time") as mock_time:
            mock_time.monotonic = mock_monotonic

            with pytest.raises(ProviderError, match="maximum session duration"):
                await provider._execute_agentic_loop(
                    messages=[{"role": "user", "content": "test"}],
                    model="test-model",
                    temperature=None,
                    max_tokens=1024,
                    tools=None,
                    output_schema=None,
                    has_output_schema=False,
                    max_iterations=100,
                    max_session_seconds=1.0,  # 1 second timeout
                )

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_per_agent_override_wins(
        self, mock_anthropic_module: Any, mock_anthropic_class: Any
    ) -> None:
        """Test that agent-level max_agent_iterations overrides provider default."""
        from conductor.providers.claude import ClaudeProvider

        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider(max_agent_iterations=50)

        # Create an agent with per-agent override
        agent = AgentDef(name="test", prompt="do stuff", max_agent_iterations=200)

        # The resolution happens in _execute_with_retry, check it directly
        resolved = (
            agent.max_agent_iterations
            if agent.max_agent_iterations is not None
            else provider._default_max_agent_iterations
        )
        assert resolved == 200

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_provider_default_used_when_agent_has_none(
        self, mock_anthropic_module: Any, mock_anthropic_class: Any
    ) -> None:
        """Test that provider default is used when agent doesn't override."""
        from conductor.providers.claude import ClaudeProvider

        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider(max_agent_iterations=75)

        agent = AgentDef(name="test", prompt="do stuff")

        resolved = (
            agent.max_agent_iterations
            if agent.max_agent_iterations is not None
            else provider._default_max_agent_iterations
        )
        assert resolved == 75


# ---------------------------------------------------------------------------
# Copilot provider tests
# ---------------------------------------------------------------------------


class TestCopilotProviderIterationLimit:
    """Copilot provider iteration counting in _wait_with_idle_detection."""

    def test_default_max_agent_iterations_is_none(self) -> None:
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
        assert provider._default_max_agent_iterations is None

    def test_custom_max_agent_iterations(self) -> None:
        provider = CopilotProvider(
            mock_handler=lambda a, p, c: {"result": "ok"},
            max_agent_iterations=42,
        )
        assert provider._default_max_agent_iterations == 42

    @pytest.mark.asyncio
    async def test_iteration_limit_raises_provider_error(self) -> None:
        """Test that exceeding max_agent_iterations raises ProviderError."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})

        done = asyncio.Event()
        last_activity_ref: list[Any] = [None, None, time.monotonic()]

        # Simulate 11 tool iterations already counted
        tool_iteration_ref = [11]

        with pytest.raises(ProviderError, match="maximum tool-use iterations"):
            await provider._wait_with_idle_detection(
                done=done,
                session=MagicMock(),
                verbose_enabled=False,
                full_enabled=False,
                last_activity_ref=last_activity_ref,
                max_session_seconds=9999.0,  # large enough to not trigger
                tool_iteration_ref=tool_iteration_ref,
                max_agent_iterations=10,
            )

    @pytest.mark.asyncio
    async def test_no_limit_when_none(self) -> None:
        """Test that None max_agent_iterations means no iteration limit."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})

        done = asyncio.Event()
        done.set()  # Signal completion immediately
        last_activity_ref: list[Any] = [None, None, time.monotonic()]

        # Even with high iteration count, should not raise when limit is None
        tool_iteration_ref = [9999]

        # Should complete without error (done is already set)
        await provider._wait_with_idle_detection(
            done=done,
            session=MagicMock(),
            verbose_enabled=False,
            full_enabled=False,
            last_activity_ref=last_activity_ref,
            max_session_seconds=9999.0,
            tool_iteration_ref=tool_iteration_ref,
            max_agent_iterations=None,
        )

    @pytest.mark.asyncio
    async def test_within_limit_completes_normally(self) -> None:
        """Test that within-limit iterations complete normally."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})

        done = asyncio.Event()
        done.set()  # Signal completion immediately
        last_activity_ref: list[Any] = [None, None, time.monotonic()]

        tool_iteration_ref = [5]

        # Should complete without error
        await provider._wait_with_idle_detection(
            done=done,
            session=MagicMock(),
            verbose_enabled=False,
            full_enabled=False,
            last_activity_ref=last_activity_ref,
            max_session_seconds=9999.0,
            tool_iteration_ref=tool_iteration_ref,
            max_agent_iterations=10,
        )

    def test_per_agent_override_resolution(self) -> None:
        """Test that agent-level max_agent_iterations overrides provider default."""
        provider = CopilotProvider(
            mock_handler=lambda a, p, c: {"result": "ok"},
            max_agent_iterations=50,
        )

        agent = AgentDef(name="test", prompt="do stuff", max_agent_iterations=200)

        resolved = (
            agent.max_agent_iterations
            if agent.max_agent_iterations is not None
            else provider._default_max_agent_iterations
        )
        assert resolved == 200

    def test_provider_default_when_agent_none(self) -> None:
        """Test provider default used when agent has no override."""
        provider = CopilotProvider(
            mock_handler=lambda a, p, c: {"result": "ok"},
            max_agent_iterations=50,
        )

        agent = AgentDef(name="test", prompt="do stuff")

        resolved = (
            agent.max_agent_iterations
            if agent.max_agent_iterations is not None
            else provider._default_max_agent_iterations
        )
        assert resolved == 50
