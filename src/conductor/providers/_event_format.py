"""Helpers for formatting tool-call event payloads emitted to subscribers.

The console renderer, JSONL event logger, and web dashboard all consume
``agent_tool_start`` / ``agent_tool_complete`` events from both providers.
These helpers ensure the payloads are human-readable strings rather than
Python ``repr()`` output of structured SDK objects.

See https://github.com/microsoft/conductor/issues/93.
"""

from __future__ import annotations

import json
from typing import Any


def format_tool_arguments(arguments: Any, max_length: int = 500) -> str | None:
    """Render tool-call arguments as a compact, human-readable string.

    Dict-like arguments are JSON-encoded so the dashboard sees ``{"k": "v"}``
    rather than Python repr (``{'k': 'v'}`` with single quotes and doubled
    backslashes on Windows paths). Falls back to ``str(arguments)`` for
    inputs that aren't JSON-serializable.

    The result is truncated to ``max_length`` characters with a trailing
    ellipsis when truncation occurs.

    Args:
        arguments: The tool-call arguments object (typically a dict).
        max_length: Maximum length of the returned string before truncation.

    Returns:
        A formatted string, or ``None`` when ``arguments`` is falsy.
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
    helper unwraps the text payload by preferring ``content`` then
    ``detailed_content``, falling back to ``str(result)`` for plain-string
    results (Claude provider) or unknown shapes.

    The result is truncated to ``max_length`` characters with a trailing
    ellipsis when truncation occurs.

    Args:
        result: The tool result object emitted by the SDK.
        max_length: Maximum length of the returned string before truncation.

    Returns:
        A formatted string, or ``None`` when ``result`` is falsy.
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
