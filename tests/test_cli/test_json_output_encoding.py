"""Tests for JSON result output under a non-UTF-8 stdout encoding (issue #342).

On Windows with a legacy locale (``cp1252``), ``conductor run`` crashed with
``UnicodeEncodeError`` *after* the workflow had already completed successfully,
while writing its final JSON result to stdout. The document was truncated
mid-field, so callers could not parse it, and the process exited non-zero.

``json.dumps`` already emits pure ASCII by default, but rich's
``Console.print_json`` re-parses and re-serialises with ``ensure_ascii=False``
immediately before the write, putting the literal character back. The fix is to
pass ``ensure_ascii=True`` at every JSON sink.

These tests deliberately **simulate** the condition rather than detect it: CI
runs on ``ubuntu-latest``, so anything gated on ``sys.platform`` would never
execute. Each test renders through a ``TextIOWrapper`` bound to ``cp1252`` and
asserts both that nothing raises *and* that the payload round-trips, which is
the property a machine consumer actually depends on.
"""

from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

# Characters that are valid UTF-8 but *unencodable* in cp1252. Each was verified
# to raise UnicodeEncodeError on a strict cp1252 stream.
#
# U+2014 EM DASH is deliberately absent: cp1252 *can* encode it (0x97), so it
# would not exercise the failure despite being common in agent prose. That is
# exactly why this bug looked intermittent in the field -- whether it fired
# depended on which glyph an agent happened to emit.
NON_CP1252_SAMPLES = [
    pytest.param("\u2192", id="rightwards-arrow"),
    pytest.param("\u2705", id="white-heavy-check-mark"),
    pytest.param("\u26a0", id="warning-sign"),
    pytest.param("\U0001f600", id="grinning-face-non-bmp"),
]


def _cp1252_console() -> tuple[Console, io.BytesIO]:
    """Build a Console writing through a strict cp1252 stream.

    ``errors="strict"`` mirrors what CPython gives ``sys.stdout`` under a
    non-UTF-8 locale; ``sys.stderr`` gets ``backslashreplace`` instead, which
    is why the original crash only ever hit the stdout JSON write.
    """
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="")
    # force_terminal=False keeps rich from emitting ANSI, so the captured bytes
    # are the JSON document and nothing else.
    return Console(file=stream, force_terminal=False, width=200), raw


def _render(console: Console, stream_owner: io.BytesIO, payload: object) -> str:
    console.print_json(json.dumps(payload), ensure_ascii=True)
    console.file.flush()
    return stream_owner.getvalue().decode("cp1252")


@pytest.mark.parametrize("char", NON_CP1252_SAMPLES)
def test_print_json_survives_non_cp1252_characters(char: str) -> None:
    """The reported crash: a single unencodable character killed the write."""
    console, raw = _cp1252_console()
    payload = {"technical_review_feedback": f"score improved {char} shipped"}

    rendered = _render(console, raw, payload)

    assert json.loads(rendered) == payload


@pytest.mark.parametrize("char", NON_CP1252_SAMPLES)
def test_print_json_round_trips_losslessly(char: str) -> None:
    """Escaping must be lossless.

    ``errors="replace"`` would also stop the crash, but it would silently turn
    review feedback into ``?``. ``ensure_ascii=True`` emits ``\\uXXXX`` escapes
    (surrogate pairs for non-BMP), which are valid JSON and decode back to the
    original text.
    """
    console, raw = _cp1252_console()
    payload = {"feedback": char}

    rendered = _render(console, raw, payload)

    assert json.loads(rendered)["feedback"] == char
    # The character must have been escaped rather than written literally.
    assert char not in rendered


def test_print_json_output_is_pure_ascii() -> None:
    """The whole document must be ASCII, so any legacy codec can encode it."""
    console, raw = _cp1252_console()
    payload = {"a": "\u2192", "b": ["\u2705", {"c": "\U0001f600"}]}

    rendered = _render(console, raw, payload)

    assert rendered.isascii()
    assert json.loads(rendered) == payload


def test_print_json_would_fail_without_ensure_ascii() -> None:
    """Guard the regression: prove the default is genuinely unsafe here.

    Without this, a future change that drops ``ensure_ascii=True`` would leave
    the tests above passing for the wrong reason (e.g. if rich changed its own
    default), and the bug could return unnoticed.
    """
    console, _ = _cp1252_console()

    with pytest.raises(UnicodeEncodeError):
        console.print_json(json.dumps({"feedback": "\u2192"}), ensure_ascii=False)


def test_print_json_data_kwarg_form_is_also_safe() -> None:
    """``conductor doctor`` uses the ``data=`` form rather than a JSON string."""
    console, raw = _cp1252_console()
    payload = {"status": "\u2705 ok", "note": "cache \u2192 warm"}

    console.print_json(data=payload, ensure_ascii=True)
    console.file.flush()
    rendered = raw.getvalue().decode("cp1252")

    assert json.loads(rendered) == payload
