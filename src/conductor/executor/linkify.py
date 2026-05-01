"""Auto-linkify bare file paths and URLs in rendered markdown text.

This module provides post-processing for human-facing rendered text (gate
prompts, etc.) to automatically convert bare file paths and URLs into
markdown links.  It is *not* used inside the generic ``TemplateRenderer`` —
only at call-sites that produce text destined for markdown rendering (web
dashboard, Rich terminal).

The processing is markdown-aware: fenced code blocks, inline code spans,
and existing markdown links are left untouched.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared extension allowlist — kept in sync with web/server.py
# ---------------------------------------------------------------------------
LINKABLE_EXTENSIONS = frozenset(
    {
        ".md",
        ".txt",
        ".yaml",
        ".yml",
        ".json",
        ".log",
        ".py",
        ".ts",
        ".js",
        ".tsx",
        ".jsx",
        ".css",
        ".html",
        ".toml",
        ".cfg",
        ".ini",
        ".csv",
        ".xml",
        ".sh",
        ".bat",
        ".ps1",
        ".plan.md",
    }
)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Fenced code block (``` or ~~~, with optional language tag)
_FENCED_CODE_RE = re.compile(r"^(`{3,}|~{3,}).*?^\1", re.MULTILINE | re.DOTALL)

# Inline code span (`...`)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# Existing markdown links: [text](url) or [text][ref]
_EXISTING_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)|\[[^\]]*\]\[[^\]]*\]")

# Bare URL: http(s)://... terminated at whitespace or common punctuation
_URL_RE = re.compile(
    r"(?<![(\[])"  # not preceded by ( or [
    r"https?://[^\s)<>\]\[\"'`]+"
)

# Bare file path: contains at least one /, ends with a known extension.
# Must start at a word boundary or line start.  Avoids matching inside
# URLs (already handled) by requiring no scheme prefix.
_FILE_PATH_RE = re.compile(
    r"(?<![a-zA-Z0-9_/\\])"  # not preceded by path-like chars (avoids partial matches)
    r"(?!https?://)"  # not a URL
    r"(?:[a-zA-Z0-9_.][a-zA-Z0-9_./-]*[a-zA-Z0-9_]"  # path chars with at least one /
    r")"
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def linkify_markdown(
    text: str,
    base_dir: Path | None = None,
) -> str:
    """Post-process rendered text to add markdown links for paths and URLs.

    Processing steps:
      1. Normalize Jinja2 whitespace artifacts (3+ consecutive newlines → 2).
      2. Auto-linkify bare ``http(s)://`` URLs.
      3. Auto-linkify bare file paths (verified against *base_dir* when given).

    Fenced code blocks, inline code spans, and existing markdown links are
    preserved unchanged.

    Args:
        text: Rendered template text (may contain markdown).
        base_dir: Optional directory for file existence checks.  When
            provided, only paths that resolve to an existing file within
            *base_dir* are linkified.

    Returns:
        Text with bare paths/URLs wrapped in markdown link syntax.
    """
    # Step 1: normalize whitespace
    text = _normalize_whitespace(text)

    # Step 2 & 3: linkify, skipping protected regions
    text = _linkify_with_protection(text, base_dir)

    return text


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalize_whitespace(text: str) -> str:
    """Collapse 3+ consecutive newlines into exactly 2 (one blank line)."""
    return re.sub(r"\n{3,}", "\n\n", text)


def _linkify_with_protection(text: str, base_dir: Path | None) -> str:
    """Linkify URLs and file paths while protecting code/links.

    Strategy: identify protected spans (fenced code, inline code, existing
    links), then process only the unprotected gaps.
    """
    protected: list[tuple[int, int]] = []

    for pattern in (_FENCED_CODE_RE, _INLINE_CODE_RE, _EXISTING_LINK_RE):
        for m in pattern.finditer(text):
            protected.append((m.start(), m.end()))

    # Sort and merge overlapping spans
    protected.sort()
    merged: list[tuple[int, int]] = []
    for start, end in protected:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # Build result by processing unprotected segments
    result: list[str] = []
    prev_end = 0
    for pstart, pend in merged:
        if prev_end < pstart:
            # Unprotected gap — linkify it
            result.append(_linkify_segment(text[prev_end:pstart], base_dir))
        # Protected span — copy verbatim
        result.append(text[pstart:pend])
        prev_end = pend
    # Final unprotected tail
    if prev_end < len(text):
        result.append(_linkify_segment(text[prev_end:], base_dir))

    return "".join(result)


def _linkify_segment(segment: str, base_dir: Path | None) -> str:
    """Linkify bare URLs and file paths in an unprotected text segment."""
    # First pass: linkify URLs
    segment = _URL_RE.sub(_wrap_url, segment)
    # Second pass: linkify file paths
    segment = _linkify_file_paths(segment, base_dir)
    return segment


def _wrap_url(m: re.Match[str]) -> str:
    """Wrap a bare URL in markdown autolink syntax."""
    url = m.group(0)
    # Strip trailing punctuation that's unlikely part of the URL
    trailing = ""
    while url and url[-1] in ".,;:!?)":
        # Keep ) only if there's a matching ( in the URL (e.g. Wikipedia links)
        if url[-1] == ")" and "(" in url:
            break
        trailing = url[-1] + trailing
        url = url[:-1]
    return f"[{url}]({url}){trailing}"


def _linkify_file_paths(segment: str, base_dir: Path | None) -> str:
    """Find and linkify bare file paths in a text segment.

    A token is considered a file path if:
    - It contains at least one ``/``
    - It ends with a known extension
    - If *base_dir* is given, the file must exist
    """
    # Split on whitespace boundaries to find path-like tokens
    # We process word-by-word to avoid partial matches
    tokens = re.split(r"(\s+)", segment)
    result: list[str] = []

    for token in tokens:
        linked = _try_linkify_path(token, base_dir)
        result.append(linked if linked else token)

    return "".join(result)


def _try_linkify_path(token: str, base_dir: Path | None) -> str | None:
    """Try to linkify a single token as a file path.

    Returns the markdown link string, or None if the token is not a file path.
    """
    # Strip leading/trailing punctuation that isn't part of the path
    prefix = ""
    suffix = ""
    stripped = token

    # Strip common leading chars
    while stripped and stripped[0] in "([\"'":
        prefix += stripped[0]
        stripped = stripped[1:]

    # Strip common trailing chars
    while stripped and stripped[-1] in ")]\"'.,;:!?":
        suffix = stripped[-1] + suffix
        stripped = stripped[:-1]

    if not stripped:
        return None

    # Must contain a path separator
    if "/" not in stripped and "\\" not in stripped:
        return None

    # Normalize to forward slashes for extension check
    normalized = stripped.replace("\\", "/")

    # Must end with a known extension
    if not _has_linkable_extension(normalized):
        return None

    # Must not look like a URL (already handled)
    if re.match(r"https?://", stripped):
        return None

    # If base_dir is provided, verify file exists
    if base_dir is not None:
        try:
            candidate = (base_dir / stripped).resolve()
            # Security: must be within base_dir
            if not str(candidate).startswith(str(base_dir.resolve())):
                return None
            if not candidate.is_file():
                return None
        except (OSError, ValueError):
            return None

    # Build markdown link with forward slashes (for dashboard API)
    link_target = normalized
    return f"{prefix}[{stripped}]({link_target}){suffix}"


def _has_linkable_extension(path: str) -> bool:
    """Check if a path ends with a known linkable extension."""
    lower = path.lower()
    return any(lower.endswith(ext) for ext in LINKABLE_EXTENSIONS)
