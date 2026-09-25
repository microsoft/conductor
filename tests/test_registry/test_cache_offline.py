"""Tests for cache-only dispatch through ``resolve_and_fetch``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conductor.registry.cache import (
    CACHE_LAYOUT_VERSION,
    _readiness_marker_payload,
    _sentinel_path,
    _write_ref_pointer,
    resolve_and_fetch,
)
from conductor.registry.config import RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError
from conductor.registry.resolver import ResolvedRef

_SHA = "a" * 40


def _warm_cache(home: Path) -> Path:
    workflow = home / "cache" / "registries" / "official" / _SHA[:12] / "flows" / "qa.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("workflow:\n  name: qa\n", encoding="utf-8")
    metadata = home / "cache" / "registries" / "official" / "_meta" / _SHA[:12]
    metadata.mkdir(parents=True)
    (metadata / "source.json").write_text(
        json.dumps(
            {
                "cache_layout_version": CACHE_LAYOUT_VERSION,
                "registry_type": "github",
                "source": "acme/workflows",
                "full_sha": _SHA,
            }
        ),
        encoding="utf-8",
    )
    (metadata / "index.yaml").write_text(
        "workflows:\n  qa:\n    description: ''\n    path: flows/qa.yaml\n",
        encoding="utf-8",
    )
    sentinel = _sentinel_path("official", _SHA, "qa")
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(_readiness_marker_payload(), encoding="utf-8")
    _write_ref_pointer("official", "main", _SHA)
    return workflow


def _resolved() -> ResolvedRef:
    return ResolvedRef(
        kind="registry",
        workflow="qa",
        registry_name="official",
        ref="main",
        registry_entry=RegistryEntry(type=RegistryType.github, source="acme/workflows"),
    )


def test_resolve_and_fetch_offline_uses_warm_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: cache-only dispatch resolves a floating ref through its warm pointer.
    home = tmp_path / "home"
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    expected = _warm_cache(home)

    assert resolve_and_fetch(_resolved(), allow_network=False) == expected


def test_resolve_and_fetch_offline_rejects_cold_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: a cold cache raises RegistryError instead of attempting network access.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))

    with pytest.raises(RegistryError, match="network access is not permitted"):
        resolve_and_fetch(_resolved(), allow_network=False)
