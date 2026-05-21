"""Python helper for raising typed Conductor error envelopes.

Contract: write a single JSON object to ``$CONDUCTOR_ERROR_OUT`` and
exit ``0``. Conductor reads the file, treats the node as raised, and
evaluates ``on_error`` routes against the envelope.

Usage::

    import conductor_error
    conductor_error.raise_kind(
        "external.git.fetch_failed",
        "remote rejected push",
        details={"remote": "origin", "exit": 128},
    )
    raise SystemExit(0)

The helper deliberately does NOT call :func:`sys.exit`; callers stay
in charge of process exit so they can do their own teardown before
returning control to Conductor.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def raise_kind(
    kind: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> Path:
    """Write a typed error envelope to ``$CONDUCTOR_ERROR_OUT``.

    Args:
        kind: Dotted-namespace error identifier (for example
            ``"external.git.fetch_failed"``). Must match the contract
            documented in the workflow's ``raises`` declaration.
        message: Human-readable description of what went wrong.
        details: Optional structured context. Must be JSON-serialisable.

    Returns:
        The path the envelope was written to.

    Raises:
        RuntimeError: If ``$CONDUCTOR_ERROR_OUT`` is not set, which
            indicates the script is being run outside of a
            Conductor script-type node.
    """
    out = os.environ.get("CONDUCTOR_ERROR_OUT")
    if not out:
        raise RuntimeError(
            "CONDUCTOR_ERROR_OUT is not set; "
            "this script must be run by Conductor as a script-type node."
        )

    envelope: dict[str, Any] = {
        "conductor_error": True,
        "kind": kind,
        "message": message,
    }
    if details is not None:
        envelope["details"] = details

    path = Path(out)
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return path
