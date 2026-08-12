"""Tests for the MCPManager class.

This module tests:
- MCPManager initialization
- Tool name prefixing
- Tool retrieval methods
- Mock-based connection and tool execution
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest


class _TaskAffineMCP:
    def __init__(self) -> None:
        self.entries: list[asyncio.Task[Any] | None] = []
        self.exits: list[asyncio.Task[Any] | None] = []
        self.initialize_started = asyncio.Event()
        self.initialize_blocker: asyncio.Event | None = None
        self.initialize_error: RuntimeError | None = None
        self.exit_started = asyncio.Event()
        self.exit_blocker: asyncio.Event | None = None
        self.exit_error: RuntimeError | None = None

    @asynccontextmanager
    async def stdio(self, _params: Any) -> AsyncIterator[tuple[MagicMock, MagicMock]]:
        self.entries.append(asyncio.current_task())
        with anyio.CancelScope():
            try:
                yield MagicMock(), MagicMock()
            finally:
                self.exit_started.set()
                if self.exit_blocker is not None:
                    await self.exit_blocker.wait()
                self.exits.append(asyncio.current_task())
                if self.exit_error is not None:
                    raise self.exit_error

    @asynccontextmanager
    async def session(self, _read: Any, _write: Any) -> AsyncIterator[AsyncMock]:
        self.entries.append(asyncio.current_task())
        with anyio.CancelScope():
            try:
                session = AsyncMock()
                session.initialize = AsyncMock(side_effect=self._initialize)
                response = MagicMock()
                response.tools = []
                session.list_tools = AsyncMock(return_value=response)
                yield session
            finally:
                self.exits.append(asyncio.current_task())

    async def _initialize(self) -> None:
        self.initialize_started.set()
        if self.initialize_error is not None:
            raise self.initialize_error
        if self.initialize_blocker is not None:
            await self.initialize_blocker.wait()


@asynccontextmanager
async def _task_affine_manager() -> AsyncIterator[tuple[Any, _TaskAffineMCP]]:
    environment = _TaskAffineMCP()
    with (
        patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True),
        patch("conductor.mcp.manager.StdioServerParameters", return_value=MagicMock()),
        patch("conductor.mcp.manager.stdio_client", side_effect=environment.stdio),
        patch("conductor.mcp.manager.ClientSession", side_effect=environment.session),
    ):
        from conductor.mcp.manager import MCPManager

        yield MCPManager(), environment


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
        """Requirement: tool discovery accepts MCP 2.x snake-case model fields."""
        from pydantic import BaseModel, Field

        class MCP2Tool(BaseModel):
            name: str
            description: str | None = None
            input_schema: dict[str, Any] = Field(alias="inputSchema")

        mock_tool = MCP2Tool.model_validate(
            {
                "name": "search",
                "description": "Search the web",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            }
        )

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
        mock_stdio_context.__aenter__ = AsyncMock(
            return_value=(mock_read_stream, mock_write_stream)
        )
        mock_stdio_context.__aexit__ = AsyncMock(return_value=False)
        mock_client_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_client_session.__aexit__ = AsyncMock(return_value=False)

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
            assert tools[0]["input_schema"] == {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            }

            # Verify internal state
            assert "web-search" in manager.sessions
            assert "web-search" in manager.tools
            assert manager.tool_to_server["web-search__search"] == "web-search"
            await manager.close()

    async def test_connect_server_forwards_cwd(self, manager: Any) -> None:
        """Requirement: agent working_dir must reach the MCP server subprocess.

        ``connect_server(cwd=...)`` must forward the value to
        ``StdioServerParameters(cwd=...)`` so the stdio MCP server process
        is spawned in the agent's working directory (issue:
        agent-mcp-working-dir todo 4).
        """
        mock_list_tools_response = MagicMock()
        mock_list_tools_response.tools = []

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.list_tools = AsyncMock(return_value=mock_list_tools_response)

        mock_read_stream = MagicMock()
        mock_write_stream = MagicMock()
        mock_stdio_context = MagicMock()
        mock_client_session = MagicMock()
        mock_stdio_context.__aenter__ = AsyncMock(
            return_value=(mock_read_stream, mock_write_stream)
        )
        mock_stdio_context.__aexit__ = AsyncMock(return_value=False)
        mock_client_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_client_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True),
            patch("conductor.mcp.manager.StdioServerParameters") as mock_params_cls,
            patch(
                "conductor.mcp.manager.stdio_client",
                return_value=mock_stdio_context,
            ),
            patch(
                "conductor.mcp.manager.ClientSession",
                return_value=mock_client_session,
            ),
        ):
            await manager.connect_server(
                name="fs",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-filesystem"],
                cwd="/repo/worktree-a",
            )
            await manager.close()

        # StdioServerParameters must receive the cwd so the child process
        # starts in that directory.
        mock_params_cls.assert_called_once()
        assert mock_params_cls.call_args.kwargs["cwd"] == "/repo/worktree-a"

    async def test_connect_server_cwd_defaults_to_none(self, manager: Any) -> None:
        """Requirement: omitting cwd keeps legacy behavior (spawn in process cwd).

        When no cwd is given, ``StdioServerParameters`` must still be built
        with ``cwd=None`` so the MCP SDK spawns the server in the conductor
        process's own working directory (backward compatible).
        """
        mock_list_tools_response = MagicMock()
        mock_list_tools_response.tools = []

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.list_tools = AsyncMock(return_value=mock_list_tools_response)

        mock_read_stream = MagicMock()
        mock_write_stream = MagicMock()
        mock_stdio_context = MagicMock()
        mock_client_session = MagicMock()
        mock_stdio_context.__aenter__ = AsyncMock(
            return_value=(mock_read_stream, mock_write_stream)
        )
        mock_stdio_context.__aexit__ = AsyncMock(return_value=False)
        mock_client_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_client_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True),
            patch("conductor.mcp.manager.StdioServerParameters") as mock_params_cls,
            patch(
                "conductor.mcp.manager.stdio_client",
                return_value=mock_stdio_context,
            ),
            patch(
                "conductor.mcp.manager.ClientSession",
                return_value=mock_client_session,
            ),
        ):
            await manager.connect_server(name="fs", command="npx")
            await manager.close()

        mock_params_cls.assert_called_once()
        assert mock_params_cls.call_args.kwargs["cwd"] is None


class TestMCPManagerTaskAffinity:
    async def test_worker_connect_parent_close_uses_lifecycle_owner_task(self) -> None:
        # Requirement: parent shutdown exits MCP contexts in the task that entered them.
        async with _task_affine_manager() as (manager, environment):
            await asyncio.create_task(manager.connect_server(name="fs", command="server"))

            await manager.close()

        assert environment.exits == environment.entries

    async def test_failed_connection_cleans_up_in_lifecycle_owner_task(self) -> None:
        # Requirement: a failed MCP connection releases every entered context in its owner task.
        async with _task_affine_manager() as (manager, environment):
            environment.initialize_error = RuntimeError("initialization failed")

            with pytest.raises(RuntimeError, match="initialization failed"):
                await manager.connect_server(name="fs", command="server")
            await manager.close()

        assert environment.exits == environment.entries

    async def test_cancelled_connection_cleans_up_in_lifecycle_owner_task(self) -> None:
        # Requirement: cancelling a connection cannot orphan its task-affine MCP contexts.
        async with _task_affine_manager() as (manager, environment):
            environment.initialize_blocker = asyncio.Event()
            connection = asyncio.create_task(manager.connect_server(name="fs", command="server"))
            await environment.initialize_started.wait()

            connection.cancel()
            with pytest.raises(asyncio.CancelledError):
                await connection
            await manager.close()

        assert environment.exits == environment.entries

    async def test_parallel_managers_close_from_parent_task(self) -> None:
        # Requirement: parent shutdown safely closes managers created by parallel workers.
        async with _task_affine_manager() as (first_manager, environment):
            with patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True):
                from conductor.mcp.manager import MCPManager

                second_manager = MCPManager()
            await asyncio.gather(
                asyncio.create_task(first_manager.connect_server(name="one", command="server")),
                asyncio.create_task(second_manager.connect_server(name="two", command="server")),
            )

            await asyncio.gather(first_manager.close(), second_manager.close())

        assert environment.exits == environment.entries

    async def test_cancelled_connection_preserves_cancellation_when_cleanup_fails(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Requirement: cleanup errors must not replace cancellation requested by the caller.
        async with _task_affine_manager() as (manager, environment):
            environment.initialize_blocker = asyncio.Event()
            environment.exit_error = RuntimeError("cleanup failed")
            connection = asyncio.create_task(manager.connect_server(name="fs", command="server"))
            await environment.initialize_started.wait()

            connection.cancel()
            with pytest.raises(asyncio.CancelledError):
                await connection

        assert environment.exits == environment.entries
        assert "Error cancelling MCP connection 'fs': cleanup failed" in caplog.messages

    async def test_double_cancelled_connection_clears_bookkeeping(self) -> None:
        # Requirement: a repeated cancellation cannot leave a stale _connection_tasks entry.
        async with _task_affine_manager() as (manager, environment):
            environment.initialize_blocker = asyncio.Event()
            environment.exit_blocker = asyncio.Event()
            connection = asyncio.create_task(manager.connect_server(name="fs", command="server"))
            await environment.initialize_started.wait()

            connection.cancel()
            await environment.exit_started.wait()
            connection.cancel()
            await asyncio.sleep(0)
            assert not connection.done()

            environment.exit_blocker.set()
            with pytest.raises(asyncio.CancelledError):
                await connection

        assert environment.exits == environment.entries
        assert "fs" not in manager._connection_tasks
        assert "fs" not in manager._connection_stops

    async def test_cancelled_close_finishes_owner_task_cleanup(self) -> None:
        # Requirement: cancelling shutdown cannot interrupt task-affine owner cleanup.
        async with _task_affine_manager() as (manager, environment):
            environment.exit_blocker = asyncio.Event()
            await manager.connect_server(name="fs", command="server")

            closing = asyncio.create_task(manager.close())
            await environment.exit_started.wait()
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()

            environment.exit_blocker.set()
            await closing

        assert environment.exits == environment.entries
        assert manager.sessions == {}
        assert manager._connection_tasks == {}

    async def test_double_cancelled_close_still_finishes_cleanup(self) -> None:
        # Requirement: a repeated cancellation of close() cannot orphan teardown either.
        async with _task_affine_manager() as (manager, environment):
            environment.exit_blocker = asyncio.Event()
            await manager.connect_server(name="fs", command="server")

            closing = asyncio.create_task(manager.close())
            await environment.exit_started.wait()
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()

            environment.exit_blocker.set()
            await closing

        assert environment.exits == environment.entries
        assert manager.sessions == {}
        assert manager._connection_tasks == {}

    async def test_duplicate_server_name_is_rejected_without_replacing_owner(self) -> None:
        # Requirement: a duplicate server name cannot orphan the original lifecycle task.
        async with _task_affine_manager() as (manager, environment):
            await manager.connect_server(name="fs", command="server")

            with pytest.raises(RuntimeError, match="already connected or connecting"):
                await manager.connect_server(name="fs", command="server")
            await manager.close()

        assert environment.exits == environment.entries
