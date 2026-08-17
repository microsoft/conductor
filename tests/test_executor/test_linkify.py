"""Tests for the linkify_markdown post-processor."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from conductor.executor.linkify import MAX_LINKIFY_CHARS, linkify_markdown

# ---------------------------------------------------------------------------
# Whitespace normalisation
# ---------------------------------------------------------------------------


class TestWhitespaceNormalization:
    """Tests for Jinja2 whitespace artifact cleanup."""

    def test_collapses_triple_newlines(self) -> None:
        text = "line1\n\n\nline2"
        assert linkify_markdown(text) == "line1\n\nline2"

    def test_collapses_many_newlines(self) -> None:
        text = "a\n\n\n\n\n\nb"
        assert linkify_markdown(text) == "a\n\nb"

    def test_preserves_double_newlines(self) -> None:
        text = "a\n\nb"
        assert linkify_markdown(text) == "a\n\nb"

    def test_preserves_single_newlines(self) -> None:
        text = "a\nb"
        assert linkify_markdown(text) == "a\nb"

    def test_jinja_for_loop_artifact(self) -> None:
        """Simulates the exact Jinja2 for-loop blank-line issue."""
        text = "Items found:\n\n- item1\n\n- item2\n\n- item3\n\n"
        result = linkify_markdown(text)
        assert "\n\n\n" not in result
        assert "- item1" in result
        assert "- item2" in result


# ---------------------------------------------------------------------------
# URL auto-linking
# ---------------------------------------------------------------------------


class TestUrlLinking:
    """Tests for bare URL auto-detection and linking."""

    def test_bare_http_url(self) -> None:
        result = linkify_markdown("Visit https://example.com for info")
        assert "[https://example.com](https://example.com)" in result

    def test_bare_http_url_with_path(self) -> None:
        result = linkify_markdown("See https://example.com/docs/api")
        assert "[https://example.com/docs/api](https://example.com/docs/api)" in result

    def test_strips_trailing_punctuation(self) -> None:
        result = linkify_markdown("Check https://example.com.")
        assert "[https://example.com](https://example.com)." in result

    def test_preserves_existing_markdown_link(self) -> None:
        text = "See [docs](https://example.com/docs) for more"
        assert linkify_markdown(text) == text

    def test_url_in_inline_code_untouched(self) -> None:
        text = "Run `curl https://example.com/api` to test"
        assert linkify_markdown(text) == text

    def test_url_in_fenced_code_untouched(self) -> None:
        text = "```\nhttps://example.com/api\n```"
        assert linkify_markdown(text) == text

    def test_multiple_urls(self) -> None:
        text = "Visit https://a.com and https://b.com"
        result = linkify_markdown(text)
        assert "[https://a.com](https://a.com)" in result
        assert "[https://b.com](https://b.com)" in result


# ---------------------------------------------------------------------------
# File path auto-linking
# ---------------------------------------------------------------------------


class TestFilePathLinking:
    """Tests for bare file path auto-detection and linking."""

    def test_relative_path_with_extension(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "readme.md").write_text("hello")

        result = linkify_markdown("See docs/readme.md for details", base_dir=tmp_path)
        assert "[docs/readme.md](docs/readme.md)" in result

    def test_nested_path(self, tmp_path: Path) -> None:
        (tmp_path / "docs" / "projects").mkdir(parents=True)
        (tmp_path / "docs" / "projects" / "plan.md").write_text("plan")

        result = linkify_markdown("Plan at docs/projects/plan.md", base_dir=tmp_path)
        assert "[docs/projects/plan.md](docs/projects/plan.md)" in result

    def test_nonexistent_file_not_linked(self, tmp_path: Path) -> None:
        result = linkify_markdown("See docs/missing.md for details", base_dir=tmp_path)
        assert "[docs/missing.md]" not in result
        assert "docs/missing.md" in result  # still present as plain text

    def test_no_base_dir_still_links(self) -> None:
        """Without base_dir, file paths are linked without existence check."""
        result = linkify_markdown("See docs/readme.md for details")
        assert "[docs/readme.md](docs/readme.md)" in result

    def test_unknown_extension_not_linked(self, tmp_path: Path) -> None:
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "file.xyz").write_text("data")

        result = linkify_markdown("See data/file.xyz", base_dir=tmp_path)
        assert "[data/file.xyz]" not in result

    def test_path_in_markdown_list(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.md").write_text("a")
        (tmp_path / "docs" / "b.md").write_text("b")

        text = "Plans:\n- docs/a.md\n- docs/b.md"
        result = linkify_markdown(text, base_dir=tmp_path)
        assert "[docs/a.md](docs/a.md)" in result
        assert "[docs/b.md](docs/b.md)" in result

    def test_path_in_inline_code_untouched(self) -> None:
        text = "Edit `src/config/schema.py` to fix"
        assert linkify_markdown(text) == text

    def test_path_in_fenced_code_untouched(self) -> None:
        text = "```\nsrc/config/schema.py\n```"
        assert linkify_markdown(text) == text

    def test_existing_markdown_link_untouched(self) -> None:
        text = "See [config](src/config/schema.py) for details"
        assert linkify_markdown(text) == text

    def test_path_without_separator_not_linked(self) -> None:
        result = linkify_markdown("See readme.md for details")
        assert "[readme.md]" not in result

    def test_url_not_treated_as_path(self) -> None:
        result = linkify_markdown("Visit https://example.com/docs/api.json")
        # Should be a URL link, not a file path link
        assert "[https://example.com/docs/api.json]" in result

    def test_plan_md_extension(self, tmp_path: Path) -> None:
        """The .plan.md compound extension should be recognized."""
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "sprint.plan.md").write_text("plan")

        result = linkify_markdown("See docs/sprint.plan.md", base_dir=tmp_path)
        assert "[docs/sprint.plan.md](docs/sprint.plan.md)" in result


# ---------------------------------------------------------------------------
# Combined / edge cases
# ---------------------------------------------------------------------------


class TestCombined:
    """Tests for combined scenarios and edge cases."""

    def test_mixed_urls_and_paths(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "api.md").write_text("api")

        text = "See docs/api.md and https://example.com for info"
        result = linkify_markdown(text, base_dir=tmp_path)
        assert "[docs/api.md](docs/api.md)" in result
        assert "[https://example.com](https://example.com)" in result

    def test_empty_string(self) -> None:
        assert linkify_markdown("") == ""

    def test_no_links(self) -> None:
        text = "Just some plain text with no links."
        assert linkify_markdown(text) == text

    def test_realistic_gate_prompt(self, tmp_path: Path) -> None:
        """Simulates the exact gate prompt from the bug report."""
        (tmp_path / "docs" / "projects").mkdir(parents=True)
        for name in ["area-mode.plan.md", "init-help-updates.plan.md", "recent-mode.plan.md"]:
            (tmp_path / "docs" / "projects" / name).write_text("plan")

        text = (
            "Epic with 3 child issue plans found:\n\n"
            "- docs/projects/area-mode.plan.md\n\n"
            "- docs/projects/init-help-updates.plan.md\n\n"
            "- docs/projects/recent-mode.plan.md\n\n"
            "What would you like to do?"
        )
        result = linkify_markdown(text, base_dir=tmp_path)

        # Whitespace should be normalized
        assert "\n\n\n" not in result

        # All paths should be linkified
        assert "[docs/projects/area-mode.plan.md](docs/projects/area-mode.plan.md)" in result
        assert (
            "[docs/projects/init-help-updates.plan.md](docs/projects/init-help-updates.plan.md)"
            in result
        )
        assert "[docs/projects/recent-mode.plan.md](docs/projects/recent-mode.plan.md)" in result

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        """Paths that escape base_dir should not be linked."""
        result = linkify_markdown("See ../../../etc/passwd.txt", base_dir=tmp_path)
        assert "[../../../etc/passwd.txt]" not in result


# ---------------------------------------------------------------------------
# Fenced code block edge cases (issue #395 rewrite)
# ---------------------------------------------------------------------------


class TestFencedCodeEdgeCases:
    """Pins the fenced-code semantics the possessive-quantifier rewrite must
    preserve — deterministic, no wall-clock assertions."""

    def test_well_formed_fence_protects_contents(self) -> None:
        text = "```\nsrc/config/schema.py\n```"
        assert linkify_markdown(text) == text

    def test_language_tag_works(self) -> None:
        text = "```py\nsrc/config/schema.py\n```"
        assert linkify_markdown(text) == text

    def test_tilde_fence_works(self) -> None:
        text = "~~~\nsrc/config/schema.py\n~~~"
        assert linkify_markdown(text) == text

    def test_two_fences_each_protect_independently(self) -> None:
        text = "```\nsrc/a.py\n```\n\ntext\n\n```\nsrc/b.py\n```"
        result = linkify_markdown(text)
        assert "[src/a.py]" not in result
        assert "[src/b.py]" not in result

    def test_unclosed_fence_protects_nothing(self) -> None:
        """An unclosed fence keeps today's behaviour: it protects nothing,
        so a bare path after it is still linkified (issue #395 Q1)."""
        text = "```\ncode here\nmore docs/a.md"
        result = linkify_markdown(text)
        assert "[docs/a.md](docs/a.md)" in result

    def test_empty_fence_matches(self) -> None:
        text = "```\n```"
        assert linkify_markdown(text) == text

    def test_longer_opener_than_closer_no_longer_protects(self) -> None:
        """The possessive quantifier is CommonMark-correct (a closer must be
        at least as long as the opener) but is a behaviour change from the
        pre-fix greedy regex, which backtracked the opener to match a
        shorter closer. Pins the new behaviour: a 4-backtick opener closed
        by a 3-backtick fence no longer matches, so its contents are
        linkified."""
        text = "````\nsrc/a.py\n```\n"
        assert "[src/a.py](src/a.py)" in linkify_markdown(text)

    def test_longer_tilde_opener_than_closer_no_longer_protects(self) -> None:
        text = "~~~~\nsrc/a.py\n~~~\n"
        assert "[src/a.py](src/a.py)" in linkify_markdown(text)


# ---------------------------------------------------------------------------
# Existing markdown link protection (issue #395 rewrite)
# ---------------------------------------------------------------------------


class TestExistingLinkProtection:
    """Pins the ``_find_existing_link_spans`` scanner against the shapes
    fuzzed against the regex it replaces — output must equal input since
    nothing here should be re-linkified.

    Note: several of the shapes below (bare bracket/paren walls with no
    linkable content) can't actually detect whether protection fired, since
    the assertion holds whether or not the scanner works. The tests that
    embed real linkable content (paths/URLs) inside the candidate link are
    the ones that actually exercise the scanner.
    """

    def test_simple_paren_link(self) -> None:
        text = "See [a](b) for details"
        assert linkify_markdown(text) == text

    def test_simple_paren_link_with_linkable_content(self) -> None:
        """A path inside an existing link must stay unlinked."""
        text = "See [config](src/config/schema.py) for details"
        assert linkify_markdown(text) == text

    def test_simple_bracket_ref_link(self) -> None:
        text = "See [a][b] for details"
        assert linkify_markdown(text) == text

    def test_bracket_ref_link_with_linkable_content(self) -> None:
        """A URL inside the text half of a ``[text][ref]`` link must stay
        unlinked — this only holds if the bracket-ref alternative is
        actually evaluated (not merely deleted)."""
        text = "[see https://example.com here][ref]"
        assert linkify_markdown(text) == text

    def test_nested_image_link(self) -> None:
        text = "[![img](i.png)](u)"
        # Matches the current regex's actual (non-greedy-across-`]`) behaviour:
        # only the inner "[![img](i.png)" is protected, so the result is
        # unchanged from input either way (no path/URL content to linkify).
        assert linkify_markdown(text) == text

    def test_adjacent_links(self) -> None:
        text = "[x](y)[z](w)"
        assert linkify_markdown(text) == text

    def test_span_end_is_exclusive_of_following_content(self) -> None:
        """Pins the exact span boundary: a path immediately following a
        closed link (no separator) must still be linkified on its own,
        which only holds if the span end is `close_paren + 1`, not
        `close_paren`."""
        text = "x [a](b)docs/a.md"
        assert linkify_markdown(text) == "x [a](b)[docs/a.md](docs/a.md)"

    def test_unterminated_paren_link(self) -> None:
        text = "before [a]( after"
        assert linkify_markdown(text) == text

    def test_unterminated_bracket_link(self) -> None:
        text = "before [a][ after"
        assert linkify_markdown(text) == text

    def test_bare_bracket_wall(self) -> None:
        text = "[[[[[[[[[["
        assert linkify_markdown(text) == text

    def test_mixed_walls(self) -> None:
        text = "[[[[[]]]]]"
        assert linkify_markdown(text) == text


# ---------------------------------------------------------------------------
# Pathological-input performance (issue #395)
# ---------------------------------------------------------------------------


class TestPathologicalInputPerformance:
    """Guards against the O(n^2) regressions issue #395 fixed.

    CI runs ``-m "not real_api and not performance"``, so a
    ``performance``-marked test alone would not guard this regression in CI.
    The wall-clock assertions below are unmarked and deliberately generous
    (~500x margin over the fixed-code runtime) so they run in CI and still
    fail loudly if the quadratic behaviour returns, without being flaky
    under CI load.
    """

    def test_backtick_wall_completes_quickly(self) -> None:
        text = "`" * 100_000
        start = time.perf_counter()
        linkify_markdown(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, f"backtick wall took {elapsed:.3f}s (expected < 2s)"

    def test_bracket_wall_completes_quickly(self) -> None:
        text = "[" * 100_000
        start = time.perf_counter()
        linkify_markdown(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, f"bracket wall took {elapsed:.3f}s (expected < 2s)"

    def test_bracket_and_close_wall_completes_quickly(self) -> None:
        """Unlike a bare bracket wall (which exits the scanner loop on its
        first iteration and never exercises the skip-past-`]` logic the
        design rests on), a balanced wall of `[`s followed by `]`s reaches
        that path on every iteration. Pre-fix, this shape measures ~2.2s."""
        text = "[" * 50_000 + "]" * 50_000
        start = time.perf_counter()
        linkify_markdown(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, f"bracket+close wall took {elapsed:.3f}s (expected < 2s)"

    def test_paren_wall_completes_quickly(self) -> None:
        """Guards the `"[](" * k` shape: pre-fix, every `)` lookup failed
        and rescanned to the end of the string while the cursor advanced
        only ~3 characters per iteration."""
        text = "[](" * 30_000
        start = time.perf_counter()
        linkify_markdown(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, f"paren wall took {elapsed:.3f}s (expected < 2s)"

    def test_quote_token_completes_quickly(self) -> None:
        text = '"' * 100_000
        start = time.perf_counter()
        linkify_markdown(text)
        elapsed = time.perf_counter() - start
        # Tightened from the original 2.0s budget: fixed code measures
        # ~3ms here (still ~15x headroom), while the pre-fix quadratic
        # strip loop measures ~0.10s — so this threshold actually fails
        # on a revert, unlike a round 2.0s (see issue #395 review).
        assert elapsed < 0.05, f"quote token took {elapsed:.3f}s (expected < 0.05s)"

    @pytest.mark.performance
    def test_backtick_wall_scales_near_linearly(self) -> None:
        small = "`" * 40_000
        large = "`" * 80_000

        start = time.perf_counter()
        linkify_markdown(small)
        small_time = time.perf_counter() - start

        start = time.perf_counter()
        linkify_markdown(large)
        large_time = time.perf_counter() - start

        # Guard against a division by (near) zero making the ratio meaningless.
        floor = 1e-4
        ratio = max(large_time, floor) / max(small_time, floor)
        assert ratio < 2.5, f"doubling input scaled cost by {ratio:.2f}x (expected < 2.5x)"

    @pytest.mark.performance
    def test_bracket_wall_scales_near_linearly(self) -> None:
        small = "[" * 20_000 + "]" * 20_000
        large = "[" * 40_000 + "]" * 40_000

        start = time.perf_counter()
        linkify_markdown(small)
        small_time = time.perf_counter() - start

        start = time.perf_counter()
        linkify_markdown(large)
        large_time = time.perf_counter() - start

        floor = 1e-4
        ratio = max(large_time, floor) / max(small_time, floor)
        assert ratio < 2.5, f"doubling input scaled cost by {ratio:.2f}x (expected < 2.5x)"

    @pytest.mark.performance
    def test_quote_token_scales_near_linearly(self) -> None:
        small = '"' * 40_000
        large = '"' * 80_000

        start = time.perf_counter()
        linkify_markdown(small)
        small_time = time.perf_counter() - start

        start = time.perf_counter()
        linkify_markdown(large)
        large_time = time.perf_counter() - start

        floor = 1e-4
        ratio = max(large_time, floor) / max(small_time, floor)
        assert ratio < 2.5, f"doubling input scaled cost by {ratio:.2f}x (expected < 2.5x)"

    def test_cap_skips_linkification_but_still_normalizes(self) -> None:
        """A string longer than MAX_LINKIFY_CHARS skips linkification (a
        would-be-linkified path stays bare) but whitespace is still
        normalized."""
        padding = "x" * MAX_LINKIFY_CHARS
        text = f"{padding}\n\n\ndocs/a.md"
        assert len(text) > MAX_LINKIFY_CHARS

        result = linkify_markdown(text)

        assert "[docs/a.md](docs/a.md)" not in result
        assert "docs/a.md" in result
        assert "\n\n\n" not in result
