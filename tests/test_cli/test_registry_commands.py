"""Tests for the ``conductor registry`` CLI subcommand group."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.registry.index import RegistryIndex, WorkflowInfo

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:  # type: ignore[type-arg]
    """Point CONDUCTOR_HOME to a temp directory so tests don't touch real config."""
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


class TestRegistryHelp:
    """Verify the registry subcommand group is wired up."""

    def test_registry_help(self) -> None:
        result = runner.invoke(app, ["registry", "--help"])
        assert result.exit_code == 0
        assert "registry" in result.output.lower()
        # All subcommands should be listed
        for cmd in ("list", "add", "remove", "set-default", "update", "show"):
            assert cmd in result.output


# ---------------------------------------------------------------------------
# list (no registries)
# ---------------------------------------------------------------------------


class TestListEmpty:
    """Listing when no registries are configured."""

    def test_list_no_registries(self) -> None:
        result = runner.invoke(app, ["registry", "list"])
        assert result.exit_code == 0
        assert "No registries configured" in result.output


# ---------------------------------------------------------------------------
# add / list
# ---------------------------------------------------------------------------


class TestAddAndList:
    """Adding registries and listing them."""

    def test_add_registry(self) -> None:
        result = runner.invoke(app, ["registry", "add", "team", "acme/workflows"])
        assert result.exit_code == 0
        assert "team" in result.output
        assert "added" in result.output

    def test_add_with_default(self) -> None:
        result = runner.invoke(app, ["registry", "add", "team", "acme/workflows", "--default"])
        assert result.exit_code == 0
        assert "default" in result.output.lower()

    def test_add_duplicate_name(self) -> None:
        runner.invoke(app, ["registry", "add", "dup", "acme/workflows"])
        result = runner.invoke(app, ["registry", "add", "dup", "acme/other"])
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_list_after_add(self) -> None:
        runner.invoke(app, ["registry", "add", "myteam", "acme/workflows", "--default"])
        result = runner.invoke(app, ["registry", "list"])
        assert result.exit_code == 0
        assert "myteam" in result.output
        assert "acme/workflows" in result.output
        assert "✓" in result.output  # default marker


# ---------------------------------------------------------------------------
# list <name> (workflows)
# ---------------------------------------------------------------------------


class TestListWorkflows:
    """Listing workflows in a specific registry (index mocked)."""

    def test_list_workflows(self) -> None:
        runner.invoke(app, ["registry", "add", "team", "acme/workflows"])

        mock_index = RegistryIndex(
            workflows={
                "qa-bot": WorkflowInfo(
                    description="QA helper", path="qa/bot.yaml", versions=["1.0", "1.1"]
                ),
                "summarizer": WorkflowInfo(
                    description="Summarize docs", path="summarizer.yaml", versions=["2.0"]
                ),
            }
        )

        with patch("conductor.cli.registry.load_index", return_value=mock_index):
            result = runner.invoke(app, ["registry", "list", "team"])

        assert result.exit_code == 0
        assert "qa-bot" in result.output
        assert "summarizer" in result.output
        assert "1.0" in result.output

    def test_list_workflows_unknown_registry(self) -> None:
        result = runner.invoke(app, ["registry", "list", "nope"])
        assert result.exit_code == 1
        assert "not found" in result.output


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


class TestRemove:
    """Removing registries."""

    def test_remove_existing(self) -> None:
        runner.invoke(app, ["registry", "add", "removeme", "acme/workflows"])
        result = runner.invoke(app, ["registry", "remove", "removeme"])
        assert result.exit_code == 0
        assert "removed" in result.output

        # Should be gone now
        result = runner.invoke(app, ["registry", "list"])
        assert "removeme" not in result.output

    def test_remove_nonexistent(self) -> None:
        result = runner.invoke(app, ["registry", "remove", "ghost"])
        assert result.exit_code == 1
        assert "not found" in result.output


# ---------------------------------------------------------------------------
# set-default
# ---------------------------------------------------------------------------


class TestSetDefault:
    """Setting the default registry."""

    def test_set_default(self) -> None:
        runner.invoke(app, ["registry", "add", "first", "acme/a"])
        runner.invoke(app, ["registry", "add", "second", "acme/b"])
        result = runner.invoke(app, ["registry", "set-default", "second"])
        assert result.exit_code == 0
        assert "second" in result.output

        # Verify via list
        result = runner.invoke(app, ["registry", "list"])
        assert "✓" in result.output

    def test_set_default_nonexistent(self) -> None:
        result = runner.invoke(app, ["registry", "set-default", "nope"])
        assert result.exit_code == 1
        assert "not found" in result.output


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


class TestShow:
    """Showing registry details."""

    def test_show_registry(self) -> None:
        runner.invoke(app, ["registry", "add", "team", "acme/workflows", "--default"])

        mock_index = RegistryIndex(
            workflows={
                "qa-bot": WorkflowInfo(
                    description="QA helper", path="qa/bot.yaml", versions=["1.0", "1.1"]
                ),
            }
        )

        with patch("conductor.cli.registry.load_index", return_value=mock_index):
            result = runner.invoke(app, ["registry", "show", "team"])

        assert result.exit_code == 0
        assert "team" in result.output
        assert "acme/workflows" in result.output
        assert "qa-bot" in result.output
        assert "QA helper" in result.output
        assert "conductor show" in result.output

    def test_show_unknown_registry(self) -> None:
        result = runner.invoke(app, ["registry", "show", "missing"])

        assert result.exit_code == 1
        assert "not found" in result.output


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


class TestUpdate:
    """Updating registry indexes."""

    def test_update_single(self) -> None:
        runner.invoke(app, ["registry", "add", "team", "acme/workflows"])

        mock_index = RegistryIndex(workflows={})

        with (
            patch("conductor.cli.registry.clear_cache") as mock_clear,
            patch("conductor.cli.registry.load_index", return_value=mock_index),
        ):
            result = runner.invoke(app, ["registry", "update", "team"])

        assert result.exit_code == 0
        assert "updated" in result.output
        mock_clear.assert_called_once_with("team")

    def test_update_all(self) -> None:
        runner.invoke(app, ["registry", "add", "a", "acme/a"])
        runner.invoke(app, ["registry", "add", "b", "acme/b"])

        mock_index = RegistryIndex(workflows={})

        with (
            patch("conductor.cli.registry.clear_cache") as mock_clear,
            patch("conductor.cli.registry.load_index", return_value=mock_index),
        ):
            result = runner.invoke(app, ["registry", "update"])

        assert result.exit_code == 0
        mock_clear.assert_called_once_with()  # no args = clear all

    def test_update_nonexistent(self) -> None:
        result = runner.invoke(app, ["registry", "update", "nope"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_update_no_registries(self) -> None:
        result = runner.invoke(app, ["registry", "update"])
        assert result.exit_code == 0
        assert "No registries configured" in result.output
