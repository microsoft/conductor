"""MCP server manager for spawning and managing MCP server connections.

This module provides the MCPManager class that handles:
- Spawning MCP server processes using stdio transport
- Collecting tools from connected servers
- Executing tool calls and returning results
- Managing server lifecycle (connect, close)

Oversized tool results can be truncated to a per-result character limit. The full
text is optionally spilled to a temporary file so no data is lost. The
resulting marker is generated entirely inside this manager; callers detect
truncation by looking for the ``[output truncated:`` prefix in the trailing
part of the returned string.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from conductor.config.schema import ToolOutputConfig

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


# Marker constants. The generic hint is embedded by the manager and replaced
# with the fs hint by Claude's agentic loop when filesystem-like tools are
# available. No placeholder mechanism is used; callers replace the exact
# generic-hint constant string.
_TRUNCATION_MARKER_PREFIX = "[output truncated:"
_GENERIC_HINT = "The full output was truncated; refine the tool arguments to return less data."
_FS_HINT = (
    "The full output was saved to a file; read it with your filesystem tools if you need more."
)


def _sanitize_for_filename(value: str) -> str:
    """Replace characters that are unsafe in filenames with an underscore."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


class MCPManager:
    """Manages MCP server connections and tool execution.

    This class handles the lifecycle of MCP server processes, including:
    - Connecting to servers via stdio transport
    - Collecting available tools from servers
    - Routing tool calls to the appropriate server
    - Cleaning up connections on close
    - Optionally truncating oversized tool results and spilling the full text to disk

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

    def __init__(self, tool_output: ToolOutputConfig | None = None) -> None:
        """Initialize the MCP manager.

        Args:
            tool_output: MCP tool result output-size configuration. When None,
                the default configuration is used.

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
        self._tool_output = tool_output or ToolOutputConfig()

    async def connect_server(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        cwd: str | None = None,
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
            cwd: Working directory for the spawned server process. When None,
                the server inherits the conductor process's current working
                directory (pre-pool legacy behavior).

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
            cwd=cwd,
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
                if TextContent is not None and isinstance(content, TextContent):
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

            response_text = self._maybe_truncate_response(
                response_text,
                server_name=server_name,
                original_name=original_name,
            )

            logger.debug(f"MCP tool '{original_name}' returned: {response_text[:200]}...")
            return response_text

        except Exception as e:
            logger.error(f"MCP tool call failed: {prefixed_name}: {e}")
            raise RuntimeError(f"MCP tool call failed: {prefixed_name}: {e}") from e

    def _maybe_truncate_response(
        self,
        response_text: str,
        server_name: str,
        original_name: str,
    ) -> str:
        """Cap oversized tool results and optionally spill the full text to disk.

        The marker is generated entirely here. Callers detect truncation by
        looking for ``[output truncated:`` in the trailing part of the returned
        string and may replace the embedded generic hint with a filesystem hint
        when the agent has filesystem-like tools available.

        Args:
            response_text: The full assembled tool result text.
            server_name: Name of the MCP server that handled the tool.
            original_name: Original tool name without the server prefix.

        Returns:
            The (possibly truncated) result string with a plain-text marker at the end.
        """
        if not self._tool_output.enabled:
            return response_text

        max_chars = self._tool_output.max_chars
        if len(response_text) <= max_chars:
            return response_text

        original = len(response_text)
        kept = max_chars
        truncated = response_text[:kept]

        spill_path: str | None = None
        if self._tool_output.spill_to_file:
            spill_path = self._spill_full_output(
                full_text=response_text,
                server_name=server_name,
                original_name=original_name,
            )

        if spill_path:
            marker = (
                f"\n\n[{_TRUNCATION_MARKER_PREFIX[1:]} "
                f"{original} chars -> {kept} kept; "
                f"full output saved to: {spill_path}. {_GENERIC_HINT}]"
            )
        else:
            marker = (
                f"\n\n[{_TRUNCATION_MARKER_PREFIX[1:]} "
                f"{original} chars -> {kept} kept. {_GENERIC_HINT}]"
            )

        return f"{truncated}{marker}"

    def _spill_full_output(
        self,
        full_text: str,
        server_name: str,
        original_name: str,
    ) -> str | None:
        """Write the full tool result to a process-private temporary file.

        Files are created with mode 0o600 and may contain raw tool output
        (possibly including secrets). The caller is responsible for lifecycle.

        Args:
            full_text: The full tool result text to persist.
            server_name: Name of the MCP server that produced the result.
            original_name: Original tool name without the server prefix.

        Returns:
            The absolute path of the spill file, or None if writing failed.
        """
        spill_dir_str = self._tool_output.spill_dir
        if spill_dir_str:
            spill_dir = Path(spill_dir_str)
        else:
            spill_dir = Path(tempfile.gettempdir()) / "conductor" / "tool-output"

        try:
            spill_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            safe_server = _sanitize_for_filename(server_name)
            safe_tool = _sanitize_for_filename(original_name)
            unique = uuid.uuid4().hex[:8]
            filename = f"mcp-{safe_server}-{safe_tool}-{unique}.txt"
            path = spill_dir / filename

            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(full_text)
            except Exception:
                os.close(fd)
                raise
            return str(path)
        except OSError as e:
            logger.warning(
                "Failed to spill full MCP tool output to disk: %s",
                e,
            )
            return None

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
