"""Static placement and secret delivery contracts for agent realms."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

import pytest

from conductor.config.environment import (
    AcaProfileOptions,
    DockerProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
    SecretBinding,
    SecretBindingSource,
)
from conductor.config.schema import (
    AgentDef,
    MCPServerDef,
    ProviderName,
    RouteDef,
    RuntimeConfig,
    ScriptStepDef,
    SecretDelivery,
    StepExecutionConfig,
    StepSecretRef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.engine import execution_resolution
from conductor.engine import workflow as workflow_module
from conductor.engine.run_manifest import ResolvedRunManifest, compile_run_manifest
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ConfigurationError
from conductor.execution import (
    AgentEventSink,
    AgentResult,
    AgentSpec,
    RunnerCapabilities,
    WorkspaceLease,
)
from conductor.execution.local import LocalRunnerBackend
from conductor.providers.copilot import CopilotProvider


class _RemoteBackend(LocalRunnerBackend):
    """In-memory agent backend for testing dispatch without a daemon."""

    def __init__(self) -> None:
        self.specs: list[AgentSpec] = []

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            batch=False, sessions=False, shared_workspace=False, snapshots=False, agent=True
        )

    async def run_agent(
        self,
        spec: AgentSpec,
        lease: WorkspaceLease | None,
        *,
        on_event: AgentEventSink | None = None,
        interrupt_signal: asyncio.Event | None = None,
        execute_local: Callable[[], Awaitable[AgentResult]] | None = None,
    ) -> AgentResult:
        assert execute_local is None
        self.specs.append(spec)
        return AgentResult(content={"answer": "from-remote"})


def _environment(kind: str) -> ResolvedEnvironment:
    profile = {
        "local": ProfileDefinition(backend="local"),
        "docker": ProfileDefinition(
            backend="docker",
            docker=DockerProfileOptions(image="script:test", runner_image="runner:test"),
        ),
        "docker_missing": ProfileDefinition(
            backend="docker", docker=DockerProfileOptions(image="script:test")
        ),
        "aca": ProfileDefinition(
            backend="aca", aca=AcaProfileOptions(pool_endpoint="https://pool.example.test")
        ),
    }[kind]
    return ResolvedEnvironment(
        document=EnvironmentDocument(
            default="realm",
            profiles={"realm": profile},
            secrets={
                "token": SecretBinding(
                    source=SecretBindingSource(env="CONDUCTOR_REALM_TEST_TOKEN"),
                )
            },
        ),
        name="realm-test",
        source="path",
        path=None,
        digest="sha256:test",
    )


def _config(
    *,
    provider: ProviderName | None = None,
    default_provider: ProviderName = "copilot",
    env_name: str = "MCP_TOKEN",
    with_stdio_mcp: bool = True,
) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="realm-test",
            entry_point="worker",
            runtime=RuntimeConfig(
                provider=default_provider,
                mcp_servers={"echo": MCPServerDef(command="echo")} if with_stdio_mcp else {},
            ),
        ),
        agents=[
            AgentDef(
                name="worker",
                provider=provider,
                prompt="Answer",
                execution=StepExecutionConfig(
                    secrets=[
                        StepSecretRef(
                            ref="token", scope="agent", delivery=SecretDelivery(env=env_name)
                        )
                    ]
                ),
                routes=[RouteDef(to="$end")],
            )
        ],
    )


def _validate(config: WorkflowConfig, environment: ResolvedEnvironment) -> list[str]:
    return validate_workflow_config(
        config,
        _environment_context={
            "refs_found": False,
            "environments": {environment.name: environment},
            "explicit": True,
            "root_workflow_dir": None,
            "warned_no_environments": False,
            "warned_no_secret_environments": False,
        },
    )


@pytest.mark.parametrize("kind", ["local", "docker", "docker_missing", "aca"])
@pytest.mark.parametrize("surface", ["bare", "explicit", "compile", "dispatch"])
@pytest.mark.asyncio
async def test_agent_scope_placement_matrix(
    kind: str, surface: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: remote-only delivery is checked statically and never starts a daemon.
    monkeypatch.setenv("CONDUCTOR_REALM_TEST_TOKEN", "test-only-secret")
    config, environment = _config(), _environment(kind)
    if surface == "bare":
        warnings = validate_workflow_config(config)
        assert warnings == [
            "Agent step 'worker' agent-scope delivery requires a remote execution "
            "profile; run conductor validate --environment <name> for the full cross-check",
            "secret references could not be checked against any authored environment; "
            "run conductor validate --environment <name> for the full cross-check",
        ]
        return
    expected = None
    if kind == "local":
        expected = (
            "Agent step 'worker': local agent runtime inherits the control environment; "
            "scoped delivery for local agents is not built — use a remote execution profile "
            "or mcp/script scope."
        )
    elif kind == "docker_missing":
        expected = (
            "Agent step 'worker' resolves to Docker without docker.runner_image."
            "\n\n💡 Suggestion: Set docker.runner_image to an image containing the "
            "Conductor runner."
        )
    if expected is not None:
        if surface == "explicit":
            prefix = "environment 'realm-test': " if kind == "docker_missing" else ""
            expected = (
                f"Workflow configuration validation failed:\n  - {prefix}{expected}"
                "\n\n💡 Suggestion: Fix the validation errors listed above and try again."
            )
        with pytest.raises(ConfigurationError) as failure:
            if surface == "explicit":
                _validate(config, environment)
            elif surface == "compile":
                compile_run_manifest(config, workflow_path=None, environment=environment)
            else:
                WorkflowEngine(config, execution_environment=environment)
        assert str(failure.value) == expected
        return
    if surface == "explicit":
        assert _validate(config, environment) == []
    elif surface == "compile":
        manifest = compile_run_manifest(config, workflow_path=None, environment=environment)
        profile = manifest.profiles["worker"]
        assert profile.inner_provider == "copilot"
        assert profile.realm_image == ("runner:test" if kind == "docker" else None)
        assert [(use.scope, use.delivery_name) for use in manifest.secrets] == [
            ("agent", "MCP_TOKEN")
        ]
        assert "test-only-secret" not in str(manifest.model_dump(mode="json"))
    else:
        backend = _RemoteBackend()
        monkeypatch.setitem(execution_resolution.BACKEND_FACTORIES, kind, lambda: backend)
        # Bundle materialization belongs to the Docker backend suite, not dispatch.
        monkeypatch.setattr(workflow_module, "prepare_run_bundle", AsyncMock(return_value=None))
        engine = WorkflowEngine(
            config,
            CopilotProvider(mock_handler=lambda *_: {}),
            execution_environment=environment,
        )
        assert await engine.run({}) == {}
        assert len(backend.specs) == 1
        assert backend.specs[0].env_overlay == {"MCP_TOKEN": "test-only-secret"}


@pytest.mark.parametrize(
    "name",
    [
        "base_url",
        "api_key",
        "bearer_token",
        "github_token",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "COPILOT_PROVIDER_API_KEY",
        "COPILOT_PROVIDER_BEARER_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ],
)
def test_reserved_agent_delivery_names_are_rejected_before_execution(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: per-call MCP variables cannot shadow model provider credentials.
    monkeypatch.setenv("CONDUCTOR_REALM_TEST_TOKEN", "test-only-secret")
    config, environment = _config(env_name=name), _environment("docker")
    with pytest.raises(ConfigurationError, match="reserved for provider credentials"):
        _validate(config, environment)
    with pytest.raises(ConfigurationError, match="reserved for provider credentials"):
        compile_run_manifest(config, workflow_path=None, environment=environment)


@pytest.mark.parametrize("provider", ["claude-agent-sdk", "hermes"])
def test_unsupported_inner_provider_override_fails_both_static_gates(
    provider: ProviderName,
) -> None:
    # Requirement: an agent override, not just the workflow default, controls inner placement.
    config, environment = _config(provider=provider), _environment("aca")
    with pytest.raises(
        ConfigurationError, match="supported inner providers: copilot, openai, claude"
    ):
        _validate(config, environment)
    with pytest.raises(
        ConfigurationError, match="supported inner providers: copilot, openai, claude"
    ):
        compile_run_manifest(config, workflow_path=None, environment=environment)


def test_legacy_manifest_payload_round_trips_without_realm_fields() -> None:
    # Requirement: older version-one payloads stay readable and script-only dumps stay stable.
    config = WorkflowConfig(
        workflow=WorkflowDef(name="local", entry_point="worker"),
        agents=[AgentDef(name="worker", prompt="Answer", routes=[RouteDef(to="$end")])],
    )
    payload = compile_run_manifest(
        config, workflow_path=None, environment=_environment("local")
    ).model_dump(mode="json")
    assert payload["version"] == 1
    assert "realm_image" not in payload["profiles"]["worker"]
    assert "inner_provider" not in payload["profiles"]["worker"]
    assert ResolvedRunManifest.model_validate(payload).model_dump(mode="json") == payload


def test_script_only_manifest_omits_agent_realm_fields() -> None:
    # Requirement: adding remote-agent audit fields does not alter script-only dumps.
    config = WorkflowConfig(
        workflow=WorkflowDef(name="script", entry_point="run"),
        agents=[ScriptStepDef(name="run", command="echo")],
    )
    profile = compile_run_manifest(
        config, workflow_path=None, environment=_environment("local")
    ).model_dump(mode="json")["profiles"]["run"]
    assert profile == {"profile": "realm", "backend": "local", "inherit_control_environment": True}


@pytest.mark.parametrize("provider", ["claude-agent-sdk", "hermes"])
def test_unsupported_workflow_default_provider_fails_remote_compile(
    provider: ProviderName,
) -> None:
    # Requirement: the workflow default is the effective inner provider without an override.
    config = _config(default_provider=provider)
    with pytest.raises(
        ConfigurationError, match="supported inner providers: copilot, openai, claude"
    ):
        compile_run_manifest(config, workflow_path=None, environment=_environment("docker"))
