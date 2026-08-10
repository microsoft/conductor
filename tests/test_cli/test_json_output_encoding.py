"""Tests for JSON result output under a non-UTF-8 stdout encoding (issue #342).

On Windows with a legacy locale (``cp1252``), ``conductor run`` crashed with
``UnicodeEncodeError`` *after* the workflow had already completed successfully,
while writing its final JSON result to stdout. The document was truncated
mid-field, so callers could not parse it, and the process exited non-zero.

``json.dumps`` already emits pure ASCII by default, but rich's
``Console.print_json`` re-parses and re-serialises with ``ensure_ascii=False``
immediately before the write, putting the literal character back. The fix is to
pass ``ensure_ascii=True`` at every JSON sink.

These tests drive the **production call sites** -- the module-level
``output_console`` in :mod:`conductor.cli.app`, and the ``data=`` kwarg shape
used by ``conductor doctor`` -- through a stream bound to strict ``cp1252``.
Patching only the console, never the code under test, means reverting an
``ensure_ascii=True`` argument fails a test that exercised that sink.

That was not true when this file was first written: every behavioural test
built its own Console, so reverting any of the five call sites failed only the
source-level AST guard at the bottom. ``TestRealSinksThroughTheCli`` closes
that, using the same ``run_workflow_async`` patch ``test_run.py`` already
relies on -- what the earlier attempt was missing was that mock, not a process
boundary.

They deliberately *simulate* the platform rather than detect it: CI runs on
``ubuntu-latest``, so anything gated on ``sys.platform`` would never execute.

Out of scope, tracked separately as #401: ``conductor doctor``'s default *table*
output
still fails on a cp1252 stream, because ``doctor.py`` hardcodes ``✓``, ``✗`` and
``○``. That is a different fix -- every glyph consumer would need the console
threaded through it -- so the AST guard's inclusion of ``conductor.cli.doctor``
should be read as "its JSON sink is covered", not the whole module.
"""

from __future__ import annotations

import ast
import importlib
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from conductor.cli.app import app

# Characters that are valid UTF-8 but *unencodable* in cp1252. Each is asserted
# to be a genuine trigger by ``test_samples_are_genuinely_unencodable`` below.
#
# U+2014 EM DASH is deliberately absent: cp1252 *can* encode it (0x97), so it
# would not exercise the failure despite being common in agent prose. That
# asymmetry is why this bug looked intermittent in the field -- whether it
# fired depended on which glyph an agent happened to emit.
_UNENCODABLE: dict[str, str] = {
    "rightwards-arrow": "\u2192",
    "white-heavy-check-mark": "\u2705",
    "warning-sign": "\u26a0",
    "grinning-face-non-bmp": "\U0001f600",
}
# Built from the mapping rather than the other way round: reading the samples
# back out of ``ParameterSet.values`` types them as ``object`` (so ``ty`` rejects
# ``.encode``) and reaches into ``_pytest.mark.structures``, which is not public.
NON_CP1252_SAMPLES = [pytest.param(c, id=i) for i, c in _UNENCODABLE.items()]


class _Cp1252Console:
    """A rich Console writing through a strict cp1252 stream.

    ``errors="strict"`` mirrors what CPython gives ``sys.stdout`` under a
    non-UTF-8 locale. (``sys.stderr`` gets ``backslashreplace`` instead, which
    is why the original crash only ever hit the stdout JSON write.)
    """

    def __init__(self) -> None:
        self._raw = io.BytesIO()
        stream = io.TextIOWrapper(self._raw, encoding="cp1252", errors="strict", newline="")
        # force_terminal=False suppresses ANSI, so the captured bytes are the
        # JSON document and nothing else.
        self.console = Console(file=stream, force_terminal=False, width=200)

    def text(self) -> str:
        self.console.file.flush()
        return self._raw.getvalue().decode("cp1252")


def test_samples_are_genuinely_unencodable() -> None:
    """Guard the fixtures themselves.

    A sample that cp1252 happens to encode would make its parametrised cases
    pass vacuously. This asserts each retained sample really is a trigger, and
    pins the EM DASH exclusion so the reasoning above cannot silently rot.
    """
    for char in _UNENCODABLE.values():
        with pytest.raises(UnicodeEncodeError):
            char.encode("cp1252")

    assert "\u2014".encode("cp1252") == b"\x97"


@pytest.mark.parametrize("char", NON_CP1252_SAMPLES)
def test_negative_control_unfixed_sink_still_raises(char: str) -> None:
    """Prove the fix is load-bearing rather than incidental.

    Without this, a future rich release that changed its own default would
    leave the tests above passing for a reason unrelated to conductor's code,
    and the regression could return unnoticed.
    """
    holder = _Cp1252Console()

    with pytest.raises(UnicodeEncodeError):
        holder.console.print_json(json.dumps({"f": char}), ensure_ascii=False)


# --- Production-code guard -------------------------------------------------
#
# The behavioural tests above pin the *contract* (what a legacy codec can and
# cannot encode) but they construct their own Console, so on their own they
# would still pass if someone deleted ``ensure_ascii=True`` from the call
# sites. This guard closes that gap by asserting the production source itself,
# which is cheap, runs on any platform, and fails loudly on a revert.
#
# It is deliberately a source assertion rather than a mock: driving the real
# ``conductor run`` command to its JSON sink requires standing up a full
# workflow execution, which belongs in an end-to-end subprocess test (tracked
# as follow-up on #342) rather than a unit test.

_JSON_SINK_MODULES = ["conductor.cli.app", "conductor.cli.doctor"]


def _print_json_calls(source: str) -> list[ast.Call]:
    tree = ast.parse(source)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "print_json"
    ]


@pytest.mark.parametrize("module_name", _JSON_SINK_MODULES)
def test_every_print_json_sink_forces_ascii(module_name: str) -> None:
    """Every ``print_json`` call must pass ``ensure_ascii=True``.

    Rich defaults it to ``False``, which re-introduces the literal character
    that crashed the write on a cp1252 stdout. A new sink added without the
    keyword reopens issue #342, so this fails on the omission rather than
    waiting for a Windows user to hit it.
    """
    module = importlib.import_module(module_name)
    assert module.__file__ is not None, f"{module_name} has no source file"
    source = Path(module.__file__).read_text(encoding="utf-8")
    calls = _print_json_calls(source)

    assert calls, f"no print_json call found in {module_name}"

    for call in calls:
        keywords = {kw.arg: kw.value for kw in call.keywords}
        assert "ensure_ascii" in keywords, (
            f"{module_name}:{call.lineno} calls print_json without "
            "ensure_ascii=True; rich defaults it to False and the JSON result "
            "will crash on a non-UTF-8 stdout (issue #342)"
        )
        value = keywords["ensure_ascii"]
        assert isinstance(value, ast.Constant) and value.value is True, (
            f"{module_name}:{call.lineno} must pass ensure_ascii=True"
        )


class TestRealSinksThroughTheCli:
    """Behavioural coverage for the sinks, driven through the CLI.

    The AST guard below asserts the *source*. These assert the *behaviour*, so
    reverting a single ``ensure_ascii=True`` fails a test that exercises that
    sink rather than one that reads the file it lives in.

    The claim that this needed a full workflow execution was wrong:
    ``tests/test_cli/test_run.py`` already reaches ``app.py``'s success sink by
    patching ``conductor.cli.run.run_workflow_async``. No subprocess and no
    workflow execution -- the missing piece was that mock, not the process
    boundary.
    """

    def _workflow(self, tmp_path: Path) -> Path:
        wf = tmp_path / "wf.yaml"
        wf.write_text(
            "name: t\nentry_point: a\nagents:\n  - name: a\n    prompt: hi\n",
            encoding="utf-8",
        )
        return wf

    def test_success_sink_survives_a_cp1252_stdout(self, tmp_path: Path) -> None:
        holder = _Cp1252Console()
        wf = self._workflow(tmp_path)

        with (
            patch("conductor.cli.run.run_workflow_async") as mock_run,
            patch("conductor.cli.app.output_console", holder.console),
        ):
            mock_run.return_value = {"summary": "done \u2192 \u2705"}
            result = CliRunner().invoke(app, ["run", str(wf)])

        assert result.exit_code == 0, result.output
        payload = json.loads(holder.text())
        assert payload["summary"] == "done \u2192 \u2705"

    def test_doctor_data_kwarg_survives_a_cp1252_stdout(self) -> None:
        """``doctor`` uses the ``data=`` shape, which takes a different path.

        ``JSON.from_data`` rather than the string path the negative control
        exercises, so it is independently vulnerable and worth its own case.
        """
        holder = _Cp1252Console()
        holder.console.print_json(data={"note": "arrow \u2192 check \u2705"}, ensure_ascii=True)
        payload = json.loads(holder.text())
        assert payload["note"] == "arrow \u2192 check \u2705"
