"""MCP server manager for spawning and managing MCP server connections.

This module provides the MCPManager class that handles:
- Spawning MCP server processes using stdio transport
- Collecting tools from connected servers
- Executing tool calls and returning results
- Managing server lifecycle (connect, close)
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

logger = logging.getLogger(__name__)

# Try to import the MCP SDK
try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.types import TextContent

    MCP_SDK_AVAILABLE = True
except ImportError:
    MCP_SDK_AVAILABLE = False
    ClientSession = None  # type: ignore[misc, assignment]
    StdioServerParameters = None  # type: ignore[misc, assignment]
    stdio_client = None  # type: ignore[misc, assignment]
    TextContent = None  # type: ignore[misc, assignment]


class MCPManager:
    """Manages MCP server connections and tool execution.

    This class handles the lifecycle of MCP server processes, including:
    - Connecting to servers via stdio transport
    - Collecting available tools from servers
    - Routing tool calls to the appropriate server
    - Cleaning up connections on close

    Tool names are prefixed with the server name to avoid collisions:
    `{server_name}__{tool_name}` (e.g., "web-search__search")

    Example:
        >>> manager = MCPManager()
        >>> await manager.connect_server(
        ...     name="web-search",
        ...     command="npx",
        ...     args=["-y", "open-websearch@latest"],
        ...     env={"MODE": "stdio"}
        ... )
        >>> tools = manager.get_all_tools()
        >>> result = await manager.call_tool("web-search__search", {"query": "python"})
        >>> await manager.close()
    """

    def __init__(self) -> None:
        """Initialize the MCP manager.

        Raises:
            ImportError: If MCP SDK is not installed.
        """
        if not MCP_SDK_AVAILABLE:
            raise ImportError("MCP SDK not installed. Install with: uv add 'mcp>=1.0.0'")

        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, list[dict[str, Any]]] = {}  # server -> tools
        self.tool_to_server: dict[str, str] = {}  # prefixed_name -> server
        self._exit_stack = AsyncExitStack()
        self._initialized = False

    async def connect_server(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        """Connect to an MCP server and return its tools.

        Spawns the server process using stdio transport, initializes the
        session, and fetches the available tools.

        Args:
            name: Unique name for this server (used as tool prefix).
            command: Command to execute (e.g., "npx", "node").
            args: Command arguments.
            env: Environment variables for the server process.
            timeout: Connection timeout in seconds (not currently used).

        Returns:
            List of tool definitions from this server. Each tool dict contains:
            - name: Prefixed tool name ({server}__{tool})
            - description: Tool description
            - input_schema: JSON schema for tool input
            - server: Server name
            - original_name: Original tool name without prefix

        Raises:
            RuntimeError: If connection fails.
        """
        if not MCP_SDK_AVAILABLE:
            raise RuntimeError("MCP SDK not available")

        logger.info(f"Connecting to MCP server '{name}': {command} {args or []}")

        # Build server parameters
        server_params = StdioServerParameters(
            command=command,
            args=args or [],
            env=env,
        )

        try:
            # Enter the stdio_client context
            transport = await self._exit_stack.enter_async_context(stdio_client(server_params))
            read_stream, write_stream = transport

            # Create and initialize session
            session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()

            self.sessions[name] = session
            self._initialized = True

            # Fetch and store tools
            response = await session.list_tools()
            tools: list[dict[str, Any]] = []

            for tool in response.tools:
                # Prefix tool name with server name to avoid collisions
                prefixed_name = f"{name}__{tool.name}"
                tool_def = {
                    "name": prefixed_name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema,
                    "server": name,
                    "original_name": tool.name,
                }
                tools.append(tool_def)
                self.tool_to_server[prefixed_name] = name

            self.tools[name] = tools
            logger.info(
                f"Connected to MCP server '{name}' with {len(tools)} tools: "
                f"{[t['original_name'] for t in tools]}"
            )

            return tools

        except Exception as e:
            logger.error(f"Failed to connect to MCP server '{name}': {e}")
            raise RuntimeError(f"Failed to connect to MCP server '{name}': {e}") from e

    async def call_tool(
        self,
        prefixed_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Call a tool by its prefixed name.

        Routes the tool call to the appropriate MCP server and returns
        the result as a string.

        Args:
            prefixed_name: Full tool name with server prefix (e.g., "web-search__search").
            arguments: Tool input arguments matching the tool's input schema.

        Returns:
            Tool result as a string. Text content is returned directly;
            other content types are stringified.

        Raises:
            ValueError: If the tool is not found.
            RuntimeError: If tool execution fails.
        """
        server_name = self.tool_to_server.get(prefixed_name)
        if not server_name:
            raise ValueError(f"Unknown tool: {prefixed_name}")

        session = self.sessions.get(server_name)
        if not session:
            raise RuntimeError(f"No session for server: {server_name}")

        # Extract original tool name (remove server prefix)
        original_name = prefixed_name.split("__", 1)[1]

        logger.debug(
            f"Calling MCP tool '{original_name}' on server '{server_name}' "
            f"with arguments: {arguments}"
        )

        try:
            result = await session.call_tool(original_name, arguments=arguments)

            # Extract text content from result
            # The result.content is a list of content items
            text_parts: list[str] = []
            for content in result.content:
                if isinstance(content, TextContent):
                    text_parts.append(content.text)
                elif hasattr(content, "text"):
                    # Fallback for other text-like content
                    text_parts.append(str(content.text))
                else:
                    # For non-text content, stringify it
                    text_parts.append(str(content))

            response_text = "\n".join(text_parts) if text_parts else ""

            # If no text content, try structured content
            if not response_text and result.structuredContent:
                response_text = str(result.structuredContent)

            logger.debug(f"MCP tool '{original_name}' returned: {response_text[:200]}...")
            return response_text

        except Exception as e:
            logger.error(f"MCP tool call failed: {prefixed_name}: {e}")
            raise RuntimeError(f"MCP tool call failed: {prefixed_name}: {e}") from e

    def get_all_tools(self) -> list[dict[str, Any]]:
        """Get all tools from all connected servers.

        Returns:
            List of all tool definitions across all servers.
        """
        all_tools: list[dict[str, Any]] = []
        for tools in self.tools.values():
            all_tools.extend(tools)
        return all_tools

    def get_server_tools(self, server_name: str) -> list[dict[str, Any]]:
        """Get tools from a specific server.

        Args:
            server_name: Name of the server.

        Returns:
            List of tool definitions from the specified server,
            or empty list if server not found.
        """
        return self.tools.get(server_name, [])

    def has_servers(self) -> bool:
        """Check if any servers are connected.

        Returns:
            True if at least one server is connected.
        """
        return len(self.sessions) > 0

    async def close(self) -> None:
        """Close all server connections and clean up resources.

        This method should be called when the manager is no longer needed.
        It properly closes all stdio connections and cleans up internal state.
        """
        if not self._initialized:
            return

        logger.debug(f"Closing {len(self.sessions)} MCP server connection(s)")

        try:
            await self._exit_stack.aclose()
        except Exception as e:
            logger.warning(f"Error closing MCP connections: {e}")

        self.sessions.clear()
        self.tools.clear()
        self.tool_to_server.clear()
        self._initialized = False

        logger.debug("MCP manager closed")
