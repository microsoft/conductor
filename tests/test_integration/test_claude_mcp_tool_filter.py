"""Integration test for MCP tool filtering bug in a Claude workflow.

Regression test for https://github.com/microsoft/conductor/issues/37:
When a Claude workflow has mcp_servers configured but no workflow-level
``tools:`` section, MCP tools are silently excluded from API requests.

This test exercises the full path:
  YAML workflow (mcp_servers, no tools:) → WorkflowEngine → AgentExecutor
  → ClaudeProvider._execute_with_retry → _convert_mcp_tools_to_claude(tools=[])

The bug causes ``tool_filter=[]`` to be treated as "include nothing" instead
of "no filter — include all MCP tools".
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from conductor.config.loader import load_workflow
from conductor.config.schema import (
    AgentDef,
    InputDef,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.claude import ClaudeProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "filesystem__read_file",
        "description": "Read a file from the filesystem",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        "server": "filesystem",
        "original_name": "read_file",
    },
    {
        "name": "filesystem__write_file",
        "description": "Write a file to the filesystem",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        "server": "filesystem",
        "original_name": "write_file",
    },
    {
        "name": "web_search__search",
        "description": "Search the web",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "server": "web_search",
        "original_name": "search",
    },
]


def _make_provider_with_mcp() -> ClaudeProvider:
    """Create a ClaudeProvider pre-wired with a mock MCP manager.

    The provider is constructed via ``__new__`` to bypass real SDK
    initialisation, then populated with the minimum attributes needed
    for ``execute()`` → ``_execute_with_retry()`` to reach the MCP
    tool-building code path.
    """
    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider._client = MagicMock()
    provider._default_model = "claude-3-5-sonnet-latest"
    provider._default_temperature = None
    provider._default_max_tokens = 8192
    provider._retry_config = MagicMock()
    provider._retry_config.max_attempts = 1
    provider._retry_config.base_delay = 1.0
    provider._retry_config.max_delay = 30.0
    provider._retry_config.jitter = 0.0
    provider._retry_history = []
    provider._max_parse_recovery_attempts = 2
    provider._max_schema_depth = 10

    # Pre-wire a mock MCP manager so _ensure_mcp_connected is a no-op
    mock_mcp = MagicMock()
    mock_mcp.get_all_tools.return_value = FAKE_MCP_TOOLS
    mock_mcp.has_servers.return_value = True
    provider._mcp_manager = mock_mcp
    provider._mcp_servers_config = {
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]},
    }

    return provider


def _make_emit_output_response(output: dict[str, Any]) -> MagicMock:
    """Create a mock Claude API response that calls emit_output."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "emit_output"
    tool_block.id = "tool_call_1"
    tool_block.input = output

    response = MagicMock()
    response.content = [tool_block]
    response.usage = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    response.usage.cache_read_input_tokens = None
    response.usage.cache_creation_input_tokens = None
    response.stop_reason = "end_turn"
    return response


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMcpToolsReachApiInWorkflow:
    """Verify MCP tools survive the full workflow → provider pipeline.

    The common scenario: a workflow declares ``mcp_servers`` but has no
    ``tools:`` key.  WorkflowConfig.tools defaults to ``[]``, which flows
    through AgentExecutor → resolve_agent_tools → ClaudeProvider.execute
    as ``tools=[]``.

    The bug: ``_convert_mcp_tools_to_claude(tool_filter=[])`` treated
    ``[]`` as "include nothing", silently dropping every MCP tool.
    """

    @pytest.mark.asyncio
    async def test_mcp_tools_included_when_workflow_has_no_tools_section(self) -> None:
        """MCP tools must appear in the API request even without a tools: section."""
        provider = _make_provider_with_mcp()

        # Capture the tools kwarg passed to _execute_agentic_loop
        captured: dict[str, Any] = {}

        async def spy_agentic_loop(**kwargs: Any) -> Any:
            captured["tools"] = kwargs.get("tools")
            # Return a canned emit_output response so execute completes
            response = _make_emit_output_response({"content": "file contents here"})
            return (response, 150, False)

        provider._execute_agentic_loop = AsyncMock(side_effect=spy_agentic_loop)

        agent = AgentDef(
            name="reader",
            model="claude-3-5-sonnet-latest",
            prompt="Read the file at /tmp/test.txt",
            output={"content": OutputField(type="string", description="File contents")},
            routes=[RouteDef(to="$end")],
        )

        # Simulate what the engine does: tools=[] (no workflow tools defined)
        await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Read the file at /tmp/test.txt",
            tools=[],
        )

        # The tools list sent to the agentic loop must contain the MCP tools
        # plus emit_output.  Before the fix, only emit_output would appear.
        api_tools = captured["tools"]
        assert api_tools is not None, "tools should not be None"

        tool_names = {t["name"] for t in api_tools}
        assert "emit_output" in tool_names, "emit_output should always be present"

        # THE KEY ASSERTION: MCP tools must be included
        assert "filesystem__read_file" in tool_names, (
            "MCP tool 'filesystem__read_file' was filtered out — "
            "this is the bug from issue #37"
        )
        assert "filesystem__write_file" in tool_names
        assert "web_search__search" in tool_names
        assert len(tool_names) == 4  # 3 MCP + 1 emit_output

    @pytest.mark.asyncio
    async def test_mcp_tools_included_in_full_workflow_engine_run(
        self, tmp_path
    ) -> None:
        """End-to-end: WorkflowEngine → AgentExecutor → ClaudeProvider with MCP.

        Wires up a real WorkflowConfig (no ``tools:`` section) and verifies
        MCP tools reach the agentic loop through the full call chain.
        """
        provider = _make_provider_with_mcp()

        # Spy on _execute_agentic_loop to capture the tools list
        captured: dict[str, Any] = {}

        async def spy_agentic_loop(**kwargs: Any) -> Any:
            captured["tools"] = kwargs.get("tools")
            response = _make_emit_output_response({"content": "hello world"})
            return (response, 150, False)

        provider._execute_agentic_loop = AsyncMock(side_effect=spy_agentic_loop)

        # Build a workflow config — note: no `tools` key, so defaults to []
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-test",
                description="Test MCP tool filtering",
                entry_point="reader",
                runtime=RuntimeConfig(
                    provider="claude",
                    mcp_servers={
                        "filesystem": {
                            "command": "npx",
                            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                            "tools": ["*"],
                        },
                    },
                ),
                input={
                    "path": InputDef(type="string", required=True),
                },
            ),
            agents=[
                AgentDef(
                    name="reader",
                    model="claude-3-5-sonnet-latest",
                    prompt="Use read_file to read {{ workflow.input.path }}",
                    output={"content": OutputField(type="string", description="File contents")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"content": "{{ reader.output.content }}"},
        )

        # Sanity: the workflow has no explicit tools
        assert config.tools == []

        engine = WorkflowEngine(config, provider)
        result = await engine.run({"path": "/tmp/test.txt"})

        # Verify the workflow completed
        assert result["content"] == "hello world"

        # Verify MCP tools were included in the API call
        api_tools = captured["tools"]
        assert api_tools is not None

        tool_names = {t["name"] for t in api_tools}
        assert "filesystem__read_file" in tool_names, (
            "MCP tool missing from API request — issue #37 regression"
        )
        assert "filesystem__write_file" in tool_names
        assert "web_search__search" in tool_names

    @pytest.mark.asyncio
    async def test_explicit_tool_filter_still_works(self) -> None:
        """When workflow defines specific tools, only those MCP tools are included."""
        provider = _make_provider_with_mcp()

        captured: dict[str, Any] = {}

        async def spy_agentic_loop(**kwargs: Any) -> Any:
            captured["tools"] = kwargs.get("tools")
            response = _make_emit_output_response({"content": "filtered"})
            return (response, 150, False)

        provider._execute_agentic_loop = AsyncMock(side_effect=spy_agentic_loop)

        agent = AgentDef(
            name="reader",
            model="claude-3-5-sonnet-latest",
            prompt="Read the file",
            output={"content": OutputField(type="string")},
            routes=[RouteDef(to="$end")],
        )

        # Pass a specific tool filter — only filesystem__read_file
        await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Read the file",
            tools=["filesystem__read_file"],
        )

        api_tools = captured["tools"]
        tool_names = {t["name"] for t in api_tools}

        # Only the explicitly listed MCP tool + emit_output
        assert "filesystem__read_file" in tool_names
        assert "filesystem__write_file" not in tool_names
        assert "web_search__search" not in tool_names
        assert "emit_output" in tool_names


class TestWorkflowYamlWithMcpServers:
    """Test loading and validating a workflow YAML that mirrors the issue repro."""

    def test_load_mcp_workflow_has_empty_tools(self, tmp_path) -> None:
        """A workflow with mcp_servers but no tools: section has config.tools == []."""
        workflow_yaml = tmp_path / "mcp_workflow.yaml"
        workflow_yaml.write_text("""\
workflow:
  name: mcp-test
  entry_point: reader
  runtime:
    provider: claude
    mcp_servers:
      filesystem:
        command: npx
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        tools: ["*"]
  input:
    path: { type: string, required: true }

agents:
  - name: reader
    model: claude-sonnet-4.6
    prompt: "Use read_file to read {{ workflow.input.path }}"
    output:
      content: { type: string }
    routes:
      - to: $end
""")

        config = load_workflow(str(workflow_yaml))

        # The workflow has no `tools:` key → defaults to []
        assert config.tools == []

        # But mcp_servers IS configured
        assert "filesystem" in config.workflow.runtime.mcp_servers

        # The agent has no explicit tools → agent.tools is None (meaning "all")
        assert config.agents[0].tools is None
