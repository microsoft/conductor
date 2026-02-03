"""Unit tests for the ProviderRegistry."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from conductor.config.schema import AgentDef, RuntimeConfig, WorkflowConfig, WorkflowDef
from conductor.providers.base import AgentOutput, AgentProvider
from conductor.providers.registry import ProviderRegistry


class MockProvider(AgentProvider):
    """Mock provider for testing."""

    def __init__(self, provider_type: str = "test") -> None:
        self.provider_type = provider_type
        self.closed = False

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
    ) -> AgentOutput:
        return AgentOutput(content={"result": "mock"}, raw_response="mock")

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


def create_test_config(
    default_provider: str = "copilot",
    agents: list[AgentDef] | None = None,
) -> WorkflowConfig:
    """Create a minimal test config."""
    if agents is None:
        agents = [
            AgentDef(
                name="agent1",
                prompt="test prompt",
            )
        ]

    return WorkflowConfig(
        workflow=WorkflowDef(
            name="test-workflow",
            entry_point="agent1",
            runtime=RuntimeConfig(provider=default_provider),
        ),
        agents=agents,
        output={"result": "{{ agent1.output.result }}"},
    )


class TestProviderRegistryBasics:
    """Basic functionality tests for ProviderRegistry."""

    def test_initialization(self) -> None:
        """Test registry initializes correctly."""
        config = create_test_config(default_provider="copilot")
        registry = ProviderRegistry(config)

        assert registry.default_provider_type == "copilot"
        assert len(registry.get_active_providers()) == 0

    def test_default_provider_type(self) -> None:
        """Test default provider type is read from config."""
        config = create_test_config(default_provider="claude")
        registry = ProviderRegistry(config)

        assert registry.default_provider_type == "claude"

    def test_is_provider_active_when_empty(self) -> None:
        """Test is_provider_active returns False for inactive providers."""
        config = create_test_config()
        registry = ProviderRegistry(config)

        assert not registry.is_provider_active("copilot")
        assert not registry.is_provider_active("claude")


class TestProviderResolution:
    """Tests for provider type resolution logic."""

    def test_get_provider_type_uses_workflow_default(self) -> None:
        """Test agent without provider uses workflow default."""
        config = create_test_config(default_provider="copilot")
        registry = ProviderRegistry(config)

        agent = AgentDef(name="test", prompt="test", provider=None)
        provider_type = registry._get_provider_type_for_agent(agent)

        assert provider_type == "copilot"

    def test_get_provider_type_uses_agent_override(self) -> None:
        """Test agent with provider override uses that provider."""
        config = create_test_config(default_provider="copilot")
        registry = ProviderRegistry(config)

        agent = AgentDef(name="test", prompt="test", provider="claude")
        provider_type = registry._get_provider_type_for_agent(agent)

        assert provider_type == "claude"


class TestProviderCaching:
    """Tests for provider caching behavior."""

    @patch("conductor.providers.registry.create_provider")
    @pytest.mark.asyncio
    async def test_providers_are_cached(self, mock_create: MagicMock) -> None:
        """Test that providers are cached and reused."""
        mock_provider = MockProvider()
        mock_create.return_value = mock_provider

        config = create_test_config(default_provider="copilot")
        registry = ProviderRegistry(config)

        agent1 = AgentDef(name="agent1", prompt="test")
        agent2 = AgentDef(name="agent2", prompt="test")

        # Get provider for two agents with same provider type
        provider1 = await registry.get_provider(agent1)
        provider2 = await registry.get_provider(agent2)

        # Should be the same instance
        assert provider1 is provider2

        # create_provider should only be called once
        assert mock_create.call_count == 1

    @patch("conductor.providers.registry.create_provider")
    @pytest.mark.asyncio
    async def test_different_providers_created_separately(
        self, mock_create: MagicMock
    ) -> None:
        """Test that different provider types create different instances."""
        copilot_provider = MockProvider("copilot")
        claude_provider = MockProvider("claude")

        async def create_side_effect(**kwargs: Any) -> MockProvider:
            if kwargs.get("provider_type") == "copilot":
                return copilot_provider
            return claude_provider

        mock_create.side_effect = create_side_effect

        config = create_test_config(default_provider="copilot")
        registry = ProviderRegistry(config)

        agent_copilot = AgentDef(name="agent1", prompt="test", provider=None)
        agent_claude = AgentDef(name="agent2", prompt="test", provider="claude")

        provider1 = await registry.get_provider(agent_copilot)
        provider2 = await registry.get_provider(agent_claude)

        # Should be different instances
        assert provider1 is not provider2
        assert provider1.provider_type == "copilot"
        assert provider2.provider_type == "claude"

        # create_provider should be called twice
        assert mock_create.call_count == 2


class TestProviderLifecycle:
    """Tests for provider lifecycle management."""

    @patch("conductor.providers.registry.create_provider")
    @pytest.mark.asyncio
    async def test_close_closes_all_providers(self, mock_create: MagicMock) -> None:
        """Test that close() closes all active providers."""
        copilot_provider = MockProvider("copilot")
        claude_provider = MockProvider("claude")

        async def create_side_effect(**kwargs: Any) -> MockProvider:
            if kwargs.get("provider_type") == "copilot":
                return copilot_provider
            return claude_provider

        mock_create.side_effect = create_side_effect

        config = create_test_config(default_provider="copilot")
        registry = ProviderRegistry(config)

        # Create both providers
        agent_copilot = AgentDef(name="agent1", prompt="test", provider=None)
        agent_claude = AgentDef(name="agent2", prompt="test", provider="claude")
        await registry.get_provider(agent_copilot)
        await registry.get_provider(agent_claude)

        # Verify providers are active
        assert registry.is_provider_active("copilot")
        assert registry.is_provider_active("claude")

        # Close registry
        await registry.close()

        # Verify both providers were closed
        assert copilot_provider.closed
        assert claude_provider.closed

        # Verify registry is empty
        assert len(registry.get_active_providers()) == 0

    @patch("conductor.providers.registry.create_provider")
    @pytest.mark.asyncio
    async def test_context_manager_closes_providers(self, mock_create: MagicMock) -> None:
        """Test that async context manager closes providers on exit."""
        mock_provider = MockProvider()
        mock_create.return_value = mock_provider

        config = create_test_config()

        async with ProviderRegistry(config) as registry:
            agent = AgentDef(name="agent1", prompt="test")
            await registry.get_provider(agent)
            assert not mock_provider.closed

        # After context exit, provider should be closed
        assert mock_provider.closed

    @patch("conductor.providers.registry.create_provider")
    @pytest.mark.asyncio
    async def test_context_manager_closes_on_exception(self, mock_create: MagicMock) -> None:
        """Test that async context manager closes providers even on exception."""
        mock_provider = MockProvider()
        mock_create.return_value = mock_provider

        config = create_test_config()

        with pytest.raises(ValueError, match="test error"):
            async with ProviderRegistry(config) as registry:
                agent = AgentDef(name="agent1", prompt="test")
                await registry.get_provider(agent)
                raise ValueError("test error")

        # Provider should still be closed
        assert mock_provider.closed


class TestLazyInstantiation:
    """Tests for lazy provider instantiation."""

    @patch("conductor.providers.registry.create_provider")
    @pytest.mark.asyncio
    async def test_providers_created_lazily(self, mock_create: MagicMock) -> None:
        """Test that providers are not created until first use."""
        mock_provider = MockProvider()
        mock_create.return_value = mock_provider

        config = create_test_config()
        registry = ProviderRegistry(config)

        # No providers should be created yet
        assert mock_create.call_count == 0
        assert len(registry.get_active_providers()) == 0

        # Get provider for an agent
        agent = AgentDef(name="agent1", prompt="test")
        await registry.get_provider(agent)

        # Now provider should be created
        assert mock_create.call_count == 1
        assert len(registry.get_active_providers()) == 1

    @patch("conductor.providers.registry.create_provider")
    @pytest.mark.asyncio
    async def test_only_needed_providers_created(self, mock_create: MagicMock) -> None:
        """Test that only providers actually needed are created."""
        mock_provider = MockProvider()
        mock_create.return_value = mock_provider

        config = create_test_config(default_provider="copilot")
        registry = ProviderRegistry(config)

        # Only use copilot provider (no claude agents)
        agent = AgentDef(name="agent1", prompt="test", provider=None)
        await registry.get_provider(agent)

        # Only copilot should be created
        assert registry.is_provider_active("copilot")
        assert not registry.is_provider_active("claude")


class TestConfigPassing:
    """Tests for config passing to providers."""

    @patch("conductor.providers.registry.create_provider")
    @pytest.mark.asyncio
    async def test_runtime_config_passed_to_provider(self, mock_create: MagicMock) -> None:
        """Test that runtime config is passed when creating providers."""
        mock_provider = MockProvider()
        mock_create.return_value = mock_provider

        # Create config with runtime settings
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-workflow",
                entry_point="agent1",
                runtime=RuntimeConfig(
                    provider="copilot",
                    default_model="gpt-4",
                    temperature=0.7,
                    max_tokens=4096,
                    timeout=60.0,
                ),
            ),
            agents=[AgentDef(name="agent1", prompt="test")],
            output={"result": "test"},
        )

        registry = ProviderRegistry(config)
        agent = AgentDef(name="agent1", prompt="test")
        await registry.get_provider(agent)

        # Verify create_provider was called with config values
        mock_create.assert_called_once_with(
            provider_type="copilot",
            validate=True,
            mcp_servers=None,
            default_model="gpt-4",
            temperature=0.7,
            max_tokens=4096,
            timeout=60.0,
        )

    @patch("conductor.providers.registry.create_provider")
    @pytest.mark.asyncio
    async def test_mcp_servers_passed_to_provider(self, mock_create: MagicMock) -> None:
        """Test that MCP servers are passed when creating providers."""
        mock_provider = MockProvider()
        mock_create.return_value = mock_provider

        config = create_test_config()
        mcp_servers = {"test-server": {"type": "stdio", "command": "test"}}

        registry = ProviderRegistry(config, mcp_servers=mcp_servers)
        agent = AgentDef(name="agent1", prompt="test")
        await registry.get_provider(agent)

        # Verify MCP servers were passed
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["mcp_servers"] == mcp_servers
