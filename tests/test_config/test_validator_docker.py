"""Validation requirements for Docker execution profiles."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from conductor.config.environment import (
    DockerProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
)
from conductor.config.schema import (
    AgentDef,
    RouteDef,
    ScriptStepDef,
    StepExecutionConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError


def _environment(
    profiles: dict[str, ProfileDefinition],
    *,
    default: str | None = None,
    name: str = "test",
) -> ResolvedEnvironment:
    return ResolvedEnvironment(
        document=EnvironmentDocument(default=default, profiles=profiles),
        name=name,
        source="path",
        path=None,
        digest="sha256:test",
    )


def _docker(image: str = "busybox:latest", **kwargs: Any) -> ProfileDefinition:
    inherit = kwargs.pop("inherit_control_environment", None)
    return ProfileDefinition(
        backend="docker",
        inherit_control_environment=inherit,
        docker=DockerProfileOptions(image=image, **kwargs),
    )


def _script(name: str, profile: str) -> ScriptStepDef:
    return ScriptStepDef(
        name=name,
        command="true",
        timeout=None,
        execution=StepExecutionConfig(profile=profile),
        routes=[RouteDef(to="$end")],
    )


def _config(*steps: AgentDef | ScriptStepDef) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(name="docker-validation", entry_point=steps[0].name),
        agents=list(steps),
    )


def _validate_explicit(config: WorkflowConfig, environment: ResolvedEnvironment) -> list[str]:
    context = {
        "refs_found": False,
        "environments": {environment.name: environment},
        "explicit": True,
        "root_workflow_dir": None,
        "warned_no_environments": False,
        "warned_no_secret_environments": False,
    }
    return validate_workflow_config(config, _environment_context=cast(Any, context))


def test_non_script_docker_profile_uses_reserved_step_7_error() -> None:
    # Requirement: Docker remains script-only until agent execution realms arrive.
    config = _config(
        AgentDef(
            name="agent",
            prompt="work",
            timeout_seconds=None,
            max_session_seconds=None,
            max_agent_iterations=None,
            execution=StepExecutionConfig(profile="container"),
            routes=[RouteDef(to="$end")],
        )
    )

    with pytest.raises(ConfigurationError, match="script steps only.*step 7"):
        _validate_explicit(config, _environment({"container": _docker()}))


def test_bare_validate_rejects_non_script_docker_profile_from_discovery(tmp_path: Path) -> None:
    # Requirement: ambient validation applies the script-only Docker rule to every discovery.
    config = _config(
        AgentDef(
            name="agent",
            prompt="work",
            timeout_seconds=None,
            max_session_seconds=None,
            max_agent_iterations=None,
            execution=StepExecutionConfig(profile="container"),
            routes=[RouteDef(to="$end")],
        )
    )
    with (
        patch(
            "conductor.config.environment.discover_all_environments",
            return_value={"dev": _environment({"container": _docker()}, name="dev")},
        ),
        pytest.raises(ConfigurationError, match="environment 'dev'.*script steps only"),
    ):
        validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")


def test_mixed_local_and_docker_scripts_warn_about_snapshot_semantics() -> None:
    # Requirement: mixed scripts disclose the pre-run bundle snapshot boundary.
    warnings = _validate_explicit(
        _config(_script("prepare", "local"), _script("consume", "container")),
        _environment(
            {
                "local": ProfileDefinition(backend="local"),
                "container": _docker(),
            }
        ),
    )

    warning = next(item for item in warnings if "mixes local and docker" in item)
    assert "prepare (local)" in warning
    assert "consume (docker)" in warning
    assert "bundle snapshot collected before the run" in warning
    assert "not host filesystem mutations" in warning


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (
            _docker(inherit_control_environment=True),
            "container metadata visible to Docker daemon administrators",
        ),
        (_docker(read_only=True), "set tmpfs: true"),
        (_docker(network="host"), "Docker Desktop or remote Docker daemons"),
    ],
)
def test_docker_profile_warning_classes(profile: ProfileDefinition, expected: str) -> None:
    # Requirement: risky authored Docker settings receive actionable warnings.
    warnings = _validate_explicit(
        _config(_script("run", "container")),
        _environment({"container": profile}),
    )
    assert any(expected in warning for warning in warnings)


def test_different_explicit_users_warn_about_staging_ownership() -> None:
    # Requirement: used Docker profiles with different explicit users warn once about staging.
    warnings = _validate_explicit(
        _config(_script("one", "first"), _script("two", "second")),
        _environment(
            {
                "first": _docker(user="1000:1000"),
                "second": _docker(user="2000:2000"),
            }
        ),
    )
    ownership_warnings = [warning for warning in warnings if "workspace volume" in warning]
    assert len(ownership_warnings) == 1
    warning = ownership_warnings[0]
    assert "environment 'test'" in warning
    assert "first (1000:1000)" in warning
    assert "second (2000:2000)" in warning
    assert "staged once under the first resolved user" in warning
    assert "align user across docker profiles" in warning
    assert "split them across runs or environments" in warning


def test_equal_explicit_users_do_not_warn_about_staging_ownership() -> None:
    # Requirement: equal explicit users across used Docker profiles are staging-compatible.
    warnings = _validate_explicit(
        _config(_script("one", "first"), _script("two", "second")),
        _environment(
            {
                "first": _docker(user="1000:1000"),
                "second": _docker(user="1000:1000"),
            }
        ),
    )
    assert not any("workspace volume" in warning for warning in warnings)


def test_one_explicit_and_one_image_user_do_not_warn_about_staging_ownership() -> None:
    # Requirement: an image-default user does not create a statically proven ownership conflict.
    warnings = _validate_explicit(
        _config(_script("one", "first"), _script("two", "second")),
        _environment(
            {
                "first": _docker(user="1000:1000"),
                "second": _docker(),
            }
        ),
    )
    assert not any("workspace volume" in warning for warning in warnings)


def test_different_explicit_users_do_not_warn_when_only_one_profile_is_used() -> None:
    # Requirement: unused Docker profiles cannot create a run's staging-ownership conflict.
    warnings = _validate_explicit(
        _config(_script("one", "first")),
        _environment(
            {
                "first": _docker(user="1000:1000"),
                "unused": _docker(user="2000:2000"),
            }
        ),
    )
    assert not any("workspace volume" in warning for warning in warnings)


def test_bare_validate_checks_any_discovered_environment(tmp_path: Path) -> None:
    # Requirement: ambient validation sees Docker resolution in every discovered document.
    config = _config(_script("host", "local"), _script("box", "container"))
    discovered = {
        "dev": _environment(
            {
                "local": ProfileDefinition(backend="local"),
                "container": _docker(),
            },
            name="dev",
        ),
        "ci": _environment(
            {
                "local": ProfileDefinition(backend="local"),
                "container": ProfileDefinition(backend="local"),
            },
            name="ci",
        ),
    }
    with patch(
        "conductor.config.environment.discover_all_environments",
        return_value=discovered,
    ):
        warnings = validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")

    assert any("environment 'dev' mixes local and docker" in warning for warning in warnings)
    assert not any("environment 'ci' mixes local and docker" in warning for warning in warnings)


def test_docker_free_workflow_has_zero_noise_and_no_discovery(tmp_path: Path) -> None:
    # Requirement: profile-free local workflows retain the lazy zero-noise path.
    config = _config(
        ScriptStepDef(name="run", command="true", timeout=None, routes=[RouteDef(to="$end")])
    )
    with patch("conductor.config.environment.discover_all_environments") as discover:
        warnings = validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")
    discover.assert_not_called()
    assert warnings == []
