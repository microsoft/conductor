"""Tests for resolving ``plugins:`` entries to on-disk plugins.

Resolution is deliberately strict. Unlike skill *discovery*, which is
lenient because the author never named what it found, every plugin entry
was written down — so a missing, ambiguous, or broken plugin is an error
rather than a quiet skip.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conductor.plugins.errors import (
    PluginManifestError,
    PluginNotFoundError,
)
from conductor.plugins.registry import resolve_plugin, resolve_plugins
from conductor.skills.frontmatter import SkillManifestError

from .conftest import make_plugin, write_skill


class Entry:
    """Stand-in for ``PluginDef`` so these tests need no schema import."""

    def __init__(self, name: str, skills: bool = True, agents: bool = True, mcp: bool = True):
        self.name = name
        self.skills = skills
        self.agents = agents
        self.mcp = mcp


class TestNameResolution:
    def test_resolves_an_installed_plugin(self, installed, home: Path) -> None:
        installed("prs", skills=["review"], agents=["code-reviewer"])
        plugin = resolve_plugin("prs", home=home)
        assert plugin.name == "prs"
        assert [s.name for s in plugin.skills] == ["review"]
        assert [a.qualified_name for a in plugin.agents] == ["prs:code-reviewer"]

    def test_claude_location_is_searched_too(self, home: Path) -> None:
        # A workflow must resolve the same plugin whichever provider its
        # agents use, so both CLIs' install roots are searched.
        make_plugin(home / ".claude" / "plugins" / "market" / "tools", "tools", skills=["a"])
        assert resolve_plugin("tools", home=home).name == "tools"

    def test_missing_plugin_names_where_it_looked(self, home: Path) -> None:
        with pytest.raises(PluginNotFoundError, match="is not installed"):
            resolve_plugin("nope", home=home)

    def test_ambiguous_name_is_refused(self, installed, home: Path) -> None:
        # Two marketplaces shipping a `git` plugin are different plugins;
        # picking one silently is how a workflow drifts between machines.
        installed("git", marketplace="alpha", skills=["a"])
        installed("git", marketplace="beta", skills=["b"])
        with pytest.raises(PluginNotFoundError, match="ambiguous"):
            resolve_plugin("git", home=home)

    def test_directory_without_a_manifest_is_not_a_candidate(self, home: Path) -> None:
        stray = home / ".copilot" / "installed-plugins" / "market" / "notaplugin"
        (stray / "skills").mkdir(parents=True)
        with pytest.raises(PluginNotFoundError, match="is not installed"):
            resolve_plugin("notaplugin", home=home)


class TestPathResolution:
    def test_relative_path_resolves_against_base_dir(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "tools" / "mine", "mine", skills=["a"])
        plugin = resolve_plugin("./tools/mine", base_dir=tmp_path)
        assert plugin.name == "mine"

    def test_absolute_path(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "mine", "mine", agents=["helper"])
        assert resolve_plugin(str(root)).name == "mine"

    def test_missing_path(self, tmp_path: Path) -> None:
        with pytest.raises(PluginNotFoundError, match="does not exist"):
            resolve_plugin("./nope", base_dir=tmp_path)

    def test_path_to_a_file(self, tmp_path: Path) -> None:
        (tmp_path / "afile").write_text("x")
        with pytest.raises(PluginNotFoundError, match="not a\n?\\s*directory"):
            resolve_plugin("./afile", base_dir=tmp_path)

    def test_directory_that_is_not_a_plugin(self, tmp_path: Path) -> None:
        write_skill(tmp_path / "plain" / "skills" / "a")
        with pytest.raises(PluginManifestError, match="is not a plugin"):
            resolve_plugin("./plain", base_dir=tmp_path)

    def test_a_bare_name_is_never_treated_as_a_path(self, tmp_path: Path, home: Path) -> None:
        # Classification is syntactic, so a same-named local directory can
        # never shadow an installed plugin name.
        make_plugin(tmp_path / "prs", "prs", skills=["a"])
        with pytest.raises(PluginNotFoundError, match="is not installed"):
            resolve_plugin("prs", base_dir=tmp_path, home=home)


class TestComponentSwitches:
    def test_all_components_load_by_default(self, installed, home: Path) -> None:
        installed(
            "full",
            skills=["s"],
            agents=["a"],
            mcp={"srv": {"type": "stdio", "command": "npx"}},
        )
        plugin = resolve_plugin("full", home=home)
        assert len(plugin.skills) == 1
        assert len(plugin.agents) == 1
        assert list(plugin.mcp_servers) == ["srv"]
        assert plugin.disabled == ()

    @pytest.mark.parametrize(
        ("switch", "attribute"),
        [("want_skills", "skills"), ("want_agents", "agents"), ("want_mcp", "mcp_servers")],
    )
    def test_each_component_can_be_switched_off(
        self, installed, home: Path, switch: str, attribute: str
    ) -> None:
        installed(
            "full",
            skills=["s"],
            agents=["a"],
            mcp={"srv": {"type": "stdio", "command": "npx"}},
        )
        plugin = resolve_plugin("full", home=home, **{switch: False})
        assert not getattr(plugin, attribute)

    def test_disabled_reports_only_what_the_plugin_ships(self, installed, home: Path) -> None:
        # Switching off a component the plugin does not have is not an
        # omission worth reporting.
        installed("skills-only", skills=["s"])
        plugin = resolve_plugin("skills-only", home=home, want_agents=False, want_mcp=False)
        assert plugin.disabled == ()

    def test_disabled_reports_a_real_omission(self, installed, home: Path) -> None:
        installed("full", skills=["s"], agents=["a"], mcp={"srv": {"command": "npx"}})
        plugin = resolve_plugin("full", home=home, want_mcp=False)
        assert plugin.disabled == ("mcp",)


class TestDroppedComponents:
    def test_hooks_and_commands_are_reported(self, installed, home: Path) -> None:
        installed("noisy", skills=["s"], hooks=True, commands=True)
        assert resolve_plugin("noisy", home=home).dropped == ("hooks", "commands")

    def test_nothing_dropped_when_absent(self, installed, home: Path) -> None:
        installed("clean", skills=["s"])
        assert resolve_plugin("clean", home=home).dropped == ()


class TestSkillExpansion:
    def test_broken_skill_frontmatter_is_fatal(self, installed, home: Path) -> None:
        # Both CLIs skip an unparseable skill silently, so this is the only
        # place the author finds out.
        root = installed("broken", skills=["ok"])
        bad = root / "skills" / "bad"
        bad.mkdir()
        (bad / "SKILL.md").write_text("---\nname: bad\ndescription: Oops. Triggers: a, b\n---\n")
        with pytest.raises(SkillManifestError):
            resolve_plugin("broken", home=home)

    def test_subdirectory_without_a_skill_md_warns(self, installed, home: Path) -> None:
        root = installed("partial", skills=["good"])
        (root / "skills" / "notaskill").mkdir()
        seen: list[str] = []
        plugin = resolve_plugin("partial", home=home, on_warning=seen.append)
        assert [s.name for s in plugin.skills] == ["good"]
        assert any("notaskill" in message for message in seen)


class TestResolvePlugins:
    def test_resolves_in_order(self, installed, home: Path) -> None:
        installed("one", skills=["a"])
        installed("two", skills=["b"])
        resolved = resolve_plugins([Entry("one"), Entry("two")], home=home)
        assert [p.name for p in resolved] == ["one", "two"]

    def test_same_plugin_by_name_and_path_is_deduplicated(self, installed, home: Path) -> None:
        root = installed("dup", skills=["a"])
        resolved = resolve_plugins([Entry("dup"), Entry(str(root))], home=home)
        assert [p.name for p in resolved] == ["dup"]

    def test_two_plugins_claiming_one_name_are_refused(self, tmp_path: Path, home: Path) -> None:
        # The name namespaces skills and agents, so one would shadow the other.
        a = make_plugin(tmp_path / "a", "same", skills=["x"])
        b = make_plugin(tmp_path / "b", "same", skills=["y"])
        with pytest.raises(PluginNotFoundError, match="both resolve to a plugin named"):
            resolve_plugins([Entry(str(a)), Entry(str(b))], home=home)

    def test_colliding_mcp_server_names_are_refused(self, installed, home: Path) -> None:
        # The server name prefixes the tool names the model sees, so one
        # plugin's tools would appear under the other's configuration.
        installed("alpha", mcp={"shared": {"command": "a"}})
        installed("beta", mcp={"shared": {"command": "b"}})
        with pytest.raises(PluginManifestError, match="both declare an MCP server"):
            resolve_plugins([Entry("alpha"), Entry("beta")], home=home)

    def test_collision_is_avoidable_by_disabling_mcp(self, installed, home: Path) -> None:
        installed("alpha", mcp={"shared": {"command": "a"}})
        installed("beta", mcp={"shared": {"command": "b"}})
        resolved = resolve_plugins([Entry("alpha"), Entry("beta", mcp=False)], home=home)
        assert [p.name for p in resolved] == ["alpha", "beta"]

    def test_empty_list_resolves_to_nothing(self, home: Path) -> None:
        assert resolve_plugins([], home=home) == []


class TestReviewFollowUps:
    """Regressions for issues found in review of the initial implementation."""

    def test_disabled_agents_does_not_parse_agent_definitions(self, tmp_path: Path) -> None:
        # `agents: false` is the documented opt-out, so it must not fail
        # over the very files it opted out of.
        root = make_plugin(tmp_path / "p", "p", skills=["s"])
        (root / "agents").mkdir()
        (root / "agents" / "broken.agent.md").write_text("no frontmatter at all\n")
        plugin = resolve_plugin(str(root), want_agents=False)
        assert plugin.agents == ()
        assert plugin.disabled == ("agents",)

    def test_broken_agent_still_fails_when_agents_are_enabled(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "p", "p", skills=["s"])
        (root / "agents").mkdir()
        (root / "agents" / "broken.agent.md").write_text("no frontmatter at all\n")
        with pytest.raises(PluginManifestError, match="no YAML frontmatter"):
            resolve_plugin(str(root))

    def test_two_plugins_shipping_one_skill_name_are_refused(self, tmp_path: Path) -> None:
        # Skills reach the provider as one flat, name-keyed list, so one
        # would be dropped — the exact failure this feature removes.
        a = make_plugin(tmp_path / "a", "pa", skills=["review"])
        b = make_plugin(tmp_path / "b", "pb", skills=["review"])
        with pytest.raises(PluginManifestError, match="both ship a skill named"):
            resolve_plugins([Entry(str(a)), Entry(str(b))])

    def test_skill_clash_is_avoidable_by_disabling_skills(self, tmp_path: Path) -> None:
        a = make_plugin(tmp_path / "a", "pa", skills=["review"])
        b = make_plugin(tmp_path / "b", "pb", skills=["review"], agents=["helper"])
        resolved = resolve_plugins([Entry(str(a)), Entry(str(b), skills=False)])
        assert [p.name for p in resolved] == ["pa", "pb"]

    def test_github_convention_plugin_resolves_as_a_skill_plugin(self, tmp_path: Path) -> None:
        # The whole point of widening the manifest: a Copilot-convention
        # plugin must resolve through the *skills* path too, or
        # claude-agent-sdk rejects its skills at run time after validate
        # reported them as loaded.
        from conductor.skills import resolve_skill_plugin

        root = make_plugin(tmp_path / "p", "demo", manifest=".github/plugin", skills=["thing"])
        plugin = resolve_skill_plugin(root / "skills" / "thing")
        assert plugin is not None
        assert plugin.qualified_name == "demo:thing"


class TestGithubConventionEndToEnd:
    """The convention 12 of 13 installed plugins use, past the manifest layer.

    ``test_manifest.py`` parametrizes both conventions, but only at parse
    time — this covers a Copilot-convention plugin resolving all three
    components, which is the configuration most users actually hit.
    """

    @pytest.mark.parametrize("manifest", [".claude-plugin", ".github/plugin"])
    def test_all_components_resolve_under_either_convention(
        self, tmp_path: Path, manifest: str
    ) -> None:
        make_plugin(
            tmp_path / "p",
            "demo",
            manifest=manifest,
            skills=["s"],
            agents=["helper"],
            mcp={"srv": {"type": "stdio", "command": "npx"}},
        )
        plugin = resolve_plugin("./p", base_dir=tmp_path)
        assert [s.name for s in plugin.skills] == ["s"]
        assert [a.qualified_name for a in plugin.agents] == ["demo:helper"]
        assert list(plugin.mcp_servers) == ["srv"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
class TestUnreadableTrees:
    """Each of these carries a bespoke message that was previously unverified."""

    def test_unreadable_skill_subdirectory_is_reported(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "p", "p", skills=["ok"])
        blocked = root / "skills" / "blocked"
        blocked.mkdir()
        blocked.chmod(0o000)
        try:
            with pytest.raises(PluginManifestError, match="could not be read"):
                resolve_plugin(str(root))
        finally:
            blocked.chmod(0o755)

    def test_blocked_claude_manifest_falls_through_to_github(self, tmp_path: Path) -> None:
        # The stated premise of probing two conventions: an unreadable
        # candidate must not stop a sibling convention resolving.
        root = make_plugin(tmp_path / "p", "demo", manifest=".github/plugin", skills=["s"])
        blocked = root / ".claude-plugin"
        blocked.mkdir()
        (blocked / "plugin.json").write_text("{}")
        blocked.chmod(0o000)
        try:
            assert resolve_plugin(str(root)).name == "demo"
        finally:
            blocked.chmod(0o755)


class TestConstructorInvariants:
    """A relative root must fail as a plugin error, not leak from the skills layer."""

    def test_relative_root_is_refused(self, tmp_path: Path) -> None:
        from conductor.plugins.registry import ResolvedPlugin

        with pytest.raises(PluginManifestError, match="must be absolute"):
            ResolvedPlugin(name="x", root=Path("relative/root"), source="x")

    def test_delimiter_bearing_agent_name_is_refused(self) -> None:
        from conductor.plugins.agents import PluginAgent

        with pytest.raises(PluginManifestError, match="must match"):
            PluginAgent(
                name="ok",
                plugin_name="bad,name",
                description="d",
                prompt="p",
                tools=None,
                path=Path("/tmp/a.agent.md"),
            )


class TestPathObjectsAreNotSilentlyReclassified:
    """``Path`` has a ``.name``, so duck-typing reduced it to a basename."""

    def test_path_entry_fails_the_protocol(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", skills=["s"])
        with pytest.raises(AttributeError):
            resolve_plugins([tmp_path / "p"], base_dir=tmp_path)  # type: ignore[list-item]


class TestDuplicateRootSwitchMismatch:
    """One plugin reached twice with different switches has no correct merge."""

    def test_conflicting_switches_are_refused(self, tmp_path: Path) -> None:
        # Keeping the first silently would grant the MCP server the second
        # entry declined — an over-grant, in the permissive direction.
        root = make_plugin(tmp_path / "p", "p", mcp={"srv": {"command": "npx"}})
        with pytest.raises(PluginNotFoundError, match="different components"):
            resolve_plugins([Entry(str(root)), Entry(str(root) + "/", mcp=False)])

    def test_identical_switches_are_a_harmless_repeat(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "p", "p", mcp={"srv": {"command": "npx"}})
        resolved = resolve_plugins([Entry(str(root)), Entry(str(root) + "/")])
        assert [p.name for p in resolved] == ["p"]


class TestSkillNameMustMatchItsDirectory:
    """``resolve_skill_plugin`` sends the directory name; the CLI resolves the
    frontmatter name. A divergence hides the skill rather than failing."""

    def test_mismatched_frontmatter_name_is_refused(self, tmp_path: Path) -> None:
        from conductor.skills import SkillPluginError, resolve_skill_plugin

        root = make_plugin(tmp_path / "p", "demo")
        write_skill(root / "skills" / "on-disk", name="in-frontmatter")
        with pytest.raises(SkillPluginError, match="lives in a directory named"):
            resolve_skill_plugin(root / "skills" / "on-disk")
