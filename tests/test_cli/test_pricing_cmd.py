"""Tests for ``conductor pricing`` CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.config.user_pricing import USER_PRICING_ENV_VAR

runner = CliRunner()


def test_path_prints_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(USER_PRICING_ENV_VAR, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = runner.invoke(app, ["pricing", "path"])
    assert result.exit_code == 0
    assert str(tmp_path / ".conductor" / "pricing.yaml") in result.stdout


def test_path_honors_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "elsewhere.yaml"
    monkeypatch.setenv(USER_PRICING_ENV_VAR, str(custom))
    result = runner.invoke(app, ["pricing", "path"])
    assert result.exit_code == 0
    assert str(custom) in result.stdout


def test_path_warns_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "absent.yaml"
    monkeypatch.setenv(USER_PRICING_ENV_VAR, str(custom))
    result = runner.invoke(app, ["pricing", "path"])
    assert result.exit_code == 0
    # stderr is merged into output for CliRunner by default in older click,
    # otherwise read it from .stderr. result.output is the full stream.
    combined = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "does not exist" in combined


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, ["pricing"])
    assert result.exit_code != 0  # typer convention for no_args_is_help
    assert "path" in result.stdout
