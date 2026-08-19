"""Unit tests for the provider factory."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from conductor.config.schema import ProviderSettings, ToolOutputConfig
from conductor.exceptions import ProviderError
from conductor.providers.claude import ClaudeProvider
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

    @pytest.mark.asyncio
    async def test_copilot_provider_receives_default_context_tier(self) -> None:
        """default_context_tier is threaded into the Copilot provider."""
        provider = await create_provider(
            "copilot",
            validate=False,
            default_context_tier="long_context",
        )
        assert isinstance(provider, CopilotProvider)
        assert provider._default_context_tier == "long_context"
        await provider.close()

    @pytest.mark.asyncio
    async def test_copilot_provider_default_context_tier_none(self) -> None:
        """default_context_tier defaults to None on the Copilot provider."""
        provider = await create_provider("copilot", validate=False)
        assert isinstance(provider, CopilotProvider)
        assert provider._default_context_tier is None
        await provider.close()

    @pytest.mark.asyncio
    async def test_copilot_provider_receives_tool_output_config(self) -> None:
        """tool_output config is threaded into the Copilot provider."""
        config = ToolOutputConfig(max_chars=2000, spill_to_file=False, spill_dir="/tmp/out")
        provider = await create_provider(
            "copilot",
            validate=False,
            tool_output=config,
        )
        assert isinstance(provider, CopilotProvider)
        assert provider._tool_output_config.max_chars == 2000
        assert provider._tool_output_config.spill_to_file is False
        assert provider._tool_output_config.spill_dir == "/tmp/out"
        await provider.close()

    @pytest.mark.asyncio
    async def test_copilot_provider_tool_output_config_defaults_when_none(self) -> None:
        """tool_output defaults to a ToolOutputConfig instance when not supplied."""
        provider = await create_provider("copilot", validate=False)
        assert isinstance(provider, CopilotProvider)
        assert provider._tool_output_config.max_chars == 50000
        assert provider._tool_output_config.enabled is True
        await provider.close()

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
        assert provider._tool_output_config.max_chars == 50000

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

        tool_output = ToolOutputConfig(max_chars=2500, spill_dir="/tmp/claude-out")
        provider = await create_provider(
            "claude",
            validate=False,
            default_model="claude-3-5-sonnet-latest",
            temperature=0.7,
            max_tokens=4096,
            timeout=300.0,
            tool_output=tool_output,
        )
        assert provider is not None
        assert provider.__class__.__name__ == "ClaudeProvider"
        # Verify config was passed - check provider attributes
        assert provider._default_model == "claude-3-5-sonnet-latest"
        assert provider._default_temperature == 0.7
        assert provider._default_max_tokens == 4096
        assert provider._timeout == 300.0
        assert provider._tool_output_config.max_chars == 2500
        assert provider._tool_output_config.spill_dir == "/tmp/claude-out"

    @patch("conductor.providers.factory.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_create_claude_provider_extracts_api_key_from_settings(
        self,
        mock_anthropic_module: Any,
        mock_anthropic_class: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """YAML api_key for name='claude' is forwarded to ClaudeProvider._api_key."""
        from unittest.mock import AsyncMock

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(return_value=MagicMock(data=[]))
        mock_client.close = AsyncMock(return_value=None)
        mock_anthropic_class.return_value = mock_client

        settings = ProviderSettings(name="claude", api_key=SecretStr("sk-yaml"))
        provider = await create_provider(
            "claude",
            validate=False,
            provider_settings=settings,
        )
        assert isinstance(provider, ClaudeProvider)
        assert provider._api_key == "sk-yaml"
        await provider.close()

    @patch("conductor.providers.factory.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_create_claude_provider_forwards_api_key_to_sdk_client(
        self,
        mock_anthropic_module: Any,
        mock_anthropic_class: Any,
    ) -> None:
        """The YAML api_key reaches the Anthropic client as api_key, not as a bearer token."""
        from unittest.mock import AsyncMock

        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(return_value=MagicMock(data=[]))
        mock_client.close = AsyncMock(return_value=None)
        mock_anthropic_class.return_value = mock_client

        settings = ProviderSettings(name="claude", api_key=SecretStr("sk-yaml"))
        provider = await create_provider(
            "claude",
            validate=False,
            provider_settings=settings,
        )
        assert isinstance(provider, ClaudeProvider)
        assert mock_anthropic_class.call_count == 1
        assert mock_anthropic_class.call_args.kwargs["api_key"] == "sk-yaml"
        assert "auth_token" not in mock_anthropic_class.call_args.kwargs
        await provider.close()

    @patch("conductor.providers.factory.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_create_claude_provider_api_key_defaults_to_none(
        self,
        mock_anthropic_module: Any,
        mock_anthropic_class: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without YAML api_key and with no env key, ClaudeProvider._api_key remains None."""
        from unittest.mock import AsyncMock

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(return_value=MagicMock(data=[]))
        mock_client.close = AsyncMock(return_value=None)
        mock_anthropic_class.return_value = mock_client

        provider = await create_provider("claude", validate=False)
        assert isinstance(provider, ClaudeProvider)
        assert provider._api_key is None
        await provider.close()

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


class TestClaudeAgentSdkFactoryRejections:
    """Factory rejects workflow features claude-agent-sdk does not honor (#241 / A2).

    Silently dropping temperature or max_tokens at the factory boundary is a
    parity violation: agents that expect those features end up running with
    different behavior than declared. Refuse loudly until proper plumbing
    exists. ``mcp_servers`` IS supported as of #335 — it is translated to the
    SDK's own MCP config shapes and forwarded.
    """

    @pytest.mark.asyncio
    async def test_factory_forwards_mcp_servers(self) -> None:
        pytest.importorskip("claude_agent_sdk")
        from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider

        provider = await create_provider(
            "claude-agent-sdk",
            validate=False,
            mcp_servers={
                "docs": {
                    "type": "stdio",
                    "command": "docs-server",
                    "args": ["--port", "1234"],
                    # Dropped by the translation: no SDK equivalent.
                    "tools": ["*"],
                    "timeout": 5000,
                }
            },
        )
        assert isinstance(provider, ClaudeAgentSdkProvider)
        # Translated to the SDK shape, not stored verbatim.
        assert provider._mcp_servers == {
            "docs": {"type": "stdio", "command": "docs-server", "args": ["--port", "1234"]}
        }
        await provider.close()

    @pytest.mark.asyncio
    async def test_factory_keeps_per_server_tool_filter(self) -> None:
        """A narrowing per-server allowlist is honored via complement denial."""
        pytest.importorskip("claude_agent_sdk")
        provider = await create_provider(
            "claude-agent-sdk",
            validate=False,
            mcp_servers={"docs": {"type": "stdio", "command": "docs-server", "tools": ["search"]}},
        )
        assert provider._server_tool_filters == {"docs": {"search"}}

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


class TestHermesFactory:
    """Tests for the hermes factory branch."""

    @patch("conductor.providers.factory.HERMES_SDK_AVAILABLE", False)
    @pytest.mark.asyncio
    async def test_factory_raises_when_sdk_not_available(self) -> None:
        """Test that hermes provider raises ProviderError when SDK not available."""
        with pytest.raises(ProviderError, match="hermes-agent package"):
            await create_provider("hermes", validate=False)

    @patch("conductor.providers.factory.HERMES_SDK_AVAILABLE", True)
    @patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True)
    @pytest.mark.asyncio
    async def test_factory_creates_hermes_provider(self) -> None:
        """Test that hermes provider can be created successfully."""
        from conductor.providers.hermes import HermesProvider

        provider = await create_provider("hermes", validate=False)
        assert isinstance(provider, HermesProvider)
        await provider.close()

    @patch("conductor.providers.factory.HERMES_SDK_AVAILABLE", True)
    @patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True)
    @pytest.mark.asyncio
    async def test_factory_passes_all_config_to_hermes(self) -> None:
        """Test that factory forwards all runtime config to HermesProvider."""
        from conductor.providers.hermes import HermesProvider

        provider = await create_provider(
            "hermes",
            validate=False,
            default_model="anthropic/claude-sonnet-4",
            max_tokens=4096,
            temperature=0.7,
            max_agent_iterations=25,
            max_session_seconds=120.0,
            default_reasoning_effort="high",
        )
        assert isinstance(provider, HermesProvider)
        assert provider._default_model == "anthropic/claude-sonnet-4"
        assert provider._default_max_tokens == 4096
        assert provider._default_temperature == 0.7
        assert provider._default_max_agent_iterations == 25
        assert provider._default_max_session_seconds == 120.0
        assert provider._default_reasoning_effort == "high"
        await provider.close()

    @patch("conductor.providers.factory.HERMES_SDK_AVAILABLE", True)
    @patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True)
    @pytest.mark.asyncio
    async def test_factory_extracts_provider_settings_for_hermes(self) -> None:
        """Test that ProviderSettings with name='hermes' extracts base_url and api_key."""
        from conductor.providers.hermes import HermesProvider

        settings = ProviderSettings(
            name="hermes",
            base_url="http://localhost:8080",
            api_key=SecretStr("sk-test-key"),
            hermes_home="/tmp/hermes-test",
            hermes_toolsets=["filesystem", "web"],
            hermes_skip_memory=True,
            hermes_skip_context_files=False,
        )
        provider = await create_provider(
            "hermes",
            validate=False,
            provider_settings=settings,
        )
        assert isinstance(provider, HermesProvider)
        assert provider._base_url == "http://localhost:8080"
        assert provider._api_key == "sk-test-key"
        assert provider._hermes_home == "/tmp/hermes-test"
        assert provider._hermes_toolsets == ["filesystem", "web"]
        assert provider._skip_memory is True
        assert provider._skip_context_files is False
        await provider.close()

    @patch("conductor.providers.factory.HERMES_SDK_AVAILABLE", True)
    @patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True)
    @pytest.mark.asyncio
    async def test_factory_ignores_non_hermes_provider_settings(self) -> None:
        """Test that ProviderSettings with name != 'hermes' leaves base_url/api_key as None."""
        from conductor.providers.hermes import HermesProvider

        settings = ProviderSettings(name="copilot")
        provider = await create_provider(
            "hermes",
            validate=False,
            provider_settings=settings,
        )
        assert isinstance(provider, HermesProvider)
        assert provider._base_url is None
        assert provider._api_key is None
        assert provider._hermes_home is None
        assert provider._hermes_toolsets is None
        assert provider._skip_memory is None
        assert provider._skip_context_files is None
        await provider.close()
