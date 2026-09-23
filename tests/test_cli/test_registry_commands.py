"""Tests for the ``conductor registry`` CLI subcommand group."""

from __future__ import annotations

import importlib
import io
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.console import make_console
from conductor.registry.config import RegistriesConfig, RegistryEntry, RegistryType, save_config
from conductor.registry.index import RegistryIndex, WorkflowInfo

runner = CliRunner()
registry_module = importlib.import_module("conductor.cli.registry")


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


class TestListEncoding:
    @staticmethod
    def _save_registries(*, default: str | None) -> None:
        save_config(
            RegistriesConfig(
                default=default,
                registries={
                    "primary": RegistryEntry(type=RegistryType.github, source="acme/primary"),
                    "secondary": RegistryEntry(type=RegistryType.github, source="acme/secondary"),
                },
            )
        )

    @staticmethod
    def _list_on_stream(
        monkeypatch: pytest.MonkeyPatch, encoding: str, *, errors: str = "strict"
    ) -> str:
        monkeypatch.setenv("CONDUCTOR_NO_UPDATE_CHECK", "1")
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding=encoding, errors=errors, newline="")
        monkeypatch.setattr(registry_module, "output_console", make_console(file=stream, width=200))
        result = runner.invoke(app, ["registry", "list"])
        stream.flush()
        assert result.exception is None, repr(result.exception)
        assert result.exit_code == 0
        return buffer.getvalue().decode(encoding)

    @pytest.mark.parametrize(
        ("encoding", "errors", "marker"),
        [
            ("cp1252", "strict", "OK"),
            ("cp1252", "surrogateescape", "OK"),
            ("ascii", "strict", "OK"),
            ("utf-8", "strict", "✓"),
            ("gb18030", "strict", "✓"),
        ],
    )
    def test_list_default_marker_uses_output_encoding(
        self, monkeypatch: pytest.MonkeyPatch, encoding: str, errors: str, marker: str
    ) -> None:
        self._save_registries(default="primary")
        output = self._list_on_stream(monkeypatch, encoding, errors=errors)
        primary = next(line for line in output.splitlines() if "acme/primary" in line)
        secondary = next(line for line in output.splitlines() if "acme/secondary" in line)
        assert "primary" in primary and "github" in primary
        assert "secondary" in secondary and "github" in secondary
        assert primary.rsplit(primary.lstrip()[0], 2)[1].strip() == marker
        assert output.count(marker) == 1
        assert secondary.rsplit(secondary.lstrip()[0], 2)[1].strip() == ""

    def test_list_without_default_has_blank_markers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._save_registries(default=None)
        output = self._list_on_stream(monkeypatch, "cp1252")
        for source in ("acme/primary", "acme/secondary"):
            row = next(line for line in output.splitlines() if source in line)
            assert row.rsplit(row.lstrip()[0], 2)[1].strip() == ""
        assert "OK" not in output and "✓" not in output

    def test_list_resolves_marker_again_for_each_console(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._save_registries(default="primary")
        for encoding, marker in (("cp1252", "OK"), ("utf-8", "✓"), ("cp1252", "OK")):
            output = self._list_on_stream(monkeypatch, encoding)
            assert output.count(marker) == 1


# ---------------------------------------------------------------------------
# list <name> (workflows)
# ---------------------------------------------------------------------------


class TestListWorkflows:
    """Listing workflows in a specific registry (index mocked)."""

    def test_list_workflows(self) -> None:
        runner.invoke(app, ["registry", "add", "team", "acme/workflows"])

        mock_index = RegistryIndex(
            workflows={
                "qa-bot": WorkflowInfo(description="QA helper", path="qa/bot.yaml"),
                "summarizer": WorkflowInfo(description="Summarize docs", path="summarizer.yaml"),
            }
        )

        with (
            patch("conductor.cli.registry.load_index", return_value=mock_index),
            patch(
                "conductor.cli.registry.list_tags",
                return_value=["v2.0.0", "v1.5.0", "v1.4.0", "v1.3.0", "v1.2.0", "v1.1.0"],
            ),
        ):
            result = runner.invoke(app, ["registry", "list", "team"])

        assert result.exit_code == 0
        assert "qa-bot" in result.output
        assert "summarizer" in result.output
        assert "Latest tags:" in result.output
        assert "v2.0.0" in result.output
        assert "..." in result.output  # >5 tags → truncation indicator

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
                "qa-bot": WorkflowInfo(description="QA helper", path="qa/bot.yaml"),
            }
        )

        with (
            patch("conductor.cli.registry.load_index", return_value=mock_index),
            patch("conductor.cli.registry.list_tags", return_value=["v1.0.0"]),
        ):
            result = runner.invoke(app, ["registry", "show", "team"])

        assert result.exit_code == 0
        assert "team" in result.output
        assert "acme/workflows" in result.output
        assert "qa-bot" in result.output
        assert "QA helper" in result.output
        assert "Latest tags:" in result.output
        assert "v1.0.0" in result.output
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


# ---------------------------------------------------------------------------
# update: tmp dir pruning
# ---------------------------------------------------------------------------


class TestUpdatePrunesTempDirs:
    def test_update_prunes_temp_dirs(
        self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pathlib import Path

        runner.invoke(app, ["registry", "add", "team", "acme/workflows"])

        # Drop a .tmp-* dir under the cache layout for this registry.
        cache_base = Path(str(tmp_path)) / "cache" / "registries"
        orphan = cache_base / "team" / "wf" / ".tmp-orphan"
        orphan.mkdir(parents=True)

        from conductor.registry.index import RegistryIndex

        # Mock clear_cache as a no-op so the orphan survives until prune_temp_dirs runs.
        with (
            patch("conductor.cli.registry.clear_cache"),
            patch("conductor.cli.registry.load_index", return_value=RegistryIndex(workflows={})),
        ):
            result = runner.invoke(app, ["registry", "update", "team"])

        assert result.exit_code == 0
        assert not orphan.exists()
        assert "Pruned 1 stale .tmp-* directories." in result.output

    def test_update_no_prune_message_when_zero(
        self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner.invoke(app, ["registry", "add", "team", "acme/workflows"])

        from conductor.registry.index import RegistryIndex

        with patch("conductor.cli.registry.load_index", return_value=RegistryIndex(workflows={})):
            result = runner.invoke(app, ["registry", "update", "team"])

        assert result.exit_code == 0
        assert "Pruned" not in result.output


# ---------------------------------------------------------------------------
# _format_latest_tags: error surfacing
# ---------------------------------------------------------------------------


class TestFormatLatestTagsErrors:
    def test_format_latest_tags_surfaces_registry_error_message(self) -> None:
        from conductor.registry.errors import RegistryError
        from conductor.registry.index import RegistryIndex, WorkflowInfo

        runner.invoke(app, ["registry", "add", "team", "acme/workflows"])

        mock_index = RegistryIndex(
            workflows={"qa-bot": WorkflowInfo(description="QA helper", path="qa/bot.yaml")},
        )

        with (
            patch("conductor.cli.registry.load_index", return_value=mock_index),
            patch(
                "conductor.cli.registry.list_tags",
                side_effect=RegistryError("rate limit"),
            ),
        ):
            result = runner.invoke(app, ["registry", "list", "team"])

        assert result.exit_code == 0
        assert "rate limit" in result.output
        assert "unavailable" in result.output

    def test_format_latest_tags_surfaces_http_error_class(self) -> None:
        import httpx

        from conductor.registry.index import RegistryIndex, WorkflowInfo

        runner.invoke(app, ["registry", "add", "team", "acme/workflows"])

        mock_index = RegistryIndex(
            workflows={"qa-bot": WorkflowInfo(description="QA helper", path="qa/bot.yaml")},
        )

        with (
            patch("conductor.cli.registry.load_index", return_value=mock_index),
            patch(
                "conductor.cli.registry.list_tags",
                side_effect=httpx.ConnectError("boom"),
            ),
        ):
            result = runner.invoke(app, ["registry", "list", "team"])

        assert result.exit_code == 0
        assert "ConnectError" in result.output
