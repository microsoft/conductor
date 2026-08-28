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
    RouteDef,
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


class TestBrokenPluginContentSurfacesAsValidationError:
    """A malformed plugin must not escape the CLI as a raw traceback.

    ``_check_agent_plugins`` catches ``(PluginError, SkillError)`` — two
    unrelated hierarchies, since a plugin's skills are resolved by the
    skills layer. Narrowing that to ``PluginError`` alone leaves 1526
    tests green and turns a broken ``SKILL.md`` into an unhandled crash.
    """

    def test_broken_skill_frontmatter_is_reported(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "p", "p")
        bad = root / "skills" / "bad"
        bad.mkdir(parents=True)
        (bad / "SKILL.md").write_text("---\nname: bad\ndescription: Oops. Triggers: a, b\n---\n")
        config = _config(plugins=[PluginDef(name="./p")])
        with pytest.raises(ConfigurationError, match="invalid YAML frontmatter"):
            _validate(config, _wf_path(tmp_path))

    def test_broken_agent_definition_is_reported(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "p", "p")
        (root / "agents").mkdir()
        (root / "agents" / "broken.agent.md").write_text("no frontmatter at all\n")
        config = _config(plugins=[PluginDef(name="./p")])
        with pytest.raises(ConfigurationError, match="no YAML frontmatter"):
            _validate(config, _wf_path(tmp_path))


class TestOneBadPathDoesNotSilenceOtherChecks:
    """An un-anchorable relative entry must not suppress the rest."""

    def test_mcp_clash_still_reported_without_a_workflow_path(self, tmp_path: Path) -> None:
        absolute = make_plugin(tmp_path / "abs", "abs", mcp={"shared": {"command": "npx"}})
        config = _config(
            plugins=[PluginDef(name="./unanchorable"), PluginDef(name=str(absolute))],
            mcp_servers={"shared": MCPServerDef(command="other")},
        )
        with pytest.raises(ConfigurationError, match="which the workflow also"):
            _validate(config, None)


def _source_config(
    sources: dict[str, Any],
    plugins: list[str],
    *,
    provider: str = "copilot",
) -> WorkflowConfig:
    """A workflow declaring ``plugin_sources`` alongside ``plugins``."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="wf",
            entry_point="worker",
            runtime=RuntimeConfig(provider=provider, plugin_sources=sources, plugins=plugins),
        ),
        agents=[
            AgentDef(
                name="worker",
                prompt="Do it.",
                output={"result": OutputField(type="string")},
            )
        ],
        output={"result": "{{ worker.output.result }}"},
    )


class TestDeclaredSources:
    """``runtime.plugin_sources`` cross-checks, all of them offline.

    ``conductor validate`` must never clone, so every case here resolves
    from cache or not at all.
    """

    @pytest.fixture(autouse=True)
    def _isolated_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep these cases off the developer's real home directory.

        Source resolution reaches ``Path.home()`` twice without going
        through a fixture — ``fetch.get_plugin_cache_base`` when
        ``CONDUCTOR_HOME`` is unset, and ``resolve_plugins(home=None)``
        for installed-marketplace lookup. Without this, a marketplace
        the developer happens to have installed, or a checkout left in
        their plugin cache, decides whether these assertions hold.
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("CONDUCTOR_HOME", str(home / ".conductor"))

    def test_a_local_source_resolves(self, tmp_path: Path) -> None:
        from .conftest import make_marketplace

        make_plugin(tmp_path / "catalog" / "prs", "prs", skills=["review"])
        make_marketplace(tmp_path / "catalog", "acme", {"prs": "./prs"})
        config = _source_config({"acme": "./catalog"}, ["prs@acme"])

        assert _validate(config, _wf_path(tmp_path)) == []

    def test_an_unfetched_source_warns_rather_than_failing(self, tmp_path: Path) -> None:
        """A freshly cloned repository must not fail validation for a
        workflow that runs perfectly — ``conductor run`` fetches it."""
        config = _source_config({"acme": "acme/plugins#v1.0.0"}, ["prs@acme"])

        warnings = _validate(config, _wf_path(tmp_path))

        assert any("conductor plugin fetch" in warning for warning in warnings)

    def test_the_warning_says_which_checks_were_skipped(self, tmp_path: Path) -> None:
        """Reporting "valid" when whole categories of check never ran would
        be the worse lie."""
        config = _source_config({"acme": "acme/plugins"}, ["prs@acme"])

        warnings = _validate(config, _wf_path(tmp_path))

        assert any("were not checked here" in warning for warning in warnings)

    def test_an_undeclared_marketplace_is_an_error(self, tmp_path: Path) -> None:
        config = _source_config({}, ["prs@nowhere"])

        with pytest.raises(ConfigurationError, match="neither declared"):
            _validate(config, _wf_path(tmp_path))

    def test_an_unreferenced_source_is_reported_as_dead_config(self, tmp_path: Path) -> None:
        from .conftest import make_marketplace

        make_plugin(tmp_path / "catalog" / "prs", "prs")
        make_marketplace(tmp_path / "catalog", "acme", {"prs": "./prs"})
        make_plugin(tmp_path / "spare" / "thing", "thing")
        make_marketplace(tmp_path / "spare", "unused", {"thing": "./thing"})
        config = _source_config({"acme": "./catalog", "unused": "./spare"}, ["prs@acme"])

        warnings = _validate(config, _wf_path(tmp_path))

        assert any(
            "'unused'" in warning and "no 'plugins:' entry" in warning for warning in warnings
        )
        # Both sources resolve, so the dead-config notice must be the *only*
        # thing reported. Asserting a single `any(...)` let this pass while
        # the same call misreported a healthy sibling as unacquired.
        assert len(warnings) == 1, warnings

    def test_a_source_referenced_only_by_one_agent_is_not_dead(self, tmp_path: Path) -> None:
        from .conftest import make_marketplace

        make_plugin(tmp_path / "catalog" / "prs", "prs")
        make_marketplace(tmp_path / "catalog", "acme", {"prs": "./prs"})
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="wf",
                entry_point="worker",
                runtime=RuntimeConfig(provider="copilot", plugin_sources={"acme": "./catalog"}),
            ),
            agents=[
                AgentDef(
                    name="worker",
                    prompt="Do it.",
                    output={"result": OutputField(type="string")},
                    plugins=[PluginDef(name="prs@acme")],
                )
            ],
            output={"result": "{{ worker.output.result }}"},
        )

        warnings = _validate(config, _wf_path(tmp_path))

        assert not any("no 'plugins:' entry" in warning for warning in warnings)

    def test_provider_rejection_still_applies_to_sourced_plugins(self, tmp_path: Path) -> None:
        """A git source does not change which providers can load a plugin."""
        from .conftest import make_marketplace

        make_plugin(tmp_path / "catalog" / "prs", "prs")
        make_marketplace(tmp_path / "catalog", "acme", {"prs": "./prs"})
        config = _source_config({"acme": "./catalog"}, ["prs@acme"], provider="claude")

        with pytest.raises(ConfigurationError, match="cannot load them"):
            _validate(config, _wf_path(tmp_path))


class TestPartialSourceResolution:
    """One unusable source must not misreport its healthy neighbours.

    Resolving the declared sources as a batch meant a single failure
    discarded the whole table, so a local directory sitting on disk was
    reported as "has not been acquired" and the user was sent to a
    command that could never help it. It also emptied the table for the
    per-agent checks, silently skipping the MCP-clash and
    dropped-component reporting that this feature exists to provide.
    """

    @pytest.fixture(autouse=True)
    def _isolated_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("CONDUCTOR_HOME", str(home / ".conductor"))

    def test_a_resolvable_source_is_not_blamed_for_an_unfetched_sibling(
        self, tmp_path: Path
    ) -> None:
        from .conftest import make_marketplace

        make_plugin(tmp_path / "catalog" / "mine", "mine")
        make_marketplace(tmp_path / "catalog", "local", {"mine": "./mine"})
        config = _source_config(
            {"local": "./catalog", "acme": "acme/never-fetched#v1.0.0"},
            ["mine@local", "prs@acme"],
        )

        warnings = _validate(config, _wf_path(tmp_path))

        assert not any("mine@local" in warning for warning in warnings)
        assert any("prs@acme" in warning for warning in warnings)

    def test_a_source_path_that_does_not_exist_is_an_error(self, tmp_path: Path) -> None:
        """Not a warning: no amount of fetching fixes a wrong path, and
        reporting it as one blamed the network and prescribed a command
        that fails on the same input."""
        config = _source_config({"acme": "./nope"}, ["prs@acme"])

        with pytest.raises(ConfigurationError, match="is unusable"):
            _validate(config, _wf_path(tmp_path))

    def test_a_path_escaping_the_checkout_is_an_error(self, tmp_path: Path) -> None:
        """A traversal attempt must not read as an advisory note."""
        make_plugin(tmp_path / "vendor", "thing")
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="wf",
                entry_point="worker",
                runtime=RuntimeConfig(
                    provider="copilot",
                    plugin_sources={"acme": {"source": "./vendor", "path": "../../etc"}},
                    plugins=["prs@acme"],
                ),
            ),
            agents=[
                AgentDef(
                    name="worker",
                    prompt="Do it.",
                    output={"result": OutputField(type="string")},
                )
            ],
            output={"result": "{{ worker.output.result }}"},
        )

        with pytest.raises(ConfigurationError, match="escapes the source directory"):
            _validate(config, _wf_path(tmp_path))

    def test_a_broken_source_does_not_also_advise_fetching(self, tmp_path: Path) -> None:
        """The hard error already names the real cause; a second line
        saying 'run conductor plugin fetch' would be the wrong remedy."""
        config = _source_config({"acme": "./nope"}, ["prs@acme"])

        with pytest.raises(ConfigurationError) as excinfo:
            _validate(config, _wf_path(tmp_path))

        assert "conductor plugin fetch" not in str(excinfo.value)


class TestPluginFlavorCacheKey:
    """Issue #497: the plugin-resolution cache must be keyed by flavor too.

    Two agents naming the same entry list on different providers can
    resolve to different builds (a dual-catalog marketplace). Sharing one
    cache slot between them — keyed only by the entries — served the
    first agent's provider's resolution to the second, silently.
    """

    def _dual_build_config(self, tmp_path: Path) -> WorkflowConfig:
        from .conftest import make_marketplace

        catalog = tmp_path / "catalog"
        make_plugin(
            catalog / "dist" / "claude" / "prs",
            "prs",
            manifest=".claude-plugin",
            mcp={"claude-only": {"command": "npx"}},
        )
        make_plugin(
            catalog / "dist" / "copilot" / "prs",
            "prs",
            manifest=".github/plugin",
            mcp={"copilot-only": {"command": "npx"}},
        )
        make_marketplace(
            catalog,
            "acme",
            {"prs": "./dist/claude/prs"},
            manifest=".claude-plugin",
            plugin_root="./dist/claude",
        )
        make_marketplace(
            catalog,
            "acme",
            {"prs": "./prs"},
            manifest=".github/plugin",
            plugin_root="./dist/copilot",
        )

        return WorkflowConfig(
            workflow=WorkflowDef(
                name="wf",
                entry_point="copilot_agent",
                runtime=RuntimeConfig(
                    provider="copilot",
                    plugin_sources={"acme": str(catalog)},
                    # Only the Claude build's own server name collides —
                    # if the cache incorrectly served the Claude agent the
                    # Copilot-flavored resolution (or vice versa), this
                    # clash would be reported for the wrong agent, or not
                    # reported at all.
                    mcp_servers={"claude-only": {"command": "npx"}},
                ),
            ),
            agents=[
                AgentDef(
                    name="copilot_agent",
                    provider="copilot",
                    prompt="Do it.",
                    output={"result": OutputField(type="string")},
                    plugins=[PluginDef(name="prs@acme")],
                    routes=[RouteDef(to="claude_agent")],
                ),
                AgentDef(
                    name="claude_agent",
                    provider="claude-agent-sdk",
                    prompt="Do it too.",
                    output={"result": OutputField(type="string")},
                    plugins=[PluginDef(name="prs@acme")],
                ),
            ],
            output={"result": "{{ claude_agent.output.result }}"},
        )

    def test_agents_on_different_providers_resolve_different_builds(self, tmp_path: Path) -> None:
        config = self._dual_build_config(tmp_path)

        with pytest.raises(ConfigurationError) as excinfo:
            _validate(config, _wf_path(tmp_path))

        # Only the agent that actually resolved the Claude build (whose
        # plugin declares the colliding "claude-only" server) is named.
        message = str(excinfo.value)
        assert "'claude_agent'" in message
        assert "'copilot_agent'" not in message
        assert "claude-only" in message
