"""Tests for registry configuration module."""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.registry.config import (
    RegistriesConfig,
    RegistryEntry,
    RegistryType,
    add_registry,
    get_config_path,
    get_registry,
    load_config,
    remove_registry,
    save_config,
)
from conductor.registry.errors import RegistryError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CONDUCTOR_HOME at a temp directory and return its path."""
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    return home


SAMPLE_TOML = """\
default = "official"

[registries.official]
type = "github"
source = "microsoft/conductor-workflows"

[registries.local]
type = "path"
source = "/Users/jason/workflows"
"""


# ---------------------------------------------------------------------------
# get_config_path
# ---------------------------------------------------------------------------


class TestGetConfigPath:
    def test_default_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CONDUCTOR_HOME", raising=False)
        path = get_config_path()
        assert path == Path.home() / ".conductor" / "registries.toml"

    def test_conductor_home_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "custom"))
        path = get_config_path()
        assert path == tmp_path / "custom" / "registries.toml"


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_missing_file_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        config = load_config()
        assert config.default is None
        assert config.registries == {}

    def test_valid_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _setup_config_home(tmp_path, monkeypatch)
        (home / "registries.toml").write_text(SAMPLE_TOML)

        config = load_config()
        assert config.default == "official"
        assert len(config.registries) == 2
        assert config.registries["official"].type == RegistryType.github
        assert config.registries["official"].source == "microsoft/conductor-workflows"
        assert config.registries["local"].type == RegistryType.path
        assert config.registries["local"].source == "/Users/jason/workflows"

    def test_malformed_toml_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _setup_config_home(tmp_path, monkeypatch)
        (home / "registries.toml").write_text("not valid [[[toml")

        with pytest.raises(RegistryError, match="Failed to parse"):
            load_config()

    def test_invalid_structure_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_config_home(tmp_path, monkeypatch)
        # default references a registry that doesn't exist
        (home / "registries.toml").write_text('default = "nope"\n')

        with pytest.raises(RegistryError, match="Invalid registry config"):
            load_config()


# ---------------------------------------------------------------------------
# save_config / round-trip
# ---------------------------------------------------------------------------


class TestSaveConfig:
    def test_save_and_reload_roundtrip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_config_home(tmp_path, monkeypatch)

        original = RegistriesConfig(
            default="team",
            registries={
                "team": RegistryEntry(type=RegistryType.github, source="myorg/workflows"),
                "dev": RegistryEntry(type=RegistryType.path, source="/dev/workflows"),
            },
        )
        save_config(original)

        loaded = load_config()
        assert loaded.default == "team"
        assert len(loaded.registries) == 2
        assert loaded.registries["team"].type == RegistryType.github
        assert loaded.registries["team"].source == "myorg/workflows"
        assert loaded.registries["dev"].type == RegistryType.path
        assert loaded.registries["dev"].source == "/dev/workflows"

    def test_save_creates_parent_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        deep = tmp_path / "a" / "b" / "c"
        monkeypatch.setenv("CONDUCTOR_HOME", str(deep))

        save_config(RegistriesConfig())
        assert (deep / "registries.toml").exists()

    def test_save_empty_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        save_config(RegistriesConfig())
        loaded = load_config()
        assert loaded.default is None
        assert loaded.registries == {}


# ---------------------------------------------------------------------------
# add_registry
# ---------------------------------------------------------------------------


class TestAddRegistry:
    def test_explicit_type(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        config = add_registry("myrepo", "some/source", registry_type=RegistryType.github)
        assert config.registries["myrepo"].type == RegistryType.github
        assert config.registries["myrepo"].source == "some/source"

    def test_infer_github_type(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        config = add_registry("gh", "owner/repo")
        assert config.registries["gh"].type == RegistryType.github

    def test_infer_path_type(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        config = add_registry("local", "/some/path/to/workflows")
        assert config.registries["local"].type == RegistryType.path

    def test_infer_path_for_absolute(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        config = add_registry("abs", "/home/user/my-workflows")
        assert config.registries["abs"].type == RegistryType.path

    def test_infer_path_for_deep_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        config = add_registry("deep", "a/b/c")
        assert config.registries["deep"].type == RegistryType.path

    def test_set_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        config = add_registry("main", "org/repo", set_default=True)
        assert config.default == "main"

    def test_duplicate_name_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        add_registry("dup", "owner/repo")
        with pytest.raises(RegistryError, match="already exists"):
            add_registry("dup", "other/repo")

    def test_persists_to_disk(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        add_registry("persisted", "org/workflows")
        loaded = load_config()
        assert "persisted" in loaded.registries


# ---------------------------------------------------------------------------
# remove_registry
# ---------------------------------------------------------------------------


class TestRemoveRegistry:
    def test_remove(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        add_registry("victim", "org/repo")
        config = remove_registry("victim")
        assert "victim" not in config.registries

    def test_remove_clears_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        add_registry("main", "org/repo", set_default=True)
        config = remove_registry("main")
        assert config.default is None

    def test_remove_preserves_other_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        add_registry("keep", "org/keep", set_default=True)
        add_registry("drop", "org/drop")
        config = remove_registry("drop")
        assert config.default == "keep"

    def test_remove_nonexistent_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        with pytest.raises(RegistryError, match="not found"):
            remove_registry("ghost")


# ---------------------------------------------------------------------------
# get_registry
# ---------------------------------------------------------------------------


class TestGetRegistry:
    def test_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        add_registry("findme", "org/repo")
        entry = get_registry("findme")
        assert entry.type == RegistryType.github
        assert entry.source == "org/repo"

    def test_not_found_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_config_home(tmp_path, monkeypatch)
        with pytest.raises(RegistryError, match="not found"):
            get_registry("nope")


# ---------------------------------------------------------------------------
# RegistriesConfig validation
# ---------------------------------------------------------------------------


class TestRegistriesConfigValidation:
    def test_default_must_exist_in_registries(self) -> None:
        with pytest.raises(ValueError, match="not defined in registries"):
            RegistriesConfig(default="missing", registries={})

    def test_valid_default(self) -> None:
        config = RegistriesConfig(
            default="ok",
            registries={"ok": RegistryEntry(type=RegistryType.github, source="a/b")},
        )
        assert config.default == "ok"

    def test_none_default_always_valid(self) -> None:
        config = RegistriesConfig(default=None, registries={})
        assert config.default is None
