"""Tests for conductor.registry.version_resolver."""

from __future__ import annotations

import pytest

from conductor.registry import version_resolver
from conductor.registry.config import RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError
from conductor.registry.version_resolver import (
    _sort_tags,
    materialize_to_sha,
    resolve_ref,
)


def _path_entry() -> RegistryEntry:
    return RegistryEntry(type=RegistryType.path, source="/tmp/workflows")


def _gh_entry() -> RegistryEntry:
    return RegistryEntry(type=RegistryType.github, source="acme/widgets")


# ---------------------------------------------------------------------------
# resolve_ref — path registries
# ---------------------------------------------------------------------------


def test_resolve_ref_path_none_returns_empty() -> None:
    assert resolve_ref(_path_entry(), None) == ""


def test_resolve_ref_path_empty_string_returns_empty() -> None:
    assert resolve_ref(_path_entry(), "") == ""


def test_resolve_ref_path_with_ref_raises() -> None:
    with pytest.raises(RegistryError) as exc_info:
        resolve_ref(_path_entry(), "v1")
    assert "Path registries do not support refs" in str(exc_info.value)
    assert exc_info.value.suggestion is not None
    assert "remove '#<ref>'" in exc_info.value.suggestion


# ---------------------------------------------------------------------------
# resolve_ref — github registries
# ---------------------------------------------------------------------------


def test_resolve_ref_github_none_picks_newest_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        version_resolver, "list_tags", lambda owner, repo: ["v1.0.0", "v2.0.0", "v1.1.0"]
    )
    monkeypatch.setattr(
        version_resolver,
        "get_default_branch",
        lambda owner, repo: pytest.fail("should not be called"),
    )
    assert resolve_ref(_gh_entry(), None) == "v2.0.0"


def test_resolve_ref_github_latest_same_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        version_resolver, "list_tags", lambda owner, repo: ["v1.0.0", "v2.0.0", "v1.1.0"]
    )
    assert resolve_ref(_gh_entry(), "latest") == "v2.0.0"
    assert resolve_ref(_gh_entry(), "LATEST") == "v2.0.0"


def test_resolve_ref_github_no_tags_returns_default_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(version_resolver, "list_tags", lambda owner, repo: [])
    monkeypatch.setattr(version_resolver, "get_default_branch", lambda owner, repo: "main")
    assert resolve_ref(_gh_entry(), None) == "main"


def test_resolve_ref_github_branch_returned_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        version_resolver,
        "list_tags",
        lambda owner, repo: pytest.fail("should not be called"),
    )
    assert resolve_ref(_gh_entry(), "main") == "main"


def test_resolve_ref_github_tag_returned_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        version_resolver,
        "list_tags",
        lambda owner, repo: pytest.fail("should not be called"),
    )
    assert resolve_ref(_gh_entry(), "v1.0.0") == "v1.0.0"


# ---------------------------------------------------------------------------
# _sort_tags
# ---------------------------------------------------------------------------


def test_sort_tags_v_prefix() -> None:
    assert _sort_tags(["v1.0.0", "v2.0.0", "v1.1.0"]) == ["v2.0.0", "v1.1.0", "v1.0.0"]


def test_sort_tags_prereleases() -> None:
    assert _sort_tags(["1.0.0", "1.0.0-rc1", "0.9.0"]) == ["1.0.0", "1.0.0-rc1", "0.9.0"]


def test_sort_tags_mixed_parseable_and_not() -> None:
    result = _sort_tags(["v1.0.0", "release-2024", "v2.0.0", "experimental"])
    assert result == ["v2.0.0", "v1.0.0", "release-2024", "experimental"]


def test_sort_tags_empty() -> None:
    assert _sort_tags([]) == []


# ---------------------------------------------------------------------------
# materialize_to_sha
# ---------------------------------------------------------------------------


def test_materialize_to_sha_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_resolve(owner: str, repo: str, ref: str) -> str:
        captured["args"] = (owner, repo, ref)
        return "abc123" * 6 + "abcd"  # 40 chars

    monkeypatch.setattr(version_resolver, "resolve_ref_to_sha", fake_resolve)
    sha = materialize_to_sha(_gh_entry(), "main")
    assert sha == "abc123" * 6 + "abcd"
    assert captured["args"] == ("acme", "widgets", "main")


def test_materialize_to_sha_path_raises() -> None:
    with pytest.raises(RegistryError, match="not applicable to path registries"):
        materialize_to_sha(_path_entry(), "main")
