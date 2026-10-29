"""HTTP-boundary tests for the runner's provider and MCP spawn environment."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Literal
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import SecretStr

from conductor.config.schema import AgentDef, ProviderSettings
from conductor.engine.realm_credentials import resolve_realm_credentials
from conductor.exceptions import ProviderError
from conductor.execution.agent_wire import agent_spec_to_wire_body
from conductor.execution.types import AgentSpec
from conductor.providers.aca import AcaRuntimeProvider
from conductor.providers.base import AgentOutput
from conductor.redaction import REDACTED_MARKER
from tests.test_runner.conftest import (
    ClaudeDeliveryProvider,
    CopilotDeliveryProvider,
    DeliveryProvider,
    OpenAIDeliveryProvider,
    RunnerClientFactory,
)

pytestmark = pytest.mark.usefixtures("delivery_provider")


def _body(provider: str = "copilot", **extra: Any) -> dict[str, Any]:
    return {
        "agent": {"name": "a"},
        "rendered_prompt": "prompt",
        "inner_provider": provider,
        **extra,
    }


def _frames(response: httpx.Response) -> list[dict[str, Any]]:
    return [json.loads(line) for line in response.text.splitlines()]


@pytest.mark.parametrize("provider", ["copilot", "openai", "claude"])
async def test_factory_executes_each_supported_provider(
    provider: str, runner_client_factory: RunnerClientFactory
) -> None:
    # Requirement: all three model SDKs can be selected without a CLI inside the runner.
    async with runner_client_factory() as client:
        response = await client.post(
            "/execute",
            json=_body(
                provider,
                inner_provider_settings={"api_key": "model-key"},
                skill_directories=["/workspace/skills/review"],
                custom_agents=[{"name": "reviewer", "prompt": "Review."}],
            ),
        )
        assert response.status_code == 200
        frames = _frames(response)
        assert [frame["type"] for frame in frames] == ["agent_turn_start", "result"]
        assert sum(frame["type"] in {"result", "error"} for frame in frames) == 1
        assert frames[-1]["data"]["content"] == {"answer": f"{provider}-done"}
        assert frames[-1]["data"]["model"] == f"{provider}-model"
        assert "model-key" not in response.text
        instance = DeliveryProvider.instances[0]
        assert (
            type(instance)
            is {
                "copilot": CopilotDeliveryProvider,
                "openai": OpenAIDeliveryProvider,
                "claude": ClaudeDeliveryProvider,
            }[provider]
        )
        if provider == "copilot":
            assert instance.settings["provider_settings"].api_key.get_secret_value() == "model-key"
        else:
            assert instance.settings["api_key"] == "model-key"
        assert instance.calls[0]["skill_directories"] == ["/workspace/skills/review"]
        assert instance.calls[0]["custom_agents"] == [{"name": "reviewer", "prompt": "Review."}]
        assert instance.calls[0]["interrupt_signal"] is None
        assert instance.settings["mcp_servers"] is None
        assert instance.settings["resolve_environment"] is False
    assert DeliveryProvider.instances[0].closed


@pytest.mark.parametrize("provider", ["claude-agent-sdk", "hermes"])
async def test_unknown_provider_returns_named_follow_up(
    provider: str, runner_client_factory: RunnerClientFactory
) -> None:
    # Requirement: unsupported model SDKs fail before opening the event stream.
    async with runner_client_factory() as client:
        response = await client.post("/execute", json=_body(provider))
    assert response.status_code == 400
    assert provider in response.json()["error"]["message"]
    assert "follow-up" in response.json()["error"]["message"]
    assert not DeliveryProvider.instances


async def test_overlapping_overlays_keep_spawn_env_and_provider_isolated(
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: distinct in-flight overlays cannot close or contaminate each other.
    original_env = {"BASE": "base", "MCP_TOKEN": "default"}
    servers: dict[str, dict[str, Any]] = {"echo": {"command": "echo", "env": original_env}}
    async with runner_client_factory() as client:
        requests = [
            asyncio.create_task(
                client.post(
                    "/execute",
                    json=_body(mcp_servers=servers, env_overlay={"MCP_TOKEN": value}),
                )
            )
            for value in ("secret-one", "secret-two")
        ]
        await asyncio.wait_for(DeliveryProvider.started.wait(), timeout=5)
        assert len(DeliveryProvider.instances) == 2
        assert not any(instance.closed for instance in DeliveryProvider.instances)
        assert {
            instance.settings["mcp_servers"]["echo"]["env"]["MCP_TOKEN"]
            for instance in DeliveryProvider.instances
        } == {"secret-one", "secret-two"}
        assert all(
            instance.settings["mcp_servers"]["echo"]["env"]["BASE"] == "base"
            for instance in DeliveryProvider.instances
        )
        assert original_env["MCP_TOKEN"] == "default"
        assert os.environ.get("MCP_TOKEN") is None
        DeliveryProvider.release.set()
        responses = await asyncio.wait_for(asyncio.gather(*requests), timeout=5)
        assert all(response.status_code == 200 for response in responses)
        assert all(
            "secret-one" not in response.text and "secret-two" not in response.text
            for response in responses
        )
        assert all(_frames(response)[-1]["type"] == "result" for response in responses)
    assert all(instance.closed for instance in DeliveryProvider.instances)


async def test_overlay_without_stdio_server_is_an_explicit_error(
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: an overlay with no spawn target is rejected, never dropped silently.
    async with runner_client_factory() as client:
        response = await client.post("/execute", json=_body(env_overlay={"MCP_TOKEN": "secret"}))
    assert response.status_code == 400
    assert "stdio MCP server" in response.json()["error"]["message"]
    assert "secret" not in response.text


async def test_error_frames_and_runner_logs_redact_overlay(
    caplog: pytest.LogCaptureFixture,
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: even an inner SDK error echoing the spawn secret is redacted.
    async with runner_client_factory() as client:
        response = await client.post(
            "/execute",
            json=_body(
                mcp_servers={"echo": {"command": "echo"}},
                env_overlay={"MCP_TOKEN": "private-overlay"},
                context={"raise_with_secret": True},
            ),
        )
    assert response.status_code == 200
    assert _frames(response)[-1]["type"] == "error"
    assert "private-overlay" not in response.text
    assert "private-overlay" not in caplog.text


@pytest.mark.parametrize("transport", ["http", "sse"])
async def test_remote_mcp_header_secret_is_scrubbed_from_events_errors_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    runner_client_factory: RunnerClientFactory,
    transport: str,
) -> None:
    # Requirement: all HTTP/SSE header secrets are redacted from events and diagnostics.
    secret = "TEST_SECRET_SENTINEL"

    async def echo_header(
        self: CopilotDeliveryProvider,
        agent: AgentDef,
        context: dict[str, Any],
        prompt: str,
        **kwargs: Any,
    ) -> AgentOutput:
        value = self.settings["mcp_servers"]["remote"]["headers"]["X-Api-Key"]
        kwargs["event_callback"]("agent_message", {"content": value})
        raise RuntimeError(f"header failure: {value}")

    monkeypatch.setattr(CopilotDeliveryProvider, "execute", echo_header)
    async with runner_client_factory() as client:
        response = await client.post(
            "/execute",
            json=_body(
                agent={"name": "header-agent"},
                execution_id="header-call",
                mcp_servers={
                    "remote": {
                        "type": transport,
                        "url": "https://mcp.example.test",
                        "headers": {"X-Api-Key": secret},
                    }
                },
            ),
        )
    frames = _frames(response)
    assert response.status_code == 200
    assert [frame["type"] for frame in frames] == ["agent_message", "error"]
    assert frames[0]["data"]["content"] == REDACTED_MARKER
    assert REDACTED_MARKER in frames[1]["data"]["message"]
    assert secret not in response.text
    assert secret not in caplog.text
    assert "execution_id=header-call" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text


@pytest.mark.parametrize("provider", ["copilot", "openai", "claude"])
async def test_sampling_settings_reach_distinct_provider_constructors(
    provider: str, runner_client_factory: RunnerClientFactory
) -> None:
    # Requirement: runner constructs the selected SDK with host-effective sampling settings.
    async with runner_client_factory() as client:
        response = await client.post(
            "/execute",
            json=_body(
                provider,
                agent={
                    "name": "sampler",
                    "temperature": 0.37,
                    "max_tokens": 2048 if provider != "copilot" else None,
                },
                inner_provider_settings={"api_key": "specific-key"},
            ),
        )
    assert response.status_code == 200
    instance = DeliveryProvider.instances[0]
    assert instance.settings["temperature"] == 0.37
    if provider != "copilot":
        assert instance.settings["max_tokens"] == 2048


async def test_copilot_refuses_unsupported_output_limit(
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: a requested Copilot output limit cannot be silently discarded.
    async with runner_client_factory() as client:
        response = await client.post(
            "/execute", json=_body(agent={"name": "sampler", "max_tokens": 512})
        )
    assert response.status_code == 400
    assert "max_tokens" in response.text
    assert not DeliveryProvider.instances


async def test_claude_bearer_credential_is_forwarded_to_auth_token(
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: Anthropic bearer auth cannot silently become an API key or vanish.
    async with runner_client_factory() as client:
        response = await client.post(
            "/execute",
            json=_body(
                "claude",
                inner_provider_settings={
                    "base_url": "https://gateway.example.test",
                    "bearer_token": "claude-bearer",
                },
            ),
        )
    assert response.status_code == 200
    instance = DeliveryProvider.instances[0]
    assert type(instance) is ClaudeDeliveryProvider
    assert instance.settings["auth_token"] == "claude-bearer"
    assert instance.settings["api_key"] is None
    assert instance.settings["base_url"] == "https://gateway.example.test"
    assert "claude-bearer" not in response.text


@pytest.mark.parametrize("provider", ["copilot", "openai", "claude"])
async def test_host_credentials_survive_wire_to_matching_provider(
    provider: Literal["copilot", "openai", "claude"],
    monkeypatch: pytest.MonkeyPatch,
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: authored credentials and endpoint reach only their matching SDK.
    monkeypatch.delenv("COPILOT_PROVIDER_RUNTIME_URL", raising=False)
    monkeypatch.delenv("COPILOT_PROVIDER_BASE_URL", raising=False)
    monkeypatch.delenv("COPILOT_PROVIDER_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    settings = ProviderSettings(
        name=provider,
        base_url=f"https://{provider}.example.test",
        api_key=SecretStr(f"{provider}-key"),
    )
    spec = AgentSpec(
        name="routed",
        execution_id=f"{provider}-call",
        model_provider=provider,
        model="model",
        rendered_prompt="task",
        provider_credentials=resolve_realm_credentials(provider, settings),
        temperature=0.23,
        max_tokens=1536 if provider != "copilot" else None,
    )
    async with runner_client_factory() as client:
        response = await client.post("/execute", json=agent_spec_to_wire_body(spec))
    assert response.status_code == 200
    instance = DeliveryProvider.instances[0]
    assert (
        type(instance)
        is {
            "copilot": CopilotDeliveryProvider,
            "openai": OpenAIDeliveryProvider,
            "claude": ClaudeDeliveryProvider,
        }[provider]
    )
    if provider == "copilot":
        assert instance.settings["provider_settings"].base_url == settings.base_url
        assert instance.settings["provider_settings"].bearer_token is None
        assert (
            instance.settings["provider_settings"].api_key.get_secret_value() == f"{provider}-key"
        )
    else:
        assert instance.settings["base_url"] == settings.base_url
        assert instance.settings["api_key"] == f"{provider}-key"
        assert instance.settings["max_tokens"] == 1536
    assert instance.settings["temperature"] == 0.23
    assert instance.settings["resolve_environment"] is False
    terminal = _frames(response)[-1]["data"]
    assert terminal["content"] == {"answer": f"{provider}-done"}
    assert terminal["model"] == f"{provider}-model"


async def test_unexpected_exception_logs_redacted_trace_and_execution_identity(
    caplog: pytest.LogCaptureFixture,
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: diagnostic trace and invocation ID survive without exposing credentials.
    async with runner_client_factory() as client:
        response = await client.post(
            "/execute",
            json=_body(
                execution_id="trace-42",
                mcp_servers={"echo": {"command": "echo"}},
                env_overlay={"MCP_TOKEN": "private-overlay"},
                context={"raise_with_secret": True},
            ),
        )
    assert _frames(response)[-1]["type"] == "error"
    assert "trace-42" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "raise RuntimeError" in caplog.text
    assert "private-overlay" not in caplog.text


async def test_matching_overlay_reuses_instance_and_idle_lru_is_bounded(
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: a changing overlay cannot reuse stale credentials; idle SDKs are bounded.
    DeliveryProvider.release.set()
    servers = {"echo": {"command": "echo"}}
    async with runner_client_factory() as client:
        for index in range(17):
            response = await client.post(
                "/execute",
                json=_body(mcp_servers=servers, env_overlay={"MCP_TOKEN": f"secret-{index}"}),
            )
            assert response.status_code == 200
        assert len(DeliveryProvider.instances) == 17
        assert DeliveryProvider.instances[0].closed
        assert sum(not instance.closed for instance in DeliveryProvider.instances) == 16
        response = await client.post(
            "/execute",
            json=_body(mcp_servers=servers, env_overlay={"MCP_TOKEN": "secret-16"}),
        )
        assert response.status_code == 200
        assert len(DeliveryProvider.instances) == 17


async def test_malformed_request_is_rejected_before_streaming(
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: malformed requests cannot report a successful NDJSON stream.
    async with runner_client_factory() as client:
        response = await client.post("/execute", json={"agent": {"name": "a"}, "unknown": 1})
    assert response.status_code == 422
    assert not DeliveryProvider.instances


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (b'{"type":', json.JSONDecodeError),
        (b'{"type":"agent_message","data":{}}\n', ProviderError),
    ],
    ids=["malformed", "truncated"],
)
async def test_malformed_or_truncated_stream_fails_at_host_boundary(
    body: bytes, error: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: an invalid or incomplete runner stream cannot look successful.
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "host-token")
    with patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True):
        provider = AcaRuntimeProvider(
            provider_settings=ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
        )
    provider._get_access_token = AsyncMock(return_value="transport-token")
    provider._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    )
    try:
        with pytest.raises(error):
            await provider.execute(
                AgentDef(
                    name="agent",
                    prompt="task",
                    timeout_seconds=None,
                    max_agent_iterations=None,
                    max_session_seconds=None,
                ),
                {},
                "rendered",
            )
    finally:
        await provider.close()
