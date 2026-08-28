"""Tests for reading marketplace catalogs out of a source checkout."""

from __future__ import annotations

import json
import shutil
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


class TestCatalogNarrowing:
    """``plugin:`` against a catalog, with and without a root plugin.

    The ambiguity error recommends adding ``plugin:``, so that key has to
    work in both directions — naming the root plugin *and* naming any
    entry the catalog lists. Checking the root manifest first meant the
    catalog was never consulted, and every catalog entry was answered
    with "the plugin there is named <root>" about a manifest that
    visibly lists it.
    """

    def test_narrows_a_pure_catalog_to_one_entry(self, tmp_path: Path):
        make_plugin(tmp_path / "prs", "prs")
        make_plugin(tmp_path / "ado", "ado")
        make_marketplace(tmp_path, "acme", {"prs": "./prs", "ado": "./ado"})

        marketplace = read_marketplace(tmp_path, name="acme", plugin="prs")

        assert marketplace.plugins == {"prs": tmp_path / "prs"}
        assert marketplace.is_catalog is True

    def test_a_catalog_that_no_longer_ships_the_named_plugin_says_so(self, tmp_path: Path):
        make_plugin(tmp_path / "prs", "prs")
        make_marketplace(tmp_path, "acme", {"prs": "./prs"})

        with pytest.raises(PluginNotFoundError, match="It provides: prs"):
            read_marketplace(tmp_path, name="acme", plugin="gone")

    def test_plugin_key_reaches_a_catalog_entry_in_a_both_shaped_repo(self, tmp_path: Path):
        make_plugin(tmp_path, "rootplug")
        make_plugin(tmp_path / "listed", "listed")
        make_marketplace(tmp_path, "acme", {"listed": "./listed"}, manifest=".github/plugin")

        marketplace = read_marketplace(tmp_path, name="acme", plugin="listed")

        assert marketplace.plugins == {"listed": tmp_path / "listed"}
        assert marketplace.is_catalog is True

    def test_plugin_key_still_reaches_the_root_plugin(self, tmp_path: Path):
        make_plugin(tmp_path, "rootplug")
        make_plugin(tmp_path / "listed", "listed")
        make_marketplace(tmp_path, "acme", {"listed": "./listed"}, manifest=".github/plugin")

        marketplace = read_marketplace(tmp_path, name="acme", plugin="rootplug")

        assert marketplace.plugins == {"rootplug": tmp_path}
        assert marketplace.is_catalog is False


class TestManifestPrecedence:
    """``.claude-plugin`` is probed before ``.github/plugin``."""

    def test_claude_convention_wins_when_both_are_present(self, tmp_path: Path):
        make_plugin(tmp_path / "from-claude", "from-claude")
        make_plugin(tmp_path / "from-github", "from-github")
        make_marketplace(
            tmp_path, "claude-side", {"from-claude": "./from-claude"}, manifest=".claude-plugin"
        )
        make_marketplace(
            tmp_path, "github-side", {"from-github": "./from-github"}, manifest=".github/plugin"
        )

        assert read_marketplace(tmp_path, name="acme").name == "claude-side"

    @pytest.mark.parametrize("convention", [".claude-plugin", ".github/plugin"])
    def test_found_at_its_exact_path(self, tmp_path: Path, convention: str):
        make_marketplace(tmp_path, "acme", {}, manifest=convention)

        assert find_marketplace_manifest(tmp_path) == tmp_path / convention / "marketplace.json"


class TestDualCatalogFlavorResolution:
    """A repository shipping both catalog conventions, mirroring the real
    dual-build marketplace verified for issue #497: each catalog points at
    its own build directory, and ``flavor=`` picks between them."""

    def _build(self, tmp_path: Path):
        make_plugin(tmp_path / "dist" / "claude" / "prs", "prs")
        make_plugin(tmp_path / "dist" / "copilot" / "prs", "prs", manifest=".github/plugin")
        make_marketplace(
            tmp_path,
            "acme",
            {"prs": "./dist/claude/prs"},
            manifest=".claude-plugin",
            plugin_root="./dist/claude",
        )
        make_marketplace(
            tmp_path,
            "acme",
            {"prs": "./prs"},
            manifest=".github/plugin",
            plugin_root="./dist/copilot",
        )

    def test_flavored_tables_are_populated(self, tmp_path: Path):
        self._build(tmp_path)

        marketplace = read_marketplace(tmp_path, name="acme")

        assert marketplace.flavored["claude"] == {"prs": tmp_path / "dist" / "claude" / "prs"}
        assert marketplace.flavored["copilot"] == {"prs": tmp_path / "dist" / "copilot" / "prs"}

    def test_resolve_picks_the_claude_build(self, tmp_path: Path):
        self._build(tmp_path)

        marketplace = read_marketplace(tmp_path, name="acme")

        assert marketplace.resolve("prs", flavor="claude") == tmp_path / "dist" / "claude" / "prs"

    def test_resolve_picks_the_copilot_build(self, tmp_path: Path):
        self._build(tmp_path)

        marketplace = read_marketplace(tmp_path, name="acme")

        assert marketplace.resolve("prs", flavor="copilot") == tmp_path / "dist" / "copilot" / "prs"

    def test_unflavored_resolve_keeps_the_claude_first_default(self, tmp_path: Path):
        self._build(tmp_path)

        marketplace = read_marketplace(tmp_path, name="acme")

        assert marketplace.resolve("prs") == tmp_path / "dist" / "claude" / "prs"
        assert marketplace.plugins == {"prs": tmp_path / "dist" / "claude" / "prs"}

    def test_single_catalog_marketplace_never_warns_on_a_flavor_mismatch(
        self, tmp_path: Path
    ) -> None:
        # Only one build exists at all, so using it is not a "fallback" —
        # there was never a genuine choice to have gotten wrong.
        make_plugin(tmp_path / "prs", "prs")
        make_marketplace(tmp_path, "acme", {"prs": "./prs"})

        marketplace = read_marketplace(tmp_path, name="acme")
        warnings: list[str] = []
        root = marketplace.resolve("prs", flavor="copilot", on_warning=warnings.append)

        assert root == tmp_path / "prs"
        assert warnings == []

    def test_dual_catalog_missing_flavor_table_warns_and_falls_back(self, tmp_path: Path) -> None:
        self._build(tmp_path)
        # Delete the Copilot build so its catalog entry cannot resolve,
        # leaving only the Claude flavor populated.
        shutil.rmtree(tmp_path / "dist" / "copilot")

        marketplace = read_marketplace(tmp_path, name="acme")
        assert marketplace.flavored["copilot"] == {}
        warnings: list[str] = []
        root = marketplace.resolve("prs", flavor="copilot", on_warning=warnings.append)

        assert root == tmp_path / "dist" / "claude" / "prs"
        assert any("no 'copilot'-flavored build" in message for message in warnings)

    def test_a_secondary_catalog_that_cannot_be_read_does_not_fail_the_marketplace(
        self, tmp_path: Path
    ) -> None:
        # A repository with a valid primary (Claude) catalog and a broken
        # secondary (Copilot) one used to hard-fail the whole marketplace —
        # a strict regression against `main`, which only ever read the
        # first match. The secondary convention must be best-effort.
        self._build(tmp_path)
        (tmp_path / ".github" / "plugin" / "marketplace.json").write_text(
            "not json", encoding="utf-8"
        )

        marketplace = read_marketplace(tmp_path, name="acme")

        assert marketplace.plugins == {"prs": tmp_path / "dist" / "claude" / "prs"}
        assert "copilot" not in marketplace.flavored

    def test_plugin_key_narrowing_preserves_the_multi_flavor_signal(self, tmp_path: Path) -> None:
        # A plugin published in only one of two catalogs must not make
        # `plugin:` narrowing collapse `flavored` to a single key — that
        # silently suppressed the flavor-fallback warning below (issue
        # #497's own failure mode recurring inside its fix).
        self._build(tmp_path)
        shutil.rmtree(tmp_path / "dist" / "copilot")

        marketplace = read_marketplace(tmp_path, name="acme", plugin="prs")

        assert marketplace.flavored["copilot"] == {}
        warnings: list[str] = []
        root = marketplace.resolve("prs", flavor="copilot", on_warning=warnings.append)
        assert root == tmp_path / "dist" / "claude" / "prs"
        assert any("no 'copilot'-flavored build" in message for message in warnings)

    def test_plugin_key_narrowing_finds_a_plugin_published_only_in_the_secondary_catalog(
        self, tmp_path: Path
    ) -> None:
        # `resolved.resolve(plugin)` with no flavor only ever consults the
        # primary (Claude-first) table, so a plugin listed only in the
        # Copilot catalog was unreachable through the narrowed marketplace
        # even though its own `flavored` table held a perfectly good entry.
        make_plugin(tmp_path / "dist" / "copilot" / "solo", "solo", manifest=".github/plugin")
        make_marketplace(
            tmp_path, "acme", {}, manifest=".claude-plugin", plugin_root="./dist/claude"
        )
        make_marketplace(
            tmp_path,
            "acme",
            {"solo": "./solo"},
            manifest=".github/plugin",
            plugin_root="./dist/copilot",
        )

        marketplace = read_marketplace(tmp_path, name="acme", plugin="solo")

        assert marketplace.plugins == {"solo": tmp_path / "dist" / "copilot" / "solo"}
