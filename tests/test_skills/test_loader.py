"""Tests for the skill content loader (eager preamble injection path)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conductor.skills import SkillManifestError, get_skill_directory, load_skill_content
from conductor.skills.loader import _cached_skill_payload


class TestLoadSkillContent:
    def setup_method(self) -> None:
        _cached_skill_payload.cache_clear()

    def test_empty_skills_returns_empty(self) -> None:
        assert load_skill_content([]) == ""

    def test_wraps_in_skills_tag(self) -> None:
        d = get_skill_directory("conductor")
        result = load_skill_content([("conductor", d)])
        assert result.startswith("<skills>\n")
        assert "</skills>\n\n" in result

    def test_wraps_each_skill_in_named_tag(self) -> None:
        d = get_skill_directory("conductor")
        result = load_skill_content([("conductor", d)])
        assert '<skill name="conductor">' in result
        assert "</skill>" in result

    def test_includes_skill_md_content(self) -> None:
        d = get_skill_directory("conductor")
        result = load_skill_content([("conductor", d)])
        assert "# SKILL.md" in result

    def test_includes_references(self) -> None:
        d = get_skill_directory("conductor")
        result = load_skill_content([("conductor", d)])
        # yaml-schema.md is a known reference in the conductor skill.
        assert "# references/yaml-schema.md" in result

    def test_substantial_content(self) -> None:
        d = get_skill_directory("conductor")
        result = load_skill_content([("conductor", d)])
        size_kb = len(result.encode("utf-8")) / 1024
        assert size_kb > 50, f"Expected >50KB, got {size_kb:.1f}KB"

    def test_caches_per_dir(self) -> None:
        d = get_skill_directory("conductor")
        first = _cached_skill_payload(str(d), "conductor")
        second = _cached_skill_payload(str(d), "conductor")
        assert first is second

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        # No SKILL.md, no references/.
        assert load_skill_content([("empty", tmp_path)]) == ""


class TestUnreadableContentFailsLoudly:
    """A skill file that cannot be read must raise, not be skipped.

    This package exists because the upstream CLIs drop an unloadable skill in
    silence. Doing the same for a `references/*.md` file would be the same
    defect one directory deeper — and by volume it is the worse half: for the
    bundled `conductor` skill the references are ~93% of the payload, so a
    single unreadable file could cut the agent's knowledge to a fraction while
    the run reported success.
    """

    @staticmethod
    def _make_skill(directory: Path) -> Path:
        (directory / "references").mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {directory.name}\ndescription: A test skill.\n---\nBody\n"
        )
        (directory / "references" / "a.md").write_text("Reference A")
        return directory

    def test_undecodable_reference_raises(self, tmp_path: Path) -> None:
        skill = self._make_skill(tmp_path / "acme")
        (skill / "references" / "bad.md").write_bytes(b"\xff\xfe not utf-8")
        with pytest.raises(SkillManifestError, match="reference.*could not be read"):
            load_skill_content([("acme", skill)])

    def test_undecodable_manifest_raises(self, tmp_path: Path) -> None:
        skill = self._make_skill(tmp_path / "acme")
        (skill / "SKILL.md").write_bytes(b"\xff\xfe not utf-8")
        with pytest.raises(SkillManifestError, match="manifest.*could not be read"):
            load_skill_content([("acme", skill)])

    @pytest.mark.skipif(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="POSIX permission semantics; chmod(0o000) blocks neither Windows owners nor root",
    )
    def test_unreadable_reference_raises(self, tmp_path: Path) -> None:
        skill = self._make_skill(tmp_path / "acme")
        blocked = skill / "references" / "blocked.md"
        blocked.write_text("secret")
        blocked.chmod(0o000)
        try:
            with pytest.raises(SkillManifestError, match="could not be read"):
                load_skill_content([("acme", skill)])
        finally:
            blocked.chmod(0o644)

    def test_failure_is_not_cached(self, tmp_path: Path) -> None:
        """``lru_cache`` never memoizes a raising call, so a transient error is
        retried rather than frozen in as an empty payload for the whole run."""
        skill = self._make_skill(tmp_path / "acme")
        bad = skill / "references" / "bad.md"
        bad.write_bytes(b"\xff\xfe")
        with pytest.raises(SkillManifestError):
            load_skill_content([("acme", skill)])

        bad.write_text("Now readable")
        content = load_skill_content([("acme", skill)])
        assert "Now readable" in content
        assert "Reference A" in content

    def test_a_readable_skill_is_unaffected(self, tmp_path: Path) -> None:
        skill = self._make_skill(tmp_path / "acme")
        content = load_skill_content([("acme", skill)])
        assert "Reference A" in content and "Body" in content
