"""End-to-end: ``skill_directories`` reaches ``create_session`` on Copilot."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from conductor.config.schema import AgentDef
from conductor.providers.copilot import CopilotProvider


def _build_mocked_provider(captured: dict[str, Any]) -> CopilotProvider:
    provider = CopilotProvider(model="custom-model")
    provider._started = True

    session = AsyncMock()
    captured_callback: dict[str, Any] = {}

    def on_event(callback: Any) -> None:
        captured_callback["cb"] = callback

    session.on = on_event

    async def send(prompt: str) -> None:
        cb = captured_callback["cb"]

        def make_event(t: str, content: str = "") -> Any:
            ev = SimpleNamespace()
            ev.type = SimpleNamespace(value=t)
            ev.data = SimpleNamespace(message=content, content=content)
            return ev

        cb(make_event("assistant.message", "ok"))
        cb(make_event("session.idle"))

    session.send = send
    session.destroy = AsyncMock()

    async def create_session(**kwargs: Any) -> Any:
        captured["create_session_kwargs"] = kwargs
        return session

    client = AsyncMock()
    client.create_session = create_session
    provider._client = client
    return provider


@pytest.mark.asyncio
async def test_skill_directories_passed_to_create_session() -> None:
    captured: dict[str, Any] = {}
    provider = _build_mocked_provider(captured)
    agent = AgentDef(name="a", model="custom-model", prompt="hi")

    await provider.execute(
        agent,
        context={},
        rendered_prompt="hi",
        skill_directories=["/path/to/skill-a", "/path/to/skill-b"],
    )

    kwargs = captured["create_session_kwargs"]
    assert kwargs["skill_directories"] == ["/path/to/skill-a", "/path/to/skill-b"]


@pytest.mark.asyncio
async def test_skill_directories_absent_when_not_supplied() -> None:
    captured: dict[str, Any] = {}
    provider = _build_mocked_provider(captured)
    agent = AgentDef(name="a", model="custom-model", prompt="hi")

    await provider.execute(agent, context={}, rendered_prompt="hi")

    kwargs = captured["create_session_kwargs"]
    assert "skill_directories" not in kwargs


@pytest.mark.asyncio
async def test_empty_skill_directories_not_passed() -> None:
    """Empty list shouldn't set the kwarg — keeps default SDK behaviour."""
    captured: dict[str, Any] = {}
    provider = _build_mocked_provider(captured)
    agent = AgentDef(name="a", model="custom-model", prompt="hi")

    await provider.execute(agent, context={}, rendered_prompt="hi", skill_directories=[])

    kwargs = captured["create_session_kwargs"]
    assert "skill_directories" not in kwargs
