"""Tests for the `aca` provider factory arm and capability registration (#284, E2)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from conductor.config.schema import ProviderSettings, ToolOutputConfig
from conductor.exceptions import ProviderError
from conductor.providers.capabilities import ProviderCapabilities, get_capabilities
from conductor.providers.factory import create_provider


class TestAcaCapabilities:
    """`get_capabilities("aca")` resolves the declared descriptor without instantiation."""

    def test_aca_registered_in_known_provider_names(self) -> None:
        from conductor.providers.capabilities import known_provider_names

        assert "aca" in known_provider_names()

    def test_get_capabilities_resolves_without_azure_identity_installed(self) -> None:
        """Resolving capabilities must not require azure-identity or any network access.

        `azure-identity` is gated behind the `aca` extra and may or may not be
        installed in a given test environment. A successful resolution here
        (regardless of that) proves the resolver only imports the provider
        module and reads the class-level CAPABILITIES attribute — it never
        instantiates the provider (which is the thing that actually requires
        `azure-identity`).
        """
        caps = get_capabilities("aca")
        assert isinstance(caps, ProviderCapabilities)

    def test_aca_capabilities_match_declared_table(self) -> None:
        """Declared capabilities match the design's capability table exactly."""
        caps = get_capabilities("aca")
        assert caps.tier == "experimental"
        assert caps.is_experimental is True
        assert caps.mcp_tools is True
        assert caps.workflow_tools_passthrough is True
        assert caps.streaming_events is True
        assert caps.agent_reasoning_events is True
        assert caps.reasoning_effort == ("low", "medium", "high", "xhigh", "max")
        assert caps.structured_output == "prompt_injection"
        assert caps.interrupt is True
        assert caps.max_session_seconds is True
        assert caps.checkpoint_resume is False
        assert caps.usage_tracking is True
        assert caps.concurrent_safe is True
        assert caps.working_dir is True

    def test_aca_working_dir_capability_true(self) -> None:
        """`aca` interprets working_dir container-relative but declares it True."""
        caps = get_capabilities("aca")
        assert caps.working_dir is True

    def test_aca_declared_limitations_lists_no_checkpoint_resume(self) -> None:
        caps = get_capabilities("aca")
        assert "no checkpoint resume" in caps.declared_limitations()


class TestAcaFactory:
    """`create_provider("aca", ...)` wiring, mirroring the claude/hermes availability guards."""

    @patch("conductor.providers.factory.AZURE_IDENTITY_AVAILABLE", False)
    @pytest.mark.asyncio
    async def test_factory_raises_when_azure_identity_not_available(self) -> None:
        """The `aca` extra may or may not be installed in this test env, so the
        unavailable-SDK branch is exercised explicitly via a patched flag
        rather than relying on the ambient environment (mirrors how the
        claude/hermes availability guards are tested elsewhere)."""
        settings = ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
        with pytest.raises(ProviderError, match="azure-identity"):
            await create_provider("aca", validate=False, provider_settings=settings)

    @patch("conductor.providers.factory.AZURE_IDENTITY_AVAILABLE", False)
    @pytest.mark.asyncio
    async def test_factory_error_includes_install_suggestion(self) -> None:
        settings = ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
        with pytest.raises(ProviderError) as exc_info:
            await create_provider("aca", validate=False, provider_settings=settings)
        assert exc_info.value.suggestion is not None
        assert "aca" in exc_info.value.suggestion

    @patch("conductor.providers.factory.AZURE_IDENTITY_AVAILABLE", True)
    @pytest.mark.asyncio
    async def test_factory_raises_when_provider_settings_missing(self) -> None:
        """aca requires structured provider_settings (pool_endpoint lives there)."""
        with pytest.raises(ProviderError, match="requires structured"):
            await create_provider("aca", validate=False)

    @patch("conductor.providers.factory.AZURE_IDENTITY_AVAILABLE", True)
    @pytest.mark.asyncio
    async def test_factory_raises_when_provider_settings_wrong_name(self) -> None:
        settings = ProviderSettings(name="copilot")
        with pytest.raises(ProviderError, match="requires structured"):
            await create_provider("aca", validate=False, provider_settings=settings)

    @patch("conductor.providers.factory.AZURE_IDENTITY_AVAILABLE", True)
    @patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True)
    @pytest.mark.asyncio
    async def test_factory_creates_aca_provider_when_available(self) -> None:
        """With azure-identity mocked as available, the factory constructs the provider."""
        from conductor.providers.aca import AcaRuntimeProvider

        settings = ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
        provider = await create_provider("aca", validate=False, provider_settings=settings)
        assert isinstance(provider, AcaRuntimeProvider)

    @patch("conductor.providers.factory.AZURE_IDENTITY_AVAILABLE", True)
    @patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True)
    @pytest.mark.asyncio
    async def test_factory_forwards_provider_settings_and_config(self) -> None:
        from conductor.providers.aca import AcaRuntimeProvider

        settings = ProviderSettings(
            name="aca",
            pool_endpoint="https://pool.example.com",
            api_version="2025-07-01",
        )
        mcp_servers = {"my-server": {"command": "npx", "args": ["some-mcp-server"]}}
        tool_output = ToolOutputConfig(enabled=False, max_chars=12345, spill_to_file=False)
        provider = await create_provider(
            "aca",
            validate=False,
            provider_settings=settings,
            mcp_servers=mcp_servers,
            default_model="gpt-4o",
            max_agent_iterations=25,
            max_session_seconds=120.0,
            default_reasoning_effort="high",
            tool_output=tool_output,
        )
        assert isinstance(provider, AcaRuntimeProvider)
        assert provider._provider_settings is settings
        assert provider._mcp_servers is mcp_servers
        assert provider._default_model == "gpt-4o"
        assert provider._default_max_agent_iterations == 25
        assert provider._default_max_session_seconds == 120.0
        assert provider._default_reasoning_effort == "high"
        assert provider._tool_output_config is tool_output

    @patch("conductor.providers.factory.AZURE_IDENTITY_AVAILABLE", False)
    @pytest.mark.asyncio
    async def test_factory_does_not_construct_provider_when_unavailable(self) -> None:
        """The provider class must never be instantiated when the SDK is missing."""
        with patch(
            "conductor.providers.factory.AcaRuntimeProvider", new_callable=MagicMock
        ) as mock_cls:
            settings = ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
            with pytest.raises(ProviderError):
                await create_provider("aca", validate=False, provider_settings=settings)
            mock_cls.assert_not_called()


class TestAcaRuntimeProviderInit:
    """Direct construction guards, independent of the factory."""

    def test_init_raises_when_azure_identity_unavailable(self) -> None:
        from conductor.providers.aca import AcaRuntimeProvider

        with patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", False):
            settings = ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
            with pytest.raises(ProviderError, match="azure-identity"):
                AcaRuntimeProvider(provider_settings=settings)

    def test_init_succeeds_when_azure_identity_available(self) -> None:
        from conductor.providers.aca import AcaRuntimeProvider

        with patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True):
            settings = ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
            provider = AcaRuntimeProvider(provider_settings=settings)
            assert provider._provider_settings is settings

    def test_class_declares_capabilities(self) -> None:
        from conductor.providers.aca import AcaRuntimeProvider

        assert isinstance(AcaRuntimeProvider.CAPABILITIES, ProviderCapabilities)
