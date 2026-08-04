"""Tests for skill discovery (issue #362).

Discovery was split out of #350 because the obvious design — one flag,
each provider discovering its own locations — would surface different
skill sets to different agents inside a single run. Conductor scans the
union of both CLIs' locations itself instead, so these tests are largely
about two properties that fall out of that decision: the set does not
depend on which provider an agent resolves to, and it does not depend on
the order ``sources:`` happens to be written in.

Every test passes an explicit ``home``. Reading the real one would make
results depend on what the machine running the suite has installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conductor.skills import (
    SkillNotFoundError,
    discover_skills,
    resolve_effective_skills,
)

_FRONTMATTER = "---\nname: {name}\ndescription: A test skill.\n---\nBody\n"


def _make_skill(directory: Path, name: str | None = None) -> Path:
    """Create a valid skill directory."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(_FRONTMATTER.format(name=name or directory.name))
    return directory


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An isolated home directory for discovery to scan."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    return fake_home


class TestDisabledByDefault:
    """Discovery has to be asked for; ambient behaviour is opt-in."""

    def test_no_sources_finds_nothing(self, home: Path) -> None:
        _make_skill(home / ".copilot" / "skills" / "alpha")
        assert discover_skills([], home=home) == []

    def test_no_sources_short_circuits_before_touching_disk(self) -> None:
        # ``home=None`` would resolve the real home directory; returning
        # early means the default config never reads the filesystem.
        assert discover_skills([], home=None) == []


class TestPersonalSource:
    """``personal`` covers both CLIs' home directories."""

    def test_finds_copilot_and_claude_skills(self, home: Path) -> None:
        _make_skill(home / ".copilot" / "skills" / "alpha")
        _make_skill(home / ".claude" / "skills" / "beta")
        found = discover_skills(["personal"], home=home)
        assert [skill.name for skill in found] == ["alpha", "beta"]
        assert {skill.source for skill in found} == {"personal"}

    def test_absent_roots_are_not_an_error(self, home: Path) -> None:
        _make_skill(home / ".copilot" / "skills" / "alpha")
        # ``~/.claude/skills`` does not exist; most users have only one.
        assert [skill.name for skill in discover_skills(["personal"], home=home)] == ["alpha"]

    def test_root_itself_is_not_treated_as_a_skill(self, home: Path) -> None:
        # A SKILL.md directly in the root would make the root a skill
        # directory under path-entry rules; as a discovery location it is
        # always a root, so its children are what count.
        root = home / ".copilot" / "skills"
        _make_skill(root / "alpha")
        (root / "SKILL.md").write_text(_FRONTMATTER.format(name="skills"))
        assert [skill.name for skill in discover_skills(["personal"], home=home)] == ["alpha"]


class TestProjectSource:
    """``project`` walks up from the workflow file, bounded by the repo root."""

    def test_finds_github_and_claude_skills(self, tmp_path: Path, home: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        _make_skill(repo / ".github" / "skills" / "alpha")
        _make_skill(repo / ".claude" / "skills" / "beta")
        found = discover_skills(["project"], base_dir=repo, home=home)
        assert [skill.name for skill in found] == ["alpha", "beta"]

    def test_walks_up_to_the_repo_root(self, tmp_path: Path, home: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        _make_skill(repo / ".github" / "skills" / "alpha")
        workflows = repo / "workflows" / "nested"
        workflows.mkdir(parents=True)
        # The overwhelmingly common layout: workflow in a subdirectory,
        # skills at the repo root.
        found = discover_skills(["project"], base_dir=workflows, home=home)
        assert [skill.name for skill in found] == ["alpha"]

    def test_does_not_walk_past_the_repo_root(self, tmp_path: Path, home: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        # Sits *above* the repository. A walk that did not stop would
        # sweep in skills belonging to an unrelated checkout.
        _make_skill(tmp_path / ".github" / "skills" / "outsider")
        assert discover_skills(["project"], base_dir=repo, home=home) == []

    def test_no_repo_marker_considers_only_the_workflow_directory(
        self, tmp_path: Path, home: Path
    ) -> None:
        loose = tmp_path / "loose" / "deep"
        loose.mkdir(parents=True)
        _make_skill(loose / ".github" / "skills" / "near")
        _make_skill(tmp_path / ".github" / "skills" / "far")
        found = discover_skills(["project"], base_dir=loose, home=home)
        assert [skill.name for skill in found] == ["near"]

    def test_nearer_directory_shadows_the_repo_root(self, tmp_path: Path, home: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        nested = repo / "workflows"
        nested.mkdir()
        _make_skill(repo / ".github" / "skills" / "shared", name="shared")
        _make_skill(nested / ".github" / "skills" / "shared", name="shared")
        found = discover_skills(["project"], base_dir=nested, home=home)
        assert [skill.directory for skill in found] == [nested / ".github" / "skills" / "shared"]

    def test_skipped_without_a_base_dir(self, tmp_path: Path, home: Path) -> None:
        # The process working directory is not an anchor — it is wherever
        # the user happened to invoke the CLI from.
        _make_skill(tmp_path / ".github" / "skills" / "alpha")
        assert discover_skills(["project"], base_dir=None, home=home) == []


class TestPluginsSource:
    """``plugins`` needs the two-level marketplace/plugin glob."""

    def test_finds_skills_two_levels_deep(self, home: Path) -> None:
        _make_skill(home / ".copilot" / "installed-plugins" / "market" / "tools" / "skills" / "a")
        _make_skill(home / ".claude" / "plugins" / "market" / "tools" / "skills" / "b")
        found = discover_skills(["plugins"], home=home)
        assert [skill.name for skill in found] == ["a", "b"]

    def test_ignores_a_plugin_root_without_skills(self, home: Path) -> None:
        plugin = home / ".copilot" / "installed-plugins" / "market" / "agents-only"
        (plugin / "agents").mkdir(parents=True)
        _make_skill(home / ".copilot" / "installed-plugins" / "market" / "tools" / "skills" / "a")
        assert [skill.name for skill in discover_skills(["plugins"], home=home)] == ["a"]

    def test_one_level_deep_is_not_matched(self, home: Path) -> None:
        # Globbing a single level finds nothing — the layout really is
        # <marketplace>/<plugin>/skills/<skill>.
        _make_skill(home / ".copilot" / "installed-plugins" / "tools" / "skills" / "a")
        assert discover_skills(["plugins"], home=home) == []


class TestOrdering:
    """Canonical order, so the result is a property of the filesystem."""

    def test_source_order_is_independent_of_yaml_order(self, tmp_path: Path, home: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        _make_skill(repo / ".github" / "skills" / "from-project")
        _make_skill(home / ".copilot" / "skills" / "from-personal")
        _make_skill(home / ".copilot" / "installed-plugins" / "m" / "p" / "skills" / "from-plugin")
        expected = ["from-project", "from-personal", "from-plugin"]
        for written in (
            ["personal", "project", "plugins"],
            ["plugins", "personal", "project"],
            ["project", "plugins", "personal"],
        ):
            found = discover_skills(written, base_dir=repo, home=home)
            assert [skill.name for skill in found] == expected

    def test_skills_within_a_root_are_sorted(self, home: Path) -> None:
        for name in ("zulu", "alpha", "mike"):
            _make_skill(home / ".copilot" / "skills" / name)
        found = discover_skills(["personal"], home=home)
        assert [skill.name for skill in found] == ["alpha", "mike", "zulu"]


class TestCollisions:
    """A name maps to one directory; every downstream consumer is name-keyed."""

    def test_first_source_in_canonical_order_wins(self, tmp_path: Path, home: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        _make_skill(repo / ".github" / "skills" / "shared", name="shared")
        _make_skill(home / ".copilot" / "skills" / "shared", name="shared")
        warnings: list[str] = []
        found = discover_skills(
            ["personal", "project"], base_dir=repo, home=home, on_warning=warnings.append
        )
        assert [skill.directory for skill in found] == [repo / ".github" / "skills" / "shared"]
        assert any("two skills named 'shared'" in warning for warning in warnings)

    def test_collision_does_not_raise(self, home: Path) -> None:
        # Unlike two explicit entries, ambient duplicates must not fail a run.
        _make_skill(home / ".copilot" / "skills" / "shared", name="shared")
        _make_skill(home / ".claude" / "skills" / "shared", name="shared")
        assert len(discover_skills(["personal"], home=home)) == 1


class TestExclude:
    """``exclude`` is the lever for an otherwise all-or-nothing category."""

    def test_named_skill_is_dropped(self, home: Path) -> None:
        _make_skill(home / ".copilot" / "skills" / "keep")
        _make_skill(home / ".copilot" / "skills" / "drop")
        found = discover_skills(["personal"], home=home, exclude=["drop"])
        assert [skill.name for skill in found] == ["keep"]

    def test_excluding_everything_is_not_an_error(self, home: Path) -> None:
        _make_skill(home / ".copilot" / "skills" / "drop")
        assert discover_skills(["personal"], home=home, exclude=["drop"]) == []


class TestWarnings:
    """A config that quietly did nothing is worth saying out loud."""

    def test_source_that_finds_nothing_warns(self, home: Path) -> None:
        warnings: list[str] = []
        discover_skills(["personal"], home=home, on_warning=warnings.append)
        assert any("'personal' found no skills" in warning for warning in warnings)
        # The searched locations are named so the user can see where to look.
        assert any(".copilot/skills" in warning for warning in warnings)

    def test_source_that_finds_something_does_not_warn(self, home: Path) -> None:
        _make_skill(home / ".copilot" / "skills" / "alpha")
        warnings: list[str] = []
        discover_skills(["personal"], home=home, on_warning=warnings.append)
        assert warnings == []

    def test_subdirectory_without_a_manifest_warns(self, home: Path) -> None:
        _make_skill(home / ".copilot" / "skills" / "alpha")
        (home / ".copilot" / "skills" / "not-a-skill").mkdir()
        warnings: list[str] = []
        found = discover_skills(["personal"], home=home, on_warning=warnings.append)
        assert [skill.name for skill in found] == ["alpha"]
        assert any("not-a-skill" in warning for warning in warnings)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
    def test_unreadable_root_warns_instead_of_raising(self, home: Path) -> None:
        root = home / ".copilot" / "skills"
        _make_skill(root / "alpha")
        root.chmod(0o000)
        try:
            warnings: list[str] = []
            # An unreadable ambient location is not worth failing a run
            # over — unlike a path the user wrote in ``skills:``.
            found = discover_skills(["personal"], home=home, on_warning=warnings.append)
            assert found == []
            assert any("could not read" in warning for warning in warnings)
        finally:
            root.chmod(0o755)


class TestResolveEffectiveSkills:
    """Explicit entries are authoritative; discovered ones are best-effort."""

    def test_discovery_appends_to_explicit_entries(self, tmp_path: Path, home: Path) -> None:
        declared = _make_skill(tmp_path / "team" / "declared")
        _make_skill(home / ".copilot" / "skills" / "found")
        resolved = resolve_effective_skills(
            [str(declared)], sources=["personal"], home=home, base_dir=tmp_path
        )
        assert [skill.name for skill in resolved] == ["declared", "found"]
        assert [skill.discovered for skill in resolved] == [False, True]

    def test_no_sources_is_plain_resolution(self, tmp_path: Path, home: Path) -> None:
        declared = _make_skill(tmp_path / "team" / "declared")
        _make_skill(home / ".copilot" / "skills" / "found")
        resolved = resolve_effective_skills([str(declared)], home=home, base_dir=tmp_path)
        assert [skill.name for skill in resolved] == ["declared"]

    def test_explicit_entry_wins_a_name_collision(self, tmp_path: Path, home: Path) -> None:
        # The exact case a user hits immediately: a skill both declared and
        # installed. Hard-failing here would make discovery unusable.
        declared = _make_skill(tmp_path / "team" / "shared", name="shared")
        _make_skill(home / ".copilot" / "skills" / "shared", name="shared")
        warnings: list[str] = []
        resolved = resolve_effective_skills(
            [str(declared)],
            sources=["personal"],
            home=home,
            base_dir=tmp_path,
            on_warning=warnings.append,
        )
        assert [skill.directory for skill in resolved] == [declared]
        assert any("Keeping the declared one" in warning for warning in warnings)

    def test_broken_discovered_manifest_warns_and_skips(self, home: Path) -> None:
        _make_skill(home / ".copilot" / "skills" / "good")
        broken = home / ".copilot" / "skills" / "broken"
        broken.mkdir(parents=True)
        # A colon inside an unquoted plain scalar — the ordinary trap.
        (broken / "SKILL.md").write_text(
            "---\nname: broken\ndescription: Does things. Triggers: a, b\n---\n"
        )
        warnings: list[str] = []
        resolved = resolve_effective_skills(
            [], sources=["personal"], home=home, on_warning=warnings.append
        )
        assert [skill.name for skill in resolved] == ["good"]
        assert any("skipped 'broken'" in warning for warning in warnings)

    def test_broken_explicit_manifest_still_raises(self, tmp_path: Path, home: Path) -> None:
        broken = tmp_path / "team" / "broken"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_text("---\nname: broken\n---\n")
        with pytest.raises(Exception, match="description"):
            resolve_effective_skills([str(broken)], sources=["personal"], home=home)

    def test_unknown_explicit_name_still_raises(self, home: Path) -> None:
        with pytest.raises(SkillNotFoundError, match="Unknown skill"):
            resolve_effective_skills(["no-such-skill"], sources=["personal"], home=home)

    def test_exclude_does_not_apply_to_explicit_entries(self, tmp_path: Path, home: Path) -> None:
        # Removing a declared skill is a matter of deleting its line.
        declared = _make_skill(tmp_path / "team" / "declared")
        resolved = resolve_effective_skills(
            [str(declared)],
            sources=["personal"],
            exclude=["declared"],
            home=home,
            base_dir=tmp_path,
        )
        assert [skill.name for skill in resolved] == ["declared"]
