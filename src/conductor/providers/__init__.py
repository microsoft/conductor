"""Providers module for Conductor.

This module defines the agent provider abstraction and implementations
for different LLM providers (Copilot SDK, Claude SDK, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from conductor.providers.base import AgentOutput, AgentProvider

if TYPE_CHECKING:
    from conductor.providers.claude import ClaudeProvider
    from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider
    from conductor.providers.copilot import CopilotProvider
    from conductor.providers.factory import create_provider

__all__ = [
    "AgentOutput",
    "AgentProvider",
    "ClaudeAgentSdkProvider",
    "ClaudeProvider",
    "CopilotProvider",
    "create_provider",
]


def __getattr__(name: str) -> Any:
    if name == "ClaudeProvider":
        from conductor.providers.claude import ClaudeProvider

        return ClaudeProvider
    if name == "ClaudeAgentSdkProvider":
        from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider

        return ClaudeAgentSdkProvider
    if name == "CopilotProvider":
        from conductor.providers.copilot import CopilotProvider

        return CopilotProvider
    if name == "create_provider":
        from conductor.providers.factory import create_provider

        return create_provider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
