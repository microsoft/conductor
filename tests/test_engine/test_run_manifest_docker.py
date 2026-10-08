"""Docker execution requirements for the resolved run manifest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conductor.config.environment import (
    DockerProfileOptions,
    DockerResources,
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
    builtin_local_environment,
)
from conductor.config.schema import (
    AgentDef,
    ForEachDef,
    MCPStepDef,
    OutputField,
    RestartConfig,
    RouteDef,
    ScriptStepDef,
    StepExecutionConfig,
    WorkflowConfig,
    WorkflowDef,
    WorkflowDefaults,
    WorkflowStepDef,
    WorkspaceConfig,
)
from conductor.digest import canonical_json_digest
from conductor.engine.run_manifest import (
    BACKEND_CAPABILITY_PROVIDERS,
    ManifestExecutionSpec,
    ResolvedRunManifest,
    compile_run_manifest,
    manifest_semantic_digest,
    script_step_backends,
)
from conductor.exceptions import ConfigurationError
from conductor.execution.types import RunnerCapabilities


class _BatchBackend:
    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            batch=True,
            sessions=False,
            shared_workspace=False,
            snapshots=False,
        )

    async def prepare_run(self, run: Any) -> Any:
        raise NotImplementedError

    async def run_command(
        self, spec: Any, lease: Any, *, diagnostics: Any = None, on_dispatch: Any = None
    ) -> Any:
        raise NotImplementedError

    async def finalize_run(self, lease: Any, outcome: Any, *, retain: bool = False) -> None:
        raise NotImplementedError


@pytest.fixture
def docker_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(BACKEND_CAPABILITY_PROVIDERS, "docker", _BatchBackend())


def _environment(*, docker: DockerProfileOptions | None = None) -> ResolvedEnvironment:
    profiles = {"local": ProfileDefinition(backend="local")}
    if docker is not None:
        profiles["container"] = ProfileDefinition(backend="docker", docker=docker)
    document = EnvironmentDocument(default="local", profiles=profiles)
    return ResolvedEnvironment(
        document=document,
        name="test",
        source="path",
        path=None,
        digest="sha256:test",
    )


def _script(name: str, profile: str) -> ScriptStepDef:
    return ScriptStepDef(
        name=name,
        command="echo",
        timeout=None,
        execution=StepExecutionConfig(profile=profile),
        routes=[RouteDef(to="$end")],
    )


def _config(*steps: Any, entry_point: str | None = None) -> WorkflowConfig:
    first = entry_point or steps[0].name
    return WorkflowConfig(
        workflow=WorkflowDef(name="docker-manifest", entry_point=first),
        agents=list(steps),
        output={"result": "ok"},
    )


def test_docker_script_records_normalized_execution_with_explicit_nulls(
    docker_backend: None,
) -> None:
    # Requirement: a present execution has a stable complete key set, including nulls.
    docker = DockerProfileOptions(
        image="python:3.12-slim",
        platform="linux/amd64",
        network="none",
        user="1000:1000",
        init=True,
        read_only=True,
        cap_drop_all=True,
        no_new_privileges=True,
        tmpfs="512m",
        resources=DockerResources(cpu=2.0, memory="4g", pids=512),
    )
    manifest = compile_run_manifest(
        _config(_script("run", "container")),
        workflow_path=None,
        environment=_environment(docker=docker),
    )

    assert manifest.profiles["run"].execution == ManifestExecutionSpec(
        image="python:3.12-slim",
        platform="linux/amd64",
        network="none",
        user="1000:1000",
        init=True,
        read_only=True,
        cap_drop_all=True,
        no_new_privileges=True,
        tmpfs="512m",
        cpu=2.0,
        memory="4g",
        pids=512,
    )

    minimal = compile_run_manifest(
        _config(_script("run", "container")),
        workflow_path=None,
        environment=_environment(docker=DockerProfileOptions(image="busybox:latest")),
    ).model_dump(mode="json")["profiles"]["run"]["execution"]
    assert minimal == {
        "image": "busybox:latest",
        "platform": None,
        "network": None,
        "user": None,
        "init": False,
        "read_only": False,
        "cap_drop_all": False,
        "no_new_privileges": False,
        "tmpfs": False,
        "cpu": None,
        "memory": None,
        "pids": None,
    }


def test_docker_compile_is_byte_identical(docker_backend: None) -> None:
    # Requirement: container execution data is run-invariant.
    config = _config(_script("run", "container"))
    environment = _environment(docker=DockerProfileOptions(image="busybox:latest"))
    first = compile_run_manifest(config, workflow_path=None, environment=environment)
    second = compile_run_manifest(config, workflow_path=None, environment=environment)
    assert json.dumps(first.model_dump(mode="json"), sort_keys=True) == json.dumps(
        second.model_dump(mode="json"), sort_keys=True
    )


def test_local_dump_matches_legacy_shape() -> None:
    # Requirement: manifests without Docker steps do not gain an execution key.
    manifest = compile_run_manifest(
        _config(_script("run", "default")),
        workflow_path=None,
        environment=builtin_local_environment(),
    )
    assert manifest.profiles["run"].model_dump(mode="json") == {
        "profile": "default",
        "backend": "local",
        "inherit_control_environment": True,
    }
    assert manifest.model_dump(mode="json")["profiles"]["run"] == {
        "profile": "default",
        "backend": "local",
        "inherit_control_environment": True,
    }


def test_old_manifest_payload_round_trips_without_execution() -> None:
    # Requirement: additive v1 readers accept payloads written before execution existed.
    payload = {
        "version": 1,
        "workflow": {"name": "old", "digest": None},
        "environment": {"name": "local/default", "source": "builtin", "digest": "sha256:x"},
        "profiles": {
            "run": {
                "profile": "default",
                "backend": "local",
                "inherit_control_environment": True,
            }
        },
        "secrets": [],
        "conductor_version": "old",
        "audit": {"hermetic": False, "classification": "non-hermetic-compatibility"},
    }
    manifest = ResolvedRunManifest.model_validate(payload)
    assert manifest.model_dump(mode="json") == payload


def test_script_step_backends_reports_local_docker_and_mixed(docker_backend: None) -> None:
    # Requirement: backend sets support local, Docker, and mixed-run detection.
    local = compile_run_manifest(
        _config(_script("local", "default")),
        workflow_path=None,
        environment=builtin_local_environment(),
    )
    docker = compile_run_manifest(
        _config(_script("docker", "container")),
        workflow_path=None,
        environment=_environment(docker=DockerProfileOptions(image="busybox")),
    )
    mixed = compile_run_manifest(
        _config(
            _script("local", "local"),
            _script("docker", "container"),
            AgentDef(
                name="agent",
                model="gpt-4",
                prompt="test",
                timeout_seconds=None,
                max_session_seconds=None,
                max_agent_iterations=None,
                execution=StepExecutionConfig(profile="local"),
            ),
            entry_point="local",
        ),
        workflow_path=None,
        environment=_environment(docker=DockerProfileOptions(image="busybox")),
    )
    assert script_step_backends(local) == frozenset({"local"})
    assert script_step_backends(docker) == frozenset({"docker"})
    assert script_step_backends(mixed) == frozenset({"local", "docker"})


def test_script_step_backends_ignores_non_script_profiles(docker_backend: None) -> None:
    # Requirement: agent/workflow/MCP steps are compile-time pinned to the local
    # backend, so their profiles must not pollute script-backend detection —
    # an agent(local) + script(docker) workflow is NOT mixed.
    manifest = compile_run_manifest(
        _config(
            AgentDef(
                name="agent",
                model="gpt-4",
                prompt="test",
                timeout_seconds=None,
                max_session_seconds=None,
                max_agent_iterations=None,
                execution=StepExecutionConfig(profile="local"),
                routes=[RouteDef(to="build")],
            ),
            _script("build", "container"),
            entry_point="agent",
        ),
        workflow_path=None,
        environment=_environment(docker=DockerProfileOptions(image="busybox")),
    )
    assert script_step_backends(manifest) == frozenset({"docker"})


def test_script_steps_identities_are_never_serialized(docker_backend: None) -> None:
    # Requirement: the script-identity list is an in-memory discrimination aid
    # and never enters the audit dump — serialized key sets stay identical to
    # pre-recording v1 producers (three keys per profile, plus execution on
    # Docker, and no top-level script_steps key).
    manifest = compile_run_manifest(
        _config(
            AgentDef(
                name="agent",
                model="gpt-4",
                prompt="test",
                timeout_seconds=None,
                max_session_seconds=None,
                max_agent_iterations=None,
                execution=StepExecutionConfig(profile="local"),
                routes=[RouteDef(to="build")],
            ),
            _script("build", "container"),
            entry_point="agent",
        ),
        workflow_path=None,
        environment=_environment(docker=DockerProfileOptions(image="busybox")),
    )
    assert manifest.script_steps == ("build",)
    dump = manifest.model_dump(mode="json")
    assert "script_steps" not in dump
    assert set(dump["profiles"]["agent"]) == {
        "profile",
        "backend",
        "inherit_control_environment",
    }
    assert set(dump["profiles"]["build"]) == {
        "profile",
        "backend",
        "inherit_control_environment",
        "execution",
    }


def test_legacy_payload_without_script_steps_uses_all_backends_fallback() -> None:
    # Requirement: a v1 payload written before script-identity recording (or any
    # manifest revalidated from its dump, since script_steps never serializes)
    # cannot distinguish script steps, so script_step_backends conservatively
    # answers as the pre-recording implementation did — backends of ALL
    # profiles — rather than silently dropping the mixed-backend diagnostic.
    payload: dict[str, Any] = {
        "version": 1,
        "workflow": {"name": "old", "digest": None},
        "environment": {"name": "test", "source": "path", "digest": "sha256:x"},
        "profiles": {
            "agent": {
                "profile": "default",
                "backend": "local",
                "inherit_control_environment": True,
            },
            "build": {
                "profile": "container",
                "backend": "docker",
                "inherit_control_environment": False,
            },
        },
        "secrets": [],
        "conductor_version": "old",
        "audit": {"hermetic": False, "classification": "non-hermetic-compatibility"},
    }
    manifest = ResolvedRunManifest.model_validate(payload)
    assert manifest.script_steps == ()
    assert script_step_backends(manifest) == frozenset({"local", "docker"})


@pytest.mark.parametrize(
    "step",
    [
        AgentDef(
            name="agent",
            model="gpt-4",
            prompt="test",
            output={"value": OutputField(type="string")},
            timeout_seconds=None,
            max_session_seconds=None,
            max_agent_iterations=None,
            execution=StepExecutionConfig(profile="container"),
            routes=[RouteDef(to="$end")],
        ),
        WorkflowStepDef(
            name="workflow",
            workflow="child.yaml",
            max_depth=None,
            execution=StepExecutionConfig(profile="container"),
            routes=[RouteDef(to="$end")],
        ),
        MCPStepDef(
            name="mcp",
            server="tools",
            tool="read",
            timeout=None,
            execution=StepExecutionConfig(profile="container"),
            routes=[RouteDef(to="$end")],
        ),
    ],
    ids=["agent", "workflow", "mcp"],
)
def test_reserved_non_script_docker_profiles_fail(step: Any) -> None:
    # Requirement: Docker remains reserved to scripts until agent realms arrive.
    with pytest.raises(ConfigurationError, match="script steps only.*step 7"):
        compile_run_manifest(
            _config(step),
            workflow_path=None,
            environment=_environment(docker=DockerProfileOptions(image="busybox")),
        )


def test_inline_for_each_complete_miss_uses_qualified_key() -> None:
    # Requirement: complete-miss diagnostics identify an inline step by its manifest key.
    config = WorkflowConfig(
        workflow=WorkflowDef(name="inline-miss", entry_point="batch"),
        agents=[],
        for_each=[
            ForEachDef.model_validate(
                {
                    "name": "batch",
                    "type": "for_each",
                    "source": "workflow.input.items",
                    "as": "item",
                    "agent": AgentDef(
                        name="worker",
                        model="gpt-4",
                        prompt="test",
                        timeout_seconds=None,
                        max_session_seconds=None,
                        max_agent_iterations=None,
                    ),
                }
            )
        ],
        output={"result": "ok"},
    )
    environment = _environment()
    environment = ResolvedEnvironment(
        document=EnvironmentDocument(default=None, profiles=environment.document.profiles),
        name=environment.name,
        source=environment.source,
        path=None,
        digest=environment.digest,
    )
    with pytest.raises(ConfigurationError, match=r"for_each\.batch\.agent"):
        compile_run_manifest(config, workflow_path=None, environment=environment)


def test_docker_script_checks_registered_batch_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: Docker compilation consults the backend capability registry.
    backend = _BatchBackend()
    monkeypatch.setitem(BACKEND_CAPABILITY_PROVIDERS, "docker", backend)
    manifest = compile_run_manifest(
        _config(_script("run", "container")),
        workflow_path=None,
        environment=_environment(docker=DockerProfileOptions(image="busybox")),
    )
    assert manifest.profiles["run"].backend == "docker"


class TestWorkspaceManifest:
    def test_legacy_bytes_and_digest_are_unchanged(self) -> None:
        # Requirement: absent workspace keeps the pre-policy manifest byte-for-byte stable.
        payload = {
            "version": 1,
            "workflow": {"name": "old", "digest": None},
            "environment": {"name": "test", "source": "path", "digest": "sha256:x"},
            "profiles": {},
            "secrets": [],
            "conductor_version": "old",
            "audit": {"hermetic": False, "classification": "non-hermetic-compatibility"},
        }
        manifest = ResolvedRunManifest.model_validate(payload)
        expected = json.dumps(payload, separators=(",", ":")).encode()
        assert (
            json.dumps(manifest.model_dump(mode="json"), separators=(",", ":")).encode() == expected
        )
        assert manifest.model_dump_json().encode() == expected
        assert canonical_json_digest(manifest.model_dump(mode="json")) == canonical_json_digest(
            payload
        )
        assert canonical_json_digest(manifest.model_dump(mode="json")) == (
            "sha256:90cbc54ea127c0cd6a12c66223e5613dc6e53b21c607393eddf28b967ccface7"
        )
        assert "workspace" not in manifest.model_dump(exclude={"audit"})

    def test_workspace_is_recorded_and_affects_semantic_digest(self) -> None:
        # Requirement: a declared policy is audited and changes the semantic identity.
        plain = compile_run_manifest(
            _config(_script("run", "default")),
            workflow_path=None,
            environment=builtin_local_environment(),
        )
        config = _config(_script("run", "default"))
        config.workflow.workspace = WorkspaceConfig(mode="shared", persistence="ephemeral")
        manifest = compile_run_manifest(
            config, workflow_path=None, environment=builtin_local_environment()
        )
        assert manifest.model_dump(mode="json")["workspace"] == {
            "mode": "shared",
            "persistence": "ephemeral",
        }
        assert manifest_semantic_digest(manifest) != manifest_semantic_digest(plain)
        assert manifest_semantic_digest(manifest) == manifest_semantic_digest(manifest)
        assert manifest_semantic_digest(
            manifest.model_copy(update={"conductor_version": "new"})
        ) == (manifest_semantic_digest(manifest))
        assert manifest_semantic_digest(
            manifest.model_copy(
                update={"audit": manifest.audit.model_copy(update={"classification": "changed"})}
            )
        ) == manifest_semantic_digest(manifest)

    @pytest.mark.parametrize(
        ("workspace", "defaults", "step_restart", "error"),
        [
            (WorkspaceConfig(mode="isolated"), None, None, "isolated.*follow-up"),
            (None, RestartConfig(mode="reuse"), None, "reuse.*follow-up"),
            (None, None, RestartConfig(mode="reuse"), "reuse.*follow-up"),
            (WorkspaceConfig(persistence="durable"), None, None, "local backend.*retained"),
        ],
        ids=["isolated", "reuse_default", "reuse_step", "retained_local"],
    )
    def test_reserved_and_unsupported_policies_fail_compilation(
        self,
        workspace: WorkspaceConfig | None,
        defaults: RestartConfig | None,
        step_restart: RestartConfig | None,
        error: str,
    ) -> None:
        # Requirement: compiler refuses unsupported policies before the run starts.
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="policy",
                entry_point="run",
                workspace=workspace,
                defaults=WorkflowDefaults(restart=defaults),
            ),
            agents=[ScriptStepDef(name="run", command="true", timeout=None, restart=step_restart)],
        )
        with pytest.raises(ConfigurationError, match=error):
            compile_run_manifest(
                config, workflow_path=None, environment=builtin_local_environment()
            )

    def test_retention_uses_registered_backend_capability(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: retention selection follows capabilities, not backend name.
        class RetainingBackend(_BatchBackend):
            def capabilities(self) -> RunnerCapabilities:
                return RunnerCapabilities(
                    batch=True,
                    sessions=False,
                    shared_workspace=False,
                    snapshots=False,
                    retained_workspace=True,
                )

        monkeypatch.setitem(BACKEND_CAPABILITY_PROVIDERS, "docker", RetainingBackend())
        config = _config(_script("run", "container"))
        config.workflow.workspace = WorkspaceConfig(persistence="durable")
        manifest = compile_run_manifest(
            config,
            workflow_path=None,
            environment=_environment(docker=DockerProfileOptions(image="busybox")),
        )
        assert manifest.workspace is not None
        assert manifest.workspace.persistence == "durable"

    def test_local_child_workspace_is_rejected(self, tmp_path: Path) -> None:
        # Requirement: a statically resolved child inherits the root policy.
        child = tmp_path / "child.yaml"
        child.write_text(
            "workflow:\n  name: child\n  entry_point: run\n  workspace:\n"
            "    persistence: durable\nagents:\n  - name: run\n    type: script\n"
            "    command: 'true'\n"
        )
        config = _config(WorkflowStepDef(name="child", workflow="child.yaml", max_depth=None))
        with pytest.raises(ConfigurationError, match="inherited from the root"):
            compile_run_manifest(
                config,
                workflow_path=tmp_path / "root.yaml",
                environment=builtin_local_environment(),
            )

    def test_legacy_manifest_digest_matches_fixed_golden(self) -> None:
        # Requirement: a missing workspace leaves the existing canonical digest unchanged.
        payload = {
            "version": 1,
            "workflow": {"name": "old", "digest": None},
            "environment": {"name": "test", "source": "path", "digest": "sha256:x"},
            "profiles": {},
            "secrets": [],
            "conductor_version": "old",
            "audit": {"hermetic": False, "classification": "non-hermetic-compatibility"},
        }
        manifest = ResolvedRunManifest.model_validate(payload)
        assert canonical_json_digest(manifest.model_dump(mode="json")) == (
            "sha256:90cbc54ea127c0cd6a12c66223e5613dc6e53b21c607393eddf28b967ccface7"
        )

    def test_retained_docker_script_policy_compiles(self) -> None:
        # Requirement: a backend advertising retention accepts a durable script workspace.
        config = _config(_script("run", "container"))
        config.workflow.workspace = WorkspaceConfig(persistence="durable")
        manifest = compile_run_manifest(
            config,
            workflow_path=None,
            environment=_environment(docker=DockerProfileOptions(image="busybox")),
        )
        assert manifest.workspace is not None
        assert manifest.workspace.persistence == "durable"
