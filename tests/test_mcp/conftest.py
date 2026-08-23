"""Shared fixtures for ``conductor mcp serve`` tests.

Provides a home-directory fixture and small builder helpers for the two
registry shapes the catalogue builder (E7) must resolve: a local path
registry, and a warm, offline-resolvable GitHub-registry cache. Individual
test modules import the specific pieces they need rather than a single
monolithic fixture, matching the existing ``tests/test_registry/`` style.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from conductor.registry.cache import CACHE_LAYOUT_VERSION
from conductor.registry.config import RegistryEntry, RegistryType


@pytest.fixture
def conductor_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``CONDUCTOR_HOME`` at a fresh temp directory, isolating the
    registry cache (and anything else keyed off it) from the developer's
    real ``~/.conductor``."""
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    return home


def write_path_registry(
    root: Path, *, name: str = "registry", workflows: dict[str, str]
) -> RegistryEntry:
    """Write a minimal local path registry to ``root / name`` and return its
    ``RegistryEntry``.

    ``workflows`` maps a workflow key to the raw YAML text of its workflow
    file (already a complete, loadable ``workflow:``/``agents:`` document).
    Each is written at ``workflows/<key>.yaml`` and referenced from a
    generated ``index.yaml`` with an empty ``description:`` — callers that
    need tier-1 index-provided ``input:``/``mcp:`` blocks should write
    ``index.yaml`` themselves instead.
    """
    registry_dir = root / name
    wf_dir = registry_dir / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)

    index_entries = "\n".join(
        f'  {key}:\n    description: ""\n    path: workflows/{key}.yaml' for key in workflows
    )
    (registry_dir / "index.yaml").write_text(f"workflows:\n{index_entries}\n", encoding="utf-8")
    for key, yaml_text in workflows.items():
        (wf_dir / f"{key}.yaml").write_text(textwrap.dedent(yaml_text), encoding="utf-8")

    return RegistryEntry(type=RegistryType.path, source=str(registry_dir))


def populate_github_warm_cache(
    home: Path,
    *,
    registry_name: str,
    sha: str,
    registry_source: str,
    index_yaml: str,
    workflow_files: dict[str, bytes] | None = None,
    ref: str | None = "main",
) -> None:
    """Populate the on-disk registry cache as if a prior online build had
    already warmed it: the SHA-rooted mirror (for any workflow files given),
    ``_meta/<sha>/source.json`` + ``index.yaml``, and (when ``ref`` is not
    ``None``) the ``_meta/_refs/`` pointer recording what that floating ref
    last resolved to (E5-T3).

    A subsequent ``build_catalogue(..., allow_network=False)`` against this
    registry must resolve entirely from what this function wrote.
    """
    from conductor.registry.cache import _write_ref_pointer

    base = home / "cache" / "registries" / registry_name
    sha_dir = sha[:12]

    sha_root = base / sha_dir
    for repo_path, content in (workflow_files or {}).items():
        target = sha_root / repo_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    meta_dir = base / "_meta" / sha_dir
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "source.json").write_text(
        json.dumps(
            {
                "cache_layout_version": CACHE_LAYOUT_VERSION,
                "registry_type": "github",
                "source": registry_source,
                "full_sha": sha,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (meta_dir / "index.yaml").write_text(textwrap.dedent(index_yaml), encoding="utf-8")

    if ref is not None:
        _write_ref_pointer(registry_name, ref if ref != "main" else None, sha)


def patch_github_network_to_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch every ``registry/github.py`` function to raise, everywhere it
    is bound (the module itself, plus the copies ``cache.py`` /
    ``version_resolver.py`` imported into their own namespaces at module
    load time). Mirrors ``tests/test_registry/test_cache.py``'s
    ``_patch_all_github_functions_to_raise`` — the load-bearing pattern
    that proves a warm cache never touches the network (NFR1)."""
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
