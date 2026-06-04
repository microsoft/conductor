"""Factory for creating agent providers.

This module provides the create_provider factory function for instantiating
the appropriate AgentProvider based on the requested provider type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from conductor.exceptions import ProviderError
from conductor.providers.base import AgentProvider
from conductor.providers.claude import ANTHROPIC_SDK_AVAILABLE, ClaudeProvider
from conductor.providers.claude_agent_sdk import (
    CLAUDE_AGENT_SDK_AVAILABLE,
    ClaudeAgentSdkProvider,
)
from conductor.providers.copilot import CopilotProvider, IdleRecoveryConfig
from conductor.providers.pydantic_deep import PYDANTIC_DEEP_AVAILABLE, PydanticDeepProvider
from conductor.providers.reasoning import ReasoningEffort

if TYPE_CHECKING:
    from conductor.config.schema import ProviderSettings


async def create_provider(
    provider_type: Literal["copilot", "openai-agents", "claude", "pydantic-deep", "claude-agent-sdk"] = "copilot",
    validate: bool = True,
    mcp_servers: dict[str, Any] | None = None,
    default_model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    max_session_seconds: float | None = None,
    max_agent_iterations: int | None = None,
    default_reasoning_effort: ReasoningEffort | None = None,
    skill_directories: list[str] | None = None,
    provider_settings: ProviderSettings | None = None,
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
        max_session_seconds: Maximum wall-clock duration for agent sessions.
        max_agent_iterations: Maximum tool-use iterations per agent execution.
        default_reasoning_effort: Workflow-wide default reasoning effort
            (``low`` / ``medium`` / ``high`` / ``xhigh``) applied when an agent
            does not specify its own ``reasoning.effort``.
        skill_directories: Directories to load skills from for agent sessions
            (Copilot provider only; ignored for other providers).  Paths must
            be absolute—resolve relative paths before calling this function.
        provider_settings: Structured ``runtime.provider`` settings. Only
            applied when ``provider_type == "copilot"`` and the settings
            opted into custom routing; ignored for all other providers
            (structured config for those providers is not yet implemented).

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
            idle_recovery_config = None
            if max_session_seconds is not None:
                idle_recovery_config = IdleRecoveryConfig(
                    max_session_seconds=max_session_seconds,
                )
            provider = CopilotProvider(
                mcp_servers=mcp_servers,
                model=default_model,
                temperature=temperature,
                idle_recovery_config=idle_recovery_config,
                max_agent_iterations=max_agent_iterations,
                default_reasoning_effort=default_reasoning_effort,
                skill_directories=skill_directories,
                provider_settings=provider_settings,
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
                max_agent_iterations=max_agent_iterations,
                max_session_seconds=max_session_seconds,
                default_reasoning_effort=default_reasoning_effort,
            )
        case "pydantic-deep":
            if not PYDANTIC_DEEP_AVAILABLE:
                raise ProviderError(
                    "pydantic-deep provider requires the pydantic-deep package",
                    suggestion="Install with: uv add 'pydantic-deep>=0.3.14'",
                )
            provider = PydanticDeepProvider(
                model=default_model,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout if timeout is not None else 600.0,
                mcp_servers=mcp_servers,
                max_agent_iterations=max_agent_iterations,
                default_reasoning_effort=default_reasoning_effort,
            )
        case "claude-agent-sdk":
            if not CLAUDE_AGENT_SDK_AVAILABLE:
                raise ProviderError(
                    "Claude Agent SDK provider requires claude-agent-sdk package",
                    suggestion="Install with: uv add 'claude-agent-sdk>=0.1.0'",
                )
            # claude-agent-sdk delegates the agentic loop to the underlying
            # `claude` CLI, which currently does not expose hooks for
            # workflow-level MCP servers, sampling temperature, or token
            # caps. Silently dropping any of these would either change
            # behavior (mcp tools the workflow expects suddenly missing)
            # or quietly violate user intent (temperature/max_tokens).
            # Refuse loudly until proper plumbing exists.
            if mcp_servers:
                raise ProviderError(
                    "claude-agent-sdk does not support workflow MCP servers "
                    f"(received {sorted(mcp_servers)!r}).",
                    suggestion=(
                        "Remove `runtime.mcp_servers` for this workflow, or "
                        "use the `copilot` or `claude` provider for agents "
                        "that need MCP tools."
                    ),
                )
            if temperature is not None:
                raise ProviderError(
                    f"claude-agent-sdk does not support `temperature` (received {temperature!r}).",
                    suggestion=(
                        "Remove `runtime.temperature` for workflows that use claude-agent-sdk."
                    ),
                )
            if max_tokens is not None:
                raise ProviderError(
                    f"claude-agent-sdk does not support `max_tokens` (received {max_tokens!r}).",
                    suggestion=(
                        "Remove `runtime.max_tokens` for workflows that use claude-agent-sdk."
                    ),
                )
            provider = ClaudeAgentSdkProvider(
                model=default_model,
                max_turns=max_agent_iterations,
                max_session_seconds=max_session_seconds,
            )
        case _:
            raise ProviderError(
                f"Unknown provider: {provider_type}",
                suggestion="Valid providers are: copilot, openai-agents, claude, pydantic-deep, claude-agent-sdk",
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
        provider_settings = getattr(runtime_config, "provider", None)
        # Support both the new ProviderSettings object and any legacy
        # string-typed mock that test code might still pass in.
        if hasattr(provider_settings, "name"):
            provider_type = provider_settings.name
        elif isinstance(provider_settings, str):
            provider_type = provider_settings
            provider_settings = None
        else:
            provider_type = "copilot"
            provider_settings = None

        default_model = getattr(runtime_config, "model", None)
        temperature = getattr(runtime_config, "temperature", None)
        max_tokens = getattr(runtime_config, "max_tokens", None)
        timeout = getattr(runtime_config, "timeout", None)
        max_session_seconds = getattr(runtime_config, "max_session_seconds", None)
        max_agent_iterations = getattr(runtime_config, "max_agent_iterations", None)
        default_reasoning_effort = getattr(runtime_config, "default_reasoning_effort", None)

        return await create_provider(
            provider_type=provider_type,
            validate=validate,
            default_model=default_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            max_session_seconds=max_session_seconds,
            max_agent_iterations=max_agent_iterations,
            default_reasoning_effort=default_reasoning_effort,
            provider_settings=provider_settings,
        )
