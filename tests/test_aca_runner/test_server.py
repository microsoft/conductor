"""Tests for the in-container `conductor-agent-runner` server (#284, epic E4).

Exercises `POST /execute` (NDJSON streaming, terminal `result` frame) and
`GET /health` against a mocked `CopilotProvider` — the real SDK spawns a
nested `copilot` process and is out of scope for these unit tests. See
docs/projects/aca/aca-provider.plan.md, epic E4.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from conductor.providers.base import AgentOutput


class _FakeCopilotProvider:
    """Stand-in for `conductor.providers.copilot.CopilotProvider`.

    Records every constructor call and every `execute()` call so tests can
    assert the runner forwards `mcp_servers`/`provider_settings`/`tool_output`
    at construction time and `agent`/`context`/`tools` at execute time.
    """

    instances: list[_FakeCopilotProvider] = []

    def __init__(
        self,
        mcp_servers: dict[str, Any] | None = None,
        provider_settings: Any | None = None,
        tool_output: Any | None = None,
        **kwargs: Any,
    ) -> None:
        self.mcp_servers = mcp_servers
        self.provider_settings = provider_settings
        self.tool_output = tool_output
        self.close = AsyncMock(return_value=None)
        self.execute_calls: list[dict[str, Any]] = []
        self.execute_error: Exception | None = None
        self._result = AgentOutput(
            content={"summary": "done"},
            raw_response=None,
            input_tokens=10,
            output_tokens=5,
            model="gpt-4.1",
        )
        _FakeCopilotProvider.instances.append(self)

    async def execute(
        self,
        agent: Any,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: Any | None = None,
        event_callback: Any | None = None,
    ) -> AgentOutput:
        self.execute_calls.append(
            {
                "agent": agent,
                "context": context,
                "rendered_prompt": rendered_prompt,
                "tools": tools,
            }
        )
        if self.execute_error is not None:
            raise self.execute_error
        if event_callback is not None:
            event_callback("agent_turn_start", {"turn": "awaiting_model"})
            event_callback("agent_message", {"content": "hi"})
        return self._result


class _DelayedCloseFakeCopilotProvider(_FakeCopilotProvider):
    """`_FakeCopilotProvider` whose `close()` yields to the event loop.

    Used to widen the race window in `_InnerProviderCache` concurrency
    tests: without a real `await` suspension inside `close()`, two
    concurrent `get()` calls racing on the same stale cache entry might not
    reliably interleave under cooperative scheduling.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        async def _delayed_close() -> None:
            await asyncio.sleep(0.01)

        self.close = AsyncMock(side_effect=_delayed_close)


@pytest.fixture(autouse=True)
def _reset_fake_provider_instances() -> Any:
    _FakeCopilotProvider.instances = []
    yield
    _FakeCopilotProvider.instances = []


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("conductor.aca_runner.server.CopilotProvider", _FakeCopilotProvider)
    from conductor.aca_runner.server import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def _execute_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "agent": {"name": "implement", "model": "gpt-4.1"},
        "rendered_prompt": "do the thing",
        "tools": ["git"],
        "mcp_servers": None,
        "context": {},
        "inner_provider": "copilot",
    }
    body.update(overrides)
    return body


def _parse_ndjson(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class TestHealth:
    """`GET /health` (E4-T1) — readiness + Conductor/runner version."""

    def test_health_reports_readiness_and_version(self, client: TestClient) -> None:
        from conductor import __version__ as conductor_version

        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert body["conductor_version"] == conductor_version
        assert "runner_version" in body


class TestExecuteStreaming:
    """`POST /execute` (E4-T2) — NDJSON event frames + terminal `result`."""

    def test_execute_streams_events_and_terminal_result(self, client: TestClient) -> None:
        response = client.post("/execute", json=_execute_body())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")

        frames = _parse_ndjson(response.text)
        assert frames[0] == {"type": "agent_turn_start", "data": {"turn": "awaiting_model"}}
        assert frames[1] == {"type": "agent_message", "data": {"content": "hi"}}
        result = frames[-1]
        assert result["type"] == "result"
        assert result["data"]["content"] == {"summary": "done"}
        assert result["data"]["model"] == "gpt-4.1"
        assert result["data"]["input_tokens"] == 10
        assert result["data"]["output_tokens"] == 5
        assert result["data"]["partial"] is False
        assert isinstance(result["data"]["session_seconds"], (int, float))
        assert result["data"]["session_seconds"] >= 0

    def test_execute_forwards_tools_and_mcp_servers_to_inner_provider(
        self, client: TestClient
    ) -> None:
        mcp_servers = {"git": {"type": "stdio", "command": "echo", "args": ["hi"], "tools": ["*"]}}
        response = client.post(
            "/execute",
            json=_execute_body(tools=["git", "read_file"], mcp_servers=mcp_servers),
        )
        assert response.status_code == 200
        _parse_ndjson(response.text)  # drain the stream

        assert len(_FakeCopilotProvider.instances) == 1
        instance = _FakeCopilotProvider.instances[0]
        assert instance.mcp_servers == mcp_servers
        assert len(instance.execute_calls) == 1
        assert instance.execute_calls[0]["tools"] == ["git", "read_file"]
        assert instance.execute_calls[0]["agent"].name == "implement"
        assert instance.execute_calls[0]["rendered_prompt"] == "do the thing"

    def test_execute_reuses_provider_across_calls_with_same_settings(
        self, client: TestClient
    ) -> None:
        client.post("/execute", json=_execute_body())
        client.post("/execute", json=_execute_body())
        assert len(_FakeCopilotProvider.instances) == 1

    def test_execute_reconstructs_provider_when_mcp_servers_change(
        self, client: TestClient
    ) -> None:
        client.post("/execute", json=_execute_body())
        client.post(
            "/execute",
            json=_execute_body(mcp_servers={"git": {"type": "stdio", "command": "echo"}}),
        )
        assert len(_FakeCopilotProvider.instances) == 2
        assert _FakeCopilotProvider.instances[0].close.await_count == 1


class TestExecuteMissingStdioBinary:
    """Runner-image contract (E4-T3): a missing stdio binary fails loudly."""

    def test_missing_stdio_binary_surfaces_as_runner_error(self, client: TestClient) -> None:
        mcp_servers = {
            "ghost": {
                "type": "stdio",
                "command": "definitely-not-a-real-binary-xyz",
                "args": [],
                "tools": ["*"],
            }
        }
        response = client.post("/execute", json=_execute_body(mcp_servers=mcp_servers))

        assert response.status_code >= 400
        body = response.json()
        message = body.get("error", body).get("message", "")
        assert "definitely-not-a-real-binary-xyz" in message
        # The provider must never be constructed/executed for a rejected
        # request — this is a hard failure, not a silent drop of the tool.
        assert _FakeCopilotProvider.instances == []

    def test_remote_mcp_server_does_not_require_a_binary(self, client: TestClient) -> None:
        mcp_servers = {"remote": {"type": "http", "url": "https://example.com/mcp", "tools": ["*"]}}
        response = client.post("/execute", json=_execute_body(mcp_servers=mcp_servers))
        assert response.status_code == 200


class TestExecuteInnerProviderCredentials:
    """OQ#6 stopgap (E4-T4): `inner_provider_settings` builds `ProviderSettings`."""

    def test_inner_provider_settings_build_copilot_provider_settings(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/execute",
            json=_execute_body(inner_provider_settings={"bearer_token": "stopgap-token-value"}),
        )
        assert response.status_code == 200
        _parse_ndjson(response.text)

        instance = _FakeCopilotProvider.instances[0]
        assert instance.provider_settings is not None
        assert instance.provider_settings.name == "copilot"
        assert instance.provider_settings.bearer_token.get_secret_value() == ("stopgap-token-value")

    def test_unsupported_inner_provider_is_rejected(self, client: TestClient) -> None:
        response = client.post("/execute", json=_execute_body(inner_provider="claude-agent-sdk"))
        assert response.status_code >= 400
        assert _FakeCopilotProvider.instances == []


class TestExecuteValidatesBeforeStreaming:
    """Review fix: agent reconstruction runs before `StreamingResponse` opens.

    An invalid agent payload (e.g. a bad `context_tier` literal) must
    surface as a clean, non-streaming 4xx JSON error — not as a broken
    mid-stream frame after a 200 has already been sent.
    """

    def test_invalid_context_tier_returns_400_without_opening_stream(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/execute",
            json=_execute_body(agent={"name": "implement", "context_tier": "not-a-real-tier"}),
        )
        assert response.status_code == 400
        assert not response.headers["content-type"].startswith("application/x-ndjson")
        body = response.json()
        message = body.get("error", body).get("message", "")
        assert "context_tier" in message
        # The provider must never be constructed for a rejected payload —
        # confirms validation ran before the cache lookup / stream, too.
        assert _FakeCopilotProvider.instances == []


class TestExecuteTerminalError:
    """A failing inner `execute()` call still terminates the stream cleanly."""

    def test_execute_failure_after_stream_open_yields_error_frame(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original_execute = _FakeCopilotProvider.execute

        async def _failing_execute(self: _FakeCopilotProvider, *args: Any, **kwargs: Any) -> Any:
            self.execute_error = RuntimeError("inner SDK exploded")
            return await original_execute(self, *args, **kwargs)

        monkeypatch.setattr(_FakeCopilotProvider, "execute", _failing_execute)

        response = client.post("/execute", json=_execute_body())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")

        frames = _parse_ndjson(response.text)
        terminal = frames[-1]
        assert terminal["type"] == "error"
        assert "inner SDK exploded" in terminal["data"]["message"]
        # Exactly one terminal frame — never a result *and* an error.
        assert sum(1 for f in frames if f["type"] in ("result", "error")) == 1


class TestExecuteForwardsRetryAndContextTier:
    """Review fix: `retry` and `context_tier` reach the inner `AgentDef`."""

    def test_retry_and_context_tier_are_forwarded_to_inner_agent(self, client: TestClient) -> None:
        response = client.post(
            "/execute",
            json=_execute_body(
                agent={
                    "name": "implement",
                    "model": "gpt-4.1",
                    "context_tier": "long_context",
                    "retry": {"max_attempts": 5, "backoff": "fixed", "delay_seconds": 1.0},
                }
            ),
        )
        assert response.status_code == 200
        _parse_ndjson(response.text)

        instance = _FakeCopilotProvider.instances[0]
        agent = instance.execute_calls[0]["agent"]
        assert agent.context_tier == "long_context"
        assert agent.retry is not None
        assert agent.retry.max_attempts == 5
        assert agent.retry.backoff == "fixed"

    def test_no_retry_or_context_tier_forwards_none(self, client: TestClient) -> None:
        response = client.post("/execute", json=_execute_body())
        assert response.status_code == 200
        _parse_ndjson(response.text)

        instance = _FakeCopilotProvider.instances[0]
        agent = instance.execute_calls[0]["agent"]
        assert agent.retry is None
        assert agent.context_tier is None


class TestInnerProviderCacheConcurrency:
    """Review fix: concurrent `get()` calls don't corrupt cache state."""

    async def test_concurrent_rebuilds_close_the_stale_provider_exactly_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "conductor.aca_runner.server.CopilotProvider", _DelayedCloseFakeCopilotProvider
        )
        from conductor.aca_runner.server import _InnerProviderCache

        cache = _InnerProviderCache()
        # Prime the cache with an initial provider (key1).
        await cache.get(mcp_servers=None, inner_provider_settings=None, tool_output=None)
        assert len(_FakeCopilotProvider.instances) == 1
        first = _FakeCopilotProvider.instances[0]

        # Two concurrent requests, each with a *different* key from the
        # cached one (and from each other), racing to rebuild.
        results = await asyncio.gather(
            cache.get(
                mcp_servers={"git": {"type": "stdio", "command": "echo"}},
                inner_provider_settings=None,
                tool_output=None,
            ),
            cache.get(
                mcp_servers={"grep": {"type": "stdio", "command": "echo"}},
                inner_provider_settings=None,
                tool_output=None,
            ),
        )

        # The initial provider must be closed exactly once — a race in the
        # unlocked check-close-rebuild sequence would double-close it.
        assert first.close.await_count == 1
        # No provider is ever silently orphaned: every constructed instance
        # is either the live cache entry or has been closed.
        live = cache._provider
        for instance in _FakeCopilotProvider.instances:
            if instance is not live:
                assert instance.close.await_count == 1
        # Both concurrent callers got a real, live provider back.
        assert all(r is not None for r in results)
        assert cache._key is not None

    async def test_concurrent_get_with_same_key_returns_single_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "conductor.aca_runner.server.CopilotProvider", _DelayedCloseFakeCopilotProvider
        )
        from conductor.aca_runner.server import _InnerProviderCache

        cache = _InnerProviderCache()
        results = await asyncio.gather(
            *[
                cache.get(mcp_servers=None, inner_provider_settings=None, tool_output=None)
                for _ in range(5)
            ]
        )
        assert len(_FakeCopilotProvider.instances) == 1
        assert all(r is results[0] for r in results)
