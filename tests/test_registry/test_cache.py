"""Tests for the registry cache layer."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.registry.cache import (
    clear_cache,
    fetch_workflow,
    get_cache_base,
    get_cached_workflow_path,
)
from conductor.registry.config import RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_conductor_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CONDUCTOR_HOME at a temp directory and return its path."""
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    return home


def _create_path_registry(tmp_path: Path) -> Path:
    """Create a minimal local path registry with one workflow and a sibling.

    Layout::

        tmp_path/my-registry/
            index.yaml
            workflows/
                qa-bot.yaml
                prompt.txt
    """
    registry_dir = tmp_path / "my-registry"
    wf_dir = registry_dir / "workflows"
    wf_dir.mkdir(parents=True)

    (registry_dir / "index.yaml").write_text(
        textwrap.dedent("""\
            workflows:
              qa-bot:
                description: "Simple Q&A"
                path: workflows/qa-bot.yaml
                versions: ["1.0.0", "2.0.0"]
        """),
        encoding="utf-8",
    )
    (wf_dir / "qa-bot.yaml").write_text("name: qa-bot\nagents: []\n", encoding="utf-8")
    (wf_dir / "prompt.txt").write_text("You are a helpful assistant.\n", encoding="utf-8")
    return registry_dir


class _FakeWorkflowInfo:
    """Minimal stand-in for WorkflowInfo returned by the index module."""

    def __init__(self, *, description: str, path: str, versions: list[str]) -> None:
        self.description = description
        self.path = path
        self.versions = versions


class _FakeIndex:
    """Minimal stand-in for RegistryIndex returned by load_index."""

    def __init__(self, workflows: dict[str, _FakeWorkflowInfo]) -> None:
        self.workflows = workflows


# ---------------------------------------------------------------------------
# get_cache_base
# ---------------------------------------------------------------------------


class TestGetCacheBase:
    def test_default_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CONDUCTOR_HOME", raising=False)
        result = get_cache_base()
        assert result == Path.home() / ".conductor" / "cache" / "registries"

    def test_conductor_home_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "custom"
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))
        result = get_cache_base()
        assert result == home / "cache" / "registries"


# ---------------------------------------------------------------------------
# get_cached_workflow_path
# ---------------------------------------------------------------------------


class TestGetCachedWorkflowPath:
    def test_returns_none_when_not_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        result = get_cached_workflow_path("myregistry", "qa-bot", "1.0.0")
        assert result is None

    def test_returns_path_when_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        version_dir = home / "cache" / "registries" / "myregistry" / "qa-bot" / "1.0.0"
        version_dir.mkdir(parents=True)
        wf_file = version_dir / "qa-bot.yaml"
        wf_file.write_text("name: qa-bot\n", encoding="utf-8")

        result = get_cached_workflow_path("myregistry", "qa-bot", "1.0.0")
        assert result is not None
        assert result == wf_file

    def test_returns_none_for_empty_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        version_dir = home / "cache" / "registries" / "myregistry" / "qa-bot" / "1.0.0"
        version_dir.mkdir(parents=True)
        # directory exists but has no YAML files
        result = get_cached_workflow_path("myregistry", "qa-bot", "1.0.0")
        assert result is None


# ---------------------------------------------------------------------------
# fetch_workflow — path registry
# ---------------------------------------------------------------------------


class TestFetchWorkflowPath:
    def _make_index(self) -> _FakeIndex:
        return _FakeIndex(
            workflows={
                "qa-bot": _FakeWorkflowInfo(
                    description="Simple Q&A",
                    path="workflows/qa-bot.yaml",
                    versions=["1.0.0", "2.0.0"],
                ),
            }
        )

    @patch("conductor.registry.cache.load_index")
    def test_returns_source_path_directly(
        self, mock_load_index: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path registries return the source file directly (no caching)."""
        _setup_conductor_home(tmp_path, monkeypatch)
        registry_dir = _create_path_registry(tmp_path)
        mock_load_index.return_value = self._make_index()  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.path, source=str(registry_dir))
        result = fetch_workflow("local", entry, "qa-bot", version="1.0.0")

        assert result.exists()
        assert result.name == "qa-bot.yaml"
        # Should point to the source directory, not the cache
        assert str(result).startswith(str(registry_dir))

    @patch("conductor.registry.cache.load_index")
    def test_version_is_ignored(
        self, mock_load_index: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path registries ignore the version — same file regardless."""
        _setup_conductor_home(tmp_path, monkeypatch)
        registry_dir = _create_path_registry(tmp_path)
        mock_load_index.return_value = self._make_index()  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.path, source=str(registry_dir))

        v1 = fetch_workflow("local", entry, "qa-bot", version="1.0.0")
        v2 = fetch_workflow("local", entry, "qa-bot", version="2.0.0")
        latest = fetch_workflow("local", entry, "qa-bot", version=None)

        # All return the same source file
        assert v1 == v2 == latest

    @patch("conductor.registry.cache.load_index")
    def test_edits_reflected_immediately(
        self, mock_load_index: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Changes to the source file are visible without cache refresh."""
        _setup_conductor_home(tmp_path, monkeypatch)
        registry_dir = _create_path_registry(tmp_path)
        mock_load_index.return_value = self._make_index()  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.path, source=str(registry_dir))
        result = fetch_workflow("local", entry, "qa-bot", version="1.0.0")

        original = result.read_text()
        result.write_text(original + "\n# edited")

        # Re-fetch returns the same path with the edit visible
        result2 = fetch_workflow("local", entry, "qa-bot", version="1.0.0")
        assert "# edited" in result2.read_text()

    @patch("conductor.registry.cache.load_index")
    def test_missing_workflow_raises(
        self, mock_load_index: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        mock_load_index.return_value = _FakeIndex(workflows={})  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.path, source=str(tmp_path))
        with pytest.raises(RegistryError, match="not found"):
            fetch_workflow("local", entry, "nonexistent", version="1.0.0")

    @patch("conductor.registry.cache.load_index")
    def test_missing_source_file_raises(
        self, mock_load_index: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        mock_load_index.return_value = self._make_index()  # type: ignore[union-attr]

        empty_registry = tmp_path / "empty-registry"
        empty_registry.mkdir()
        entry = RegistryEntry(type=RegistryType.path, source=str(empty_registry))

        with pytest.raises(RegistryError, match="not found"):
            fetch_workflow("local", entry, "qa-bot", version="1.0.0")


# ---------------------------------------------------------------------------
# fetch_workflow — GitHub registry
# ---------------------------------------------------------------------------


class TestFetchWorkflowGitHub:
    def _make_index(self) -> _FakeIndex:
        return _FakeIndex(
            workflows={
                "qa-bot": _FakeWorkflowInfo(
                    description="Simple Q&A",
                    path="workflows/qa-bot.yaml",
                    versions=["1.0.0"],
                ),
            }
        )

    @patch("conductor.registry.cache.fetch_file")
    @patch("conductor.registry.cache.list_directory")
    @patch("conductor.registry.cache.parse_github_source", return_value=("myorg", "workflows"))
    @patch("conductor.registry.cache.load_index")
    def test_fetches_from_github(
        self,
        mock_load_index: object,
        mock_parse: object,
        mock_list_dir: object,
        mock_fetch_file: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        mock_load_index.return_value = self._make_index()  # type: ignore[union-attr]
        mock_list_dir.return_value = [  # type: ignore[union-attr]
            "qa-bot.yaml",
            "prompt.txt",
        ]

        def fake_fetch(owner: str, repo: str, path: str, *, ref: str) -> bytes:
            if path.endswith("qa-bot.yaml"):
                return b"name: qa-bot\nagents: []\n"
            return b"You are a helpful assistant.\n"

        mock_fetch_file.side_effect = fake_fetch  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow("official", entry, "qa-bot", version="1.0.0")

        assert result.exists()
        assert result.name == "qa-bot.yaml"
        assert "1.0.0" in str(result)

        # Sibling should be cached
        sibling = result.parent / "prompt.txt"
        assert sibling.exists()

        mock_parse.assert_called_once_with("myorg/workflows")  # type: ignore[union-attr]

    @patch("conductor.registry.cache.fetch_file")
    @patch("conductor.registry.cache.list_directory")
    @patch("conductor.registry.cache.parse_github_source", return_value=("myorg", "workflows"))
    @patch("conductor.registry.cache.load_index")
    def test_cached_github_not_refetched(
        self,
        mock_load_index: object,
        mock_parse: object,
        mock_list_dir: object,
        mock_fetch_file: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_load_index.return_value = self._make_index()  # type: ignore[union-attr]

        # Pre-populate the cache
        version_dir = home / "cache" / "registries" / "official" / "qa-bot" / "1.0.0"
        version_dir.mkdir(parents=True)
        (version_dir / "qa-bot.yaml").write_text("name: qa-bot\n", encoding="utf-8")

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow("official", entry, "qa-bot", version="1.0.0")

        assert result.exists()
        # fetch_file should never have been called — cache hit
        mock_fetch_file.assert_not_called()  # type: ignore[union-attr]
        mock_list_dir.assert_not_called()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# clear_cache
# ---------------------------------------------------------------------------


class TestClearCache:
    def test_clear_all(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        cache_base = home / "cache" / "registries"

        # Create two registries in the cache
        (cache_base / "reg-a" / "wf" / "1.0").mkdir(parents=True)
        (cache_base / "reg-b" / "wf" / "2.0").mkdir(parents=True)

        clear_cache()

        assert not cache_base.exists()

    def test_clear_specific_registry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        cache_base = home / "cache" / "registries"

        (cache_base / "reg-a" / "wf" / "1.0").mkdir(parents=True)
        (cache_base / "reg-b" / "wf" / "2.0").mkdir(parents=True)

        clear_cache(registry_name="reg-a")

        assert not (cache_base / "reg-a").exists()
        assert (cache_base / "reg-b").exists()

    def test_clear_nonexistent_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        # Should not raise
        clear_cache(registry_name="does-not-exist")
        clear_cache()
