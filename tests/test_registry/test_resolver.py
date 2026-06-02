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


# ---------------------------------------------------------------------------
# Ad-hoc owner/repo refs
# ---------------------------------------------------------------------------


class TestAdhocRef:
    """Tests for the ad-hoc ``workflow@owner/repo[#ref]`` form."""

    def test_adhoc_with_pinned_tag(self) -> None:
        """``analysis@acme/workflows#v1.0.0`` parses as adhoc — no config needed."""
        ref = resolve_ref("analysis@acme/workflows#v1.0.0")
        assert ref.kind == "adhoc"
        assert ref.workflow == "analysis"
        assert ref.adhoc_owner == "acme"
        assert ref.adhoc_repo == "workflows"
        assert ref.ref == "v1.0.0"
        # registry_name preserved as the raw owner/repo string for diagnostics
        assert ref.registry_name == "acme/workflows"
        # registry_entry intentionally None — no config lookup happened
        assert ref.registry_entry is None

    def test_adhoc_with_pinned_branch(self) -> None:
        ref = resolve_ref("analysis@acme/workflows#main")
        assert ref.kind == "adhoc"
        assert ref.adhoc_owner == "acme"
        assert ref.adhoc_repo == "workflows"
        assert ref.ref == "main"

    def test_adhoc_without_ref_defaults_to_none(self) -> None:
        """Omitted ``#ref`` resolves to default branch HEAD at fetch time."""
        ref = resolve_ref("analysis@acme/workflows")
        assert ref.kind == "adhoc"
        assert ref.adhoc_owner == "acme"
        assert ref.adhoc_repo == "workflows"
        assert ref.ref is None

    def test_adhoc_with_dashes_and_dots(self) -> None:
        """Owner/repo names with dashes, dots, underscores are accepted."""
        ref = resolve_ref("qa.bot_v2@my-org/team.workflows_v2#v1.0.0")
        assert ref.kind == "adhoc"
        assert ref.adhoc_owner == "my-org"
        assert ref.adhoc_repo == "team.workflows_v2"
        assert ref.workflow == "qa.bot_v2"

    def test_adhoc_does_not_load_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No call to load_config() when the registry slot looks like owner/repo."""
        called = {"n": 0}

        def fake_load_config() -> RegistriesConfig:
            called["n"] += 1
            return _make_config()

        monkeypatch.setattr("conductor.registry.resolver.load_config", fake_load_config)
        resolve_ref("analysis@acme/workflows#v1.0.0")
        assert called["n"] == 0, "ad-hoc form must not consult registry config"

    def test_adhoc_too_many_slashes_rejected(self) -> None:
        """``owner/repo/extra`` is rejected — exactly one slash required."""
        with pytest.raises(RegistryError, match="Invalid ad-hoc registry source"):
            resolve_ref("analysis@acme/workflows/extra#v1.0.0")

    def test_adhoc_empty_owner_rejected(self) -> None:
        with pytest.raises(RegistryError, match="Invalid ad-hoc registry source"):
            resolve_ref("analysis@/workflows#v1.0.0")

    def test_adhoc_empty_repo_rejected(self) -> None:
        with pytest.raises(RegistryError, match="Invalid ad-hoc registry source"):
            resolve_ref("analysis@acme/#v1.0.0")

    def test_named_registry_not_treated_as_adhoc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Refs without '/' in the registry slot still use named-registry lookup."""
        config = _make_config()
        _patch_config(monkeypatch, config)
        ref = resolve_ref("qa-bot@team#v1.0.0")
        assert ref.kind == "registry"
        assert ref.registry_name == "team"
        assert ref.adhoc_owner is None
        assert ref.adhoc_repo is None

    def test_adhoc_help_in_unknown_registry_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unknown named registry error mentions the ad-hoc fallback."""
        config = _make_config(default="team")
        _patch_config(monkeypatch, config)
        with pytest.raises(RegistryError, match="ad-hoc form") as exc_info:
            resolve_ref("qa-bot@nope#v1.0.0")
        # The hint should suggest the workflow@owner/repo form.
        assert "workflow@owner/repo" in str(exc_info.value)
