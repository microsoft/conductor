"""Tests for markup safety when rendering agent-supplied text (issue #382).

Rich parses ``[...]`` as markup. Agent output is arbitrary text and regularly
contains bracketed tokens — code, globs, regexes, ARM/URI patterns — so a
closing-tag form like ``[/nestedType]`` in ordinary technical prose is parsed as
a closing tag and raises ``MarkupError``.

This is not cosmetic. It killed a long-running workflow mid-execution and then
killed both resume attempts at the identical byte offset, because the offending
text is checkpointed and replayed verbatim. Until the checkpoint was hand-edited
the run was permanently unresumable.

Two sinks render agent text, and only one of them is verbosity-gated:

- ``run.py`` console path — FULL + verbose only.
- ``run.py`` file path — live whenever ``--log-file`` is passed, with **no**
  verbosity condition. ``--log-file auto`` is the natural choice for a long
  background run, which is exactly when losing the run hurts most.

The trigger is user data: the content is the rendered prompt, which embeds the
plan/workflow text the user supplied.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from conductor.cli import run as run_module

# Bracketed forms that appear in ordinary technical prose and that rich parses
# as closing tags. The first is the real-world trigger: an Azure RBAC permission
# format, verbatim from a plan document.
MARKUP_TRIGGERS = [
    "Permission format: {provider}/{type}[/{nestedType}...]/read",
    "[/nestedType]",
    "[/]",
    "[/bold]",
    "text [/foo] more",
]

# Opening tags are the quiet failure: rich consumes them without raising, and
# the text is simply gone. A crash-only suite cannot tell a correct fix from one
# that silently drops agent text — a future narrowing of the escaper to a
# closing-tag-only regex would pass every test above while losing content.
OPENING_TAG_TRIGGERS = [
    "Use [bold] to emphasise and [red] for errors",
    "Config key [section] then [other]",
]


def _plain_console(file: io.StringIO, *, markup: bool) -> Console:
    """A console shaped like the file-logging one, with markup configurable."""
    return Console(file=file, no_color=True, highlight=False, width=200, markup=markup)


class TestMarkupTriggersAreRealTriggers:
    """Guard the fixtures: these must genuinely raise without the fix."""

    @pytest.mark.parametrize("content", MARKUP_TRIGGERS)
    def test_sample_raises_when_markup_is_enabled(self, content: str) -> None:
        """A sample that does not raise would make its cases pass vacuously."""
        from rich.errors import MarkupError

        buf = io.StringIO()
        console = _plain_console(buf, markup=True)
        with pytest.raises(MarkupError):
            console.print(Panel(content, title="Prompt", border_style="dim"))


class TestFileConsoleRendersAgentTextSafely:
    """The non-verbose sink — reachable with only ``--log-file``."""

    @pytest.mark.parametrize("content", MARKUP_TRIGGERS)
    def test_markup_disabled_console_does_not_raise(self, content: str) -> None:
        buf = io.StringIO()
        console = _plain_console(buf, markup=False)
        console.print(Panel(content, title="Prompt", border_style="dim"))

    def test_literal_text_is_preserved(self) -> None:
        """Disabling markup must not silently drop the bracketed token."""
        content = "Permission format: {provider}/{type}[/{nestedType}...]/read"
        buf = io.StringIO()
        console = _plain_console(buf, markup=False)
        console.print(Panel(content, title="Prompt", border_style="dim"))
        # The panel wraps, so compare with whitespace and borders removed.
        rendered = "".join(buf.getvalue().split())
        assert "".join(content.split()) in rendered

    def test_init_file_logging_builds_a_markup_free_console(self, tmp_path: Path) -> None:
        """The production console, not a hand-rolled one.

        This is the assertion that fails if someone drops ``markup=False`` from
        ``init_file_logging`` — the behavioural tests above construct their own
        console and would keep passing.
        """
        try:
            run_module.init_file_logging(tmp_path / "run.log")
            console = run_module._file_console
            assert console is not None
            assert console.no_color is True
            # Asserted behaviourally rather than via ``console._markup``: that
            # attribute is private, ``rich`` is pinned only ``>=13.0.0``, and
            # there is no public accessor. What matters is that markup text
            # reaches the file intact, which survives a rich upgrade.
            console.print("[bold]not a style[/bold]")
        finally:
            run_module.close_file_logging()

        assert "[bold]not a style[/bold]" in (tmp_path / "run.log").read_text(encoding="utf-8")

    def test_agent_text_survives_the_real_file_sink(self, tmp_path: Path) -> None:
        """End-to-end through ``verbose_log_section`` with the real console.

        ``should_console`` is left false so this exercises the *file* path
        specifically — the one that has no verbosity gate.
        """
        log = tmp_path / "run.log"
        try:
            run_module.init_file_logging(log)
            with (
                patch("conductor.cli.app.is_verbose", return_value=False),
                patch("conductor.cli.app.is_full", return_value=False),
            ):
                for content in MARKUP_TRIGGERS:
                    run_module.verbose_log_section("Prompt for 'coder'", content)
        finally:
            run_module.close_file_logging()

        written = "".join(log.read_text(encoding="utf-8").split())
        assert "".join(MARKUP_TRIGGERS[0].split()) in written


class TestConsoleSinkRendersAgentTextSafely:
    """The verbose console sink.

    When #382 was fixed this console still parsed markup for conductor's own
    ``[cyan]`` title, so the content had to be wrapped at the call site. Issue
    #406 inverted that default — ``_verbose_console`` is now markup-free like
    the file console — but the call-site wrapper is still what the tests here
    pin, because it is what keeps the panel *body* exact.
    """

    def test_wrapped_content_does_not_raise_with_markup_enabled(self) -> None:
        """The old mechanism, kept as the isolating case.

        A ``Text`` is safe even on a console that still parses markup, so this
        fails if the call-site wrapper is dropped on the assumption that
        ``markup=False`` alone now covers it — which is true for the body but
        not for a ``Panel`` title.
        """
        buf = io.StringIO()
        console = _plain_console(buf, markup=True)
        for content in MARKUP_TRIGGERS:
            console.print(Panel(Text(content), title=Text("Prompt"), border_style="dim"))

    def test_verbose_section_renders_agent_text_intact(self) -> None:
        """Drive the real console path and assert it does not raise.

        Deliberately no ``init_file_logging`` here. Pulling the file sink in
        costs the isolating signal: reverting ``markup=False`` alone would kill
        this test on the file console before it ever reached its console
        assertion, so no test would isolate the console-side fix.
        """
        with (
            patch("conductor.cli.app.is_verbose", return_value=True),
            patch("conductor.cli.app.is_full", return_value=True),
            run_module._verbose_console.capture() as captured,
        ):
            for content in MARKUP_TRIGGERS:
                run_module.verbose_log_section("Prompt for 'coder'", content)

        rendered = "".join(captured.get().split())
        for trigger in MARKUP_TRIGGERS:
            assert "".join(trigger.split()) in rendered


class TestOpeningTagsSurviveToo:
    """Opening tags never raise -- rich just eats the text.

    A crash-only suite passes against a fix that silently drops agent content,
    so the quiet failure needs its own assertions.
    """

    @pytest.mark.parametrize("content", OPENING_TAG_TRIGGERS)
    def test_opening_tags_are_dropped_without_the_fix(self, content: str) -> None:
        """The negative control: proves these samples are real triggers."""
        buf = io.StringIO()
        _plain_console(buf, markup=True).print(content)
        assert content not in buf.getvalue(), "sample does not exercise tag-eating"

    @pytest.mark.parametrize("content", OPENING_TAG_TRIGGERS)
    def test_opening_tags_survive_the_file_sink(self, content: str) -> None:
        buf = io.StringIO()
        _plain_console(buf, markup=False).print(content)
        assert content in buf.getvalue()

    @pytest.mark.parametrize("content", OPENING_TAG_TRIGGERS)
    def test_opening_tags_survive_the_console_sink(self, content: str) -> None:
        with (
            patch("conductor.cli.app.is_verbose", return_value=True),
            patch("conductor.cli.app.is_full", return_value=True),
            run_module._verbose_console.capture() as captured,
        ):
            run_module.verbose_log_section("Prompt", content)
        assert "".join(content.split()) in "".join(captured.get().split())


class TestEveryVerboseSinkIsSafe:
    """`style=` does not disable markup parsing, which hid three more sinks.

    Two of these are reachable on a bare ``conductor run``: ``verbose_mode`` and
    ``full_mode`` both default to True, so this is not a verbose-only path.
    """

    @pytest.mark.parametrize("content", MARKUP_TRIGGERS)
    def test_verbose_log_survives_agent_text(self, content: str) -> None:
        with (
            patch("conductor.cli.app.is_verbose", return_value=True),
            run_module._verbose_console.capture() as captured,
        ):
            run_module.verbose_log(content, style="dim")
        assert "".join(content.split()) in "".join(captured.get().split())

    def test_style_kwarg_does_not_disable_markup(self) -> None:
        """The assumption that made those sinks look safe, pinned as false."""
        from rich.errors import MarkupError

        buf = io.StringIO()
        with pytest.raises(MarkupError):
            _plain_console(buf, markup=True).print(MARKUP_TRIGGERS[1], style="red dim")


class TestExperimentalBannerIsNotMarkupInTheLog:
    """The banner prints one Panel to both consoles.

    Both are markup-free (the file console since #382, the verbose console
    since #406), so a markup-bearing string writes its tags out literally --
    a readability regression in the log file that no existing test could
    observe, because they swap the console for a MagicMock.
    """

    def test_banner_is_styled_not_tagged_in_the_file_log(self, tmp_path: Path) -> None:
        log = tmp_path / "run.log"
        run_module._PRINTED_EXPERIMENTAL_BANNERS.clear()
        try:
            run_module.init_file_logging(log)
            run_module._maybe_print_experimental_banner(
                {
                    "run_id": "r1",
                    "providers": {
                        "claude-agent-sdk": {
                            "tier": "experimental",
                            "upstream_pin": "0.2.87",
                            "maintainer": "@someone",
                        }
                    },
                }
            )
        finally:
            run_module.close_file_logging()
            run_module._PRINTED_EXPERIMENTAL_BANNERS.clear()

        written = log.read_text(encoding="utf-8")
        assert "claude-agent-sdk" in written
        for tag in ("[bold]", "[/bold]", "[dim]", "[/dim]", "[link]"):
            assert tag not in written, f"{tag} leaked into the log as literal text"
