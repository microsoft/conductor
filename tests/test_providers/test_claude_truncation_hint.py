"""Tests for the Claude provider's truncation-hint replacement logic."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from conductor.config.schema import ToolOutputConfig
from conductor.providers.claude import (
    _FS_HINT,
    _GENERIC_HINT,
    _TRUNCATION_MARKER_PREFIX,
    ClaudeProvider,
)


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
    return resp


def _make_provider_with_tool_output(tool_output: ToolOutputConfig | None = None) -> ClaudeProvider:
    """Create a minimal ClaudeProvider with a mock MCP manager."""
    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider._client = MagicMock()
    provider._mcp_servers_config = None
    provider._default_model = "claude-3-5-sonnet-latest"
    provider._default_temperature = None
    provider._default_max_tokens = 8192
    provider._retry_config = MagicMock()
    provider._retry_config.max_attempts = 1
    provider._retry_config.max_parse_recovery_attempts = 2
    provider._retry_history = []
    provider._max_schema_depth = 10
    provider._default_max_agent_iterations = 50
    provider._default_max_session_seconds = None
    provider._default_reasoning_effort = None
    provider._tool_output_config = tool_output or ToolOutputConfig()

    mock_mcp_manager = MagicMock()
    mock_mcp_manager.has_servers.return_value = True
    mock_mcp_manager.get_all_tools.return_value = []
    mock_mcp_manager.call_tool = AsyncMock(return_value="tool result")
    provider._mock_mcp_manager = mock_mcp_manager

    return provider


class TestHasFsLikeTool:
    """Tests for the private _has_fs_like_tool helper."""

    def test_returns_true_for_read_tool(self) -> None:
        """A read_file-like tool should be detected as filesystem-like."""
        provider = _make_provider_with_tool_output()
        tools = [{"name": "fs__read_file"}]
        assert provider._has_fs_like_tool(tools) is True

    def test_returns_true_for_bash_tool(self) -> None:
        """A bash tool should be detected as filesystem-like."""
        provider = _make_provider_with_tool_output()
        tools = [{"name": "shell__bash"}]
        assert provider._has_fs_like_tool(tools) is True

    def test_returns_true_for_grep_tool(self) -> None:
        """A grep tool should be detected as filesystem-like."""
        provider = _make_provider_with_tool_output()
        tools = [{"name": "filesystem__grep"}]
        assert provider._has_fs_like_tool(tools) is True

    def test_returns_false_for_web_search_tool(self) -> None:
        """A web search tool should not be detected as filesystem-like."""
        provider = _make_provider_with_tool_output()
        tools = [{"name": "web_search__search"}]
        assert provider._has_fs_like_tool(tools) is False

    def test_returns_false_when_tools_is_none(self) -> None:
        """No tools means no filesystem-like tools."""
        provider = _make_provider_with_tool_output()
        assert provider._has_fs_like_tool(None) is False

    def test_returns_false_when_tools_is_empty(self) -> None:
        """An empty tool list means no filesystem-like tools."""
        provider = _make_provider_with_tool_output()
        assert provider._has_fs_like_tool([]) is False

    def test_strips_server_prefix_before_matching(self) -> None:
        """The server prefix is ignored; the tool name is what matters."""
        provider = _make_provider_with_tool_output()
        tools = [{"name": "not_fs__read"}]
        assert provider._has_fs_like_tool(tools) is True

    def test_matches_substring_case_insensitive(self) -> None:
        """Matching is substring-based and case-insensitive."""
        provider = _make_provider_with_tool_output()
        tools = [{"name": "fs__ViewCode"}]
        assert provider._has_fs_like_tool(tools) is True


class TestMaybeRewriteTruncationHint:
    """Tests for the _maybe_rewrite_truncation_hint helper."""

    def _truncated_result(self, hint: str = _GENERIC_HINT, path: str | None = None) -> str:
        """Build a result that looks like a truncated MCP tool output."""
        base = "x" * 1000
        if path:
            return (
                f"{base}\n\n[output truncated: 2000 chars -> 1000 kept; "
                f"full output saved to: {path}. {hint}]"
            )
        return f"{base}\n\n[output truncated: 2000 chars -> 1000 kept. {hint}]"

    def test_rewrites_generic_hint_when_fs_tool_and_spill_path_present(self) -> None:
        """Generic hint is replaced with fs hint when fs tools exist and a spill path is present."""
        provider = _make_provider_with_tool_output()
        result = self._truncated_result(path="/tmp/spill.txt")
        tools = [{"name": "fs__read_file"}]

        rewritten = provider._maybe_rewrite_truncation_hint(result, tools)

        assert _FS_HINT in rewritten
        assert _GENERIC_HINT not in rewritten
        assert _TRUNCATION_MARKER_PREFIX in rewritten

    def test_keeps_generic_hint_when_no_fs_tool(self) -> None:
        """Generic hint is retained when no filesystem-like tools are available."""
        provider = _make_provider_with_tool_output()
        result = self._truncated_result()
        tools = [{"name": "web_search__search"}]

        rewritten = provider._maybe_rewrite_truncation_hint(result, tools)

        assert _GENERIC_HINT in rewritten
        assert _FS_HINT not in rewritten

    def test_returns_unchanged_when_no_truncation_marker(self) -> None:
        """Non-truncated results are returned unchanged."""
        provider = _make_provider_with_tool_output()
        result = "This is a normal tool result."
        tools = [{"name": "fs__read_file"}]

        rewritten = provider._maybe_rewrite_truncation_hint(result, tools)

        assert rewritten == result

    def test_returns_unchanged_when_generic_hint_literal_without_truncation(self) -> None:
        """A literal generic hint in a non-truncated result must not be mutated."""
        provider = _make_provider_with_tool_output()
        result = f"The agent said: {_GENERIC_HINT}"
        tools = [{"name": "fs__read_file"}]

        rewritten = provider._maybe_rewrite_truncation_hint(result, tools)

        assert rewritten == result
        assert _FS_HINT not in rewritten

    def test_returns_unchanged_for_none_tools(self) -> None:
        """Truncated result with tools=None stays generic."""
        provider = _make_provider_with_tool_output()
        result = self._truncated_result()

        rewritten = provider._maybe_rewrite_truncation_hint(result, None)

        assert _GENERIC_HINT in rewritten
        assert _FS_HINT not in rewritten

    def test_keeps_generic_hint_when_truncated_but_no_spill_path(self) -> None:
        """Generic hint is kept when the marker has no path, even with fs tools."""
        provider = _make_provider_with_tool_output()
        result = self._truncated_result()  # no spill path
        tools = [{"name": "fs__read_file"}]

        rewritten = provider._maybe_rewrite_truncation_hint(result, tools)

        assert _GENERIC_HINT in rewritten
        assert _FS_HINT not in rewritten

    def test_payload_literal_does_not_trigger_false_fs_rewrite(self) -> None:
        """Payload containing the literal 'full output saved to:' must not trigger rewrite."""
        provider = _make_provider_with_tool_output()
        payload = "x" * 800 + " full output saved to: /tmp/evil.txt " + "x" * 100
        result = f"{payload}\n\n[output truncated: 2000 chars -> 1000 kept. {_GENERIC_HINT}]"
        tools = [{"name": "fs__read_file"}]

        rewritten = provider._maybe_rewrite_truncation_hint(result, tools)

        assert _GENERIC_HINT in rewritten
        assert _FS_HINT not in rewritten

    def test_rewrites_only_when_marker_is_in_tail(self) -> None:
        """Marker detection looks at the trailing 2000 characters of the result."""
        provider = _make_provider_with_tool_output()
        prefix = "y" * 500
        result = f"{prefix}{self._truncated_result(path='/tmp/spill.txt')[-500:]}"
        tools = [{"name": "fs__read_file"}]

        rewritten = provider._maybe_rewrite_truncation_hint(result, tools)

        assert _FS_HINT in rewritten
        assert _GENERIC_HINT not in rewritten

    def test_no_rewrite_when_marker_is_not_in_tail(self) -> None:
        """A marker too far from the end is ignored."""
        provider = _make_provider_with_tool_output()
        prefix = "y" * 1000
        result = f"{prefix}{self._truncated_result()[-100:]} more trailing text"
        tools = [{"name": "fs__read_file"}]

        rewritten = provider._maybe_rewrite_truncation_hint(result, tools)

        assert _GENERIC_HINT in rewritten
        assert _FS_HINT not in rewritten


class TestParseTruncationMarker:
    """Direct unit tests for _parse_truncation_marker parsing."""

    def test_parses_generic_hint(self) -> None:
        """A marker using the generic hint parses correctly."""
        provider = _make_provider_with_tool_output()
        result = "x" * 100 + (
            f"\n\n[output truncated: 200 chars -> 100 kept; "
            f"full output saved to: /tmp/s.txt. {_GENERIC_HINT}]"
        )

        parsed = provider._parse_truncation_marker(result)

        assert parsed == {
            "original_chars": 200,
            "kept_chars": 100,
            "spill_path": "/tmp/s.txt",
        }

    def test_parses_fs_hint(self) -> None:
        """A marker using the fs hint (after rewrite) parses correctly."""
        provider = _make_provider_with_tool_output()
        result = "x" * 100 + (
            f"\n\n[output truncated: 200 chars -> 100 kept; "
            f"full output saved to: /tmp/s.txt. {_FS_HINT}]"
        )

        parsed = provider._parse_truncation_marker(result)

        assert parsed == {
            "original_chars": 200,
            "kept_chars": 100,
            "spill_path": "/tmp/s.txt",
        }

    def test_parses_no_path(self) -> None:
        """A marker without a spill path parses with None path."""
        provider = _make_provider_with_tool_output()
        result = "x" * 100 + (f"\n\n[output truncated: 200 chars -> 100 kept. {_GENERIC_HINT}]")

        parsed = provider._parse_truncation_marker(result)

        assert parsed == {
            "original_chars": 200,
            "kept_chars": 100,
            "spill_path": None,
        }

    def test_parses_last_marker_when_payload_contains_spoof(self) -> None:
        """A fake marker earlier in the payload is ignored; the real trailing marker wins."""
        provider = _make_provider_with_tool_output()
        fake = "[output truncated: 9999 chars -> 1 kept; full output saved to: /tmp/fake.txt."
        real = (
            "\n\n[output truncated: 2000 chars -> 1000 kept; "
            f"full output saved to: /tmp/real.txt. {_GENERIC_HINT}]"
        )
        result = "x" * 500 + fake + "y" * 500 + real

        parsed = provider._parse_truncation_marker(result)

        assert parsed == {
            "original_chars": 2000,
            "kept_chars": 1000,
            "spill_path": "/tmp/real.txt",
        }

    def test_parses_long_spill_path_exceeding_old_window(self) -> None:
        """A ~4000-char spill path is parsed and the fs hint is applied."""
        provider = _make_provider_with_tool_output()
        long_path = "/tmp/" + "a" * 4000 + "/spill.txt"
        result = "x" * 1000 + (
            f"\n\n[output truncated: 2000 chars -> 1000 kept; "
            f"full output saved to: {long_path}. {_GENERIC_HINT}]"
        )
        tools = [{"name": "fs__read_file"}]

        parsed = provider._parse_truncation_marker(result)
        assert parsed == {
            "original_chars": 2000,
            "kept_chars": 1000,
            "spill_path": long_path,
        }

        rewritten = provider._maybe_rewrite_truncation_hint(result, tools)
        assert _FS_HINT in rewritten
        assert _GENERIC_HINT not in rewritten
        assert long_path in rewritten

    def test_payload_generic_hint_literal_is_not_corrupted(self) -> None:
        """A generic hint literal inside the payload is unchanged; only the marker
        hint is rewritten."""
        provider = _make_provider_with_tool_output()
        payload = "x" * 100 + _GENERIC_HINT + "y" * 100
        marker = (
            "\n\n[output truncated: 2000 chars -> 1000 kept; "
            "full output saved to: /tmp/spill.txt. " + _GENERIC_HINT + "]"
        )
        result = payload + marker
        tools = [{"name": "fs__read_file"}]

        rewritten = provider._maybe_rewrite_truncation_hint(result, tools)

        # Payload occurrence stays generic; marker occurrence becomes fs hint.
        assert payload in rewritten
        assert rewritten.count(_GENERIC_HINT) == 1
        assert rewritten.count(_FS_HINT) == 1
        assert rewritten.endswith("]")


class TestAgenticLoopHintReplacement:
    """Tests for hint replacement via the full _execute_agentic_loop."""

    @pytest.mark.asyncio
    async def test_loop_rewrites_hint_when_fs_tool_present(self) -> None:
        """The agentic loop replaces the generic hint when fs-like tools are present."""
        provider = _make_provider_with_tool_output()
        events: list[tuple[str, dict[str, Any]]] = []
        truncated_result = "x" * 100 + (
            "\n\n[output truncated: 200 chars -> 100 kept; "
            f"full output saved to: /tmp/spill.txt. {_GENERIC_HINT}]"
        )

        mcp_response = _make_response(
            [_make_tool_use_block("filesystem__read_file", {"path": "/tmp/test.txt"})]
        )
        text_response = _make_response([_make_text_block("Done")])
        provider._execute_api_call = AsyncMock(side_effect=[mcp_response, text_response])
        provider._mock_mcp_manager.call_tool = AsyncMock(return_value=truncated_result)

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=[{"name": "filesystem__read_file"}],
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
            mcp_manager=getattr(provider, "_mock_mcp_manager", None),
        )

        complete_events = [d for t, d in events if t == "agent_tool_complete"]
        assert len(complete_events) == 1
        assert _FS_HINT in complete_events[0]["result"]
        assert _GENERIC_HINT not in complete_events[0]["result"]

    @pytest.mark.asyncio
    async def test_loop_keeps_generic_hint_when_no_spill_path(self) -> None:
        """The agentic loop keeps the generic hint when the marker omits a path."""
        provider = _make_provider_with_tool_output()
        events: list[tuple[str, dict[str, Any]]] = []
        truncated_result = "x" * 100 + (
            f"\n\n[output truncated: 200 chars -> 100 kept. {_GENERIC_HINT}]"
        )

        mcp_response = _make_response(
            [_make_tool_use_block("filesystem__read_file", {"path": "/tmp/test.txt"})]
        )
        text_response = _make_response([_make_text_block("Done")])
        provider._execute_api_call = AsyncMock(side_effect=[mcp_response, text_response])
        provider._mock_mcp_manager.call_tool = AsyncMock(return_value=truncated_result)

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=[{"name": "filesystem__read_file"}],
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
            mcp_manager=getattr(provider, "_mock_mcp_manager", None),
        )

        complete_events = [d for t, d in events if t == "agent_tool_complete"]
        assert len(complete_events) == 1
        assert _GENERIC_HINT in complete_events[0]["result"]
        assert _FS_HINT not in complete_events[0]["result"]

    @pytest.mark.asyncio
    async def test_loop_keeps_generic_hint_when_no_fs_tool(self) -> None:
        """The agentic loop keeps the generic hint when no fs-like tools are present."""
        provider = _make_provider_with_tool_output()
        events: list[tuple[str, dict[str, Any]]] = []
        truncated_result = "x" * 100 + (
            f"\n\n[output truncated: 200 chars -> 100 kept. {_GENERIC_HINT}]"
        )

        mcp_response = _make_response(
            [_make_tool_use_block("web_search__search", {"query": "test"})]
        )
        text_response = _make_response([_make_text_block("Done")])
        provider._execute_api_call = AsyncMock(side_effect=[mcp_response, text_response])
        provider._mock_mcp_manager.call_tool = AsyncMock(return_value=truncated_result)

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=[{"name": "web_search__search"}],
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
            mcp_manager=getattr(provider, "_mock_mcp_manager", None),
        )

        complete_events = [d for t, d in events if t == "agent_tool_complete"]
        assert len(complete_events) == 1
        assert _GENERIC_HINT in complete_events[0]["result"]
        assert _FS_HINT not in complete_events[0]["result"]

    @pytest.mark.asyncio
    async def test_loop_with_tools_none_keeps_generic_hint(self) -> None:
        """tools=None means no resolved tools, so the generic hint is kept."""
        provider = _make_provider_with_tool_output()
        events: list[tuple[str, dict[str, Any]]] = []
        truncated_result = "x" * 100 + (
            f"\n\n[output truncated: 200 chars -> 100 kept. {_GENERIC_HINT}]"
        )

        mcp_response = _make_response(
            [_make_tool_use_block("filesystem__read_file", {"path": "/tmp/test.txt"})]
        )
        text_response = _make_response([_make_text_block("Done")])
        provider._execute_api_call = AsyncMock(side_effect=[mcp_response, text_response])
        provider._mock_mcp_manager.call_tool = AsyncMock(return_value=truncated_result)

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=None,
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
            mcp_manager=getattr(provider, "_mock_mcp_manager", None),
        )

        complete_events = [d for t, d in events if t == "agent_tool_complete"]
        assert len(complete_events) == 1
        assert _GENERIC_HINT in complete_events[0]["result"]
        assert _FS_HINT not in complete_events[0]["result"]


class TestFullLoopEventAndRewrite:
    """Tests for event emission and fs-hint rewrite in the full agentic loop."""

    @pytest.mark.asyncio
    async def test_truncation_event_with_fs_tools_after_hint_rewrite(self) -> None:
        """Truncation event fires even after the generic hint is rewritten to fs hint."""
        provider = _make_provider_with_tool_output()
        events: list[tuple[str, dict[str, Any]]] = []

        mcp_response = _make_response(
            [_make_tool_use_block("filesystem__read_file", {"path": "/tmp/test.txt"})]
        )
        text_response = _make_response([_make_text_block("Done")])
        provider._execute_api_call = AsyncMock(side_effect=[mcp_response, text_response])
        truncated_result = (
            "x" * 100
            + "\n\n[output truncated: 200 chars -> 100 kept; "
            + f"full output saved to: /tmp/spill.txt. {_GENERIC_HINT}]"
        )
        provider._mock_mcp_manager.call_tool = AsyncMock(return_value=truncated_result)

        await provider._execute_agentic_loop(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=8192,
            tools=[{"name": "filesystem__read_file"}],
            output_schema=None,
            has_output_schema=False,
            event_callback=lambda t, d: events.append((t, d)),
            mcp_manager=getattr(provider, "_mock_mcp_manager", None),
        )

        truncation_events = [d for t, d in events if t == "agent_tool_output_truncated"]
        assert len(truncation_events) == 1
        assert truncation_events[0]["tool_name"] == "filesystem__read_file"
        assert truncation_events[0]["original_chars"] == 200
        assert truncation_events[0]["kept_chars"] == 100
        assert truncation_events[0]["spill_path"] == "/tmp/spill.txt"

        complete_events = [d for t, d in events if t == "agent_tool_complete"]
        assert len(complete_events) == 1
        assert _FS_HINT in complete_events[0]["result"]
        assert _GENERIC_HINT not in complete_events[0]["result"]
