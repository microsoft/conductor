"""Unit tests for the Pydantic AI MCP toolset adapter.

These tests verify that ``MCPManagerToolset`` bridges Conductor's ``MCPManager``
to Pydantic AI agents while preserving naming, schema passthrough, tool
execution delegation, error semantics, and output truncation behavior.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.models.test import TestModel

from conductor.config.schema import AgentDef, ToolOutputConfig
from conductor.mcp.manager import GENERIC_HINT, MCPManager
from conductor.providers._pydantic_ai.agent_builder import build_agent
from conductor.providers._pydantic_ai.mcp_toolset import MCPManagerToolset


class _FakeMCPManager:
    """A lightweight stand-in for ``MCPManager`` that tracks calls and results."""

    def __init__(self, tools: list[dict[str, Any]], result: str | None = None) -> None:
        self.tools = tools
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = result
        self._override_result: dict[str, Any] = {}

    def get_all_tools(self) -> list[dict[str, Any]]:
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        if name in self._override_result:
            result = self._override_result[name]
            if isinstance(result, Exception):
                raise result
            return result
        if self.result is not None:
            return self.result
        return f"ok:{name}"

    def set_result(self, name: str, result: str | Exception) -> None:
        self._override_result[name] = result


@pytest.mark.asyncio
async def test_toolset_exposes_prefixed_names() -> None:
    """Requirement: MCP tools are exposed as ``{server}__{tool}`` to the model.

    This matches the naming convention used by ClaudeProvider so that the
    same MCP server configuration produces identical tool names across
    providers and collisions between servers are avoided.
    """
    manager = _FakeMCPManager(
        [
            {"name": "fs__read", "description": "read file", "input_schema": {}},
            {"name": "web__search", "description": "search web", "input_schema": {}},
        ]
    )
    toolset = MCPManagerToolset(manager, None, None)
    tools = await toolset.get_tools(None)

    assert set(tools) == {"fs__read", "web__search"}


@pytest.mark.asyncio
async def test_toolset_schema_passthrough() -> None:
    """Requirement: each tool definition preserves its JSON schema from the manager.

    The model receives the exact parameter schema discovered by ``MCPManager``,
    not a regenerated or stripped-down version.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
    }
    manager = _FakeMCPManager(
        [{"name": "fs__read", "description": "read file", "input_schema": schema}]
    )
    toolset = MCPManagerToolset(manager, None, None)
    tools = await toolset.get_tools(None)

    assert tools["fs__read"].tool_def.parameters_json_schema == schema


@pytest.mark.asyncio
async def test_toolset_allowlist_filter() -> None:
    """Requirement: passing a non-empty ``tool_names`` allowlist restricts exposed tools.

    ``None`` grants all tools, ``[]`` grants none, and a list grants only the
    listed names. This mirrors the ``tools: [...]`` filtering semantics in
    Conductor's agent definitions.
    """
    manager = _FakeMCPManager(
        [
            {"name": "fs__read", "description": "read", "input_schema": {}},
            {"name": "fs__write", "description": "write", "input_schema": {}},
        ]
    )
    all_tools = await MCPManagerToolset(manager, None, None).get_tools(None)
    assert set(all_tools) == {"fs__read", "fs__write"}

    allowed_tools = await MCPManagerToolset(manager, ["fs__read"], None).get_tools(None)
    assert set(allowed_tools) == {"fs__read"}

    no_tools = await MCPManagerToolset(manager, [], None).get_tools(None)
    assert no_tools == {}


@pytest.mark.asyncio
async def test_call_tool_delegates_to_manager() -> None:
    """Requirement: executing a tool delegates to the same ``MCPManager.call_tool`` path.

    The adapter must not reimplement execution or truncation; it forwards the
    prefixed name and arguments to the manager and returns the manager's string.
    """
    manager = _FakeMCPManager(
        [{"name": "fs__read", "description": "read", "input_schema": {}}],
        result="file contents",
    )
    toolset = MCPManagerToolset(manager, None, None)
    tools = await toolset.get_tools(None)
    result = await toolset.call_tool("fs__read", {"path": "/etc/passwd"}, None, tools["fs__read"])

    assert result == "file contents"
    assert manager.calls == [("fs__read", {"path": "/etc/passwd"})]


@pytest.mark.asyncio
async def test_call_tool_emits_truncation_event() -> None:
    # Requirement: production MCP calls surface truncation metadata to subscribers.
    marker = (
        "\n\n[output truncated: 2000 chars -> 1000 kept; "
        f"full output saved to: /tmp/spill.txt. {GENERIC_HINT}]"
    )
    manager = _FakeMCPManager(
        [{"name": "fs__read", "description": "read", "input_schema": {}}],
        result="x" * 1000 + marker,
    )
    recorded: list[tuple[str, dict[str, Any]]] = []
    toolset = MCPManagerToolset(
        manager,
        None,
        None,
        event_callback=lambda event_type, data: recorded.append((event_type, data)),
    )
    tools = await toolset.get_tools(None)
    agent = build_agent(
        AgentDef(name="truncation-agent"),
        system_prompt="",
        rendered_prompt="",
        api_key="dummy",
    )
    ctx = type("ToolContext", (), {"agent": agent})()

    await toolset.call_tool("fs__read", {}, ctx, tools["fs__read"])

    assert recorded == [
        (
            "agent_tool_output_truncated",
            {
                "agent_name": "truncation-agent",
                "tool_name": "fs__read",
                "original_chars": 2000,
                "kept_chars": 1000,
                "spill_path": "/tmp/spill.txt",
            },
        )
    ]


@pytest.mark.asyncio
async def test_call_tool_error_becomes_tool_failed() -> None:
    """Requirement: a failing ``MCPManager.call_tool`` returns an error tool result.

    The adapter raises ``pydantic_ai.exceptions.ToolFailed``. The Pydantic AI
    agent loop converts this to a ``ToolReturnPart`` with ``outcome='failed'``,
    which Anthropic maps to ``is_error=True``. This preserves ClaudeProvider's
    behavior of NOT crashing the agent loop on MCP errors.
    """
    manager = _FakeMCPManager([{"name": "fs__read", "description": "read", "input_schema": {}}])
    manager.set_result("fs__read", RuntimeError("disk error"))
    toolset = MCPManagerToolset(manager, None, None)
    tools = await toolset.get_tools(None)

    with pytest.raises(Exception) as exc_info:
        await toolset.call_tool("fs__read", {"path": "/x"}, None, tools["fs__read"])

    assert "disk error" in str(exc_info.value)


def test_end_to_end_tool_call_via_build_agent() -> None:
    """Requirement: ``build_agent(toolsets=[MCPManagerToolset(...)])`` wires tools correctly.

    The Pydantic AI agent built by ``build_agent`` sees the MCP tools, can call
    them through the manager, and returns the manager's results.
    """
    manager = _FakeMCPManager(
        [
            {
                "name": "fs__read",
                "description": "read file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            }
        ],
        result="hello world",
    )
    toolset = MCPManagerToolset(manager, None, ToolOutputConfig())
    agent = build_agent(
        AgentDef(name="agent"),
        system_prompt="sys",
        rendered_prompt="",
        api_key="dummy",
        toolsets=[toolset],
    )

    result = agent.run_sync("read a file", model=TestModel())

    assert "hello world" in result.output
    assert manager.calls == [("fs__read", {})]


def test_end_to_end_error_result_outcome_is_failed() -> None:
    """Requirement: a failing MCP tool inside a full agent run yields a failed outcome.

    This confirms that the error-signaling mechanism survives the entire agent
    graph and is observable on the resulting ``ToolReturnPart``.
    """
    manager = _FakeMCPManager(
        [
            {"name": "good__tool", "description": "good", "input_schema": {"type": "object"}},
            {"name": "bad__tool", "description": "bad", "input_schema": {"type": "object"}},
        ]
    )
    manager.set_result("bad__tool", RuntimeError("boom"))
    toolset = MCPManagerToolset(manager, None, ToolOutputConfig())
    agent = build_agent(
        AgentDef(name="agent"),
        system_prompt="sys",
        rendered_prompt="",
        api_key="dummy",
        toolsets=[toolset],
    )

    result = agent.run_sync("call both", model=TestModel())

    # The TestModel final response contains the tool results as JSON.
    assert '"good__tool":"ok:good__tool"' in result.output
    assert '"bad__tool"' in result.output
    assert "boom" in result.output

    # Verify the actual message outcome is failed for the bad tool.
    from pydantic_ai.messages import ToolReturnPart

    tool_return_parts = [
        p
        for m in result.all_messages()
        for p in (m.parts if hasattr(m, "parts") else [])
        if isinstance(p, ToolReturnPart)
    ]
    bad_part = next(p for p in tool_return_parts if getattr(p, "tool_name", None) == "bad__tool")
    assert bad_part.outcome == "failed"


def test_end_to_end_truncation_passthrough() -> None:
    """Requirement: output truncation performed inside ``MCPManager.call_tool`` is preserved.

    The adapter returns the manager's string unchanged, including the truncation
    marker and spill path, so upstream consumers can detect truncation.
    """
    marker = "\n\n[output truncated: 2000 chars -> 1000 kept; full output saved to: /tmp/spill.txt."
    truncated_result = "x" * 1000 + marker

    manager = _FakeMCPManager(
        [{"name": "fs__read", "description": "read", "input_schema": {"type": "object"}}],
        result=truncated_result,
    )
    toolset = MCPManagerToolset(manager, None, ToolOutputConfig())
    agent = build_agent(
        AgentDef(name="agent"),
        system_prompt="sys",
        rendered_prompt="",
        api_key="dummy",
        toolsets=[toolset],
    )

    result = agent.run_sync("read a big file", model=TestModel())

    assert "[output truncated:" in result.output
    assert "/tmp/spill.txt" in result.output


def test_end_to_end_with_real_manager_truncate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Requirement: truncation and spill-to-file logic in ``MCPManager`` is reused.

    Using a real ``MCPManager`` with a fake session, this verifies that the
    adapter delegates to ``call_tool`` and the truncation/spill behavior is
    preserved without reimplementation.
    """
    import stat
    from types import SimpleNamespace

    monkeypatch.setattr("conductor.mcp.manager.tempfile.gettempdir", lambda: str(tmp_path))

    manager = MCPManager(tool_output=ToolOutputConfig(max_chars=1000, spill_to_file=True))
    manager.tool_to_server["big__data"] = "big"
    manager.tools["big"] = [
        {
            "name": "big__data",
            "description": "return big data",
            "input_schema": {"type": "object"},
            "server": "big",
            "original_name": "data",
        }
    ]

    class _FakeSession:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            return SimpleNamespace(
                content=[SimpleNamespace(text="x" * 5000)], structuredContent=None
            )

    manager.sessions["big"] = _FakeSession()
    toolset = MCPManagerToolset(manager, None, ToolOutputConfig())
    agent = build_agent(
        AgentDef(name="agent"),
        system_prompt="sys",
        rendered_prompt="",
        api_key="dummy",
        toolsets=[toolset],
    )

    result = agent.run_sync("fetch big data", model=TestModel())

    assert "[output truncated: 5000 chars -> 1000 kept" in result.output
    assert "full output saved to:" in result.output

    spill_file = next((tmp_path / "conductor" / "tool-output").glob("*.txt"))
    assert spill_file.exists()
    assert spill_file.read_text() == "x" * 5000
    assert stat.S_IMODE(spill_file.stat().st_mode) == 0o600


def test_end_to_end_with_real_manager_explicit_spill_dir(tmp_path: Any) -> None:
    """Requirement: explicit ``spill_dir`` in ``ToolOutputConfig`` is honored through the adapter.

    This covers the user-requested branch where a custom spill directory is
    configured; the adapter must still delegate truncation to ``MCPManager`` and
    the resulting spill file must land under the requested directory.
    """
    import stat
    from types import SimpleNamespace

    custom_dir = tmp_path / "custom-spill"

    manager = MCPManager(
        tool_output=ToolOutputConfig(max_chars=1000, spill_to_file=True, spill_dir=str(custom_dir))
    )
    manager.tool_to_server["big__data"] = "big"
    manager.tools["big"] = [
        {
            "name": "big__data",
            "description": "return big data",
            "input_schema": {"type": "object"},
            "server": "big",
            "original_name": "data",
        }
    ]

    class _FakeSession:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            return SimpleNamespace(
                content=[SimpleNamespace(text="x" * 5000)], structuredContent=None
            )

    manager.sessions["big"] = _FakeSession()
    toolset = MCPManagerToolset(manager, None, ToolOutputConfig())
    agent = build_agent(
        AgentDef(name="agent"),
        system_prompt="sys",
        rendered_prompt="",
        api_key="dummy",
        toolsets=[toolset],
    )

    result = agent.run_sync("fetch big data", model=TestModel())

    assert "[output truncated: 5000 chars -> 1000 kept" in result.output
    assert "full output saved to:" in result.output
    assert str(custom_dir) in result.output

    spill_file = next(custom_dir.glob("*.txt"))
    assert spill_file.exists()
    assert spill_file.read_text() == "x" * 5000
    assert stat.S_IMODE(spill_file.stat().st_mode) == 0o600


def test_attach_mcp_toolset_with_none_manager() -> None:
    """Requirement: attaching a toolset with no manager is a no-op."""
    from conductor.providers._pydantic_ai.mcp_toolset import attach_mcp_toolset

    agent = build_agent(
        AgentDef(name="agent"), system_prompt="", rendered_prompt="", api_key="dummy"
    )
    original_toolsets = list(agent._user_toolsets)
    attach_mcp_toolset(agent, None, None, None)
    assert agent._user_toolsets == original_toolsets


def test_attach_mcp_toolset_registers_toolset() -> None:
    """Requirement: ``attach_mcp_toolset`` registers a toolset on an existing agent."""
    from conductor.providers._pydantic_ai.mcp_toolset import attach_mcp_toolset

    manager = _FakeMCPManager(
        [{"name": "fs__read", "description": "read", "input_schema": {"type": "object"}}]
    )
    agent = build_agent(
        AgentDef(name="agent"), system_prompt="", rendered_prompt="", api_key="dummy"
    )
    attach_mcp_toolset(agent, manager, None, ToolOutputConfig())
    assert len(agent._user_toolsets) == 1
    assert isinstance(agent._user_toolsets[0], MCPManagerToolset)


def test_mcp_manager_toolset_id_is_stable() -> None:
    """Requirement: the toolset reports an ID derived from the manager instance."""
    manager = _FakeMCPManager([])
    toolset = MCPManagerToolset(manager, None, None)
    assert toolset.id is not None
    assert toolset.id == f"mcp_manager_{id(manager)}"


@pytest.mark.asyncio
async def test_call_tool_max_retries_is_zero() -> None:
    """Requirement: the adapter disables Pydantic AI's own per-tool retries.

    Conductor's engine manages retries, so the toolset must set ``max_retries=0``
    to avoid double retrying at the Pydantic AI layer.
    """
    manager = _FakeMCPManager([{"name": "t", "description": "", "input_schema": {}}])
    toolset = MCPManagerToolset(manager, None, None)
    tools = await toolset.get_tools(None)
    assert tools["t"].max_retries == 0
