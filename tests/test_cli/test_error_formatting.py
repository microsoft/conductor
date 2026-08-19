"""The error panel must show WHY, not just the headline line."""

from __future__ import annotations

from conductor.cli.app import _MAX_ERROR_LINES, format_error
from conductor.console import make_console
from conductor.exceptions import ExecutionError


def _render(error: Exception) -> str:
    console = make_console(width=200)
    with console.capture() as cap:
        console.print(format_error(error))
    return cap.get()


def test_per_item_detail_is_shown() -> None:
    out = _render(
        ExecutionError(
            "All items in for-each group 'deliver_per_ticket' failed:\n"
            "  - [0]: SubworkflowTerminatedError: registry preflight failed",
            suggestion="run: aws sso login",
        )
    )
    assert "registry preflight failed" in out


def test_suggestion_is_not_duplicated() -> None:
    """``ConductorError.__str__`` embeds the suggestion; the panel adds it too."""
    out = _render(ExecutionError("boom", suggestion="do the thing"))
    assert out.count("do the thing") == 1


def test_long_errors_are_bounded() -> None:
    out = _render(ExecutionError("\n".join(f"line {i}" for i in range(_MAX_ERROR_LINES + 30))))
    assert "more lines)" in out
    assert f"line {_MAX_ERROR_LINES + 29}" not in out
