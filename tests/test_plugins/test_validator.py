"""Tests for ``conductor validate``'s plugin cross-checks.

Everything here is a failure the author can only be told about *before*
the run — by the time an agent is mid-flight, a dropped subagent or an
unfiltered hook is already the silent divergence issue #378 describes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conductor.config.schema import (
    AgentDef,
    MCPServerDef,
    OutputField,
    PluginDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError

from .conftest import make_plugin


def _config(
    *,
    provider: str = "copilot",
    plugins: list[PluginDef] | None = None,
    agent_plugins: list[PluginDef] | None = None,
    mcp_servers: dict[str, MCPServerDef] | None = None,
) -> WorkflowConfig:
    runtime: dict[str, Any] = {"provider": provider, "plugins": plugins or []}
    if mcp_servers:
        runtime["mcp_servers"] = mcp_servers
    return WorkflowConfig(
        workflow=WorkflowDef(name="wf", entry_point="worker", runtime=RuntimeConfig(**runtime)),
        agents=[
            AgentDef(
                name="worker",
                prompt="Do it.",
                output={"result": OutputField(type="string")},
                plugins=agent_plugins,
            )
        ],
        output={"result": "{{ worker.output.result }}"},
    )


def _wf_path(tmp_path: Path) -> Path:
    path = tmp_path / "wf.yaml"
    path.write_text("# placeholder\n")
    return path


def _validate(config: WorkflowConfig, path: Path | None) -> list[str]:
    return validate_workflow_config(config, workflow_path=path)


class TestProviderSupport:
    @pytest.mark.parametrize("provider", ["claude", "hermes"])
    def test_plugins_rejected_on_providers_that_cannot_load_them(
        self, tmp_path: Path, provider: str
    ) -> None:
        make_plugin(tmp_path / "p", "p", skills=["s"])
        config = _config(provider=provider, plugins=[PluginDef(name="./p")])
        with pytest.raises(ConfigurationError, match="cannot load them"):
            _validate(config, _wf_path(tmp_path))

    def test_plugins_rejected_on_aca(self, tmp_path: Path) -> None:
        # `aca` needs a pool endpoint to construct, so it is exercised via a
        # per-agent provider override rather than the runtime default.
        make_plugin(tmp_path / "p", "p", skills=["s"])
        config = _config(plugins=[PluginDef(name="./p")])
        config.agents[0].provider = "aca"
        with pytest.raises(ConfigurationError, match="cannot load them"):
            _validate(config, _wf_path(tmp_path))

    @pytest.mark.parametrize("provider", ["copilot", "claude-agent-sdk"])
    def test_plugins_accepted_on_native_providers(self, tmp_path: Path, provider: str) -> None:
        make_plugin(tmp_path / "p", "p", skills=["s"], agents=["helper"])
        config = _config(provider=provider, plugins=[PluginDef(name="./p")])
        _validate(config, _wf_path(tmp_path))

    def test_opt_out_is_not_an_error_on_an_unsupported_provider(self, tmp_path: Path) -> None:
        config = _config(provider="claude", agent_plugins=[])
        _validate(config, _wf_path(tmp_path))


class TestResolutionFailures:
    def test_missing_plugin_is_an_error(self, tmp_path: Path) -> None:
        config = _config(plugins=[PluginDef(name="./nope")])
        with pytest.raises(ConfigurationError, match="does not exist"):
            _validate(config, _wf_path(tmp_path))

    def test_uninstalled_name_names_the_search_locations(self, tmp_path: Path) -> None:
        config = _config(plugins=[PluginDef(name="definitely-not-installed-xyz")])
        with pytest.raises(ConfigurationError, match="Looked in"):
            _validate(config, _wf_path(tmp_path))

    def test_directory_that_is_not_a_plugin(self, tmp_path: Path) -> None:
        (tmp_path / "plain").mkdir()
        config = _config(plugins=[PluginDef(name="./plain")])
        with pytest.raises(ConfigurationError, match="is not a plugin"):
            _validate(config, _wf_path(tmp_path))

    def test_relative_path_without_a_workflow_path_warns(self, tmp_path: Path) -> None:
        # No base directory to resolve against; still resolved at run time.
        config = _config(plugins=[PluginDef(name="./p")])
        warnings = _validate(config, None)
        assert any("not checked because no workflow file path" in w for w in warnings)


class TestClaudeAgentSdkCarveOut:
    """``agents: false`` is unreachable there when skills are enabled."""

    def test_agents_false_with_skills_is_refused(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", skills=["s"], agents=["helper"])
        config = _config(
            provider="claude-agent-sdk",
            plugins=[PluginDef(name="./p", agents=False)],
        )
        with pytest.raises(ConfigurationError, match="cannot honour"):
            _validate(config, _wf_path(tmp_path))

    def test_agents_false_without_skills_is_fine(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", skills=["s"], agents=["helper"])
        config = _config(
            provider="claude-agent-sdk",
            plugins=[PluginDef(name="./p", skills=False, agents=False)],
        )
        _validate(config, _wf_path(tmp_path))

    def test_copilot_honours_the_same_combination(self, tmp_path: Path) -> None:
        # The identical plugin config works on copilot, which registers
        # each component individually.
        make_plugin(tmp_path / "p", "p", skills=["s"], agents=["helper"])
        config = _config(provider="copilot", plugins=[PluginDef(name="./p", agents=False)])
        _validate(config, _wf_path(tmp_path))


class TestDroppedComponents:
    def test_hooks_are_reported(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", skills=["s"], hooks=True)
        config = _config(plugins=[PluginDef(name="./p")])
        warnings = _validate(config, _wf_path(tmp_path))
        assert any("hooks/" in w and "does not load" in w for w in warnings)

    def test_commands_are_reported(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", skills=["s"], commands=True)
        config = _config(plugins=[PluginDef(name="./p")])
        warnings = _validate(config, _wf_path(tmp_path))
        assert any("commands/" in w for w in warnings)

    def test_claude_agent_sdk_says_hooks_are_exposed_not_dropped(self, tmp_path: Path) -> None:
        # Registering the plugin root is the only way to reach its skills
        # there, and the root carries hooks with it — so "not loaded"
        # would be false.
        make_plugin(tmp_path / "p", "p", skills=["s"], hooks=True)
        config = _config(provider="claude-agent-sdk", plugins=[PluginDef(name="./p")])
        warnings = _validate(config, _wf_path(tmp_path))
        assert any("exposed to the CLI" in w for w in warnings)

    def test_nothing_reported_for_a_clean_plugin(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", skills=["s"])
        config = _config(plugins=[PluginDef(name="./p")])
        warnings = _validate(config, _wf_path(tmp_path))
        assert not any("does not load" in w for w in warnings)


class TestMcpNameCollisions:
    def test_collision_with_a_workflow_server_is_refused(self, tmp_path: Path) -> None:
        # The server name prefixes the tool names the model sees.
        make_plugin(tmp_path / "p", "p", mcp={"shared": {"type": "stdio", "command": "npx"}})
        config = _config(
            plugins=[PluginDef(name="./p")],
            mcp_servers={"shared": MCPServerDef(command="other")},
        )
        with pytest.raises(ConfigurationError, match="which the workflow also"):
            _validate(config, _wf_path(tmp_path))

    def test_disabling_plugin_mcp_avoids_the_collision(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", mcp={"shared": {"type": "stdio", "command": "npx"}})
        config = _config(
            plugins=[PluginDef(name="./p", mcp=False)],
            mcp_servers={"shared": MCPServerDef(command="other")},
        )
        _validate(config, _wf_path(tmp_path))

    def test_distinct_names_are_fine(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", mcp={"plugin-srv": {"type": "stdio", "command": "npx"}})
        config = _config(
            plugins=[PluginDef(name="./p")],
            mcp_servers={"workflow-srv": MCPServerDef(command="other")},
        )
        _validate(config, _wf_path(tmp_path))
