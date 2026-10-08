"""Capability-driven Docker agent profile checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from conductor.config.environment import (
    DockerProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
)
from conductor.config.loader import load_config
from conductor.config.schema import (
    AgentDef,
    RouteDef,
    StepExecutionConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.engine.bundle_prep import prepare_run_bundle
from conductor.engine.run_manifest import compile_run_manifest, script_step_backends
from conductor.exceptions import ConfigurationError


def _config() -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(name="realm", entry_point="agent"),
        agents=[
            AgentDef(
                name="agent",
                prompt="work",
                timeout_seconds=None,
                max_session_seconds=None,
                max_agent_iterations=None,
                execution=StepExecutionConfig(profile="container"),
                routes=[RouteDef(to="$end")],
            )
        ],
    )


def _environment(runner_image: str | None) -> ResolvedEnvironment:
    return ResolvedEnvironment(
        document=EnvironmentDocument(
            default="container",
            profiles={
                "container": ProfileDefinition(
                    backend="docker",
                    docker=DockerProfileOptions(image="script:1", runner_image=runner_image),
                )
            },
        ),
        name="dev",
        source="path",
        path=None,
        digest="sha256:fixture",
    )


@pytest.mark.parametrize("runner_image", [None, "runner:2"])
def test_realm_flip_agent_profile_compiles_and_validates_together(runner_image: str | None) -> None:
    # Requirement: compile and validate agree on Docker agent-capability completeness.
    config = _config()
    environment = _environment(runner_image)
    context = {
        "refs_found": True,
        "environments": {"dev": environment},
        "explicit": True,
        "root_workflow_dir": None,
        "warned_no_environments": False,
        "warned_no_secret_environments": False,
    }
    if runner_image is None:
        with pytest.raises(ConfigurationError, match="docker.runner_image"):
            compile_run_manifest(config, workflow_path=None, environment=environment)
        with pytest.raises(ConfigurationError, match="docker.runner_image"):
            validate_workflow_config(config, _environment_context=cast(Any, context))
        return
    manifest = compile_run_manifest(config, workflow_path=None, environment=environment)
    assert manifest.profiles["agent"].execution is not None
    assert manifest.profiles["agent"].execution.image == runner_image
    assert script_step_backends(manifest) == frozenset()
    assert validate_workflow_config(config, _environment_context=cast(Any, context)) == []


@pytest.mark.asyncio
async def test_agent_only_docker_run_publishes_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: an agent-only Docker workflow stages its closure before any runner starts.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        "workflow:\n  name: demo\n  entry_point: agent\n"
        "agents:\n  - name: agent\n    prompt: work\n"
        "    execution:\n      profile: container\n"
        "    routes:\n      - to: $end\n",
        encoding="utf-8",
    )
    environment = _environment("runner:2")
    manifest = compile_run_manifest(
        load_config(workflow), workflow_path=workflow, environment=environment
    )
    bundle = await prepare_run_bundle(manifest, workflow, environment)
    assert bundle is not None
    assert (Path(bundle.store_path) / "tree/main/workflow.yaml").is_file()
    assert bundle.source_roots == ((str(tmp_path), "main"),)
