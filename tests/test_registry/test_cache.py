"""Tests for the registry cache layer."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.registry.cache import (
    CACHE_LAYOUT_VERSION,
    ParsedToolInfo,
    _ref_slug,
    _safe_repo_path,
    _write_ref_pointer,
    auto_fetch_relative_workflow,
    clear_cache,
    fetch_workflow,
    find_registry_cache_location,
    get_cache_base,
    get_cached_workflow_path,
    load_parsed_tools,
    prune_temp_dirs,
    save_parsed_tools,
)
from conductor.registry.config import RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError
from conductor.registry.index import RegistryIndex, WorkflowInfo

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


def _make_index() -> RegistryIndex:
    """Default index used in most GitHub-fetch tests."""
    return RegistryIndex(
        workflows={
            "qa-bot": WorkflowInfo(
                description="Simple Q&A",
                path="workflows/qa-bot.yaml",
            ),
        }
    )


def _write_workflow_into_staging(dest_dir: Path, repo_path: str = "workflows/qa-bot.yaml") -> None:
    """Mimic _fetch_github writing a workflow file into a staging dir.

    Preserves the workflow's repo parent directory inside ``dest_dir``.
    """
    target = dest_dir / repo_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"name: qa-bot\nagents: []\n")


def _pre_populate_cache(
    home: Path,
    *,
    registry_name: str,
    workflow_name: str,
    sha: str,
    workflow_repo_path: str,
    registry_source: str,
    registry_type: str = "github",
    workflow_content: bytes = b"name: qa-bot\n",
) -> Path:
    """Populate the cache for a workflow as if it had been fully fetched.

    Writes the workflow file at the mirrored repo path, the source.json
    metadata, the cached index, and the per-workflow readiness sentinel.

    Returns the absolute path to the cached workflow file.
    """
    base = home / "cache" / "registries"

    # Mirrored workflow file
    sha_root = base / registry_name / sha[:12]
    workflow_path = sha_root / workflow_repo_path
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_bytes(workflow_content)

    # Metadata directory
    meta_dir = base / registry_name / "_meta" / sha[:12]
    meta_dir.mkdir(parents=True, exist_ok=True)

    (meta_dir / "source.json").write_text(
        json.dumps(
            {
                "cache_layout_version": CACHE_LAYOUT_VERSION,
                "registry_type": registry_type,
                "source": registry_source,
                "full_sha": sha,
            },
            sort_keys=True,
            indent=2,
        ),
        encoding="utf-8",
    )

    (meta_dir / "index.yaml").write_text(
        textwrap.dedent(f"""\
            workflows:
              {workflow_name}:
                description: ""
                path: {workflow_repo_path}
            """),
        encoding="utf-8",
    )

    safe_name = workflow_name.replace("/", "_")
    (meta_dir / f"{safe_name}.complete").write_text("", encoding="utf-8")

    return workflow_path


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
# _safe_repo_path
# ---------------------------------------------------------------------------


class TestSafeRepoPath:
    @pytest.mark.parametrize(
        "path",
        ["workflows/foo.yaml", "foo.yaml", "a/b/c/d.yaml", "deep/nested/file.yml"],
    )
    def test_accepts_safe_paths(self, path: str) -> None:
        result = _safe_repo_path(path)
        assert str(result) == path

    @pytest.mark.parametrize(
        "path",
        [
            "../escape.yaml",
            "../../etc/passwd",
            "ok/../escape.yaml",
            "ok/../../escape.yaml",
        ],
    )
    def test_rejects_dotdot(self, path: str) -> None:
        with pytest.raises(RegistryError, match="must not contain '..'"):
            _safe_repo_path(path)

    @pytest.mark.parametrize(
        "path",
        ["/abs/path.yaml", "\\abs\\path.yaml", "C:/Win/path.yaml", "Z:\\Win\\path.yaml"],
    )
    def test_rejects_absolute(self, path: str) -> None:
        with pytest.raises(RegistryError, match="absolute"):
            _safe_repo_path(path)

    @pytest.mark.parametrize("path", ["", ".", "./"])
    def test_rejects_empty(self, path: str) -> None:
        with pytest.raises(RegistryError, match="empty"):
            _safe_repo_path(path)

    def test_rejects_nul_byte(self) -> None:
        with pytest.raises(RegistryError, match="NUL byte"):
            _safe_repo_path("ok\x00/file.yaml")


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

    def test_returns_path_when_fully_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        wf_path = _pre_populate_cache(
            home,
            registry_name="myregistry",
            workflow_name="qa-bot",
            sha=_FAKE_SHA,
            workflow_repo_path="workflows/qa-bot.yaml",
            registry_source="myorg/workflows",
        )

        result = get_cached_workflow_path("myregistry", "qa-bot", _FAKE_SHA)
        assert result == wf_path

    def test_returns_none_without_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with the workflow file present, no sentinel == cache miss."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        # Write the workflow file but skip the sentinel.
        sha_root = home / "cache" / "registries" / "myregistry" / _SHA_DIR
        wf_dir = sha_root / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "qa-bot.yaml").write_bytes(b"name: qa-bot\n")
        # Index is also present, but no sentinel.
        meta_dir = home / "cache" / "registries" / "myregistry" / "_meta" / _SHA_DIR
        meta_dir.mkdir(parents=True)
        (meta_dir / "index.yaml").write_text(
            "workflows:\n  qa-bot:\n    description: ''\n    path: workflows/qa-bot.yaml\n"
        )

        result = get_cached_workflow_path("myregistry", "qa-bot", _FAKE_SHA)
        assert result is None

    def test_returns_none_when_sentinel_present_but_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the sentinel exists but the workflow YAML doesn't, treat as miss."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        meta_dir = home / "cache" / "registries" / "myregistry" / "_meta" / _SHA_DIR
        meta_dir.mkdir(parents=True)
        (meta_dir / "qa-bot.complete").write_text("")
        (meta_dir / "index.yaml").write_text(
            "workflows:\n  qa-bot:\n    description: ''\n    path: workflows/qa-bot.yaml\n"
        )
        # No workflow file under sha_root.

        result = get_cached_workflow_path("myregistry", "qa-bot", _FAKE_SHA)
        assert result is None

    def test_uses_first_12_chars_of_sha(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cache directory name is sha[:12]; full SHA is accepted for lookup."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        sha = "0123456789abcdef" * 2 + "01234567"  # 40 chars
        wf_path = _pre_populate_cache(
            home,
            registry_name="myregistry",
            workflow_name="qa-bot",
            sha=sha,
            workflow_repo_path="qa-bot.yaml",
            registry_source="myorg/workflows",
        )

        result = get_cached_workflow_path("myregistry", "qa-bot", sha)
        assert result == wf_path
        assert sha[:12] in str(result)

    def test_explicit_repo_path_skips_index_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Passing workflow_repo_path explicitly avoids loading the cached index."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        meta_dir = home / "cache" / "registries" / "myregistry" / "_meta" / _SHA_DIR
        meta_dir.mkdir(parents=True)
        (meta_dir / "qa-bot.complete").write_text("")
        # No index.yaml on disk — should still work because we pass the path.

        sha_root = home / "cache" / "registries" / "myregistry" / _SHA_DIR
        wf = sha_root / "custom" / "qa-bot.yaml"
        wf.parent.mkdir(parents=True)
        wf.write_bytes(b"x")

        result = get_cached_workflow_path(
            "myregistry", "qa-bot", _FAKE_SHA, workflow_repo_path="custom/qa-bot.yaml"
        )
        assert result == wf


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
        mock_load_index.return_value = RegistryIndex(workflows={})  # type: ignore[union-attr]

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

    @patch("conductor.registry.cache.load_index")
    def test_path_registry_rejects_unsafe_workflow_path(
        self, mock_load_index: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An index with an unsafe path (e.g. ../escape.yaml) is rejected."""
        _setup_conductor_home(tmp_path, monkeypatch)
        bad_index = RegistryIndex(
            workflows={
                "evil": WorkflowInfo(description="", path="../escape.yaml"),
            }
        )
        mock_load_index.return_value = bad_index  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.path, source=str(tmp_path))
        with pytest.raises(RegistryError, match=r"\.\.|absolute"):
            fetch_workflow("local", entry, "evil")


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
        """Happy path: ref → SHA → cache miss → fetch → file present at mirrored path."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "v1.0.0"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]
        mock_fetch_github.side_effect = (  # type: ignore[union-attr]
            lambda entry, path, sha, dest_dir: _write_workflow_into_staging(dest_dir, path)
        )

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow("official", entry, "qa-bot", ref="v1.0.0")

        assert result.exists()
        assert result.name == "qa-bot.yaml"
        # Mirrored repo path inside per-SHA root
        expected = (
            home / "cache" / "registries" / "official" / _SHA_DIR / "workflows" / "qa-bot.yaml"
        )
        assert result == expected

        # Sentinel was written
        sentinel = (
            home / "cache" / "registries" / "official" / "_meta" / _SHA_DIR / "qa-bot.complete"
        )
        assert sentinel.is_file()

        # Source metadata was written and matches
        meta_path = home / "cache" / "registries" / "official" / "_meta" / _SHA_DIR / "source.json"
        assert meta_path.is_file()
        meta = json.loads(meta_path.read_text())
        assert meta["full_sha"] == _FAKE_SHA
        assert meta["source"] == "myorg/workflows"
        assert meta["registry_type"] == "github"
        assert meta["cache_layout_version"] == CACHE_LAYOUT_VERSION

        # Cached index was persisted
        cached_index = (
            home / "cache" / "registries" / "official" / "_meta" / _SHA_DIR / "index.yaml"
        )
        assert cached_index.is_file()

        # load_index pinned to the SHA
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
        """When sentinel + file are present, skip the fetch and the index load."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "v1.0.0"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]

        wf_path = _pre_populate_cache(
            home,
            registry_name="official",
            workflow_name="qa-bot",
            sha=_FAKE_SHA,
            workflow_repo_path="workflows/qa-bot.yaml",
            registry_source="myorg/workflows",
        )

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow("official", entry, "qa-bot", ref="v1.0.0")

        assert result == wf_path
        mock_fetch_github.assert_not_called()  # type: ignore[union-attr]
        mock_load_index.assert_not_called()  # type: ignore[union-attr]

    @patch("conductor.registry.cache._fetch_github")
    @patch("conductor.registry.cache.load_index")
    @patch("conductor.registry.cache.materialize_to_sha")
    @patch("conductor.registry.cache.resolve_ref")
    def test_stale_metadata_triggers_refetch(
        self,
        mock_resolve_ref: object,
        mock_materialize: object,
        mock_load_index: object,
        mock_fetch_github: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If source.json doesn't match (e.g. wrong source), re-fetch and rewrite."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "v1.0.0"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]
        mock_fetch_github.side_effect = (  # type: ignore[union-attr]
            lambda entry, path, sha, dest_dir: _write_workflow_into_staging(dest_dir, path)
        )

        # Pre-populate with a DIFFERENT registry source so metadata mismatch triggers re-fetch.
        _pre_populate_cache(
            home,
            registry_name="official",
            workflow_name="qa-bot",
            sha=_FAKE_SHA,
            workflow_repo_path="workflows/qa-bot.yaml",
            registry_source="someone-else/workflows",  # Different source
        )

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow("official", entry, "qa-bot", ref="v1.0.0")

        assert result.exists()
        # Metadata was rewritten with the new source
        meta_path = home / "cache" / "registries" / "official" / "_meta" / _SHA_DIR / "source.json"
        meta = json.loads(meta_path.read_text())
        assert meta["source"] == "myorg/workflows"
        # Fetch and index load were both invoked
        mock_fetch_github.assert_called_once()  # type: ignore[union-attr]
        mock_load_index.assert_called_once()  # type: ignore[union-attr]

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
        """If fetch fails mid-write, the sentinel is not written and tmp dir is cleaned up."""
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

        # Sentinel was NEVER written — cache hit must fail on retry.
        sentinel = (
            home / "cache" / "registries" / "official" / "_meta" / _SHA_DIR / "qa-bot.complete"
        )
        assert not sentinel.exists()

        # No leftover .tmp-* directories under the meta dir.
        meta_root = home / "cache" / "registries" / "official" / "_meta"
        if meta_root.exists():
            leftovers = [p for p in meta_root.rglob(".tmp-*") if p.is_dir()]
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
        """Two fetches with the same branch ref re-resolve the SHA each time.

        When the underlying branch advances between calls (materialize_to_sha
        returns different SHAs), the second fetch must populate a *new* SHA
        directory rather than reuse the old one.
        """
        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "main"  # type: ignore[union-attr]
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]
        mock_fetch_github.side_effect = (  # type: ignore[union-attr]
            lambda entry, path, sha, dest_dir: _write_workflow_into_staging(dest_dir, path)
        )

        # Branch advances between calls.
        mock_materialize.side_effect = [_FAKE_SHA, _FAKE_SHA2]  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")

        first = fetch_workflow("official", entry, "qa-bot", ref="main")
        second = fetch_workflow("official", entry, "qa-bot", ref="main")

        # Each call resolved to a different SHA → different SHA dirs.
        assert first != second
        assert _SHA_DIR in str(first)
        assert _SHA_DIR2 in str(second)

        first_dir = home / "cache" / "registries" / "official" / _SHA_DIR
        second_dir = home / "cache" / "registries" / "official" / _SHA_DIR2
        assert first_dir.exists()
        assert second_dir.exists()

        # Both fetches actually executed (no spurious cache reuse).
        assert mock_fetch_github.call_count == 2  # type: ignore[union-attr]
        assert mock_materialize.call_count == 2  # type: ignore[union-attr]

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
        mock_load_index.return_value = RegistryIndex(workflows={})  # type: ignore[union-attr]

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

        (cache_base / "reg-a" / _SHA_DIR / "wf").mkdir(parents=True)
        (cache_base / "reg-b" / _SHA_DIR2 / "wf").mkdir(parents=True)

        clear_cache()

        assert not cache_base.exists()

    def test_clear_specific_registry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        cache_base = home / "cache" / "registries"

        (cache_base / "reg-a" / _SHA_DIR / "wf").mkdir(parents=True)
        (cache_base / "reg-b" / _SHA_DIR2 / "wf").mkdir(parents=True)

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


# ---------------------------------------------------------------------------
# prune_temp_dirs
# ---------------------------------------------------------------------------


class TestPruneTempDirs:
    def test_prune_temp_dirs_removes_orphans(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        cache_base = home / "cache" / "registries"

        # Real SHA dir alongside an orphan .tmp-* dir under _meta/<sha>/.
        real = cache_base / "reg-a" / _SHA_DIR
        orphan = cache_base / "reg-a" / "_meta" / _SHA_DIR / ".tmp-abc"
        real.mkdir(parents=True)
        orphan.mkdir(parents=True)
        (orphan / "junk.yaml").write_text("x", encoding="utf-8")

        removed = prune_temp_dirs()

        assert removed == 1
        assert not orphan.exists()
        assert real.exists()

    def test_prune_temp_dirs_scoped_to_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        cache_base = home / "cache" / "registries"

        orphan_a = cache_base / "reg-a" / "_meta" / _SHA_DIR / ".tmp-aaa"
        orphan_b = cache_base / "reg-b" / "_meta" / _SHA_DIR2 / ".tmp-bbb"
        orphan_a.mkdir(parents=True)
        orphan_b.mkdir(parents=True)

        removed = prune_temp_dirs("reg-a")

        assert removed == 1
        assert not orphan_a.exists()
        assert orphan_b.exists()

    def test_prune_temp_dirs_returns_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        cache_base = home / "cache" / "registries"

        for n in range(3):
            (cache_base / "reg-a" / "_meta" / _SHA_DIR / f".tmp-{n}").mkdir(parents=True)
        (cache_base / "reg-b" / "_meta" / _SHA_DIR2 / ".tmp-xyz").mkdir(parents=True)
        # Real dirs - should not be counted.
        (cache_base / "reg-a" / _SHA_DIR / "wf").mkdir(parents=True)

        removed = prune_temp_dirs()

        assert removed == 4

    def test_prune_temp_dirs_missing_base_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        # No cache dir at all
        assert prune_temp_dirs() == 0
        assert prune_temp_dirs("reg-a") == 0


# ---------------------------------------------------------------------------
# Ad-hoc fetch + resolve_and_fetch unifier
# ---------------------------------------------------------------------------


class TestFetchWorkflowAdhoc:
    """Tests for fetch_workflow_adhoc and the _adhoc cache namespace."""

    @patch("conductor.registry.cache._fetch_github")
    @patch("conductor.registry.cache.load_index")
    @patch("conductor.registry.cache.materialize_to_sha")
    @patch("conductor.registry.cache.resolve_ref")
    def test_adhoc_fetches_under_adhoc_namespace(
        self,
        mock_resolve_ref: object,
        mock_materialize: object,
        mock_load_index: object,
        mock_fetch_github: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ad-hoc fetch caches under <base>/_adhoc/<owner>/<repo>/<sha>/<repo_path>."""
        from conductor.registry.cache import fetch_workflow_adhoc

        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "v1.0.0"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]
        mock_fetch_github.side_effect = (  # type: ignore[union-attr]
            lambda entry, path, sha, dest_dir: _write_workflow_into_staging(dest_dir, path)
        )

        result = fetch_workflow_adhoc(
            owner="myorg",
            repo="workflows",
            workflow_name="qa-bot",
            ref="v1.0.0",
        )

        assert result.exists()
        assert result.name == "qa-bot.yaml"
        # Cache directory is namespaced under _adhoc/<owner>/<repo>/<sha>/<repo_path>
        expected = (
            home
            / "cache"
            / "registries"
            / "_adhoc"
            / "myorg"
            / "workflows"
            / _SHA_DIR
            / "workflows"
            / "qa-bot.yaml"
        )
        assert result == expected

    @patch("conductor.registry.cache._fetch_github")
    @patch("conductor.registry.cache.load_index")
    @patch("conductor.registry.cache.materialize_to_sha")
    @patch("conductor.registry.cache.resolve_ref")
    def test_adhoc_isolated_from_named_registry_cache(
        self,
        mock_resolve_ref: object,
        mock_materialize: object,
        mock_load_index: object,
        mock_fetch_github: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same SHA fetched ad-hoc and as named-registry produces distinct caches."""
        from conductor.registry.cache import fetch_workflow_adhoc

        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "v1.0.0"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]
        mock_fetch_github.side_effect = (  # type: ignore[union-attr]
            lambda entry, path, sha, dest_dir: _write_workflow_into_staging(dest_dir, path)
        )

        # Fetch via named registry first
        named_entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        named_result = fetch_workflow("official", named_entry, "qa-bot", ref="v1.0.0")

        # Fetch the same workflow via ad-hoc
        adhoc_result = fetch_workflow_adhoc(
            owner="myorg",
            repo="workflows",
            workflow_name="qa-bot",
            ref="v1.0.0",
        )

        # Both succeed but live in different cache trees
        assert named_result.parent != adhoc_result.parent
        # Sanity: named_result lives under official/, adhoc under _adhoc/myorg/workflows/
        assert (home / "cache" / "registries" / "official").exists()
        assert (home / "cache" / "registries" / "_adhoc" / "myorg" / "workflows").exists()
        # Adhoc cache path includes the _adhoc/ namespace segment; named does not.
        assert "_adhoc" in adhoc_result.relative_to(home).parts
        assert "_adhoc" not in named_result.relative_to(home).parts

    @patch("conductor.registry.cache._fetch_github")
    @patch("conductor.registry.cache.load_index")
    @patch("conductor.registry.cache.materialize_to_sha")
    @patch("conductor.registry.cache.resolve_ref")
    def test_adhoc_cache_hit_skips_fetch(
        self,
        mock_resolve_ref: object,
        mock_materialize: object,
        mock_load_index: object,
        mock_fetch_github: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pre-populated ad-hoc cache returns immediately without fetching."""
        from conductor.registry.cache import fetch_workflow_adhoc

        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "v1.0.0"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]

        wf_path = _pre_populate_cache(
            home,
            registry_name="_adhoc/myorg/workflows",
            workflow_name="qa-bot",
            sha=_FAKE_SHA,
            workflow_repo_path="workflows/qa-bot.yaml",
            registry_source="myorg/workflows",
        )

        result = fetch_workflow_adhoc(
            owner="myorg",
            repo="workflows",
            workflow_name="qa-bot",
            ref="v1.0.0",
        )

        assert result == wf_path
        mock_fetch_github.assert_not_called()  # type: ignore[union-attr]
        mock_load_index.assert_not_called()  # type: ignore[union-attr]


class TestResolveAndFetch:
    """Tests for the resolve_and_fetch unifier dispatcher."""

    def test_file_kind_returns_path_unchanged(self, tmp_path: Path) -> None:
        from conductor.registry.cache import resolve_and_fetch
        from conductor.registry.resolver import ResolvedRef

        local = tmp_path / "wf.yaml"
        local.write_text("name: wf\n")
        ref = ResolvedRef(kind="file", path=local)
        assert resolve_and_fetch(ref) == local

    def test_file_kind_missing_path_raises(self) -> None:
        from conductor.registry.cache import resolve_and_fetch
        from conductor.registry.resolver import ResolvedRef

        ref = ResolvedRef(kind="file", path=None)
        with pytest.raises(ValueError, match="non-None path"):
            resolve_and_fetch(ref)

    @patch("conductor.registry.cache.fetch_workflow")
    def test_registry_kind_dispatches_to_fetch_workflow(
        self, mock_fetch: object, tmp_path: Path
    ) -> None:
        from conductor.registry.cache import resolve_and_fetch
        from conductor.registry.resolver import ResolvedRef

        entry = RegistryEntry(type=RegistryType.github, source="o/r")
        ref = ResolvedRef(
            kind="registry",
            workflow="qa-bot",
            registry_name="team",
            ref="v1.0.0",
            registry_entry=entry,
        )
        expected_path = tmp_path / "result.yaml"
        mock_fetch.return_value = expected_path  # type: ignore[union-attr]

        result = resolve_and_fetch(ref)

        assert result == expected_path
        mock_fetch.assert_called_once_with(  # type: ignore[union-attr]
            registry_name="team",
            registry_entry=entry,
            workflow_name="qa-bot",
            ref="v1.0.0",
            allow_network=True,
        )

    @patch("conductor.registry.cache.fetch_workflow_adhoc")
    def test_adhoc_kind_dispatches_to_fetch_workflow_adhoc(
        self, mock_fetch: object, tmp_path: Path
    ) -> None:
        from conductor.registry.cache import resolve_and_fetch
        from conductor.registry.resolver import ResolvedRef

        ref = ResolvedRef(
            kind="adhoc",
            workflow="qa-bot",
            registry_name="myorg/workflows",
            ref="v1.0.0",
            adhoc_owner="myorg",
            adhoc_repo="workflows",
        )
        expected_path = tmp_path / "result.yaml"
        mock_fetch.return_value = expected_path  # type: ignore[union-attr]

        result = resolve_and_fetch(ref)

        assert result == expected_path
        mock_fetch.assert_called_once_with(  # type: ignore[union-attr]
            owner="myorg",
            repo="workflows",
            workflow_name="qa-bot",
            ref="v1.0.0",
            allow_network=True,
        )

    def test_adhoc_kind_missing_fields_raises(self) -> None:
        from conductor.registry.cache import resolve_and_fetch
        from conductor.registry.resolver import ResolvedRef

        ref = ResolvedRef(
            kind="adhoc",
            workflow="qa-bot",
            adhoc_owner=None,  # missing!
            adhoc_repo="workflows",
        )
        with pytest.raises(ValueError, match="adhoc_owner"):
            resolve_and_fetch(ref)


# ---------------------------------------------------------------------------
# find_registry_cache_location
# ---------------------------------------------------------------------------


class TestFindRegistryCacheLocation:
    def test_named_registry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        sha_root = home / "cache" / "registries" / "official" / _SHA_DIR
        wf = sha_root / "sdd-plan" / "plan.yaml"
        wf.parent.mkdir(parents=True)
        wf.write_text("x")

        location = find_registry_cache_location(wf)
        assert location is not None
        assert location.registry_name == "official"
        assert location.sha == _SHA_DIR
        assert location.sha_root == sha_root.resolve()

    def test_adhoc_registry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        sha_root = home / "cache" / "registries" / "_adhoc" / "myorg" / "workflows" / _SHA_DIR
        wf = sha_root / "deep" / "nested" / "workflow.yaml"
        wf.parent.mkdir(parents=True)
        wf.write_text("x")

        location = find_registry_cache_location(wf)
        assert location is not None
        assert location.registry_name == "_adhoc/myorg/workflows"
        assert location.sha == _SHA_DIR
        assert location.sha_root == sha_root.resolve()

    def test_meta_dir_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        # Files inside _meta/<sha>/ are NOT a SHA-rooted mirror.
        meta_path = home / "cache" / "registries" / "official" / "_meta" / _SHA_DIR / "source.json"
        meta_path.parent.mkdir(parents=True)
        meta_path.write_text("{}")

        assert find_registry_cache_location(meta_path) is None

    def test_outside_cache_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        outside = tmp_path / "elsewhere" / "workflow.yaml"
        outside.parent.mkdir(parents=True)
        outside.write_text("x")
        assert find_registry_cache_location(outside) is None

    def test_non_hex_sha_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        # 12 chars but not hex
        path = home / "cache" / "registries" / "official" / "ZZZZZZZZZZZZ" / "wf.yaml"
        path.parent.mkdir(parents=True)
        path.write_text("x")
        assert find_registry_cache_location(path) is None


# ---------------------------------------------------------------------------
# auto_fetch_relative_workflow (Part 2)
# ---------------------------------------------------------------------------


class TestAutoFetchRelativeWorkflow:
    """Cross-workflow relative refs like ../other/workflow.yaml."""

    @patch("conductor.registry.cache._fetch_github")
    @patch("conductor.registry.cache.load_index")
    @patch("conductor.registry.cache.materialize_to_sha")
    @patch("conductor.registry.cache.resolve_ref")
    def test_auto_fetches_sibling_workflow_in_same_registry(
        self,
        mock_resolve_ref: object,
        mock_materialize: object,
        mock_load_index: object,
        mock_fetch_github: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The classic bug: parent workflow refs ../sibling/workflow.yaml."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "v1.0.0"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]

        # Pre-populate the parent workflow (sdd-plan/plan.yaml) only.
        index = RegistryIndex(
            workflows={
                "sdd-plan": WorkflowInfo(description="", path="sdd-plan/plan.yaml"),
                "document-review": WorkflowInfo(
                    description="", path="document-review/workflow.yaml"
                ),
            }
        )
        # Pre-write the cache as if sdd-plan were already fetched.
        sha_root = home / "cache" / "registries" / "official" / _SHA_DIR
        (sha_root / "sdd-plan").mkdir(parents=True)
        (sha_root / "sdd-plan" / "plan.yaml").write_bytes(b"name: sdd-plan\n")
        meta_dir = home / "cache" / "registries" / "official" / "_meta" / _SHA_DIR
        meta_dir.mkdir(parents=True)
        (meta_dir / "source.json").write_text(
            json.dumps(
                {
                    "cache_layout_version": CACHE_LAYOUT_VERSION,
                    "registry_type": "github",
                    "source": "myorg/workflows",
                    "full_sha": _FAKE_SHA,
                },
                sort_keys=True,
                indent=2,
            )
        )
        (meta_dir / "index.yaml").write_text(
            "workflows:\n"
            "  sdd-plan:\n    description: ''\n    path: sdd-plan/plan.yaml\n"
            "  document-review:\n    description: ''\n    path: document-review/workflow.yaml\n"
        )
        (meta_dir / "sdd-plan.complete").write_text("")

        # Mock fetch — should be invoked for the auto-fetch of document-review.
        mock_load_index.return_value = index  # type: ignore[union-attr]
        mock_fetch_github.side_effect = (  # type: ignore[union-attr]
            lambda entry, path, sha, dest_dir: _write_workflow_into_staging(dest_dir, path)
        )

        # Simulate the engine's relative-path resolution from sdd-plan/plan.yaml.
        candidate = (sha_root / "sdd-plan" / "../document-review/workflow.yaml").resolve()
        assert not candidate.exists()  # confirms the bug pre-conditions

        fetched = auto_fetch_relative_workflow(candidate)
        assert fetched is not None
        assert fetched.exists()
        assert fetched == sha_root / "document-review" / "workflow.yaml"
        mock_fetch_github.assert_called_once()  # type: ignore[union-attr]

    def test_returns_none_when_not_in_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        outside = tmp_path / "anywhere" / "wf.yaml"
        outside.parent.mkdir(parents=True)
        # Don't even create the file
        assert auto_fetch_relative_workflow(outside) is None

    def test_returns_none_when_no_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cache root exists but no metadata → can't auto-fetch."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        sha_root = home / "cache" / "registries" / "official" / _SHA_DIR
        sha_root.mkdir(parents=True)
        candidate = sha_root / "missing" / "wf.yaml"
        assert auto_fetch_relative_workflow(candidate) is None

    def test_returns_none_when_path_not_in_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cache + metadata + index exist but path doesn't match any workflow."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        _pre_populate_cache(
            home,
            registry_name="official",
            workflow_name="parent",
            sha=_FAKE_SHA,
            workflow_repo_path="parent/wf.yaml",
            registry_source="myorg/workflows",
        )
        sha_root = home / "cache" / "registries" / "official" / _SHA_DIR
        candidate = sha_root / "not-in-index" / "wf.yaml"
        assert auto_fetch_relative_workflow(candidate) is None

    def test_returns_none_when_cache_layout_version_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stale cache_layout_version in source.json should not be used."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        _pre_populate_cache(
            home,
            registry_name="official",
            workflow_name="parent",
            sha=_FAKE_SHA,
            workflow_repo_path="parent/wf.yaml",
            registry_source="myorg/workflows",
        )
        # Overwrite source.json with an older layout version.
        meta = home / "cache" / "registries" / "official" / "_meta" / _SHA_DIR
        (meta / "source.json").write_text(
            json.dumps(
                {
                    "cache_layout_version": CACHE_LAYOUT_VERSION - 1,
                    "registry_type": "github",
                    "source": "myorg/workflows",
                    "full_sha": _FAKE_SHA,
                },
                sort_keys=True,
                indent=2,
            )
        )

        sha_root = home / "cache" / "registries" / "official" / _SHA_DIR
        candidate = sha_root / "parent" / "wf.yaml"  # exists but stale meta
        # Even valid candidate path returns None — metadata is rejected.
        assert auto_fetch_relative_workflow(candidate) is None

    def test_returns_none_when_metadata_sha_does_not_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """source.json full_sha must agree with the on-disk SHA dir prefix."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        _pre_populate_cache(
            home,
            registry_name="official",
            workflow_name="parent",
            sha=_FAKE_SHA,
            workflow_repo_path="parent/wf.yaml",
            registry_source="myorg/workflows",
        )
        # Tamper with source.json to claim a different SHA.
        meta = home / "cache" / "registries" / "official" / "_meta" / _SHA_DIR
        (meta / "source.json").write_text(
            json.dumps(
                {
                    "cache_layout_version": CACHE_LAYOUT_VERSION,
                    "registry_type": "github",
                    "source": "myorg/workflows",
                    "full_sha": _FAKE_SHA2,  # mismatched!
                },
                sort_keys=True,
                indent=2,
            )
        )
        sha_root = home / "cache" / "registries" / "official" / _SHA_DIR
        candidate = sha_root / "parent" / "wf.yaml"
        assert auto_fetch_relative_workflow(candidate) is None


# ---------------------------------------------------------------------------
# E5-T2: SHA-keyed parse cache
# ---------------------------------------------------------------------------


class TestParsedToolsCache:
    """Tests for save_parsed_tools / load_parsed_tools (E5-T2)."""

    def _make_tools(self) -> dict[str, ParsedToolInfo]:
        from conductor.config.schema import InputDef, McpConfig

        return {
            "qa-bot": ParsedToolInfo(
                description="Simple Q&A",
                input={"question": InputDef(type="string", required=True)},
                mcp=McpConfig(mode="sync", read_only=True),
            ),
        }

    def test_round_trips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        tools = self._make_tools()

        save_parsed_tools("official", _FAKE_SHA, tools)
        loaded = load_parsed_tools("official", _FAKE_SHA)

        assert loaded is not None
        assert loaded["qa-bot"].description == "Simple Q&A"
        assert loaded["qa-bot"].input["question"].type == "string"
        assert loaded["qa-bot"].mcp.mode == "sync"
        assert loaded["qa-bot"].mcp.read_only is True

    def test_sentinel_written_last(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The tools.json file exists before the tools.complete sentinel does."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        write_order: list[str] = []

        original_atomic_write = __import__(
            "conductor.registry.cache", fromlist=["_atomic_write_text"]
        )._atomic_write_text

        def _tracking_write(target: Path, text: str) -> None:
            write_order.append(target.name)
            original_atomic_write(target, text)

        monkeypatch.setattr("conductor.registry.cache._atomic_write_text", _tracking_write)

        save_parsed_tools("official", _FAKE_SHA, self._make_tools())

        assert write_order == ["tools.json", "tools.complete"]
        meta = home / "cache" / "registries" / "official" / "_meta" / _SHA_DIR
        assert (meta / "tools.json").is_file()
        assert (meta / "tools.complete").is_file()

    def test_missing_sentinel_is_a_miss(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tools.json present without the sentinel is treated as no cache."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        meta = home / "cache" / "registries" / "official" / "_meta" / _SHA_DIR
        meta.mkdir(parents=True)
        (meta / "tools.json").write_text(
            json.dumps({"cache_layout_version": CACHE_LAYOUT_VERSION, "tools": {}}),
            encoding="utf-8",
        )
        assert load_parsed_tools("official", _FAKE_SHA) is None

    def test_missing_entirely_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        assert load_parsed_tools("official", _FAKE_SHA) is None

    def test_cache_layout_version_bump_invalidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stored cache written under an older layout version is rejected."""
        _setup_conductor_home(tmp_path, monkeypatch)
        save_parsed_tools("official", _FAKE_SHA, self._make_tools())

        with patch("conductor.registry.cache.CACHE_LAYOUT_VERSION", CACHE_LAYOUT_VERSION + 1):
            assert load_parsed_tools("official", _FAKE_SHA) is None

    def test_malformed_single_entry_is_skipped_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single corrupt tool entry doesn't discard the rest of the cache."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        meta = home / "cache" / "registries" / "official" / "_meta" / _SHA_DIR
        meta.mkdir(parents=True)
        (meta / "tools.json").write_text(
            json.dumps(
                {
                    "cache_layout_version": CACHE_LAYOUT_VERSION,
                    "tools": {
                        "good": {
                            "description": "fine",
                            "input": {},
                            "mcp": {},
                        },
                        "bad": {"description": "broken"},  # missing "input"/"mcp" keys
                    },
                }
            ),
            encoding="utf-8",
        )
        (meta / "tools.complete").write_text("", encoding="utf-8")

        loaded = load_parsed_tools("official", _FAKE_SHA)
        assert loaded is not None
        assert "good" in loaded
        assert "bad" not in loaded

    def test_different_shas_are_independent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        save_parsed_tools("official", _FAKE_SHA, self._make_tools())
        assert load_parsed_tools("official", _FAKE_SHA2) is None


# ---------------------------------------------------------------------------
# E5-T3: offline ref pointer
# ---------------------------------------------------------------------------


class TestRefPointer:
    """Tests for the _refs/<slug>.json pointer (E5-T3, R2)."""

    def test_write_then_read_round_trips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conductor.registry.cache import _read_ref_pointer

        _setup_conductor_home(tmp_path, monkeypatch)
        _write_ref_pointer("official", "main", _FAKE_SHA)
        assert _read_ref_pointer("official", "main") == _FAKE_SHA

    def test_read_normalizes_stored_uppercase_sha_to_lowercase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pointer file recorded with an uppercase SHA still reads lowercase."""
        from conductor.registry.cache import _read_ref_pointer, _ref_pointer_path

        _setup_conductor_home(tmp_path, monkeypatch)
        pointer = _ref_pointer_path("official", "main")
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps({"ref": "main", "sha": _FAKE_SHA.upper()}), encoding="utf-8")
        assert _read_ref_pointer("official", "main") == _FAKE_SHA

    def test_none_and_latest_share_one_pointer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None and 'latest' both mean 'the default branch' and share a slug."""
        from conductor.registry.cache import _read_ref_pointer

        _setup_conductor_home(tmp_path, monkeypatch)
        _write_ref_pointer("official", None, _FAKE_SHA)
        assert _read_ref_pointer("official", "latest") == _FAKE_SHA
        assert _read_ref_pointer("official", None) == _FAKE_SHA
        assert _ref_slug(None) == _ref_slug("latest") == _ref_slug("LATEST")

    def test_missing_pointer_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conductor.registry.cache import _read_ref_pointer

        _setup_conductor_home(tmp_path, monkeypatch)
        assert _read_ref_pointer("official", "main") is None

    def test_malformed_pointer_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conductor.registry.cache import _read_ref_pointer, _ref_pointer_path

        home = _setup_conductor_home(tmp_path, monkeypatch)
        pointer = _ref_pointer_path("official", "main")
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text("not json", encoding="utf-8")
        assert _read_ref_pointer("official", "main") is None

        pointer.write_text(json.dumps({"ref": "main", "sha": "not-a-sha"}), encoding="utf-8")
        assert _read_ref_pointer("official", "main") is None
        assert home  # keep home referenced

    @pytest.mark.parametrize(
        "ref",
        ["release/1.x", "feature/foo/bar", "a/b/c", "weird\\slashes\\ref"],
    )
    def test_slug_is_safe_for_slash_bearing_refs(self, ref: str) -> None:
        """A '/'-bearing ref cannot escape the _refs directory."""
        slug = _ref_slug(ref)
        assert "/" not in slug
        assert "\\" not in slug
        assert ".." not in slug

    def test_slug_disambiguates_similar_names(self) -> None:
        """Two distinct refs that sanitize to the same characters get distinct slugs."""
        assert _ref_slug("release/1.x") != _ref_slug("release_1.x")

    def test_write_is_atomic_via_tempfile_rename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pointer is written via the shared atomic tempfile+rename helper."""
        _setup_conductor_home(tmp_path, monkeypatch)
        calls: list[Path] = []
        original = __import__(
            "conductor.registry.cache", fromlist=["_atomic_write_text"]
        )._atomic_write_text

        def _tracking(target: Path, text: str) -> None:
            calls.append(target)
            original(target, text)

        monkeypatch.setattr("conductor.registry.cache._atomic_write_text", _tracking)
        _write_ref_pointer("official", "main", _FAKE_SHA)
        assert len(calls) == 1
        assert calls[0].name.endswith(".json")

    def test_write_failure_is_best_effort_and_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_conductor_home(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "conductor.registry.cache._atomic_write_text",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )
        # Must not raise.
        _write_ref_pointer("official", "main", _FAKE_SHA)


# ---------------------------------------------------------------------------
# E5-T4: allow_network seam
# ---------------------------------------------------------------------------


def _patch_all_github_functions_to_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch every function in registry/github.py to raise, everywhere it is
    bound — github.py itself, and the copies cache.py / version_resolver.py
    imported into their own namespaces via ``from ... import ...`` at their
    own module load time. Patching only the source module would leave those
    already-bound names untouched.
    """
    import conductor.registry.cache as cache_module
    import conductor.registry.github as github_module
    import conductor.registry.version_resolver as version_resolver_module

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("registry/github.py function called during offline resolution")

    function_names = [
        "fetch_file",
        "fetch_file_text",
        "list_tags",
        "get_default_branch",
        "resolve_ref_to_sha",
        "list_directory",
        "parse_github_source",
    ]
    for module in (github_module, cache_module, version_resolver_module):
        for name in function_names:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, _boom)


class TestFetchWorkflowAllowNetwork:
    """Tests for the allow_network seam on fetch_workflow (E5-T4)."""

    def test_offline_with_pinned_sha_ref_never_touches_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ref that is already a full SHA resolves without any network call."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        wf_path = _pre_populate_cache(
            home,
            registry_name="official",
            workflow_name="qa-bot",
            sha=_FAKE_SHA,
            workflow_repo_path="workflows/qa-bot.yaml",
            registry_source="myorg/workflows",
        )
        _patch_all_github_functions_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow("official", entry, "qa-bot", ref=_FAKE_SHA, allow_network=False)
        assert result == wf_path

    def test_offline_with_uppercase_sha_ref_normalizes_to_lowercase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An uppercase SHA ref resolves against the lowercase on-disk cache."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        wf_path = _pre_populate_cache(
            home,
            registry_name="official",
            workflow_name="qa-bot",
            sha=_FAKE_SHA,
            workflow_repo_path="workflows/qa-bot.yaml",
            registry_source="myorg/workflows",
        )
        _patch_all_github_functions_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow(
            "official", entry, "qa-bot", ref=_FAKE_SHA.upper(), allow_network=False
        )
        assert result == wf_path

    def test_offline_with_floating_ref_uses_pointer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A floating ref resolves through the ref pointer recorded earlier."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        wf_path = _pre_populate_cache(
            home,
            registry_name="official",
            workflow_name="qa-bot",
            sha=_FAKE_SHA,
            workflow_repo_path="workflows/qa-bot.yaml",
            registry_source="myorg/workflows",
        )
        _write_ref_pointer("official", "main", _FAKE_SHA)
        _patch_all_github_functions_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow("official", entry, "qa-bot", ref="main", allow_network=False)
        assert result == wf_path

    def test_offline_with_no_pointer_raises_typed_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cold pointer is a typed RegistryError naming the fetch path."""
        _setup_conductor_home(tmp_path, monkeypatch)
        _patch_all_github_functions_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        with pytest.raises(RegistryError, match="network access is not permitted"):
            fetch_workflow("official", entry, "qa-bot", ref="main", allow_network=False)

    def test_offline_with_matching_sha_but_uncached_workflow_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SHA resolves offline, but the specific workflow was never cached."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        _pre_populate_cache(
            home,
            registry_name="official",
            workflow_name="qa-bot",
            sha=_FAKE_SHA,
            workflow_repo_path="workflows/qa-bot.yaml",
            registry_source="myorg/workflows",
        )
        _patch_all_github_functions_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        with pytest.raises(RegistryError, match="not available in the local cache"):
            fetch_workflow("official", entry, "other-workflow", ref=_FAKE_SHA, allow_network=False)

    @patch("conductor.registry.cache._fetch_github")
    @patch("conductor.registry.cache.load_index")
    @patch("conductor.registry.cache.materialize_to_sha")
    @patch("conductor.registry.cache.resolve_ref")
    def test_online_path_unchanged_and_writes_pointer(
        self,
        mock_resolve_ref: object,
        mock_materialize: object,
        mock_load_index: object,
        mock_fetch_github: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Acceptance: a cold cache still resolves online exactly as today,
        and additionally writes the ref pointer as a side effect."""
        from conductor.registry.cache import _read_ref_pointer

        home = _setup_conductor_home(tmp_path, monkeypatch)
        mock_resolve_ref.return_value = "v1.0.0"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]
        mock_load_index.return_value = _make_index()  # type: ignore[union-attr]
        mock_fetch_github.side_effect = (  # type: ignore[union-attr]
            lambda entry, path, sha, dest_dir: _write_workflow_into_staging(dest_dir, path)
        )

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow("official", entry, "qa-bot", ref="v1.0.0")

        assert result.exists()
        assert home  # keep home referenced
        # Side effect: the ref pointer now resolves offline too.
        assert _read_ref_pointer("official", "v1.0.0") == _FAKE_SHA

    def test_path_registry_ignores_allow_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path registries never touch the network regardless of the flag."""
        _setup_conductor_home(tmp_path, monkeypatch)
        registry_dir = _create_path_registry(tmp_path)
        entry = RegistryEntry(type=RegistryType.path, source=str(registry_dir))

        result = fetch_workflow("my-registry", entry, "qa-bot", ref=None, allow_network=False)
        assert result.exists()


# ---------------------------------------------------------------------------
# E5-T5 load-bearing test: warm cache resolves with github.py fully patched
# ---------------------------------------------------------------------------


class TestWarmCacheZeroNetworkIO:
    """The load-bearing E5 test (NFR1, G9, R2).

    With every function in ``registry/github.py`` patched to raise, a warm
    cache must still resolve a GitHub registry's workflow to its schema
    (input + mcp block) and its pinned SHA — for both an explicit SHA ref
    and a floating ref recorded via the E5-T3 pointer.
    """

    def _populate_warm_cache_with_schema(
        self, home: Path, *, registry_name: str, sha: str, registry_source: str
    ) -> Path:
        from conductor.config.schema import InputDef, McpConfig
        from conductor.registry.cache import _index_to_yaml

        wf_path = _pre_populate_cache(
            home,
            registry_name=registry_name,
            workflow_name="qa-bot",
            sha=sha,
            workflow_repo_path="workflows/qa-bot.yaml",
            registry_source=registry_source,
        )
        # Overwrite the cached index with a tier-1 schema: input + mcp block,
        # serialized through the real _index_to_yaml (the same function
        # fetch_workflow uses to cache a fetched index) rather than
        # handwritten YAML, so the serializer itself is exercised.
        index = RegistryIndex(
            workflows={
                "qa-bot": WorkflowInfo(
                    description="Simple Q&A",
                    path="workflows/qa-bot.yaml",
                    input={
                        "question": InputDef(
                            type="string", required=True, description="The question to ask"
                        )
                    },
                    mcp=McpConfig(expose=True, mode="sync", read_only=True),
                )
            }
        )
        meta = home / "cache" / "registries" / registry_name / "_meta" / sha[:12]
        (meta / "index.yaml").write_text(_index_to_yaml(index), encoding="utf-8")
        return wf_path

    def test_resolves_schema_and_sha_for_pinned_ref(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_conductor_home(tmp_path, monkeypatch)
        wf_path = self._populate_warm_cache_with_schema(
            home, registry_name="official", sha=_FAKE_SHA, registry_source="myorg/workflows"
        )
        _patch_all_github_functions_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow("official", entry, "qa-bot", ref=_FAKE_SHA, allow_network=False)
        assert result == wf_path

        # The schema is answerable from the warm cache alone.
        from conductor.registry.cache import _load_cached_index, _meta_dir

        idx = _load_cached_index(_meta_dir("official", _FAKE_SHA))
        assert idx is not None
        info = idx.workflows["qa-bot"]
        assert info.input["question"].type == "string"
        assert info.mcp.mode == "sync"
        assert info.mcp.read_only is True

    def test_resolves_schema_and_sha_for_floating_ref(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same as above, but for a floating ref resolved via the pointer."""
        home = _setup_conductor_home(tmp_path, monkeypatch)
        wf_path = self._populate_warm_cache_with_schema(
            home, registry_name="official", sha=_FAKE_SHA, registry_source="myorg/workflows"
        )
        _write_ref_pointer("official", "latest", _FAKE_SHA)
        _patch_all_github_functions_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow("official", entry, "qa-bot", ref=None, allow_network=False)
        assert result == wf_path

        from conductor.registry.cache import _load_cached_index, _meta_dir

        idx = _load_cached_index(_meta_dir("official", _FAKE_SHA))
        assert idx is not None
        info = idx.workflows["qa-bot"]
        assert info.input["question"].required is True
        assert info.mcp.expose is True

    def test_resolves_schema_via_parse_cache_when_index_lacks_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier-2 (parse cache) hit: the cached index has no input/mcp (a
        pre-E5 index, or one the source repo simply never declared), so the
        schema can only come from the SHA-keyed parse cache written by
        :func:`save_parsed_tools`. Workflow YAML parsing must never happen —
        ``conductor.config.loader.load_config_string`` is patched to raise,
        proving the schema was served from ``tools.json`` alone.
        """
        home = _setup_conductor_home(tmp_path, monkeypatch)
        wf_path = _pre_populate_cache(
            home,
            registry_name="official",
            workflow_name="qa-bot",
            sha=_FAKE_SHA,
            workflow_repo_path="workflows/qa-bot.yaml",
            registry_source="myorg/workflows",
        )
        # Tier-1 miss: the cached index has no input/mcp for this workflow.
        from conductor.registry.cache import _index_to_yaml

        meta = home / "cache" / "registries" / "official" / "_meta" / _SHA_DIR
        plain_index = RegistryIndex(
            workflows={
                "qa-bot": WorkflowInfo(description="Simple Q&A", path="workflows/qa-bot.yaml")
            }
        )
        (meta / "index.yaml").write_text(_index_to_yaml(plain_index), encoding="utf-8")

        # Tier-2 hit: the SHA-keyed parse cache carries the schema instead.
        from conductor.config.schema import InputDef, McpConfig

        save_parsed_tools(
            "official",
            _FAKE_SHA,
            {
                "qa-bot": ParsedToolInfo(
                    description="Simple Q&A",
                    input={"question": InputDef(type="string", required=True)},
                    mcp=McpConfig(mode="sync", read_only=True),
                )
            },
        )

        _patch_all_github_functions_to_raise(monkeypatch)
        monkeypatch.setattr(
            "conductor.config.loader.load_config_string",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("workflow YAML must not be parsed on a tier-2 cache hit")
            ),
        )

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        result = fetch_workflow("official", entry, "qa-bot", ref=_FAKE_SHA, allow_network=False)
        assert result == wf_path

        from conductor.registry.cache import _load_cached_index, _meta_dir

        idx = _load_cached_index(_meta_dir("official", _FAKE_SHA))
        assert idx is not None
        assert idx.workflows["qa-bot"].input is None
        assert idx.workflows["qa-bot"].mcp is None

        loaded = load_parsed_tools("official", _FAKE_SHA)
        assert loaded is not None
        assert loaded["qa-bot"].input["question"].type == "string"
        assert loaded["qa-bot"].mcp.mode == "sync"
        assert loaded["qa-bot"].mcp.read_only is True
