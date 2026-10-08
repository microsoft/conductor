"""Validator cross-checks for secret references: structural, explicit, ambient."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import ANY, patch

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
    WorkflowDefaults,
    WorkflowStepDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.engine.run_manifest import compile_run_manifest
from conductor.exceptions import ConfigurationError


def _secret(
    ref: str,
    scope: Literal["script", "mcp", "agent"],
    *,
    env: str | None = None,
    header: str | None = None,
) -> StepSecretRef:
    return StepSecretRef(ref=ref, scope=scope, delivery=SecretDelivery(env=env, header=header))


def _binding(
    env_var: str,
    allow: list[Literal["script", "mcp"]] | None = None,
) -> SecretBinding:
    return SecretBinding(source=SecretBindingSource(env=env_var), allow=allow)


def _environment(
    secrets: dict[str, SecretBinding] | None = None,
    *,
    name: str = "test-env",
) -> ResolvedEnvironment:
    document = EnvironmentDocument(
        default="default",
        profiles={"default": ProfileDefinition(backend="local")},
        secrets=secrets,
    )
    return ResolvedEnvironment(
        document=document,
        name=name,
        source="path",
        path=None,
        digest="sha256:test",
    )


def _alpha_beta_environment() -> ResolvedEnvironment:
    return _environment(
        {
            "alpha": _binding("CONDUCTOR_TEST_TASK10_ALPHA"),
            "beta": _binding("CONDUCTOR_TEST_TASK10_BETA"),
        }
    )


def _script_config(
    *secrets: StepSecretRef,
    env: dict[str, str] | None = None,
) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(name="script-secrets", entry_point="run"),
        agents=[
            ScriptStepDef(
                name="run",
                command="echo",
                env=env or {},
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


def _explicit_context(environment: ResolvedEnvironment, root: Path) -> dict[str, Any]:
    return {
        "refs_found": False,
        "environments": {environment.name: environment},
        "explicit": True,
        "root_workflow_dir": root,
        "warned_no_environments": False,
        "warned_no_secret_environments": False,
    }


def _validate_explicit(
    config: WorkflowConfig,
    environment: ResolvedEnvironment,
    tmp_path: Path,
) -> list[str]:
    return validate_workflow_config(
        config,
        workflow_path=tmp_path / "workflow.yaml",
        _environment_context=cast(Any, _explicit_context(environment, tmp_path)),
    )


class TestScopeVsPosition:
    """Structural checks mirroring the manifest compiler's fail-fast choke."""

    def test_script_step_rejects_mcp_scope(self, tmp_path: Path) -> None:
        # Requirement: a secret attached to a script position must declare script scope.
        config = _script_config(_secret("alpha", "mcp", env="TOKEN"))
        with pytest.raises(ConfigurationError, match="position requires scope 'script'"):
            _validate_explicit(config, _alpha_beta_environment(), tmp_path)

    def test_agent_step_with_secrets_is_reserved_until_step_7(self, tmp_path: Path) -> None:
        # Requirement: an agent cannot consume a script-scoped secret.
        config = _agent_config(_secret("alpha", "script", env="TOKEN"))
        with pytest.raises(ConfigurationError, match="position requires scope 'agent'"):
            _validate_explicit(config, _alpha_beta_environment(), tmp_path)

    def test_script_step_rejects_agent_scope(self, tmp_path: Path) -> None:
        # Requirement: script steps cannot consume an agent-scoped secret.
        config = _script_config(_secret("alpha", "agent", env="TOKEN"))
        with pytest.raises(ConfigurationError, match="position requires scope 'script'"):
            _validate_explicit(config, _alpha_beta_environment(), tmp_path)

    def test_mcp_server_rejects_script_scope(self, tmp_path: Path) -> None:
        # Requirement: a secret attached to an MCP server must declare mcp scope.
        server = MCPServerDef(command="server", secrets=[_secret("alpha", "script", env="TOKEN")])
        with pytest.raises(ConfigurationError, match="position requires scope 'mcp'"):
            _validate_explicit(_mcp_config(server), _alpha_beta_environment(), tmp_path)

    def test_mcp_server_rejects_agent_scope(self, tmp_path: Path) -> None:
        # Requirement: local MCP consumers cannot receive agent-scoped delivery.
        server = MCPServerDef(command="server", secrets=[_secret("alpha", "agent", env="TOKEN")])
        with pytest.raises(
            ConfigurationError, match="local agent runtime inherits the control environment"
        ):
            _validate_explicit(_mcp_config(server), _alpha_beta_environment(), tmp_path)

    def test_script_step_rejects_header_delivery(self, tmp_path: Path) -> None:
        # Requirement: HTTP header delivery is valid only at an MCP server use-site.
        config = _script_config(_secret("alpha", "script", header="Authorization"))
        with pytest.raises(ConfigurationError, match="header delivery is MCP-only"):
            _validate_explicit(config, _alpha_beta_environment(), tmp_path)

    def test_stdio_server_rejects_header_delivery_naming_transport(self, tmp_path: Path) -> None:
        # Requirement: stdio servers cannot receive headers; the error names the transport.
        server = MCPServerDef(
            command="server",
            secrets=[_secret("alpha", "mcp", header="Authorization")],
        )
        with pytest.raises(ConfigurationError, match=r"transport 'stdio'"):
            _validate_explicit(_mcp_config(server), _alpha_beta_environment(), tmp_path)

    def test_http_server_accepts_header_delivery(self, tmp_path: Path) -> None:
        # Requirement: header delivery over an HTTP transport is a valid use-site.
        server = MCPServerDef(
            type="http",
            url="https://example.test/mcp",
            secrets=[_secret("alpha", "mcp", header="Authorization")],
        )
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("CONDUCTOR_TEST_TASK10_ALPHA", "x")
            monkeypatch.setenv("CONDUCTOR_TEST_TASK10_BETA", "x")
            warnings = _validate_explicit(_mcp_config(server), _alpha_beta_environment(), tmp_path)
        assert warnings == []

    def test_for_each_inline_agent_secrets_error_names_the_group(self, tmp_path: Path) -> None:
        # Requirement: an inline for-each agent's consumer label matches the
        # manifest identity key (``for_each.<group>.agent``).
        config = WorkflowConfig(
            workflow=WorkflowDef(name="fe", entry_point="start"),
            agents=[AgentDef(name="start", prompt="x", routes=[RouteDef(to="$end")])],
            for_each=[
                ForEachDef(
                    name="batch",
                    type="for_each",
                    source="workflow.input.items",
                    agent=AgentDef(
                        name="worker",
                        model="gpt-4",
                        prompt="work",
                        execution=StepExecutionConfig(
                            secrets=[_secret("alpha", "script", env="TOKEN")]
                        ),
                    ),
                    **{"as": "item"},
                )
            ],
            output={"result": "{{ start.output.value }}"},
        )
        with pytest.raises(
            ConfigurationError, match=r"for_each\.batch\.agent.*requires scope 'agent'"
        ):
            _validate_explicit(config, _alpha_beta_environment(), tmp_path)


class TestDeliveryCollisions:
    """Delivery-name collisions within one consumer."""

    def test_script_literal_env_collides_with_binding(self, tmp_path: Path) -> None:
        # Requirement: a literal script ``env:`` name and a binding delivery share
        # one namespace, so a collision is an error.
        config = _script_config(_secret("alpha", "script", env="TOKEN"), env={"TOKEN": "literal"})
        with pytest.raises(ConfigurationError, match="collides within consumer 'run'"):
            _validate_explicit(config, _alpha_beta_environment(), tmp_path)

    def test_two_bindings_collide_on_env_name(self, tmp_path: Path) -> None:
        # Requirement: two bindings delivering to the same env name collide.
        config = _script_config(
            _secret("alpha", "script", env="TOKEN"),
            _secret("beta", "script", env="TOKEN"),
        )
        with pytest.raises(ConfigurationError, match="collides within consumer 'run'"):
            _validate_explicit(config, _alpha_beta_environment(), tmp_path)

    def test_mcp_server_literal_env_collides_with_binding(self, tmp_path: Path) -> None:
        # Requirement: a literal server ``env:`` name collides with a binding delivery.
        server = MCPServerDef(
            command="server",
            env={"TOKEN": "literal"},
            secrets=[_secret("alpha", "mcp", env="TOKEN")],
        )
        with pytest.raises(ConfigurationError, match="collides within consumer 'mcp:server'"):
            _validate_explicit(_mcp_config(server), _alpha_beta_environment(), tmp_path)

    def test_header_collision_is_always_case_insensitive(self, tmp_path: Path) -> None:
        # Requirement: header names collide case-insensitively on every platform.
        server = MCPServerDef(
            type="http",
            url="https://example.test/mcp",
            headers={"X-Token": "literal"},
            secrets=[_secret("alpha", "mcp", header="x-token")],
        )
        with pytest.raises(ConfigurationError, match=r"collides within consumer 'mcp:server'"):
            _validate_explicit(_mcp_config(server), _alpha_beta_environment(), tmp_path)

    def test_env_names_are_case_sensitive_off_windows(self, tmp_path: Path) -> None:
        # Requirement: env names collide only by exact case off Windows.
        config = _script_config(_secret("alpha", "script", env="token"), env={"TOKEN": "literal"})
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(os, "name", "posix")
            monkeypatch.setenv("CONDUCTOR_TEST_TASK10_ALPHA", "x")
            monkeypatch.setenv("CONDUCTOR_TEST_TASK10_BETA", "x")
            warnings = _validate_explicit(config, _alpha_beta_environment(), tmp_path)
        assert warnings == []

    def test_env_names_casefold_on_windows(self, tmp_path: Path) -> None:
        # Requirement: env names collide case-insensitively on Windows.
        config = _script_config(_secret("alpha", "script", env="token"), env={"TOKEN": "literal"})
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(os, "name", "nt")
            with pytest.raises(ConfigurationError, match="collides within consumer 'run'"):
                _validate_explicit(config, _alpha_beta_environment(), tmp_path)

    def test_distinct_delivery_names_are_clean(self, tmp_path: Path) -> None:
        # Requirement: distinct literal and delivery names produce no collision.
        config = _script_config(
            _secret("alpha", "script", env="TOKEN"),
            _secret("beta", "script", env="OTHER_TOKEN"),
            env={"LITERAL": "literal"},
        )
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("CONDUCTOR_TEST_TASK10_ALPHA", "x")
            monkeypatch.setenv("CONDUCTOR_TEST_TASK10_BETA", "x")
            warnings = _validate_explicit(config, _alpha_beta_environment(), tmp_path)
        assert warnings == []


class TestEffectiveMcpConsumerProviders:
    """Provider transport checks use agents that can actually consume workflow MCP servers."""

    @pytest.mark.parametrize("server_type", ["http", "sse"])
    def test_default_claude_agent_sdk_with_only_copilot_consumers_passes(
        self,
        server_type: Literal["http", "sse"],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Requirement: explicit Copilot overrides replace an unused Claude Agent SDK default.
        config = _remote_mcp_config(
            server_type,
            default_provider="claude-agent-sdk",
            agent_providers=("copilot", "copilot"),
        )
        monkeypatch.setenv("CONDUCTOR_TEST_TASK10_ALPHA", "x")
        monkeypatch.setenv("CONDUCTOR_TEST_TASK10_BETA", "x")

        assert _validate_explicit(config, _alpha_beta_environment(), tmp_path) == []

    @pytest.mark.parametrize("server_type", ["http", "sse"])
    def test_claude_agent_sdk_override_rejects_remote_env_delivery(
        self,
        server_type: Literal["http", "sse"],
        tmp_path: Path,
    ) -> None:
        # Requirement: one effective Claude Agent SDK override rejects remote env delivery.
        config = _remote_mcp_config(
            server_type,
            agent_providers=(None, "claude-agent-sdk"),
        )

        with pytest.raises(ConfigurationError, match="cannot deliver environment variables"):
            _validate_explicit(config, _alpha_beta_environment(), tmp_path)

    def test_for_each_claude_agent_sdk_override_rejects_remote_env_delivery(
        self,
        tmp_path: Path,
    ) -> None:
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
            _validate_explicit(config, _alpha_beta_environment(), tmp_path)

    def test_remote_env_delivery_without_provider_backed_agents_passes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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
        monkeypatch.setenv("CONDUCTOR_TEST_TASK10_ALPHA", "x")
        monkeypatch.setenv("CONDUCTOR_TEST_TASK10_BETA", "x")

        assert _validate_explicit(config, _alpha_beta_environment(), tmp_path) == []

    @pytest.mark.parametrize("server_type", ["http", "sse"])
    @pytest.mark.parametrize("provider", ["claude", "openai"])
    def test_stdio_only_consumer_rejects_remote_server(
        self,
        server_type: Literal["http", "sse"],
        provider: ProviderName,
        tmp_path: Path,
    ) -> None:
        # Requirement: Claude and OpenAI reject each remote transport at the server level.
        config = _remote_mcp_config(
            server_type,
            agent_providers=(provider,),
            env_delivery=False,
        )

        with pytest.raises(ConfigurationError) as exc_info:
            _validate_explicit(config, _alpha_beta_environment(), tmp_path)

        assert (
            f"MCP server 'server' uses remote transport '{server_type}', but provider "
            f"'{provider}' supports only stdio MCP."
        ) in exc_info.value.args[0]

    @pytest.mark.parametrize("default_provider", ["claude", "openai"])
    def test_unused_stdio_only_default_does_not_reject_remote_server(
        self,
        default_provider: ProviderName,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Requirement: an unused stdio-only workflow default does not constrain Copilot agents.
        config = _remote_mcp_config(
            "http",
            default_provider=default_provider,
            agent_providers=("copilot",),
            env_delivery=False,
        )
        monkeypatch.setenv("CONDUCTOR_TEST_TASK10_ALPHA", "x")
        monkeypatch.setenv("CONDUCTOR_TEST_TASK10_BETA", "x")

        assert _validate_explicit(config, _alpha_beta_environment(), tmp_path) == []

    def test_mixed_consumers_allow_remote_header(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Requirement: Claude Agent SDK restricts remote env delivery, not remote headers.
        config = _remote_mcp_config(
            "http",
            agent_providers=("copilot", "claude-agent-sdk"),
            env_delivery=False,
        )
        monkeypatch.setenv("CONDUCTOR_TEST_TASK10_ALPHA", "x")
        monkeypatch.setenv("CONDUCTOR_TEST_TASK10_BETA", "x")

        assert _validate_explicit(config, _alpha_beta_environment(), tmp_path) == []

    def test_mixed_consumers_reject_remote_env(self, tmp_path: Path) -> None:
        # Requirement: any effective Claude Agent SDK consumer makes remote env delivery invalid.
        config = _remote_mcp_config(
            "http",
            agent_providers=("copilot", "claude-agent-sdk"),
        )

        with pytest.raises(ConfigurationError, match="cannot deliver environment variables"):
            _validate_explicit(config, _alpha_beta_environment(), tmp_path)


class TestRemoteTransportWithoutSecrets:
    """The remote-transport check runs with or without secret references."""

    @pytest.mark.parametrize("server_type", ["http", "sse"])
    @pytest.mark.parametrize("provider", ["claude", "openai"])
    def test_plain_validate_rejects_remote_server_without_secrets(
        self,
        server_type: Literal["http", "sse"],
        provider: ProviderName,
        tmp_path: Path,
    ) -> None:
        # Requirement: a remote server with no secrets is still rejected on the
        # plain validate path when an effective consumer is stdio-only.
        config = _remote_mcp_config(server_type, agent_providers=(provider,))
        config.workflow.runtime.mcp_servers["server"].secrets = []

        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")

        assert (
            f"MCP server 'server' uses remote transport '{server_type}', but provider "
            f"'{provider}' supports only stdio MCP."
        ) in exc_info.value.args[0]

    @pytest.mark.parametrize("default_provider", ["claude", "openai"])
    def test_unused_stdio_only_default_passes_remote_server_without_secrets(
        self,
        default_provider: ProviderName,
        tmp_path: Path,
    ) -> None:
        # Requirement: an unused stdio-only default does not reject a remote
        # server without secrets when every consumer overrides to Copilot.
        config = _remote_mcp_config(
            "http",
            default_provider=default_provider,
            agent_providers=("copilot",),
            env_delivery=False,
        )
        config.workflow.runtime.mcp_servers["server"].secrets = []

        validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")

    def test_compile_and_validate_share_remote_transport_error_text(
        self,
        tmp_path: Path,
    ) -> None:
        # Requirement: compilation and validation use one remote-transport
        # error formatter, with deterministically sorted provider lists.
        config = _remote_mcp_config(
            "http",
            agent_providers=("openai", "claude"),
            env_delivery=False,
        )

        with pytest.raises(ConfigurationError) as exc_info:
            compile_run_manifest(config, workflow_path=None, environment=_alpha_beta_environment())
        with pytest.raises(ConfigurationError) as validate_info:
            validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")

        message = (
            "MCP server 'server' uses remote transport 'http', but providers "
            "['claude', 'openai'] support only stdio MCP."
        )
        assert exc_info.value.args[0] == message
        assert message in validate_info.value.args[0]


class TestExplicitEnvironment:
    """Explicit ``--environment`` makes environment checks authoritative."""

    def test_allow_violation_is_an_error_naming_the_allow_list(self, tmp_path: Path) -> None:
        # Requirement: a known ref consumed outside its allow list is an error
        # naming the allow list (the compiler checks only membership, not allow).
        environment = _environment({"token": _binding("CONDUCTOR_TEST_TASK10_TOKEN", ["mcp"])})
        config = _script_config(_secret("token", "script", env="TOKEN"))
        with pytest.raises(ConfigurationError, match=r"binding allow list is \[mcp\]"):
            _validate_explicit(config, environment, tmp_path)

    def test_allow_list_covering_the_scope_is_clean(self, tmp_path: Path) -> None:
        # Requirement: a consumer class present in the allow list passes.
        environment = _environment(
            {"token": _binding("CONDUCTOR_TEST_TASK10_TOKEN", ["script", "mcp"])}
        )
        config = _script_config(_secret("token", "script", env="TOKEN"))
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("CONDUCTOR_TEST_TASK10_TOKEN", "x")
            warnings = _validate_explicit(config, environment, tmp_path)
        assert warnings == []

    def test_empty_allow_list_allows_no_consumer_classes(self, tmp_path: Path) -> None:
        # Requirement: ``allow: []`` is fail-closed and rejects every consumer.
        environment = _environment({"token": _binding("CONDUCTOR_TEST_TASK10_TOKEN", [])})
        config = _script_config(_secret("token", "script", env="TOKEN"))
        with pytest.raises(ConfigurationError, match=r"binding allow list is \[none\]"):
            _validate_explicit(config, environment, tmp_path)

    def test_unset_source_env_warns(self, tmp_path: Path) -> None:
        # Requirement: under --environment an unset source variable warns, because
        # the validation machine is not necessarily the run machine.
        environment = _environment({"token": _binding("CONDUCTOR_TEST_TASK10_UNSET")})
        config = _script_config(_secret("token", "script", env="TOKEN"))
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.delenv("CONDUCTOR_TEST_TASK10_UNSET", raising=False)
            warnings = _validate_explicit(config, environment, tmp_path)
        assert any(
            "secret binding 'token' uses an environment source that is unset" in w for w in warnings
        )

    def test_set_source_env_does_not_warn(self, tmp_path: Path) -> None:
        # Requirement: a present source variable produces no unset warning.
        environment = _environment({"token": _binding("CONDUCTOR_TEST_TASK10_SET")})
        config = _script_config(_secret("token", "script", env="TOKEN"))
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("CONDUCTOR_TEST_TASK10_SET", "x")
            warnings = _validate_explicit(config, environment, tmp_path)
        assert warnings == []

    def test_unknown_ref_is_rejected_by_the_validator(self, tmp_path: Path) -> None:
        # Requirement: under --environment an unknown reference is reported by
        # the validator itself. Only the ROOT manifest is compiled before
        # validation (in cli/validate.py); this validator then recursively
        # re-runs on child workflows, which no compilation covered — so the
        # "compilation already rejected it" assumption does not hold there and
        # the check must live here. For the root the CLI flow still fails
        # earlier at compile time, so the error is not double-reported there.
        environment = _environment({"token": _binding("CONDUCTOR_TEST_TASK10_TOKEN")})
        config = _script_config(_secret("ghost", "script", env="TOKEN"))
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("CONDUCTOR_TEST_TASK10_TOKEN", "x")
            with pytest.raises(ConfigurationError, match="not defined in environment 'test-env'"):
                _validate_explicit(config, environment, tmp_path)

    def test_claude_agent_sdk_remote_env_delivery_is_rejected(self, tmp_path: Path) -> None:
        # Requirement (review r6): env delivery on an http/sse server is
        # rejected at validation when the workflow runs on claude-agent-sdk,
        # whose remote config shape has no env field — the docs direct remote
        # MCP users to header delivery, and the message matches the manifest
        # compiler's verbatim.
        config = _mcp_config(
            MCPServerDef(
                type="http",
                url="https://example.test",
                secrets=[_secret("alpha", "mcp", env="REMOTE_TOKEN")],
            )
        )
        config.workflow.runtime.provider = RuntimeConfig(provider="claude-agent-sdk").provider
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("CONDUCTOR_TEST_TASK10_ALPHA", "x")
            with pytest.raises(ConfigurationError, match="cannot deliver environment variables"):
                _validate_explicit(config, _alpha_beta_environment(), tmp_path)

    def test_nested_workflow_unknown_ref_is_rejected(self, tmp_path: Path) -> None:
        # Requirement (review r3): a CHILD workflow referencing a binding the
        # explicit environment does not define fails validate --environment
        # even though only the root manifest was compiled — previously this
        # passed validation and failed only when the child started executing.
        environment = _environment({"token": _binding("CONDUCTOR_TEST_TASK10_TOKEN")})
        child = tmp_path / "child.yaml"
        child.write_text(
            textwrap.dedent(
                """\
                workflow:
                  name: child
                  entry_point: inner
                agents:
                  - name: inner
                    type: script
                    command: run
                    execution:
                      secrets:
                        - ref: ghost
                          scope: script
                          delivery:
                            env: CHILD_TOKEN
                    routes:
                      - to: $end
                """
            )
        )
        config = _script_config(_secret("token", "script", env="TOKEN"))
        config.agents.append(
            WorkflowStepDef(name="sub", workflow="child.yaml", routes=[RouteDef(to="$end")])
        )
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("CONDUCTOR_TEST_TASK10_TOKEN", "x")
            with pytest.raises(
                ConfigurationError,
                match=r"sub-workflow 'child.yaml' failed validation[\s\S]*"
                r"not defined in environment 'test-env'",
            ):
                _validate_explicit(config, environment, tmp_path)

    def test_explicit_mode_never_discovers(self, tmp_path: Path) -> None:
        # Requirement: an explicit environment disables ambient discovery entirely.
        config = _script_config(_secret("alpha", "script", env="TOKEN"))
        with (
            patch("conductor.config.environment.discover_all_environments") as discover,
            pytest.MonkeyPatch.context() as monkeypatch,
        ):
            monkeypatch.setenv("CONDUCTOR_TEST_TASK10_ALPHA", "x")
            monkeypatch.setenv("CONDUCTOR_TEST_TASK10_BETA", "x")
            _validate_explicit(config, _alpha_beta_environment(), tmp_path)
        discover.assert_not_called()


class TestAmbientDiscovery:
    """Ambient (no --environment) three-level cross-check against authored envs."""

    def test_no_authored_environments_warns_and_never_errors(self, tmp_path: Path) -> None:
        # Requirement (Metis RISK-4): with zero authored environments the
        # built-in ``local/default`` never counts, so the check degrades to a
        # single warning pointing at ``--environment``.
        config = _script_config(_secret("alpha", "script", env="TOKEN"))
        with patch("conductor.config.environment.discover_all_environments", return_value={}):
            warnings = validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")
        assert any(
            "could not be checked against any authored environment" in w
            and "conductor validate --environment" in w
            for w in warnings
        )

    def test_builtin_source_environment_never_counts_as_authored(self, tmp_path: Path) -> None:
        # Requirement (Metis RISK-4): even if discovery ever surfaced a
        # builtin-sourced document, it must not count toward the ambient check.
        builtin = ResolvedEnvironment(
            document=EnvironmentDocument(
                default="default",
                profiles={"default": ProfileDefinition(backend="local")},
            ),
            name="local/default",
            source="builtin",
            path=None,
            digest="sha256:builtin",
        )
        config = _script_config(_secret("alpha", "script", env="TOKEN"))
        with patch(
            "conductor.config.environment.discover_all_environments",
            return_value={"local/default": builtin},
        ):
            warnings = validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")
        assert any("could not be checked against any authored environment" in w for w in warnings)
        assert not any("not defined in any discovered" in w for w in warnings)

    def test_ref_unknown_in_all_authored_environments_is_an_error(self, tmp_path: Path) -> None:
        # Requirement: a ref unknown to every authored environment is an error
        # naming all of them.
        environments = {
            "one": _environment({"token": _binding("CONDUCTOR_TEST_TASK10_ONE")}, name="one"),
            "two": _environment({"other": _binding("CONDUCTOR_TEST_TASK10_TWO")}, name="two"),
        }
        config = _script_config(_secret("alpha", "script", env="TOKEN"))
        with (
            patch(
                "conductor.config.environment.discover_all_environments",
                return_value=environments,
            ),
            pytest.raises(
                ConfigurationError, match=r"not defined in any discovered authored environment"
            ) as exc_info,
        ):
            validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")
        assert "one" in str(exc_info.value) and "two" in str(exc_info.value)

    def test_ref_missing_from_some_environments_warns(self, tmp_path: Path) -> None:
        # Requirement: a ref known in only some authored environments warns,
        # naming just the ones missing it.
        environments = {
            "has-alpha": _environment(
                {"alpha": _binding("CONDUCTOR_TEST_TASK10_ALPHA")}, name="has-alpha"
            ),
            "missing-alpha": _environment(
                {"beta": _binding("CONDUCTOR_TEST_TASK10_BETA")}, name="missing-alpha"
            ),
        }
        config = _script_config(_secret("alpha", "script", env="TOKEN"))
        with patch(
            "conductor.config.environment.discover_all_environments",
            return_value=environments,
        ):
            warnings = validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")
        absent = [w for w in warnings if "absent from environment(s)" in w]
        assert len(absent) == 1
        assert "missing-alpha" in absent[0]
        assert "has-alpha" not in absent[0]

    def test_ref_known_in_all_environments_is_clean(self, tmp_path: Path) -> None:
        # Requirement: a ref present in every authored environment is silent.
        environments = {
            "one": _environment({"alpha": _binding("CONDUCTOR_TEST_TASK10_ONE")}, name="one"),
            "two": _environment({"alpha": _binding("CONDUCTOR_TEST_TASK10_TWO")}, name="two"),
        }
        config = _script_config(_secret("alpha", "script", env="TOKEN"))
        with patch(
            "conductor.config.environment.discover_all_environments",
            return_value=environments,
        ):
            warnings = validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")
        assert warnings == []

    def test_ambient_mode_never_checks_source_presence(self, tmp_path: Path) -> None:
        # Requirement: unset-source warnings fire only under --environment;
        # ambient validation never reads ``os.environ`` for secret sources.
        environments = {
            "one": _environment({"alpha": _binding("CONDUCTOR_TEST_TASK10_UNSET")}, name="one"),
        }
        config = _script_config(_secret("alpha", "script", env="TOKEN"))
        with (
            patch(
                "conductor.config.environment.discover_all_environments",
                return_value=environments,
            ),
            pytest.MonkeyPatch.context() as monkeypatch,
        ):
            monkeypatch.delenv("CONDUCTOR_TEST_TASK10_UNSET", raising=False)
            warnings = validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")
        assert not any("unset" in w for w in warnings)

    def test_malformed_environment_counts_as_missing(self, tmp_path: Path) -> None:
        # Requirement: a malformed discovered document (``None`` entry) counts as
        # not providing the ref, mirroring the profile cross-check.
        environments = {
            "good": _environment({"alpha": _binding("CONDUCTOR_TEST_TASK10_ONE")}, name="good"),
            "broken": None,
        }
        config = _script_config(_secret("alpha", "script", env="TOKEN"))
        with patch(
            "conductor.config.environment.discover_all_environments",
            return_value=environments,
        ):
            warnings = validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")
        absent = [w for w in warnings if "absent from environment(s)" in w]
        assert len(absent) == 1
        assert "broken" in absent[0]

    def test_no_secret_refs_never_discovers(self, tmp_path: Path) -> None:
        # Requirement: a secret-free workflow pays zero secret validation and
        # zero environment discovery I/O.
        config = WorkflowConfig(
            workflow=WorkflowDef(name="plain", entry_point="run"),
            agents=[ScriptStepDef(name="run", command="echo", routes=[RouteDef(to="$end")])],
            output={"result": "{{ run.output.stdout }}"},
        )
        with patch("conductor.config.environment.discover_all_environments") as discover:
            warnings = validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")
        discover.assert_not_called()
        assert warnings == []

    def test_secret_validator_is_not_called_without_references(self, tmp_path: Path) -> None:
        # Requirement: the lazy gate in ``validate_workflow_config`` skips the
        # secret validator entirely for secret-free workflows.
        config = WorkflowConfig(
            workflow=WorkflowDef(name="plain", entry_point="run"),
            agents=[ScriptStepDef(name="run", command="echo", routes=[RouteDef(to="$end")])],
            output={"result": "{{ run.output.stdout }}"},
        )
        with patch("conductor.config.validator._validate_secret_references") as validator:
            validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")
        validator.assert_not_called()

    def test_profile_and_secret_refs_share_one_discovery(self, tmp_path: Path) -> None:
        # Requirement: profile and secret cross-checks share the context's single
        # lazy discovery scan.
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="both",
                entry_point="run",
                defaults=WorkflowDefaults(execution=StepExecutionConfig(profile="shell")),
            ),
            agents=[
                ScriptStepDef(
                    name="run",
                    command="echo",
                    execution=StepExecutionConfig(secrets=[_secret("alpha", "script", env="T")]),
                    routes=[RouteDef(to="$end")],
                )
            ],
            output={"result": "{{ run.output.stdout }}"},
        )
        with patch(
            "conductor.config.environment.discover_all_environments", return_value={}
        ) as discover:
            warnings = validate_workflow_config(config, workflow_path=tmp_path / "workflow.yaml")
        discover.assert_called_once_with(tmp_path, on_warning=ANY)
        assert any("profile references could not be checked" in w for w in warnings)
        assert any("secret references could not be checked" in w for w in warnings)
