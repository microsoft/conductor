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
from conductor.providers.copilot import CopilotProvider


def _build_provider() -> tuple[CopilotProvider, AsyncMock]:
    provider = CopilotProvider(model="custom-model")
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
    provider: CopilotProvider, agent: AgentDef, resolved_tools: list[str] | None
) -> None:
    with (
        patch("conductor.cli.app.is_verbose", return_value=False),
        patch("conductor.cli.app.is_full", return_value=False),
    ):
        await provider.execute(agent, context={}, rendered_prompt="hi", tools=resolved_tools)


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
    provider.set_resume_session_ids({"solo": "previous-session"})
    provider.set_resume_session_cwds({"solo": os.getcwd()})

    await _run(provider, _agent(tools=[]), resolved_tools=[])

    # Requirement: a checkpoint resume must not widen the agent's tools.
    assert client.resume_session.call_args.kwargs["available_tools"] == []
    client.create_session.assert_not_called()
