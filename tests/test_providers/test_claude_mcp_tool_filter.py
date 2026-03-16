"""Tests for MCP tool filtering in the Claude provider.

Regression tests for:
- https://github.com/microsoft/conductor/issues/37:
  An empty tool_filter ([]) silently excludes all MCP tools because
  `[] is not None` evaluates to True, causing every tool to be skipped.
- https://github.com/microsoft/conductor/issues/38:
  _execute_with_parse_recovery blocks MCP tool calls — it enters parse
  recovery instead of returning to the agentic loop when the API response
  contains non-emit_output tool_use blocks.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from conductor.config.schema import OutputField
from conductor.exceptions import ProviderError
from conductor.providers.claude import ClaudeProvider


def _make_provider_with_mcp_tools(tools: list[dict[str, Any]]) -> ClaudeProvider:
    """Create a ClaudeProvider with a mock MCP manager that returns the given tools."""
    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider._client = MagicMock()
    provider._mcp_servers_config = None
    provider._default_model = "claude-3-5-sonnet-latest"
    provider._default_temperature = None
    provider._default_max_tokens = 8192
    provider._retry_config = MagicMock()
    provider._retry_config.max_attempts = 1
    provider._retry_history = []
    provider._max_parse_recovery_attempts = 2
    provider._max_schema_depth = 10

    # Set up a mock MCP manager with tools
    mock_mcp_manager = MagicMock()
    mock_mcp_manager.get_all_tools.return_value = tools
    mock_mcp_manager.has_servers.return_value = True
    provider._mcp_manager = mock_mcp_manager

    return provider


FAKE_MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "filesystem__read_file",
        "description": "Read a file from the filesystem",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
    {
        "name": "filesystem__write_file",
        "description": "Write a file to the filesystem",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
    {
        "name": "web_search__search",
        "description": "Search the web",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
]


class TestConvertMcpToolsFilter:
    """Tests for _convert_mcp_tools_to_claude tool filtering."""

    def test_none_filter_includes_all_tools(self) -> None:
        """tool_filter=None should include all MCP tools (no filtering)."""
        provider = _make_provider_with_mcp_tools(FAKE_MCP_TOOLS)
        result = provider._convert_mcp_tools_to_claude(tool_filter=None)
        assert len(result) == 3

    def test_empty_list_filter_includes_all_tools(self) -> None:
        """tool_filter=[] should include all MCP tools (no filtering).

        Regression test for issue #37: empty list was treated as
        'filter to nothing' instead of 'no filter applied'.
        """
        provider = _make_provider_with_mcp_tools(FAKE_MCP_TOOLS)
        result = provider._convert_mcp_tools_to_claude(tool_filter=[])
        assert len(result) == 3

    def test_specific_filter_includes_only_matching_tools(self) -> None:
        """tool_filter=['name'] should include only matching tools."""
        provider = _make_provider_with_mcp_tools(FAKE_MCP_TOOLS)
        result = provider._convert_mcp_tools_to_claude(tool_filter=["filesystem__read_file"])
        assert len(result) == 1
        assert result[0]["name"] == "filesystem__read_file"

    def test_filter_with_multiple_tools(self) -> None:
        """tool_filter with multiple names should include all matching tools."""
        provider = _make_provider_with_mcp_tools(FAKE_MCP_TOOLS)
        result = provider._convert_mcp_tools_to_claude(
            tool_filter=["filesystem__read_file", "web_search__search"]
        )
        assert len(result) == 2
        names = {t["name"] for t in result}
        assert names == {"filesystem__read_file", "web_search__search"}

    def test_filter_with_nonexistent_tool_excludes_it(self) -> None:
        """tool_filter with names not in MCP tools should return no matches for those."""
        provider = _make_provider_with_mcp_tools(FAKE_MCP_TOOLS)
        result = provider._convert_mcp_tools_to_claude(tool_filter=["nonexistent_tool"])
        assert len(result) == 0

    def test_no_mcp_manager_returns_empty(self) -> None:
        """No MCP manager should return empty list regardless of filter."""
        provider = ClaudeProvider.__new__(ClaudeProvider)
        provider._mcp_manager = None
        result = provider._convert_mcp_tools_to_claude(tool_filter=[])
        assert result == []
        result = provider._convert_mcp_tools_to_claude(tool_filter=None)
        assert result == []

    def test_tool_format_preserved(self) -> None:
        """Converted tools should have name, description, and input_schema."""
        provider = _make_provider_with_mcp_tools(FAKE_MCP_TOOLS)
        result = provider._convert_mcp_tools_to_claude(tool_filter=None)
        for tool in result:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool


# ---------------------------------------------------------------------------
# Helpers for _has_mcp_tool_use / _execute_with_parse_recovery tests
# ---------------------------------------------------------------------------


def _make_tool_use_block(name: str, input_data: dict[str, Any] | None = None) -> MagicMock:
    """Create a mock tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.id = f"call_{name}"
    block.input = input_data or {}
    return block


def _make_text_block(text: str) -> MagicMock:
    """Create a mock text content block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_response(blocks: list[MagicMock]) -> MagicMock:
    """Create a mock Claude API response with the given content blocks."""
    resp = MagicMock()
    resp.content = blocks
    resp.usage = MagicMock()
    resp.usage.input_tokens = 100
    resp.usage.output_tokens = 50
    resp.usage.cache_read_input_tokens = None
    resp.usage.cache_creation_input_tokens = None
    return resp


def _make_bare_provider() -> ClaudeProvider:
    """Create a minimal ClaudeProvider for unit-testing helper methods."""
    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider._client = MagicMock()
    provider._mcp_manager = None
    provider._mcp_servers_config = None
    provider._default_model = "claude-3-5-sonnet-latest"
    provider._default_temperature = None
    provider._default_max_tokens = 8192
    provider._retry_config = MagicMock()
    provider._retry_config.max_attempts = 1
    provider._retry_history = []
    provider._max_parse_recovery_attempts = 2
    provider._max_schema_depth = 10
    return provider


# ---------------------------------------------------------------------------
# Issue #38: _has_mcp_tool_use
# ---------------------------------------------------------------------------


class TestHasMcpToolUse:
    """Tests for the _has_mcp_tool_use helper."""

    def test_detects_mcp_tool_use(self) -> None:
        """Response with a non-emit_output tool_use should return True."""
        provider = _make_bare_provider()
        response = _make_response([_make_tool_use_block("filesystem__read_file")])
        assert provider._has_mcp_tool_use(response) is True

    def test_ignores_emit_output(self) -> None:
        """Response with only emit_output should return False."""
        provider = _make_bare_provider()
        response = _make_response([_make_tool_use_block("emit_output", {"result": "hi"})])
        assert provider._has_mcp_tool_use(response) is False

    def test_mixed_emit_and_mcp(self) -> None:
        """Response with both emit_output and MCP tool_use should return True."""
        provider = _make_bare_provider()
        response = _make_response(
            [
                _make_tool_use_block("emit_output", {"result": "hi"}),
                _make_tool_use_block("filesystem__read_file"),
            ]
        )
        assert provider._has_mcp_tool_use(response) is True

    def test_text_only_response(self) -> None:
        """Response with only text blocks should return False."""
        provider = _make_bare_provider()
        response = _make_response([_make_text_block("Hello world")])
        assert provider._has_mcp_tool_use(response) is False

    def test_empty_response(self) -> None:
        """Response with no content blocks should return False."""
        provider = _make_bare_provider()
        response = _make_response([])
        assert provider._has_mcp_tool_use(response) is False


# ---------------------------------------------------------------------------
# Issue #38: _execute_with_parse_recovery returns MCP tool calls
# ---------------------------------------------------------------------------


class TestParseRecoveryMcpPassthrough:
    """Verify _execute_with_parse_recovery returns MCP tool_use responses
    immediately instead of entering parse recovery.

    Regression tests for issue #38.
    """

    @pytest.mark.asyncio
    async def test_mcp_tool_use_returned_without_recovery(self) -> None:
        """Initial API response with MCP tool_use should be returned directly."""
        provider = _make_bare_provider()

        mcp_response = _make_response(
            [
                _make_tool_use_block("filesystem__read_file", {"path": "/tmp/test.txt"}),
            ]
        )
        provider._execute_api_call = AsyncMock(return_value=mcp_response)

        result = await provider._execute_with_parse_recovery(
            messages=[{"role": "user", "content": "read the file"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=[{"name": "emit_output", "description": "d", "input_schema": {}}],
            output_schema={"content": OutputField(type="string")},
        )

        # Should return the MCP response, not enter recovery
        assert result is mcp_response
        # API should have been called exactly once (no recovery retries)
        assert provider._execute_api_call.call_count == 1

    @pytest.mark.asyncio
    async def test_emit_output_still_returned_directly(self) -> None:
        """emit_output response should still be returned on the fast path."""
        provider = _make_bare_provider()

        emit_response = _make_response(
            [
                _make_tool_use_block("emit_output", {"content": "result"}),
            ]
        )
        provider._execute_api_call = AsyncMock(return_value=emit_response)

        result = await provider._execute_with_parse_recovery(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=[{"name": "emit_output", "description": "d", "input_schema": {}}],
            output_schema={"content": OutputField(type="string")},
        )

        assert result is emit_response
        assert provider._execute_api_call.call_count == 1

    @pytest.mark.asyncio
    async def test_text_response_still_triggers_recovery(self) -> None:
        """Plain text response (no tool_use) should still enter parse recovery."""
        provider = _make_bare_provider()

        text_response = _make_response([_make_text_block("I cannot use tools")])
        provider._execute_api_call = AsyncMock(return_value=text_response)

        with pytest.raises(ProviderError):
            # Should exhaust recovery attempts and raise
            await provider._execute_with_parse_recovery(
                messages=[{"role": "user", "content": "test"}],
                model="claude-3-5-sonnet-latest",
                temperature=None,
                max_tokens=8192,
                tools=[{"name": "emit_output", "description": "d", "input_schema": {}}],
                output_schema={"content": OutputField(type="string")},
            )

        # Should have called API 1 (initial) + 2 (recovery attempts) = 3 times
        assert provider._execute_api_call.call_count == 3
