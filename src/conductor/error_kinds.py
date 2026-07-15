"""Shared validation for typed workflow error kinds."""

from __future__ import annotations

import re

KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
RESERVED_KIND_PREFIXES = ("internal.", "provider.", "subworkflow.", "retry.")


def validate_error_kind(kind: str) -> str:
    """Validate and return a dotted lowercase error kind."""
    if not KIND_PATTERN.fullmatch(kind):
        raise ValueError(
            f"error kind '{kind}' must be a dotted lowercase identifier "
            "(for example, 'external.git.fetch_failed')"
        )
    return kind


def is_reserved_error_kind(kind: str) -> bool:
    """Return whether an error kind belongs to an engine-owned namespace."""
    return kind.startswith(RESERVED_KIND_PREFIXES)
