"""Remote model credentials never fall back to the runner's ambient environment."""

from __future__ import annotations

import subprocess

import pytest
from pydantic import SecretStr

from conductor.config.schema import ProviderSettings
from conductor.engine.realm_credentials import resolve_realm_credentials
from conductor.exceptions import ProviderError


@pytest.fixture(autouse=True)
def clear_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: every precedence assertion is independent of host credentials.
    for name in (
        "COPILOT_PROVIDER_BASE_URL",
        "COPILOT_PROVIDER_API_KEY",
        "COPILOT_PROVIDER_BEARER_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_copilot_byok_takes_precedence_over_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: BYOK forwarding contains only allowlisted custom-routing fields.
    monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", " https://api.example.test ")
    monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "api-secret")
    monkeypatch.setenv("COPILOT_PROVIDER_BEARER_TOKEN", "bearer-secret")
    monkeypatch.setenv("GH_TOKEN", "ignored")

    settings = resolve_realm_credentials("copilot")

    assert settings["base_url"] == "https://api.example.test"
    assert isinstance(settings["api_key"], SecretStr)
    assert settings["api_key"].get_secret_value() == "api-secret"
    assert settings["bearer_token"].get_secret_value() == "bearer-secret"
    assert "github_token" not in settings


@pytest.mark.parametrize("name", ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"])
def test_copilot_github_env_chain(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    # Requirement: each accepted GitHub credential reaches the runner as github_token.
    monkeypatch.setenv(name, " github-secret ")
    settings = resolve_realm_credentials("copilot")
    assert settings["github_token"].get_secret_value() == "github-secret"


def test_copilot_gh_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: the CLI fallback is used only when all environment sources miss.
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["gh", "auth", "token"], returncode=0, stdout="cli-secret\n"
        ),
    )
    assert resolve_realm_credentials("copilot")["github_token"].get_secret_value() == "cli-secret"


def test_missing_copilot_credential_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: an unavailable CLI and empty env fail explicitly, before dispatch.
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("gh unavailable")),
    )
    with pytest.raises(ProviderError, match="Copilot credential"):
        resolve_realm_credentials("copilot")


def test_openai_explicit_custom_endpoint_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: only an authored key may accompany a custom OpenAI endpoint.
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key")
    settings = ProviderSettings(
        name="openai", base_url="https://custom.example.test", api_key="paired"
    )
    credential = resolve_realm_credentials("openai", settings)
    assert credential["base_url"] == "https://custom.example.test"
    assert credential["api_key"].get_secret_value() == "paired"


def test_openai_ambient_key_without_custom_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: a standard OpenAI endpoint may use its ambient API key.
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key")
    assert resolve_realm_credentials("openai")["api_key"].get_secret_value() == "ambient-key"


def test_openai_custom_endpoint_rejects_ambient_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: ambient credentials must never leak to a custom endpoint.
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://custom.example.test")
    with pytest.raises(ProviderError, match="explicitly paired API key"):
        resolve_realm_credentials("openai")


def test_claude_ambient_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: Anthropic API key is delivered only as an allowlisted field.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "claude-secret")
    assert resolve_realm_credentials("claude")["api_key"].get_secret_value() == "claude-secret"


@pytest.mark.parametrize(
    "provider,key", [("openai", "OPENAI_API_KEY"), ("claude", "ANTHROPIC_API_KEY")]
)
def test_missing_provider_key_is_named(provider: str, key: str) -> None:
    # Requirement: missing model credentials fail explicitly per provider.
    with pytest.raises(ProviderError, match=key):
        resolve_realm_credentials(provider)
