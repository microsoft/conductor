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
            # The property that prevents #382.
            assert console._markup is False
        finally:
            run_module.close_file_logging()

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


class TestConsoleSinkEscapesAgentText:
    """The verbose console sink keeps markup for conductor's own styling, so
    agent text must be escaped rather than the console de-featured."""

    def test_escaped_content_does_not_raise_with_markup_enabled(self) -> None:
        from rich.markup import escape

        buf = io.StringIO()
        console = _plain_console(buf, markup=True)
        for content in MARKUP_TRIGGERS:
            console.print(Panel(escape(content), title="[cyan]Prompt[/cyan]", border_style="dim"))

    def test_verbose_section_escapes_before_rendering(self, tmp_path: Path) -> None:
        """Drive the real console path and assert it does not raise.

        Without ``escape()`` at the call site this raises ``MarkupError``,
        because ``_verbose_console`` deliberately keeps ``markup=True`` for the
        ``[cyan]`` title.
        """
        try:
            run_module.init_file_logging(tmp_path / "run.log")
            with (
                patch("conductor.cli.app.is_verbose", return_value=True),
                patch("conductor.cli.app.is_full", return_value=True),
                run_module._verbose_console.capture() as captured,
            ):
                for content in MARKUP_TRIGGERS:
                    run_module.verbose_log_section("Prompt for 'coder'", content)
        finally:
            run_module.close_file_logging()

        rendered = "".join(captured.get().split())
        assert "".join(MARKUP_TRIGGERS[0].split()) in rendered
