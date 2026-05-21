"""Error kind constants and validation helpers for typed error envelopes.

The "kind" of an error is a flat dotted lowercase identifier (e.g.
``external.git.fetch_failed``) authored by workflow authors at the
failure site. The runtime never infers a kind; it carries the authored
kind verbatim or, in a small set of well-defined situations, synthesizes
a reserved kind.

See ``docs/projects/error-routing/on-error-routing.brainstorm.md`` for
the full design.
"""

from __future__ import annotations

import re

KIND_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+$")
"""Pattern for an error kind: at least one dot, lowercase segments only.

Examples that match: ``external.git.fetch_failed``, ``policy.budget_exceeded``.
Examples that do NOT match: ``oops`` (no dot), ``Git.Fetch`` (uppercase),
``.leading_dot``, ``trailing_dot.``.
"""

RESERVED_KIND_PREFIXES: tuple[str, ...] = (
    "internal.",
    "provider.",
    "subworkflow.",
    "retry.",
)
"""Prefixes the runtime reserves for synthetic envelopes.

Workflow authors cannot declare a kind under these prefixes in their
``raises:`` list. The runtime may emit kinds under these prefixes
(e.g. ``internal.script_error``, ``internal.schema_violation``) when
classifying its own failures.
"""

RESERVED_ON_ERROR_ALLOWLIST: frozenset[str] = frozenset(
    {
        "internal.script_error",
        "internal.schema_violation",
        "internal.undeclared_kind",
    }
)
"""Reserved kinds that are legal to match in ``on_error`` even though
they cannot be declared in ``raises``.

This is the closed set of runtime-synthesized kinds in Phase 1. Phase 2
will add ``subworkflow.*`` propagation kinds and Phase 3 will add
``provider.exhausted``.
"""


def is_reserved_prefix(kind: str) -> bool:
    """Return True if ``kind`` begins with a reserved prefix."""
    return any(kind.startswith(prefix) for prefix in RESERVED_KIND_PREFIXES)
