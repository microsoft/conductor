"""ACA profile agent-component and sandbox validation requirements."""

from pathlib import Path

import pytest

from conductor.config.environment import (
    AcaProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
)
from conductor.config.schema import WorkflowConfig
from conductor.config.validator import _EnvironmentValidationContext, validate_workflow_config
from conductor.engine.run_manifest import compile_run_manifest
from conductor.exceptions import ConfigurationError


def _environment() -> ResolvedEnvironment:
    return ResolvedEnvironment(
        document=EnvironmentDocument(
            default="remote",
            profiles={
                "remote": ProfileDefinition(
                    backend="aca", aca=AcaProfileOptions(pool_endpoint="https://pool.example.com")
                )
            },
        ),
        name="test",
        source="path",
        path=None,
        digest="sha256:test",
    )


def _config(
    *,
    agent_fields: dict[str, object] | None = None,
    runtime_fields: dict[str, object] | None = None,
) -> WorkflowConfig:
    return WorkflowConfig.model_validate(
        {
            "workflow": {
                "name": "aca-profile-validation",
                "entry_point": "reviewer",
                "runtime": {"provider": "copilot", **(runtime_fields or {})},
            },
            "agents": [
                {
                    "name": "reviewer",
                    "prompt": "review",
                    "execution": {"profile": "remote"},
                    **(agent_fields or {}),
                }
            ],
        }
    )


def _validate(config: WorkflowConfig) -> list[str]:
    context: _EnvironmentValidationContext = {
        "refs_found": True,
        "environments": {"test": _environment()},
        "explicit": True,
        "root_workflow_dir": None,
        "warned_no_environments": False,
        "warned_no_secret_environments": False,
    }
    workflow_path = Path(__file__).resolve().parents[2] / "examples" / "plugins.yaml"
    return validate_workflow_config(
        config, workflow_path=workflow_path, _environment_context=context
    )


def test_aca_skills_rejected_at_validate_time() -> None:
    # Requirement: ACA does not stage skills even when the model provider supports them.
    with pytest.raises(ConfigurationError):
        _validate(_config(agent_fields={"skills": ["conductor"]}))


def test_aca_skills_bearing_plugin_rejected_at_validate_time() -> None:
    # Requirement: ACA cannot expose host plugin roots across its sandbox boundary.
    with pytest.raises(ConfigurationError):
        _validate(_config(runtime_fields={"plugins": ["./demo-plugin"]}))


def test_aca_flip_rejects_per_agent_identifier_scope_at_validate_and_compile() -> None:
    # Requirement: the ACA profile owns session scope, never an agent sandbox override.
    config = _config(agent_fields={"sandbox": {"identifier_scope": "item"}})
    with pytest.raises(ConfigurationError):
        _validate(config)
    with pytest.raises(ConfigurationError):
        compile_run_manifest(config, workflow_path=None, environment=_environment())


@pytest.mark.parametrize("compile_manifest", [False, True])
def test_aca_inherited_skill_discovery_is_rejected(compile_manifest: bool) -> None:
    # Requirement: an ambient skill set is still a skill set that ACA cannot stage.
    config = _config(runtime_fields={"skill_discovery": {"sources": ["project"]}})
    with pytest.raises(ConfigurationError):
        if compile_manifest:
            compile_run_manifest(config, workflow_path=None, environment=_environment())
        else:
            _validate(config)


@pytest.mark.parametrize("compile_manifest", [False, True])
def test_aca_explicit_opt_out_overrides_inherited_components(compile_manifest: bool) -> None:
    # Requirement: explicit empty lists disable inherited skills, plugins, and discovery.
    config = _config(
        agent_fields={"skills": [], "plugins": []},
        runtime_fields={
            "skills": ["conductor"],
            "plugins": ["./demo-plugin"],
            "skill_discovery": {"sources": ["project"]},
        },
    )
    if compile_manifest:
        manifest = compile_run_manifest(config, workflow_path=None, environment=_environment())
        assert manifest.profiles["reviewer"].backend == "aca"
    else:
        _validate(config)
