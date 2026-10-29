"""The static gate must reject requests the real runner cannot deliver."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from conductor.aca_runner.server import create_app
from conductor.config.schema import AgentDef, MCPServerDef, PluginDef
from conductor.config.validator import validate_workflow_config
from conductor.engine.run_manifest import compile_run_manifest
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ConfigurationError
from tests.test_engine.test_agent_realm_validation import _config, _environment, _validate
from tests.test_plugins.conftest import make_plugin


@pytest.mark.asyncio
async def test_agent_secret_without_stdio_mcp_is_rejected_before_runner_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: an undeliverable remote overlay fails at both static gates, not HTTP 400.
    monkeypatch.setenv("CONDUCTOR_REALM_TEST_TOKEN", "test-only-secret")
    monkeypatch.delenv("ACA_RUNNER_AUTH_TOKEN", raising=False)
    config, environment = _config(with_stdio_mcp=False), _environment("aca")
    transport = httpx.ASGITransport(app=create_app())
    with patch.object(
        transport, "handle_async_request", wraps=transport.handle_async_request
    ) as wire:
        warnings = validate_workflow_config(config)
        assert any("--environment" in warning for warning in warnings)
        expected = (
            "Agent step 'worker' requests agent-scope delivery.env, but no stdio MCP server "
            "can receive env_overlay: it reaches only the spawn-env of stdio MCP processes "
            "in the remote realm, never the model SDK environment. Declare a stdio MCP server "
            "in workflow.runtime.mcp_servers or enable an MCP-shipping plugin for this agent "
            "(and enable its tools), or remove the agent-scope secret."
        )
        with pytest.raises(ConfigurationError) as validated:
            _validate(config, environment)
        assert str(validated.value) == (
            "Workflow configuration validation failed:\n  - "
            f"{expected}\n\n💡 Suggestion: Fix the validation errors listed above and try again."
        )
        with pytest.raises(ConfigurationError) as compiled:
            compile_run_manifest(config, workflow_path=None, environment=environment)
        assert str(compiled.value) == expected
        with pytest.raises(ConfigurationError) as dispatched:
            WorkflowEngine(config, execution_environment=environment)
        assert str(dispatched.value) == expected
        assert wire.call_count == 0

        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            response = await client.post(
                "/execute",
                json={
                    "agent": {"name": "worker"},
                    "rendered_prompt": "Answer",
                    "env_overlay": {"MCP_TOKEN": "test-only-secret"},
                },
            )
        assert wire.call_count == 1
        assert response.status_code == 400
        assert response.json() == {
            "error": {
                "message": (
                    "runner: env_overlay requires at least one stdio MCP server in this request"
                )
            }
        }


@pytest.mark.parametrize("case", ["http_only", "tools_disabled"])
def test_agent_secret_requires_a_consumable_stdio_server(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: HTTP MCP and explicit tools opt-out cannot consume a stdio overlay.
    monkeypatch.setenv("CONDUCTOR_REALM_TEST_TOKEN", "test-only-secret")
    config, environment = _config(), _environment("docker")
    if case == "http_only":
        config.workflow.runtime.mcp_servers = {
            "remote": MCPServerDef(type="http", url="https://example.test/mcp")
        }
    else:
        agent = config.agents[0]
        assert isinstance(agent, AgentDef)
        agent.tools = []
    with pytest.raises(ConfigurationError, match="no stdio MCP server can receive env_overlay"):
        _validate(config, environment)
    with pytest.raises(ConfigurationError, match="no stdio MCP server can receive env_overlay"):
        compile_run_manifest(config, workflow_path=None, environment=environment)


@pytest.mark.parametrize(
    ("placement", "mcp_enabled", "allowed"),
    [
        ("workflow", True, True),
        ("agent", True, True),
        ("workflow", False, False),
        ("agent", False, False),
        ("agent_opt_out", True, False),
    ],
)
def test_declared_plugin_mcp_is_a_possible_consumer(
    placement: str,
    mcp_enabled: bool,
    allowed: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: an enabled plugin might supply stdio MCP without static plugin resolution.
    monkeypatch.setenv("CONDUCTOR_REALM_TEST_TOKEN", "test-only-secret")
    root = make_plugin(tmp_path / "prs", "prs", mcp={"srv": {"type": "stdio", "command": "echo"}})
    config, environment = _config(with_stdio_mcp=False), _environment("docker")
    plugin = PluginDef(name=str(root), mcp=mcp_enabled)
    agent = config.agents[0]
    assert isinstance(agent, AgentDef)
    if placement == "workflow":
        config.workflow.runtime.plugins = [plugin]
    elif placement == "agent":
        agent.plugins = [plugin]
    else:
        config.workflow.runtime.plugins = [plugin]
        agent.plugins = []
    if allowed:
        assert _validate(config, environment) == []
        assert compile_run_manifest(config, workflow_path=None, environment=environment)
    else:
        with pytest.raises(ConfigurationError, match="no stdio MCP server can receive env_overlay"):
            _validate(config, environment)
        with pytest.raises(ConfigurationError, match="no stdio MCP server can receive env_overlay"):
            compile_run_manifest(config, workflow_path=None, environment=environment)


def test_compiler_does_not_resolve_declared_plugin_contents(tmp_path: Path) -> None:
    # Requirement: a declared MCP-capable plugin is a conservative static consumer.
    config, environment = _config(with_stdio_mcp=False), _environment("docker")
    config.workflow.runtime.plugins = [PluginDef(name=str(tmp_path / "not-fetched"))]
    assert compile_run_manifest(config, workflow_path=None, environment=environment)
