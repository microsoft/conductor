"""Tests for acquiring git-backed plugin sources.

Everything here runs against real ``git`` over ``file://`` URLs, with the
cache pointed at a temporary directory. Mocking ``subprocess`` would test
the mock: the behaviours that actually matter — an annotated tag
dereferencing to its commit, a bare SHA fetched shallowly, an unreachable
remote falling back to cache — are all properties of git itself.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conductor.plugins.errors import PluginFetchError
from conductor.plugins.fetch import (
    fetch_source,
    fetch_sources,
    get_plugin_cache_base,
    is_cached,
    resolve_ref,
)
from conductor.plugins.sources import parse_plugin_source

from .conftest import make_git_repo, make_plugin


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    """A real git repository holding one plugin, tagged ``v1.0.0``."""
    root = tmp_path / "source-repo"
    make_plugin(root, "demo", skills=["review"], agents=["helper"])
    sha = make_git_repo(root, tag="v1.0.0")
    return root, sha


def _url(root: Path, ref: str | None = None) -> str:
    return f"file://{root}" + (f"#{ref}" if ref else "")


class TestCacheLocation:
    """The cache follows the repo's existing ``$CONDUCTOR_HOME`` convention."""

    def test_honours_conductor_home(self, plugin_cache_home: Path):
        assert get_plugin_cache_base() == plugin_cache_home / "cache" / "plugins"

    def test_defaults_under_dot_conductor(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CONDUCTOR_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/fake/home")))

        assert get_plugin_cache_base() == Path("/fake/home/.conductor/cache/plugins")


class TestFetching:
    """Acquiring a source, and re-acquiring one already on disk."""

    def test_clones_into_a_sha_keyed_directory(self, repo, plugin_cache_home: Path):
        root, sha = repo

        result = fetch_source(parse_plugin_source(_url(root)))

        assert result.sha == sha
        assert result.fetched is True
        assert result.root.name == sha[:12]
        assert (result.root / ".claude-plugin" / "plugin.json").is_file()

    def test_content_arrives_intact(self, repo, plugin_cache_home: Path):
        root, _ = repo

        result = fetch_source(parse_plugin_source(_url(root)))

        assert (result.root / "skills" / "review" / "SKILL.md").is_file()
        assert (result.root / "agents" / "helper.agent.md").is_file()

    def test_a_second_fetch_reuses_the_checkout(self, repo, plugin_cache_home: Path):
        root, _ = repo
        source = parse_plugin_source(_url(root))
        first = fetch_source(source)

        second = fetch_source(source)

        assert second.fetched is False
        assert second.root == first.root

    def test_a_tag_resolves_to_its_commit(self, repo, plugin_cache_home: Path):
        root, sha = repo

        assert fetch_source(parse_plugin_source(_url(root, "v1.0.0"))).sha == sha

    def test_an_annotated_tag_dereferences_to_the_commit(self, repo, plugin_cache_home: Path):
        """git only emits the ``^{}`` line when asked, so this is easy to miss.

        Without dereferencing, the cache is keyed on the tag *object* — a
        SHA no checkout ever equals, so every run would refetch.
        """
        import subprocess

        root, sha = repo
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "tag", "-a", "v2", "-m", "x"],
            cwd=root,
            check=True,
            capture_output=True,
        )

        assert fetch_source(parse_plugin_source(_url(root, "v2"))).sha == sha

    def test_a_full_sha_ref_is_honoured(self, repo, plugin_cache_home: Path):
        root, sha = repo

        assert fetch_source(parse_plugin_source(_url(root, sha))).sha == sha

    def test_ref_matching_nothing_is_an_error(self, repo, plugin_cache_home: Path):
        root, _ = repo

        with pytest.raises(PluginFetchError, match="has no ref 'nope'"):
            fetch_source(parse_plugin_source(_url(root, "nope")))

    def test_a_local_source_is_never_fetched(self, plugin_cache_home: Path):
        """Local sources are read in place; reaching here is a caller bug."""
        with pytest.raises(PluginFetchError, match="read in place"):
            fetch_source(parse_plugin_source("./vendor/plugins"))


class TestPinning:
    """A pinned source never touches the network again."""

    def test_pinned_source_resolves_without_the_remote(self, repo, plugin_cache_home: Path):
        root, sha = repo
        fetch_source(parse_plugin_source(_url(root, sha)))
        shutil.rmtree(root)

        result = fetch_source(parse_plugin_source(_url(root, sha)))

        assert result.sha == sha
        assert result.stale is False

    def test_resolve_ref_short_circuits_for_a_pinned_source(self, plugin_cache_home: Path):
        """No remote exists at all, so any network access would fail."""
        sha = "9c4e1f2a8b3d5e7091a2c4b6d8e0f1a3c5b7d9e1"

        assert resolve_ref(parse_plugin_source(f"file:///nonexistent#{sha}")) == sha

    def test_a_floating_source_picks_up_a_moved_ref(self, repo, plugin_cache_home: Path):
        import subprocess

        root, first_sha = repo
        fetch_source(parse_plugin_source(_url(root, "main")))
        (root / "NEW.md").write_text("added", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "second"],
            cwd=root,
            check=True,
            capture_output=True,
        )

        from conductor.plugins.fetch import clear_resolution_memo

        clear_resolution_memo()
        result = fetch_source(parse_plugin_source(_url(root, "main")))

        assert result.sha != first_sha
        assert (result.root / "NEW.md").is_file()


class TestOfflineFallback:
    """An unreachable remote must not break a run that has the content."""

    def test_warns_and_reuses_the_cached_checkout(self, repo, plugin_cache_home: Path):
        from conductor.plugins.fetch import clear_resolution_memo

        root, sha = repo
        source = parse_plugin_source(_url(root, "v1.0.0"))
        fetch_source(source)
        shutil.rmtree(root)
        clear_resolution_memo()

        warnings: list[str] = []
        result = fetch_source(source, on_warning=warnings.append)

        assert result.sha == sha
        assert result.stale is True
        assert "using the cached checkout" in warnings[0]

    def test_errors_when_nothing_is_cached(self, tmp_path: Path, plugin_cache_home: Path):
        missing = tmp_path / "never-existed"

        with pytest.raises(PluginFetchError):
            fetch_source(parse_plugin_source(_url(missing)))

    def test_the_ref_pointer_is_what_makes_the_fallback_possible(
        self, repo, plugin_cache_home: Path
    ):
        """Without a record of what the ref meant, no checkout can be chosen."""
        from conductor.plugins.fetch import clear_resolution_memo

        root, sha = repo
        source = parse_plugin_source(_url(root, "v1.0.0"))
        fetch_source(source)
        pointers = list((get_plugin_cache_base()).rglob("_refs/*.json"))
        assert len(pointers) == 1
        assert json.loads(pointers[0].read_text())["sha"] == sha

        pointers[0].unlink()
        shutil.rmtree(root)
        clear_resolution_memo()

        with pytest.raises(PluginFetchError):
            fetch_source(source)


class TestCacheOnlyMode:
    """``allow_network=False`` is what keeps ``conductor validate`` offline."""

    def test_uses_a_warm_cache(self, repo, plugin_cache_home: Path):
        root, sha = repo
        source = parse_plugin_source(_url(root, "v1.0.0"))
        fetch_source(source)

        result = fetch_source(source, allow_network=False)

        assert result.sha == sha
        assert result.fetched is False

    def test_names_the_fetch_verb_on_a_cold_cache(self, repo, plugin_cache_home: Path):
        root, _ = repo

        with pytest.raises(PluginFetchError, match="conductor plugin fetch"):
            fetch_source(parse_plugin_source(_url(root)), allow_network=False)


class TestReadiness:
    """A partial checkout must never be mistaken for a complete one."""

    def test_a_checkout_without_its_sentinel_is_not_cached(self, repo, plugin_cache_home: Path):
        root, sha = repo
        source = parse_plugin_source(_url(root))
        fetch_source(source)
        assert is_cached(source, sha) is True

        sentinel = next(get_plugin_cache_base().rglob("*.ready"))
        sentinel.unlink()

        assert is_cached(source, sha) is False


class TestFetchSources:
    """Several sources acquired together."""

    def test_returns_one_result_per_remote_source(self, tmp_path: Path, plugin_cache_home: Path):
        roots = []
        for name in ("one", "two", "three"):
            root = tmp_path / name
            make_plugin(root, name)
            make_git_repo(root)
            roots.append(root)
        sources = [parse_plugin_source(_url(root)) for root in roots]

        results = fetch_sources(sources)

        assert sorted(results) == sorted(source.raw for source in sources)
        assert all(result.fetched for result in results.values())

    def test_local_sources_are_left_out(self, plugin_cache_home: Path):
        assert fetch_sources([parse_plugin_source("./vendor/plugins")]) == {}
