"""Provider registry for multi-provider workflow support.

This module provides the ProviderRegistry class for managing multiple
provider instances with lazy instantiation and caching.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from conductor.providers.base import AgentProvider
from conductor.providers.factory import create_provider

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef, WorkflowConfig


ProviderType = Literal["copilot", "openai-agents", "claude"]


class ProviderRegistry:
    """Manages multiple provider instances with lazy instantiation and caching.

    The ProviderRegistry enables multi-provider workflows where different
    agents can use different providers (e.g., Claude for research, Copilot
    for code generation). Providers are created lazily on first use and
    cached for subsequent agent executions.

    Example:
        >>> async with ProviderRegistry(config) as registry:
        ...     provider = await registry.get_provider(agent)
        ...     output = await executor.execute(agent, context)

    Key behaviors:
    - **Lazy creation**: Providers created on first agent that needs them
    - **Caching**: Same provider type reused across agents
    - **Lifecycle management**: Closes all providers at workflow end
    """

    def __init__(
        self,
        config: WorkflowConfig,
        mcp_servers: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the ProviderRegistry.

        Args:
            config: The workflow configuration.
            mcp_servers: MCP server configurations to pass to providers.
        """
        self._config = config
        self._mcp_servers = mcp_servers
        self._providers: dict[ProviderType, AgentProvider] = {}
        self._default_provider_type: ProviderType = config.workflow.runtime.provider

    @property
    def default_provider_type(self) -> ProviderType:
        """Get the default provider type from workflow config."""
        return self._default_provider_type

    def _get_provider_type_for_agent(self, agent: AgentDef) -> ProviderType:
        """Determine which provider type an agent should use.

        Args:
            agent: The agent definition.

        Returns:
            The provider type to use for this agent.
        """
        if agent.provider is not None:
            return agent.provider
        return self._default_provider_type

    async def get_provider(self, agent: AgentDef) -> AgentProvider:
        """Get the provider for an agent, creating it if necessary.

        This method resolves which provider the agent should use (based on
        agent.provider override or workflow default) and returns a cached
        or newly created provider instance.

        Args:
            agent: The agent definition.

        Returns:
            The AgentProvider instance for this agent.

        Raises:
            ProviderError: If provider creation fails.
        """
        provider_type = self._get_provider_type_for_agent(agent)
        return await self._get_or_create_provider(provider_type)

    async def _get_or_create_provider(self, provider_type: ProviderType) -> AgentProvider:
        """Get or create a provider of the specified type.

        Args:
            provider_type: The provider type to get or create.

        Returns:
            The AgentProvider instance.

        Raises:
            ProviderError: If provider creation fails.
        """
        if provider_type in self._providers:
            return self._providers[provider_type]

        # Create the provider with runtime config
        runtime = self._config.workflow.runtime
        provider = await create_provider(
            provider_type=provider_type,
            validate=True,
            mcp_servers=self._mcp_servers,
            default_model=runtime.default_model,
            temperature=runtime.temperature,
            max_tokens=runtime.max_tokens,
            timeout=runtime.timeout,
        )

        self._providers[provider_type] = provider
        return provider

    async def close(self) -> None:
        """Close all provider instances.

        This method should be called when the workflow completes to
        clean up all provider resources.
        """
        errors: list[Exception] = []
        for _provider_type, provider in self._providers.items():
            try:
                await provider.close()
            except Exception as e:
                # Collect errors but continue closing other providers
                errors.append(e)

        self._providers.clear()

        # Re-raise the first error if any occurred
        if errors:
            raise errors[0]

    async def __aenter__(self) -> ProviderRegistry:
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit - closes all providers."""
        await self.close()

    def get_active_providers(self) -> dict[ProviderType, AgentProvider]:
        """Get all currently active providers.

        Returns:
            Dictionary mapping provider type to provider instance.
        """
        return self._providers.copy()

    def is_provider_active(self, provider_type: ProviderType) -> bool:
        """Check if a provider type is currently active.

        Args:
            provider_type: The provider type to check.

        Returns:
            True if the provider is active, False otherwise.
        """
        return provider_type in self._providers
