"""Tests for reading marketplace catalogs out of a source checkout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conductor.plugins.errors import PluginNotFoundError, PluginSourceError
from conductor.plugins.marketplace import find_marketplace_manifest, read_marketplace

from .conftest import make_marketplace, make_plugin


class TestCatalogAnchoring:
    """The two conventions anchor per-plugin ``source`` differently.

    Verified against a real marketplace repository shipping both:
    ``.claude-plugin/marketplace.json`` writes ``./dist/claude/ado``
    (repo-root-relative) while ``.github/plugin/marketplace.json`` writes
    ``./ado`` alongside ``pluginRoot: ./dist/copilot``. Assuming either
    one strands every plugin published under the other.
    """

    def test_repo_root_relative_sources(self, tmp_path: Path):
        make_plugin(tmp_path / "dist" / "claude" / "ado", "ado")
        make_marketplace(
            tmp_path,
            "acme",
            {"ado": "./dist/claude/ado"},
            manifest=".claude-plugin",
            plugin_root="./dist/claude",
        )

        marketplace = read_marketplace(tmp_path, name="acme")

        assert marketplace.plugins == {"ado": tmp_path / "dist" / "claude" / "ado"}

    def test_plugin_root_relative_sources(self, tmp_path: Path):
        make_plugin(tmp_path / "dist" / "copilot" / "ado", "ado", manifest=".github/plugin")
        make_marketplace(
            tmp_path,
            "acme",
            {"ado": "./ado"},
            manifest=".github/plugin",
            plugin_root="./dist/copilot",
        )

        marketplace = read_marketplace(tmp_path, name="acme")

        assert marketplace.plugins == {"ado": tmp_path / "dist" / "copilot" / "ado"}

    def test_plugins_directly_at_the_root(self, tmp_path: Path):
        make_plugin(tmp_path / "prs", "prs")
        make_marketplace(tmp_path, "acme", {"prs": "./prs"})

        assert read_marketplace(tmp_path, name="acme").plugins == {"prs": tmp_path / "prs"}


class TestCatalogContents:
    """What a catalog reports, and what it quietly leaves out."""

    def test_name_comes_from_the_manifest(self, tmp_path: Path):
        make_plugin(tmp_path / "prs", "prs")
        make_marketplace(tmp_path, "declared-name", {"prs": "./prs"})

        assert read_marketplace(tmp_path, name="registered-as").name == "declared-name"

    def test_is_flagged_as_a_catalog(self, tmp_path: Path):
        make_plugin(tmp_path / "prs", "prs")
        make_marketplace(tmp_path, "acme", {"prs": "./prs"})

        assert read_marketplace(tmp_path, name="acme").is_catalog is True

    def test_entries_pointing_at_no_plugin_are_skipped(self, tmp_path: Path):
        """A marketplace commonly publishes for several CLIs from one repo.

        One unbuilt variant must not make every other plugin in the
        catalog unreachable — the miss surfaces when that specific plugin
        is asked for.
        """
        make_plugin(tmp_path / "prs", "prs")
        make_marketplace(tmp_path, "acme", {"prs": "./prs", "ghost": "./not-built"})

        marketplace = read_marketplace(tmp_path, name="acme")

        assert sorted(marketplace.plugins) == ["prs"]

    def test_resolving_a_missing_plugin_lists_what_is_available(self, tmp_path: Path):
        make_plugin(tmp_path / "prs", "prs")
        make_plugin(tmp_path / "ado", "ado")
        make_marketplace(tmp_path, "acme", {"prs": "./prs", "ado": "./ado"})

        marketplace = read_marketplace(tmp_path, name="acme")

        with pytest.raises(PluginNotFoundError, match="It provides: ado, prs"):
            marketplace.resolve("nope")

    def test_malformed_entries_are_skipped(self, tmp_path: Path):
        make_plugin(tmp_path / "prs", "prs")
        make_marketplace(tmp_path, "acme", {"prs": "./prs"})
        manifest = tmp_path / ".claude-plugin" / "marketplace.json"
        document = json.loads(manifest.read_text())
        document["plugins"] += [{"name": "no-source"}, {"source": "./x"}, "not-an-object"]
        manifest.write_text(json.dumps(document), encoding="utf-8")

        assert sorted(read_marketplace(tmp_path, name="acme").plugins) == ["prs"]


class TestSinglePluginSources:
    """A repository that *is* one plugin, rather than a catalog of them."""

    def test_resolves_under_its_declared_name(self, tmp_path: Path):
        make_plugin(tmp_path, "reviewer")

        marketplace = read_marketplace(tmp_path, name="acme")

        assert marketplace.is_catalog is False
        assert marketplace.plugins == {"reviewer": tmp_path}

    def test_marketplace_keeps_the_registered_name(self, tmp_path: Path):
        """``plugins: [reviewer@acme]`` must work regardless of the repo name."""
        make_plugin(tmp_path, "reviewer")

        assert read_marketplace(tmp_path, name="acme").name == "acme"

    def test_an_explicit_plugin_key_must_match(self, tmp_path: Path):
        make_plugin(tmp_path, "reviewer")

        with pytest.raises(PluginSourceError, match="the plugin there is named 'reviewer'"):
            read_marketplace(tmp_path, name="acme", plugin="something-else")


class TestAmbiguousSources:
    """A repository holding both manifests needs ``plugin:`` to settle it."""

    def test_refused_without_a_disambiguator(self, tmp_path: Path):
        make_plugin(tmp_path, "self")
        make_marketplace(tmp_path, "acme", {"prs": "./prs"}, manifest=".github/plugin")

        with pytest.raises(PluginSourceError, match="both a catalog and a plugin"):
            read_marketplace(tmp_path, name="acme")

    def test_plugin_key_selects_the_single_plugin(self, tmp_path: Path):
        make_plugin(tmp_path, "self")
        make_marketplace(tmp_path, "acme", {"prs": "./prs"}, manifest=".github/plugin")

        marketplace = read_marketplace(tmp_path, name="acme", plugin="self")

        assert marketplace.plugins == {"self": tmp_path}


class TestPathTraversal:
    """A catalog manifest is fetched content, not something the author wrote."""

    def test_a_source_escaping_the_checkout_is_not_resolved(self, tmp_path: Path):
        outside = tmp_path / "outside"
        make_plugin(outside, "evil")
        checkout = tmp_path / "checkout"
        make_marketplace(checkout, "acme", {"evil": "../outside"})

        marketplace = read_marketplace(checkout, name="acme")

        assert marketplace.plugins == {}

    def test_a_plugin_root_escaping_the_checkout_is_ignored(self, tmp_path: Path):
        outside = tmp_path / "outside"
        make_plugin(outside / "evil", "evil")
        checkout = tmp_path / "checkout"
        make_marketplace(checkout, "acme", {"evil": "./evil"}, plugin_root="../outside")

        assert read_marketplace(checkout, name="acme").plugins == {}


class TestRejections:
    """A source that is neither a catalog nor a plugin."""

    def test_empty_directory(self, tmp_path: Path):
        with pytest.raises(PluginSourceError, match="neither a marketplace nor a plugin"):
            read_marketplace(tmp_path, name="acme")

    def test_catalog_without_a_name(self, tmp_path: Path):
        manifest = tmp_path / ".claude-plugin"
        manifest.mkdir(parents=True)
        (manifest / "marketplace.json").write_text(json.dumps({"plugins": []}), encoding="utf-8")

        with pytest.raises(PluginSourceError, match="no usable 'name'"):
            read_marketplace(tmp_path, name="acme")

    def test_catalog_without_a_plugins_list(self, tmp_path: Path):
        manifest = tmp_path / ".claude-plugin"
        manifest.mkdir(parents=True)
        (manifest / "marketplace.json").write_text(json.dumps({"name": "acme"}), encoding="utf-8")

        with pytest.raises(PluginSourceError, match="no 'plugins' list"):
            read_marketplace(tmp_path, name="acme")

    def test_unparseable_catalog(self, tmp_path: Path):
        manifest = tmp_path / ".claude-plugin"
        manifest.mkdir(parents=True)
        (manifest / "marketplace.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(PluginSourceError, match="could not be read"):
            read_marketplace(tmp_path, name="acme")


class TestFindMarketplaceManifest:
    """Both catalog conventions are probed."""

    @pytest.mark.parametrize("convention", [".claude-plugin", ".github/plugin"])
    def test_found(self, tmp_path: Path, convention: str):
        make_marketplace(tmp_path, "acme", {}, manifest=convention)

        found = find_marketplace_manifest(tmp_path)

        assert found is not None
        assert found.name == "marketplace.json"

    def test_absent(self, tmp_path: Path):
        assert find_marketplace_manifest(tmp_path) is None
