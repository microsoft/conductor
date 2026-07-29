"""Helpers for formatting event payloads emitted to subscribers.

The console renderer, JSONL event logger, and web dashboard all consume
``agent_tool_start`` / ``agent_tool_complete`` events from both providers.
These helpers ensure the payloads are human-readable strings rather than
Python ``repr()`` output of structured SDK objects.

Parse-recovery events share this module so every provider emits an
identically-shaped payload.

See https://github.com/microsoft/conductor/issues/93.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def emit_parse_recovery_event(
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    *,
    attempt: int,
    max_attempts: int,
    is_schema_failure: bool,
    error: str,
    max_error_chars: int = 500,
) -> None:
    """Emit an ``agent_parse_recovery`` event, swallowing subscriber errors.

    Recovery attempts were previously visible only under verbose console
    logging, so a run that silently burned its recovery budget left no trace
    in the dashboard or the structured event log.

    Args:
        event_callback: Subscriber callback, or ``None`` to skip emission.
        attempt: 1-based recovery attempt about to be made.
        max_attempts: Configured recovery budget.
        is_schema_failure: True when the response parsed but failed schema
            validation, False for a JSON syntax failure.
        error: The error that triggered this recovery attempt.
        max_error_chars: Truncation limit for ``error``.

    Raises:
        Nothing. A failing subscriber must not break agent execution.
    """
    if event_callback is None:
        return

    truncated = error if len(error) <= max_error_chars else error[:max_error_chars] + "..."
    try:
        event_callback(
            "agent_parse_recovery",
            {
                "attempt": attempt,
                "max_attempts": max_attempts,
                "reason": "schema" if is_schema_failure else "syntax",
                "error": truncated,
            },
        )
    except Exception:
        logger.debug("Error in event_callback for agent_parse_recovery", exc_info=True)


def format_tool_arguments(arguments: Any, max_length: int = 500) -> str | None:
    """Render tool-call arguments as a compact, human-readable string.

    Dict-like arguments are JSON-encoded so the dashboard sees ``{"k": "v"}``
    rather than Python repr (``{'k': 'v'}`` with single quotes and doubled
    backslashes on Windows paths). Falls back to ``str(arguments)`` for
    inputs that aren't JSON-serializable.

    When the rendered string exceeds ``max_length``, it is truncated to
    exactly ``max_length`` characters and a single-character ellipsis
    (``"…"``) is appended, so the returned string is at most
    ``max_length + 1`` characters long.

    Args:
        arguments: The tool-call arguments object (typically a dict).
        max_length: Maximum length of the rendered string before an
            ellipsis is appended. Note: the returned string may be
            ``max_length + 1`` characters long when truncation occurs.

    Returns:
        A formatted string, or ``None`` when ``arguments`` is falsy
        (including ``None``, ``""``, and ``{}``).
    """
    if not arguments:
        return None

    try:
        # ``default=str`` lets us serialize objects (e.g. Path) without crashing.
        rendered = json.dumps(arguments, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(arguments)

    if len(rendered) > max_length:
        return rendered[:max_length] + "…"
    return rendered


def extract_tool_result_text(result: Any, max_length: int = 500) -> str | None:
    """Extract human-readable text from a tool-call result.

    The Copilot SDK emits structured ``Result(content=..., detailed_content=...,
    contents=..., kind=...)`` objects whose ``str()`` is the unhelpful
    Python repr — newlines escaped, paths doubled, wrapper visible. This
    helper unwraps the text payload as follows:

    1. Plain strings (e.g. from the Claude provider's ``MCPManager``) are
       returned unchanged.
    2. For other objects, the helper reads the ``content`` attribute; if
       absent or ``None``, it falls back to ``detailed_content``.
    3. If neither attribute yields a non-empty string, ``str(result)`` is
       used as a last resort for unknown shapes.

    When the extracted text exceeds ``max_length``, it is truncated to
    exactly ``max_length`` characters and a single-character ellipsis
    (``"…"``) is appended, so the returned string is at most
    ``max_length + 1`` characters long.

    Args:
        result: The tool result object emitted by the SDK.
        max_length: Maximum length of the extracted text before an
            ellipsis is appended. Note: the returned string may be
            ``max_length + 1`` characters long when truncation occurs.

    Returns:
        A formatted string, or ``None`` when ``result`` is falsy
        (including ``None`` and ``""``).
    """
    if not result:
        return None

    if isinstance(result, str):
        text: str = result
    else:
        text_attr = getattr(result, "content", None)
        if text_attr is None:
            text_attr = getattr(result, "detailed_content", None)
        text = text_attr if isinstance(text_attr, str) and text_attr else str(result)

    if len(text) > max_length:
        return text[:max_length] + "…"
    return text
