"""Tests for parsing a plugin's ``agents/*.agent.md`` subagent definitions.

These are the component whose absence opened issue #378: a skill loads,
reads instructions telling it to dispatch to ``prs:code-reviewer``, and
cannot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.plugins.agents import read_plugin_agent, read_plugin_agents
from conductor.plugins.errors import PluginManifestError

from .conftest import make_plugin, write_agent


class TestReadPluginAgent:
    def test_parses_frontmatter_and_body(self, tmp_path: Path) -> None:
        path = write_agent(
            tmp_path / "agents",
            "code-reviewer",
            description="Reviews code.",
            tools=["read", "edit", "ado/*"],
            prompt="You are an expert reviewer.",
        )
        agent = read_plugin_agent(path, "prs")
        assert agent.name == "code-reviewer"
        assert agent.plugin_name == "prs"
        assert agent.description == "Reviews code."
        assert agent.tools == ["read", "edit", "ado/*"]
        assert agent.prompt == "You are an expert reviewer."

    def test_omitted_tools_is_none_not_empty(self, tmp_path: Path) -> None:
        # `None` means "inherit the session default"; `[]` would mean "no
        # tools at all", which is a different and much weaker agent.
        path = write_agent(tmp_path / "agents", "plain")
        assert read_plugin_agent(path, "p").tools is None

    def test_qualified_name_namespaces_by_plugin(self, tmp_path: Path) -> None:
        path = write_agent(tmp_path / "agents", "review")
        assert read_plugin_agent(path, "prs").qualified_name == "prs:review"

    def test_custom_agent_config_shape(self, tmp_path: Path) -> None:
        path = write_agent(tmp_path / "agents", "review", tools=["read"])
        config = read_plugin_agent(path, "prs").to_custom_agent_config()
        assert config == {
            "name": "prs:review",
            "description": "Does a thing.",
            "prompt": "You are a test agent.",
            "infer": True,
            "tools": ["read"],
        }

    def test_custom_agent_config_omits_absent_tools(self, tmp_path: Path) -> None:
        # Sending `tools: None` would tell the SDK something different from
        # not mentioning tools at all.
        path = write_agent(tmp_path / "agents", "review")
        assert "tools" not in read_plugin_agent(path, "p").to_custom_agent_config()


class TestRejections:
    """Each of these would otherwise register an unusable agent."""

    def test_missing_frontmatter(self, tmp_path: Path) -> None:
        path = tmp_path / "a.agent.md"
        path.write_text("Just a prompt, no frontmatter.\n")
        with pytest.raises(PluginManifestError, match="no YAML frontmatter"):
            read_plugin_agent(path, "p")

    def test_invalid_yaml_frontmatter(self, tmp_path: Path) -> None:
        # The colon-in-a-plain-scalar trap, which both CLIs skip silently.
        path = tmp_path / "a.agent.md"
        path.write_text("---\nname: a\ndescription: Does things. Triggers: x, y\n---\nBody\n")
        with pytest.raises(PluginManifestError, match="invalid YAML frontmatter"):
            read_plugin_agent(path, "p")

    def test_missing_description(self, tmp_path: Path) -> None:
        path = tmp_path / "a.agent.md"
        path.write_text("---\nname: a\n---\nBody\n")
        with pytest.raises(PluginManifestError, match="no usable 'description'"):
            read_plugin_agent(path, "p")

    def test_empty_body(self, tmp_path: Path) -> None:
        path = tmp_path / "a.agent.md"
        path.write_text("---\nname: a\ndescription: d\n---\n\n   \n")
        with pytest.raises(PluginManifestError, match="empty body"):
            read_plugin_agent(path, "p")

    def test_delimiter_bearing_name(self, tmp_path: Path) -> None:
        path = tmp_path / "a.agent.md"
        path.write_text("---\nname: bad:name\ndescription: d\n---\nBody\n")
        with pytest.raises(PluginManifestError, match="characters outside"):
            read_plugin_agent(path, "p")

    def test_non_list_tools(self, tmp_path: Path) -> None:
        path = tmp_path / "a.agent.md"
        path.write_text("---\nname: a\ndescription: d\ntools: read\n---\nBody\n")
        with pytest.raises(PluginManifestError, match="expected a list"):
            read_plugin_agent(path, "p")

    def test_non_string_tool_entry(self, tmp_path: Path) -> None:
        path = tmp_path / "a.agent.md"
        path.write_text("---\nname: a\ndescription: d\ntools: [1, 2]\n---\nBody\n")
        with pytest.raises(PluginManifestError, match="not a non-empty string"):
            read_plugin_agent(path, "p")


class TestReadPluginAgents:
    def test_no_agents_directory_yields_nothing(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "p", "demo")
        assert read_plugin_agents(root, "demo", flavor="copilot") == []

    def test_sorted_by_file_name(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "p", "demo", agents=["zeta", "alpha", "mid"])
        agents = read_plugin_agents(root, "demo", flavor="copilot")
        assert [a.name for a in agents] == ["alpha", "mid", "zeta"]

    def test_non_agent_files_are_ignored(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "p", "demo", agents=["real"])
        (root / "agents" / "README.md").write_text("not an agent")
        agents = read_plugin_agents(root, "demo", flavor="copilot")
        assert [a.name for a in agents] == ["real"]

    def test_nested_directories_are_not_descended(self, tmp_path: Path) -> None:
        # Every plugin observed keeps a flat agents/ directory, and a nested
        # file would get a name indistinguishable from a top-level one.
        root = make_plugin(tmp_path / "p", "demo", agents=["flat"])
        write_agent(root / "agents" / "nested", "deep")
        agents = read_plugin_agents(root, "demo", flavor="copilot")
        assert [a.name for a in agents] == ["flat"]

    def test_duplicate_agent_names_are_refused(self, tmp_path: Path) -> None:
        # Two files, one declared name: deduping would drop one silently.
        root = make_plugin(tmp_path / "p", "demo")
        write_agent(root / "agents", "first")
        second = root / "agents" / "second.agent.md"
        second.write_text("---\nname: first\ndescription: d\n---\nBody\n")
        with pytest.raises(PluginManifestError, match="two agents named"):
            read_plugin_agents(root, "demo", flavor="copilot")

    def test_claude_flavor_reads_bare_md_agents(self, tmp_path: Path) -> None:
        # The regression at the heart of issue #497: a Claude-built plugin's
        # agents/*.md files were never read because the candidate rule was
        # hardcoded to the Copilot suffix.
        root = make_plugin(tmp_path / "p", "demo", agents=["code-reviewer"], agent_suffix=".md")
        agents = read_plugin_agents(root, "demo", flavor="claude")
        assert [a.name for a in agents] == ["code-reviewer"]

    def test_copilot_flavor_ignores_bare_md(self, tmp_path: Path) -> None:
        # The Copilot build's convention is strictly ".agent.md" — a bare
        # ".md" file is not a candidate under that flavor, even though it
        # would be under "claude".
        root = make_plugin(tmp_path / "p", "demo", agents=["code-reviewer"], agent_suffix=".md")
        assert read_plugin_agents(root, "demo", flavor="copilot") == []

    def test_claude_flavor_warns_and_skips_frontmatterless_bare_md(self, tmp_path: Path) -> None:
        # A bare ".md" with no frontmatter at all reads as documentation
        # (a README), not a broken agent — it never explicitly claimed to
        # be one the way an ".agent.md" extension does.
        root = make_plugin(tmp_path / "p", "demo", agents=["real"], agent_suffix=".md")
        (root / "agents" / "README.md").write_text("Just some prose.\n")
        warnings: list[str] = []
        agents = read_plugin_agents(root, "demo", flavor="claude", on_warning=warnings.append)
        assert [a.name for a in agents] == ["real"]
        assert any(
            "README.md" in message and "no YAML frontmatter" in message for message in warnings
        )

    def test_explicit_agent_md_with_no_frontmatter_still_raises(self, tmp_path: Path) -> None:
        # An ".agent.md" extension is an explicit claim to be an agent, so
        # the same "no frontmatter" failure stays fatal regardless of
        # flavor — it never falls into the warn-and-skip path above.
        root = make_plugin(tmp_path / "p", "demo")
        (root / "agents").mkdir(parents=True)
        (root / "agents" / "broken.agent.md").write_text("No frontmatter here.\n")
        with pytest.raises(PluginManifestError, match="no YAML frontmatter"):
            read_plugin_agents(root, "demo", flavor="claude")
        with pytest.raises(PluginManifestError, match="no YAML frontmatter"):
            read_plugin_agents(root, "demo", flavor="copilot")

    def test_malformed_frontmatter_still_raises_under_both_flavors(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "p", "demo")
        (root / "agents").mkdir(parents=True)
        (root / "agents" / "bad.md").write_text(
            "---\nname: a\ndescription: Does things. Triggers: x, y\n---\nBody\n"
        )
        with pytest.raises(PluginManifestError, match="invalid YAML frontmatter"):
            read_plugin_agents(root, "demo", flavor="claude")

    def test_missing_name_still_raises_under_both_flavors(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "p", "demo")
        (root / "agents").mkdir(parents=True)
        (root / "agents" / "bad.md").write_text("---\ndescription: d\n---\nBody\n")
        with pytest.raises(PluginManifestError, match="no usable 'name'"):
            read_plugin_agents(root, "demo", flavor="claude")

    def test_empty_body_still_raises_under_both_flavors(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "p", "demo")
        (root / "agents").mkdir(parents=True)
        (root / "agents" / "bad.md").write_text("---\nname: a\ndescription: d\n---\n\n   \n")
        with pytest.raises(PluginManifestError, match="empty body"):
            read_plugin_agents(root, "demo", flavor="claude")
