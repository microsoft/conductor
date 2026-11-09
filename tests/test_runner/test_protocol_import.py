"""Canonical wire protocol after removal of its deprecated provider shim."""

from pydantic import SecretStr

from conductor.providers.aca import AcaGatewayErrorData
from conductor.runner.protocol import RunnerAgentRequest


def test_canonical_request_redacts_credentials() -> None:
    # Requirement: the canonical wire model still wraps plaintext credentials.
    request = RunnerAgentRequest.model_validate(
        {
            "agent": {"name": "reviewer"},
            "rendered_prompt": "hello",
            "inner_provider_settings": {"github_token": "ghp_secret"},
        }
    )
    assert request.inner_provider_settings is not None
    assert isinstance(request.inner_provider_settings["github_token"], SecretStr)


def test_aca_error_extension_keeps_gateway_identifiers() -> None:
    # Requirement: gateway code and traceId still parse outside the neutral protocol.
    error = AcaGatewayErrorData.model_validate({"message": "m", "code": "c", "traceId": "t"})
    assert (error.code, error.trace_id) == ("c", "t")
