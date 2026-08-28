"""Tests for reading git-backed marketplaces the Copilot CLI itself registered.

``~/.copilot/settings.json`` is where most installed plugins actually come
from on an ordinary machine — its ``extraKnownMarketplaces`` entries, not
the ``~/.copilot/installed-plugins/`` directory
:mod:`conductor.plugins.registry` otherwise searches. See issue #497.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conductor.plugins.copilot_settings import read_copilot_marketplaces
from conductor.plugins.errors import PluginNotFoundError
from conductor.plugins.registry import resolve_plugin

from .conftest import make_marketplace, make_plugin


def _write_settings(home: Path, marketplaces: dict) -> None:
    settings_dir = home / ".copilot"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.json").write_text(
        json.dumps({"extraKnownMarketplaces": marketplaces}), encoding="utf-8"
    )


class TestReadCopilotMarketplaces:
    def test_directory_marketplaces_are_parsed(self, home: Path, tmp_path: Path) -> None:
        target = tmp_path / "plugins-repo"
        target.mkdir()
        _write_settings(
            home, {"jason-tools": {"source": {"path": str(target), "source": "directory"}}}
        )

        assert read_copilot_marketplaces(home) == {"jason-tools": target}

    def test_tilde_paths_are_expanded(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(home))
        _write_settings(
            home,
            {"mine": {"source": {"path": "~/src/conductor-workflows", "source": "directory"}}},
        )

        result = read_copilot_marketplaces(home)

        assert result == {"mine": home / "src" / "conductor-workflows"}

    def test_trailing_slash_is_normalised(self, home: Path, tmp_path: Path) -> None:
        target = tmp_path / "repo"
        target.mkdir()
        _write_settings(
            home, {"conductor": {"source": {"path": f"{target}/", "source": "directory"}}}
        )

        assert read_copilot_marketplaces(home) == {"conductor": target}

    def test_non_directory_source_kinds_are_skipped(self, home: Path) -> None:
        _write_settings(
            home,
            {"git-remote": {"source": {"url": "https://example.com/x.git", "source": "git"}}},
        )

        assert read_copilot_marketplaces(home) == {}

    def test_missing_file_yields_empty(self, home: Path) -> None:
        assert read_copilot_marketplaces(home) == {}

    def test_corrupt_file_yields_empty(self, home: Path) -> None:
        settings_dir = home / ".copilot"
        settings_dir.mkdir(parents=True)
        (settings_dir / "settings.json").write_text("{not json", encoding="utf-8")

        assert read_copilot_marketplaces(home) == {}

    def test_non_object_file_yields_empty(self, home: Path) -> None:
        settings_dir = home / ".copilot"
        settings_dir.mkdir(parents=True)
        (settings_dir / "settings.json").write_text("[1, 2, 3]", encoding="utf-8")

        assert read_copilot_marketplaces(home) == {}

    def test_missing_extra_known_marketplaces_yields_empty(self, home: Path) -> None:
        settings_dir = home / ".copilot"
        settings_dir.mkdir(parents=True)
        (settings_dir / "settings.json").write_text(json.dumps({"foo": "bar"}), encoding="utf-8")

        assert read_copilot_marketplaces(home) == {}

    def test_entries_missing_a_usable_path_are_skipped(self, home: Path) -> None:
        _write_settings(
            home,
            {
                "no-path": {"source": {"source": "directory"}},
                "blank-path": {"source": {"path": "   ", "source": "directory"}},
                "not-a-dict": "oops",
            },
        )

        assert read_copilot_marketplaces(home) == {}


class TestSettingsRegisteredMarketplaceResolution:
    """``plugin@marketplace`` resolving through the Copilot CLI's own registry."""

    def test_resolves_under_copilot_flavor(self, home: Path, tmp_path: Path) -> None:
        catalog = tmp_path / "catalog"
        make_plugin(catalog / "prs", "prs", skills=["review"])
        make_marketplace(catalog, "jason-tools", {"prs": "./prs"})
        _write_settings(
            home, {"jason-tools": {"source": {"path": str(catalog), "source": "directory"}}}
        )

        plugin = resolve_plugin("prs@jason-tools", home=home, flavor="copilot")

        assert plugin.name == "prs"
        assert plugin.root == catalog / "prs"

    def test_does_not_resolve_under_claude_flavor(self, home: Path, tmp_path: Path) -> None:
        # Scoped to Copilot only: this is the Copilot CLI's own settings
        # file, and applying it to a Claude-flavored agent would resolve a
        # marketplace the Claude CLI was never told about.
        catalog = tmp_path / "catalog"
        make_plugin(catalog / "prs", "prs")
        make_marketplace(catalog, "jason-tools", {"prs": "./prs"})
        _write_settings(
            home, {"jason-tools": {"source": {"path": str(catalog), "source": "directory"}}}
        )

        with pytest.raises(PluginNotFoundError, match="neither declared"):
            resolve_plugin("prs@jason-tools", home=home, flavor="claude")

    def test_advisory_warning_names_the_standalone_remedy(self, home: Path, tmp_path: Path) -> None:
        catalog = tmp_path / "catalog"
        make_plugin(catalog / "prs", "prs")
        make_marketplace(catalog, "jason-tools", {"prs": "./prs"})
        _write_settings(
            home, {"jason-tools": {"source": {"path": str(catalog), "source": "directory"}}}
        )

        warnings: list[str] = []
        resolve_plugin("prs@jason-tools", home=home, flavor="copilot", on_warning=warnings.append)

        assert any("plugin_sources" in message and "jason-tools" in message for message in warnings)

    def test_declared_source_and_installed_glob_both_take_precedence(
        self, home: Path, tmp_path: Path
    ) -> None:
        # The settings.json fallback is deliberately last: it can only turn
        # a hard error into a resolution, never override an installed glob
        # match that already succeeded.
        installed_root = make_plugin(
            home / ".copilot" / "installed-plugins" / "jason-tools" / "prs", "prs"
        )
        catalog = tmp_path / "catalog"
        make_plugin(catalog / "prs", "prs")
        make_marketplace(catalog, "jason-tools", {"prs": "./prs"})
        _write_settings(
            home, {"jason-tools": {"source": {"path": str(catalog), "source": "directory"}}}
        )

        plugin = resolve_plugin("prs@jason-tools", home=home, flavor="copilot")

        assert plugin.root == installed_root

    def test_unknown_marketplace_names_still_lists_settings_registered_ones(
        self, home: Path, tmp_path: Path
    ) -> None:
        catalog = tmp_path / "catalog"
        make_plugin(catalog / "prs", "prs")
        make_marketplace(catalog, "jason-tools", {"prs": "./prs"})
        _write_settings(
            home, {"jason-tools": {"source": {"path": str(catalog), "source": "directory"}}}
        )

        with pytest.raises(PluginNotFoundError, match="jason-tools"):
            resolve_plugin("prs@not-jason-tools", home=home, flavor="copilot")
