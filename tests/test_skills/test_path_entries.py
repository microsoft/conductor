"""Tests for path entries in ``skills:`` (issue #350).

Before this, ``skills:`` accepted exactly one hardcoded name and
``get_skill_directory`` raised for anything else, so a team could not
version a skill alongside its workflow. These tests cover the two things
that makes possible — classifying an entry as a name or a path, and
expanding a path at either granularity.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conductor.skills import (
    SkillManifestError,
    SkillNotFoundError,
    get_skill_directory,
    resolve_skill_directories,
    resolve_skills,
)
from conductor.skills.registry import _is_path_entry

_FRONTMATTER = "---\nname: {name}\ndescription: A test skill.\n---\nBody\n"


def _make_skill(directory: Path, name: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(_FRONTMATTER.format(name=name or directory.name))
    return directory


class TestPathClassification:
    """Classification is syntactic so it never depends on what happens to
    exist locally — a bare name cannot be shadowed by a same-named directory."""

    @pytest.mark.parametrize(
        "entry",
        ["./skills/a", "../a", "~/skills", "/abs/a", "team/a", r"team\a", "~"],
    )
    def test_path_shaped_entries(self, entry: str) -> None:
        assert _is_path_entry(entry) is True

    @pytest.mark.parametrize("entry", ["conductor", "acme-widgets", "a_b.c"])
    def test_name_shaped_entries(self, entry: str) -> None:
        assert _is_path_entry(entry) is False

    def test_bare_name_is_not_shadowed_by_a_local_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A directory named ``conductor`` in the cwd must not hijack the
        built-in — otherwise resolution would silently depend on cwd."""
        _make_skill(tmp_path / "conductor")
        monkeypatch.chdir(tmp_path)
        resolved = resolve_skills(["conductor"])
        assert resolved[0].directory == get_skill_directory("conductor")


class TestResolveNames:
    def test_builtin_name_resolves(self) -> None:
        resolved = resolve_skills(["conductor"])
        assert [item.name for item in resolved] == ["conductor"]
        assert resolved[0].directory == get_skill_directory("conductor")
        assert resolved[0].source == "conductor"

    def test_unknown_name_points_at_the_path_form(self) -> None:
        with pytest.raises(SkillNotFoundError, match="Unknown skill 'nope'") as exc_info:
            resolve_skills(["nope"])
        assert "./team-skills/my-skill" in str(exc_info.value)


class TestResolveSingleSkillDirectory:
    def test_absolute_path(self, tmp_path: Path) -> None:
        skill = _make_skill(tmp_path / "acme-widgets")
        resolved = resolve_skills([str(skill)])
        assert [(item.name, item.directory) for item in resolved] == [("acme-widgets", skill)]

    def test_relative_path_resolves_against_base_dir(self, tmp_path: Path) -> None:
        skill = _make_skill(tmp_path / "team-skills" / "acme-widgets")
        resolved = resolve_skills(["./team-skills/acme-widgets"], base_dir=tmp_path)
        assert resolved[0].directory == skill
        assert resolved[0].source == "./team-skills/acme-widgets"

    def test_relative_path_falls_back_to_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skill = _make_skill(tmp_path / "acme")
        monkeypatch.chdir(tmp_path)
        assert resolve_skills(["./acme"])[0].directory == skill

    def test_user_home_is_expanded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        skill = _make_skill(tmp_path / "scratch" / "acme")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert resolve_skills(["~/scratch/acme"])[0].directory == skill

    def test_dot_segments_are_normalised(self, tmp_path: Path) -> None:
        skill = _make_skill(tmp_path / "a" / "acme")
        resolved = resolve_skills(["./a/../a/acme"], base_dir=tmp_path)
        assert resolved[0].directory == skill

    def test_symlink_alias_is_not_collapsed(self, tmp_path: Path) -> None:
        """``normpath`` rather than ``resolve()``, matching how the engine
        treats ``working_dir`` — a symlinked path stays the path the user wrote."""
        skill = _make_skill(tmp_path / "real" / "acme")
        link = tmp_path / "link"
        link.symlink_to(tmp_path / "real", target_is_directory=True)
        assert resolve_skills([str(link / "acme")])[0].directory == link / "acme"
        assert skill.exists()


class TestResolveSkillsRoot:
    def test_root_expands_to_every_child(self, tmp_path: Path) -> None:
        root = tmp_path / "skills"
        for name in ("gamma", "alpha", "beta"):
            _make_skill(root / name)
        resolved = resolve_skills([str(root)])
        assert [item.name for item in resolved] == ["alpha", "beta", "gamma"]

    def test_expanded_children_share_the_written_source(self, tmp_path: Path) -> None:
        root = tmp_path / "skills"
        for name in ("alpha", "beta"):
            _make_skill(root / name)
        resolved = resolve_skills(["./skills"], base_dir=tmp_path)
        assert {item.source for item in resolved} == {"./skills"}

    def test_children_without_skill_md_are_not_skills(self, tmp_path: Path) -> None:
        root = tmp_path / "skills"
        _make_skill(root / "alpha")
        (root / "not-a-skill").mkdir()
        assert [item.name for item in resolve_skills([str(root)])] == ["alpha"]

    def test_a_directory_holding_skill_md_wins_over_child_scan(self, tmp_path: Path) -> None:
        """A skill directory that happens to contain a nested skill directory
        resolves as itself, not as a root."""
        skill = _make_skill(tmp_path / "acme")
        _make_skill(skill / "nested")
        assert [item.name for item in resolve_skills([str(skill)])] == ["acme"]

    def test_expansion_is_not_recursive(self, tmp_path: Path) -> None:
        root = tmp_path / "skills"
        _make_skill(root / "group" / "deep")
        with pytest.raises(SkillNotFoundError, match="neither a SKILL.md nor"):
            resolve_skills([str(root)])


class TestResolutionErrors:
    def test_missing_path(self, tmp_path: Path) -> None:
        with pytest.raises(SkillNotFoundError, match="does not exist"):
            resolve_skills(["./nope"], base_dir=tmp_path)

    def test_path_pointing_at_a_file(self, tmp_path: Path) -> None:
        (tmp_path / "a-file").write_text("hi")
        with pytest.raises(SkillNotFoundError, match="is not a directory"):
            resolve_skills(["./a-file"], base_dir=tmp_path)

    def test_empty_directory(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        with pytest.raises(SkillNotFoundError, match="neither a SKILL.md nor"):
            resolve_skills(["./empty"], base_dir=tmp_path)

    def test_malformed_frontmatter_fails_resolution(self, tmp_path: Path) -> None:
        """Resolution — not just ``conductor validate`` — rejects a broken
        manifest, because ``conductor run`` never calls the static validator."""
        skill = tmp_path / "acme"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: acme\ndescription: A. Triggers: b, c\n---\n")
        with pytest.raises(SkillManifestError, match="invalid YAML frontmatter"):
            resolve_skills([str(skill)])

    def test_one_bad_skill_in_a_root_fails_the_whole_entry(self, tmp_path: Path) -> None:
        root = tmp_path / "skills"
        _make_skill(root / "good")
        bad = root / "bad"
        bad.mkdir(parents=True)
        (bad / "SKILL.md").write_text("---\nname: bad\n---\n")
        with pytest.raises(SkillManifestError, match="no usable 'description'"):
            resolve_skills([str(root)])


class TestOrderingAndDeduplication:
    def test_entry_order_is_preserved(self, tmp_path: Path) -> None:
        zulu = _make_skill(tmp_path / "zulu")
        alpha = _make_skill(tmp_path / "alpha")
        resolved = resolve_skills([str(zulu), str(alpha), "conductor"])
        assert [item.name for item in resolved] == ["zulu", "alpha", "conductor"]

    def test_duplicate_directories_collapse_to_first_occurrence(self, tmp_path: Path) -> None:
        root = tmp_path / "skills"
        _make_skill(root / "alpha")
        _make_skill(root / "beta")
        resolved = resolve_skills([str(root), str(root / "alpha")])
        assert [item.name for item in resolved] == ["alpha", "beta"]
        assert resolved[0].source == str(root), "first occurrence wins"

    def test_name_and_equivalent_path_deduplicate(self) -> None:
        builtin = get_skill_directory("conductor")
        resolved = resolve_skills(["conductor", str(builtin)])
        assert [item.name for item in resolved] == ["conductor"]


class TestResolveSkillDirectoriesWrapper:
    def test_returns_directories_only(self, tmp_path: Path) -> None:
        skill = _make_skill(tmp_path / "acme")
        assert resolve_skill_directories([str(skill)]) == [skill]

    def test_forwards_base_dir(self, tmp_path: Path) -> None:
        skill = _make_skill(tmp_path / "acme")
        assert resolve_skill_directories(["./acme"], base_dir=tmp_path) == [skill]


class TestUnreadableDirectory:
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root bypasses directory permissions",
    )
    def test_unreadable_directory_is_reported_not_raised_raw(self, tmp_path: Path) -> None:
        """A stat-able but unreadable directory must name the entry rather than
        surfacing a bare PermissionError traceback from ``iterdir``."""
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        blocked.chmod(0o000)
        try:
            with pytest.raises(SkillNotFoundError, match="could not be read"):
                resolve_skills([str(blocked)])
        finally:
            blocked.chmod(0o755)
