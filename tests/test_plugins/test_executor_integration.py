"""Tests for the executor → provider seam that plugins ride on.

This is the load-bearing suite for issue #378. A plugin's subagents and
MCP servers have no fallback delivery path — unlike a skill, whose text
the executor can inject into the prompt — so if they do not arrive at
``execute`` they are simply gone, which is precisely the silent
divergence the feature exists to remove. A negative assertion could not
tell a working path from a dropped one, so these capture the positive
side.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from conductor.config.schema import AgentDef, PluginDef
from conductor.exceptions import ExecutionError
from conductor.executor.agent import AgentExecutor
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback
from conductor.providers.capabilities import ProviderCapabilities

from .conftest import make_plugin

_CAPS = ProviderCapabilities(
    tier="stable",
    mcp_tools=True,
    workflow_tools_passthrough=True,
    streaming_events=True,
    agent_reasoning_events=True,
    reasoning_effort=None,
    structured_output="native",
    interrupt=True,
    max_session_seconds=True,
    checkpoint_resume=True,
    usage_tracking=True,
    concurrent_safe=True,
    skills=True,
    plugins=True,
    plugin_flavor="copilot",
)


class _CapturingProvider(AgentProvider, abstract=True):
    """Records everything the executor forwards for plugins."""

    CAPABILITIES = _CAPS

    @property
    def supports_native_skills(self) -> bool:
        return True

    @property
    def supports_native_plugins(self) -> bool:
        return True

    def __init__(self) -> None:
        self.skill_directories: list[str] | None = None
        self.custom_agents: list[dict[str, Any]] | None = None
        self.extra_mcp_servers: dict[str, Any] | None = None
        self.rendered_prompt: str = ""

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
        skill_directories: list[str] | None = None,
        custom_agents: list[dict[str, Any]] | None = None,
        extra_mcp_servers: dict[str, Any] | None = None,
    ) -> AgentOutput:
        self.skill_directories = skill_directories
        self.custom_agents = custom_agents
        self.extra_mcp_servers = extra_mcp_servers
        self.rendered_prompt = rendered_prompt
        return AgentOutput(content={"ok": True}, raw_response="ok")

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class _NoPluginProvider(_CapturingProvider, abstract=True):
    """Declares no plugin support, to exercise the run-time refusal."""

    CAPABILITIES = _CAPS.model_copy(update={"plugins": False})

    @property
    def supports_native_plugins(self) -> bool:
        return False


class _ClaudeFlavorProvider(_CapturingProvider, abstract=True):
    """Declares the Claude build's plugin flavor (issue #497)."""

    CAPABILITIES = _CAPS.model_copy(update={"plugin_flavor": "claude"})


def _agent(**kwargs: Any) -> AgentDef:
    return AgentDef(name="a", model="m", prompt="Hello", **kwargs)


def _run(executor: AgentExecutor, agent: AgentDef) -> None:
    asyncio.run(executor.execute(agent, {}))


class TestComponentsReachTheProvider:
    def test_all_three_components_arrive(self, tmp_path: Path) -> None:
        make_plugin(
            tmp_path / "prs",
            "prs",
            skills=["review"],
            agents=["code-reviewer", "test-analyzer"],
            mcp={"srv": {"type": "stdio", "command": "npx"}},
        )
        provider = _CapturingProvider()
        executor = AgentExecutor(
            provider,
            workflow_dir=tmp_path,
            workflow_plugins=[PluginDef(name="./prs")],
        )
        _run(executor, _agent())

        assert provider.skill_directories == [str(tmp_path / "prs" / "skills" / "review")]
        assert [spec["name"] for spec in provider.custom_agents or []] == [
            "prs:code-reviewer",
            "prs:test-analyzer",
        ]
        assert list(provider.extra_mcp_servers or {}) == ["srv"]

    def test_agents_arrive_in_custom_agent_config_shape(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", agents=["helper"])
        provider = _CapturingProvider()
        executor = AgentExecutor(
            provider, workflow_dir=tmp_path, workflow_plugins=[PluginDef(name="./p")]
        )
        _run(executor, _agent())
        spec = (provider.custom_agents or [])[0]
        assert spec["name"] == "p:helper"
        assert spec["infer"] is True
        assert spec["prompt"]

    def test_plugin_skills_are_not_eager_injected(self, tmp_path: Path) -> None:
        # The provider is native, so the body must not also be prepended.
        make_plugin(tmp_path / "p", "p", skills=["s"])
        provider = _CapturingProvider()
        executor = AgentExecutor(
            provider, workflow_dir=tmp_path, workflow_plugins=[PluginDef(name="./p")]
        )
        _run(executor, _agent())
        # Both halves: the body is not injected AND the directory arrived.
        # The negative alone cannot tell a working native path from one that
        # dropped the skill entirely.
        assert "<skills>" not in provider.rendered_prompt
        assert provider.skill_directories == [str(tmp_path / "p" / "skills" / "s")]

    def test_nothing_is_forwarded_without_plugins(self, tmp_path: Path) -> None:
        provider = _CapturingProvider()
        executor = AgentExecutor(provider, workflow_dir=tmp_path)
        _run(executor, _agent())
        assert provider.custom_agents is None
        assert provider.extra_mcp_servers is None

    def test_disabled_component_does_not_arrive(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", agents=["helper"], mcp={"srv": {"command": "npx"}})
        provider = _CapturingProvider()
        executor = AgentExecutor(
            provider,
            workflow_dir=tmp_path,
            workflow_plugins=[PluginDef(name="./p", mcp=False)],
        )
        _run(executor, _agent())
        assert provider.custom_agents is not None
        assert provider.extra_mcp_servers is None

    def test_claude_built_plugin_subagents_arrive(self, tmp_path: Path) -> None:
        # The regression at the heart of issue #497: a Claude-built
        # plugin's ``agents/*.md`` files (no ``.agent.md`` suffix) were
        # never read because the candidate rule was hardcoded to the
        # Copilot suffix. This passes on this branch and yields an empty
        # ``custom_agents`` on `main` — the missing regression test.
        make_plugin(
            tmp_path / "prs",
            "prs",
            manifest=".claude-plugin",
            agents=["code-reviewer"],
            agent_suffix=".md",
            mcp={"srv": {"type": "stdio", "command": "npx"}},
        )
        provider = _ClaudeFlavorProvider()
        executor = AgentExecutor(
            provider,
            workflow_dir=tmp_path,
            workflow_plugins=[PluginDef(name="./prs")],
        )
        _run(executor, _agent())

        assert [spec["name"] for spec in provider.custom_agents or []] == ["prs:code-reviewer"]
        assert list(provider.extra_mcp_servers or {}) == ["srv"]

    def test_each_provider_receives_its_own_flavor_build(self, tmp_path: Path) -> None:
        # A dual-catalog marketplace: each provider must receive its OWN
        # build's components. This is what actually kills the
        # `flavor = None` mutation on ``executor/agent.py``'s
        # ``_resolve_plugins`` — with flavor dropped, both providers would
        # be served the same (Claude-first) build.
        from conductor.plugins.marketplace import read_marketplace

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
        marketplaces = {"acme": read_marketplace(catalog, name="acme")}

        copilot_provider = _CapturingProvider()
        claude_provider = _ClaudeFlavorProvider()
        copilot_executor = AgentExecutor(
            copilot_provider,
            workflow_dir=tmp_path,
            workflow_plugins=[PluginDef(name="prs@acme")],
            plugin_marketplaces=marketplaces,
        )
        claude_executor = AgentExecutor(
            claude_provider,
            workflow_dir=tmp_path,
            workflow_plugins=[PluginDef(name="prs@acme")],
            plugin_marketplaces=marketplaces,
        )
        _run(copilot_executor, _agent())
        _run(claude_executor, _agent())

        assert list(copilot_provider.extra_mcp_servers or {}) == ["copilot-only"]
        assert list(claude_provider.extra_mcp_servers or {}) == ["claude-only"]


class TestTriStateInheritance:
    def test_agent_inherits_the_workflow_default(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", agents=["helper"])
        provider = _CapturingProvider()
        executor = AgentExecutor(
            provider, workflow_dir=tmp_path, workflow_plugins=[PluginDef(name="./p")]
        )
        _run(executor, _agent())
        assert provider.custom_agents is not None

    def test_empty_list_opts_out(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", agents=["helper"])
        provider = _CapturingProvider()
        executor = AgentExecutor(
            provider, workflow_dir=tmp_path, workflow_plugins=[PluginDef(name="./p")]
        )
        _run(executor, _agent(plugins=[]))
        assert provider.custom_agents is None

    def test_agent_list_overrides_the_workflow_default(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "wide", "wide", agents=["from-workflow"])
        make_plugin(tmp_path / "narrow", "narrow", agents=["from-agent"])
        provider = _CapturingProvider()
        executor = AgentExecutor(
            provider,
            workflow_dir=tmp_path,
            workflow_plugins=[PluginDef(name="./wide")],
        )
        _run(executor, _agent(plugins=[PluginDef(name="./narrow")]))
        assert [spec["name"] for spec in provider.custom_agents or []] == ["narrow:from-agent"]


class TestSkillMerging:
    def test_declared_and_plugin_skills_both_arrive(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", skills=["from-plugin"])
        standalone = tmp_path / "team" / "from-skills"
        standalone.mkdir(parents=True)
        (standalone / "SKILL.md").write_text("---\nname: from-skills\ndescription: d\n---\nBody\n")
        provider = _CapturingProvider()
        executor = AgentExecutor(
            provider,
            workflow_dir=tmp_path,
            workflow_skills=["./team/from-skills"],
            workflow_plugins=[PluginDef(name="./p")],
        )
        _run(executor, _agent())
        assert provider.skill_directories == [
            str(standalone),
            str(tmp_path / "p" / "skills" / "from-plugin"),
        ]

    def test_declared_skill_shadows_a_plugin_skill_of_the_same_name(self, tmp_path: Path) -> None:
        # Both reach the provider through one name-keyed list, so the
        # author's own entry must win. This fires in practice: installing
        # Conductor's plugin puts a second `conductor` skill on the machine.
        make_plugin(tmp_path / "p", "p", skills=["shared"])
        declared = tmp_path / "mine" / "shared"
        declared.mkdir(parents=True)
        (declared / "SKILL.md").write_text("---\nname: shared\ndescription: d\n---\nBody\n")
        provider = _CapturingProvider()
        executor = AgentExecutor(
            provider,
            workflow_dir=tmp_path,
            workflow_skills=["./mine/shared"],
            workflow_plugins=[PluginDef(name="./p")],
        )
        _run(executor, _agent())
        assert provider.skill_directories == [str(declared)]


class TestRunTimeRefusal:
    def test_unsupported_provider_is_refused(self, tmp_path: Path) -> None:
        # `conductor run` never calls the static validator, so the refusal
        # has to hold here too.
        make_plugin(tmp_path / "p", "p", agents=["helper"])
        executor = AgentExecutor(
            provider=_NoPluginProvider(),
            workflow_dir=tmp_path,
            workflow_plugins=[PluginDef(name="./p")],
        )
        with pytest.raises(ExecutionError, match="cannot load plugins"):
            _run(executor, _agent())

    def test_unresolvable_plugin_fails_the_run(self, tmp_path: Path) -> None:
        executor = AgentExecutor(
            provider=_CapturingProvider(),
            workflow_dir=tmp_path,
            workflow_plugins=[PluginDef(name="./missing")],
        )
        with pytest.raises(Exception, match="does not exist"):
            _run(executor, _agent())


class _PluginRootProvider(_CapturingProvider, abstract=True):
    """Reaches skills only by registering the whole plugin root.

    Mirrors ``claude-agent-sdk``, where that registration also contributes
    every subagent the plugin ships and exposes its hooks.
    """

    @property
    def skills_require_plugin_root(self) -> bool:
        return True


class TestReviewFollowUps:
    """Regressions for issues found in review of the initial implementation."""

    def test_unfilterable_agents_refused_at_run_time(self, tmp_path: Path) -> None:
        # `conductor run` never calls the static validator, so a provider
        # that cannot honour `agents: false` must refuse here too —
        # otherwise it silently grants MORE than the workflow declared.
        make_plugin(tmp_path / "p", "p", skills=["s"], agents=["helper"])
        executor = AgentExecutor(
            provider=_PluginRootProvider(),
            workflow_dir=tmp_path,
            workflow_plugins=[PluginDef(name="./p", agents=False)],
        )
        with pytest.raises(ExecutionError, match="cannot honour"):
            _run(executor, _agent())

    def test_agents_false_without_skills_is_allowed_there(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", skills=["s"], agents=["helper"])
        executor = AgentExecutor(
            provider=_PluginRootProvider(),
            workflow_dir=tmp_path,
            workflow_plugins=[PluginDef(name="./p", skills=False, agents=False)],
        )
        _run(executor, _agent())
        # Not merely "does not raise" — both disabled components must be absent.
        assert executor.provider.custom_agents is None  # type: ignore[attr-defined]
        assert executor.provider.skill_directories is None  # type: ignore[attr-defined]

    def test_a_component_registering_provider_honours_the_same_config(self, tmp_path: Path) -> None:
        # The identical config must work on a provider that registers each
        # component individually.
        make_plugin(tmp_path / "p", "p", skills=["s"], agents=["helper"])
        provider = _CapturingProvider()
        executor = AgentExecutor(
            provider,
            workflow_dir=tmp_path,
            workflow_plugins=[PluginDef(name="./p", agents=False)],
        )
        _run(executor, _agent())
        assert provider.custom_agents is None
        assert provider.skill_directories is not None

    def test_shadowed_plugin_skill_is_reported_not_silent(self, tmp_path: Path) -> None:
        # A dropped skill must reach the user. Conductor installs no logging
        # handlers, so a debug log would reach nobody.
        make_plugin(tmp_path / "p", "p", skills=["shared"])
        declared = tmp_path / "mine" / "shared"
        declared.mkdir(parents=True)
        (declared / "SKILL.md").write_text("---\nname: shared\ndescription: d\n---\nBody\n")
        seen: list[str] = []
        executor = AgentExecutor(
            _CapturingProvider(),
            workflow_dir=tmp_path,
            workflow_skills=["./mine/shared"],
            workflow_plugins=[PluginDef(name="./p")],
        )
        with patch(
            "conductor.executor.agent._verbose_log",
            side_effect=lambda m, **k: seen.append(m),
        ):
            _run(executor, _agent())
        assert any("shadowed by the declared skill" in message for message in seen)


class TestCacheKeyDistinguishesComponentSwitches:
    """Two agents naming one plugin with different switches must not share a cache entry."""

    def test_per_agent_switches_are_not_cached_together(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", agents=["helper"], mcp={"srv": {"command": "npx"}})
        provider = _CapturingProvider()
        executor = AgentExecutor(provider, workflow_dir=tmp_path)

        _run(executor, _agent(plugins=[PluginDef(name="./p")]))
        assert list(provider.extra_mcp_servers or {}) == ["srv"]

        # An over-grant would be the failure here: agent B opted out.
        _run(executor, _agent(plugins=[PluginDef(name="./p", mcp=False)]))
        assert provider.extra_mcp_servers is None


class TestNamedPluginBeatsAmbientDiscovery:
    """A plugin the author named outranks a skill that merely happened to be installed."""

    def test_discovered_skill_does_not_shadow_a_plugin_skill(self, tmp_path: Path) -> None:
        from conductor.config.schema import SkillDiscoveryConfig

        make_plugin(tmp_path / "p", "p", skills=["shared"])
        home = tmp_path / "home"
        ambient = home / ".copilot" / "skills" / "shared"
        ambient.mkdir(parents=True)
        (ambient / "SKILL.md").write_text("---\nname: shared\ndescription: d\n---\nBody\n")

        provider = _CapturingProvider()
        seen: list[str] = []
        executor = AgentExecutor(
            provider,
            workflow_dir=tmp_path,
            workflow_plugins=[PluginDef(name="./p")],
            skill_discovery=SkillDiscoveryConfig(sources=["personal"]),
        )
        with (
            patch("pathlib.Path.home", return_value=home),
            patch(
                "conductor.executor.agent._verbose_log",
                side_effect=lambda m, **k: seen.append(m),
            ),
        ):
            _run(executor, _agent())

        assert provider.skill_directories == [str(tmp_path / "p" / "skills" / "shared")]
        assert any("superseded by the copy from plugin" in message for message in seen)
