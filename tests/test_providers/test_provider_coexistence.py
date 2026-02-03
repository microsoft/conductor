"""Tests for provider coexistence.

Verifies that Claude and Copilot providers can coexist in the same
installation without conflicts. Includes both unit tests with mocks
and integration tests for real provider instances.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from conductor.providers.claude import ANTHROPIC_SDK_AVAILABLE


class TestProviderCoexistence:
    """Tests for Claude and Copilot provider coexistence."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_both_providers_can_be_imported(
        self,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
    ) -> None:
        """Test that both providers can be imported without conflicts."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_claude_client = Mock()
        mock_claude_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_claude_client

        # Import both providers
        from conductor.providers.claude import ClaudeProvider
        from conductor.providers.copilot import CopilotProvider

        # Verify both can be instantiated
        claude = ClaudeProvider()
        copilot = CopilotProvider()

        assert claude is not None
        assert copilot is not None
        assert type(claude).__name__ == "ClaudeProvider"
        assert type(copilot).__name__ == "CopilotProvider"

    @pytest.mark.asyncio
    async def test_factory_can_create_both_providers(self) -> None:
        """Test that factory can create both provider types."""
        from conductor.providers.factory import create_provider

        with (
            patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True),
            patch("conductor.providers.claude.AsyncAnthropic") as mock_anthropic,
            patch("conductor.providers.claude.anthropic") as mock_module,
        ):
            mock_module.__version__ = "0.77.0"
            mock_client = Mock()
            mock_anthropic.return_value = mock_client
            mock_client.close = AsyncMock()

            # Create Claude provider
            claude = await create_provider(provider_type="claude", validate=False)
            assert claude is not None
            assert type(claude).__name__ == "ClaudeProvider"

            # Create Copilot provider
            copilot = await create_provider(provider_type="copilot", validate=False)
            assert copilot is not None
            assert type(copilot).__name__ == "CopilotProvider"

            await claude.close()
            await copilot.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_both_providers_can_execute_concurrently(
        self,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
    ) -> None:
        """Test that both providers can execute concurrently without interference."""
        import asyncio

        # Setup Claude mock
        mock_anthropic_module.__version__ = "0.77.0"
        mock_claude_client = Mock()
        mock_claude_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_claude_response = Mock()
        mock_claude_response.content = [Mock(type="text", text='{"result": "Claude response"}')]
        mock_claude_response.model = "claude-3-5-sonnet-latest"
        mock_claude_response.usage = Mock(
            input_tokens=10, output_tokens=20, cache_creation_input_tokens=0
        )
        mock_claude_response.stop_reason = "end_turn"
        mock_claude_response.id = "msg_123"
        mock_claude_response.type = "message"
        mock_claude_response.role = "assistant"
        mock_claude_client.messages.create = AsyncMock(return_value=mock_claude_response)
        mock_claude_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_claude_client

        # Import providers
        from conductor.config.schema import AgentDef, OutputField
        from conductor.providers.claude import ClaudeProvider
        from conductor.providers.copilot import CopilotProvider

        # Setup Copilot mock handler
        def copilot_mock_handler(agent, prompt, context):
            return {"result": "Copilot response"}

        claude_provider = ClaudeProvider()
        copilot_provider = CopilotProvider(mock_handler=copilot_mock_handler)

        # Create agent with output schema
        agent = AgentDef(
            name="test",
            prompt="Test",
            output={"result": OutputField(type="string")},
        )

        async def run_claude():
            return await claude_provider.execute(agent, {}, "Claude test")

        async def run_copilot():
            return await copilot_provider.execute(agent, {}, "Copilot test")

        # Run concurrently
        claude_result, copilot_result = await asyncio.gather(run_claude(), run_copilot())

        # Verify both executed successfully
        assert "result" in claude_result.content
        assert "result" in copilot_result.content

        await claude_provider.close()
        await copilot_provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_claude_retry_config_independent_from_copilot(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that Claude RetryConfig doesn't conflict with Copilot's."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_claude_client = Mock()
        mock_claude_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_claude_client

        # Import both RetryConfigs
        from conductor.providers.claude import (
            RetryConfig as ClaudeRetryConfig,
        )
        from conductor.providers.copilot import (
            RetryConfig as CopilotRetryConfig,
        )

        # Create instances
        claude_config = ClaudeRetryConfig(max_attempts=3, base_delay=1.0)
        copilot_config = CopilotRetryConfig(max_attempts=5, base_delay=2.0)

        # Verify they are independent
        assert claude_config.max_attempts == 3
        assert copilot_config.max_attempts == 5
        assert claude_config.base_delay == 1.0
        assert copilot_config.base_delay == 2.0

        # Verify they have different defaults for parse recovery
        assert claude_config.max_parse_recovery_attempts == 2  # Claude: conservative
        assert copilot_config.max_parse_recovery_attempts == 5  # Copilot: more retries

    def test_claude_exceptions_dont_conflict_with_copilot(self) -> None:
        """Test that Claude-specific exception handling doesn't affect Copilot."""
        # This test verifies that both providers can handle their own exceptions
        # without namespace collisions
        from conductor.exceptions import ProviderError, ValidationError

        # Both providers should use the same base exceptions
        # This ensures consistent error handling across providers

        error1 = ProviderError("Claude error", status_code=400)
        error2 = ValidationError("Copilot validation error")

        assert isinstance(error1, ProviderError)
        assert isinstance(error2, ValidationError)
        assert error1.status_code == 400
        assert "Claude error" in str(error1)
        assert "Copilot validation error" in str(error2)


class TestProviderCoexistenceIntegration:
    """Integration tests for real provider coexistence (no mocks)."""

    @pytest.mark.skipif(not ANTHROPIC_SDK_AVAILABLE, reason="Anthropic SDK not installed")
    @pytest.mark.asyncio
    async def test_both_providers_can_be_created_and_closed(self) -> None:
        """Test creating and closing both provider types without validation."""
        from conductor.providers.factory import create_provider

        # Create both providers (without API validation)
        copilot = await create_provider("copilot", validate=False)
        claude = await create_provider("claude", validate=False)

        # Verify different types
        assert type(copilot).__name__ == "CopilotProvider"
        assert type(claude).__name__ == "ClaudeProvider"
        assert copilot is not claude

        # Close both
        await copilot.close()
        await claude.close()

    @pytest.mark.skipif(not ANTHROPIC_SDK_AVAILABLE, reason="Anthropic SDK not installed")
    @pytest.mark.asyncio
    async def test_multiple_claude_instances_with_different_configs(self) -> None:
        """Test multiple Claude instances with different configurations."""
        from conductor.providers.claude import ClaudeProvider

        claude1 = ClaudeProvider(
            model="claude-3-5-sonnet-latest",
            temperature=0.3,
            max_tokens=1000,
        )
        claude2 = ClaudeProvider(
            model="claude-3-haiku-20240307",
            temperature=0.7,
            max_tokens=2000,
        )

        # Verify independent configurations
        assert claude1._default_model == "claude-3-5-sonnet-latest"
        assert claude2._default_model == "claude-3-haiku-20240307"
        assert claude1._default_temperature == 0.3
        assert claude2._default_temperature == 0.7
        assert claude1._default_max_tokens == 1000
        assert claude2._default_max_tokens == 2000

        # Verify independent clients
        assert claude1._client is not None
        assert claude2._client is not None
        assert claude1._client is not claude2._client

        await claude1.close()
        await claude2.close()

    @pytest.mark.skipif(not ANTHROPIC_SDK_AVAILABLE, reason="Anthropic SDK not installed")
    @pytest.mark.asyncio
    async def test_provider_state_isolation(self) -> None:
        """Test that provider state is isolated between instances."""
        from conductor.providers.claude import ClaudeProvider

        claude1 = ClaudeProvider()
        claude2 = ClaudeProvider()

        # Verify independent retry history
        assert claude1.get_retry_history() == []
        assert claude2.get_retry_history() == []

        # Simulate state change in one instance
        claude1._retry_history.append({"attempt": 1, "error": "test"})

        # Verify isolation
        assert len(claude1.get_retry_history()) == 1
        assert len(claude2.get_retry_history()) == 0

        await claude1.close()
        await claude2.close()
