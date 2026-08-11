"""Static guards that keep new call sites markup-safe (issue #406).

The behavioural tests next door prove the *current* call sites are correct.
They cannot prove the next one will be, and that is the actual defect here:
#382 was fixed in one file, #387 fixed ``cli/run.py`` thoroughly and still
left ``Panel(title=)`` in the function it changed, and two commands written
that same week — ``conductor status`` (#389) and ``conductor plugin list``
(#398) — were written against the unfixed pattern in files the fix never
touched.

So the convention is enforced by reading the source. Four rules, each closing
a hole the previous one leaves open:

A. Every ``Console`` is built by ``make_console`` (or explicitly passes
   ``markup=False``). This is what makes a plain string literal by default.
B. ``Panel(title=/subtitle=)`` and ``Prompt``/``Confirm``/``IntPrompt``
   prompts are handed a ``Text``, never an interpolated string. Rich parses
   those with ``Text.from_markup`` unconditionally, so rule A never reaches
   them.
C. ``Text.from_markup`` is never given an f-string, because the interpolated
   value would be parsed.
D. No bare string literal containing markup reaches a print/cell sink. Under
   rule A such a literal renders its tags as visible text, so this rule is
   what makes the conversion verifiable rather than eyeballed.

A failure names file and line, and the fix is always one of: wrap in
``styled(...)``, wrap in ``Text.from_markup(...)`` when there is nothing to
interpolate, or build the console with ``make_console``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "conductor"

# rich treats a bracketed token as a tag when its first character is a
# lowercase letter, '#', '/' or '@' (rich.markup.RE_TAGS).
MARKUP_RE = re.compile(r"\[[a-z#/@][^\[]*?\]")

PRINT_SINKS = {"print", "log", "rule"}
# Local fan-out helpers that forward to a console. Named explicitly because a
# rule keyed on the callee name cannot otherwise tell them from any other
# function; add new ones here.
HELPER_SINKS = {"_print", "verbose_log"}
CELL_SINKS = {"add_row", "add_column"}
PROMPT_CLASSES = {"Prompt", "Confirm", "IntPrompt", "FloatPrompt"}

# Calls whose result is already a ``Text`` and so never reaches the parser.
SAFE_WRAPPERS = {"styled", "join", "Text", "from_markup", "assemble", "make_console"}

# ``typer`` renders its own help text through its own console, so these are
# outside this convention entirely.
TYPER_TEXT_KWARGS = {"help", "epilog", "rich_help_panel", "short_help"}


def _python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: Path) -> str:
    return str(path.relative_to(SRC.parent.parent))


def _callee(node: ast.Call) -> str | None:
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return None


def _is_safe_wrapper(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _callee(node) in SAFE_WRAPPERS


def _annotation_is_text(node: ast.AST | None) -> bool:
    """Is this annotation ``Text`` or a union containing only ``Text``/``None``?"""
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id == "Text"
    if isinstance(node, ast.Attribute):
        return node.attr == "Text"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return any(_annotation_is_text(side) for side in (node.left, node.right))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.replace(" ", "").split("|")[0] == "Text"
    return False


def _safe_text_names(scope: ast.AST) -> set[str]:
    """Names in *scope* that provably hold a ``Text``.

    A purely syntactic rule sees a bare ``Name`` as opaque and flags it, which
    would push a real call site onto an allowlist — and an allowlist is what
    lets the next one in. Instead, resolve the two forms that actually occur:
    a parameter annotated ``Text``, and a local assigned from a safe wrapper
    (including a conditional between them). Anything else stays flagged.
    """
    safe: set[str] = set()

    for node in ast.walk(scope):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                if _annotation_is_text(arg.annotation):
                    safe.add(arg.arg)

    def _value_is_safe(value: ast.AST) -> bool:
        if _is_safe_wrapper(value):
            return True
        if isinstance(value, ast.IfExp):
            return _value_is_safe(value.body) and _value_is_safe(value.orelse)
        if isinstance(value, ast.Name):
            return value.id in safe
        return False

    # Two passes: an assignment may reference a name defined by a later branch.
    for _ in range(2):
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign) and _value_is_safe(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        safe.add(target.id)
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and (
                    _annotation_is_text(node.annotation) or _value_is_safe(node.value or ast.Pass())
                )
            ):
                safe.add(node.target.id)
    return safe


def _is_dynamic(node: ast.AST, safe_names: frozenset[str] = frozenset()) -> bool:
    """Does this argument carry a runtime value that would reach the parser?"""
    if _is_safe_wrapper(node):
        return False
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in node.values)
    if isinstance(node, ast.Constant):
        return False
    if isinstance(node, ast.Name) and node.id in safe_names:
        return False
    if isinstance(node, ast.IfExp):
        return _is_dynamic(node.body, safe_names) or _is_dynamic(node.orelse, safe_names)
    return isinstance(node, ast.Name | ast.Attribute | ast.Call | ast.Subscript | ast.BinOp)


def _literal_markup(node: ast.AST) -> bool:
    """Is this a bare string literal carrying rich markup?"""
    if isinstance(node, ast.JoinedStr):
        return any(
            isinstance(v, ast.Constant) and isinstance(v.value, str) and MARKUP_RE.search(v.value)
            for v in node.values
        )
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(MARKUP_RE.search(node.value))
    return False


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                out.add(id(first.value))
    return out


def _report(violations: list[str], remedy: str) -> str:
    listing = "\n".join(f"  {v}" for v in violations)
    return f"{len(violations)} markup-unsafe call site(s):\n{listing}\n\n{remedy}"


class TestRuleAConsolesAreMarkupFree:
    """Every ``Console`` inverts the default, or none of the rest holds."""

    def test_no_bare_console_construction(self) -> None:
        violations: list[str] = []
        for path in _python_files():
            if path.name == "console.py" and path.parent == SRC:
                continue  # the factory itself
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or _callee(node) != "Console":
                    continue
                explicit = any(
                    kw.arg == "markup"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is False
                    for kw in node.keywords
                )
                if not explicit:
                    violations.append(f"{_rel(path)}:{node.lineno}: Console(...)")
        assert not violations, _report(
            violations,
            "Build consoles with conductor.console.make_console() so an "
            "interpolated runtime value is never parsed as styling.",
        )

    def test_console_subclasses_lock_markup_off(self) -> None:
        """A subclass bypasses ``make_console`` and must lock it itself."""
        from conductor.cli.run import _SilentAwareConsole

        console = _SilentAwareConsole(markup=True)
        with console.capture() as captured:
            console.print("keep [dim] this")
        # ``markup=True`` must be ignored, not honoured.
        assert "[dim]" in captured.get()


class TestRuleBBypassSinksReceiveText:
    """``Panel`` titles and prompts parse markup whatever the console says."""

    def test_panel_titles_are_not_interpolated_strings(self) -> None:
        violations: list[str] = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            safe = frozenset(_safe_text_names(tree))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or _callee(node) != "Panel":
                    continue
                for kw in node.keywords:
                    if kw.arg in {"title", "subtitle"} and _is_dynamic(kw.value, safe):
                        violations.append(f"{_rel(path)}:{node.lineno}: Panel({kw.arg}=...)")
        assert not violations, _report(
            violations,
            "rich calls Text.from_markup on a Panel title regardless of the "
            "console's markup=False, so pass styled(...) or Text(...).",
        )

    def test_prompts_are_not_interpolated_strings(self) -> None:
        violations: list[str] = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            safe = frozenset(_safe_text_names(tree))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                is_ask = (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "ask"
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id in PROMPT_CLASSES
                )
                if not is_ask or not node.args:
                    continue
                if _is_dynamic(node.args[0], safe):
                    violations.append(f"{_rel(path)}:{node.lineno}: {fn.value.id}.ask(...)")
        assert not violations, _report(
            violations,
            "rich calls Text.from_markup on a prompt regardless of the "
            "console's markup=False, so pass styled(...) or Text(...).",
        )


class TestRuleCFromMarkupTakesOnlyLiterals:
    """``Text.from_markup`` always parses, so it must never see a value."""

    def test_from_markup_is_never_given_an_f_string(self) -> None:
        violations: list[str] = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            safe = frozenset(_safe_text_names(tree))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or _callee(node) != "from_markup":
                    continue
                if node.args and _is_dynamic(node.args[0], safe):
                    violations.append(f"{_rel(path)}:{node.lineno}: Text.from_markup(...)")
        assert not violations, _report(
            violations,
            "Use styled('<template>', value) — it parses the template but "
            "inserts the value as literal text.",
        )


class TestRuleDNoUnwrappedMarkupAtASink:
    """Under rule A a markup literal at a sink prints its tags verbatim.

    This is the rule that makes the conversion checkable: it fails for every
    site that was missed, rather than leaving it to be noticed in output.
    """

    def test_no_markup_string_literal_reaches_a_sink(self) -> None:
        violations: list[str] = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = _docstring_ids(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _callee(node)
                if name not in PRINT_SINKS | HELPER_SINKS | CELL_SINKS:
                    continue
                # The *builtin* ``print`` is rule E's job: a markup literal
                # there is correct, because nothing parses it. A bare-Name
                # call to anything else (``_print``, a console fan-out
                # helper) still routes to a console and stays covered here.
                if isinstance(node.func, ast.Name) and node.func.id == "print":
                    continue
                args: list[ast.AST] = list(node.args)
                args += [
                    kw.value for kw in node.keywords if kw.arg not in {"style", *TYPER_TEXT_KWARGS}
                ]
                for arg in args:
                    if id(arg) in docstrings:
                        continue
                    if _literal_markup(arg):
                        violations.append(f"{_rel(path)}:{node.lineno}: {name}(...)")
        assert not violations, _report(
            violations,
            "Wrap in styled('<template>', ...) — or Text.from_markup('...') "
            "when there is nothing to interpolate.",
        )


class TestRuleENoTextThroughBuiltinPrint:
    """A Rich ``Text`` handed to the *builtin* ``print`` is a silent data loss.

    ``print`` renders it via ``str(Text)``, which is its plain form: any
    styling is discarded, and — worse — any text rich already consumed as a
    tag is simply gone. That is not hypothetical. Converting call sites for
    this issue wrapped a stderr log label in ``Text.from_markup``, and because
    ``[workspace-instructions]`` starts with a lowercase letter rich read it
    as a style tag and deleted it, leaving ``" 0 files discovered from CWD."``.
    The existing test passed, because it asserted on a substring after the
    prefix.

    Rules A–D all cleared that site: the callee is named ``print`` and the
    argument is a ``SAFE_WRAPPERS`` call, which is exactly right at a Rich
    console and exactly wrong here.
    """

    def test_builtin_print_is_not_given_a_text(self) -> None:
        violations: list[str] = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # A bare Name callee is the builtin; ``console.print`` is an
                # Attribute and is governed by rules A and D instead.
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id != "print":
                    continue
                for arg in node.args:
                    if isinstance(arg, ast.Call) and _callee(arg) in SAFE_WRAPPERS:
                        violations.append(f"{_rel(path)}:{node.lineno}: print({_callee(arg)}(...))")
        assert not violations, _report(
            violations,
            "The builtin print() renders a Text as its plain form, dropping "
            "styling and any text rich parsed as a tag. Pass a plain string, "
            "or print through a console built by make_console().",
        )


class TestTheGuardsActuallyDetectViolations:
    """Negative controls.

    A static check that silently matches nothing is worse than no check: it
    reports "all clear" forever. Each rule is run against a snippet that
    violates it, and one that does not.
    """

    @staticmethod
    def _violations_in(source: str, rule: str) -> list[str]:
        tree = ast.parse(source)
        docstrings = _docstring_ids(tree)
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _callee(node)
            if rule == "A" and name == "Console":
                if not any(
                    kw.arg == "markup"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is False
                    for kw in node.keywords
                ):
                    found.append(f"line {node.lineno}")
            elif rule == "B" and name == "Panel":
                for kw in node.keywords:
                    if kw.arg in {"title", "subtitle"} and _is_dynamic(kw.value):
                        found.append(f"line {node.lineno}")
            elif rule == "C" and name == "from_markup":
                if node.args and _is_dynamic(node.args[0]):
                    found.append(f"line {node.lineno}")
            elif rule == "D" and name in PRINT_SINKS | HELPER_SINKS | CELL_SINKS:
                if isinstance(node.func, ast.Name) and node.func.id == "print":
                    continue
                for arg in list(node.args) + [k.value for k in node.keywords if k.arg != "style"]:
                    if id(arg) not in docstrings and _literal_markup(arg):
                        found.append(f"line {node.lineno}")
            elif rule == "E" and isinstance(node.func, ast.Name) and node.func.id == "print":
                for arg in node.args:
                    if isinstance(arg, ast.Call) and _callee(arg) in SAFE_WRAPPERS:
                        found.append(f"line {node.lineno}")
        return found

    def test_rule_a_flags_a_bare_console(self) -> None:
        assert self._violations_in("c = Console(stderr=True)", "A")

    def test_rule_a_accepts_an_explicit_opt_out(self) -> None:
        assert not self._violations_in("c = Console(stderr=True, markup=False)", "A")

    def test_rule_b_flags_an_interpolated_title(self) -> None:
        assert self._violations_in('Panel(body, title=f"[cyan]{name}[/cyan]")', "B")

    def test_rule_b_flags_a_bare_name_title(self) -> None:
        assert self._violations_in("Panel(body, title=agent_name)", "B")

    def test_rule_b_accepts_styled(self) -> None:
        assert not self._violations_in('Panel(body, title=styled("[cyan]{}[/cyan]", name))', "B")

    def test_rule_b_accepts_a_constant_title(self) -> None:
        """Conductor's own literal title is parsed as intended."""
        assert not self._violations_in('Panel(body, title="[cyan]Plan[/cyan]")', "B")

    def test_rule_c_flags_an_f_string(self) -> None:
        assert self._violations_in('Text.from_markup(f"[b]{x}[/b]")', "C")

    def test_rule_c_accepts_a_literal(self) -> None:
        assert not self._violations_in('Text.from_markup("[b]hi[/b]")', "C")

    @pytest.mark.parametrize(
        "snippet",
        [
            'console.print("[red]boom[/red]")',
            'console.print(f"[red]{err}[/red]")',
            'table.add_row("[dim]x[/dim]")',
            'console.log("[green]ok[/green]")',
        ],
    )
    def test_rule_d_flags_unwrapped_markup(self, snippet: str) -> None:
        assert self._violations_in(snippet, "D")

    @pytest.mark.parametrize(
        "snippet",
        [
            'console.print(styled("[red]{}[/red]", err))',
            'console.print(Text.from_markup("[red]boom[/red]"))',
            'console.print(f"plain {value}")',
            "table.add_row(name, str(count))",
            'console.print("[0] not a tag")',
            # The builtin print does not parse markup; rule E governs it.
            'print("[workspace-instructions] 0 files", file=sys.stderr)',
        ],
    )
    def test_rule_d_accepts_safe_sites(self, snippet: str) -> None:
        assert not self._violations_in(snippet, "D")

    def test_rule_d_still_covers_a_console_fanout_helper(self) -> None:
        """``_print`` is a bare Name too, but it routes to a console."""
        assert self._violations_in('_print("[bold cyan]Token Usage Summary[/bold cyan]")', "D")

    @pytest.mark.parametrize(
        "snippet",
        [
            'print(Text.from_markup("[b]x[/b]"), file=sys.stderr)',
            'print(styled("[b]{}[/b]", x))',
            'print(Text("plain"))',
        ],
    )
    def test_rule_e_flags_text_through_builtin_print(self, snippet: str) -> None:
        assert self._violations_in(snippet, "E")

    @pytest.mark.parametrize(
        "snippet",
        [
            'print("[workspace-instructions] 0 files", file=sys.stderr)',
            'print(f"  {path}", file=sys.stderr)',
            'console.print(styled("[b]{}[/b]", x))',
        ],
    )
    def test_rule_e_accepts_safe_sites(self, snippet: str) -> None:
        assert not self._violations_in(snippet, "E")


class TestSafeNameResolution:
    """``_safe_text_names`` must resolve narrowly, or rule B goes blind.

    It exists so a real ``Text``-typed local is not pushed onto an allowlist.
    If it over-accepts, every bare name becomes invisible to rule B and the
    guard silently stops working.
    """

    def test_text_annotated_parameter_is_safe(self) -> None:
        src = "def f(prompt: Text) -> None:\n    Prompt.ask(prompt)\n"
        assert "prompt" in _safe_text_names(ast.parse(src))

    def test_optional_text_parameter_is_safe(self) -> None:
        src = "def f(prompt: Text | None = None) -> None:\n    Prompt.ask(prompt)\n"
        assert "prompt" in _safe_text_names(ast.parse(src))

    def test_local_assigned_from_styled_is_safe(self) -> None:
        src = 'def f():\n    p = styled("[b]x[/b]")\n    Prompt.ask(p)\n'
        assert "p" in _safe_text_names(ast.parse(src))

    def test_conditional_between_safe_values_is_safe(self) -> None:
        src = (
            "def f(prompt: Text | None = None):\n"
            '    p = styled("[b]x[/b]") if prompt is None else prompt\n'
            "    Prompt.ask(p)\n"
        )
        assert "p" in _safe_text_names(ast.parse(src))

    def test_str_annotated_parameter_is_not_safe(self) -> None:
        src = "def f(prompt: str) -> None:\n    Prompt.ask(prompt)\n"
        assert "prompt" not in _safe_text_names(ast.parse(src))

    def test_unannotated_parameter_is_not_safe(self) -> None:
        src = "def f(prompt):\n    Prompt.ask(prompt)\n"
        assert "prompt" not in _safe_text_names(ast.parse(src))

    def test_local_assigned_from_an_f_string_is_not_safe(self) -> None:
        src = 'def f(x):\n    p = f"[b]{x}[/b]"\n    Prompt.ask(p)\n'
        assert "p" not in _safe_text_names(ast.parse(src))

    def test_conditional_with_one_unsafe_branch_is_not_safe(self) -> None:
        src = 'def f(x, flag):\n    p = styled("[b]y[/b]") if flag else x\n    Prompt.ask(p)\n'
        assert "p" not in _safe_text_names(ast.parse(src))

    def test_rule_b_still_flags_an_unresolvable_name(self) -> None:
        """The end-to-end consequence of the two cases above."""
        src = "def f(prompt: str) -> None:\n    Panel(body, title=prompt)\n"
        tree = ast.parse(src)
        safe = frozenset(_safe_text_names(tree))
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and _callee(n) == "Panel")
        title = next(kw.value for kw in call.keywords if kw.arg == "title")
        assert _is_dynamic(title, safe)


class TestTheGuardsCoverRealSource:
    """Guard the guards: a path typo would make every rule vacuous."""

    def test_source_tree_is_found(self) -> None:
        files = _python_files()
        assert len(files) > 50, f"only found {len(files)} files under {SRC}"
        assert any(p.name == "run.py" for p in files)

    def test_sinks_are_actually_present_in_the_source(self) -> None:
        """If nothing matches the sink names, rule D can never fail."""
        seen = 0
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and _callee(node) in PRINT_SINKS | HELPER_SINKS | CELL_SINKS
                ):
                    seen += 1
        assert seen > 100, f"only {seen} sink calls found; the scan is not reaching the source"

    def test_panel_titles_are_actually_present(self) -> None:
        """Likewise for rule B, whose match set is much smaller."""
        seen = 0
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _callee(node) == "Panel":
                    seen += sum(1 for kw in node.keywords if kw.arg in {"title", "subtitle"})
        assert seen > 5, f"only {seen} Panel titles found; the scan is not reaching the source"
