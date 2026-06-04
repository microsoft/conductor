"""Unit tests for the provider factory."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from conductor.exceptions import ProviderError
from conductor.providers.copilot import CopilotProvider
from conductor.providers.factory import create_provider


class TestCreateProvider:
    """Tests for the create_provider factory function."""

    @pytest.mark.asyncio
    async def test_create_copilot_provider(self) -> None:
        """Test creating a Copilot provider."""
        # Use validate=False since Copilot CLI may not be installed in test env
        provider = await create_provider("copilot", validate=False)
        assert isinstance(provider, CopilotProvider)
        await provider.close()

    @pytest.mark.asyncio
    async def test_create_copilot_provider_default(self) -> None:
        """Test that copilot is the default provider."""
        # Use validate=False since Copilot CLI may not be installed in test env
        provider = await create_provider(validate=False)
        assert isinstance(provider, CopilotProvider)
        await provider.close()

    @pytest.mark.asyncio
    async def test_create_copilot_provider_no_validation(self) -> None:
        """Test creating a provider without validation."""
        provider = await create_provider("copilot", validate=False)
        assert isinstance(provider, CopilotProvider)
        await provider.close()

    @pytest.mark.asyncio
    async def test_create_openai_provider_raises(self) -> None:
        """Test that OpenAI provider raises ProviderError (not implemented)."""
        with pytest.raises(ProviderError) as exc_info:
            await create_provider("openai-agents")
        assert "not yet implemented" in str(exc_info.value)
        assert exc_info.value.suggestion is not None
        assert "copilot" in exc_info.value.suggestion

    @patch("conductor.providers.factory.ANTHROPIC_SDK_AVAILABLE", False)
    @pytest.mark.asyncio
    async def test_create_claude_provider_raises_when_sdk_not_available(self) -> None:
        """Test that Claude provider raises ProviderError when SDK not available."""
        with pytest.raises(ProviderError) as exc_info:
            await create_provider("claude")
        assert "anthropic SDK" in str(exc_info.value)
        assert exc_info.value.suggestion is not None

    @patch("conductor.providers.factory.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_create_claude_provider_success(
        self, mock_anthropic_module: Any, mock_anthropic_class: Any
    ) -> None:
        """Test that Claude provider can be created successfully."""
        from unittest.mock import AsyncMock

        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(return_value=MagicMock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = await create_provider("claude", validate=False)
        assert provider is not None
        assert provider.__class__.__name__ == "ClaudeProvider"

    @patch("conductor.providers.factory.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_create_claude_provider_with_config(
        self, mock_anthropic_module: Any, mock_anthropic_class: Any
    ) -> None:
        """Test that Claude provider accepts runtime config parameters."""
        from unittest.mock import AsyncMock

        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(return_value=MagicMock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = await create_provider(
            "claude",
            validate=False,
            default_model="claude-3-5-sonnet-latest",
            temperature=0.7,
            max_tokens=4096,
            timeout=300.0,
        )
        assert provider is not None
        assert provider.__class__.__name__ == "ClaudeProvider"
        # Verify config was passed - check provider attributes
        assert provider._default_model == "claude-3-5-sonnet-latest"
        assert provider._default_temperature == 0.7
        assert provider._default_max_tokens == 4096
        assert provider._timeout == 300.0

    @patch("conductor.providers.factory.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_create_claude_provider_with_validation(
        self, mock_anthropic_module: Any, mock_anthropic_class: Any
    ) -> None:
        """Test that Claude provider can be created with connection validation."""
        from unittest.mock import AsyncMock

        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = MagicMock()
        # Mock successful connection validation
        mock_client.models.list = AsyncMock(
            return_value=MagicMock(
                data=[
                    MagicMock(id="claude-3-5-sonnet-latest"),
                    MagicMock(id="claude-3-opus-latest"),
                ]
            )
        )
        mock_anthropic_class.return_value = mock_client

        provider = await create_provider("claude", validate=True)
        assert provider is not None
        assert provider.__class__.__name__ == "ClaudeProvider"
        # Verify models.list was called for validation
        # Called twice: once in __init__ and once in validate_connection
        assert mock_client.models.list.call_count == 2

    @pytest.mark.asyncio
    async def test_create_unknown_provider_raises(self) -> None:
        """Test that unknown provider types raise ProviderError."""
        with pytest.raises(ProviderError) as exc_info:
            await create_provider("unknown-provider")  # type: ignore
        assert "Unknown provider" in str(exc_info.value)
        assert "unknown-provider" in str(exc_info.value)
        assert exc_info.value.suggestion is not None
        assert "copilot" in exc_info.value.suggestion

    @pytest.mark.asyncio
    async def test_provider_error_includes_valid_providers(self) -> None:
        """Test that error message lists valid providers."""
        with pytest.raises(ProviderError) as exc_info:
            await create_provider("invalid")  # type: ignore
        suggestion = exc_info.value.suggestion
        assert suggestion is not None
        assert "copilot" in suggestion
        assert "openai-agents" in suggestion
        assert "claude" in suggestion


class TestProviderValidation:
    """Tests for provider connection validation."""

    @pytest.mark.asyncio
    async def test_validation_can_be_skipped(self) -> None:
        """Test that validation can be skipped."""
        provider = await create_provider("copilot", validate=False)
        assert isinstance(provider, CopilotProvider)
        await provider.close()


class TestMaxSessionSeconds:
    """Tests for max_session_seconds parameter in create_provider."""

    @pytest.mark.asyncio
    async def test_max_session_seconds_flows_to_copilot_idle_recovery_config(self) -> None:
        """Test that max_session_seconds is plumbed into CopilotProvider's IdleRecoveryConfig."""
        provider = await create_provider("copilot", validate=False, max_session_seconds=120.0)
        assert isinstance(provider, CopilotProvider)
        assert provider._idle_recovery_config.max_session_seconds == 120.0
        await provider.close()

    @pytest.mark.asyncio
    async def test_default_max_session_seconds_without_override(self) -> None:
        """Test that without max_session_seconds, the default (1800s) is used."""
        provider = await create_provider("copilot", validate=False)
        assert isinstance(provider, CopilotProvider)
        assert provider._idle_recovery_config.max_session_seconds == 1800.0
        await provider.close()

    @pytest.mark.asyncio
    async def test_max_session_seconds_preserves_other_idle_recovery_defaults(self) -> None:
        """Test that setting max_session_seconds doesn't change other defaults."""
        provider = await create_provider("copilot", validate=False, max_session_seconds=300.0)
        assert isinstance(provider, CopilotProvider)
        # max_session_seconds should be overridden
        assert provider._idle_recovery_config.max_session_seconds == 300.0
        # Other fields should retain their defaults
        assert provider._idle_recovery_config.idle_timeout_seconds == 90.0
        assert provider._idle_recovery_config.max_recovery_attempts == 5
        await provider.close()


class TestSkillDirectories:
    """Tests for skill_directories parameter in create_provider."""

    @pytest.mark.asyncio
    async def test_skill_directories_stored_in_copilot_provider(self) -> None:
        """Test that skill_directories are stored on the CopilotProvider."""
        skill_dirs = ["/path/to/skills", "/other/skills"]
        provider = await create_provider("copilot", validate=False, skill_directories=skill_dirs)
        assert isinstance(provider, CopilotProvider)
        assert provider._skill_directories == skill_dirs
        await provider.close()

    @pytest.mark.asyncio
    async def test_skill_directories_default_empty(self) -> None:
        """Test that skill_directories defaults to an empty list."""
        provider = await create_provider("copilot", validate=False)
        assert isinstance(provider, CopilotProvider)
        assert provider._skill_directories == []
        await provider.close()

    @pytest.mark.asyncio
    async def test_skill_directories_none_becomes_empty_list(self) -> None:
        """Test that None skill_directories is normalised to an empty list."""
        provider = await create_provider("copilot", validate=False, skill_directories=None)
        assert isinstance(provider, CopilotProvider)
        assert provider._skill_directories == []
        await provider.close()


class TestClaudeAgentSdkFactoryRejections:
    """Factory rejects workflow features claude-agent-sdk does not honor (#241 / A2).

    Silently dropping mcp_servers, temperature, or max_tokens at the factory
    boundary is a parity violation: agents that expect those features end up
    running with different behavior than declared. Refuse loudly until proper
    plumbing exists.
    """

    @pytest.mark.asyncio
    async def test_factory_rejects_mcp_servers(self) -> None:
        pytest.importorskip("claude_agent_sdk")
        with pytest.raises(ProviderError, match="does not support workflow MCP servers"):
            await create_provider(
                "claude-agent-sdk",
                validate=False,
                mcp_servers={"docs": {"command": "docs-server"}},
            )

    @pytest.mark.asyncio
    async def test_factory_rejects_temperature(self) -> None:
        pytest.importorskip("claude_agent_sdk")
        with pytest.raises(ProviderError, match="does not support `temperature`"):
            await create_provider(
                "claude-agent-sdk",
                validate=False,
                temperature=0.5,
            )

    @pytest.mark.asyncio
    async def test_factory_rejects_max_tokens(self) -> None:
        pytest.importorskip("claude_agent_sdk")
        with pytest.raises(ProviderError, match="does not support `max_tokens`"):
            await create_provider(
                "claude-agent-sdk",
                validate=False,
                max_tokens=4096,
            )

    @pytest.mark.asyncio
    async def test_factory_accepts_supported_params(self) -> None:
        pytest.importorskip("claude_agent_sdk")
        from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider

        provider = await create_provider(
            "claude-agent-sdk",
            validate=False,
            default_model="claude-sonnet-4-5",
            max_agent_iterations=20,
            max_session_seconds=600.0,
        )
        assert isinstance(provider, ClaudeAgentSdkProvider)
        assert provider._default_model == "claude-sonnet-4-5"
        assert provider._default_max_turns == 20
        assert provider._max_session_seconds == 600.0
        await provider.close()

    @pytest.mark.asyncio
    async def test_factory_accepts_empty_mcp_servers(self) -> None:
        """Empty dict / None mcp_servers should NOT raise — only non-empty values."""
        pytest.importorskip("claude_agent_sdk")
        from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider

        provider = await create_provider(
            "claude-agent-sdk",
            validate=False,
            mcp_servers={},
        )
        assert isinstance(provider, ClaudeAgentSdkProvider)
        await provider.close()

        provider = await create_provider(
            "claude-agent-sdk",
            validate=False,
            mcp_servers=None,
        )
        assert isinstance(provider, ClaudeAgentSdkProvider)
        await provider.close()
