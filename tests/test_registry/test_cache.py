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

# A canned 40-char hex SHA used throughout these tests.
_FAKE_SHA = "a" * 40
_FAKE_SHA2 = "b" * 40
_SHA_DIR = _FAKE_SHA[:12]
_SHA_DIR2 = _FAKE_SHA2[:12]


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
        """),
        encoding="utf-8",
    )
    (wf_dir / "qa-bot.yaml").write_text("name: qa-bot\nagents: []\n", encoding="utf-8")
    (wf_dir / "prompt.txt").write_text("You are a helpful assistant.\n", encoding="utf-8")
    return registry_dir


class _FakeWorkflowInfo:
    """Minimal stand-in for WorkflowInfo returned by the index module."""

    def __init__(self, *, description: str, path: str) -> None:
        self.description = description
        self.path = path


class _FakeIndex:
    """Minimal stand-in for RegistryIndex returned by load_index."""

    def __init__(self, workflows: dict[str, _FakeWorkflowInfo]) -> None:
        self.workflows = workflows


def _make_index() -> _FakeIndex:
    return _FakeIndex(
        workflows={
            "qa-bot": _FakeWorkflowInfo(
                description="Simple Q&A",
                path="workflows/qa-bot.yaml",
            ),
        }
    )


def _write_workflow_file(dest_dir: Path) -> None:
    """Helper: simulate _fetch_github writing the workflow file."""
    (dest_dir / "qa-bot.yaml").write_bytes(b"name: qa-bot\nagents: []\n")


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
        result = get_cached_workflow_path("myregistry", "qa-bot", _FAKE_SHA)
        assert result is None

    def test_returns_path_when_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        sha_dir = home / "cache" / "registries" / "myregistry" / "qa-bot" / _SHA_DIR
        sha_dir.mkdir(parents=True)
        wf_file = sha_dir / "qa-bot.yaml"
        wf_file.write_text("name: qa-bot\n", encoding="utf-8")

        result = get_cached_workflow_path("myregistry", "qa-bot", _FAKE_SHA)
        assert result is not None
        assert result == wf_file

    def test_uses_first_12_chars_of_sha(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cache directory name is sha[:12]; full SHA is accepted for lookup."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        sha = "0123456789abcdef" * 2 + "01234567"  # 40 chars
        sha_dir = home / "cache" / "registries" / "myregistry" / "qa-bot" / sha[:12]
        sha_dir.mkdir(parents=True)
        (sha_dir / "qa-bot.yaml").write_text("name: qa-bot\n", encoding="utf-8")

        result = get_cached_workflow_path("myregistry", "qa-bot", sha)
        assert result is not None
        assert sha[:12] in str(result)

    def test_returns_none_for_empty_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        sha_dir = home / "cache" / "registries" / "myregistry" / "qa-bot" / _SHA_DIR
        sha_dir.mkdir(parents=True)
        # directory exists but has no YAML files
        result = get_cached_workflow_path("myregistry", "qa-bot", _FAKE_SHA)
        assert result is None


# ---------------------------------------------------------------------------
# fetch_workflow — path registry
# ---------------------------------------------------------------------------


class TestFetchWorkflowPath:
    @patch("conductor.registry.cache.load_index")
    def test_returns_source_path_directly(
        self, mock_load_index: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path registries return the source file directly (no caching)."""
        _setup_conductor_home(tmp_path, monkeypatch)
        registry_dir = _create_path_registry(tmp_path)
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.path, source=str(registry_dir))
        result = fetch_workflow("local", entry, "qa-bot")

        assert result.exists()
        assert result.name == "qa-bot.yaml"
        # Should point to the source directory, not the cache
        assert str(result).startswith(str(registry_dir))

    @patch("conductor.registry.cache.load_index")
    def test_no_ref_returns_source(
        self, mock_load_index: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path registries with ref=None succeed and return the source file."""
        _setup_conductor_home(tmp_path, monkeypatch)
        registry_dir = _create_path_registry(tmp_path)
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.path, source=str(registry_dir))
        result = fetch_workflow("local", entry, "qa-bot", ref=None)
        assert result.exists()

    @patch("conductor.registry.cache.load_index")
    def test_path_registry_with_ref_raises(
        self, mock_load_index: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path registries reject any non-empty ref."""
        _setup_conductor_home(tmp_path, monkeypatch)
        registry_dir = _create_path_registry(tmp_path)
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.path, source=str(registry_dir))
        with pytest.raises(RegistryError, match="Path registries do not support refs"):
            fetch_workflow("local", entry, "qa-bot", ref="v1.0.0")

    @patch("conductor.registry.cache.load_index")
    def test_edits_reflected_immediately(
        self, mock_load_index: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Changes to the source file are visible without cache refresh."""
        _setup_conductor_home(tmp_path, monkeypatch)
        registry_dir = _create_path_registry(tmp_path)
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.path, source=str(registry_dir))
        result = fetch_workflow("local", entry, "qa-bot")

        original = result.read_text()
        result.write_text(original + "\n# edited")

        result2 = fetch_workflow("local", entry, "qa-bot")
        assert "# edited" in result2.read_text()

    @patch("conductor.registry.cache.load_index")
    def test_missing_workflow_raises(
        self, mock_load_index: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        mock_load_index.return_value = _FakeIndex(workflows={})  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.path, source=str(tmp_path))
        with pytest.raises(RegistryError, match="not found"):
            fetch_workflow("local", entry, "nonexistent")

    @patch("conductor.registry.cache.load_index")
    def test_missing_source_file_raises(
        self, mock_load_index: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]

        empty_registry = tmp_path / "empty-registry"
        empty_registry.mkdir()
        entry = RegistryEntry(type=RegistryType.path, source=str(empty_registry))

        with pytest.raises(RegistryError, match="not found"):
            fetch_workflow("local", entry, "qa-bot")


# ---------------------------------------------------------------------------
# fetch_workflow — GitHub registry
# ---------------------------------------------------------------------------


class TestFetchWorkflowGitHub:
    @patch("conductor.registry.cache._fetch_github")
    @patch("conductor.registry.cache.load_index")
    @patch("conductor.registry.cache.materialize_to_sha")
    @patch("conductor.registry.cache.resolve_ref")
    def test_fetches_from_github(
        self,
        mock_resolve_ref: object,
        mock_materialize: object,
        mock_load_index: object,
        mock_fetch_github: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Happy path: ref → SHA → cache miss → fetch → file present."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "v1.0.0"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]
        mock_fetch_github.side_effect = (  # type: ignore[union-attr]
            lambda entry, path, sha, dest_dir: _write_workflow_file(dest_dir)
        )

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow("official", entry, "qa-bot", ref="v1.0.0")

        assert result.exists()
        assert result.name == "qa-bot.yaml"
        # Cache directory uses sha[:12]
        assert _SHA_DIR in str(result)
        expected_dir = home / "cache" / "registries" / "official" / "qa-bot" / _SHA_DIR
        assert result.parent == expected_dir

        # load_index should be pinned to the SHA
        mock_load_index.assert_called_once()  # type: ignore[union-attr]
        call_kwargs = mock_load_index.call_args.kwargs  # type: ignore[union-attr]
        assert call_kwargs.get("ref") == _FAKE_SHA

        # _fetch_github called with the SHA (not the ref name)
        mock_fetch_github.assert_called_once()  # type: ignore[union-attr]
        args = mock_fetch_github.call_args.args  # type: ignore[union-attr]
        assert args[2] == _FAKE_SHA  # sha positional arg

    @patch("conductor.registry.cache._fetch_github")
    @patch("conductor.registry.cache.load_index")
    @patch("conductor.registry.cache.materialize_to_sha")
    @patch("conductor.registry.cache.resolve_ref")
    def test_cache_hit_skips_fetch(
        self,
        mock_resolve_ref: object,
        mock_materialize: object,
        mock_load_index: object,
        mock_fetch_github: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When cache directory already exists for the SHA, skip the fetch."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "v1.0.0"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]

        # Pre-populate the cache at the resolved SHA dir.
        sha_dir = home / "cache" / "registries" / "official" / "qa-bot" / _SHA_DIR
        sha_dir.mkdir(parents=True)
        (sha_dir / "qa-bot.yaml").write_text("name: qa-bot\n", encoding="utf-8")

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow("official", entry, "qa-bot", ref="v1.0.0")

        assert result.exists()
        assert result.parent == sha_dir
        # Fetch and index load were not invoked — pure cache hit.
        mock_fetch_github.assert_not_called()  # type: ignore[union-attr]
        mock_load_index.assert_not_called()  # type: ignore[union-attr]

    @patch("conductor.registry.cache._fetch_github")
    @patch("conductor.registry.cache.load_index")
    @patch("conductor.registry.cache.materialize_to_sha")
    @patch("conductor.registry.cache.resolve_ref")
    def test_atomic_write_on_failure(
        self,
        mock_resolve_ref: object,
        mock_materialize: object,
        mock_load_index: object,
        mock_fetch_github: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If fetch fails mid-write, final dir is not created and tmp dir is cleaned up."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "v1.0.0"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]

        def boom(entry: object, path: str, sha: str, dest_dir: Path) -> None:
            # Simulate partial write before failure
            (dest_dir / "partial.yaml").write_bytes(b"oops")
            raise RuntimeError("network blew up")

        mock_fetch_github.side_effect = boom  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        with pytest.raises(RuntimeError, match="network blew up"):
            fetch_workflow("official", entry, "qa-bot", ref="v1.0.0")

        # Final dir was never created.
        final_dir = home / "cache" / "registries" / "official" / "qa-bot" / _SHA_DIR
        assert not final_dir.exists()

        # Workflow parent exists (mkdir runs before fetch) but contains no
        # leftover .tmp-* directories.
        parent = home / "cache" / "registries" / "official" / "qa-bot"
        assert parent.exists()
        leftovers = [p for p in parent.iterdir() if p.name.startswith(".tmp-")]
        assert leftovers == []

    @patch("conductor.registry.cache._fetch_github")
    @patch("conductor.registry.cache.load_index")
    @patch("conductor.registry.cache.materialize_to_sha")
    @patch("conductor.registry.cache.resolve_ref")
    def test_branch_ref_re_resolution(
        self,
        mock_resolve_ref: object,
        mock_materialize: object,
        mock_load_index: object,
        mock_fetch_github: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Calling fetch twice with the same branch ref re-resolves the SHA each time.

        When the underlying branch advances between calls (materialize_to_sha
        returns different SHAs), the second fetch must populate a *new* cache
        dir rather than reuse the old one.
        """
        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "main"  # type: ignore[union-attr]
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]
        mock_fetch_github.side_effect = (  # type: ignore[union-attr]
            lambda entry, path, sha, dest_dir: _write_workflow_file(dest_dir)
        )

        # Branch advances between calls.
        mock_materialize.side_effect = [_FAKE_SHA, _FAKE_SHA2]  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")

        first = fetch_workflow("official", entry, "qa-bot", ref="main")
        second = fetch_workflow("official", entry, "qa-bot", ref="main")

        # Each call resolved to a different SHA → different cache dirs.
        assert first != second
        assert _SHA_DIR in str(first)
        assert _SHA_DIR2 in str(second)

        first_dir = home / "cache" / "registries" / "official" / "qa-bot" / _SHA_DIR
        second_dir = home / "cache" / "registries" / "official" / "qa-bot" / _SHA_DIR2
        assert first_dir.exists()
        assert second_dir.exists()

        # Both fetches actually executed (no spurious cache reuse).
        assert mock_fetch_github.call_count == 2  # type: ignore[union-attr]
        assert mock_materialize.call_count == 2  # type: ignore[union-attr]

    @patch("conductor.registry.cache._fetch_github")
    @patch("conductor.registry.cache.load_index")
    @patch("conductor.registry.cache.materialize_to_sha")
    @patch("conductor.registry.cache.resolve_ref")
    def test_race_on_rename_uses_existing_cache(
        self,
        mock_resolve_ref: object,
        mock_materialize: object,
        mock_load_index: object,
        mock_fetch_github: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If another process wins the race, our tmp dir is cleaned up and the
        cached path is returned successfully."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "v1.0.0"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]

        parent = home / "cache" / "registries" / "official" / "qa-bot"
        final_dir = parent / _SHA_DIR

        def racing_fetch(entry: object, path: str, sha: str, dest_dir: Path) -> None:
            # Write our own workflow file into the temp dir.
            _write_workflow_file(dest_dir)
            # Simulate another process creating the final dir before we rename.
            final_dir.mkdir(parents=True, exist_ok=True)
            (final_dir / "qa-bot.yaml").write_bytes(b"name: qa-bot\n# from racer\n")

        mock_fetch_github.side_effect = racing_fetch  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow("official", entry, "qa-bot", ref="v1.0.0")

        # The cached path (populated by the racer) is returned.
        assert result.exists()
        assert result.parent == final_dir
        assert b"from racer" in result.read_bytes()

        # Our temp dir was cleaned up — no .tmp-* residue.
        leftovers = [p for p in parent.iterdir() if p.name.startswith(".tmp-")]
        assert leftovers == []

    @patch("conductor.registry.cache._fetch_github")
    @patch("conductor.registry.cache.load_index")
    @patch("conductor.registry.cache.materialize_to_sha")
    @patch("conductor.registry.cache.resolve_ref")
    def test_missing_workflow_after_fetch_raises(
        self,
        mock_resolve_ref: object,
        mock_materialize: object,
        mock_load_index: object,
        mock_fetch_github: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the index points to a file that wasn't written, raise RegistryError."""
        _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "v1.0.0"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]
        # Fetch that does not write the expected workflow file.
        mock_fetch_github.side_effect = (  # type: ignore[union-attr]
            lambda entry, path, sha, dest_dir: None
        )

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        with pytest.raises(RegistryError, match="not found in cache after fetch"):
            fetch_workflow("official", entry, "qa-bot", ref="v1.0.0")

    @patch("conductor.registry.cache.load_index")
    @patch("conductor.registry.cache.materialize_to_sha")
    @patch("conductor.registry.cache.resolve_ref")
    def test_unknown_workflow_raises(
        self,
        mock_resolve_ref: object,
        mock_materialize: object,
        mock_load_index: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "v1.0.0"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]
        mock_load_index.return_value = _FakeIndex(workflows={})  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        with pytest.raises(RegistryError, match="not found"):
            fetch_workflow("official", entry, "nope", ref="v1.0.0")


# ---------------------------------------------------------------------------
# clear_cache
# ---------------------------------------------------------------------------


class TestClearCache:
    def test_clear_all(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        cache_base = home / "cache" / "registries"

        (cache_base / "reg-a" / "wf" / _SHA_DIR).mkdir(parents=True)
        (cache_base / "reg-b" / "wf" / _SHA_DIR2).mkdir(parents=True)

        clear_cache()

        assert not cache_base.exists()

    def test_clear_specific_registry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        cache_base = home / "cache" / "registries"

        (cache_base / "reg-a" / "wf" / _SHA_DIR).mkdir(parents=True)
        (cache_base / "reg-b" / "wf" / _SHA_DIR2).mkdir(parents=True)

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
