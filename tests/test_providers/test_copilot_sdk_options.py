"""Copilot SDK 2.x boundary: session-option translation, runtime URLs, protocol errors."""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from copilot import CopilotClient
from copilot.client import (
    CapiSessionOptions,
    SessionAgentOptions,
    SessionEventOptions,
    SessionGitHubOptions,
    SessionInferenceOptions,
    SessionMcpOptions,
    SessionPermissionOptions,
    SessionSkillOptions,
    SessionToolOptions,
)
from copilot.session import NamedProviderConfig, ProviderModelConfig

from conductor.exceptions import ProviderError
from conductor.providers.copilot import (
    CopilotProvider,
    _parse_runtime_url,
    _sdk_session_options,
)


def _handler(request: Any, invocation: Any) -> None:
    return None


def _every_flat_option() -> dict[str, Any]:
    return {
        "model": "gpt-5",
        "working_directory": "/work",
        "system_message": {"mode": "replace", "content": "be brief"},
        "on_permission_request": _handler,
        "streaming": True,
        "available_tools": ["view"],
        "large_output": {"enabled": False},
        "mcp_servers": {"git": {"type": "stdio", "command": "git-mcp", "tools": ["*"]}},
        "skill_directories": ["/skills"],
        "custom_agents": [{"name": "plugin:reviewer", "prompt": "review"}],
        "reasoning_effort": "high",
        "context_tier": "long_context",
        "capi": {"auto_tier": "efficiency"},
        "github_token": "gho_example",
    }


_SDK_GROUPS = {
    "permissions": SessionPermissionOptions,
    "events": SessionEventOptions,
    "tools": SessionToolOptions,
    "mcp": SessionMcpOptions,
    "skills": SessionSkillOptions,
    "agents": SessionAgentOptions,
    "inference": SessionInferenceOptions,
    "github": SessionGitHubOptions,
}


def test_flat_options_map_to_sdk_groups() -> None:
    assert _sdk_session_options(_every_flat_option()) == {
        "model": "gpt-5",
        "working_directory": "/work",
        "system_message": {"mode": "replace", "content": "be brief"},
        "permissions": {"on_request": _handler},
        "events": {"streaming": True},
        "tools": {"available_tools": ["view"], "large_output": {"enabled": False}},
        "mcp": {"servers": {"git": {"type": "stdio", "command": "git-mcp", "tools": ["*"]}}},
        "skills": {"directories": ["/skills"]},
        "agents": {"definitions": [{"name": "plugin:reviewer", "prompt": "review"}]},
        "inference": {"reasoning_effort": "high", "context_tier": "long_context"},
        "github": {"capi": {"auto_tier": "efficiency"}, "token": "gho_example"},
    }


@pytest.mark.parametrize("method", ["create_session", "resume_session"])
def test_the_installed_sdk_accepts_every_translated_option(method: str) -> None:
    """Pins the translation to the SDK's own option schema, which rejects unknown keys."""
    flat = {
        **_every_flat_option(),
        "provider": {"type": "openai", "base_url": "http://x/v1"},
        "byok_model": "gpt-5",
    }
    translated = _sdk_session_options(flat)
    parameters = inspect.signature(getattr(CopilotClient, method)).parameters
    for key, value in translated.items():
        assert key in parameters, f"{method} has no {key!r} option"
        if key in _SDK_GROUPS:
            assert value.keys() <= _SDK_GROUPS[key].__annotations__.keys(), key
    assert translated["github"]["capi"].keys() <= CapiSessionOptions.__annotations__.keys()
    for connection in translated["inference"]["providers"]:
        assert connection.keys() <= NamedProviderConfig.__annotations__.keys()
    for model in translated["inference"]["models"]:
        assert model.keys() <= ProviderModelConfig.__annotations__.keys()


def test_custom_routing_becomes_the_only_default_connection() -> None:
    provider = {"type": "azure", "base_url": "https://aoai/", "azure": {"api_version": "v1"}}
    translated = _sdk_session_options(
        {"model": "my-deployment", "provider": provider, "byok_model": "my-deployment"}
    )
    assert translated == {
        "model": "conductor/my-deployment",
        "inference": {
            "providers": [{**provider, "name": "conductor"}],
            "default_provider": "conductor",
            "models": [{"provider": "conductor", "id": "my-deployment"}],
        },
        "github": {"capi": {"enabled": False}},
    }
    assert "name" not in provider


@pytest.mark.parametrize("model", ["meta-llama/Llama-3.1-8B-Instruct", "auto"])
def test_custom_routing_registers_names_the_sdk_would_reinterpret(model: str) -> None:
    """A slash prefix would be read as a provider name and ``auto`` as Copilot routing."""
    provider = {"type": "openai", "base_url": "http://x/v1"}
    created = _sdk_session_options({"model": model, "provider": provider, "byok_model": model})
    assert created["model"] == f"conductor/{model}"
    assert created["inference"]["models"] == [{"provider": "conductor", "id": model}]

    resumed = _sdk_session_options({"provider": provider, "byok_model": model})
    assert "model" not in resumed
    assert resumed["inference"]["models"] == [{"provider": "conductor", "id": model}]


def test_unknown_session_option_is_refused_not_dropped() -> None:
    with pytest.raises(ValueError, match="streaming_mode"):
        _sdk_session_options({"model": "gpt-5", "streaming_mode": True})


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("3000", ("localhost", 3000)),
        ("localhost:3000", ("localhost", 3000)),
        (":3000", ("localhost", 3000)),
        ("10.0.0.5:8123", ("10.0.0.5", 8123)),
        ("http://runtime.internal:3000", ("runtime.internal", 3000)),
        ("HTTP://runtime.internal:3000", ("runtime.internal", 3000)),
        ("[::1]:3000", ("::1", 3000)),
    ],
)
def test_runtime_url_forms_accepted_by_sdk_1_still_parse(
    url: str, expected: tuple[str, int]
) -> None:
    assert _parse_runtime_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://runtime.internal:3000",
        "runtime.internal",
        "host:0",
        "host:70000",
        "host:3000/path",
        "a:b:c",
        "[not-ipv6]:3000",
        "[::1]",
    ],
)
def test_invalid_runtime_url_is_a_non_retryable_error(url: str) -> None:
    with pytest.raises(ProviderError) as info:
        _parse_runtime_url(url)
    assert not info.value.is_retryable


async def test_protocol_mismatch_at_start_is_not_retried() -> None:
    provider = CopilotProvider()
    client = MagicMock()
    client.start = AsyncMock(
        side_effect=RuntimeError(
            "Client failed to start: SDK protocol version mismatch: SDK supports "
            "versions 4-4, but server reports version 3."
        )
    )
    provider._client = client
    with pytest.raises(ProviderError) as info:
        await provider.validate_connection()
    assert not info.value.is_retryable
    assert "server reports version 3" in str(info.value)
    assert "1.0.113" in (info.value.suggestion or "")


async def test_other_start_failures_keep_their_original_classification() -> None:
    provider = CopilotProvider()
    client = MagicMock()
    client.start = AsyncMock(side_effect=RuntimeError("Copilot CLI not found at copilot"))
    provider._client = client
    with pytest.raises(ProviderError, match="Failed to connect to Copilot SDK"):
        await provider.validate_connection()
