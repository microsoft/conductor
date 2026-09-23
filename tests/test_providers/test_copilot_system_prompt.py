"""Tests for native Copilot system-message forwarding."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from conductor.config.schema import AgentDef
from conductor.providers.copilot import CopilotProvider


def _build_provider(captured: dict[str, Any]) -> tuple[CopilotProvider, AsyncMock]:
    provider = CopilotProvider(model="custom-model")
    provider._started = True

    session = AsyncMock()
    session.session_id = "session-id"
    callbacks: dict[str, Any] = {}

    def on_event(callback: Any) -> None:
        callbacks["event"] = callback

    async def send(prompt: str) -> None:
        captured["sent_prompt"] = prompt
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


@pytest.mark.asyncio
async def test_system_prompt_uses_native_system_message() -> None:
    captured: dict[str, Any] = {}
    provider, client = _build_provider(captured)
    agent = AgentDef(
        name="solo",
        model="custom-model",
        prompt="hi",
        system_prompt="Follow the editorial policy.",
        timeout_seconds=None,
        max_session_seconds=None,
        max_agent_iterations=None,
    )

    with (
        patch("conductor.cli.app.is_verbose", return_value=False),
        patch("conductor.cli.app.is_full", return_value=False),
    ):
        await provider.execute(agent, context={}, rendered_prompt="Summarize today's news.")

    # Requirement: static instructions use Copilot's native system channel.
    assert client.create_session.call_args.kwargs["system_message"] == {
        "mode": "replace",
        "content": "Follow the editorial policy.",
    }
    assert captured["sent_prompt"] == "Summarize today's news."


@pytest.mark.asyncio
async def test_system_message_is_omitted_without_system_prompt() -> None:
    captured: dict[str, Any] = {}
    provider, client = _build_provider(captured)
    agent = AgentDef(
        name="solo",
        model="custom-model",
        prompt="hi",
        timeout_seconds=None,
        max_session_seconds=None,
        max_agent_iterations=None,
    )

    with (
        patch("conductor.cli.app.is_verbose", return_value=False),
        patch("conductor.cli.app.is_full", return_value=False),
    ):
        await provider.execute(agent, context={}, rendered_prompt="hi")

    # Requirement: no authored system prompt preserves the SDK defaults.
    assert "system_message" not in client.create_session.call_args.kwargs
    assert captured["sent_prompt"] == "hi"


@pytest.mark.asyncio
async def test_resumed_session_receives_system_message() -> None:
    captured: dict[str, Any] = {}
    provider, client = _build_provider(captured)
    provider.set_resume_session_ids({"solo": "previous-session"})
    provider.set_resume_session_cwds({"solo": os.getcwd()})
    agent = AgentDef(
        name="solo",
        model="custom-model",
        prompt="hi",
        system_prompt="Keep the established role.",
        timeout_seconds=None,
        max_session_seconds=None,
        max_agent_iterations=None,
    )

    with (
        patch("conductor.cli.app.is_verbose", return_value=False),
        patch("conductor.cli.app.is_full", return_value=False),
    ):
        await provider.execute(agent, context={}, rendered_prompt="Continue.")

    # Requirement: checkpoint resume retains the authored system-channel instructions.
    assert client.resume_session.call_args.kwargs["system_message"] == {
        "mode": "replace",
        "content": "Keep the established role.",
    }
    assert captured["sent_prompt"] == "Continue."
    client.create_session.assert_not_called()
