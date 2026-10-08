"""Resolve host credentials for a remote agent invocation."""

from __future__ import annotations

import os
import subprocess

from pydantic import SecretStr

from conductor.config.schema import ProviderSettings
from conductor.exceptions import ProviderError


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def resolve_realm_credentials(
    provider: str, settings: ProviderSettings | None = None
) -> dict[str, str | SecretStr]:
    """Apply the inner provider's host credential precedence without logging values."""
    if provider == "copilot":
        base_url = _env("COPILOT_PROVIDER_BASE_URL")
        if base_url is not None:
            settings: dict[str, str | SecretStr] = {"base_url": base_url}
            for field, variable in (
                ("api_key", "COPILOT_PROVIDER_API_KEY"),
                ("bearer_token", "COPILOT_PROVIDER_BEARER_TOKEN"),
            ):
                value = _env(variable)
                if value is not None:
                    settings[field] = SecretStr(value)
            return settings
        for variable in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            token = _env(variable)
            if token is not None:
                return {"github_token": SecretStr(token)}
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0 and result.stdout.strip():
            return {"github_token": SecretStr(result.stdout.strip())}
        raise ProviderError(
            "No Copilot credential available for the remote agent realm.",
            suggestion="Set COPILOT_GITHUB_TOKEN, sign in with gh auth login, or configure "
            "COPILOT_PROVIDER_BASE_URL with a BYOK credential.",
            provider_name=provider,
            is_retryable=False,
        )

    if provider == "openai":
        base_url = settings.base_url if settings and settings.name == provider else None
        base_url = base_url or _env("OPENAI_BASE_URL")
        explicit_key = settings.api_key if settings and settings.name == provider else None
        api_key = explicit_key or _env("OPENAI_API_KEY")
        if base_url is not None:
            if explicit_key is None:
                raise ProviderError(
                    "A remote OpenAI custom base_url requires an explicitly paired API key; "
                    "ambient OPENAI_API_KEY cannot be forwarded to that endpoint.",
                    provider_name=provider,
                    is_retryable=False,
                )
            return {"base_url": base_url, "api_key": explicit_key}
        if api_key is not None:
            return {"api_key": api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)}
        raise ProviderError(
            "No OPENAI_API_KEY available for the remote agent realm.",
            provider_name=provider,
            is_retryable=False,
        )

    if provider == "claude":
        api_key = _env("ANTHROPIC_API_KEY")
        if api_key is not None:
            return {"api_key": SecretStr(api_key)}
        raise ProviderError(
            "No ANTHROPIC_API_KEY available for the remote agent realm.",
            provider_name=provider,
            is_retryable=False,
        )

    raise ProviderError(
        f"Provider {provider!r} is unsupported in a remote agent realm.",
        provider_name=provider,
        is_retryable=False,
    )
