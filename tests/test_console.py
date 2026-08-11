"""Tests for the markup-safe console primitives (issue #406).

``styled`` exists so that conductor's own styling and interpolated runtime
data cannot be confused for one another. The properties that matter are:

* a value is reproduced **byte for byte**, whatever brackets it contains
* the template's styling still applies, including where a style spans a
  placeholder or where tags nest around one

Both halves need assertions. A suite that only checks "does not raise" cannot
tell a correct fix from one that silently deletes the value, which is exactly
the quiet failure mode of an opening tag like ``[dim]``.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from rich.errors import MarkupError
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from conductor.console import join, make_console, styled

# Values that a workflow, an agent name, a plugin manifest or an exception
# string can genuinely contain. Split by failure mode, because they fail
# differently and a fix can address one without the other.
CRASHING_VALUES = [
    "[/bold]",
    "[/]",
    "[/etc/x]",
    "docs/[/a].md",
    "probe [/bold] name",
    "Permission format: {provider}/{type}[/{nestedType}...]/read",
]

DELETED_VALUES = [
    "[dim]",
    "[task1]",
    "[#42]",
    "[user@example.com]",
    "config [section] then [other]",
]

# Rendered literally by rich even without the fix. Kept so a regression that
# only handles the dangerous shapes is still visibly wrong for the safe ones.
SAFE_VALUES = ["[0]", "[KPI_3]", "plain", ""]

ALL_VALUES = CRASHING_VALUES + DELETED_VALUES + SAFE_VALUES


def render(renderable: object, *, width: int = 400) -> str:
    """Render on a markup-free console and return the plain text."""
    buf = io.StringIO()
    make_console(file=buf, width=width, no_color=True, highlight=False).print(renderable)
    return buf.getvalue().rstrip("\n")


def render_ansi(renderable: object) -> str:
    """Render with colour so styling can be asserted."""
    buf = io.StringIO()
    make_console(
        file=buf, width=400, force_terminal=True, color_system="truecolor", highlight=False
    ).print(renderable)
    return buf.getvalue().rstrip("\n")


class TestTheseValuesAreRealTriggers:
    """Negative controls: without the fix these genuinely misbehave.

    Without them the parametrised cases below could pass vacuously against a
    corpus of harmless strings.
    """

    @pytest.mark.parametrize("value", CRASHING_VALUES)
    def test_crashing_values_raise_when_parsed_as_markup(self, value: str) -> None:
        buf = io.StringIO()
        with pytest.raises(MarkupError):
            Console(file=buf, markup=True).print(f"Name: {value}")

    @pytest.mark.parametrize("value", DELETED_VALUES)
    def test_deleted_values_lose_text_when_parsed_as_markup(self, value: str) -> None:
        buf = io.StringIO()
        Console(file=buf, width=400, markup=True, highlight=False).print(f"Name: {value}")
        assert value not in buf.getvalue(), "sample does not exercise tag-eating"


class TestValuesAreByteExact:
    """The core contract."""

    @pytest.mark.parametrize("value", ALL_VALUES)
    def test_value_survives_interpolation(self, value: str) -> None:
        assert render(styled("Name: [green]{}[/green] done", value)) == f"Name: {value} done"

    @pytest.mark.parametrize("value", ALL_VALUES)
    def test_value_survives_without_surrounding_markup(self, value: str) -> None:
        assert render(styled("Name: {}", value)) == f"Name: {value}"

    def test_backslash_before_bracket_is_preserved(self) -> None:
        """``escape()`` cannot do this, which is why it is not used.

        The parser treats ``\\[`` as an escaped bracket, so a regex round
        trips through ``escape`` + markup parsing as ``[0-9\\]+``.
        """
        from rich.markup import escape

        value = r"\[0-9\]+"
        assert render(styled("{}", value)) == value

        buf = io.StringIO()
        Console(file=buf, width=400, markup=True, highlight=False).print(escape(value))
        assert buf.getvalue().rstrip("\n") != value, "escape() is unexpectedly byte-exact now"

    def test_value_containing_the_filler_character_is_preserved(self) -> None:
        assert render(styled("a{}b", "\x00mid\x00")) == "a\x00mid\x00b"

    def test_non_string_values_are_formatted_then_inserted(self) -> None:
        assert render(styled("{} and {}", 42, None)) == "42 and None"


class TestTemplateStylingStillApplies:
    """The other half: disabling markup must not disable conductor's styling."""

    def test_literal_only_template_is_styled(self) -> None:
        assert render_ansi(styled("[green]ok[/green]")) == "\x1b[32mok\x1b[0m"

    def test_style_spans_the_placeholder(self) -> None:
        assert render_ansi(styled("[green]{}[/green]", "payload")) == "\x1b[32mpayload\x1b[0m"

    def test_nested_tags_around_the_placeholder(self) -> None:
        """Inheriting only ``spans[0]`` would collapse this to one style."""
        assert render_ansi(styled("[bold][red]{}[/red][/bold]", "v")) == "\x1b[1;31mv\x1b[0m"

    def test_style_boundaries_are_not_shifted_by_a_longer_value(self) -> None:
        out = render_ansi(styled("[bold]a [red]{}[/red] b[/bold]", "wide-value"))
        assert out == "\x1b[1ma \x1b[0m\x1b[1;31mwide-value\x1b[0m\x1b[1m b\x1b[0m"

    def test_empty_value_does_not_shift_later_styles(self) -> None:
        assert render_ansi(styled("[green]{}[/green]|[red]{}[/red]", "", "b")) == (
            "|\x1b[31mb\x1b[0m"
        )

    def test_dangerous_value_does_not_disturb_surrounding_styles(self) -> None:
        out = render_ansi(styled("[bold]a[/bold] {} [green]b[/green]", "[/etc/x]"))
        assert out == "\x1b[1ma\x1b[0m [/etc/x] \x1b[32mb\x1b[0m"


class TestFormatSyntax:
    """``styled`` uses ``str.format`` field syntax, so the usual forms work."""

    def test_auto_numbered_fields(self) -> None:
        assert render(styled("{} / {}", "a[/b]", "c[d]")) == "a[/b] / c[d]"

    def test_explicit_positional_fields(self) -> None:
        assert render(styled("{1} {0}", "a", "b")) == "b a"

    def test_named_fields(self) -> None:
        assert render(styled("{n}", n="[/x]")) == "[/x]"

    def test_format_spec(self) -> None:
        assert render(styled("{:.2f}s", 1.239)) == "1.24s"

    def test_conversion(self) -> None:
        assert render(styled("{!r}", "q[/z]")) == "'q[/z]'"

    def test_literal_braces(self) -> None:
        assert render(styled("set {{a}} to {v}", v="[/x]")) == "set {a} to [/x]"

    def test_missing_positional_field_raises(self) -> None:
        with pytest.raises(IndexError):
            styled("{} {}", "only-one")

    def test_missing_named_field_raises(self) -> None:
        with pytest.raises(KeyError):
            styled("{missing}")

    def test_malformed_template_raises(self) -> None:
        """A malformed *template* is a conductor bug and should be loud."""
        with pytest.raises(MarkupError):
            styled("[/bold] {}", "value")

    def test_filler_character_in_the_template_is_rejected(self) -> None:
        """The placeholder machinery reserves NUL.

        A NUL in the template's own literal text would capture the search that
        locates each value and shift every later one, so it is refused rather
        than silently mis-rendered. Values stay unrestricted — see
        ``test_value_containing_the_filler_character_is_preserved``.
        """
        with pytest.raises(ValueError, match="NUL"):
            styled("a\x00b{}c", "X")


class TestStyledRefusesRatherThanLosingData:
    """The paths where silently doing something reasonable would lose styling."""

    def test_format_spec_on_a_text_is_refused(self) -> None:
        with pytest.raises(ValueError, match="format spec or conversion"):
            styled("[dim]{:>10}[/dim]", styled("[green]x[/green]"))

    def test_conversion_on_a_text_is_refused(self) -> None:
        """``!s`` would call ``str()`` on it, flattening the styling away."""
        with pytest.raises(ValueError, match="format spec or conversion"):
            styled("{!s}", styled("[green]x[/green]"))

    def test_a_field_inside_a_tag_is_a_clear_error(self) -> None:
        """The parser eats the placeholder, so the value has nowhere to land.

        The bare ``'\x00' is not in list`` named an internal detail and told
        the reader nothing about which template caused it.
        """
        with pytest.raises(ValueError, match="field inside a markup tag"):
            styled("[link={}]docs[/link]", "https://example.com")

    def test_dotted_and_indexed_fields_resolve(self) -> None:
        """``get_field`` rather than hand-rolled resolution."""

        class Agent:
            name = "review[/x]"

        assert render(styled("{a.name} {d[0]}", a=Agent(), d=["first[/y]"])) == (
            "review[/x] first[/y]"
        )


class TestTextIsNotFlattenedByCallers:
    """``str(Text)`` is its plain form, so an f-string discards the styling.

    This is not hypothetical: converting call sites to ``styled`` introduced
    exactly this in three places, where a ``styled(...)`` result was then
    interpolated into an f-string and lost its colour. Worse, feeding a
    ``Text`` built from markup to the *builtin* ``print`` drops any text rich
    parsed as a tag, which silently deleted a ``[workspace-instructions]``
    log prefix that users grep for.
    """

    def test_f_string_flattens_a_styled_text(self) -> None:
        """The trap itself, pinned so the reason the guards exist stays clear."""
        assert f"{styled('[green]{}[/green]', 'ok')}" == "ok"

    def test_from_markup_deletes_a_lowercase_bracketed_prefix(self) -> None:
        """Why a plain-text log label must not be routed through markup."""
        assert Text.from_markup("[workspace-instructions] 0 files").plain == " 0 files"


class TestStyledReachesTheSinksThatBypassTheConsole:
    """``Panel`` titles and prompts parse markup regardless of the console.

    ``rich/panel.py`` and ``rich/prompt.py`` call ``Text.from_markup``
    unconditionally, so ``markup=False`` never reaches them. These are the
    sites where an f-string still crashes, so they need their own coverage.
    """

    @pytest.mark.parametrize("value", CRASHING_VALUES + DELETED_VALUES)
    def test_panel_title(self, value: str) -> None:
        title = styled("[cyan]Prompt for '{}'[/cyan]", value)
        assert f"Prompt for '{value}'" in render(Panel(Text("body"), title=title))

    @pytest.mark.parametrize("value", CRASHING_VALUES + DELETED_VALUES)
    def test_panel_subtitle(self, value: str) -> None:
        subtitle = styled("[dim]{}[/dim]", value)
        assert value in render(Panel(Text("body"), subtitle=subtitle))

    @pytest.mark.parametrize("cls", [Prompt, Confirm, IntPrompt])
    @pytest.mark.parametrize("value", CRASHING_VALUES + DELETED_VALUES)
    def test_prompt_text(self, cls: type, value: str) -> None:
        prompt = cls(styled("[bold]{}[/bold]", value), console=make_console(file=io.StringIO()))
        assert value in str(prompt.make_prompt(default=...))

    @pytest.mark.parametrize("value", CRASHING_VALUES)
    def test_f_string_in_a_panel_title_still_crashes(self, value: str) -> None:
        """The reason these sites need ``styled`` rather than the console flip.

        If a future rich release starts honouring ``markup=False`` here, this
        test fails and the corresponding guard rule can be relaxed.
        """
        with pytest.raises(MarkupError):
            render(Panel(Text("body"), title=f"[cyan]{value}[/cyan]"))

    @pytest.mark.parametrize("value", DELETED_VALUES)
    def test_f_string_in_a_panel_title_still_deletes_text(self, value: str) -> None:
        """The quiet half of the same bypass, which raises nothing at all."""
        assert value not in render(Panel(Text("body"), title=f"[cyan]{value}[/cyan]"))


class TestTextValuesAreSpliced:
    """A pre-styled fragment keeps its styling when interpolated.

    Without this, composing ``styled("{} {}", CHECK, name)`` would call
    ``format()`` on the ``Text`` and flatten it to plain characters, silently
    dropping the colour from every doctor table cell.
    """

    def test_text_value_keeps_its_style(self) -> None:
        check = styled("[green]✓[/green]")
        assert render_ansi(styled("{} ok", check)) == "\x1b[32m✓\x1b[0m ok"

    def test_text_value_and_template_styles_coexist(self) -> None:
        check = styled("[green]✓[/green]")
        out = render_ansi(styled("{} [dim]{}[/dim]", check, "note"))
        assert out == "\x1b[32m✓\x1b[0m \x1b[2mnote\x1b[0m"

    def test_text_value_contents_are_byte_exact(self) -> None:
        assert render(styled("{}", Text("[/etc/x]"))) == "[/etc/x]"

    def test_text_value_with_internal_spans(self) -> None:
        fragment = styled("[red]a[/red]b")
        assert render_ansi(styled("<{}>", fragment)) == "<\x1b[31ma\x1b[0mb>"

    def test_empty_text_value_does_not_shift_later_styles(self) -> None:
        assert render_ansi(styled("{}[red]{}[/red]", Text(""), "b")) == "\x1b[31mb\x1b[0m"

    def test_multiple_text_values(self) -> None:
        a = styled("[green]A[/green]")
        b = styled("[red]B[/red]")
        assert render_ansi(styled("{} {}", a, b)) == "\x1b[32mA\x1b[0m \x1b[31mB\x1b[0m"

    def test_a_text_with_a_base_style_keeps_it(self) -> None:
        """The ``value.style`` branch — a ``Text`` styled at construction.

        Every ``Text`` conductor passes today carries its styling in spans
        rather than a base style, so without this the branch that re-anchors
        the base style is never exercised.
        """
        assert render_ansi(styled("<{}>", Text("x", style="green"))) == "<\x1b[32mx\x1b[0m>"

    def test_a_base_style_does_not_override_an_inner_span(self) -> None:
        fragment = Text("ab", style="green")
        fragment.stylize("bold", 0, 1)
        assert render_ansi(styled("{}", fragment)) == "\x1b[1;32ma\x1b[0m\x1b[32mb\x1b[0m"

    def test_mixed_text_and_plain_values(self) -> None:
        check = styled("[green]✓[/green]")
        assert render(styled("{} {} {}", check, "x[/y]", 3)) == "✓ x[/y] 3"


class TestJoin:
    """``join`` is what carries the mixed ``content_lines`` lists.

    ``Text.join`` requires every part to already be a ``Text``, but the shape
    in ``gates/`` is a list holding some conductor-styled fragments and some
    plain runtime values. Untested, a regression to ``"\n".join(str(x))``
    passes the whole suite while flattening every styled fragment.
    """

    def test_plain_parts_stay_literal(self) -> None:
        assert render(join("\n", ["[dim]", "[/etc/x]"])) == "[dim]\n[/etc/x]"

    def test_text_parts_keep_their_styling(self) -> None:
        out = render_ansi(join(" ", [styled("[green]a[/green]"), "b[/c]"]))
        assert out == "\x1b[32ma\x1b[0m b[/c]"

    def test_a_plain_separator_stays_literal(self) -> None:
        assert render(join("[dim]", ["a", "b"])) == "a[dim]b"

    def test_a_text_separator_keeps_its_styling(self) -> None:
        out = render_ansi(join(styled("[red]|[/red]"), ["a", "b"]))
        assert out == "a\x1b[31m|\x1b[0mb"

    def test_empty_iterable_is_empty(self) -> None:
        assert render(join("\n", [])) == ""

    def test_single_part_has_no_separator(self) -> None:
        assert render(join(", ", ["only[/x]"])) == "only[/x]"

    @pytest.mark.parametrize("value", CRASHING_VALUES + DELETED_VALUES)
    def test_dangerous_values_survive(self, value: str) -> None:
        assert value in render(join(" ", ["before", value, "after"]))


class TestMakeConsole:
    """The inverted default."""

    def test_plain_strings_are_not_parsed(self) -> None:
        assert render("literal [/etc/x] text") == "literal [/etc/x] text"

    def test_opening_tags_are_not_deleted(self) -> None:
        assert render("keep [dim] this") == "keep [dim] this"

    def test_table_cells_are_not_parsed(self) -> None:
        table = Table(show_header=False, box=None)
        table.add_column("v")
        for value in CRASHING_VALUES + DELETED_VALUES:
            table.add_row(value)
        rendered = render(table)
        for value in CRASHING_VALUES + DELETED_VALUES:
            assert value in rendered

    def test_table_headers_and_titles_are_not_parsed(self) -> None:
        table = Table(title="t [/etc/x]", caption="c [dim] c")
        table.add_column("h [/etc/x]")
        table.add_row("v")
        rendered = render(table)
        assert "t [/etc/x]" in rendered
        assert "h [/etc/x]" in rendered
        assert "c [dim] c" in rendered

    def test_panel_body_is_not_parsed(self) -> None:
        assert "[/etc/x]" in render(Panel("body [/etc/x] end"))

    def test_style_kwarg_still_styles(self) -> None:
        """``style=`` is orthogonal to markup and must keep working."""
        buf = io.StringIO()
        make_console(
            file=buf, width=400, force_terminal=True, color_system="truecolor", highlight=False
        ).print("x [/etc/x] y", style="bold")
        out = buf.getvalue()
        assert "\x1b[1m" in out
        assert "[/etc/x]" in out

    def test_text_renderables_are_still_styled(self) -> None:
        text = Text()
        text.append("green", style="green")
        assert render_ansi(text) == "\x1b[32mgreen\x1b[0m"

    def test_print_json_is_unaffected(self) -> None:
        """The ``--silent`` stdout result contract runs through here."""
        buf = io.StringIO()
        make_console(file=buf, width=400, no_color=True).print_json('{"a": "x [/etc/p] y"}')
        assert "[/etc/p]" in buf.getvalue()

    def test_markup_cannot_be_re_enabled(self) -> None:
        with pytest.raises(TypeError, match="markup is not overridable"):
            make_console(markup=True)

    def test_markup_cannot_be_re_enabled_per_call(self) -> None:
        """The per-call kwarg reopens both original failure modes.

        Rich lets ``print(..., markup=True)`` override the instance setting, so
        without this refusal one call site restores the silent deletion and the
        MarkupError. It is also the obvious-looking fix for the visible
        ``[green]`` that forgetting ``styled`` now produces, which is exactly
        why it has to be refused rather than documented against.
        """
        console = make_console(file=io.StringIO())
        with pytest.raises(TypeError, match="markup is not overridable"):
            console.print("plugin [task1] name", markup=True)
        with pytest.raises(TypeError, match="markup is not overridable"):
            console.log("plugin [task1] name", markup=True)

    def test_a_redundant_markup_false_is_tolerated(self) -> None:
        """Only a *truthy* markup is refused.

        ``Console.input`` forwards ``markup=`` to ``print`` unconditionally,
        so refusing the kwarg's mere presence breaks every prompt that passes
        ``console=``. A redundant ``markup=False`` restates the guarantee.
        """
        buf = io.StringIO()
        make_console(file=buf, width=200, no_color=True).print("x [/etc/p] y", markup=False)
        assert "[/etc/p]" in buf.getvalue()

    def test_prompt_works_when_given_this_console(self) -> None:
        """``Prompt.ask(console=...)`` goes through ``Console.input``."""
        from rich.prompt import Prompt

        console = make_console(file=io.StringIO(), width=200, no_color=True)
        answer = Prompt.ask(
            styled("[bold]Enter {}[/bold]", "a[/etc/x]"),
            console=console,
            stream=io.StringIO("value\n"),
        )
        assert answer == "value"

    def test_prompt_text_is_not_parsed_when_given_this_console(self) -> None:
        buf = io.StringIO()
        console = make_console(file=buf, width=200, no_color=True)
        Prompt.ask(Text("name [/etc/x]"), console=console, stream=io.StringIO("v\n"))
        assert "[/etc/x]" in buf.getvalue()

    def test_log_still_forwards_when_markup_is_not_passed(self) -> None:
        """The override must guard the kwarg without breaking the method."""
        buf = io.StringIO()
        make_console(file=buf, width=200, no_color=True).log("keep [dim] this")
        assert "keep [dim] this" in buf.getvalue()

    def test_subclasses_inherit_the_refusal(self) -> None:
        """The rule lives on the class, so a subclass cannot bypass it."""
        from conductor.cli.run import _SilentAwareConsole

        with pytest.raises(TypeError, match="markup is not overridable"):
            _SilentAwareConsole(markup=True)

    def test_other_console_options_are_forwarded(self) -> None:
        console = make_console(stderr=True, width=123, no_color=True)
        assert console.stderr is True
        assert console.width == 123


class TestHighlightingIsPreserved:
    """Replacing a markup string with a ``Text`` must not drop rich's colour.

    rich applies its ``ReprHighlighter`` to a plain ``str`` passed to
    ``print`` but not to a ``Text``. Without compensating, converting call
    sites to ``styled`` would silently de-colour every number, path and
    quoted value across the whole CLI — a cosmetic regression bundled into a
    safety fix, which is exactly what makes a change hard to review.
    """

    @staticmethod
    def _ansi(renderable: object, **kwargs: object) -> str:
        buf = io.StringIO()
        console = make_console(
            file=buf, width=400, force_terminal=True, color_system="truecolor", **kwargs
        )
        console.print(renderable)
        return buf.getvalue()

    def test_numbers_in_a_styled_text_are_highlighted(self) -> None:
        """The concrete regression: ``Total steps: 1`` lost its cyan number."""
        out = self._ansi(styled("[dim]Total steps:[/dim] {}", 42))
        assert "\x1b[1;36m42\x1b[0m" in out, out

    def test_conductor_styling_wins_over_the_highlighter(self) -> None:
        """rich highlights first and copies markup styles on top.

        The number highlighter would colour ``42`` bold cyan; the template's
        green wins the colour while the highlighter's bold survives — which
        is exactly what the equivalent markup string produced.
        """
        buf = io.StringIO()
        Console(
            file=buf, width=400, force_terminal=True, color_system="truecolor", markup=True
        ).print("[green]42[/green]")
        assert self._ansi(styled("[green]{}[/green]", "42")) == buf.getvalue()

    def test_highlight_false_console_is_not_highlighted(self) -> None:
        out = self._ansi(styled("count {}", 42), highlight=False)
        assert "\x1b[1;36m" not in out

    def test_highlight_false_kwarg_is_honoured(self) -> None:
        buf = io.StringIO()
        console = make_console(file=buf, width=400, force_terminal=True, color_system="truecolor")
        console.print(styled("count {}", 42), highlight=False)
        assert "\x1b[1;36m" not in buf.getvalue()

    def test_highlighting_does_not_reintroduce_markup_parsing(self) -> None:
        """The highlighter only adds styles; it must not eat or reject text."""
        for value in CRASHING_VALUES + DELETED_VALUES:
            assert value in render(styled("{}", value))

    def test_matches_the_markup_string_it_replaces(self) -> None:
        """Byte-for-byte parity with the pre-fix rendering path."""
        buf = io.StringIO()
        Console(
            file=buf, width=400, force_terminal=True, color_system="truecolor", markup=True
        ).print("[dim]Total:[/dim] 1353 checkpoint(s)")
        before = buf.getvalue()
        after = self._ansi(styled("[dim]Total:[/dim] {} checkpoint(s)", 1353))
        assert after == before
