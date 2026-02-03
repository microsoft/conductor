"""Providers module for Conductor.

This module defines the agent provider abstraction and implementations
for different LLM providers (Copilot SDK, Claude SDK, etc.).
"""

from conductor.providers.base import AgentOutput, AgentProvider
from conductor.providers.claude import ClaudeProvider
from conductor.providers.copilot import CopilotProvider
from conductor.providers.factory import create_provider

__all__ = [
    "AgentOutput",
    "AgentProvider",
    "ClaudeProvider",
    "CopilotProvider",
    "create_provider",
]
