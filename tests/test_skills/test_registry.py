"""Tests for the built-in skill registry."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

from conductor.skills import (
    ResolvedSkill,
    SkillError,
    SkillManifestError,
    SkillNotFoundError,
    SkillPlugin,
    SkillPluginError,
    get_skill_directory,
    list_builtin_skills,
    resolve_skill_plugin,
    resolve_skills,
)
from conductor.skills.registry import _BUILTIN_SKILLS


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


class TestListBuiltinSkills:
    def test_includes_conductor(self) -> None:
        names = list_builtin_skills()
        assert "conductor" in names

    def test_returns_sorted(self) -> None:
        names = list_builtin_skills()
        assert names == sorted(names)


class TestGetSkillDirectory:
    def test_returns_existing_directory(self) -> None:
        path = get_skill_directory("conductor")
        assert isinstance(path, Path)
        assert path.is_dir()
        assert (path / "SKILL.md").is_file()

    def test_returns_absolute_path(self) -> None:
        path = get_skill_directory("conductor")
        assert path.is_absolute()

    def test_unknown_skill_raises(self) -> None:
        with pytest.raises(SkillNotFoundError, match="Unknown skill"):
            get_skill_directory("does-not-exist")

    def test_unknown_skill_lists_available(self) -> None:
        with pytest.raises(SkillNotFoundError, match="conductor"):
            get_skill_directory("does-not-exist")


class TestResolveSkills:
    def test_empty_input_returns_empty(self) -> None:
        assert resolve_skills([]) == []

    def test_single_skill(self) -> None:
        resolved = resolve_skills(["conductor"])
        assert len(resolved) == 1
        assert resolved[0].name == "conductor"
        assert resolved[0].directory.is_dir()

    def test_deduplicates(self) -> None:
        assert len(resolve_skills(["conductor", "conductor"])) == 1

    def test_unknown_raises(self) -> None:
        with pytest.raises(SkillNotFoundError):
            resolve_skills(["conductor", "nope"])


def _make_plugin(
    tmp_path: Path,
    *,
    manifest: str = '{"name": "p"}',
    skill: str = "s",
    frontmatter_name: str | None = "s",
    nest: str = "",
) -> Path:
    """Build a throwaway plugin tree and return its skill directory."""
    root = tmp_path / "plug"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(manifest)
    skill_dir = root / "skills" / nest / skill if nest else root / "skills" / skill
    skill_dir.mkdir(parents=True)
    if frontmatter_name is not None:
        # ``description`` is required frontmatter — without it the manifest
        # parser rejects the skill before any plugin resolution happens, and
        # these tests would stop covering what they name.
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {frontmatter_name}\ndescription: A test skill.\n---\n"
        )
    return skill_dir


class TestResolveSkillPlugin:
    """Built-in skills ship inside a Claude Code plugin, which is how the
    claude-agent-sdk provider loads them (``--plugin-dir`` + qualified name)."""

    def test_builtin_skill_resolves_to_its_plugin(self) -> None:
        plugin = resolve_skill_plugin(get_skill_directory("conductor"))
        assert plugin is not None
        assert plugin.skill_name == "conductor"
        assert plugin.plugin_name == "conductor"
        assert plugin.qualified_name == "conductor:conductor"

    def test_plugin_root_holds_the_manifest(self) -> None:
        plugin = resolve_skill_plugin(get_skill_directory("conductor"))
        assert plugin is not None
        assert (plugin.plugin_root / ".claude-plugin" / "plugin.json").is_file()

    @pytest.mark.parametrize("name", list_builtin_skills())
    def test_frontmatter_name_matches_directory_name(self, name: str) -> None:
        """The CLI enables skills by frontmatter name while the qualified name
        uses the directory name; drift silently loads no skill."""
        skill_dir = get_skill_directory(name)
        frontmatter = (skill_dir / "SKILL.md").read_text().split("---")[1]
        assert f"name: {skill_dir.name}\n" in frontmatter

    def test_builtin_names_match_their_directory_basenames(self) -> None:
        """``skill_name`` is re-derived from the basename, so a registry key
        that diverges from it would silently rename the skill."""
        for name, rel in _BUILTIN_SKILLS.items():
            assert Path(rel).name == name

    def test_manifest_is_packaged_for_wheel_installs(self) -> None:
        """The manifest must ship alongside the skill body, or no plugin root
        resolves and every skills-enabled agent fails."""
        import tomllib

        with (_repo_root() / "pyproject.toml").open("rb") as handle:
            pyproject = tomllib.load(handle)
        included = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        assert "plugins/conductor/.claude-plugin" in included

    def test_manifest_lands_in_the_built_wheel(self, tmp_path: Path) -> None:
        """The force-include entry is only worth as much as the artifact it
        produces — a stray exclude pattern would leave the string in place and
        the manifest out of the wheel."""
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
            cwd=_repo_root(),
            check=True,
            capture_output=True,
        )
        names = zipfile.ZipFile(next(tmp_path.glob("*.whl"))).namelist()
        assert "plugins/conductor/.claude-plugin/plugin.json" in names
        assert "plugins/conductor/skills/conductor/SKILL.md" in names

    def test_directory_outside_a_plugin_returns_none(self, tmp_path: Path) -> None:
        orphan = tmp_path / "skills" / "lonely"
        orphan.mkdir(parents=True)
        assert resolve_skill_plugin(orphan) is None

    def test_manifest_beyond_search_depth_is_ignored(self, tmp_path: Path) -> None:
        """An unbounded walk would let a distant ancestor adopt the skill."""
        skill = _make_plugin(tmp_path, nest="a/b", skill="s", frontmatter_name="s")
        assert resolve_skill_plugin(skill) is None

    def test_plugin_that_does_not_ship_the_skill_is_skipped(self, tmp_path: Path) -> None:
        """A manifest above a skill does not make that plugin its owner."""
        root = tmp_path / "plug"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text('{"name": "unrelated"}')
        stray = root / "elsewhere" / "mySkill"
        stray.mkdir(parents=True)
        (stray / "SKILL.md").write_text("---\nname: mySkill\ndescription: Stray.\n---\n")
        assert resolve_skill_plugin(stray) is None

    @pytest.mark.parametrize(
        "manifest",
        [
            pytest.param("{not json", id="invalid-json"),
            pytest.param('{"version": "1.0.0"}', id="no-name"),
            pytest.param('{"name": 7}', id="non-string-name"),
            pytest.param('{"name": ""}', id="empty-name"),
            pytest.param("[1, 2, 3]", id="json-array"),
            pytest.param("null", id="json-null"),
            pytest.param('"a string"', id="json-string"),
            pytest.param("42", id="json-number"),
        ],
    )
    def test_unusable_manifest_raises(self, tmp_path: Path, manifest: str) -> None:
        """Anything but an object with a usable 'name' is unusable. Raising
        rather than returning None keeps the real reason reachable."""
        skill = _make_plugin(tmp_path, manifest=manifest)
        with pytest.raises(SkillPluginError):
            resolve_skill_plugin(skill)

    @pytest.mark.parametrize("name", ["evil,Bash", "a:b", "has space", "paren)"])
    def test_unsafe_plugin_name_raises(self, tmp_path: Path, name: str) -> None:
        """Names are joined into a delimited --allowedTools value, so a comma
        or colon would split into extra permission rules."""
        skill = _make_plugin(tmp_path, manifest=f'{{"name": "{name}"}}')
        with pytest.raises(SkillPluginError, match="outside"):
            resolve_skill_plugin(skill)

    def test_missing_skill_md_raises(self, tmp_path: Path) -> None:
        skill = _make_plugin(tmp_path, frontmatter_name=None)
        with pytest.raises(SkillPluginError, match="no SKILL.md"):
            resolve_skill_plugin(skill)

    def test_frontmatter_without_name_raises(self, tmp_path: Path) -> None:
        skill = _make_plugin(tmp_path, frontmatter_name=None)
        (skill / "SKILL.md").write_text("---\ndescription: no name here\n---\n")
        with pytest.raises(SkillPluginError, match="no usable 'name'"):
            resolve_skill_plugin(skill)

    def test_frontmatter_name_disagreeing_with_directory_raises(self, tmp_path: Path) -> None:
        """The CLI resolves by frontmatter name; a mismatch would hide the
        skill instead of failing."""
        skill = _make_plugin(tmp_path, skill="dir-name", frontmatter_name="other-name")
        with pytest.raises(SkillPluginError, match="matches nothing"):
            resolve_skill_plugin(skill)

    def test_relative_path_is_resolved(self, tmp_path: Path, monkeypatch) -> None:
        skill = _make_plugin(tmp_path)
        monkeypatch.chdir(skill.parent)
        plugin = resolve_skill_plugin(Path("s"))
        assert plugin is not None
        assert plugin.plugin_root.is_absolute()


class TestSkillPluginInvariants:
    """The type is exported, so it guards itself rather than trusting its
    producer."""

    @pytest.mark.parametrize("bad", ["", "a,b", "a:b", "has space"])
    def test_unsafe_names_rejected(self, bad: str) -> None:
        with pytest.raises(SkillPluginError, match="must match"):
            SkillPlugin(skill_name=bad, plugin_name="p", plugin_root=Path("/plug"))
        with pytest.raises(SkillPluginError, match="must match"):
            SkillPlugin(skill_name="s", plugin_name=bad, plugin_root=Path("/plug"))

    def test_relative_plugin_root_rejected(self) -> None:
        with pytest.raises(SkillPluginError, match="must be absolute"):
            SkillPlugin(skill_name="s", plugin_name="p", plugin_root=Path("relative"))

    def test_invariant_failures_are_skill_plugin_errors(self) -> None:
        """The provider catches SkillPluginError to report the real reason; a
        bare ValueError here would escape as an unhandled exception."""
        with pytest.raises(SkillPluginError):
            SkillPlugin(skill_name="bad!name", plugin_name="p", plugin_root=Path("/plug"))

    def test_unsafe_directory_name_surfaces_as_skill_plugin_error(self, tmp_path: Path) -> None:
        """The directory basename becomes the skill name, and nothing upstream
        constrains it -- the type is the last line of defence."""
        skill = _make_plugin(tmp_path, skill="bad!name", frontmatter_name="bad!name")
        with pytest.raises(SkillPluginError, match="must match"):
            resolve_skill_plugin(skill)

    def test_valid_instance_builds_qualified_name(self) -> None:
        plugin = SkillPlugin(skill_name="s", plugin_name="p", plugin_root=Path("/plug"))
        assert plugin.qualified_name == "p:s"


class TestResolvedSkillInvariants:
    """Also exported, and ``name`` is interpolated unescaped into the
    ``<skill name="...">`` tag the loader emits — so it guards itself for the
    same reason :class:`SkillPlugin` does."""

    def test_name_must_be_the_directory_basename(self) -> None:
        with pytest.raises(SkillNotFoundError, match="must equal its directory"):
            ResolvedSkill(name="other", directory=Path("/skills/acme"), source="./acme")

    def test_directory_must_be_absolute(self) -> None:
        with pytest.raises(SkillNotFoundError, match="must be absolute"):
            ResolvedSkill(name="acme", directory=Path("skills/acme"), source="./acme")

    def test_valid_construction_is_unaffected(self) -> None:
        item = ResolvedSkill(name="acme", directory=Path("/skills/acme"), source="./acme")
        assert item.name == "acme"


class TestSkillErrorHierarchy:
    """Resolution and manifest failures originate in different modules but
    reach the same handlers, so a call site that can trigger both needs one
    correct thing to catch."""

    @pytest.mark.parametrize("exc_type", [SkillNotFoundError, SkillPluginError, SkillManifestError])
    def test_every_skill_failure_shares_a_base(self, exc_type: type[Exception]) -> None:
        assert issubclass(exc_type, SkillError)
        # ValueError so these still nest inside Pydantic field validation.
        assert issubclass(exc_type, ValueError)
