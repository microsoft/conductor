"""Executable tests for the changelog fragment gate (PR #535 review).

``.github/scripts/enforce-changelog-contract.sh`` decides whether
contributions and release-prep PRs may proceed, so it is exercised for real:
each test builds a temporary git repository (base branch + PR branch), runs
the actual gate script via subprocess, and asserts on its exit code and
diagnostics. Mocking git would test the mock — the failure modes here are
shell-quoting, git-diff, and towncrier-behavior mistakes that only the real
composition surfaces.

The script's release mode loads ``extract-release-notes.sh`` from the *base*
branch, so every fixture repository carries a copy of the real extractor in
its base commit — the trusted-script loading path is exercised too.

Tests that reach ``towncrier check`` or the PEP 440 lockfile comparison need
uv to fetch towncrier/packaging (cached after the first run); they skip
gracefully when uv cannot provide them (e.g. fully offline machines).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_SCRIPT = REPO_ROOT / ".github" / "scripts" / "enforce-changelog-contract.sh"
EXTRACTOR_SCRIPT = REPO_ROOT / ".github" / "scripts" / "extract-release-notes.sh"

# Gate on the platform, not just on `which`: GitHub's windows-latest images
# put Git-Bash's bash.exe on PATH, but this harness (POSIX paths, env layout)
# does not work there. Precedent: test_install_script_extras.py.
_BINARIES_OK = (
    sys.platform != "win32"
    and shutil.which("bash") is not None
    and shutil.which("git") is not None
    and shutil.which("uv") is not None
    and shutil.which("uvx") is not None
)
pytestmark = pytest.mark.skipif(not _BINARIES_OK, reason="requires bash, git and uv on POSIX")

BASE_VERSION = "0.1.37"
EXEMPTION_LABEL = "changelog-not-required"

CHANGELOG_TEMPLATE = """\
# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

Pending changes are collected as fragment files in [`changelog.d/`](changelog.d/)
and compiled into this file at release time.

<!-- towncrier release notes start -->

## [0.1.36] - 2026-09-01

### Added

- Something historical.
"""


def _towncrier_config() -> str:
    """The repo's real [tool.towncrier] block, so fixtures validate with the
    same categories and naming rules the gate protects in production.

    Sliced from its header to the next top-level TOML section (not to EOF),
    so a later [tool.*] section appended after towncrier's does not silently
    leak into the fixture.
    """
    lines = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.rstrip() == "[tool.towncrier]")
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].startswith("[")
            and not lines[i].startswith(("[tool.towncrier]", "[[tool.towncrier."))
        ),
        len(lines),
    )
    return "".join(lines[start:end]).rstrip() + "\n"


def _pyproject_text(version: str) -> str:
    return f'[project]\nname = "conductor-cli"\nversion = "{version}"\n\n' + _towncrier_config()


def _uv_lock_text(version: str) -> str:
    return (
        "version = 1\n\n"
        "[[package]]\n"
        'name = "conductor-cli"\n'
        f'version = "{version}"\n'
        'source = { editable = "." }\n'
    )


def _release_section(version: str, date: str = "2026-09-17") -> str:
    return f"## [{version}] - {date}\n\n### Added\n\n- Something new (#100).\n\n"


def _changelog_with_release(version: str) -> str:
    marker = "<!-- towncrier release notes start -->\n"
    head, tail = CHANGELOG_TEMPLATE.split(marker, maxsplit=1)
    return head + marker + "\n" + _release_section(version) + tail.lstrip("\n")


class GateRepo:
    """A temporary git repository in the shape the gate expects:

    base branch ``main`` (pyproject.toml with the real towncrier config,
    CHANGELOG.md with the insertion marker, uv.lock pinning the base version,
    the base-branch extractor script, and one pre-existing fragment), with the
    PR's changes committed on branch ``pr``.
    """

    def __init__(self, root: Path, *, include_extractor: bool = True) -> None:
        self.root = root
        root.mkdir(parents=True)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "gate@test.invalid")
        self._git("config", "user.name", "Gate Test")
        self._git("config", "commit.gpgsign", "false")
        self.write("pyproject.toml", _pyproject_text(BASE_VERSION))
        self.write("CHANGELOG.md", CHANGELOG_TEMPLATE)
        self.write("uv.lock", _uv_lock_text(BASE_VERSION))
        self.write("changelog.d/README.md", "# Fragment contract\n")
        self.write("changelog.d/100.added.md", "An earlier pending change (#100).\n")
        if include_extractor:
            self.write(
                ".github/scripts/extract-release-notes.sh",
                EXTRACTOR_SCRIPT.read_text(encoding="utf-8"),
            )
        self._git("add", "-A")
        self._git("commit", "-qm", "base")
        self._git("checkout", "-qb", "pr")

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def delete(self, rel: str) -> None:
        (self.root / rel).unlink()

    def rename(self, src: str, dst: str) -> None:
        self._git("mv", src, dst)

    def commit_all(self, message: str = "change") -> None:
        self._git("add", "-A")
        self._git("commit", "-qm", message)

    def prepare_release(self, version: str, *, lock_version: str | None = None) -> None:
        """Put the branch into a valid release-prep shape for ``version``."""
        self.write("pyproject.toml", _pyproject_text(version))
        self.write("CHANGELOG.md", _changelog_with_release(version))
        self.write("uv.lock", _uv_lock_text(lock_version or version))
        self.delete("changelog.d/100.added.md")
        self.commit_all("release prep")


@pytest.fixture()
def repo(tmp_path: Path) -> GateRepo:
    return GateRepo(tmp_path / "repo")


def run_gate(
    repo: GateRepo,
    labels: tuple[str, ...] = (),
    *,
    author_login: str = "",
) -> subprocess.CompletedProcess[str]:
    scratch = repo.root / ".gate-tmp"
    scratch.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["PR_LABELS"] = json.dumps(list(labels))
    env["PR_AUTHOR_LOGIN"] = author_login
    env["CHANGELOG_BASE"] = "main"
    env["RUNNER_TEMP"] = str(scratch)
    env.pop("BASE_REF", None)
    return subprocess.run(
        ["bash", str(GATE_SCRIPT)],
        cwd=repo.root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def assert_passed(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode == 0, f"gate unexpectedly failed:\n{proc.stdout}\n{proc.stderr}"


def assert_failed(proc: subprocess.CompletedProcess[str], *needles: str) -> None:
    assert proc.returncode != 0, f"gate unexpectedly passed:\n{proc.stdout}\n{proc.stderr}"
    output = proc.stdout + proc.stderr
    for needle in needles:
        assert needle in output, f"missing {needle!r} in gate output:\n{output}"


@pytest.fixture(scope="session")
def uv_tools() -> None:
    """Skip tests needing uv-managed tools when uv cannot provide them.

    The first probe downloads towncrier/packaging into the uv cache (needs
    network once); subsequent runs are cached.
    """
    probes = [
        ["uvx", "--from", "towncrier==25.8.0", "towncrier", "--version"],
        ["uv", "run", "--no-project", "--with", "packaging", "python", "-c", "import packaging"],
    ]
    try:
        for probe in probes:
            subprocess.run(probe, capture_output=True, timeout=300, check=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"uv-managed towncrier/packaging unavailable: {exc}")


class TestFeatureMode:
    def test_valid_fragment_passes(self, repo: GateRepo, uv_tools: None) -> None:
        # Requirement: a feature PR adding a well-named fragment passes the gate.
        repo.write("changelog.d/123.fixed.md", "A fix (#123).\n")
        repo.write("src.py", "x = 1\n")
        repo.commit_all()
        assert_passed(run_gate(repo))

    def test_sequence_suffix_fragment_passes(self, repo: GateRepo, uv_tools: None) -> None:
        # Requirement: the optional .<seq> suffix (`450.fixed.1.md`) is accepted.
        repo.write("changelog.d/450.fixed.1.md", "First of two (#450).\n")
        repo.commit_all()
        assert_passed(run_gate(repo))

    def test_missing_fragment_fails(self, repo: GateRepo) -> None:
        # Requirement: a feature PR with no new fragment is rejected with an
        # actionable message.
        repo.write("src.py", "x = 1\n")
        repo.commit_all()
        assert_failed(run_gate(repo), "No changelog fragment found")

    def test_changelog_edit_fails(self, repo: GateRepo) -> None:
        # Requirement: CHANGELOG.md may not be edited in a feature PR, even
        # when a fragment is also added.
        repo.write("CHANGELOG.md", CHANGELOG_TEMPLATE + "\nHand-edited entry.\n")
        repo.write("changelog.d/123.fixed.md", "A fix (#123).\n")
        repo.commit_all()
        assert_failed(run_gate(repo), "CHANGELOG.md must not be edited")

    def test_invalid_fragment_name_fails(self, repo: GateRepo) -> None:
        # Requirement: non-numeric slugs need the mandatory '+' prefix.
        repo.write("changelog.d/no-prefix.added.md", "Bad name.\n")
        repo.commit_all()
        assert_failed(run_gate(repo), "Invalid fragment name 'no-prefix.added.md'")

    def test_exemption_waives_fragment_and_edit(self, repo: GateRepo) -> None:
        # Requirement: the changelog-not-required label waives both the
        # fragment requirement and the CHANGELOG.md edit prohibition.
        repo.write("CHANGELOG.md", CHANGELOG_TEMPLATE + "\nBootstrap edit.\n")
        repo.commit_all()
        assert_passed(run_gate(repo, labels=(EXEMPTION_LABEL,)))

    def test_exemption_still_rejects_bad_fragment_name(self, repo: GateRepo) -> None:
        # Requirement: fragments present under the exemption label are still
        # validated (a bad name on main breaks towncrier for every later PR).
        repo.write("changelog.d/no-prefix.added.md", "Bad name.\n")
        repo.commit_all()
        proc = run_gate(repo, labels=(EXEMPTION_LABEL,))
        assert_failed(proc, "Invalid fragment name 'no-prefix.added.md'")

    def test_exemption_label_matches_case_insensitively(self, repo: GateRepo) -> None:
        # Requirement: GitHub label names are unique case-insensitively, so a
        # label stored with different case still waives.
        repo.write("CHANGELOG.md", CHANGELOG_TEMPLATE + "\nBootstrap edit.\n")
        repo.commit_all()
        assert_passed(run_gate(repo, labels=("Changelog-Not-Required",)))

    def test_dependabot_author_waives_fragment_requirement(self, repo: GateRepo) -> None:
        # Requirement: Dependabot-authored PRs do not need an automatically
        # generated fragment.
        repo.write("uv.lock", _uv_lock_text("2.14.0"))
        repo.commit_all()
        proc = run_gate(repo, author_login="dependabot[bot]")
        assert_passed(proc)
        assert "Dependabot-authored PR" in proc.stdout

    def test_dependabot_author_does_not_waive_changelog_edit(self, repo: GateRepo) -> None:
        # Requirement: the bot exception is no broader than necessary; only a
        # maintainer-applied label may permit a direct CHANGELOG.md edit.
        repo.write("CHANGELOG.md", CHANGELOG_TEMPLATE + "\nUnexpected edit.\n")
        repo.commit_all()
        proc = run_gate(repo, author_login="dependabot[bot]")
        assert_failed(proc, "CHANGELOG.md must not be edited")

    def test_dependabot_author_still_rejects_bad_fragment_name(self, repo: GateRepo) -> None:
        # Requirement: the author exemption does not let a broken fragment
        # reach main and poison towncrier checks for later pull requests.
        repo.write("changelog.d/no-prefix.added.md", "Bad name.\n")
        repo.commit_all()
        proc = run_gate(repo, author_login="dependabot[bot]")
        assert_failed(proc, "Invalid fragment name 'no-prefix.added.md'")

    def test_nested_fragment_path_fails(self, repo: GateRepo) -> None:
        # Requirement: fragments must live directly in changelog.d/ — towncrier
        # reads a flat directory and a nested entry breaks 'towncrier check'
        # for every later PR, even though a rename-only change never triggers
        # towncrier itself.
        (repo.root / "changelog.d" / "sub").mkdir()
        repo.rename("changelog.d/100.added.md", "changelog.d/sub/100.fixed.md")
        repo.commit_all()
        proc = run_gate(repo, labels=(EXEMPTION_LABEL,))
        assert_failed(proc, "not directly under changelog.d/")

    def test_duplicate_issue_category_pair_fails_via_towncrier(
        self, repo: GateRepo, uv_tools: None
    ) -> None:
        # Requirement: towncrier check is load-bearing, not decorative —
        # `100.added.0.md` collides with the existing `100.added.md` (both are
        # counter 0 for issue 100/added), which the name scan accepts but
        # towncrier rejects with "multiple files". Proves towncrier actually
        # runs on the fragment-adding path.
        repo.write("changelog.d/100.added.0.md", "Duplicate counter (#100).\n")
        repo.commit_all()
        assert_failed(run_gate(repo), "towncrier check rejected")

    def test_exemption_with_new_fragment_and_changelog_edit_passes(
        self, repo: GateRepo, uv_tools: None
    ) -> None:
        # Requirement: the bootstrap shape of the migration PR itself —
        # label, a CHANGELOG.md edit, and new valid fragments — passes.
        # (towncrier check exits early with "Checks SKIPPED" when CHANGELOG.md
        # changed; the name scan is what validates the fragments.)
        repo.write("CHANGELOG.md", CHANGELOG_TEMPLATE + "\nStatic header change.\n")
        repo.write("changelog.d/+slug.changed.md", "A slug-named change.\n")
        repo.commit_all()
        assert_passed(run_gate(repo, labels=(EXEMPTION_LABEL,)))

    def test_fragment_deletion_only_with_label_passes(self, repo: GateRepo) -> None:
        # Requirement: deleting an obsolete fragment under the exemption label
        # passes — towncrier check must not run (it would fail with "No new
        # newsfragments found", reintroducing the waived requirement).
        repo.delete("changelog.d/100.added.md")
        repo.commit_all()
        proc = run_gate(repo, labels=(EXEMPTION_LABEL,))
        assert_passed(proc)
        assert "skipping 'towncrier check'" in proc.stdout

    def test_fragment_deletion_only_without_label_fails(self, repo: GateRepo) -> None:
        # Requirement: a fragment deletion is not a fragment addition — the
        # fragment requirement still applies without the label.
        repo.delete("changelog.d/100.added.md")
        repo.commit_all()
        assert_failed(run_gate(repo), "No changelog fragment found")

    def test_rename_to_invalid_name_fails(self, repo: GateRepo) -> None:
        # Requirement: a rename destination with an invalid name is caught by
        # the surviving-tree scan (a rename is not an addition, so only the
        # scan — not towncrier check — can see it).
        repo.rename("changelog.d/100.added.md", "changelog.d/100.added.markdown")
        repo.commit_all()
        proc = run_gate(repo, labels=(EXEMPTION_LABEL,))
        assert_failed(proc, "Invalid fragment name '100.added.markdown'")

    def test_valid_rename_with_label_passes(self, repo: GateRepo) -> None:
        # Requirement: renaming a fragment to another valid name under the
        # label passes, with towncrier check skipped (no new fragment).
        repo.rename("changelog.d/100.added.md", "changelog.d/100.fixed.md")
        repo.commit_all()
        assert_passed(run_gate(repo, labels=(EXEMPTION_LABEL,)))

    def test_readme_only_change_fails_without_label(self, repo: GateRepo) -> None:
        # Requirement: editing the contract README is not a fragment.
        repo.write("changelog.d/README.md", "# Updated contract\n")
        repo.commit_all()
        assert_failed(run_gate(repo), "No changelog fragment found")

    def test_readme_only_change_with_label_skips_validation(self, repo: GateRepo) -> None:
        # Requirement: a README-only change under the label passes without
        # invoking towncrier at all.
        repo.write("changelog.d/README.md", "# Updated contract\n")
        repo.commit_all()
        proc = run_gate(repo, labels=(EXEMPTION_LABEL,))
        assert_passed(proc)
        assert "No changes under changelog.d/" in proc.stdout


class TestReleaseMode:
    def test_release_prep_passes(self, repo: GateRepo, uv_tools: None) -> None:
        # Requirement: a complete release-prep PR (bumped version, compiled
        # section, consumed fragments, synced lockfile) passes.
        repo.prepare_release("0.1.38")
        assert_passed(run_gate(repo))

    def test_release_without_changelog_change_fails(self, repo: GateRepo) -> None:
        # Requirement: a version bump without a compiled CHANGELOG.md is rejected.
        repo.write("pyproject.toml", _pyproject_text("0.1.38"))
        repo.commit_all()
        assert_failed(run_gate(repo), "CHANGELOG.md is not changed")

    def test_release_with_duplicate_section_fails(self, repo: GateRepo) -> None:
        # Requirement: exactly one section for the bumped version may exist.
        repo.prepare_release("0.1.38")
        repo.write(
            "CHANGELOG.md",
            repo.root.joinpath("CHANGELOG.md").read_text(encoding="utf-8")
            + "\n"
            + _release_section("0.1.38"),
        )
        repo.commit_all()
        assert_failed(run_gate(repo), "compiled more than once")

    def test_release_with_missing_marker_fails(self, repo: GateRepo) -> None:
        # Requirement: the towncrier insertion marker must survive compilation.
        repo.prepare_release("0.1.38")
        changelog = repo.root.joinpath("CHANGELOG.md").read_text(encoding="utf-8")
        repo.write(
            "CHANGELOG.md", changelog.replace("<!-- towncrier release notes start -->\n", "")
        )
        repo.commit_all()
        assert_failed(run_gate(repo), "marker must appear exactly once")

    def test_release_with_leftover_fragment_fails(self, repo: GateRepo) -> None:
        # Requirement: fragments must be consumed by the compilation — a
        # leftover (e.g. merged after the branch was built) is rejected.
        repo.prepare_release("0.1.38")
        repo.write("changelog.d/555.added.md", "Landed after the build (#555).\n")
        repo.commit_all()
        assert_failed(run_gate(repo), "fragments must be consumed")

    def test_release_with_lockfile_mismatch_fails(self, repo: GateRepo, uv_tools: None) -> None:
        # Requirement: uv.lock must pin conductor-cli at the bumped version.
        repo.prepare_release("0.1.38", lock_version="0.1.37")
        assert_failed(run_gate(repo), "uv.lock pins conductor-cli at 0.1.37")

    def test_release_with_normalized_prerelease_passes(
        self, repo: GateRepo, uv_tools: None
    ) -> None:
        # Requirement: uv normalizes prerelease spellings at lock time
        # (0.2.0-beta.1 -> 0.2.0b1), so the comparison is PEP 440-normalized
        # while the authored spelling keeps the section header and tag.
        repo.prepare_release("0.2.0-beta.1", lock_version="0.2.0b1")
        assert_passed(run_gate(repo))

    def test_release_without_extractor_on_base_fails(self, tmp_path: Path) -> None:
        # Requirement: the extraction rehearsal runs the trusted base-branch
        # script — if it is missing there, the PR cannot pass.
        repo = GateRepo(tmp_path / "repo", include_extractor=False)
        repo.prepare_release("0.1.38")
        assert_failed(run_gate(repo), "extract-release-notes.sh is missing on the base branch")
