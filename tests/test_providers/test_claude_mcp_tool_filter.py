"""Tests for MCP tool filtering in the Claude provider.

Regression tests for https://github.com/microsoft/conductor/issues/37:
An empty tool_filter ([]) silently excludes all MCP tools because
`[] is not None` evaluates to True, causing every tool to be skipped.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

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
        result = provider._convert_mcp_tools_to_claude(
            tool_filter=["filesystem__read_file"]
        )
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
        result = provider._convert_mcp_tools_to_claude(
            tool_filter=["nonexistent_tool"]
        )
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
