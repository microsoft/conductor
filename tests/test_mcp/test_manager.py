"""Tests for the MCPManager class.

This module tests:
- MCPManager initialization
- Tool name prefixing
- Tool retrieval methods
- Mock-based connection and tool execution
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from pydantic import BaseModel, Field


class MCP2Tool(BaseModel):
    """MCP 2.x-shaped stand-in for Tool with snake-case field names."""

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(alias="inputSchema")


class MCP2CallToolResult(BaseModel):
    """MCP 2.x-shaped stand-in for CallToolResult with snake-case fields."""

    content: list[Any] = []
    structured_content: Any | None = Field(alias="structuredContent", default=None)
    isError: bool = False


def _make_mcp1_tool() -> Any:
    """Build a real mcp.types.Tool using its 1.x field name."""
    from mcp.types import Tool

    return Tool.model_validate(
        {
            "name": "search",
            "description": "Search the web",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        }
    )


def _make_mcp2_tool() -> MCP2Tool:
    """Build an MCP 2.x-shaped Tool stand-in."""
    return MCP2Tool.model_validate(
        {
            "name": "search",
            "description": "Search the web",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        }
    )


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
        self.exit_finished = asyncio.Event()
        self.exit_cancelled = asyncio.Event()
        self.ignore_exit_cancellation = False

    @asynccontextmanager
    async def stdio(self, _params: Any) -> AsyncIterator[tuple[MagicMock, MagicMock]]:
        self.entries.append(asyncio.current_task())
        with anyio.CancelScope():
            try:
                yield MagicMock(), MagicMock()
            finally:
                self.exit_started.set()
                if self.exit_blocker is not None:
                    while not self.exit_blocker.is_set():
                        try:
                            await self.exit_blocker.wait()
                        except asyncio.CancelledError:
                            self.exit_cancelled.set()
                            if not self.ignore_exit_cancellation:
                                raise
                self.exits.append(asyncio.current_task())
                self.exit_finished.set()
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


@pytest.fixture
def owner_waits(monkeypatch: pytest.MonkeyPatch) -> asyncio.Queue[None]:
    """Signal entry into each real owner observation, including cancellation re-entry."""
    original_wait = asyncio.wait
    entries: asyncio.Queue[None] = asyncio.Queue()

    async def observed_wait(
        tasks: Iterable[asyncio.Task[None]], *, timeout: float | None = None
    ) -> tuple[set[asyncio.Task[None]], set[asyncio.Task[None]]]:
        entries.put_nowait(None)
        return await original_wait(tasks, timeout=timeout)

    monkeypatch.setattr("conductor.mcp.manager.asyncio.wait", observed_wait)
    return entries


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

    async def test_call_tool_structured_content_both_shapes(self, manager: Any) -> None:
        """Requirement: call_tool reads structuredContent from both MCP 1.x and 2.x results."""
        from mcp.types import CallToolResult

        async def _run_case(result: Any) -> str:
            mock_session = AsyncMock()
            mock_session.call_tool.return_value = result

            manager.tool_to_server["test-server__my-tool"] = "test-server"
            manager.sessions["test-server"] = mock_session

            return await manager.call_tool("test-server__my-tool", {"arg": "value"})

        real_result = CallToolResult.model_validate(
            {
                "content": [],
                "structuredContent": {"answer": 42},
                "isError": False,
            }
        )
        assert await _run_case(real_result) == str({"answer": 42})

        mcp2_result = MCP2CallToolResult.model_validate(
            {
                "content": [],
                "structuredContent": {"answer": 42},
                "isError": False,
            }
        )
        assert await _run_case(mcp2_result) == str({"answer": 42})


class TestMCPManagerConnectServer:
    """Tests for MCPManager.connect_server method (with mocked MCP client)."""

    @pytest.fixture
    def manager(self) -> Any:
        """Create a MCPManager with mocked SDK."""
        with patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True):
            from conductor.mcp.manager import MCPManager

            mgr = MCPManager()
            return mgr

    @pytest.mark.parametrize(
        "make_tool",
        [
            pytest.param(_make_mcp1_tool, id="mcp1-real"),
            pytest.param(_make_mcp2_tool, id="mcp2-standin"),
        ],
    )
    async def test_connect_server_mocked(self, manager: Any, make_tool: Any) -> None:
        """Requirement: tool discovery reads both MCP 1.x and 2.x Tool field names."""
        mock_tool = make_tool()

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

    async def test_redacted_connection_error_logs_safe_metadata_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Requirement: with redact_errors=True (the deterministic `type: mcp`
        # step path), a connection failure logs only the safe server name at
        # ERROR — the raw exception (which can embed server-supplied stderr,
        # i.e. values the step's no-values policy excludes) must not appear in
        # the log record or its traceback. The raised RuntimeError still chains
        # the original exception for the caller's own diagnostic sink.
        async with _task_affine_manager() as (manager, environment):
            environment.initialize_error = RuntimeError("server stderr: SECRET_CANARY")

            with pytest.raises(RuntimeError, match="Failed to connect"):
                await manager.connect_server(name="fs", command="server", redact_errors=True)

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 1
        assert error_records[0].exc_info is None
        assert "SECRET_CANARY" not in caplog.text
        # The chained cause survives for the caller's diagnostic sink.
        assert manager.sessions == {}

    async def test_unredacted_connection_error_keeps_full_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Requirement: the default path (providers relying on it elsewhere) is
        # unchanged — the raw exception text and traceback are logged at ERROR.
        async with _task_affine_manager() as (manager, environment):
            environment.initialize_error = RuntimeError("plain failure text")

            with pytest.raises(RuntimeError, match="Failed to connect"):
                await manager.connect_server(name="fs", command="server")

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 1
        assert "plain failure text" in caplog.text
        assert error_records[0].exc_info is not None

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

    async def test_double_cancelled_connection_clears_bookkeeping(
        self, owner_waits: asyncio.Queue[None]
    ) -> None:
        # Requirement: a repeated cancellation cannot leave a stale _connection_tasks entry.
        async with _task_affine_manager() as (manager, environment):
            environment.initialize_blocker = asyncio.Event()
            environment.exit_blocker = asyncio.Event()
            connection = asyncio.create_task(manager.connect_server(name="fs", command="server"))
            await environment.initialize_started.wait()

            connection.cancel()
            await environment.exit_started.wait()
            await owner_waits.get()
            connection.cancel()
            await owner_waits.get()
            assert not connection.done()

            environment.exit_blocker.set()
            with pytest.raises(asyncio.CancelledError):
                await connection

        assert environment.exits == environment.entries
        assert "fs" not in manager._connection_tasks
        assert "fs" not in manager._connection_stops

    async def test_cancelled_close_finishes_owner_task_cleanup(
        self, owner_waits: asyncio.Queue[None]
    ) -> None:
        # Requirement: cancelling shutdown cannot interrupt task-affine owner cleanup.
        async with _task_affine_manager() as (manager, environment):
            environment.exit_blocker = asyncio.Event()
            await manager.connect_server(name="fs", command="server")

            closing = asyncio.create_task(manager.close())
            await environment.exit_started.wait()
            await owner_waits.get()
            closing.cancel()
            await owner_waits.get()
            assert not closing.done()

            environment.exit_blocker.set()
            await closing

        assert environment.exits == environment.entries
        assert manager.sessions == {}
        assert manager._connection_tasks == {}

    async def test_double_cancelled_close_still_finishes_cleanup(
        self, owner_waits: asyncio.Queue[None]
    ) -> None:
        # Requirement: a repeated cancellation of close() cannot orphan teardown either.
        async with _task_affine_manager() as (manager, environment):
            environment.exit_blocker = asyncio.Event()
            await manager.connect_server(name="fs", command="server")

            closing = asyncio.create_task(manager.close())
            await environment.exit_started.wait()
            await owner_waits.get()
            closing.cancel()
            await owner_waits.get()
            closing.cancel()
            await owner_waits.get()
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


class TestMCPManagerStrictLifecycle:
    async def test_strict_close_joins_owner_and_seals_connections(self) -> None:
        # Requirement: strict success joins the actual owner and rejects subsequent launches.
        async with _task_affine_manager() as (manager, environment):
            await manager.connect_server(name="fs", command="server")
            owner_task = manager._connection_tasks["fs"]

            await manager.close(strict=True)

            assert owner_task.done()
            assert environment.exits == environment.entries
            assert manager.sessions == {}
            assert not manager._cleanup_uncertain
            await manager.close(strict=True)
            with pytest.raises(RuntimeError):
                await manager.connect_server(name="new", command="server")

    async def test_strict_close_without_attempts_succeeds(self) -> None:
        # Requirement: a manager with no attempted lifecycle has nothing to join.
        async with _task_affine_manager() as (manager, _):
            await manager.close(strict=True)
            assert not manager._cleanup_uncertain

    async def test_failed_startup_keeps_primary_and_failed_teardown_record(self) -> None:
        # Requirement: failed startup retains cleanup failure even with an empty successful pool.
        async with _task_affine_manager() as (manager, environment):
            primary = RuntimeError("initialization failed")
            cleanup = RuntimeError("teardown failed")
            environment.initialize_error = primary
            environment.exit_error = cleanup

            with pytest.raises(RuntimeError) as caught:
                await manager.connect_server(name="fs", command="server")

            assert caught.value.__cause__ is primary
            assert manager.sessions == manager._connection_tasks == {}
            owner = next(iter(manager._owners.values()))
            assert owner.cleanup_error is cleanup
            assert owner.resources
            await manager.close()
            for _ in range(2):
                with pytest.raises(RuntimeError):
                    await manager.close(strict=True)
            assert manager._cleanup_uncertain

    @pytest.mark.parametrize("cancel_startup", [False, True])
    async def test_primary_startup_failure_allows_strict_close_after_clean_unwind(
        self, cancel_startup: bool
    ) -> None:
        # Requirement: a primary startup failure or cancel is not uncertain after proven cleanup.
        async with _task_affine_manager() as (manager, environment):
            primary = RuntimeError("initialization failed")
            if cancel_startup:
                environment.initialize_blocker = asyncio.Event()
                connection = asyncio.create_task(
                    manager.connect_server(name="fs", command="server")
                )
                await environment.initialize_started.wait()
                connection.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await connection
            else:
                environment.initialize_error = primary
                with pytest.raises(RuntimeError) as caught:
                    await manager.connect_server(name="fs", command="server")
                assert caught.value.__cause__ is primary

            task, owner = next(iter(manager._owners.items()))
            assert task.done()
            assert owner.observed
            assert owner.cleanup_complete
            assert owner.cleanup_error is None
            assert owner.finished_at is not None
            assert owner.deadline is not None
            assert owner.finished_at <= owner.deadline
            assert environment.exits == environment.entries
            assert manager.sessions == manager._connection_tasks == {}

            await manager.close(strict=True)
            await manager.close(strict=True)

            assert not manager._cleanup_uncertain
            assert owner.resources == []

    async def test_cancelled_startup_keeps_failed_cleanup_sticky(self) -> None:
        # Requirement: startup cancellation remains primary while strict release sees failed unwind.
        async with _task_affine_manager() as (manager, environment):
            environment.initialize_blocker = asyncio.Event()
            environment.exit_error = RuntimeError("teardown failed")
            connection = asyncio.create_task(manager.connect_server(name="fs", command="server"))
            await environment.initialize_started.wait()

            connection.cancel()
            with pytest.raises(asyncio.CancelledError):
                await connection

            assert manager.sessions == manager._connection_tasks == {}
            with pytest.raises(RuntimeError):
                await manager.close(strict=True)
            assert manager._cleanup_uncertain

    async def test_legacy_warning_cannot_become_strict_success(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Requirement: successful connect and empty maps cannot hide a failed transport exit.
        async with _task_affine_manager() as (manager, environment):
            await manager.connect_server(name="fs", command="server")
            environment.exit_error = RuntimeError("teardown failed")

            await manager.close()

            assert manager.sessions == manager._connection_tasks == {}
            assert any(record.levelno >= logging.WARNING for record in caplog.records)
            with pytest.raises(RuntimeError):
                await manager.close(strict=True)

    @pytest.mark.parametrize("cancel_startup", [False, True])
    async def test_startup_cleanup_timeout_preserves_primary(
        self, monkeypatch: pytest.MonkeyPatch, cancel_startup: bool
    ) -> None:
        # Requirement: stuck startup unwind is bounded without losing the primary failure or cancel.
        async with _task_affine_manager() as (manager, environment):
            primary = RuntimeError("initialization failed")
            if cancel_startup:
                environment.initialize_blocker = asyncio.Event()
            else:
                environment.initialize_error = primary
            environment.exit_blocker = asyncio.Event()
            environment.ignore_exit_cancellation = True
            environment.exit_error = RuntimeError("late teardown failure")
            monkeypatch.setattr("conductor.mcp.manager._CLEANUP_TIMEOUT_SECONDS", 0.0)
            connection = asyncio.create_task(manager.connect_server(name="fs", command="server"))
            await environment.initialize_started.wait()
            if cancel_startup:
                connection.cancel()
            try:
                done, pending = await asyncio.wait({connection}, timeout=1)
                assert done == {connection}
                assert not pending
                if cancel_startup:
                    with pytest.raises(asyncio.CancelledError):
                        await connection
                else:
                    with pytest.raises(RuntimeError) as caught:
                        await connection
                    assert caught.value.__cause__ is primary
                assert manager.sessions == {}
                with pytest.raises(RuntimeError):
                    await manager.close(strict=True)
            finally:
                environment.exit_blocker.set()
                _, pending = await asyncio.wait(set(manager._owners), timeout=1)
                assert not pending

            owner = next(iter(manager._owners.values()))
            assert owner.observed
            assert owner.cleanup_error is environment.exit_error
            with pytest.raises(RuntimeError):
                await manager.close(strict=True)

    @pytest.mark.parametrize("late_failure", [False, True])
    async def test_expired_owner_stays_uncertain_after_late_completion(
        self, monkeypatch: pytest.MonkeyPatch, late_failure: bool
    ) -> None:
        # Requirement: bounded observation retains resistant owners and never upgrades late proof.
        from conductor.mcp.manager import _PENDING_LIFECYCLES

        async with _task_affine_manager() as (manager, environment):
            await manager.connect_server(name="fs", command="server")
            task = manager._connection_tasks["fs"]
            environment.exit_blocker = asyncio.Event()
            environment.ignore_exit_cancellation = True
            if late_failure:
                environment.exit_error = RuntimeError("late teardown failure")
            monkeypatch.setattr("conductor.mcp.manager._CLEANUP_TIMEOUT_SECONDS", 0.0)

            with pytest.raises(RuntimeError):
                await manager.close(strict=True)

            try:
                await environment.exit_started.wait()
                assert task in _PENDING_LIFECYCLES
                assert manager._owners[task].resources
                task.cancel()
                await environment.exit_cancelled.wait()
                assert not task.done()
                with pytest.raises(RuntimeError):
                    await manager.close(strict=True)
            finally:
                environment.exit_blocker.set()
                done, pending = await asyncio.wait({task}, timeout=1)
                assert done == {task}
                assert not pending

            assert task not in _PENDING_LIFECYCLES
            assert manager._owners[task].observed
            assert manager._cleanup_uncertain
            assert bool(manager._owners[task].cleanup_error) is late_failure
            with pytest.raises(RuntimeError):
                await manager.close(strict=True)

    async def test_repeated_cancel_does_not_replace_primary_failure(
        self, owner_waits: asyncio.Queue[None]
    ) -> None:
        # Requirement: repeated cancellation while unwinding cannot replace the startup error.
        async with _task_affine_manager() as (manager, environment):
            primary = RuntimeError("initialization failed")
            environment.initialize_error = primary
            environment.exit_error = RuntimeError("teardown failed")
            environment.exit_blocker = asyncio.Event()
            connection = asyncio.create_task(manager.connect_server(name="fs", command="server"))
            await environment.exit_started.wait()
            await owner_waits.get()

            for _ in range(2):
                connection.cancel()
                await owner_waits.get()
            environment.exit_blocker.set()
            with pytest.raises(RuntimeError) as caught:
                await connection

            assert caught.value.__cause__ is primary
            with pytest.raises(RuntimeError):
                await manager.close(strict=True)

    async def test_strict_close_preserves_cancellation_after_join(
        self, owner_waits: asyncio.Queue[None]
    ) -> None:
        # Requirement: strict close joins through repeated cancellation but never reports success.
        async with _task_affine_manager() as (manager, environment):
            environment.exit_blocker = asyncio.Event()
            await manager.connect_server(name="fs", command="server")
            task = manager._connection_tasks["fs"]
            closing = asyncio.create_task(manager.close(strict=True))
            await environment.exit_started.wait()
            await owner_waits.get()

            for _ in range(2):
                closing.cancel()
                await owner_waits.get()
            environment.exit_blocker.set()
            with pytest.raises(asyncio.CancelledError):
                await closing

            assert task.done()
            assert environment.exits == environment.entries
            assert not manager._cleanup_uncertain
            await manager.close(strict=True)
        assert manager.sessions == {}
        assert manager._connection_tasks == {}
