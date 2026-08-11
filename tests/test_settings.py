"""Tests for ``conductor.settings`` (Fleet Manager E5 — D3).

Mirrors ``tests/test_registry/test_config.py``'s structure since
``settings.py`` deliberately follows ``registry/config.py``'s precedent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.exceptions import ConductorError
from conductor.settings import (
    ConductorSettings,
    FleetRetentionSettings,
    get_settings_path,
    load_settings,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_settings_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CONDUCTOR_HOME at a temp directory and return its path."""
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# get_settings_path
# ---------------------------------------------------------------------------


class TestGetSettingsPath:
    def test_default_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CONDUCTOR_HOME", raising=False)
        path = get_settings_path()
        assert path == Path.home() / ".conductor" / "config.toml"

    def test_conductor_home_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "custom"))
        path = get_settings_path()
        assert path == tmp_path / "custom" / "config.toml"


# ---------------------------------------------------------------------------
# load_settings
# ---------------------------------------------------------------------------


class TestLoadSettings:
    def test_missing_file_returns_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_settings_home(tmp_path, monkeypatch)
        settings = load_settings()
        assert isinstance(settings, ConductorSettings)
        assert settings.fleet.retention.enabled is True
        assert settings.fleet.retention.keep_last == 200

    def test_conductor_home_redirection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config.toml under $CONDUCTOR_HOME is actually read from there."""
        home = _setup_settings_home(tmp_path, monkeypatch)
        (home / "config.toml").write_text("[fleet.retention]\nenabled = true\nkeep_last = 42\n")

        settings = load_settings()
        assert settings.fleet.retention.enabled is True
        assert settings.fleet.retention.keep_last == 42

    def test_fleet_retention_parsed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _setup_settings_home(tmp_path, monkeypatch)
        (home / "config.toml").write_text("[fleet.retention]\nenabled = true\nkeep_last = 5\n")

        settings = load_settings()
        assert settings.fleet.retention == FleetRetentionSettings(enabled=True, keep_last=5)

    def test_malformed_toml_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _setup_settings_home(tmp_path, monkeypatch)
        (home / "config.toml").write_text("not valid [[[toml")

        with pytest.raises(ConductorError, match="Failed to parse"):
            load_settings()

    def test_invalid_values_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _setup_settings_home(tmp_path, monkeypatch)
        (home / "config.toml").write_text('[fleet.retention]\nkeep_last = "not-a-number"\n')

        with pytest.raises(ConductorError, match="Invalid Conductor settings"):
            load_settings()

    def test_invalid_enabled_type_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_settings_home(tmp_path, monkeypatch)
        (home / "config.toml").write_text("[fleet.retention]\nenabled = 42\n")

        with pytest.raises(ConductorError, match="Invalid Conductor settings"):
            load_settings()

    def test_invalid_keep_last_bool_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``keep_last = true`` must not coerce to ``1`` -- a bool is a
        subclass of ``int`` and would otherwise pass a lax ``int`` field."""
        home = _setup_settings_home(tmp_path, monkeypatch)
        (home / "config.toml").write_text("[fleet.retention]\nkeep_last = true\n")

        with pytest.raises(ConductorError, match="Invalid Conductor settings"):
            load_settings()

    def test_extra_top_level_key_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown keys are ignored (no explicit extra='forbid'), matching
        RegistriesConfig's permissive precedent -- a settings file with a
        section for a *future* feature must not break an older Conductor
        build reading it."""
        home = _setup_settings_home(tmp_path, monkeypatch)
        (home / "config.toml").write_text('[some_future_section]\nfoo = "bar"\n')

        settings = load_settings()
        assert settings.fleet.retention.enabled is True


class TestMalformedSettingsSwallowedAtStartup:
    """E5-T5: malformed TOML raises for an explicit caller (like `fleet prune`
    would), but the opportunistic startup sweep must swallow it -- verified
    here at the settings layer by confirming load_settings() raises so a
    caller *can* catch it, and separately (in test_fleet/test_retention.py
    and the cli.run wiring) that the startup path actually does."""

    def test_load_settings_raises_so_caller_can_catch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_settings_home(tmp_path, monkeypatch)
        (home / "config.toml").write_text("not valid [[[toml")

        raised = False
        try:
            load_settings()
        except ConductorError:
            raised = True
        assert raised, "load_settings() must raise on malformed TOML"
