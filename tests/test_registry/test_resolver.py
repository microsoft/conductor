"""Tests for conductor.registry.resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.registry.config import RegistriesConfig, RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError
from conductor.registry.resolver import ResolvedRef, _looks_like_file_path, resolve_ref

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_config(monkeypatch: pytest.MonkeyPatch, config: RegistriesConfig) -> None:
    """Monkey-patch ``load_config`` to return *config*."""
    monkeypatch.setattr(
        "conductor.registry.resolver.load_config",
        lambda: config,
    )


def _make_config(
    *,
    default: str | None = None,
    registries: dict[str, RegistryEntry] | None = None,
) -> RegistriesConfig:
    if registries is None:
        registries = {
            "team": RegistryEntry(type=RegistryType.github, source="acme/workflows"),
            "local": RegistryEntry(type=RegistryType.path, source="/opt/workflows"),
        }
    return RegistriesConfig(default=default, registries=registries)


# ---------------------------------------------------------------------------
# ResolvedRef dataclass
# ---------------------------------------------------------------------------


class TestResolvedRef:
    """Basic sanity checks for the frozen dataclass."""

    def test_file_ref_fields(self) -> None:
        ref = ResolvedRef(kind="file", path=Path("my.yaml"))
        assert ref.kind == "file"
        assert ref.path == Path("my.yaml")
        assert ref.workflow is None
        assert ref.registry_name is None
        assert ref.version is None
        assert ref.registry_entry is None

    def test_registry_ref_fields(self) -> None:
        entry = RegistryEntry(type=RegistryType.github, source="o/r")
        ref = ResolvedRef(
            kind="registry",
            workflow="qa-bot",
            registry_name="team",
            version="1.2.3",
            registry_entry=entry,
        )
        assert ref.kind == "registry"
        assert ref.workflow == "qa-bot"
        assert ref.registry_name == "team"
        assert ref.version == "1.2.3"
        assert ref.registry_entry is entry

    def test_frozen(self) -> None:
        ref = ResolvedRef(kind="file", path=Path("a.yaml"))
        with pytest.raises(AttributeError):
            ref.kind = "registry"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _looks_like_file_path
# ---------------------------------------------------------------------------


class TestLooksLikeFilePath:
    def test_forward_slash(self) -> None:
        assert _looks_like_file_path("./my-workflow.yaml") is True
        assert _looks_like_file_path("dir/workflow") is True

    def test_backslash(self) -> None:
        assert _looks_like_file_path("dir\\workflow.yaml") is True

    def test_yaml_extension(self) -> None:
        assert _looks_like_file_path("my-workflow.yaml") is True
        assert _looks_like_file_path("my-workflow.yml") is True
        assert _looks_like_file_path("MY-WORKFLOW.YAML") is True

    def test_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "wf"
        f.write_text("hello")
        assert _looks_like_file_path(str(f)) is True

    def test_plain_name_not_file(self) -> None:
        assert _looks_like_file_path("qa-bot") is False


# ---------------------------------------------------------------------------
# resolve_ref — file paths
# ---------------------------------------------------------------------------


class TestResolveRefFile:
    def test_existing_file_on_disk(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        f = tmp_path / "workflow"
        f.write_text("content")
        ref = resolve_ref(str(f))
        assert ref.kind == "file"
        assert ref.path == Path(str(f))

    def test_yaml_extension_nonexistent(self) -> None:
        ref = resolve_ref("nonexistent.yaml")
        assert ref.kind == "file"
        assert ref.path == Path("nonexistent.yaml")

    def test_yml_extension_nonexistent(self) -> None:
        ref = resolve_ref("nonexistent.yml")
        assert ref.kind == "file"
        assert ref.path == Path("nonexistent.yml")

    def test_path_with_slash(self) -> None:
        ref = resolve_ref("./my-workflow.yaml")
        assert ref.kind == "file"
        assert ref.path == Path("./my-workflow.yaml")

    def test_path_with_backslash(self) -> None:
        ref = resolve_ref("dir\\workflow")
        assert ref.kind == "file"
        assert ref.path == Path("dir\\workflow")


# ---------------------------------------------------------------------------
# resolve_ref — registry references
# ---------------------------------------------------------------------------


class TestResolveRefRegistry:
    def test_name_only_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        ref = resolve_ref("qa-bot")
        assert ref.kind == "registry"
        assert ref.workflow == "qa-bot"
        assert ref.registry_name == "team"
        assert ref.version is None
        assert ref.registry_entry == config.registries["team"]

    def test_name_at_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        ref = resolve_ref("qa-bot@local")
        assert ref.kind == "registry"
        assert ref.workflow == "qa-bot"
        assert ref.registry_name == "local"
        assert ref.version is None
        assert ref.registry_entry == config.registries["local"]

    def test_name_at_registry_at_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        ref = resolve_ref("qa-bot@team@1.2.3")
        assert ref.kind == "registry"
        assert ref.workflow == "qa-bot"
        assert ref.registry_name == "team"
        assert ref.version == "1.2.3"

    def test_name_at_empty_at_version_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        ref = resolve_ref("qa-bot@@1.2.3")
        assert ref.kind == "registry"
        assert ref.workflow == "qa-bot"
        assert ref.registry_name == "team"
        assert ref.version == "1.2.3"

    def test_missing_default_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default=None)
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="No default registry configured"):
            resolve_ref("qa-bot")

    def test_missing_default_with_empty_registry_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _make_config(default=None)
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="No default registry configured"):
            resolve_ref("qa-bot@@1.0.0")

    def test_unknown_registry_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="Registry 'nope' not found"):
            resolve_ref("qa-bot@nope")

    def test_registry_entry_populated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        ref = resolve_ref("qa-bot@team@2.0.0")
        assert ref.registry_entry is not None
        assert ref.registry_entry.type == RegistryType.github
        assert ref.registry_entry.source == "acme/workflows"
