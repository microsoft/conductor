"""Tests for the skill content loader (eager preamble injection path)."""

from __future__ import annotations

from pathlib import Path

from conductor.skills import get_skill_directory, load_skill_content
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
