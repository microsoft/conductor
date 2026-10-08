"""ACA execution profile schema and digest compatibility."""

import pytest
from pydantic import ValidationError

from conductor.config.environment import (
    AcaProfileOptions,
    DockerProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    _document_digest,
)


def test_aca_profile_requires_its_options_and_forbids_others() -> None:
    # Requirement: a profile's ACA options are present exactly for the ACA backend.
    with pytest.raises(ValidationError, match="requires an 'aca' configuration block"):
        ProfileDefinition(backend="aca")
    with pytest.raises(ValidationError, match="cannot specify an 'aca'"):
        ProfileDefinition(backend="local", aca=AcaProfileOptions(pool_endpoint="https://pool.test"))
    with pytest.raises(ValidationError, match="cannot specify a 'docker'"):
        ProfileDefinition(
            backend="aca",
            aca=AcaProfileOptions(pool_endpoint="https://pool.test"),
            docker=DockerProfileOptions(image="alpine"),
        )


@pytest.mark.parametrize("endpoint", ["", "http://pool.test", "https://", "https://p.test?q=1"])
def test_aca_profile_rejects_invalid_pool_endpoint(endpoint: str) -> None:
    # Requirement: the management endpoint cannot leak bearer credentials over plain HTTP.
    with pytest.raises(ValidationError, match="pool_endpoint"):
        AcaProfileOptions(pool_endpoint=endpoint)


def test_aca_profile_rejects_inner_provider_and_unknown_options() -> None:
    # Requirement: model selection belongs to the agent, not the placement profile.
    with pytest.raises(ValidationError):
        AcaProfileOptions.model_validate(
            {"pool_endpoint": "https://pool.test", "inner_provider": "copilot"}
        )


def test_aca_profile_digest_and_legacy_environment_digest() -> None:
    # Requirement: adding the optional ACA block does not rehash existing documents.
    existing = EnvironmentDocument(
        default="container",
        profiles={
            "container": ProfileDefinition(
                backend="docker", docker=DockerProfileOptions(image="python:3.12")
            )
        },
    )
    aca = EnvironmentDocument(
        default="remote",
        profiles={
            "remote": ProfileDefinition(
                backend="aca", aca=AcaProfileOptions(pool_endpoint="https://pool.test")
            )
        },
    )
    assert (
        _document_digest(existing)
        == "sha256:9ae4adaaf1f1524498bcb69b2ba4ffa8d82d78ca95569fcbaa6eb846e9ca4639"
    )
    assert "aca" not in existing.model_dump(mode="json")["profiles"]["container"]
    assert _document_digest(aca) != _document_digest(existing)
