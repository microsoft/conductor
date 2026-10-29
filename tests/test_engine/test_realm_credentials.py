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
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "COPILOT_PROVIDER_RUNTIME_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_copilot_byok_takes_precedence_over_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: opted-in BYOK forwarding contains only allowlisted routing fields.
    monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", " https://api.example.test ")
    monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "api-secret")
    monkeypatch.setenv("COPILOT_PROVIDER_BEARER_TOKEN", "bearer-secret")
    monkeypatch.setenv("GH_TOKEN", "ignored")

    settings = resolve_realm_credentials(
        "copilot", ProviderSettings(name="copilot", api_key=SecretStr("configured-key"))
    )

    assert settings["base_url"] == "https://api.example.test"
    assert isinstance(settings["api_key"], SecretStr)
    assert settings["api_key"].get_secret_value() == "configured-key"
    assert isinstance(settings["bearer_token"], SecretStr)
    assert settings["bearer_token"].get_secret_value() == "bearer-secret"
    assert "github_token" not in settings


@pytest.mark.parametrize("settings", [None, ProviderSettings(name="copilot")])
def test_copilot_ambient_endpoint_without_routing_opt_in_uses_github_identity(
    monkeypatch: pytest.MonkeyPatch, settings: ProviderSettings | None
) -> None:
    # Requirement: an ambient BYOK endpoint cannot replace a bare Copilot provider identity.
    monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "https://ambient.example.test")
    monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "ambient-key")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")

    resolved = resolve_realm_credentials("copilot", settings)

    assert set(resolved) == {"github_token"}
    assert isinstance(resolved["github_token"], SecretStr)
    assert resolved["github_token"].get_secret_value() == "github-token"


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


def test_copilot_yaml_routing_overrides_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: authored endpoint and key win over ambient routing and GitHub auth.
    monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "https://ambient.example.test")
    monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "ambient-key")
    monkeypatch.delenv("COPILOT_PROVIDER_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "github-token")
    settings = ProviderSettings(
        name="copilot", base_url="https://yaml.example.test", api_key=SecretStr("yaml-key")
    )
    resolved = resolve_realm_credentials("copilot", settings)
    assert resolved["base_url"] == "https://yaml.example.test"
    assert isinstance(resolved["api_key"], SecretStr)
    assert resolved["api_key"].get_secret_value() == "yaml-key"
    assert "github_token" not in resolved


def test_copilot_yaml_missing_values_use_dedicated_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: an authored routing opt-in uses only its dedicated credential fallback.
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unrelated.example.test")
    monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "dedicated-key")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-key")
    resolved = resolve_realm_credentials(
        "copilot", ProviderSettings(name="copilot", base_url="https://yaml.example.test")
    )
    assert resolved["base_url"] == "https://yaml.example.test"
    assert isinstance(resolved["api_key"], SecretStr)
    assert resolved["api_key"].get_secret_value() == "dedicated-key"


def test_copilot_yaml_opt_in_uses_openai_base_url_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: only opted-in custom routing can consult OPENAI_BASE_URL.
    monkeypatch.setenv("OPENAI_BASE_URL", "https://fallback.example.test")
    resolved = resolve_realm_credentials(
        "copilot", ProviderSettings(name="copilot", api_key=SecretStr("paired-key"))
    )
    assert resolved["base_url"] == "https://fallback.example.test"
    assert isinstance(resolved["api_key"], SecretStr)
    assert resolved["api_key"].get_secret_value() == "paired-key"


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "azure"),
        ("wire_api", "responses"),
        ("headers", {"x-route": "one"}),
        ("runtime_url", "localhost:4321"),
    ],
)
def test_copilot_unforwardable_mode_fails_closed(field: str, value: object) -> None:
    # Requirement: remote execution refuses configuration it cannot reproduce.
    with pytest.raises(ProviderError, match="remote"):
        resolve_realm_credentials(
            "copilot",
            ProviderSettings.model_validate(
                {"name": "copilot", "base_url": "https://route.example.test", field: value}
            ),
        )


def test_claude_yaml_auth_token_and_endpoint_override_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: YAML bearer auth excludes both ambient Anthropic credential sources.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ambient.example.test")
    resolved = resolve_realm_credentials(
        "claude",
        ProviderSettings(
            name="claude", base_url="https://yaml.example.test", auth_token=SecretStr("yaml-token")
        ),
    )
    assert resolved["base_url"] == "https://yaml.example.test"
    assert isinstance(resolved["bearer_token"], SecretStr)
    assert resolved["bearer_token"].get_secret_value() == "yaml-token"
    assert "api_key" not in resolved


def test_claude_custom_endpoint_uses_anthropic_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: Claude retains its documented environment fallback when YAML sets only URL.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    resolved = resolve_realm_credentials(
        "claude", ProviderSettings(name="claude", base_url="https://custom.example.test")
    )
    assert resolved["base_url"] == "https://custom.example.test"
    assert isinstance(resolved["api_key"], SecretStr)
    assert resolved["api_key"].get_secret_value() == "ambient-key"


@pytest.mark.parametrize(
    "provider,key", [("openai", "OPENAI_API_KEY"), ("claude", "ANTHROPIC_API_KEY")]
)
def test_missing_provider_key_is_named(provider: str, key: str) -> None:
    # Requirement: missing model credentials fail explicitly per provider.
    with pytest.raises(ProviderError, match=key):
        resolve_realm_credentials(provider)
