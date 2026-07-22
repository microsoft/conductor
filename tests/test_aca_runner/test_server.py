"""Tests for the in-container `conductor-agent-runner` server (#284, epic E4).

Exercises `POST /execute` (NDJSON streaming, terminal `result` frame) and
`GET /health` against a mocked `CopilotProvider` — the real SDK spawns a
nested `copilot` process and is out of scope for these unit tests. See
docs/projects/aca/aca-provider.plan.md, epic E4.
"""

from __future__ import annotations

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
        if event_callback is not None:
            event_callback("agent_turn_start", {"turn": "awaiting_model"})
            event_callback("agent_message", {"content": "hi"})
        return self._result


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
