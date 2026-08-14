"""Tests for composing declared sources into resolved marketplaces."""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.config.schema import PluginSourceDef
from conductor.plugins.errors import PluginFetchError, PluginSourceError
from conductor.plugins.resolution import marketplaces_from, resolve_plugin_sources

from .conftest import make_git_repo, make_marketplace, make_plugin, rmtree


def _entry(source: str, **kwargs) -> PluginSourceDef:
    return PluginSourceDef(source=source, **kwargs)


class TestLocalSources:
    """A local path is a valid source and is read in place."""

    def test_resolves_a_single_plugin_directory(self, tmp_path: Path):
        make_plugin(tmp_path / "vendor" / "mine", "mine")

        resolved = resolve_plugin_sources({"local": _entry("./vendor/mine")}, base_dir=tmp_path)

        assert resolved["local"].marketplace.plugins == {"mine": tmp_path / "vendor" / "mine"}
        assert resolved["local"].sha is None

    def test_resolves_a_catalog_directory(self, tmp_path: Path):
        make_plugin(tmp_path / "vendor" / "prs", "prs")
        make_marketplace(tmp_path / "vendor", "acme", {"prs": "./prs"})

        resolved = resolve_plugin_sources({"acme": _entry("./vendor")}, base_dir=tmp_path)

        assert sorted(resolved["acme"].marketplace.plugins) == ["prs"]

    def test_relative_paths_anchor_on_the_workflow_directory(self, tmp_path: Path):
        """The same anchoring ``skills:`` and ``working_dir`` use."""
        workflow_dir = tmp_path / "workflows"
        workflow_dir.mkdir()
        make_plugin(tmp_path / "shared" / "mine", "mine")

        resolved = resolve_plugin_sources(
            {"local": _entry("../shared/mine")}, base_dir=workflow_dir
        )

        assert resolved["local"].marketplace.plugins["mine"] == tmp_path / "shared" / "mine"

    def test_a_missing_directory_is_an_error(self, tmp_path: Path):
        with pytest.raises(PluginSourceError, match="which is not a directory"):
            resolve_plugin_sources({"local": _entry("./nope")}, base_dir=tmp_path)


class TestSubdirectory:
    """``path:`` narrows a source to a subdirectory of the checkout."""

    def test_applied(self, tmp_path: Path):
        make_plugin(tmp_path / "repo" / "packages" / "plugins" / "prs", "prs")
        make_marketplace(tmp_path / "repo" / "packages" / "plugins", "acme", {"prs": "./prs"})

        resolved = resolve_plugin_sources(
            {"acme": _entry("./repo", path="packages/plugins")}, base_dir=tmp_path
        )

        assert sorted(resolved["acme"].marketplace.plugins) == ["prs"]

    def test_escaping_the_checkout_is_refused(self, tmp_path: Path):
        make_plugin(tmp_path / "outside", "evil")
        (tmp_path / "repo").mkdir()

        with pytest.raises(PluginSourceError, match="escapes the source directory"):
            resolve_plugin_sources({"acme": _entry("./repo", path="../outside")}, base_dir=tmp_path)

    def test_a_missing_subdirectory_is_an_error(self, tmp_path: Path):
        make_plugin(tmp_path / "repo", "thing")

        with pytest.raises(PluginSourceError, match="does not exist in the source"):
            resolve_plugin_sources({"acme": _entry("./repo", path="nope")}, base_dir=tmp_path)


class TestGitSources:
    """Remote sources go through the cache."""

    def test_resolves_a_cloned_catalog(self, tmp_path: Path, plugin_cache_home: Path):
        repo = tmp_path / "repo"
        make_plugin(repo / "prs", "prs", skills=["review"])
        make_marketplace(repo, "acme", {"prs": "./prs"})
        sha = make_git_repo(repo, tag="v1.0.0")

        resolved = resolve_plugin_sources({"acme": _entry(f"file://{repo}#v1.0.0")})

        assert resolved["acme"].sha == sha
        assert resolved["acme"].fetched is True
        assert sorted(resolved["acme"].marketplace.plugins) == ["prs"]

    def test_cache_only_mode_refuses_an_unfetched_source(self, tmp_path: Path, plugin_cache_home):
        repo = tmp_path / "repo"
        make_plugin(repo, "thing")
        make_git_repo(repo)

        with pytest.raises(PluginFetchError, match="conductor plugin fetch"):
            resolve_plugin_sources({"acme": _entry(f"file://{repo}")}, allow_network=False)

    def test_mixed_local_and_remote(self, tmp_path: Path, plugin_cache_home: Path):
        repo = tmp_path / "repo"
        make_plugin(repo, "remote-plugin")
        make_git_repo(repo)
        make_plugin(tmp_path / "vendor" / "local-plugin", "local-plugin")

        resolved = resolve_plugin_sources(
            {"far": _entry(f"file://{repo}"), "near": _entry("./vendor/local-plugin")},
            base_dir=tmp_path,
        )

        assert resolved["far"].sha is not None
        assert resolved["near"].sha is None


class TestMarketplacesFrom:
    """The reduction handed to ``resolve_plugins``."""

    def test_keys_by_declared_name(self, tmp_path: Path):
        make_plugin(tmp_path / "vendor" / "mine", "mine")
        resolved = resolve_plugin_sources({"local": _entry("./vendor/mine")}, base_dir=tmp_path)

        table = marketplaces_from(resolved)

        assert list(table) == ["local"]
        assert table["local"].plugins == {"mine": tmp_path / "vendor" / "mine"}

    def test_empty_input(self):
        assert resolve_plugin_sources({}) == {}
        assert marketplaces_from({}) == {}


class TestStaleForwarding:
    """``ResolvedSource.stale`` is how a caller knows the ref was not re-checked.

    Asserted here as well as on ``FetchResult`` because the forwarding
    between the two is what the CLI reads — without it an offline run
    would use a stale checkout and say nothing.
    """

    def test_stale_survives_the_composition_layer(self, tmp_path: Path, plugin_cache_home: Path):
        from conductor.plugins.fetch import clear_resolution_memo

        repo = tmp_path / "repo"
        make_plugin(repo, "thing")
        make_git_repo(repo, tag="v1.0.0")
        entry = _entry(f"file://{repo}#v1.0.0")
        resolve_plugin_sources({"acme": entry})

        rmtree(repo)
        clear_resolution_memo()
        warnings: list[str] = []
        resolved = resolve_plugin_sources({"acme": entry}, on_warning=warnings.append)

        assert resolved["acme"].stale is True
        assert any("cached checkout" in warning for warning in warnings)

    def test_a_fresh_fetch_is_not_stale(self, tmp_path: Path, plugin_cache_home: Path):
        repo = tmp_path / "repo"
        make_plugin(repo, "thing")
        make_git_repo(repo)

        resolved = resolve_plugin_sources({"acme": _entry(f"file://{repo}")})

        assert resolved["acme"].stale is False
        assert resolved["acme"].fetched is True
