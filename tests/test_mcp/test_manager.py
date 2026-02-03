"""Tests for the MCPManager class.

This module tests:
- MCPManager initialization
- Tool name prefixing
- Tool retrieval methods
- Mock-based connection and tool execution
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestMCPManagerImport:
    """Tests for MCPManager import and SDK availability."""

    def test_mcp_sdk_available_flag_exists(self) -> None:
        """Test that MCP_SDK_AVAILABLE flag is defined."""
        from conductor.mcp.manager import MCP_SDK_AVAILABLE

        # Should be a boolean
        assert isinstance(MCP_SDK_AVAILABLE, bool)

    def test_mcp_manager_import_without_sdk(self) -> None:
        """Test that MCPManager can be imported even without SDK."""
        # This should not raise even if MCP SDK is not installed
        from conductor.mcp.manager import MCPManager

        # Class should exist
        assert MCPManager is not None


class TestMCPManagerInitialization:
    """Tests for MCPManager initialization."""

    @pytest.fixture
    def mock_mcp_available(self) -> Any:
        """Mock MCP SDK as available."""
        with patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True):
            yield

    def test_init_without_sdk_raises(self) -> None:
        """Test that initialization without SDK raises ImportError."""
        with patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", False):
            from conductor.mcp.manager import MCPManager

            with pytest.raises(ImportError, match="MCP SDK not installed"):
                MCPManager()

    def test_init_with_sdk_succeeds(self, mock_mcp_available: Any) -> None:
        """Test that initialization with SDK succeeds."""
        from conductor.mcp.manager import MCPManager

        manager = MCPManager()

        assert manager.sessions == {}
        assert manager.tools == {}
        assert manager.tool_to_server == {}


class TestMCPManagerToolMethods:
    """Tests for MCPManager tool methods (without actual MCP connections)."""

    @pytest.fixture
    def manager(self) -> Any:
        """Create a MCPManager with mocked SDK."""
        with patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True):
            from conductor.mcp.manager import MCPManager

            mgr = MCPManager()
            return mgr

    def test_get_all_tools_empty(self, manager: Any) -> None:
        """Test get_all_tools with no servers."""
        result = manager.get_all_tools()
        assert result == []

    def test_get_all_tools_with_data(self, manager: Any) -> None:
        """Test get_all_tools with pre-populated data."""
        manager.tools["server1"] = [
            {"name": "server1__tool1", "description": "Tool 1"},
            {"name": "server1__tool2", "description": "Tool 2"},
        ]
        manager.tools["server2"] = [
            {"name": "server2__tool3", "description": "Tool 3"},
        ]

        result = manager.get_all_tools()

        assert len(result) == 3
        assert {"name": "server1__tool1", "description": "Tool 1"} in result
        assert {"name": "server2__tool3", "description": "Tool 3"} in result

    def test_get_server_tools_existing(self, manager: Any) -> None:
        """Test get_server_tools for an existing server."""
        manager.tools["myserver"] = [
            {"name": "myserver__mytool", "description": "My Tool"},
        ]

        result = manager.get_server_tools("myserver")

        assert len(result) == 1
        assert result[0]["name"] == "myserver__mytool"

    def test_get_server_tools_nonexistent(self, manager: Any) -> None:
        """Test get_server_tools for a non-existent server."""
        result = manager.get_server_tools("nonexistent")
        assert result == []

    def test_has_servers_empty(self, manager: Any) -> None:
        """Test has_servers when no servers connected."""
        assert manager.has_servers() is False

    def test_has_servers_with_session(self, manager: Any) -> None:
        """Test has_servers when sessions exist."""
        manager.sessions["test"] = MagicMock()
        assert manager.has_servers() is True

    async def test_close_when_not_initialized(self, manager: Any) -> None:
        """Test close when manager was not initialized."""
        # Should not raise
        await manager.close()

        assert manager.sessions == {}
        assert manager.tools == {}


class TestMCPManagerToolPrefixing:
    """Tests for tool name prefixing convention."""

    def test_prefixed_name_format(self) -> None:
        """Test that tool names follow the {server}__{tool} format."""
        # This is a documentation test showing the expected format
        server_name = "web-search"
        tool_name = "search"
        prefixed = f"{server_name}__{tool_name}"

        assert prefixed == "web-search__search"
        assert "__" in prefixed
        assert prefixed.split("__", 1)[0] == server_name
        assert prefixed.split("__", 1)[1] == tool_name


class TestMCPManagerCallTool:
    """Tests for MCPManager.call_tool method."""

    @pytest.fixture
    def manager(self) -> Any:
        """Create a MCPManager with mocked SDK."""
        with patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True):
            from conductor.mcp.manager import MCPManager

            mgr = MCPManager()
            return mgr

    async def test_call_tool_unknown_tool(self, manager: Any) -> None:
        """Test call_tool with unknown tool name."""
        with pytest.raises(ValueError, match="Unknown tool"):
            await manager.call_tool("nonexistent__tool", {})

    async def test_call_tool_no_session(self, manager: Any) -> None:
        """Test call_tool when tool is registered but session is missing."""
        manager.tool_to_server["server__tool"] = "server"
        # But no session for "server"

        with pytest.raises(RuntimeError, match="No session for server"):
            await manager.call_tool("server__tool", {})

    async def test_call_tool_with_mock_session(self, manager: Any) -> None:
        """Test call_tool with a mocked session."""
        # Create mock TextContent
        mock_text_content = MagicMock()
        mock_text_content.text = "Tool result"

        # Create mock result
        mock_result = MagicMock()
        mock_result.content = [mock_text_content]
        mock_result.structuredContent = None

        # Create mock session
        mock_session = AsyncMock()
        mock_session.call_tool.return_value = mock_result

        # Set up manager state
        manager.tool_to_server["test-server__my-tool"] = "test-server"
        manager.sessions["test-server"] = mock_session

        # Patch TextContent isinstance check
        with patch("conductor.mcp.manager.TextContent", type(mock_text_content)):
            result = await manager.call_tool("test-server__my-tool", {"arg": "value"})

        assert result == "Tool result"
        mock_session.call_tool.assert_called_once_with("my-tool", arguments={"arg": "value"})


class TestMCPManagerConnectServer:
    """Tests for MCPManager.connect_server method (with mocked MCP client)."""

    @pytest.fixture
    def manager(self) -> Any:
        """Create a MCPManager with mocked SDK."""
        with patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True):
            from conductor.mcp.manager import MCPManager

            mgr = MCPManager()
            return mgr

    async def test_connect_server_mocked(self, manager: Any) -> None:
        """Test connect_server with fully mocked MCP client."""
        # Create mock tool
        mock_tool = MagicMock()
        mock_tool.name = "search"
        mock_tool.description = "Search the web"
        mock_tool.inputSchema = {"type": "object", "properties": {"query": {"type": "string"}}}

        # Create mock list_tools response
        mock_list_tools_response = MagicMock()
        mock_list_tools_response.tools = [mock_tool]

        # Create mock session
        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.list_tools = AsyncMock(return_value=mock_list_tools_response)

        # Create mock transport
        mock_read_stream = MagicMock()
        mock_write_stream = MagicMock()

        # Create a mock StdioServerParameters and stdio_client
        mock_server_params = MagicMock()
        mock_stdio_context = MagicMock()
        mock_client_session = MagicMock()

        # Patch all MCP SDK components
        with (
            patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True),
            patch(
                "conductor.mcp.manager.StdioServerParameters",
                return_value=mock_server_params,
            ),
            patch(
                "conductor.mcp.manager.stdio_client",
                return_value=mock_stdio_context,
            ),
            patch(
                "conductor.mcp.manager.ClientSession",
                return_value=mock_client_session,
            ),
        ):
            # Override the exit stack to return our mocked transport and session
            manager._exit_stack.enter_async_context = AsyncMock(
                side_effect=[
                    (mock_read_stream, mock_write_stream),
                    mock_session,
                ]
            )

            tools = await manager.connect_server(
                name="web-search",
                command="npx",
                args=["-y", "open-websearch@latest"],
                env={"MODE": "stdio"},
            )

        # Verify results
        assert len(tools) == 1
        assert tools[0]["name"] == "web-search__search"
        assert tools[0]["original_name"] == "search"
        assert tools[0]["server"] == "web-search"
        assert tools[0]["description"] == "Search the web"

        # Verify internal state
        assert "web-search" in manager.sessions
        assert "web-search" in manager.tools
        assert manager.tool_to_server["web-search__search"] == "web-search"
