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

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Defensive size cap: above this many characters, ``linkify_markdown`` skips
# linkification entirely (still normalizing whitespace) rather than paying
# for a full scan. With the O(n) fixes below, real gate-prompt-sized text
# never approaches this — it exists as insurance against a future
# pathological shape, not as the mechanism removing the current O(n^2) cost.
MAX_LINKIFY_CHARS = 256_000

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
    }
)

# Pre-computed tuple for fast str.endswith() checks (no Python-level loop).
_LINKABLE_SUFFIXES = tuple(LINKABLE_EXTENSIONS)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Fenced code block (``` or ~~~, with optional language tag).
#
# The trailing `+` on the opener group (`` `{3,}+ `` / `~{3,}+`) is a
# *possessive* quantifier, not a typo. Without it, a long run of backticks
# (or tildes) with no closing fence makes the regex engine backtrack the
# opener one character at a time — trying `` ` * n ``, then `` ` * (n-1) ``,
# and so on — which is O(n^2) and stalls the event loop on a ~80KB run of
# backticks. The possessive quantifier forbids backtracking into the run it
# just matched, so the opener is consumed exactly once instead of being
# retried at every shorter length — O(n) overall instead of O(n^2).
# Do not "simplify" this back to `{3,}` — see
# tests/test_executor/test_linkify.py::TestPathologicalInputPerformance.
#
# Semantic side effect: possessiveness also means a fence whose opener is
# longer than its closer (e.g. a 4-backtick opener followed by a 3-backtick
# closer) is no longer protected — the old greedy `{3,}` would backtrack the
# opener down to match the shorter closer, but `{3,}+` forbids that, so the
# regex fails to match and the block's contents get linkified. This is the
# CommonMark-correct reading (a closing fence must be at least as long as
# the opener) but is a behaviour change from the pre-fix regex — see
# test_longer_opener_than_closer_no_longer_protects.
#
# The `[^\n]*\n` after the opener group is semantically inert given the
# `^...^\1` anchoring under re.MULTILINE (a successful match already implies
# a newline follows the opener), and is retained only for explicitness.
_FENCED_CODE_RE = re.compile(r"^(`{3,}+|~{3,}+)[^\n]*\n.*?^\1", re.MULTILINE | re.DOTALL)

# Inline code span (`...`)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# Bare URL: http(s)://... terminated at whitespace or common punctuation
_URL_RE = re.compile(
    r"(?<![(\[])"  # not preceded by ( or [
    r"https?://[^\s)<>\]\[\"'`]+"
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

    Text longer than ``MAX_LINKIFY_CHARS`` skips steps 2 and 3 entirely
    (whitespace is still normalized) rather than paying for a full scan.

    Args:
        text: Rendered template text (may contain markdown).
        base_dir: Optional directory for file existence checks.  When
            provided, only paths that resolve to an existing file within
            *base_dir* are linkified.

    Returns:
        Text with bare paths/URLs wrapped in markdown link syntax.
    """
    # Step 1: normalize whitespace (linear, safe even above the cap — and
    # skipping it would visibly change rendering of exactly the large
    # prompts this cap is meant to protect).
    text = _normalize_whitespace(text)

    if len(text) > MAX_LINKIFY_CHARS:
        logger.debug(
            "linkify_markdown: text length %d exceeds MAX_LINKIFY_CHARS (%d); "
            "skipping linkification",
            len(text),
            MAX_LINKIFY_CHARS,
        )
        return text

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

    for pattern in (_FENCED_CODE_RE, _INLINE_CODE_RE):
        for m in pattern.finditer(text):
            protected.append((m.start(), m.end()))

    protected.extend(_find_existing_link_spans(text))

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


def _find_existing_link_spans(text: str) -> list[tuple[int, int]]:
    """Find spans of existing markdown links: ``[text](url)`` or ``[text][ref]``.

    This is a single forward pass with :meth:`str.find`, exactly equivalent
    to (and replacing) the regex
    ``r"\\[[^\\]]*\\]\\([^)]*\\)|\\[[^\\]]*\\]\\[[^\\]]*\\]"`` — but linear
    instead of quadratic, both on a long run of ``[`` characters and on a
    ``"[](" * k`` shape (a failed ``find`` can never succeed from a later
    start position, so ``last_bracket``/``last_paren`` guard each lookup
    once the rest of the string is known to hold no more delimiters).

    The equivalence argument: for any ``[`` at position *i*, the character
    class ``[^\\]]*`` in both regex alternatives is greedy but excludes
    ``]``, so it always stops at the *same* first ``]`` found after *i* —
    there is no backtracking freedom there. That means every ``[`` between
    *i* and that first ``]`` shares the identical next ``]`` and the
    identical following character, so if the attempt starting at *i* fails,
    every attempt starting between *i* and that ``]`` would fail for the
    exact same reason. Rather than retrying each of those ``[`` positions
    (which is what makes the regex quadratic on a ``[`` * n / ``]`` * n
    wall), this scanner jumps the cursor straight past the ``]`` once a
    failure is established. Fuzz-verified against the regex it replaces
    over 100,000+ randomly generated strings with 0 differences.

    Returns:
        A list of ``(start, end)`` spans, in the same order ``finditer``
        would yield them.
    """
    spans: list[tuple[int, int]] = []
    n = len(text)
    # A failed `find` can never succeed from a later start position, so
    # precompute the last occurrence of each delimiter and skip the lookup
    # entirely once we're past it — otherwise a shape like "[](" * k makes
    # every failed find(")", ...) rescan to the end of the string while the
    # cursor only advances ~3 characters, which is O(n^2).
    last_bracket = text.rfind("]")
    last_paren = text.rfind(")")
    i = text.find("[")
    while i != -1:
        close_bracket = text.find("]", i + 1) if last_bracket > i else -1
        if close_bracket == -1:
            # No more "]" anywhere after this "[" — no "[" at or after this
            # position can ever find one either, so nothing further can match.
            break

        matched = False
        next_pos = close_bracket + 1
        if next_pos < n and text[next_pos] == "(":
            close_paren = text.find(")", next_pos + 1) if last_paren > next_pos else -1
            if close_paren != -1:
                spans.append((i, close_paren + 1))
                i = text.find("[", close_paren + 1)
                matched = True

        if not matched and next_pos < n and text[next_pos] == "[":
            second_close = text.find("]", next_pos + 1) if last_bracket > next_pos else -1
            if second_close != -1:
                spans.append((i, second_close + 1))
                i = text.find("[", second_close + 1)
                matched = True

        if not matched:
            # Both alternatives failed for this "[" — and, by the argument
            # above, for every "[" between here and close_bracket — so skip
            # straight past it instead of retrying each intervening "[".
            i = text.find("[", close_bracket + 1)

    return spans


def _linkify_segment(segment: str, base_dir: Path | None) -> str:
    """Linkify bare URLs and file paths in an unprotected text segment."""
    # First pass: linkify URLs
    segment = _URL_RE.sub(_wrap_url, segment)
    # Second pass: linkify file paths
    segment = _linkify_file_paths(segment, base_dir)
    return segment


def _wrap_url(m: re.Match[str]) -> str:
    """Wrap a bare URL in markdown autolink syntax."""
    full = m.group(0)
    end = len(full)
    # Strip trailing punctuation that's unlikely part of the URL. Walking an
    # index backward instead of repeatedly slicing (`url = url[:-1]`) avoids
    # re-copying the shrinking string on every iteration, which is O(n^2) on
    # a long trailing-punctuation run.
    while end > 0 and full[end - 1] in ".,;:!?)":
        # Keep ) only if there's a matching ( in the URL (e.g. Wikipedia
        # links). This branch is currently unreachable in practice — _URL_RE's
        # character class excludes ")", so a URL match can never end in one —
        # retained defensively in case that ever changes.
        if full[end - 1] == ")" and "(" in full[:end]:
            break
        end -= 1
    url = full[:end]
    trailing = full[end:]
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


_LEADING_STRIP = "([\"'"
_TRAILING_STRIP = ")]\"'.,;:!?"


def _try_linkify_path(token: str, base_dir: Path | None) -> str | None:
    """Try to linkify a single token as a file path.

    Returns the markdown link string, or None if the token is not a file path.
    """
    # Strip leading/trailing punctuation that isn't part of the path.
    # str.lstrip/rstrip are implemented in C and single-pass; a manual
    # char-at-a-time loop with `token = token[1:]` / `token[:-1]` is O(n^2)
    # on a token consisting entirely of strippable characters.
    body = token.lstrip(_LEADING_STRIP)
    prefix = token[: len(token) - len(body)]
    stripped = body.rstrip(_TRAILING_STRIP)
    suffix = body[len(stripped) :]

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
            resolved_base = base_dir.resolve()
            candidate = (base_dir / normalized).resolve()
            # Security: must be within base_dir (path-aware containment, not
            # string-prefix — `/foo/bar` must not match `/foo/barbaz/...`).
            if not candidate.is_relative_to(resolved_base):
                return None
            if not candidate.is_file():
                return None
        except (OSError, ValueError):
            return None

    # Build markdown link with forward slashes (for dashboard API)
    return f"{prefix}[{stripped}]({normalized}){suffix}"


def _has_linkable_extension(path: str) -> bool:
    """Check if a path ends with a known linkable extension."""
    return path.lower().endswith(_LINKABLE_SUFFIXES)
