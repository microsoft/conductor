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
    SkillError,
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


class TestPluginsSourceIsGone:
    """Discovery must not reach into installed plugins (issue #378).

    Scanning a plugin's ``skills/`` took one of the three things a plugin
    ships and silently dropped its subagents and MCP servers. Plugins are
    named in ``runtime.plugins`` instead, which brings the whole unit.
    """

    def test_plugins_is_not_a_valid_source(self) -> None:
        with pytest.raises(SkillError, match="Unknown skill discovery source"):
            discover_skills(["plugins"], home=Path("/nonexistent"))

    def test_installed_plugin_skills_are_not_discovered(self, home: Path) -> None:
        # The exact layout the removed source used to match.
        _make_skill(home / ".copilot" / "installed-plugins" / "market" / "tools" / "skills" / "a")
        _make_skill(home / ".claude" / "plugins" / "market" / "tools" / "skills" / "b")
        _make_skill(home / ".copilot" / "skills" / "personal-one")
        found = discover_skills(["personal"], home=home)
        assert [skill.name for skill in found] == ["personal-one"]


class TestOrdering:
    """Canonical order, so the result is a property of the filesystem."""

    def test_source_order_is_independent_of_yaml_order(self, tmp_path: Path, home: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        _make_skill(repo / ".github" / "skills" / "from-project")
        _make_skill(home / ".copilot" / "skills" / "from-personal")
        expected = ["from-project", "from-personal"]
        for written in (
            ["personal", "project"],
            ["project", "personal"],
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
        # Names the source category: that is the granularity of the lever
        # the user has for narrowing what gets picked up.
        assert any("'personal'" in warning for warning in warnings)

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


class TestResilience:
    """A stray directory must not take the whole scan down with it."""

    def test_git_as_a_file_stops_the_walk(self, tmp_path: Path, home: Path) -> None:
        """Worktrees and submodules write ``.git`` as a file, not a directory."""
        repo = tmp_path / "worktree"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
        _make_skill(repo / ".github" / "skills" / "inside")
        _make_skill(tmp_path / ".github" / "skills" / "outside")
        found = discover_skills(["project"], base_dir=repo, home=home)
        assert [skill.name for skill in found] == ["inside"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
    def test_one_unreadable_child_does_not_hide_its_siblings(self, home: Path) -> None:
        root = home / ".copilot" / "skills"
        _make_skill(root / "alpha")
        _make_skill(root / "beta")
        locked = root / "locked"
        locked.mkdir()
        locked.chmod(0o000)
        try:
            warnings: list[str] = []
            found = discover_skills(["personal"], home=home, on_warning=warnings.append)
            # The readable siblings survive; only the stray directory is lost.
            assert [skill.name for skill in found] == ["alpha", "beta"]
            assert any("locked" in warning for warning in warnings)
        finally:
            locked.chmod(0o755)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
    def test_unreadable_root_does_not_also_claim_the_source_is_empty(self, home: Path) -> None:
        """ "Found nothing" would contradict the read failure and mis-advise."""
        root = home / ".copilot" / "skills"
        _make_skill(root / "alpha")
        root.chmod(0o000)
        try:
            warnings: list[str] = []
            discover_skills(["personal"], home=home, on_warning=warnings.append)
            assert any("could not read" in warning for warning in warnings)
            assert not any("found no skills" in warning for warning in warnings)
        finally:
            root.chmod(0o755)

    def test_project_without_a_base_dir_says_why(self, home: Path) -> None:
        # Not "found no skills … install skills there" — there is no
        # "there", and installing skills would not help.
        warnings: list[str] = []
        discover_skills(["project"], base_dir=None, home=home, on_warning=warnings.append)
        assert any("no workflow file path was supplied" in warning for warning in warnings)

    def test_relative_home_does_not_raise_from_the_lenient_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ResolvedSkill`` demands an absolute directory.

        Constructing one from a relative ``home`` would raise
        ``SkillNotFoundError`` straight out of the warn-and-skip path that
        promises never to raise.
        """
        _make_skill(tmp_path / "home" / ".copilot" / "skills" / "mine")
        monkeypatch.chdir(tmp_path)
        resolved = resolve_effective_skills([], sources=["personal"], home=Path("home"))
        assert [skill.name for skill in resolved] == ["mine"]
        assert resolved[0].directory.is_absolute()

    def test_relative_base_dir_does_not_raise_from_the_lenient_path(
        self, tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_skill(tmp_path / "wf" / ".github" / "skills" / "mine")
        monkeypatch.chdir(tmp_path)
        resolved = resolve_effective_skills([], sources=["project"], base_dir=Path("wf"), home=home)
        assert [skill.name for skill in resolved] == ["mine"]
        assert resolved[0].directory.is_absolute()

    def test_unknown_source_is_rejected(self, home: Path) -> None:
        # Reachable without the schema: this is a public, exported function.
        with pytest.raises(SkillError, match="Unknown skill discovery source"):
            discover_skills(["everywhere"], home=home)  # ty: ignore[invalid-argument-type]
