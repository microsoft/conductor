"""Sanitize YAML-authored description text before it reaches a tool schema
(NFR4, E7-T3).

``workflow.description`` is remote, user-controlled content — it can come
from a third-party git registry — that flows verbatim into text the host
model reads and acts on. The design's Security Considerations names this
plainly: "Tool descriptions are attack surface. ... the canonical MCP
tool-poisoning vector." This module is the boundary that runs before any
such text reaches a generated tool's ``description`` field.

This is a defense-in-depth boundary, not a prompt-injection classifier: it
removes documented obfuscation techniques (control characters, invisible
Unicode) and known instruction-marker shapes, and bounds size. It does not
— and cannot — guarantee the remaining text has no persuasive content; a
plain English sentence can carry an injected instruction with nothing to
strip.
"""

from __future__ import annotations

import re

# NFR4 hard cap. Long enough to comfortably fit a paragraph-length
# description; short enough to bound how much untrusted text a single
# workflow can inject into every tool listing a host renders.
MAX_DESCRIPTION_LENGTH = 500

# ASCII/C0 control characters (excluding plain space, which is legitimate)
# and C1 controls. `\t`/`\n` are also stripped: a description is a single
# schema-field string, not a multi-line document, and preserving raw
# newlines/tabs is exactly the kind of terminal/rendering trick this
# boundary exists to close off.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Invisible / bidi-override Unicode characters used to visually hide text
# from a human reviewer while a model still reads it -- a documented
# prompt-injection obfuscation technique (zero-width space/joiners, BOM,
# and the Unicode bidi-override control characters).
_INVISIBLE_CHARS_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u202a-\u202e\u2066-\u2069]")

# Instruction-shaped markers: text formatted to look like a system /
# instruction directive rather than plain prose -- the canonical MCP
# tool-poisoning shape named in Security Considerations. This is a
# denylist of known shapes, not a general prompt-injection classifier; it
# strips the marker itself (not the surrounding text) so an otherwise
# legitimate description survives.
_INSTRUCTION_MARKER_RE = re.compile(
    r"""
    <\s*/?\s*(system|assistant|instructions?)\s*>   # <system>, </instructions>
    | \[\s*/?\s*(system|inst|instructions?)\s*\]     # [INST], [/SYSTEM]
    | <\|[^|<>]{0,64}\|>                             # <|im_start|>, <|system|>
    | ^\s*(system|assistant)\s*:                     # a leading "system:"/"assistant:" role prefix
    """,
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)

# Runs of whitespace other than a single space, collapsed after the marker
# strip above (which can leave behind irregular spacing).
_WHITESPACE_RUN_RE = re.compile(r"[ \t]+")


def sanitize_description(text: str | None) -> str:
    """Sanitize YAML-authored text before it reaches a tool ``description``.

    Applies, in order: strip control characters, strip invisible/bidi
    Unicode characters, strip instruction-shaped markers, collapse
    resulting whitespace runs, then hard-cap the length (NFR4). Returns
    ``""`` for ``None`` or an empty/whitespace-only string.

    Args:
        text: Raw, YAML-authored text (e.g. ``workflow.description``).

    Returns:
        Sanitized, length-capped text safe to place in a tool schema.
    """
    if not text:
        return ""

    cleaned = _CONTROL_CHARS_RE.sub(" ", text)
    cleaned = _INVISIBLE_CHARS_RE.sub("", cleaned)
    cleaned = _INSTRUCTION_MARKER_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RUN_RE.sub(" ", cleaned).strip()

    if len(cleaned) > MAX_DESCRIPTION_LENGTH:
        cleaned = cleaned[: MAX_DESCRIPTION_LENGTH - 1].rstrip() + "\u2026"

    return cleaned
