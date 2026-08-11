"""Static guards that keep new call sites markup-safe (issue #406).

The behavioural tests next door prove the *current* call sites are correct.
They cannot prove the next one will be, and that is the actual defect here:
#382 was fixed in one file, #387 fixed ``cli/run.py`` thoroughly and still
left ``Panel(title=)`` in the function it changed, and two commands written
that same week — ``conductor status`` (#389) and ``conductor plugin list``
(#398) — were written against the unfixed pattern in files the fix never
touched.

So the convention is enforced by reading the source. Eight rules, each closing
a hole the previous one leaves open:

A. Every ``Console`` is built by ``make_console`` (or explicitly passes
   ``markup=False``), and every ``Console`` subclass derives from
   ``MarkupFreeConsole`` so it inherits the refusal. This is what makes a
   plain string literal by default.
B. ``Panel(title=/subtitle=)`` and ``Prompt``/``Confirm``/``IntPrompt``
   prompts are handed a ``Text``, never an interpolated string. Rich parses
   those with ``Text.from_markup`` unconditionally, so rule A never reaches
   them.
C. ``Text.from_markup`` is never given an f-string, because the interpolated
   value would be parsed.
D. No bare string literal containing markup reaches a print/cell sink. Under
   rule A such a literal renders its tags as visible text, so this rule is
   what makes the conversion verifiable rather than eyeballed.
E. No ``Text`` is handed to the *builtin* ``print``, which renders its plain
   form -- dropping styling, and dropping outright any text rich already
   parsed as a tag.
F. No ``Text`` is interpolated into an f-string, for the same reason. This is
   the general form of rule E and the defect with the worst record here: it
   shipped four separate times, twice destroying data rather than styling.
G. ``typer`` ``help=``/``epilog=`` text escapes its brackets. Typer renders
   help through its *own* rich console, so the console convention does not
   reach it -- which is how ``[@registry][@version]`` went missing from
   ``conductor run --help`` entirely.
H. ``rich.markup.escape`` is never used. It is not byte-exact, so it cannot
   round-trip a value that already contains a backslash before a bracket.

A failure names file and line, and the fix is always one of: wrap in
``styled(...)``, wrap in ``Text.from_markup(...)`` when there is nothing to
interpolate, build the console with ``make_console``, or escape the bracket.

Each rule is a ``_violates_*`` predicate called by both the source scan and
its negative control, so a control cannot pass while the rule it claims to
control has drifted -- which had already happened once.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterator
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
# Split by callee shape on purpose: ``_callee`` returns the bare attribute
# name, so a flat name set would match ``", ".join(names)`` -- a plain ``str``
# built from runtime data -- and wave it through rules B and C.
_SAFE_BARE_CALLS = {"styled", "join", "Text"}
_SAFE_TEXT_METHODS = {"from_markup", "assemble"}
SAFE_WRAPPERS = _SAFE_BARE_CALLS | _SAFE_TEXT_METHODS

# ``typer`` renders help through rich (``rich_markup_mode="rich"``), so these
# strings *are* markup-parsed -- just not by a conductor console. They are
# excluded from rule D (which is about conductor's sinks) and covered by rule
# G instead. Excluding them without rule G is what let ``[@registry]`` go
# missing from ``conductor run --help``.
TYPER_TEXT_KWARGS = {"help", "epilog", "rich_help_panel", "short_help"}
TYPER_CALLEES = {"Option", "Argument", "Typer", "command", "callback"}


def _python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _is_factory(path: Path) -> bool:
    """Is this the module that *implements* the safe primitives?

    ``console.py`` builds the very things the rules look for -- it constructs
    the markup-free console and calls ``Text.from_markup`` on an assembled
    template -- so scanning it reports the implementation as a violation of
    itself.
    """
    return path.name == "console.py" and path.parent == SRC


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
    """Does this call provably produce a ``Text``?

    Resolved by receiver rather than by bare name: ``Text.from_markup(...)``
    is safe, ``", ".join(...)`` is not, and both end in an attribute called
    ``join``/``from_markup``.
    """
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id in _SAFE_BARE_CALLS
    return (
        isinstance(fn, ast.Attribute)
        and fn.attr in _SAFE_TEXT_METHODS
        and isinstance(fn.value, ast.Name)
        and fn.value.id == "Text"
    )


def _annotation_is_text(node: ast.AST | None) -> bool:
    """Is this annotation ``Text``, or a union of only ``Text`` and ``None``?

    A union containing ``str`` is deliberately *not* safe. ``verbose_log``
    takes ``str | Text``, and accepting that would mark the name ``message``
    safe for the whole module, blinding rule B on every other function that
    happens to use the same parameter name.
    """
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id == "Text"
    if isinstance(node, ast.Attribute):
        return node.attr == "Text"
    if isinstance(node, ast.Constant):
        if node.value is None:
            return True
        if isinstance(node.value, str):
            parts = [p.strip() for p in node.value.split("|")]
            return bool(parts) and all(p in {"Text", "None"} for p in parts)
        return False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return all(_annotation_is_text(side) for side in (node.left, node.right))
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

    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        args = scope.args
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
        for node in _scope_nodes(scope):
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


def _maybe_text_names(scope: ast.AST) -> set[str]:
    """Names in *scope* that *may* hold a ``Text``.

    Deliberately a different predicate from :func:`_safe_text_names`. That one
    answers "is this definitely a ``Text``?", which rules B and C need before
    they can *clear* a name. Rule F needs the opposite: it must *flag* a name
    that could be a ``Text``, so a conditional with one ``Text`` branch counts
    even though it is not definitely a ``Text``.

    Concretely, the loop-target marker that shipped this bug was
    ``Text.from_markup(...) if step.is_loop_target else ""`` -- not "definitely
    Text", and therefore invisible to the stricter predicate.
    """
    names: set[str] = set()

    def _value_may_be_text(value: ast.AST) -> bool:
        if _is_safe_wrapper(value):
            return True
        if isinstance(value, ast.IfExp):
            return _value_may_be_text(value.body) or _value_may_be_text(value.orelse)
        if isinstance(value, ast.Name):
            return value.id in names
        return False

    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        args = scope.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if arg.annotation is not None and _annotation_mentions_text(arg.annotation):
                names.add(arg.arg)

    for _ in range(2):
        for node in _scope_nodes(scope):
            if isinstance(node, ast.Assign) and _value_may_be_text(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            # Mirrors the ``AnnAssign`` arm in ``_safe_text_names``; without it
            # ``m: Text = styled(...)`` is invisible to rule F.
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and (
                    _annotation_mentions_text(node.annotation)
                    or (node.value is not None and _value_may_be_text(node.value))
                )
            ):
                names.add(node.target.id)
    return names


def _annotation_mentions_text(node: ast.AST) -> bool:
    """Does this annotation mention ``Text`` anywhere (including a union)?"""
    if isinstance(node, ast.Name):
        return node.id == "Text"
    if isinstance(node, ast.Attribute):
        return node.attr == "Text"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return any(_annotation_mentions_text(s) for s in (node.left, node.right))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return "Text" in node.value
    return False


def _scope_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Walk *scope* without descending into a nested function or class.

    ``ast.walk`` would pull a nested function's parameters into its parent's
    name set, which is the cross-function leak this scoping exists to avoid.
    """
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _scope_names(tree: ast.Module, resolver: Callable[[ast.AST], set[str]]) -> dict[int, set[str]]:
    """Map every node to the names *resolver* finds in its enclosing function.

    Resolving names per module would let one function's annotation clear a
    same-named local everywhere else in the file. That is not hypothetical:
    ``verbose_log(message: str | Text)`` would otherwise mark ``message`` as
    Text-bearing for the whole of ``cli/run.py``, where several unrelated
    functions take a ``message: str``.
    """
    scopes: dict[int, set[str]] = {}

    def _descend(scope: ast.AST, inherited: set[str]) -> None:
        # A nested function closes over its enclosing scope, so names
        # accumulate inward. ``_get_user_input`` binds ``prompt`` and its inner
        # ``_ask`` is what actually calls ``Prompt.ask(prompt)``.
        names = inherited | resolver(scope)
        for node in ast.walk(scope):
            scopes[id(node)] = names
        for node in ast.iter_child_nodes(scope):
            _walk_for_functions(node, names)

    def _walk_for_functions(node: ast.AST, inherited: set[str]) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            _descend(node, inherited)
            return
        for child in ast.iter_child_nodes(node):
            _walk_for_functions(child, inherited)

    _descend(tree, set())
    return scopes


def _names_for(scopes: dict[int, set[str]], node: ast.AST) -> frozenset[str]:
    return frozenset(scopes.get(id(node), ()))


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


# --------------------------------------------------------------------------
# Rule predicates.
#
# Each returns the violating labels for one node. Both the source scan and its
# negative control call these, so a control cannot pass while the rule it
# claims to control has drifted -- which had already happened once, when rule
# D grew a ``TYPER_TEXT_KWARGS`` exemption its control never learned about.
# --------------------------------------------------------------------------


def _violates_a(node: ast.AST) -> list[str]:
    if not isinstance(node, ast.Call) or _callee(node) != "Console":
        return []
    explicit = any(
        kw.arg == "markup" and isinstance(kw.value, ast.Constant) and kw.value.value is False
        for kw in node.keywords
    )
    return [] if explicit else ["Console(...)"]


def _violates_b_panel(node: ast.AST, safe: frozenset[str]) -> list[str]:
    if not isinstance(node, ast.Call) or _callee(node) != "Panel":
        return []
    return [
        f"Panel({kw.arg}=...)"
        for kw in node.keywords
        if kw.arg in {"title", "subtitle"} and _is_dynamic(kw.value, safe)
    ]


def _violates_b_prompt(node: ast.AST, safe: frozenset[str]) -> list[str]:
    if not isinstance(node, ast.Call):
        return []
    fn = node.func
    is_ask = (
        isinstance(fn, ast.Attribute)
        and fn.attr == "ask"
        and isinstance(fn.value, ast.Name)
        and fn.value.id in PROMPT_CLASSES
    )
    if not is_ask or not node.args or not _is_dynamic(node.args[0], safe):
        return []
    return [f"{fn.value.id}.ask(...)"]  # type: ignore[union-attr]


def _violates_c(node: ast.AST, safe: frozenset[str]) -> list[str]:
    if not isinstance(node, ast.Call) or _callee(node) != "from_markup":
        return []
    if node.args and _is_dynamic(node.args[0], safe):
        return ["Text.from_markup(...)"]
    return []


def _violates_d(node: ast.AST, docstrings: set[int]) -> list[str]:
    if not isinstance(node, ast.Call):
        return []
    name = _callee(node)
    if name not in PRINT_SINKS | HELPER_SINKS | CELL_SINKS:
        return []
    # The *builtin* ``print`` is rule E's job: a markup literal there is
    # correct, because nothing parses it. A bare-Name call to anything else
    # (``_print``, a console fan-out helper) still routes to a console.
    if isinstance(node.func, ast.Name) and node.func.id == "print":
        return []
    args: list[ast.AST] = list(node.args)
    # No typer exemption here: rule D's sinks and rule G's typer callees are
    # disjoint sets, so exempting ``help=`` would only create a hole for a
    # conductor sink that happened to take a kwarg of that name.
    args += [kw.value for kw in node.keywords if kw.arg != "style"]
    return [f"{name}(...)" for arg in args if id(arg) not in docstrings and _literal_markup(arg)]


def _violates_e(node: ast.AST) -> list[str]:
    if not isinstance(node, ast.Call):
        return []
    if not isinstance(node.func, ast.Name) or node.func.id != "print":
        return []
    return [f"print({_callee(arg)}(...))" for arg in node.args if _is_safe_wrapper(arg)]


def _violates_f(node: ast.AST, maybe: frozenset[str]) -> list[str]:
    if not isinstance(node, ast.JoinedStr):
        return []
    out: list[str] = []
    for part in node.values:
        if not isinstance(part, ast.FormattedValue):
            continue
        # ``!r`` is a deliberate repr, not an accidental flatten.
        if part.conversion == ord("r"):
            continue
        value = part.value
        if _is_safe_wrapper(value) or (isinstance(value, ast.Name) and value.id in maybe):
            out.append("f-string")
    return out


def _violates_g(node: ast.AST) -> list[str]:
    if not isinstance(node, ast.Call) or _callee(node) not in TYPER_CALLEES:
        return []
    out: list[str] = []
    for kw in node.keywords:
        if kw.arg not in TYPER_TEXT_KWARGS:
            continue
        for part in _string_parts(kw.value):
            # An escaped bracket is fine; rich renders ``\\[`` literally.
            if MARKUP_RE.search(part.replace("\\[", "")):
                out.append(f"typer {kw.arg}=")
    return out


def _string_parts(node: ast.AST) -> list[str]:
    """Every literal string chunk in a constant or implicitly-joined value."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [
            v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
    return []


def _violates_a_subclass(node: ast.AST) -> list[str]:
    """A ``Console`` subclass bypasses ``make_console``, so it must not exist.

    Matches a dotted base (``rich.console.Console``) as well as a bare name,
    since an aliased import would otherwise slip past.
    """
    if not isinstance(node, ast.ClassDef):
        return []
    for base in node.bases:
        name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
        if name == "Console":
            return [f"class {node.name}(Console)"]
    return []


def _violates_h(node: ast.AST) -> list[str]:
    """``rich.markup.escape`` is banned: it is not byte-exact.

    The parser treats ``\\[`` as an escaped bracket, so ``\\[0-9\\]+`` renders as
    ``[0-9\\]+`` whether or not it was escaped first. Building a ``Text``
    avoids the parser entirely.
    """
    if (
        isinstance(node, ast.ImportFrom)
        and node.module == "rich.markup"
        and any(alias.name == "escape" for alias in node.names)
    ):
        return ["from rich.markup import escape"]
    if isinstance(node, ast.Call):
        fn = node.func
        if (
            isinstance(fn, ast.Attribute)
            and fn.attr == "escape"
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "markup"
        ):
            return ["markup.escape(...)"]
    return []


def _report(violations: list[str], remedy: str) -> str:
    listing = "\n".join(f"  {v}" for v in violations)
    return f"{len(violations)} markup-unsafe call site(s):\n{listing}\n\n{remedy}"


class TestRuleAConsolesAreMarkupFree:
    """Every ``Console`` inverts the default, or none of the rest holds."""

    def test_no_bare_console_construction(self) -> None:
        violations: list[str] = []
        for path in _python_files():
            if _is_factory(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                violations += [f"{_rel(path)}:{node.lineno}: {v}" for v in _violates_a(node)]
        assert not violations, _report(
            violations,
            "Build consoles with conductor.console.make_console() so an "
            "interpolated runtime value is never parsed as styling.",
        )

    def test_console_subclasses_lock_markup_off(self) -> None:
        """A subclass bypasses ``make_console``, so the class enforces it.

        Enumerated from the source rather than hardcoding one class name: a
        hardcoded import covers exactly one subclass forever, and the rule
        exists for the *next* one.
        """
        subclasses: list[str] = []
        for path in _python_files():
            if _is_factory(path):
                continue  # MarkupFreeConsole itself is the one legitimate base
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                subclasses += [
                    f"{_rel(path)}:{node.lineno}: {v}" for v in _violates_a_subclass(node)
                ]
        assert not subclasses, _report(
            subclasses,
            "Subclass conductor.console.MarkupFreeConsole rather than rich's "
            "Console, so the markup refusal is inherited.",
        )

        # And the one legitimate subclass actually refuses.
        from conductor.cli.run import _SilentAwareConsole

        with pytest.raises(TypeError):
            _SilentAwareConsole(markup=True)


class TestRuleBBypassSinksReceiveText:
    """``Panel`` titles and prompts parse markup whatever the console says."""

    def test_panel_titles_are_not_interpolated_strings(self) -> None:
        violations: list[str] = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            scopes = _scope_names(tree, _safe_text_names)
            for node in ast.walk(tree):
                violations += [
                    f"{_rel(path)}:{node.lineno}: {v}"
                    for v in _violates_b_panel(node, _names_for(scopes, node))
                ]
        assert not violations, _report(
            violations,
            "rich calls Text.from_markup on a Panel title regardless of the "
            "console's markup=False, so pass styled(...) or Text(...).",
        )

    def test_prompts_are_not_interpolated_strings(self) -> None:
        violations: list[str] = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            scopes = _scope_names(tree, _safe_text_names)
            for node in ast.walk(tree):
                violations += [
                    f"{_rel(path)}:{node.lineno}: {v}"
                    for v in _violates_b_prompt(node, _names_for(scopes, node))
                ]
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
            if _is_factory(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            scopes = _scope_names(tree, _safe_text_names)
            for node in ast.walk(tree):
                violations += [
                    f"{_rel(path)}:{node.lineno}: {v}"
                    for v in _violates_c(node, _names_for(scopes, node))
                ]
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
                violations += [
                    f"{_rel(path)}:{node.lineno}: {v}" for v in _violates_d(node, docstrings)
                ]
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
                violations += [f"{_rel(path)}:{node.lineno}: {v}" for v in _violates_e(node)]
        assert not violations, _report(
            violations,
            "The builtin print() renders a Text as its plain form, dropping "
            "styling and any text rich parsed as a tag. Pass a plain string, "
            "or print through a console built by make_console().",
        )


class TestRuleFNoTextInsideAnFString:
    """An f-string renders a ``Text`` as its plain form, discarding styling.

    This is the defect with the worst track record in this change: it shipped
    four separate times. Twice it destroyed data rather than styling -- a
    ``[workspace-instructions]`` stderr prefix vanished because rich had
    already parsed it as a tag, and a plugin listing's ✓/⚠ marker lost the
    colour that distinguishes the two states.

    Rule E is this same defect narrowed to the builtin ``print``. This is the
    general form: any ``Text``-bearing name interpolated into an f-string.
    """

    def test_no_text_value_is_interpolated_into_an_f_string(self) -> None:
        violations: list[str] = []
        for path in _python_files():
            if _is_factory(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            scopes = _scope_names(tree, _maybe_text_names)
            for node in ast.walk(tree):
                violations += [
                    f"{_rel(path)}:{node.lineno}: {v}"
                    for v in _violates_f(node, _names_for(scopes, node))
                ]
        assert not violations, _report(
            violations,
            "An f-string renders a Text as its plain form, dropping its "
            "styling. Use styled('{}{}', ...) or join(...) instead.",
        )


class TestRuleGTyperHelpIsEscaped:
    """Typer help is markup-parsed too, by typer's own rich console.

    The console convention does not reach it, so it was documented as being
    "outside" the rules -- and the four ``help=`` strings describing the
    registry reference syntax silently lost ``[@registry][@version]``, because
    ``@`` leads a tag rich deletes. That syntax appears nowhere else in
    ``--help``, so the flag was documented as not existing.
    """

    def test_typer_help_has_no_unescaped_markup(self) -> None:
        violations: list[str] = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                violations += [f"{_rel(path)}:{node.lineno}: {v}" for v in _violates_g(node)]
        assert not violations, _report(
            violations,
            "typer renders help through rich, so escape brackets as \\[ ... ] "
            "or the token is parsed as a style tag.",
        )


class TestRuleHEscapeIsNotUsed:
    """``rich.markup.escape`` is a convention the guard now actually enforces.

    It was listed as a rule while nothing checked it — the doc promised more
    than it delivered. It is banned because it is not byte-exact: the parser
    treats ``\\[`` as an escaped bracket, so an ordinary regex round-trips
    wrong whether or not it was escaped first.
    """

    def test_rich_markup_escape_is_never_used(self) -> None:
        violations: list[str] = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                violations += [f"{_rel(path)}:{node.lineno}: {v}" for v in _violates_h(node)]
        assert not violations, _report(
            violations,
            "escape() is not byte-exact. Build a Text (styled(...) / "
            "Text.from_markup(...)) so the parser is never involved.",
        )


class TestTheGuardsActuallyDetectViolations:
    """Negative controls.

    A static check that silently matches nothing is worse than no check: it
    reports "all clear" forever. Each rule is run against a snippet that
    violates it, and one that does not.
    """

    @staticmethod
    def _violations_in(source: str, rule: str) -> list[str]:
        """Run one enforced rule over a snippet.

        Calls the same predicate the source scan uses, so a control cannot
        pass while the rule it controls has drifted.
        """
        tree = ast.parse(source)
        docstrings = _docstring_ids(tree)
        safe_scopes = _scope_names(tree, _safe_text_names)
        maybe_scopes = _scope_names(tree, _maybe_text_names)
        found: list[str] = []
        for node in ast.walk(tree):
            if rule == "A":
                found += _violates_a(node)
            elif rule == "B":
                found += _violates_b_panel(node, _names_for(safe_scopes, node))
            elif rule == "B-prompt":
                found += _violates_b_prompt(node, _names_for(safe_scopes, node))
            elif rule == "C":
                found += _violates_c(node, _names_for(safe_scopes, node))
            elif rule == "D":
                found += _violates_d(node, docstrings)
            elif rule == "E":
                found += _violates_e(node)
            elif rule == "F":
                found += _violates_f(node, _names_for(maybe_scopes, node))
            elif rule == "G":
                found += _violates_g(node)
            elif rule == "A-subclass":
                found += _violates_a_subclass(node)
            elif rule == "H":
                found += _violates_h(node)
            else:  # pragma: no cover - guard against a typo in a new control
                raise AssertionError(f"unknown rule {rule!r}")
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

    def test_rule_b_prompt_flags_an_interpolated_prompt(self) -> None:
        assert self._violations_in('Prompt.ask(f"[bold]{name}[/bold]")', "B-prompt")

    def test_rule_b_prompt_accepts_styled(self) -> None:
        assert not self._violations_in('Prompt.ask(styled("[bold]{}[/bold]", name))', "B-prompt")

    def test_rule_b_prompt_accepts_a_text_annotated_parameter(self) -> None:
        src = "def f(prompt: Text) -> None:\n    Prompt.ask(prompt)\n"
        assert not self._violations_in(src, "B-prompt")

    def test_rule_b_prompt_flags_a_str_annotated_parameter(self) -> None:
        src = "def f(prompt: str) -> None:\n    Prompt.ask(prompt)\n"
        assert self._violations_in(src, "B-prompt")

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

    @pytest.mark.parametrize(
        "snippet",
        [
            # The exact shape that shipped four times.
            'm = Text.from_markup("[y]x[/y]") if flag else ""\ntable.add_row(f"{name}{m}")',
            'm = styled("[y]{}[/y]", v)\nconsole.print(f"{m} done")',
            "console.print(f\"{styled('[y]{}[/y]', v)} done\")",
            'def f(t: Text) -> str:\n    return f"<{t}>"',
        ],
    )
    def test_rule_f_flags_a_text_in_an_f_string(self, snippet: str) -> None:
        assert self._violations_in(snippet, "F")

    @pytest.mark.parametrize(
        "snippet",
        [
            'console.print(f"{name} done")',
            'console.print(styled("{}{}", name, marker))',
            'm = ", ".join(names)\nconsole.print(f"{m} done")',
            # ``!r`` is a deliberate repr, not an accidental flatten.
            'def f(t: Text) -> str:\n    return f"{t!r}"',
        ],
    )
    def test_rule_f_accepts_safe_sites(self, snippet: str) -> None:
        assert not self._violations_in(snippet, "F")

    @pytest.mark.parametrize(
        "snippet",
        [
            'typer.Argument(help="ref (name[@registry])")',
            'typer.Option(help="see [docs] for detail")',
            'typer.Typer(epilog="run [cmd] first")',
        ],
    )
    def test_rule_g_flags_unescaped_typer_help(self, snippet: str) -> None:
        assert self._violations_in(snippet, "G")

    @pytest.mark.parametrize(
        "snippet",
        [
            r'typer.Argument(help=r"ref (name\[@registry])")',
            'typer.Argument(help="ref (name)")',
            'typer.Option(help="uppercase [KPI] renders literally")',
            'console.print("[dim]not a typer call[/dim]")',
        ],
    )
    def test_rule_g_accepts_safe_typer_help(self, snippet: str) -> None:
        assert not self._violations_in(snippet, "G")

    @pytest.mark.parametrize(
        "snippet",
        [
            "class Bad(Console):\n    pass\n",
            "class Bad(rich.console.Console):\n    pass\n",
        ],
    )
    def test_rule_a_subclass_flags_a_console_subclass(self, snippet: str) -> None:
        assert self._violations_in(snippet, "A-subclass")

    @pytest.mark.parametrize(
        "snippet",
        [
            "class Fine(MarkupFreeConsole):\n    pass\n",
            "class Fine:\n    pass\n",
        ],
    )
    def test_rule_a_subclass_accepts_the_safe_base(self, snippet: str) -> None:
        assert not self._violations_in(snippet, "A-subclass")

    @pytest.mark.parametrize(
        "snippet",
        [
            "from rich.markup import escape",
            "from rich.markup import escape, render",
            "x = markup.escape(value)",
        ],
    )
    def test_rule_h_flags_escape(self, snippet: str) -> None:
        assert self._violations_in(snippet, "H")

    @pytest.mark.parametrize(
        "snippet",
        [
            "import re\nx = re.escape(value)",
            "from rich.markup import render",
            'x = styled("[b]{}[/b]", value)',
        ],
    )
    def test_rule_h_accepts_safe_sites(self, snippet: str) -> None:
        assert not self._violations_in(snippet, "H")

    def test_rule_f_does_not_leak_names_across_functions(self) -> None:
        """A ``Text`` name in one function must not clear another's local.

        ``verbose_log(message: str | Text)`` would otherwise make ``message``
        Text-bearing for all of ``cli/run.py``.
        """
        src = (
            "def a(message: str | Text) -> None:\n    pass\n\n"
            'def b(message: str) -> str:\n    return f"{message}"\n'
        )
        assert not self._violations_in(src, "F")


class TestSafeNameResolution:
    """``_safe_text_names`` must resolve narrowly, or rule B goes blind.

    It exists so a real ``Text``-typed local is not pushed onto an allowlist.
    If it over-accepts, every bare name becomes invisible to rule B and the
    guard silently stops working.

    Resolved through ``_scope_names``/``_names_for`` — the same path the rules
    take — so these assert what a rule would actually see at that call site,
    not what the resolver returns for a whole module.
    """

    @staticmethod
    def _names_at_the_prompt(source: str) -> frozenset[str]:
        """The names visible where ``Prompt.ask(...)`` is called."""
        tree = ast.parse(source)
        scopes = _scope_names(tree, _safe_text_names)
        call = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "ask"
        )
        return _names_for(scopes, call)

    def test_text_annotated_parameter_is_safe(self) -> None:
        src = "def f(prompt: Text) -> None:\n    Prompt.ask(prompt)\n"
        assert "prompt" in self._names_at_the_prompt(src)

    def test_optional_text_parameter_is_safe(self) -> None:
        src = "def f(prompt: Text | None = None) -> None:\n    Prompt.ask(prompt)\n"
        assert "prompt" in self._names_at_the_prompt(src)

    def test_local_assigned_from_styled_is_safe(self) -> None:
        src = 'def f():\n    p = styled("[b]x[/b]")\n    Prompt.ask(p)\n'
        assert "p" in self._names_at_the_prompt(src)

    def test_conditional_between_safe_values_is_safe(self) -> None:
        src = (
            "def f(prompt: Text | None = None):\n"
            '    p = styled("[b]x[/b]") if prompt is None else prompt\n'
            "    Prompt.ask(p)\n"
        )
        assert "p" in self._names_at_the_prompt(src)

    def test_str_annotated_parameter_is_not_safe(self) -> None:
        src = "def f(prompt: str) -> None:\n    Prompt.ask(prompt)\n"
        assert "prompt" not in self._names_at_the_prompt(src)

    def test_unannotated_parameter_is_not_safe(self) -> None:
        src = "def f(prompt):\n    Prompt.ask(prompt)\n"
        assert "prompt" not in self._names_at_the_prompt(src)

    def test_local_assigned_from_an_f_string_is_not_safe(self) -> None:
        src = 'def f(x):\n    p = f"[b]{x}[/b]"\n    Prompt.ask(p)\n'
        assert "p" not in self._names_at_the_prompt(src)

    def test_conditional_with_one_unsafe_branch_is_not_safe(self) -> None:
        src = 'def f(x, flag):\n    p = styled("[b]y[/b]") if flag else x\n    Prompt.ask(p)\n'
        assert "p" not in self._names_at_the_prompt(src)

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
