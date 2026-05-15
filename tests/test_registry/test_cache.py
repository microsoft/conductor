"""Tests for the registry cache layer."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.registry.cache import (
    CACHE_LAYOUT_VERSION,
    _safe_repo_path,
    auto_fetch_relative_workflow,
    clear_cache,
    fetch_workflow,
    find_registry_cache_location,
    get_cache_base,
    get_cached_workflow_path,
    prune_temp_dirs,
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
