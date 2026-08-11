"""Markup-safe console primitives.

Rich parses a plain ``str`` as console markup, so any runtime value
interpolated into a printed string is parsed as if it were conductor's own
styling. In rich, a bracketed token is a tag when its **first character** is
a lowercase letter, ``#``, ``/`` or ``@``, which splits the behaviour three
ways and only one of them is loud:

===================  ==================  =====================================
first character      example             result
===================  ==================  =====================================
digit / uppercase    ``[0]``             rendered literally
lowercase, ``#``,    ``[task1]``         **silently deleted**
``@``
``/``                ``[/etc/x]``        **MarkupError, out of the print call**
===================  ==================  =====================================

Conductor renders workflow and agent names, plugin and marketplace metadata
read out of cloned third-party repositories, exception strings and gate
payloads. All of them can contain a bracketed token, so this module exists to
keep the two apart:

* :func:`make_console` builds every console with ``markup=False``, which
  inverts the default — a plain string is literal unless it asks to be
  styled. This covers plain prints, ``Panel`` bodies, ``Table`` cells,
  headers, titles and captions, and ``Rule`` titles.
* :func:`styled` is the one way to opt back in. The template is conductor's
  own literal and is parsed; interpolated values are inserted verbatim and
  never reach the parser.

``markup=False`` is necessary but not sufficient. ``Panel(title=)``,
``Panel(subtitle=)`` and ``Prompt``/``Confirm``/``IntPrompt`` prompts call
``Text.from_markup`` unconditionally (``rich/panel.py``, ``rich/prompt.py``),
so the console setting never reaches them. Pass those a :class:`~rich.text.Text`
— :func:`styled` returns one — rather than an f-string.

``rich.markup.escape`` is deliberately not used. It is not byte-exact: the
parser treats ``\\[`` as an escaped bracket, so ``\\[0-9\\]+`` — an ordinary
regex — renders as ``[0-9\\]+`` whether or not it was escaped first. Building
a :class:`~rich.text.Text` avoids the parser entirely and is exact.

See issues #382, #387 and #406.
"""

from __future__ import annotations

import string
from collections.abc import Iterable
from typing import Any

from rich.console import Console, HighlighterType
from rich.text import Span, Text

# Placeholder filler. Runs of this character stand in for interpolated values
# while the template is parsed, and are overwritten afterwards.
#
# One filler character per character of the final value is written, so the
# spans rich computes over the parsed template already cover the value's full
# width and stay valid through the substitution. That is what makes nested
# styling around a placeholder correct; reading ``spans[0].style`` and
# re-applying it — the obvious alternative — collapses ``[bold][red]{}[/red]``
# to a single style.
_FILLER = "\x00"

_FORMATTER = string.Formatter()


def _highlighted(highlighter: HighlighterType, text: Text) -> Text:
    """Apply *highlighter* to *text* the way rich does for a plain string.

    ``Console.render_str`` highlights the plain characters first and copies
    the markup-derived spans on top, so conductor's own styling wins where the
    two overlap. Reproducing that order keeps a ``Text`` rendering identically
    to the markup string it replaced.
    """
    out = highlighter(text.plain)
    out.copy_styles(text)
    out.justify = text.justify
    out.overflow = text.overflow
    out.no_wrap = text.no_wrap
    out.end = text.end
    out.style = text.style
    return out


class _MarkupFreeConsole(Console):
    """A ``Console`` that never parses markup but still highlights.

    ``markup=False`` is the point of the class. The highlighting is what keeps
    it a pure safety change: rich applies its ``ReprHighlighter`` to a plain
    ``str`` but not to a ``Text``, so replacing a markup string with a
    ``styled`` call would otherwise silently drop the colour rich gives
    numbers, paths and quoted values across the whole CLI.

    Only top-level ``Text`` arguments are highlighted, matching rich's own
    behaviour of highlighting only the strings passed directly to ``print``.
    """

    def __init__(self, **kwargs: Any) -> None:
        # Captured rather than read back off the instance: the attribute rich
        # stores it in is private, and ``rich`` is pinned only ``>=13.0.0``.
        self._conductor_highlight: bool = bool(kwargs.get("highlight", True))
        kwargs.pop("markup", None)
        super().__init__(markup=False, **kwargs)

    def print(self, *objects: Any, **kwargs: Any) -> None:
        highlight = kwargs.get("highlight")
        if highlight is None:
            highlight = self._conductor_highlight
        if highlight and self.highlighter is not None:
            objects = tuple(
                _highlighted(self.highlighter, obj) if type(obj) is Text else obj for obj in objects
            )
        super().print(*objects, **kwargs)


def make_console(**kwargs: Any) -> Console:
    """Build a ``Console`` that does not parse markup.

    ``markup`` is locked to ``False``: this is the inverted default that keeps
    an interpolated runtime value from being read as styling. Style output
    with :func:`styled` (or any ``Text``/renderable), not with markup in a
    plain string.

    Args:
        **kwargs: Forwarded to ``rich.console.Console``. Passing ``markup``
            is an error rather than an override — a console that parses
            markup is the defect this module exists to prevent.

    Returns:
        A configured ``Console``.

    Raises:
        TypeError: If ``markup`` is passed.
    """
    if "markup" in kwargs:
        raise TypeError(
            "make_console() does not accept 'markup': consoles are markup-free by "
            "design so interpolated runtime values cannot be parsed as styling. "
            "Use styled() to style conductor's own text."
        )
    return _MarkupFreeConsole(**kwargs)


def join(separator: str | Text, parts: Iterable[str | Text]) -> Text:
    """Join a mix of plain strings and pre-styled ``Text`` into one ``Text``.

    ``Text.join`` requires every part to already be a ``Text``, but the usual
    shape here is a ``content_lines`` list holding some conductor-styled
    fragments and some plain runtime values. Plain strings are treated as
    literal text, so a bracketed token in one is never parsed as styling.

    Args:
        separator: Placed between parts. A plain string is literal.
        parts: The fragments to join.

    Returns:
        The joined ``Text``.
    """
    sep = separator if isinstance(separator, Text) else Text(separator)
    return sep.join(part if isinstance(part, Text) else Text(part) for part in parts)


def styled(template: str, /, *args: object, **kwargs: object) -> Text:
    """Render conductor's own markup with values inserted as literal text.

    ``template`` is a conductor-authored literal, so its ``[tag]`` markup is
    parsed as styling. The interpolated values are not: they are inserted
    verbatim, so a value containing ``[/etc/x]`` can neither raise
    ``MarkupError`` nor be silently deleted.

    Field syntax is ``str.format``'s, so positional, auto-numbered and named
    fields, conversions (``!r``, ``!s``, ``!a``) and format specs all work,
    and ``{{``/``}}`` are literal braces.

    A value that is already a ``Text`` is spliced in with its own styling
    intact, so pre-styled fragments compose::

        CHECK = styled("[green]✓[/green]")
        styled("{} {}", CHECK, provider_name)

    A format spec is not applied to a ``Text`` value — there is nothing
    meaningful to round or align.

    Examples::

        styled("[bold red]Error:[/bold red] {} not found", path)
        styled("{name} took {secs:.2f}s", name=agent.name, secs=elapsed)
        styled("[green]Validation Successful[/green]")

    Args:
        template: Conductor-authored text, optionally containing rich markup
            and ``str.format`` fields.
        *args: Positional values for the template's fields.
        **kwargs: Named values for the template's fields.

    Returns:
        A ``Text`` carrying the template's styling, safe to print on any
        console and to pass as a ``Panel`` title or a prompt.

    Raises:
        ValueError: If the template contains a NUL character, which the
            placeholder machinery reserves.
        rich.errors.MarkupError: If the *template* is malformed. That is a
            conductor bug rather than bad input — values never reach the
            parser.
        IndexError: If the template references a missing positional field.
        KeyError: If the template references a missing named field.
    """
    marked: list[str] = []
    values: list[str | Text] = []
    auto_index = 0

    if _FILLER in template:
        # The substitution below locates each value by searching for its
        # filler run, so a filler character in the template's own literal
        # text would capture the search and shift every later value. Templates
        # are conductor-authored literals, so this is a bug in the caller.
        raise ValueError("styled() template must not contain a NUL character")

    for literal, field, spec, conversion in _FORMATTER.parse(template):
        marked.append(literal)
        if field is None:
            continue

        if field == "":
            value = args[auto_index]
            auto_index += 1
        elif field.isdigit():
            value = args[int(field)]
        else:
            value = kwargs[field]

        if conversion:
            value = _FORMATTER.convert_field(value, conversion)

        rendered: str | Text = value if isinstance(value, Text) else format(value, spec or "")
        values.append(rendered)
        marked.append(_FILLER * len(rendered.plain if isinstance(rendered, Text) else rendered))

    text = Text.from_markup("".join(marked))
    if not values:
        return text

    # Locate each value by its filler run rather than by a precomputed offset:
    # a value may itself contain the filler character, and an empty value
    # leaves no filler to find. The template is guaranteed filler-free above,
    # and ``cursor`` advances past each substituted value, so a filler inside
    # a value cannot be mistaken for the next placeholder.
    plain = list(text.plain)
    spans = list(text.spans)
    cursor = 0
    for value in values:
        body = value.plain if isinstance(value, Text) else value
        if not body:
            continue
        start = plain.index(_FILLER, cursor)
        plain[start : start + len(body)] = list(body)
        if isinstance(value, Text):
            # Re-anchor the fragment's own styling at its position in the
            # result. Its base ``style`` covers the whole fragment and is
            # added first, so a narrower span of its own still wins.
            if value.style:
                spans.append(Span(start, start + len(body), value.style))
            spans.extend(
                Span(start + span.start, start + span.end, span.style) for span in value.spans
            )
        cursor = start + len(body)

    return Text(
        "".join(plain),
        style=text.style,
        justify=text.justify,
        overflow=text.overflow,
        no_wrap=text.no_wrap,
        end=text.end,
        tab_size=text.tab_size,
        spans=spans,
    )
