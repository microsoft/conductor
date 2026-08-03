"""Tests for ``SKILL.md`` frontmatter parsing (issue #350).

Both the Copilot CLI and Claude Code **silently skip** a skill whose
frontmatter fails to parse. Conductor parses it itself so the failure is
loud, which is the entire point of this module — every test here stands
in for a skill that would otherwise have gone missing without a word.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.skills import (
    SkillFrontmatter,
    SkillManifestError,
    get_skill_directory,
    read_skill_frontmatter,
)


def _write_skill(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(body, encoding="utf-8")
    return directory


class TestValidFrontmatter:
    def test_bundled_conductor_skill_parses(self) -> None:
        """The skill Conductor ships must satisfy its own parser."""
        parsed = read_skill_frontmatter(get_skill_directory("conductor"))
        assert parsed.name == "conductor"
        assert parsed.description

    def test_returns_name_and_description(self, tmp_path: Path) -> None:
        skill = _write_skill(
            tmp_path / "s", "---\nname: acme\ndescription: Does acme things.\n---\nBody\n"
        )
        assert read_skill_frontmatter(skill) == SkillFrontmatter(
            name="acme", description="Does acme things."
        )

    def test_block_scalar_description_parses(self, tmp_path: Path) -> None:
        """The documented workaround for the ``Triggers:`` trap must work."""
        skill = _write_skill(
            tmp_path / "s",
            "---\nname: acme\ndescription: |\n  Does things. Triggers: widget, acme.\n---\n",
        )
        assert "Triggers: widget, acme." in read_skill_frontmatter(skill).description

    def test_extra_keys_are_ignored(self, tmp_path: Path) -> None:
        skill = _write_skill(
            tmp_path / "s",
            "---\nname: acme\ndescription: D\nlicense: MIT\nallowed-tools: [Bash]\n---\n",
        )
        assert read_skill_frontmatter(skill).name == "acme"

    def test_crlf_line_endings_parse(self, tmp_path: Path) -> None:
        skill = _write_skill(
            tmp_path / "s", "---\r\nname: acme\r\ndescription: D\r\n---\r\nBody\r\n"
        )
        assert read_skill_frontmatter(skill).name == "acme"

    def test_surrounding_whitespace_is_stripped(self, tmp_path: Path) -> None:
        skill = _write_skill(tmp_path / "s", "---\nname: '  acme  '\ndescription: '  D  '\n---\n")
        assert read_skill_frontmatter(skill) == SkillFrontmatter(name="acme", description="D")

    def test_thematic_break_in_body_is_not_frontmatter(self, tmp_path: Path) -> None:
        """A ``---`` further down the file is Markdown, not a second manifest."""
        skill = _write_skill(
            tmp_path / "s", "---\nname: acme\ndescription: D\n---\n\nIntro\n\n---\n\nMore\n"
        )
        assert read_skill_frontmatter(skill).description == "D"


class TestMalformedFrontmatter:
    def test_unquoted_colon_is_reported_with_the_fix(self, tmp_path: Path) -> None:
        """The exact trap from issue #350.

        ``Triggers:`` inside an unquoted plain scalar is invalid YAML. This
        is the case that cost the issue author several debugging rounds
        because both CLIs skipped the skill without saying anything.
        """
        skill = _write_skill(
            tmp_path / "s",
            "---\nname: acme-widgets\n"
            "description: Internal ACME conventions. Triggers: widget, acme widget.\n---\n",
        )
        with pytest.raises(SkillManifestError) as exc_info:
            read_skill_frontmatter(skill)
        message = str(exc_info.value)
        assert "invalid YAML frontmatter" in message
        assert "description: |" in message, "the error must show the block-scalar fix"

    def test_missing_skill_md(self, tmp_path: Path) -> None:
        (tmp_path / "s").mkdir()
        with pytest.raises(SkillManifestError, match="has no SKILL.md"):
            read_skill_frontmatter(tmp_path / "s")

    def test_no_frontmatter_block(self, tmp_path: Path) -> None:
        skill = _write_skill(tmp_path / "s", "# Just a heading\n\nNo frontmatter here.\n")
        with pytest.raises(SkillManifestError, match="no YAML frontmatter"):
            read_skill_frontmatter(skill)

    def test_unterminated_frontmatter_block(self, tmp_path: Path) -> None:
        skill = _write_skill(tmp_path / "s", "---\nname: acme\ndescription: D\n\nBody\n")
        with pytest.raises(SkillManifestError, match="no YAML frontmatter"):
            read_skill_frontmatter(skill)

    @pytest.mark.parametrize(
        ("body", "missing"),
        [
            ("---\ndescription: D\n---\n", "name"),
            ("---\nname: acme\n---\n", "description"),
            ("---\nname: ''\ndescription: D\n---\n", "name"),
            ("---\nname: acme\ndescription: '   '\n---\n", "description"),
            ("---\nname: 42\ndescription: D\n---\n", "name"),
            ("---\nname: acme\ndescription: [a, b]\n---\n", "description"),
        ],
    )
    def test_missing_or_unusable_fields(self, tmp_path: Path, body: str, missing: str) -> None:
        skill = _write_skill(tmp_path / "s", body)
        with pytest.raises(SkillManifestError, match=f"no usable '{missing}'"):
            read_skill_frontmatter(skill)

    @pytest.mark.parametrize(
        "body",
        [
            "---\n- a\n- b\n---\n",
            "---\njust a string\n---\n",
        ],
        ids=["sequence", "scalar"],
    )
    def test_frontmatter_that_is_not_a_mapping(self, tmp_path: Path, body: str) -> None:
        skill = _write_skill(tmp_path / "s", body)
        with pytest.raises(SkillManifestError, match="not a YAML mapping"):
            read_skill_frontmatter(skill)

    def test_unreadable_skill_md_is_reported(self, tmp_path: Path) -> None:
        """Invalid UTF-8 must not escape as a bare UnicodeDecodeError."""
        skill = tmp_path / "s"
        skill.mkdir()
        (skill / "SKILL.md").write_bytes(b"---\nname: \xff\xfe\ndescription: D\n---\n")
        with pytest.raises(SkillManifestError, match="could not be read"):
            read_skill_frontmatter(skill)
