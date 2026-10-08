from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from conductor.config.environment import (
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
    SecretBinding,
    SecretBindingSource,
)
from conductor.config.schema import (
    AgentDef,
    ForEachDef,
    MCPServerDef,
    OutputField,
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
from conductor.config.validator import _EnvironmentValidationContext, _validate_secret_references
from conductor.engine.run_manifest import (
    ResolvedRunManifest,
    ResolvedSecretUse,
    compile_run_manifest,
)
from conductor.exceptions import ConfigurationError


def _secret(
    ref: str,
    scope: Literal["script", "mcp", "agent"],
    *,
    env: str | None = None,
    header: str | None = None,
) -> StepSecretRef:
    return StepSecretRef(
        ref=ref,
        scope=scope,
        delivery=SecretDelivery(env=env, header=header),
    )


def _environment(
    *,
    inherit_control_environment: bool | None = None,
    secret_names: tuple[str, ...] = ("alpha", "beta"),
) -> ResolvedEnvironment:
    document = EnvironmentDocument(
        default="default",
        profiles={
            "default": ProfileDefinition(
                backend="local",
                inherit_control_environment=inherit_control_environment,
            )
        },
        secrets={
            name: SecretBinding(source=SecretBindingSource(env=f"SOURCE_{name.upper()}"))
            for name in secret_names
        },
    )
    return ResolvedEnvironment(
        document=document,
        name="test-env",
        source="path",
        path=None,
        digest="sha256:test",
    )


def _script_config(*secrets: StepSecretRef) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(name="script-secrets", entry_point="run"),
        agents=[
            ScriptStepDef(
                name="run",
                command="echo",
                execution=StepExecutionConfig(secrets=list(secrets)),
                routes=[RouteDef(to="$end")],
            )
        ],
        output={"result": "{{ run.output.stdout }}"},
    )


def _agent_config(*secrets: StepSecretRef) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(name="agent-secrets", entry_point="agent"),
        agents=[
            AgentDef(
                name="agent",
                model="gpt-4",
                prompt="test",
                output={"value": OutputField(type="string")},
                execution=StepExecutionConfig(secrets=list(secrets)),
                routes=[RouteDef(to="$end")],
            )
        ],
        output={"result": "{{ agent.output.value }}"},
    )


def _mcp_config(server: MCPServerDef) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="mcp-secrets",
            entry_point="agent",
            runtime=RuntimeConfig(mcp_servers={"server": server}),
        ),
        agents=[
            AgentDef(
                name="agent",
                model="gpt-4",
                prompt="test",
                output={"value": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            )
        ],
        output={"result": "{{ agent.output.value }}"},
    )


def _remote_mcp_config(
    server_type: Literal["http", "sse"],
    *,
    default_provider: ProviderName = "copilot",
    agent_providers: tuple[ProviderName | None, ...] = (None,),
    env_delivery: bool = True,
) -> WorkflowConfig:
    secret = (
        _secret("alpha", "mcp", env="REMOTE_TOKEN")
        if env_delivery
        else _secret("alpha", "mcp", header="Authorization")
    )
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="remote-mcp-secrets",
            entry_point="agent-0",
            runtime=RuntimeConfig(
                provider=default_provider,
                mcp_servers={
                    "server": MCPServerDef(
                        type=server_type,
                        url="https://example.test/mcp",
                        secrets=[secret],
                    )
                },
            ),
        ),
        agents=[
            AgentDef(
                name=f"agent-{index}",
                provider=provider,
                prompt="test",
                routes=[RouteDef(to="$end")],
            )
            for index, provider in enumerate(agent_providers)
        ],
    )


def test_secret_uses_are_recorded_in_step_then_sorted_server_order() -> None:
    # Requirement: records preserve executable-step order, followed by MCP servers by name.
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="ordered-secrets",
            entry_point="first",
            runtime=RuntimeConfig(
                mcp_servers={
                    "zeta": MCPServerDef(
                        type="http",
                        url="https://example.test/zeta",
                        secrets=[_secret("beta", "mcp", header="Authorization")],
                    ),
                    "alpha": MCPServerDef(
                        command="alpha-server",
                        secrets=[_secret("alpha", "mcp", env="MCP_TOKEN")],
                    ),
                }
            ),
        ),
        agents=[
            ScriptStepDef(
                name="first",
                command="first",
                execution=StepExecutionConfig(
                    secrets=[_secret("alpha", "script", env="FIRST_TOKEN")]
                ),
                routes=[RouteDef(to="second")],
            ),
            ScriptStepDef(
                name="second",
                command="second",
                execution=StepExecutionConfig(
                    secrets=[_secret("beta", "script", env="SECOND_TOKEN")]
                ),
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={"result": "{{ second.output.stdout }}"},
    )

    manifest = compile_run_manifest(config, workflow_path=None, environment=_environment())

    assert manifest.secrets == (
        ResolvedSecretUse(
            consumer="first",
            ref="alpha",
            scope="script",
            delivery_kind="env",
            delivery_name="FIRST_TOKEN",
        ),
        ResolvedSecretUse(
            consumer="second",
            ref="beta",
            scope="script",
            delivery_kind="env",
            delivery_name="SECOND_TOKEN",
        ),
        ResolvedSecretUse(
            consumer="mcp:alpha",
            ref="alpha",
            scope="mcp",
            delivery_kind="env",
            delivery_name="MCP_TOKEN",
        ),
        ResolvedSecretUse(
            consumer="mcp:zeta",
            ref="beta",
            scope="mcp",
            delivery_kind="header",
            delivery_name="Authorization",
        ),
    )


@pytest.mark.parametrize(("configured", "expected"), [(False, False), (None, True)])
def test_profile_records_effective_inherit_control_environment(
    configured: bool | None,
    expected: bool,
) -> None:
    # Requirement: the compiler writes explicit False and the local backend's effective True.
    manifest = compile_run_manifest(
        _script_config(),
        workflow_path=None,
        environment=_environment(inherit_control_environment=configured),
    )

    assert manifest.profiles["run"].inherit_control_environment is expected
    assert (
        manifest.model_dump(mode="json")["profiles"]["run"]["inherit_control_environment"]
        is expected
    )


def test_secret_manifest_compile_is_byte_identical() -> None:
    # Requirement: secret audit records contain only run-invariant use-site metadata.
    config = _script_config(_secret("alpha", "script", env="TOKEN"))
    environment = _environment()

    first = compile_run_manifest(config, workflow_path=None, environment=environment)
    second = compile_run_manifest(config, workflow_path=None, environment=environment)

    assert json.dumps(first.model_dump(mode="json")) == json.dumps(second.model_dump(mode="json"))


def test_pre_secrets_v1_payload_round_trips_with_additive_defaults() -> None:
    # Requirement: an origin/main v1 payload without either additive field still parses.
    old_payload = {
        "version": 1,
        "workflow": {"name": "old", "digest": None},
        "environment": {"name": "local/default", "source": "builtin", "digest": "sha256:0"},
        "profiles": {"run": {"profile": "default", "backend": "local"}},
        "conductor_version": "old",
        "audit": {"hermetic": False, "classification": "non-hermetic-compatibility"},
    }

    parsed = ResolvedRunManifest.model_validate(old_payload)

    assert parsed.secrets == ()
    assert parsed.profiles["run"].inherit_control_environment is True
    assert parsed.model_dump(mode="json", exclude_defaults=True) == old_payload


def test_script_scope_conflicts_with_mcp_scope() -> None:
    # Requirement: a secret attached to a script position must declare script scope.
    with pytest.raises(ConfigurationError, match="position requires scope 'script'"):
        compile_run_manifest(
            _script_config(_secret("alpha", "mcp", env="TOKEN")),
            workflow_path=None,
            environment=_environment(),
        )


def test_mcp_scope_conflicts_with_script_scope() -> None:
    # Requirement: a secret attached to an MCP server position must declare MCP scope.
    server = MCPServerDef(
        command="server",
        secrets=[_secret("alpha", "script", env="TOKEN")],
    )
    with pytest.raises(ConfigurationError, match="position requires scope 'mcp'"):
        compile_run_manifest(_mcp_config(server), workflow_path=None, environment=_environment())


@pytest.mark.parametrize(
    "config",
    [
        _agent_config(_secret("alpha", "script", env="TOKEN")),
        _script_config(_secret("alpha", "agent", env="TOKEN")),
        _mcp_config(
            MCPServerDef(
                command="server",
                secrets=[_secret("alpha", "agent", env="TOKEN")],
            )
        ),
    ],
)
def test_agent_secret_scope_is_reserved_until_step_7(config: WorkflowConfig) -> None:
    # Requirement: each use-site rejects an incompatible secret scope or local delivery.
    with pytest.raises(ConfigurationError, match=r"scope|scoped delivery"):
        compile_run_manifest(config, workflow_path=None, environment=_environment())


def test_header_delivery_on_step_is_rejected() -> None:
    # Requirement: HTTP header delivery is valid only at an MCP server use-site.
    with pytest.raises(ConfigurationError, match="header delivery is MCP-only"):
        compile_run_manifest(
            _script_config(_secret("alpha", "script", header="Authorization")),
            workflow_path=None,
            environment=_environment(),
        )


def test_header_delivery_on_stdio_server_names_transport() -> None:
    # Requirement: stdio MCP servers cannot receive HTTP headers and the error names transport.
    server = MCPServerDef(
        command="server",
        secrets=[_secret("alpha", "mcp", header="Authorization")],
    )
    with pytest.raises(ConfigurationError, match=r"transport 'stdio'"):
        compile_run_manifest(_mcp_config(server), workflow_path=None, environment=_environment())


def test_unknown_secret_ref_lists_available_names() -> None:
    # Requirement: runtime validation identifies the unknown ref and every available binding.
    with pytest.raises(ConfigurationError) as exc_info:
        compile_run_manifest(
            _script_config(_secret("missing", "script", env="TOKEN")),
            workflow_path=None,
            environment=_environment(),
        )

    message = str(exc_info.value)
    assert "missing" in message
    assert "available secrets: alpha, beta" in message


# ---------------------------------------------------------------------------
# Delivery collision checks at compile time (review b6) and provider transport
# restrictions for remote env delivery (review r6)
# ---------------------------------------------------------------------------


def test_two_secret_refs_colliding_on_one_delivery_name_fail_at_compile() -> None:
    # Requirement (review b6): two secret references targeting the same
    # delivery variable collapse into one dictionary entry at delivery time
    # (last credential wins) while the manifest still lists both. The
    # collision is rejected at manifest compilation — the shared,
    # platform-aware check — before any delivery dict is built, and normal
    # execution hits this because the engine compiles the manifest even
    # though it never runs the semantic validator.
    config = _script_config(
        _secret("alpha", "script", env="TOKEN"),
        _secret("beta", "script", env="TOKEN"),
    )
    with pytest.raises(ConfigurationError, match="collides within consumer 'run'"):
        compile_run_manifest(config, workflow_path=None, environment=_environment())


def test_literal_env_case_insensitive_collision_fails_at_compile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement (review b6): the runtime literal-collision backstop compares
    # env names case-sensitively on POSIX, which misses the case-only overlap
    # that Windows environment semantics make real. The compile-time check is
    # platform-aware and rejects it there.
    monkeypatch.setattr("sys.platform", "win32")
    config = WorkflowConfig(
        workflow=WorkflowDef(name="case-collision", entry_point="run"),
        agents=[
            ScriptStepDef(
                name="run",
                command="echo",
                env={"Token": "authored"},
                execution=StepExecutionConfig(secrets=[_secret("alpha", "script", env="TOKEN")]),
                routes=[RouteDef(to="$end")],
            )
        ],
        output={"result": "{{ run.output.stdout }}"},
    )
    with pytest.raises(ConfigurationError, match="collides within consumer 'run'"):
        compile_run_manifest(config, workflow_path=None, environment=_environment())


def test_mcp_secret_refs_colliding_on_one_header_fail_at_compile() -> None:
    # Requirement (review b6): the same collapse exists for MCP header
    # deliveries within one server (header names are always case-insensitive).
    server = MCPServerDef(
        type="http",
        url="https://example.test",
        secrets=[
            _secret("alpha", "mcp", header="X-Api-Key"),
            _secret("beta", "mcp", header="x-api-key"),
        ],
    )
    with pytest.raises(ConfigurationError, match="collides within consumer 'mcp:server'"):
        compile_run_manifest(_mcp_config(server), workflow_path=None, environment=_environment())


@pytest.mark.parametrize("server_type", ["http", "sse"])
def test_default_claude_agent_sdk_with_only_copilot_consumers_compiles(
    server_type: Literal["http", "sse"],
) -> None:
    # Requirement: explicit Copilot overrides, not an unused Claude Agent SDK default, govern.
    config = _remote_mcp_config(
        server_type,
        default_provider="claude-agent-sdk",
        agent_providers=("copilot", "copilot"),
    )

    manifest = compile_run_manifest(config, workflow_path=None, environment=_environment())

    assert manifest.secrets[0].delivery_kind == "env"


@pytest.mark.parametrize("server_type", ["http", "sse"])
def test_claude_agent_sdk_override_rejects_remote_env_delivery(
    server_type: Literal["http", "sse"],
) -> None:
    # Requirement: one effective Claude Agent SDK override rejects remote env delivery.
    config = _remote_mcp_config(
        server_type,
        agent_providers=(None, "claude-agent-sdk"),
    )

    with pytest.raises(ConfigurationError, match="cannot deliver environment variables"):
        compile_run_manifest(config, workflow_path=None, environment=_environment())


def test_for_each_claude_agent_sdk_override_rejects_remote_env_delivery() -> None:
    # Requirement: provider-backed inline for-each agents count as MCP consumers.
    config = _remote_mcp_config("http")
    config.for_each = [
        ForEachDef.model_validate(
            {
                "name": "batch",
                "type": "for_each",
                "source": "workflow.input.items",
                "agent": {
                    "name": "worker",
                    "provider": "claude-agent-sdk",
                    "prompt": "work",
                },
                "as": "item",
            }
        )
    ]

    with pytest.raises(ConfigurationError, match="cannot deliver environment variables"):
        compile_run_manifest(config, workflow_path=None, environment=_environment())


def test_remote_env_delivery_without_provider_backed_agents_compiles() -> None:
    # Requirement: scripts and direct MCP steps do not make the workflow default a consumer.
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="script-only",
            entry_point="run",
            runtime=RuntimeConfig(
                provider="claude-agent-sdk",
                mcp_servers={
                    "server": MCPServerDef(
                        type="http",
                        url="https://example.test/mcp",
                        secrets=[_secret("alpha", "mcp", env="REMOTE_TOKEN")],
                    )
                },
            ),
        ),
        agents=[ScriptStepDef(name="run", command="echo", routes=[RouteDef(to="$end")])],
    )

    manifest = compile_run_manifest(config, workflow_path=None, environment=_environment())

    assert manifest.secrets[0].delivery_name == "REMOTE_TOKEN"


@pytest.mark.parametrize("server_type", ["http", "sse"])
@pytest.mark.parametrize("provider", ["claude", "openai"])
def test_stdio_only_consumer_rejects_remote_server_before_secret_validation(
    server_type: Literal["http", "sse"],
    provider: ProviderName,
) -> None:
    # Requirement: Claude and OpenAI reject each remote transport at the server level.
    config = _remote_mcp_config(
        server_type,
        agent_providers=(provider,),
        env_delivery=False,
    )

    with pytest.raises(ConfigurationError) as exc_info:
        compile_run_manifest(config, workflow_path=None, environment=_environment())

    assert exc_info.value.args[0] == (
        f"MCP server 'server' uses remote transport '{server_type}', but provider "
        f"'{provider}' supports only stdio MCP."
    )


def test_multiple_stdio_only_consumers_are_sorted_in_remote_server_error() -> None:
    # Requirement: a multi-provider server error names every stdio-only consumer in order.
    config = _remote_mcp_config(
        "http",
        agent_providers=("openai", "copilot", "claude"),
        env_delivery=False,
    )

    with pytest.raises(ConfigurationError) as exc_info:
        compile_run_manifest(config, workflow_path=None, environment=_environment())

    assert exc_info.value.args[0] == (
        "MCP server 'server' uses remote transport 'http', but providers "
        "['claude', 'openai'] support only stdio MCP."
    )


@pytest.mark.parametrize("default_provider", ["claude", "openai"])
def test_unused_stdio_only_default_does_not_reject_remote_server(
    default_provider: ProviderName,
) -> None:
    # Requirement: an unused stdio-only workflow default does not constrain Copilot consumers.
    config = _remote_mcp_config(
        "http",
        default_provider=default_provider,
        agent_providers=("copilot",),
        env_delivery=False,
    )

    manifest = compile_run_manifest(config, workflow_path=None, environment=_environment())

    assert manifest.secrets[0].delivery_kind == "header"


def test_mixed_copilot_and_claude_agent_sdk_consumers_allow_remote_header() -> None:
    # Requirement: Claude Agent SDK restricts remote env delivery, not remote headers.
    config = _remote_mcp_config(
        "http",
        agent_providers=("copilot", "claude-agent-sdk"),
        env_delivery=False,
    )

    manifest = compile_run_manifest(config, workflow_path=None, environment=_environment())

    assert manifest.secrets[0].delivery_kind == "header"


def test_mixed_copilot_and_claude_agent_sdk_consumers_reject_remote_env() -> None:
    # Requirement: any effective Claude Agent SDK consumer makes remote env delivery invalid.
    config = _remote_mcp_config(
        "http",
        agent_providers=("copilot", "claude-agent-sdk"),
    )

    with pytest.raises(ConfigurationError, match="cannot deliver environment variables"):
        compile_run_manifest(config, workflow_path=None, environment=_environment())


def test_compile_and_validate_share_remote_delivery_error_text(tmp_path: Path) -> None:
    # Requirement: manifest compilation and semantic validation use one delivery error formatter.
    config = _remote_mcp_config("http", agent_providers=("claude-agent-sdk",))
    context: _EnvironmentValidationContext = {
        "refs_found": False,
        "environments": {"test-env": _environment()},
        "explicit": True,
        "root_workflow_dir": tmp_path,
        "warned_no_environments": False,
        "warned_no_secret_environments": False,
    }

    with pytest.raises(ConfigurationError) as exc_info:
        compile_run_manifest(config, workflow_path=None, environment=_environment())
    errors, _warnings = _validate_secret_references(config, context)

    assert errors[0] == exc_info.value.args[0]
