"""Tests for the built-in skill registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.skills import (
    SkillNotFoundError,
    get_skill_directory,
    list_builtin_skills,
    resolve_skill_directories,
    resolve_skill_plugin,
)


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


class TestResolveSkillDirectories:
    def test_empty_input_returns_empty(self) -> None:
        assert resolve_skill_directories([]) == []

    def test_single_skill(self) -> None:
        dirs = resolve_skill_directories(["conductor"])
        assert len(dirs) == 1
        assert dirs[0].is_dir()

    def test_deduplicates(self) -> None:
        dirs = resolve_skill_directories(["conductor", "conductor"])
        assert len(dirs) == 1

    def test_unknown_raises(self) -> None:
        with pytest.raises(SkillNotFoundError):
            resolve_skill_directories(["conductor", "nope"])


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

    def test_manifest_is_packaged_for_wheel_installs(self) -> None:
        """The manifest must ship alongside the skill body, or an installed
        wheel resolves a plugin root the CLI cannot load."""
        import tomllib

        repo_root = Path(__file__).resolve().parents[2]
        with (repo_root / "pyproject.toml").open("rb") as handle:
            pyproject = tomllib.load(handle)
        included = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        assert "plugins/conductor/.claude-plugin" in included

    def test_directory_outside_a_plugin_returns_none(self, tmp_path: Path) -> None:
        orphan = tmp_path / "skills" / "lonely"
        orphan.mkdir(parents=True)
        assert resolve_skill_plugin(orphan) is None

    def test_unreadable_manifest_returns_none(self, tmp_path: Path) -> None:
        root = tmp_path / "plug"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text("{not json")
        skill = root / "skills" / "s"
        skill.mkdir(parents=True)
        assert resolve_skill_plugin(skill) is None

    def test_manifest_without_name_returns_none(self, tmp_path: Path) -> None:
        root = tmp_path / "plug"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text('{"version": "1.0.0"}')
        skill = root / "skills" / "s"
        skill.mkdir(parents=True)
        assert resolve_skill_plugin(skill) is None

    @pytest.mark.parametrize("manifest", ["[1, 2, 3]", "null", '"a string"', "42"])
    def test_non_object_manifest_returns_none(self, tmp_path: Path, manifest: str) -> None:
        """Valid JSON that is not an object is as unusable as invalid JSON."""
        root = tmp_path / "plug"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(manifest)
        skill = root / "skills" / "s"
        skill.mkdir(parents=True)
        assert resolve_skill_plugin(skill) is None

    def test_non_string_name_returns_none(self, tmp_path: Path) -> None:
        root = tmp_path / "plug"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text('{"name": 7}')
        skill = root / "skills" / "s"
        skill.mkdir(parents=True)
        assert resolve_skill_plugin(skill) is None
