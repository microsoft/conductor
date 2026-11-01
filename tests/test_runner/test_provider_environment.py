"""Resolved host credentials must not be reinterpreted in the runner."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel

from conductor.aca_runner.server import _InnerProviderCache
from conductor.config.schema import AgentDef, ProviderSettings
from conductor.engine.realm_credentials import resolve_realm_credentials
from conductor.exceptions import ValidationError
from conductor.execution.agent_wire import agent_spec_to_wire_body
from conductor.execution.types import AgentSpec
from conductor.providers.base import AgentOutput
from conductor.providers.claude import ClaudeProvider
from conductor.providers.copilot import CopilotProvider
from conductor.providers.openai import OpenAIProvider
from conductor.runner.protocol import RunnerAgentRequest


@pytest.mark.parametrize(
    ("provider_name", "credential"),
    [
        ("copilot", "api_key"),
        ("copilot", "bearer_token"),
        ("claude", "api_key"),
        ("claude", "auth_token"),
        ("openai", "api_key"),
    ],
)
async def test_runner_environment_cannot_change_host_effective_authentication(
    provider_name: Literal["copilot", "claude", "openai"],
    credential: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: real providers preserve absent host fields despite conflicting runner settings.
    runner_environment = {
        "COPILOT_PROVIDER_BASE_URL": "https://runner-copilot.example.test",
        "COPILOT_PROVIDER_API_KEY": "runner-copilot-key",
        "COPILOT_PROVIDER_BEARER_TOKEN": "runner-copilot-bearer",
        "COPILOT_PROVIDER_RUNTIME_URL": "http://runner-runtime.example.test",
        "COPILOT_PROVIDER_RUNTIME_TOKEN": "runner-runtime-token",
        "ANTHROPIC_BASE_URL": "https://runner-anthropic.example.test",
        "ANTHROPIC_API_KEY": "runner-anthropic-key",
        "ANTHROPIC_AUTH_TOKEN": "runner-anthropic-bearer",
        "OPENAI_BASE_URL": "https://runner-openai.example.test",
        "OPENAI_API_KEY": "runner-openai-key",
    }
    for name in runner_environment:
        monkeypatch.delenv(name, raising=False)
    host_settings = ProviderSettings.model_validate(
        {
            "name": provider_name,
            credential: SecretStr("host-credential"),
            **({"base_url": "https://host.example.test"} if provider_name == "copilot" else {}),
        }
    )
    request = RunnerAgentRequest.model_validate(
        agent_spec_to_wire_body(
            AgentSpec(
                name="isolated",
                execution_id="host-auth",
                model_provider=provider_name,
                model=None,
                rendered_prompt="task",
                provider_credentials=resolve_realm_credentials(provider_name, host_settings),
            )
        )
    )
    for name, value in runner_environment.items():
        monkeypatch.setenv(name, value)
    environment_before = dict(os.environ)
    provider = _InnerProviderCache._construct_provider(
        provider_name, None, request.inner_provider_settings, None
    )
    try:
        match provider:
            case CopilotProvider():
                config = provider._resolve_sdk_provider_config()
                assert config == {
                    "type": "openai",
                    "base_url": "https://host.example.test",
                    credential: "host-credential",
                }
                assert provider._resolve_runtime_connection() is None
            case ClaudeProvider():
                assert provider._client is not None
                assert str(provider._client.base_url).rstrip("/") == "https://api.anthropic.com"
                if credential == "api_key":
                    assert provider._client.api_key == "host-credential"
                    assert not provider._client.auth_token
                else:
                    assert provider._client.auth_token == "host-credential"
                    assert not provider._client.api_key
            case OpenAIProvider():
                assert provider._client is not None
                assert str(provider._client.base_url).rstrip("/") == "https://api.openai.com/v1"
                assert provider._client.api_key == "host-credential"
    finally:
        await provider.close()
    assert dict(os.environ) == environment_before


@pytest.mark.parametrize("surface", ["execute", "dialog"])
@pytest.mark.parametrize(
    ("provider_name", "credential"),
    [("claude", "api_key"), ("claude", "bearer_token"), ("openai", "api_key")],
)
async def test_lazy_sdk_clients_preserve_host_effective_authentication(
    provider_name: str,
    credential: str,
    surface: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: execute and dialog must propagate isolation through real model resolvers.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://runner-anthropic.example.test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "runner-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "runner-bearer")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://runner-openai.example.test")
    monkeypatch.setenv("OPENAI_API_KEY", "runner-key")
    provider = _InnerProviderCache._construct_provider(
        provider_name, None, {credential: SecretStr("host-credential")}, None
    )
    assert isinstance(provider, ClaudeProvider | OpenAIProvider)
    clients: list[AsyncAnthropic | AsyncOpenAI] = []

    def capture_client(agent: Agent[Any, Any]) -> None:
        assert isinstance(agent.model, AnthropicModel | OpenAIChatModel)
        client = agent.model.client
        assert isinstance(client, AsyncAnthropic | AsyncOpenAI)
        clients.append(client)

    async def pipeline(*, build_agent_fn: Any, **kwargs: Any) -> AgentOutput:
        capture_client(build_agent_fn([], max_parse_recovery_attempts=0))
        return AgentOutput(content={}, raw_response="")

    async def dialog(agent: Agent[Any, Any], **kwargs: Any) -> SimpleNamespace:
        capture_client(agent)
        return SimpleNamespace(output="done")

    monkeypatch.setattr("conductor.providers._pydantic_ai.runner.run_agent_pipeline", pipeline)
    monkeypatch.setattr(Agent, "run", dialog)
    # Metadata probes are independent of authentication and must not reach a live API.
    monkeypatch.setattr(provider, "get_max_prompt_tokens", AsyncMock(return_value=None))
    monkeypatch.setattr(provider, "get_max_output_tokens", AsyncMock(return_value=None))
    environment_before = dict(os.environ)
    try:
        if surface == "execute":
            await provider.execute(
                AgentDef(
                    name="isolated",
                    prompt="task",
                    timeout_seconds=None,
                    max_session_seconds=None,
                    max_agent_iterations=None,
                ),
                {},
                "task",
            )
        else:
            assert await provider.execute_dialog_turn("system", "task") == "done"
        assert provider._base_url is None  # Canonical defaults must not look like custom routing.
        assert len(clients) == 1
        client = clients[0]
        if isinstance(client, AsyncAnthropic):
            assert str(client.base_url).rstrip("/") == "https://api.anthropic.com"
            assert client.api_key == ("host-credential" if credential == "api_key" else "")
            assert client.auth_token == ("host-credential" if credential == "bearer_token" else "")
        else:
            assert str(client.base_url).rstrip("/") == "https://api.openai.com/v1"
            assert client.api_key == "host-credential"
        assert dict(os.environ) == environment_before
    finally:
        for client in clients:
            await client.close()
        await provider.close()


@pytest.mark.parametrize("settings", [None, {}])
async def test_runner_distinguishes_absent_and_empty_settings(
    settings: dict[str, Any] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: None keeps legacy runtime fallback; even an empty host dict is authoritative.
    monkeypatch.setenv("COPILOT_PROVIDER_RUNTIME_URL", "http://runner-runtime.example.test")
    monkeypatch.setenv("COPILOT_PROVIDER_RUNTIME_TOKEN", "runner-token")
    provider = _InnerProviderCache._construct_provider("copilot", None, settings, None)
    assert isinstance(provider, CopilotProvider)
    try:
        expected = (
            ("http://runner-runtime.example.test", "runner-token") if settings is None else None
        )
        assert provider._resolve_runtime_connection() == expected
    finally:
        await provider.close()


async def test_explicit_runtime_settings_survive_environment_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: isolation must preserve an explicit runtime without filling its absent token.
    monkeypatch.setenv("COPILOT_PROVIDER_RUNTIME_URL", "http://runner-runtime.example.test")
    monkeypatch.setenv("COPILOT_PROVIDER_RUNTIME_TOKEN", "runner-token")
    provider = _InnerProviderCache._construct_provider(
        "copilot", None, {"runtime_url": "http://host-runtime.example.test"}, None
    )
    assert isinstance(provider, CopilotProvider)
    try:
        assert provider._resolve_runtime_connection() == ("http://host-runtime.example.test", None)
    finally:
        await provider.close()


@pytest.mark.parametrize("backend", ["anthropic", "openai"])
def test_isolated_model_resolvers_reject_missing_credentials(
    backend: Literal["anthropic", "openai"], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: absent host credentials cannot be rescued by the runner's ambient keys.
    from conductor.providers._pydantic_ai.agent_builder import build_agent

    monkeypatch.setenv("ANTHROPIC_API_KEY", "runner-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "runner-bearer")
    monkeypatch.setenv("OPENAI_API_KEY", "runner-key")
    with pytest.raises(ValidationError, match="no api_key"):
        build_agent(
            AgentDef(
                name="isolated",
                timeout_seconds=None,
                max_session_seconds=None,
                max_agent_iterations=None,
            ),
            system_prompt="",
            rendered_prompt="task",
            backend=backend,
            resolve_environment=False,
        )
