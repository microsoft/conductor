# Console output

The markup-safety rules for everything Conductor prints through Rich, Textual or Typer help.
Runtime values (workflow, agent and plugin names, paths, exception text, agent output) MUST
never be parsed as markup. These rules are enforced statically by
`tests/test_cli/test_markup_guards.py` and behaviorally by
`tests/test_cli/test_markup_injection.py`.

## When to read

- Adding or changing anything that prints: `console.print`/`log`, `Panel`, `Table`, `Rule`,
  `Prompt`/`Confirm`, progress or status output.
- Building a Textual widget, `notify()` message or `DataTable` row (the Fleet TUI).
- Writing Typer `help=`/`epilog=` text or a command docstring.
- Seeing a guard failure from `tests/test_cli/test_markup_guards.py`.

## Required invariants

### Why plain strings are unsafe

- Rich parses `[...]` in a plain `str` as a style tag when the token's first character is a
  lowercase letter, `#`, `/` or `@`. That splits three ways: `[0]` renders literally, `[task1]`
  is silently deleted, and `[/etc/x]` raises `MarkupError` out of the print call. Passing
  `style=` does not disable parsing.
- The rules below make a forgotten wrapper show up as a visible literal `[green]` (which rule D
  catches) instead of a crash or a silent deletion.

### The rules (letters match `tests/test_cli/test_markup_guards.py`)

- **A. Build every console with `src/conductor/console.py::make_console`.** It returns a
  `MarkupFreeConsole` locked to `markup=False`, so plain strings print literally in `print`,
  `Panel` bodies, `Table` cells, headers, titles, captions and `Rule` titles. A truthy `markup`
  passed to the constructor, `print` or `log` raises `TypeError`; never try to re-enable it.
  Subclass `MarkupFreeConsole`, never rich's `Console`, so the refusal is inherited (see
  `src/conductor/cli/run.py::_SilentAwareConsole`). The only alternative the guard accepts is a
  `Console` constructed with explicit `markup=False`.
- **B. `Panel(title=)`, `Panel(subtitle=)` and `Prompt`/`Confirm`/`IntPrompt`/`FloatPrompt`
  prompts take a `Text`, never an interpolated string.** Rich calls `Text.from_markup` on those
  unconditionally, so the console setting never reaches them.
- **C. Never pass an f-string (or other interpolated value) to `Text.from_markup`.** Use it only
  for literal markup with nothing to interpolate.
- **D. No bare string literal containing markup at a print or cell sink.** Under rule A it would
  print its tags verbatim; wrap it in `styled(...)` or `Text.from_markup(...)`.
- **E. Never hand a `Text` to the builtin `print`.** It prints the plain form, dropping styling
  and any text rich already consumed as a tag.
- **F. Never put a `Text` into an f-string or through `str()`.** Same defect as E in general
  form (the guard checks f-strings; `str()` and `str.format` flatten just the same). Compose
  with `styled("{}{}", a, b)` or `join(...)` instead.
- **G. Escape brackets in Typer `help=`, `epilog=`, `short_help=` and `rich_help_panel=` text
  as `\[ ... ]`.** Typer renders help through its own rich console, outside rule A. Typer takes
  a `str`, so escaping (not `Text`) is the remedy here; see the `name\[@registry]\[@version]`
  help in `src/conductor/cli/app.py`.
- **H. Never use `rich.markup.escape`.** It is not byte-exact: the parser treats a backslash
  before a bracket as an escape, so a value like the regex `\[0-9\]+` renders as `[0-9\]+`
  whether or not it was escaped. A `Text` bypasses the parser and is exact.
- **I. Textual sinks take a `Text` or `markup=False` for dynamic content.** `Static`/`Label`
  content, `App.notify` messages and every `str` cell of a `DataTable` are parsed with
  `markup=True` by Textual itself, out of reach of rule A.
- **J. Escape brackets in a Typer command's docstring.** Typer uses the docstring as help when
  `help=` is absent, so rule G's keyword check never sees it.

### Helpers in `src/conductor/console.py`

- `styled(template, *args, **kwargs)` parses only the template (Conductor's own literal) and
  inserts values verbatim, using `str.format` field syntax. A value that is already a `Text`
  keeps its styling, so pre-styled fragments compose. A format spec or conversion on a `Text`
  value, a field inside a markup tag, or a NUL in the template raises rather than silently
  flattening. It returns a `Text`, so it is valid for rule B sinks.
- `join(separator, parts)` joins a mix of `str` and `Text` into one `Text`, treating strings as
  literal. Use it instead of `Text.join`, which requires every part to be a `Text` already.
- `Text.from_markup("[bold]literal[/bold]")` is fine when there is nothing to interpolate.
- `MarkupFreeConsole` still applies rich's highlighter to top-level `Text` arguments, so moving
  from markup strings to `styled(...)` does not lose number/path highlighting.
- `select_console_glyph(console, unicode, ascii)` picks one of Conductor's own decorative glyphs
  for consoles whose encoding cannot represent it (legacy Windows code pages, #401, #505).

### Safe and unsafe patterns

```python
from rich.panel import Panel
from rich.text import Text

from conductor.console import join, make_console, styled

console = make_console()

# Unsafe: a name like "[task1]" is deleted, "[/etc/x]" raises.
#   console.print(f"[bold]{name}[/bold] finished")
#   Panel(body, title=f"Workflow {name}")
#   print(styled("{}", name))            # builtin print drops styling (rule E)
#   f"{styled('[red]x[/red]')} failed"    # Text in an f-string (rule F)
#   rich.markup.escape(value)             # not byte-exact (rule H)

# Safe:
console.print(styled("[bold]{}[/bold] finished", name))
console.print(Panel(body, title=styled("Workflow {}", name)))
console.print(join(", ", [styled("[green]{}[/green]", ok), plain_value]))
console.print(Text.from_markup("[dim]No runs found[/dim]"))
console.print(path)  # plain string: literal under make_console()
```

## Change checklist

- [ ] Every new console comes from `make_console()` or subclasses `MarkupFreeConsole`.
- [ ] Every interpolated runtime value reaches the screen through `styled`, `join` or a `Text`,
      including `Panel` titles, prompts and Textual widgets.
- [ ] No `Text` flows into `str()`, an f-string or the builtin `print`.
- [ ] Typer help text and command docstrings escape their brackets.
- [ ] A new print helper that forwards to a console is added to `HELPER_SINKS` in
      `tests/test_cli/test_markup_guards.py` so rule D can see it.
- [ ] A behavioral case with a bracketed value (`[task1]`, `[/etc/x]`) is added to
      `tests/test_cli/test_markup_injection.py` for new commands that print runtime data.
- [ ] A new guard rule gets a shared `_violates_*` predicate, a negative control and a
      covers-real-source check (see [testing.md](testing.md)).

## Rationale and evidence

- Agent text containing a closing-tag-shaped token killed a long-running workflow and then
  every resume at the same offset, because the text was checkpointed and replayed (#382,
  `tests/test_cli/test_markup_safety.py`).
- #387 fixed the panel body in `src/conductor/cli/run.py` and left the `title=` f-string one
  line away; two commands written the same week reintroduced the pattern in files the fix never
  touched. That is why the convention is enforced by reading the source (#406) and not by
  review.
- Rule F (Text into a plain-string context) shipped four separate times during #406, twice
  destroying data rather than colour.
- Rule G/J: an unescaped `[@registry][@version]` removed that syntax from `conductor run --help`
  entirely, and an unescaped docstring dropped the `[fleet.retention]` section name from
  `conductor fleet prune --help`.
- Rule I: the Fleet TUI kill-confirmation dialog parsed the workflow names it listed, and a
  run-detail table kept parsing agent names after that was fixed.
- `markup=` is refused rather than overridable because rich's per-call `markup=True` would
  reopen the defect from a single line, and it is the obvious-looking "fix" for a visible
  `[green]`.

## Related references

- [Testing](testing.md) (negative controls for source-scanning tests)
- [Architecture](architecture.md) (why `console.py` is a top-level leaf module)
- [Fleet TUI](fleet-tui.md) (Textual sinks)
- [CLI and runs](cli-and-runs.md)
