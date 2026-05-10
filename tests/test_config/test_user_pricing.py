"""Tests for ``conductor.config.user_pricing``."""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.config.user_pricing import (
    USER_PRICING_ENV_VAR,
    get_user_pricing_path,
    load_user_pricing,
)
from conductor.exceptions import ConfigurationError

# --- get_user_pricing_path ---------------------------------------------------


def test_default_path_under_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(USER_PRICING_ENV_VAR, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert get_user_pricing_path() == tmp_path / ".conductor" / "pricing.yaml"


def test_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom.yaml"
    monkeypatch.setenv(USER_PRICING_ENV_VAR, str(custom))
    assert get_user_pricing_path() == custom


def test_env_var_expands_tilde(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # ``~`` is expanded by os.path.expanduser, which honors HOME on Unix
    # and USERPROFILE on Windows.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv(USER_PRICING_ENV_VAR, "~/custom.yaml")
    assert get_user_pricing_path() == tmp_path / "custom.yaml"


# --- load_user_pricing -------------------------------------------------------


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_user_pricing(tmp_path / "absent.yaml") == {}


def test_empty_file_returns_empty(tmp_path: Path) -> None:
    target = tmp_path / "p.yaml"
    target.write_text("", encoding="utf-8")
    assert load_user_pricing(target) == {}


def test_pricing_none_returns_empty(tmp_path: Path) -> None:
    """`pricing:` with no value parses as None — treat as no entries."""
    target = tmp_path / "p.yaml"
    target.write_text("pricing:\n", encoding="utf-8")
    assert load_user_pricing(target) == {}


def test_loads_entries(tmp_path: Path) -> None:
    target = tmp_path / "p.yaml"
    target.write_text(
        "pricing:\n"
        "  custom-model:\n"
        "    input_per_mtok: 1.5\n"
        "    output_per_mtok: 4.5\n"
        "    cache_read_per_mtok: 0.15\n"
        "    cache_write_per_mtok: 1.875\n",
        encoding="utf-8",
    )
    overrides = load_user_pricing(target)
    assert set(overrides) == {"custom-model"}
    pricing = overrides["custom-model"]
    assert pricing.input_per_mtok == 1.5
    assert pricing.output_per_mtok == 4.5
    assert pricing.cache_read_per_mtok == 0.15
    assert pricing.cache_write_per_mtok == 1.875


def test_cache_fields_default_to_zero(tmp_path: Path) -> None:
    target = tmp_path / "p.yaml"
    target.write_text(
        "pricing:\n  m:\n    input_per_mtok: 1\n    output_per_mtok: 2\n",
        encoding="utf-8",
    )
    pricing = load_user_pricing(target)["m"]
    assert pricing.cache_read_per_mtok == 0.0
    assert pricing.cache_write_per_mtok == 0.0


def test_invalid_yaml_raises_with_path(tmp_path: Path) -> None:
    target = tmp_path / "p.yaml"
    target.write_text("pricing:\n  bad: : :", encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_user_pricing(target)
    assert str(target) in str(exc.value)


def test_top_level_not_mapping_raises(tmp_path: Path) -> None:
    target = tmp_path / "p.yaml"
    target.write_text("- just a list\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="top-level `pricing:`"):
        load_user_pricing(target)


def test_missing_pricing_key_raises(tmp_path: Path) -> None:
    target = tmp_path / "p.yaml"
    target.write_text("other: value\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="top-level `pricing:`"):
        load_user_pricing(target)


def test_negative_price_rejected(tmp_path: Path) -> None:
    target = tmp_path / "p.yaml"
    target.write_text(
        "pricing:\n  m:\n    input_per_mtok: -1\n    output_per_mtok: 2\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="'m'"):
        load_user_pricing(target)


def test_missing_required_field_rejected(tmp_path: Path) -> None:
    target = tmp_path / "p.yaml"
    target.write_text(
        "pricing:\n  m:\n    input_per_mtok: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="'m'"):
        load_user_pricing(target)


def test_pricing_value_bool_rejected(tmp_path: Path) -> None:
    target = tmp_path / "p.yaml"
    target.write_text("pricing: true\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="bool"):
        load_user_pricing(target)


def test_pricing_value_list_rejected(tmp_path: Path) -> None:
    """An empty list at `pricing:` should not silently no-op — the user
    almost certainly meant a mapping."""
    target = tmp_path / "p.yaml"
    target.write_text("pricing: []\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="list"):
        load_user_pricing(target)


def test_non_string_model_key_rejected(tmp_path: Path) -> None:
    target = tmp_path / "p.yaml"
    target.write_text(
        "pricing:\n  123:\n    input_per_mtok: 1\n    output_per_mtok: 2\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="non-string"):
        load_user_pricing(target)


def test_load_error_suggests_bypass(tmp_path: Path) -> None:
    """The hard-error path must surface the CONDUCTOR_PRICING_FILE escape hatch."""
    target = tmp_path / "p.yaml"
    target.write_text("pricing:\n  bad: : :", encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_user_pricing(target)
    assert "CONDUCTOR_PRICING_FILE" in str(exc.value)


def test_load_uses_env_var_when_no_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "via-env.yaml"
    target.write_text(
        "pricing:\n  m:\n    input_per_mtok: 1\n    output_per_mtok: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(USER_PRICING_ENV_VAR, str(target))
    assert "m" in load_user_pricing()


def test_unreadable_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "p.yaml"
    target.write_text("pricing: {}\n", encoding="utf-8")

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    with pytest.raises(ConfigurationError, match="permission denied"):
        load_user_pricing(target)


# Ensure the env-var test cleanup is bulletproof in CI.
def test_env_var_unset_after_each_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(USER_PRICING_ENV_VAR, raising=False)
    import os

    assert USER_PRICING_ENV_VAR not in os.environ
