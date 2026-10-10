"""Tests for enforcing the resolved ``tools:`` allowlist on Copilot sessions.

The provider advertises ``workflow_tools_passthrough=True``, so the resolved
tool list the executor hands to ``execute()`` must reach the SDK as
``available_tools``. Without it the session falls back to the CLI's full
catalog (shell, file read/write, web, every MCP server), silently granting
tools the workflow did not declare.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from conductor.config.schema import AgentDef
from conductor.providers.copilot import CopilotProvider, copilot_tool_allowlist

WEB_SEARCH = {"web-search": {"type": "stdio", "command": "npx", "args": ["web-search"]}}


def _build_provider(
    mcp_servers: dict[str, Any] | None = None,
) -> tuple[CopilotProvider, AsyncMock]:
    provider = CopilotProvider(model="custom-model", mcp_servers=mcp_servers)
    provider._started = True

    session = AsyncMock()
    session.session_id = "session-id"
    callbacks: dict[str, Any] = {}

    def on_event(callback: Any) -> None:
        callbacks["event"] = callback

    async def send(prompt: str) -> None:
        callback = callbacks["event"]
        callback(
            SimpleNamespace(
                type=SimpleNamespace(value="assistant.message"),
                data=SimpleNamespace(message="ok", content="ok"),
            )
        )
        callback(
            SimpleNamespace(
                type=SimpleNamespace(value="session.idle"),
                data=SimpleNamespace(message="", content=""),
            )
        )

    session.on = on_event
    session.send = send
    session.destroy = AsyncMock()

    client = AsyncMock()
    client.create_session = AsyncMock(return_value=session)
    client.resume_session = AsyncMock(return_value=session)
    provider._client = client
    return provider, client


def _agent(tools: list[str] | None) -> AgentDef:
    return AgentDef(
        name="solo",
        model="custom-model",
        prompt="hi",
        tools=tools,
        timeout_seconds=None,
        max_session_seconds=None,
        max_agent_iterations=None,
    )


async def _run(
    provider: CopilotProvider,
    agent: AgentDef,
    resolved_tools: list[str] | None,
    extra_mcp_servers: dict[str, Any] | None = None,
) -> None:
    with (
        patch("conductor.cli.app.is_verbose", return_value=False),
        patch("conductor.cli.app.is_full", return_value=False),
    ):
        await provider.execute(
            agent,
            context={},
            rendered_prompt="hi",
            tools=resolved_tools,
            extra_mcp_servers=extra_mcp_servers,
        )


def _resuming(provider: CopilotProvider) -> CopilotProvider:
    provider.set_resume_session_ids({"solo": "previous-session"})
    provider.set_resume_session_cwds({"solo": os.getcwd()})
    return provider


@pytest.mark.asyncio
async def test_empty_tools_disables_every_tool() -> None:
    provider, client = _build_provider()

    await _run(provider, _agent(tools=[]), resolved_tools=[])

    # Requirement: ``tools: []`` means no tools at all, built-ins included.
    assert client.create_session.call_args.kwargs["available_tools"] == []


@pytest.mark.asyncio
async def test_named_tools_are_the_exact_allowlist() -> None:
    provider, client = _build_provider()

    await _run(provider, _agent(tools=["view"]), resolved_tools=["view"])

    # Requirement: a named list grants exactly those tools and nothing else.
    assert client.create_session.call_args.kwargs["available_tools"] == ["view"]


@pytest.mark.asyncio
async def test_omitted_tools_inherit_workflow_level_allowlist() -> None:
    provider, client = _build_provider()

    # The executor resolves an omitted agent ``tools:`` to the workflow-level list.
    await _run(provider, _agent(tools=None), resolved_tools=["view", "grep"])

    # Requirement: inheriting the workflow list restricts the session to it.
    assert client.create_session.call_args.kwargs["available_tools"] == ["view", "grep"]


@pytest.mark.asyncio
async def test_no_tools_declared_anywhere_keeps_cli_default_catalog() -> None:
    provider, client = _build_provider()

    # Neither the agent nor the workflow declares tools: the executor resolves [].
    await _run(provider, _agent(tools=None), resolved_tools=[])

    # Requirement: workflows that never mention tools keep the provider default.
    assert "available_tools" not in client.create_session.call_args.kwargs


@pytest.mark.asyncio
async def test_resumed_session_keeps_the_allowlist() -> None:
    provider, client = _build_provider()
    _resuming(provider)

    await _run(provider, _agent(tools=[]), resolved_tools=[])

    # Requirement: a checkpoint resume must not widen the agent's tools.
    assert client.resume_session.call_args.kwargs["available_tools"] == []
    client.create_session.assert_not_called()


@pytest.mark.asyncio
async def test_resumed_session_keeps_an_inherited_allowlist() -> None:
    provider, client = _build_provider()
    _resuming(provider)

    await _run(provider, _agent(tools=None), resolved_tools=["view", "grep"])

    # Requirement: resuming must not widen an allowlist inherited from the workflow.
    assert client.resume_session.call_args.kwargs["available_tools"] == ["view", "grep"]
    client.create_session.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_tool_names_are_translated_for_the_sdk() -> None:
    provider, client = _build_provider(mcp_servers=WEB_SEARCH)

    await _run(
        provider,
        _agent(tools=["web-search__search", "view"]),
        resolved_tools=["web-search__search", "view"],
    )

    # Requirement: Conductor's ``server__tool`` reaches the SDK as the runtime's
    # ``server-tool``, source-qualified; built-in names are forwarded unchanged.
    assert client.create_session.call_args.kwargs["available_tools"] == [
        "mcp:web-search-search",
        "view",
    ]


@pytest.mark.asyncio
async def test_resumed_session_translates_mcp_tool_names() -> None:
    provider, client = _build_provider(mcp_servers=WEB_SEARCH)
    _resuming(provider)

    await _run(provider, _agent(tools=None), resolved_tools=["web-search__search"])

    # Requirement: the resume path uses the same translated allowlist.
    assert client.resume_session.call_args.kwargs["available_tools"] == ["mcp:web-search-search"]
    client.create_session.assert_not_called()


@pytest.mark.asyncio
async def test_plugin_mcp_tool_names_are_translated() -> None:
    provider, client = _build_provider()
    plugin_servers = {"ado": {"type": "stdio", "command": "ado-mcp", "args": []}}

    await _run(
        provider,
        _agent(tools=["ado__get_work_item"]),
        resolved_tools=["ado__get_work_item"],
        extra_mcp_servers=plugin_servers,
    )

    # Requirement: plugin-contributed servers translate like workflow servers.
    assert client.create_session.call_args.kwargs["available_tools"] == ["mcp:ado-get_work_item"]


def test_allowlist_translation_edge_cases() -> None:
    # Omitted at both levels keeps the default catalog; explicit [] disables all.
    assert copilot_tool_allowlist(None, [], {"s"}) is None
    assert copilot_tool_allowlist([], [], {"s"}) == []
    # A prefix that is not a configured server is not an MCP tool name.
    assert copilot_tool_allowlist(["other__x"], ["other__x"], {"s"}) == ["other__x"]
    # The longest matching server name wins, including names containing ``__``.
    assert copilot_tool_allowlist(["a__b__tool"], ["a__b__tool"], {"a", "a__b"}) == [
        "mcp:a__b-tool"
    ]
    # A bare ``server__`` names no tool and is left for the SDK to reject.
    assert copilot_tool_allowlist(["s__"], ["s__"], {"s"}) == ["s__"]
