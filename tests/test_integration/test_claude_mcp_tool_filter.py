"""Integration test for MCP tool filtering bug in a Claude workflow.

Regression test for https://github.com/microsoft/conductor/issues/37:
When a Claude workflow has mcp_servers configured but no workflow-level
``tools:`` section, MCP tools are silently excluded from API requests.

This test exercises the full path:
  YAML workflow (mcp_servers, no tools:) → WorkflowEngine → AgentExecutor
  → ClaudeProvider.execute → build_agent(toolsets=[MCPManagerToolset])

The bug caused ``tools=[]`` to be treated as "include nothing" instead of
"no filter — include all MCP tools". The provider now normalizes an empty
filter to ``None`` so ``MCPManagerToolset`` exposes every manager tool.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

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
from conductor.providers._pydantic_ai.mcp_toolset import MCPManagerToolset
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

    The provider is constructed through its real ``__init__`` so it has
    every attribute ``execute()`` expects, then the network client and MCP
    pool are replaced with test doubles. This keeps the fixture aligned
    with the provider's attribute contract when the implementation changes.
    """
    provider = ClaudeProvider(api_key="test-key")
    provider._client = MagicMock()

    # Pre-wire a mock MCP manager so _get_mcp_manager_for_cwd returns it.
    mock_mcp = MagicMock()
    mock_mcp.get_all_tools.return_value = FAKE_MCP_TOOLS
    mock_mcp.has_servers.return_value = True
    provider._mcp_servers_config = {
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]},
    }
    import os

    provider._mcp_managers = {os.getcwd(): mock_mcp}
    provider._mcp_manager_locks = {}
    provider._tool_output_config = MagicMock()

    return provider


def _build_structured_agent(data: dict[str, Any]) -> Agent[Any, Any]:
    """Build a Pydantic AI structured-output agent backed by TestModel."""

    class DynamicModel(BaseModel):
        model_config = {"extra": "allow"}

    return Agent(
        model=TestModel(custom_output_args=data),
        output_type=DynamicModel,
    )


def _patch_build_agent(
    captured: dict[str, Any],
    output_data: dict[str, Any],
    target: str = "conductor.providers._pydantic_ai.agent_builder.build_agent",
) -> Any:
    """Return a patch that captures the toolsets kwarg and returns a canned agent.

    The canned agent completes immediately with ``output_data`` as its structured
    output, so the workflow engine can finish without touching the network.
    """

    class DynamicModel(BaseModel):
        model_config = {"extra": "allow"}

    def make_agent(**kwargs: Any) -> Agent[Any, Any]:
        captured["toolsets"] = kwargs.get("toolsets")
        return _build_structured_agent(output_data)

    return patch(target, side_effect=make_agent)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMcpToolsReachApiInWorkflow:
    """Verify MCP tools survive the full workflow → provider pipeline."""

    @pytest.mark.asyncio
    async def test_mcp_tools_included_when_workflow_has_no_tools_section(self) -> None:
        """MCP tools must appear in the Pydantic AI agent even without a tools: section."""
        provider = _make_provider_with_mcp()
        captured: dict[str, Any] = {}

        with _patch_build_agent(captured, {"content": "file contents here"}) as mock_build_agent:
            agent = AgentDef(
                name="reader",
                model="claude-3-5-sonnet-latest",
                prompt="Read the file at /tmp/test.txt",
                output={"content": OutputField(type="string", description="File contents")},
                routes=[RouteDef(to="$end")],
            )

            # Simulate what the engine does: tools=[] (no workflow tools defined)
            result = await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="Read the file at /tmp/test.txt",
                tools=[],
            )

        # The provider should have constructed a Pydantic AI agent.
        assert mock_build_agent.called

        toolsets = captured.get("toolsets")
        assert toolsets, "Expected at least one toolset to be passed to build_agent"
        assert isinstance(toolsets[0], MCPManagerToolset)

        # Issue #37: an empty tools filter must expose every MCP tool.
        tools = await toolsets[0].get_tools(None)
        tool_names = set(tools)
        assert "filesystem__read_file" in tool_names, (
            "MCP tool 'filesystem__read_file' was filtered out — this is the bug from issue #37"
        )
        assert "filesystem__write_file" in tool_names
        assert "web_search__search" in tool_names
        assert len(tool_names) == 3

        # The workflow should also have produced the canned output.
        assert result.content == {"content": "file contents here"}

    @pytest.mark.asyncio
    async def test_mcp_tools_included_in_full_workflow_engine_run(self, tmp_path) -> None:
        """End-to-end: WorkflowEngine → AgentExecutor → ClaudeProvider with MCP."""
        provider = _make_provider_with_mcp()
        captured: dict[str, Any] = {}

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
                            "args": [
                                "-y",
                                "@modelcontextprotocol/server-filesystem",
                                str(tmp_path),
                            ],
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

        with _patch_build_agent(captured, {"content": "hello world"}):
            engine = WorkflowEngine(config, provider)
            result = await engine.run({"path": str(tmp_path / "test.txt")})

        # Verify the workflow completed
        assert result["content"] == "hello world"

        # Verify MCP tools were included in the Pydantic AI agent
        toolsets = captured.get("toolsets")
        assert toolsets
        assert isinstance(toolsets[0], MCPManagerToolset)

        tools = await toolsets[0].get_tools(None)
        tool_names = set(tools)
        assert "filesystem__read_file" in tool_names, (
            "MCP tool missing from Pydantic AI agent — issue #37 regression"
        )
        assert "filesystem__write_file" in tool_names
        assert "web_search__search" in tool_names

    @pytest.mark.asyncio
    async def test_explicit_tool_filter_still_works(self) -> None:
        """When workflow defines specific tools, only those MCP tools are included."""
        provider = _make_provider_with_mcp()
        captured: dict[str, Any] = {}

        with _patch_build_agent(captured, {"content": "filtered"}):
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

        toolsets = captured.get("toolsets")
        assert toolsets
        assert isinstance(toolsets[0], MCPManagerToolset)

        tools = await toolsets[0].get_tools(None)
        tool_names = set(tools)

        # Only the explicitly listed MCP tool
        assert "filesystem__read_file" in tool_names
        assert "filesystem__write_file" not in tool_names
        assert "web_search__search" not in tool_names
        assert len(tool_names) == 1


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
