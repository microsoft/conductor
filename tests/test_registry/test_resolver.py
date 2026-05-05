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
        assert ref.ref is None
        assert ref.registry_entry is None

    def test_registry_ref_fields(self) -> None:
        entry = RegistryEntry(type=RegistryType.github, source="o/r")
        ref = ResolvedRef(
            kind="registry",
            workflow="qa-bot",
            registry_name="team",
            ref="v1.2.3",
            registry_entry=entry,
        )
        assert ref.kind == "registry"
        assert ref.workflow == "qa-bot"
        assert ref.registry_name == "team"
        assert ref.ref == "v1.2.3"
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
    def test_existing_file_on_disk(self, tmp_path: Path) -> None:
        f = tmp_path / "workflow"
        f.write_text("content")
        ref = resolve_ref(str(f))
        assert ref.kind == "file"
        assert ref.path == Path(str(f))

    def test_yaml_extension_nonexistent(self) -> None:
        ref = resolve_ref("foo.yaml")
        assert ref.kind == "file"
        assert ref.path == Path("foo.yaml")

    def test_yml_extension_nonexistent(self) -> None:
        ref = resolve_ref("foo.yml")
        assert ref.kind == "file"
        assert ref.path == Path("foo.yml")

    def test_relative_path_with_slash(self) -> None:
        ref = resolve_ref("./foo.yml")
        assert ref.kind == "file"
        assert ref.path == Path("./foo.yml")

    def test_absolute_path(self) -> None:
        ref = resolve_ref("/abs/path.yaml")
        assert ref.kind == "file"
        assert ref.path == Path("/abs/path.yaml")

    def test_path_with_backslash(self) -> None:
        ref = resolve_ref("dir\\workflow")
        assert ref.kind == "file"
        assert ref.path == Path("dir\\workflow")


# ---------------------------------------------------------------------------
# resolve_ref — registry references (positive cases)
# ---------------------------------------------------------------------------


class TestResolveRefRegistry:
    def test_bare_name_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        ref = resolve_ref("qa-bot")
        assert ref.kind == "registry"
        assert ref.workflow == "qa-bot"
        assert ref.registry_name == "team"
        assert ref.ref is None
        assert ref.registry_entry == config.registries["team"]

    def test_name_with_tag_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        ref = resolve_ref("qa-bot#v1.2.3")
        assert ref.kind == "registry"
        assert ref.workflow == "qa-bot"
        assert ref.registry_name == "team"
        assert ref.ref == "v1.2.3"

    def test_name_with_branch_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        ref = resolve_ref("qa-bot#main")
        assert ref.kind == "registry"
        assert ref.workflow == "qa-bot"
        assert ref.registry_name == "team"
        assert ref.ref == "main"

    def test_name_with_commit_sha_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        ref = resolve_ref("qa-bot#abc1234")
        assert ref.ref == "abc1234"

    def test_name_at_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        ref = resolve_ref("qa-bot@team")
        assert ref.kind == "registry"
        assert ref.workflow == "qa-bot"
        assert ref.registry_name == "team"
        assert ref.ref is None
        assert ref.registry_entry == config.registries["team"]

    def test_name_at_registry_with_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="local")
        _patch_config(monkeypatch, config)

        ref = resolve_ref("qa-bot@team#v1.2.3")
        assert ref.kind == "registry"
        assert ref.workflow == "qa-bot"
        assert ref.registry_name == "team"
        assert ref.ref == "v1.2.3"
        assert ref.registry_entry == config.registries["team"]

    def test_empty_registry_with_ref_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        ref = resolve_ref("qa-bot@#v1")
        assert ref.kind == "registry"
        assert ref.workflow == "qa-bot"
        assert ref.registry_name == "team"
        assert ref.ref == "v1"

    def test_registry_entry_populated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        ref = resolve_ref("qa-bot@team#v2.0.0")
        assert ref.registry_entry is not None
        assert ref.registry_entry.type == RegistryType.github
        assert ref.registry_entry.source == "acme/workflows"


# ---------------------------------------------------------------------------
# resolve_ref — registry references (negative cases)
# ---------------------------------------------------------------------------


class TestResolveRefRegistryErrors:
    def test_empty_ref_after_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="Ref cannot be empty"):
            resolve_ref("qa-bot#")

    def test_double_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="at most one '#'"):
            resolve_ref("qa-bot##v1")

    def test_multiple_hashes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="at most one '#'"):
            resolve_ref("qa-bot#v1#v2")

    def test_double_at(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="at most one '@'"):
            resolve_ref("qa-bot@@v1")

    def test_multiple_ats(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="at most one '@'"):
            resolve_ref("qa-bot@a@b")

    def test_empty_workflow_with_registry_and_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="Workflow name is required"):
            resolve_ref("@team#v1")

    def test_empty_workflow_with_ref_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="Workflow name is required"):
            resolve_ref("#v1")

    def test_empty_workflow_with_registry_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="Workflow name is required"):
            resolve_ref("@team")

    def test_just_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        # "#" splits into ["", ""], so empty-ref check fires first.
        with pytest.raises(RegistryError, match="Ref cannot be empty"):
            resolve_ref("#")

    def test_no_default_registry_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default=None)
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="No default registry configured"):
            resolve_ref("qa-bot")

    def test_no_default_with_empty_registry_segment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default=None)
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="No default registry configured"):
            resolve_ref("qa-bot@#v1.0.0")

    def test_unknown_registry_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="Registry 'nope' not found"):
            resolve_ref("qa-bot@nope")

    def test_unknown_registry_with_ref_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)

        with pytest.raises(RegistryError, match="Registry 'nope' not found"):
            resolve_ref("qa-bot@nope#v1.0.0")
