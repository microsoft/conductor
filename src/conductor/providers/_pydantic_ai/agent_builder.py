"""Build a Pydantic AI Agent from a Conductor ``AgentDef``.

This module will hold the factory that maps Conductor agent configuration
(model, system prompt, output schema, tools, temperature, reasoning, etc.)
to a Pydantic AI ``Agent``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic_ai import Agent

    from conductor.config.schema import AgentDef


def build_agent(
    agent: AgentDef,
    system_prompt: str,
    rendered_prompt: str,
    tools: list[str] | None = None,
) -> Agent:
    """Build a Pydantic AI Agent from a Conductor agent definition.

    Args:
        agent: The Conductor agent definition.
        system_prompt: The rendered system prompt/instructions.
        rendered_prompt: The rendered user prompt (used as the initial task).
        tools: Optional tool allowlist; ``None`` grants all available tools.

    Returns:
        A configured Pydantic AI ``Agent`` ready to run.
    """
    raise NotImplementedError("Phase 3 will implement build_agent")
