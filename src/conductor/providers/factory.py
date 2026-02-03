"""Factory for creating agent providers.

This module provides the create_provider factory function for instantiating
the appropriate AgentProvider based on the requested provider type.
"""

from __future__ import annotations

from typing import Any, Literal

from conductor.exceptions import ProviderError
from conductor.providers.base import AgentProvider
from conductor.providers.claude import ANTHROPIC_SDK_AVAILABLE, ClaudeProvider
from conductor.providers.copilot import CopilotProvider


async def create_provider(
    provider_type: Literal["copilot", "openai-agents", "claude"] = "copilot",
    validate: bool = True,
    mcp_servers: dict[str, Any] | None = None,
    default_model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> AgentProvider:
    """Factory function to create the appropriate provider.

    Creates and optionally validates an AgentProvider instance based on
    the requested provider type. Validation ensures the provider can
    connect to its backend before returning.

    Args:
        provider_type: Which SDK provider to use. Currently supports
            "copilot" and "claude".
        validate: Whether to validate connection on creation. If True,
            calls validate_connection() and raises ProviderError on failure.
        mcp_servers: MCP server configurations to pass to the provider.
            Both Copilot and Claude providers support MCP servers.
        default_model: Default model to use for agents that don't specify one.
        temperature: Default temperature for generation (0.0-1.0).
        max_tokens: Maximum output tokens.
        timeout: Request timeout in seconds.

    Returns:
        Configured AgentProvider instance.

    Raises:
        ProviderError: If provider type is unknown or connection validation fails.

    Example:
        >>> provider = await create_provider("copilot")
        >>> # Use provider for agent execution
        >>> await provider.close()
    """
    match provider_type:
        case "copilot":
            provider = CopilotProvider(
                mcp_servers=mcp_servers,
                model=default_model,
                temperature=temperature,
            )
        case "openai-agents":
            raise ProviderError(
                "OpenAI Agents provider not yet implemented",
                suggestion="Use 'copilot' provider for now",
            )
        case "claude":
            if not ANTHROPIC_SDK_AVAILABLE:
                raise ProviderError(
                    "Claude provider requires anthropic SDK",
                    suggestion="Install with: uv add 'anthropic>=0.77.0,<1.0.0'",
                )
            provider = ClaudeProvider(
                model=default_model,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout if timeout is not None else 600.0,
                mcp_servers=mcp_servers,
            )
        case _:
            raise ProviderError(
                f"Unknown provider: {provider_type}",
                suggestion="Valid providers are: copilot, openai-agents, claude",
            )

    if validate and not await provider.validate_connection():
        raise ProviderError(
            f"Failed to connect to {provider_type} provider",
            suggestion="Check your credentials and network connection",
        )

    return provider


class ProviderFactory:
    """Factory class for creating agent providers.

    This class provides a static method interface for provider creation,
    maintaining backward compatibility with tests that use the class-based API.

    Example:
        >>> provider = await ProviderFactory.create_provider(runtime_config)
        >>> await provider.close()
    """

    @staticmethod
    async def create_provider(
        runtime_config: Any,
        validate: bool = True,
    ) -> AgentProvider:
        """Create a provider from a RuntimeConfig object.

        Args:
            runtime_config: RuntimeConfig object containing provider settings.
            validate: Whether to validate connection on creation.

        Returns:
            Configured AgentProvider instance.

        Raises:
            ProviderError: If provider creation or validation fails.
        """
        provider_type = getattr(runtime_config, "provider", "copilot")
        default_model = getattr(runtime_config, "model", None)
        temperature = getattr(runtime_config, "temperature", None)
        max_tokens = getattr(runtime_config, "max_tokens", None)
        timeout = getattr(runtime_config, "timeout", None)

        return await create_provider(
            provider_type=provider_type,
            validate=validate,
            default_model=default_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
