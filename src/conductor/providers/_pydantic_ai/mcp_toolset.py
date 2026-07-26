"""Adapt Conductor's ``MCPManager`` to a Pydantic AI toolset.

This module provides a thin wrapper around ``MCPManager`` that exposes MCP
tools to Pydantic AI agents using Conductor's existing naming, truncation, and
spill-to-file conventions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic_ai import AbstractToolset, RunContext
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import ToolsetTool
from pydantic_core import SchemaValidator, core_schema

from conductor.mcp.manager import MCPManager

if TYPE_CHECKING:
    from pydantic_ai import Agent

    from conductor.config.schema import ToolOutputConfig


# A permissive validator that accepts any JSON-shaped dict. This matches how
# Pydantic AI's own MCP toolset validates arguments against the server-provided
# schema: validation is delegated to the MCP SDK, so we just need a placeholder
# that lets arbitrary keyword arguments through.
_ANY_ARGS_VALIDATOR: SchemaValidator = SchemaValidator(
    core_schema.dict_schema(
        keys_schema=core_schema.str_schema(),
        values_schema=core_schema.any_schema(),
    )
)


class MCPManagerToolset(AbstractToolset[Any]):
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
            output_config: Optional tool output truncation configuration. This is
                kept for API symmetry and potential future use; truncation itself is
                performed inside ``MCPManager.call_tool``.
        """
        self._manager = manager
        self._tool_names = tool_names
        self._output_config = output_config

    @property
    def id(self) -> str | None:
        """Return a stable toolset ID based on the manager identity."""
        return f"mcp_manager_{id(self._manager)}"

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        """Return tools from the MCP manager in ``{server}__{tool}`` form."""
        tools: dict[str, ToolsetTool[Any]] = {}
        for tool in self._manager.get_all_tools():
            name = tool["name"]
            if self._tool_names is not None and name not in self._tool_names:
                continue
            tools[name] = ToolsetTool(
                toolset=self,
                tool_def=ToolDefinition(
                    name=name,
                    description=tool.get("description", ""),
                    parameters_json_schema=tool.get("input_schema", {}),
                ),
                max_retries=0,
                args_validator=_ANY_ARGS_VALIDATOR,
            )
        return tools

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        """Execute a tool call through ``MCPManager.call_tool``.

        Errors are returned as a failed tool result (matching ClaudeProvider's
        ``is_error`` semantics) rather than propagating as unhandled exceptions
        through the agent loop.

        Args:
            name: The prefixed MCP tool name.
            tool_args: Validated arguments for the tool.
            ctx: The Pydantic AI run context (unused, but part of the protocol).
            tool: The tool definition that was selected.

        Returns:
            The tool result string on success.

        Raises:
            ToolFailed: When the underlying MCP tool call fails, so the agent loop
                records an ``outcome='failed'`` tool return that Anthropic maps to
                ``is_error=True``.
        """
        try:
            return await self._manager.call_tool(name, tool_args)
        except Exception as e:
            raise ToolFailed(f"Error executing tool {name!r}: {e}") from e

    def attach_to(self, agent: Agent[Any, Any]) -> None:
        """Register this toolset on an already-built Pydantic AI agent.

        Prefer passing the toolset into ``Agent(toolsets=...)`` at construction
        time. This method exists as a fallback and mutates the agent's internal
        toolset list, which is only safe before the first run.

        Args:
            agent: The Pydantic AI agent to register the toolset on.
        """
        # ``Agent._user_toolsets`` is the private list of explicitly registered toolsets.
        # Mutating it is only safe before the first run, which matches our intended use.
        user_toolsets = getattr(agent, "_user_toolsets", None)
        if isinstance(user_toolsets, list):
            user_toolsets.append(self)


def attach_mcp_toolset(
    agent: Agent[Any, Any],
    manager: MCPManager | None,
    tool_names: list[str] | None,
    output_config: ToolOutputConfig | None,
) -> None:
    """Attach an MCP toolset to a Pydantic AI agent when a manager is available.

    This is a convenience helper that creates a toolset and registers it on an
    agent that was already built without the ``toolsets`` argument. The canonical
    integration is to pass ``MCPManagerToolset`` directly into ``build_agent``
    via its ``toolsets`` parameter.
    """
    if manager is None:
        return
    MCPManagerToolset(manager, tool_names, output_config).attach_to(agent)
