"""Tests for plugin manifest discovery and parsing.

The manifest is where issue #378's cheapest failure lived: Conductor
recognised only ``.claude-plugin/plugin.json`` while 12 of 13 plugins on
an ordinary machine ship ``.github/plugin/plugin.json``. Both resolve at
runtime — verified against a live Copilot session with a synthetic plugin
under each convention — so the gap was Conductor's alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conductor.plugins.errors import PluginManifestError
from conductor.plugins.manifest import (
    MANIFEST_FLAVORS,
    PLUGIN_MANIFESTS,
    find_manifest,
    is_plugin_root,
    manifest_flavor,
    read_plugin_manifest,
)

from .conftest import make_plugin


class TestBothConventionsResolve:
    """Neither CLI's layout may be privileged over the other."""

    @pytest.mark.parametrize("convention", [".claude-plugin", ".github/plugin"])
    def test_manifest_is_found(self, tmp_path: Path, convention: str) -> None:
        root = make_plugin(tmp_path / "p", "demo", manifest=convention)
        assert is_plugin_root(root)
        assert read_plugin_manifest(root).name == "demo"

    def test_claude_convention_wins_when_both_exist(self, tmp_path: Path) -> None:
        # No observed plugin ships both. First match wins rather than the
        # two being merged, which would need a precedence rule per field.
        root = make_plugin(tmp_path / "p", "claude-name", manifest=".claude-plugin")
        github = root / ".github" / "plugin"
        github.mkdir(parents=True)
        (github / "plugin.json").write_text(json.dumps({"name": "github-name"}))
        assert read_plugin_manifest(root).name == "claude-name"
        assert find_manifest(root) == root / PLUGIN_MANIFESTS[0]

    def test_directory_without_a_manifest_is_not_a_plugin(self, tmp_path: Path) -> None:
        (tmp_path / "plain" / "skills").mkdir(parents=True)
        assert find_manifest(tmp_path / "plain") is None
        assert not is_plugin_root(tmp_path / "plain")
        with pytest.raises(PluginManifestError, match="is not a plugin"):
            read_plugin_manifest(tmp_path / "plain")


class TestFlavor:
    """Flavor is read off whichever manifest convention actually matched."""

    @pytest.mark.parametrize(
        ("convention", "expected"), [(".claude-plugin", "claude"), (".github/plugin", "copilot")]
    )
    def test_flavor_matches_convention(
        self, tmp_path: Path, convention: str, expected: str
    ) -> None:
        root = make_plugin(tmp_path / "p", "demo", manifest=convention)
        assert read_plugin_manifest(root).flavor == expected

    def test_prefer_reorders_the_probe(self, tmp_path: Path) -> None:
        # A root with both conventions present would normally resolve
        # Claude-first; asking for "copilot" moves that convention first
        # instead, without excluding the other one from consideration.
        root = make_plugin(tmp_path / "p", "claude-name", manifest=".claude-plugin")
        github = root / ".github" / "plugin"
        github.mkdir(parents=True)
        (github / "plugin.json").write_text(json.dumps({"name": "github-name"}))
        manifest = read_plugin_manifest(root, prefer="copilot")
        assert manifest.name == "github-name"
        assert manifest.flavor == "copilot"

    def test_prefer_does_not_exclude_the_other_convention(self, tmp_path: Path) -> None:
        # A root that only ships the Claude convention still resolves when
        # "copilot" is preferred — prefer reorders, it never excludes.
        root = make_plugin(tmp_path / "p", "demo", manifest=".claude-plugin")
        manifest = read_plugin_manifest(root, prefer="copilot")
        assert manifest.name == "demo"
        assert manifest.flavor == "claude"

    def test_manifest_flavor_matches_each_convention(self, tmp_path: Path) -> None:
        for relative, flavor in MANIFEST_FLAVORS:
            root = tmp_path / flavor
            root.mkdir()
            assert manifest_flavor(root / relative, root) == flavor

    def test_manifest_flavor_rejects_a_path_outside_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(PluginManifestError, match="is not under plugin root"):
            manifest_flavor(tmp_path / "elsewhere" / "plugin.json", root)

    def test_manifest_flavor_rejects_an_unrecognised_relative_path(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(PluginManifestError, match="does not match any recognised"):
            manifest_flavor(root / "weird" / "plugin.json", root)


class TestManifestName:
    def test_missing_name_is_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "p"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "1"}))
        with pytest.raises(PluginManifestError, match="no usable 'name'"):
            read_plugin_manifest(root)

    def test_unparseable_manifest_is_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "p"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text("{not json")
        with pytest.raises(PluginManifestError, match="could not be read"):
            read_plugin_manifest(root)

    def test_non_object_manifest_is_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "p"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text('["not", "an", "object"]')
        with pytest.raises(PluginManifestError, match="not a JSON object"):
            read_plugin_manifest(root)

    @pytest.mark.parametrize("name", ["has:colon", "has,comma", "has space"])
    def test_delimiter_bearing_name_is_rejected(self, tmp_path: Path, name: str) -> None:
        # The name is joined into a delimiter-separated tool list, so a
        # ':' or ',' would split into extra permission rules.
        root = tmp_path / "p"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}))
        with pytest.raises(PluginManifestError, match="characters outside"):
            read_plugin_manifest(root)


class TestMcpDeclarations:
    """All three declaration forms, because real plugins use the file one."""

    def test_string_path_form(self, tmp_path: Path) -> None:
        # Every MCP-shipping plugin observed writes `"mcpServers": ".mcp.json"`.
        root = make_plugin(tmp_path / "p", "demo", mcp={"ado": {"type": "stdio", "command": "npx"}})
        assert read_plugin_manifest(root).mcp_servers == {
            "ado": {"type": "stdio", "command": "npx"}
        }

    def test_inline_object_form(self, tmp_path: Path) -> None:
        root = make_plugin(
            tmp_path / "p",
            "demo",
            mcp={"ado": {"type": "stdio", "command": "npx"}},
            mcp_inline=True,
        )
        assert list(read_plugin_manifest(root).mcp_servers) == ["ado"]

    def test_conventional_file_without_a_manifest_key(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "p", "demo")
        (root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"web": {"type": "stdio", "command": "npx"}}})
        )
        assert list(read_plugin_manifest(root).mcp_servers) == ["web"]

    def test_no_declaration_yields_nothing(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "p", "demo")
        assert read_plugin_manifest(root).mcp_servers == {}

    def test_empty_declaration_is_honoured(self, tmp_path: Path) -> None:
        # An installed plugin really does ship `{"mcpServers": {}}`.
        root = make_plugin(tmp_path / "p", "demo", mcp={})
        assert read_plugin_manifest(root).mcp_servers == {}

    def test_missing_referenced_file_is_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "p"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "demo", "mcpServers": "servers.json"})
        )
        with pytest.raises(PluginManifestError, match="could not be read"):
            read_plugin_manifest(root)

    def test_non_object_server_config_is_rejected(self, tmp_path: Path) -> None:
        root = make_plugin(tmp_path / "p", "demo", mcp={"broken": "not-an-object"})
        with pytest.raises(PluginManifestError, match="rather than an object"):
            read_plugin_manifest(root)

    def test_wrong_typed_declaration_is_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "p"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "demo", "mcpServers": 42})
        )
        with pytest.raises(PluginManifestError, match="expected a path string"):
            read_plugin_manifest(root)
