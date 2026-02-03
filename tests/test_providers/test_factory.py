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
