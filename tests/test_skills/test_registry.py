"""Tests for the built-in skill registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.skills import (
    SkillNotFoundError,
    get_skill_directory,
    list_builtin_skills,
    resolve_skill_directories,
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
