"""Resolve workflow refs to git refs and immutable SHAs."""

from __future__ import annotations

from packaging.version import InvalidVersion, Version

from conductor.registry.config import RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError
from conductor.registry.github import (
    get_default_branch,
    list_tags,
    parse_github_source,
    resolve_ref_to_sha,
)


def resolve_ref(entry: RegistryEntry, requested: str | None) -> str:
    """Resolve a requested ref (or "latest") to a concrete git ref name.

    For path registries, raises RegistryError if ``requested`` is non-empty
    (path registries do not support refs).

    For github registries:
      * If ``requested`` is None or "latest", returns the newest tag
        (semver-sorted when possible). If no tags exist, returns the default
        branch name.
      * Otherwise returns ``requested`` verbatim — this allows pinning to any
        tag, branch, or commit SHA.
    """
    if entry.type == RegistryType.path:
        if requested is not None and requested != "":
            raise RegistryError(
                "Path registries do not support refs",
                suggestion=(
                    "Path registries always read from the source directory; "
                    "remove '#<ref>' from your reference."
                ),
            )
        return ""

    if requested is None or requested.lower() == "latest":
        owner, repo = parse_github_source(entry.source)
        tags = list_tags(owner, repo)
        if tags:
            return sort_tags(tags)[0]
        return get_default_branch(owner, repo)

    return requested


def materialize_to_sha(entry: RegistryEntry, ref: str) -> str:
    """Resolve a git ref to a full immutable commit SHA.

    Used as the cache key so mutable branch refs are always re-resolved to
    the current commit before fetching. For path registries, raises (callers
    should not invoke this for path).
    """
    if entry.type == RegistryType.path:
        raise RegistryError("materialize_to_sha not applicable to path registries")
    owner, repo = parse_github_source(entry.source)
    return resolve_ref_to_sha(owner, repo, ref)


def sort_tags(tags: list[str]) -> list[str]:
    """Sort tags newest-first, preferring semver order for parseable tags.

    Tags that parse as PEP 440 / semver (after stripping a leading ``v``) are
    placed first in descending version order. Unparseable tags follow in
    their original input order (which is GitHub's newest-commit-first).
    """
    parseable: list[tuple[Version, str]] = []
    unparseable: list[str] = []
    for tag in tags:
        candidate = tag[1:] if tag.startswith(("v", "V")) else tag
        try:
            parseable.append((Version(candidate), tag))
        except InvalidVersion:
            unparseable.append(tag)

    parseable.sort(key=lambda pair: pair[0], reverse=True)
    return [tag for _, tag in parseable] + unparseable
