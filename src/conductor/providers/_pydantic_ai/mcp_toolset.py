"""Adapt Conductor's ``MCPManager`` to a Pydantic AI toolset.

This module will hold a thin wrapper around ``MCPManager`` that exposes
MCP tools to Pydantic AI agents using Conductor's existing naming,
truncation, and spill-to-file conventions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic_ai import Agent

    from conductor.config.schema import ToolOutputConfig
    from conductor.mcp.manager import MCPManager


class MCPManagerToolset:
    """A Pydantic AI toolset backed by Conductor's ``MCPManager``.

    Preserves the per-working-directory manager pool, ``{server}__{tool}``
    naming, and result truncation behavior of the existing Claude provider.
    """

    def __init__(
        self,
        manager: MCPManager,
        tool_names: list[str] | None,
        output_config: ToolOutputConfig | None,
    ) -> None:
        """Create a toolset adapter.

        Args:
            manager: The shared ``MCPManager`` instance for the working directory.
            tool_names: Optional allowlist of tool names. ``None`` grants all
                tools; ``[]`` grants none.
            output_config: Optional tool output truncation configuration.
        """
        raise NotImplementedError("Phase 4 will implement MCPManagerToolset")

    def attach_to(self, agent: Agent[Any, Any]) -> None:
        """Register the wrapped MCP tools on a Pydantic AI agent."""
        raise NotImplementedError("Phase 4 will implement attach_to")
