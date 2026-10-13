"""Shared host-side fake inputs for comparing ACA provider and backend paths."""

from unittest.mock import AsyncMock, patch

import httpx

from conductor.config.schema import AgentDef, ProviderSettings
from conductor.execution.types import AgentSpec
from conductor.providers.aca import AcaRuntimeProvider


def provider(gateway: httpx.MockTransport | httpx.ASGITransport) -> AcaRuntimeProvider:
    with patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True):
        result = AcaRuntimeProvider(
            provider_settings=ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
        )
    result._http_client = httpx.AsyncClient(transport=gateway)
    result._get_access_token = AsyncMock(return_value="aad-token")
    return result


def spec() -> AgentSpec:
    return AgentSpec(
        name="reviewer",
        execution_id="call-1",
        model_provider="copilot",
        model=None,
        rendered_prompt="review",
        provider_credentials={"github_token": "host-token"},
    )


def agent() -> AgentDef:
    return AgentDef.model_validate({"name": "reviewer", "prompt": "review"})
