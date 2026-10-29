"""Per-call delivery of agent-scoped secrets declared on MCP servers."""

from __future__ import annotations

import pytest

from conductor.config.environment import ProfileDefinition
from conductor.config.schema import (
    AgentDef,
    MCPServerDef,
    SecretDelivery,
    StepExecutionConfig,
    StepSecretRef,
)
from conductor.engine.run_manifest import compile_run_manifest
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ConfigurationError
from tests.test_engine.test_agent_realm_validation import _config, _environment, _validate


def _server_secret(name: str) -> MCPServerDef:
    return MCPServerDef(
        command="docs-server",
        secrets=[StepSecretRef(ref="token", scope="agent", delivery=SecretDelivery(env=name))],
    )


def test_mcp_agent_scope_requires_all_consumers_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: one local MCP-capable consumer reserves the shared server delivery.
    monkeypatch.setenv("CONDUCTOR_REALM_TEST_TOKEN", "test-only-secret")
    config = _config()
    config.workflow.runtime.mcp_servers["docs"] = _server_secret("MCP_SERVER_TOKEN")
    config.agents.append(
        AgentDef(name="local_reader", prompt="Read", execution=StepExecutionConfig(profile="local"))
    )
    environment = _environment("docker")
    environment.document.profiles["local"] = ProfileDefinition(backend="local")
    with pytest.raises(
        ConfigurationError, match="local agent runtime inherits the control environment"
    ):
        _validate(config, environment)
    with pytest.raises(
        ConfigurationError, match="local agent runtime inherits the control environment"
    ):
        compile_run_manifest(config, workflow_path=None, environment=environment)


def test_mcp_agent_scope_is_delivered_per_call_not_in_server_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: server agent-scope refs join each remote invocation's overlay only.
    monkeypatch.setenv("CONDUCTOR_REALM_TEST_TOKEN", "test-only-secret")
    config = _config()
    config.workflow.runtime.mcp_servers["docs"] = _server_secret("MCP_SERVER_TOKEN")
    environment = _environment("docker")
    assert _validate(config, environment) == []
    engine = WorkflowEngine(config, execution_environment=environment)
    assert engine._execution_resolver.secret_env_for_step("worker") == {
        "MCP_TOKEN": "test-only-secret",
        "MCP_SERVER_TOKEN": "test-only-secret",
    }
    assert engine._execution_resolver.deliveries_for_server("docs") == ()


def test_mcp_agent_scope_rejects_reserved_env_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: server-scoped agent delivery cannot shadow provider credentials either.
    monkeypatch.setenv("CONDUCTOR_REALM_TEST_TOKEN", "test-only-secret")
    config = _config()
    config.workflow.runtime.mcp_servers["docs"] = _server_secret("OPENAI_API_KEY")
    environment = _environment("docker")
    with pytest.raises(ConfigurationError, match="reserved for provider credentials"):
        _validate(config, environment)
    with pytest.raises(ConfigurationError, match="reserved for provider credentials"):
        compile_run_manifest(config, workflow_path=None, environment=environment)


def test_agent_step_and_server_delivery_names_share_one_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: two agent-scoped bindings cannot overwrite one per-call MCP variable.
    monkeypatch.setenv("CONDUCTOR_REALM_TEST_TOKEN", "test-only-secret")
    config = _config()
    config.workflow.runtime.mcp_servers["docs"] = _server_secret("MCP_TOKEN")
    environment = _environment("docker")
    with pytest.raises(ConfigurationError, match="collides within consumer 'worker'"):
        _validate(config, environment)
    with pytest.raises(ConfigurationError, match="collides within consumer 'worker'"):
        compile_run_manifest(config, workflow_path=None, environment=environment)


def test_agent_overlay_cannot_override_literal_stdio_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: delivery.env cannot overwrite a stdio MCP server's literal env entry.
    monkeypatch.setenv("CONDUCTOR_REALM_TEST_TOKEN", "test-only-secret")
    config = _config()
    config.workflow.runtime.mcp_servers["docs"] = MCPServerDef(
        command="docs-server", env={"MCP_TOKEN": "literal"}
    )
    environment = _environment("docker")
    with pytest.raises(ConfigurationError, match="collides within consumer 'worker'"):
        _validate(config, environment)
    with pytest.raises(ConfigurationError, match="collides within consumer 'worker'"):
        compile_run_manifest(config, workflow_path=None, environment=environment)
