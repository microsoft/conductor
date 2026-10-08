"""Shared ASGI runner fixtures for daemon-free contract tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
import pytest

from conductor.aca_runner import server
from conductor.providers.base import AgentOutput

type RunnerClientFactory = Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]]


class DeliveryProvider:
    """Canned provider for component delivery, concurrent overlays and cache lifetime."""

    instances: list[DeliveryProvider] = []
    started = asyncio.Event()
    release = asyncio.Event()

    def __init__(self, **settings: Any) -> None:
        self.settings = settings
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.instances.append(self)

    async def execute(self, agent: Any, context: Any, prompt: str, **kwargs: Any) -> AgentOutput:
        self.calls.append(kwargs)
        callback = kwargs["event_callback"]
        callback("agent_turn_start", {"turn": "awaiting_model"})
        mcp = self.settings["mcp_servers"]
        if mcp:
            callback("agent_message", {"content": mcp["echo"]["env"]["MCP_TOKEN"]})
        if context.get("raise_with_secret"):
            raise RuntimeError(f"MCP failed: {mcp['echo']['env']['MCP_TOKEN']}")
        if len(self.instances) >= 2:
            self.started.set()
        if mcp:
            await self.release.wait()
        return AgentOutput(content={"answer": "done"}, raw_response=None, model="fake")

    async def close(self) -> None:
        self.closed = True


class InterruptibleProvider:
    """Canned provider whose in-flight executions expose exact event barriers."""

    started: dict[str, asyncio.Event] = {}
    finish: dict[str, asyncio.Event] = {}

    def __init__(self, **settings: Any) -> None:
        pass

    async def execute(
        self, agent: Any, context: dict[str, Any], prompt: str, **kwargs: Any
    ) -> AgentOutput:
        execution_id = context["execution_id"]
        self.started[execution_id].set()
        interrupt = kwargs["interrupt_signal"]
        if interrupt is None:
            await self.finish[execution_id].wait()
            partial = False
        else:
            completed, pending = await asyncio.wait(
                [
                    asyncio.create_task(interrupt.wait()),
                    asyncio.create_task(self.finish[execution_id].wait()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            partial = interrupt.is_set()
        return AgentOutput(
            content={"execution_id": execution_id}, raw_response=None, partial=partial
        )

    async def close(self) -> None:
        pass


@asynccontextmanager
async def runner_client() -> AsyncIterator[httpx.AsyncClient]:
    """Drive actual runner routes and release its provider cache on exit."""
    app = server.create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as client,
    ):
        yield client


@pytest.fixture
def runner_client_factory() -> RunnerClientFactory:
    """Expose a fresh runner app for each client context."""
    return runner_client


@pytest.fixture
def delivery_provider(monkeypatch: pytest.MonkeyPatch) -> type[DeliveryProvider]:
    """Install a fresh delivery fake for each real HTTP contract test."""
    DeliveryProvider.instances = []
    DeliveryProvider.started = asyncio.Event()
    DeliveryProvider.release = asyncio.Event()
    monkeypatch.delenv("ACA_RUNNER_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ACA_RUNNER_ALLOWED_BASE_URLS", raising=False)
    monkeypatch.delenv("MCP_TOKEN", raising=False)
    for name in ("CopilotProvider", "OpenAIProvider", "ClaudeProvider"):
        monkeypatch.setattr(server, name, DeliveryProvider)
    return DeliveryProvider


@pytest.fixture
def interruptible_provider(monkeypatch: pytest.MonkeyPatch) -> type[InterruptibleProvider]:
    """Install a fresh interruptible fake before each runner HTTP test."""
    InterruptibleProvider.started = {}
    InterruptibleProvider.finish = {}
    monkeypatch.setattr(server, "CopilotProvider", InterruptibleProvider)
    monkeypatch.delenv("ACA_RUNNER_AUTH_TOKEN", raising=False)
    return InterruptibleProvider
