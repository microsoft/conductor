"""The single definition of the fleet ``run_id`` contract.

A ``run_id`` is minted once (by :class:`conductor.engine.event_log.EventLogSubscriber`,
or reused verbatim from a checkpoint on resume) and from then on is
interpolated into filenames by several independent modules: the events
JSONL, the ``--web-bg`` capture logs (``cli/bg_runner.py``), and the fleet
run-record store (``fleet/records.py``). Every one of those interpolation
sites, and every filename *parser* that later recovers a ``run_id`` from
one of those names (``fleet/history.py``, ``fleet/retention.py``), must
agree on exactly the same shape -- a mismatch here doesn't just reject a
value, it makes a parent poll (or a retention sweep, or a History-screen
lookup) for a key the child never writes.

This is a stdlib-only leaf module (no conductor imports) so it can be
imported from :mod:`conductor.engine.event_log` — an always-on module
that must not depend on :mod:`conductor.fleet.records`. Importing
``conductor.fleet.records`` directly from ``event_log.py`` would import
``conductor.cli.pid``, and ``conductor.cli.__init__`` does ``from
conductor.cli.app import app``, closing an import cycle: exactly the
hazard :mod:`conductor.rundir` documents for the same reason.
"""

from __future__ import annotations

import re
import secrets

RUN_ID_PATTERN_SOURCE = r"[A-Za-z0-9_-]{1,200}"
"""The one run-id shape, as a *source string* so filename regexes that need
to anchor a ``run_id`` inside a larger pattern (e.g. ``fleet/history.py``'s
filename parser) can interpolate it directly instead of restating it."""

RUN_ID_RE = re.compile(rf"\A{RUN_ID_PATTERN_SOURCE}\Z")

# A run_id is interpolated directly into a filename (``<run_id>.json``,
# ``conductor-<name>-<ts>-<run_id>.events.jsonl``), so it must be validated
# before use in a path: ``.`` is excluded from the safe set, which rules out
# both ``..`` traversal and a value like ``"foo/../../etc"``; ``/`` and
# ``\`` are excluded outright, which rules out absolute paths and
# separators.


def is_valid_run_id(run_id: str) -> bool:
    """Return True if ``run_id`` is safe to use as a filename component.

    This is the single, authoritative "effective run-id" contract, adopted
    verbatim (no case-folding) by every validator, minter, and filename
    parser in the codebase. A resumed run reuses a checkpoint's ``run_id``
    exactly as written (``EventLogSubscriber``'s ``existing_path``/
    ``existing_run_id`` branch performs no format check of its own), so
    this rule has to be broad enough to accept whatever a legitimate
    checkpoint carries, not just what :func:`new_run_id` mints.

    Args:
        run_id: The run identifier to check.

    Returns:
        True if ``run_id`` matches the path-safe pattern used to key run
        record files and event log filenames.
    """
    return bool(RUN_ID_RE.fullmatch(run_id))


def new_run_id() -> str:
    """Mint a fresh run id.

    Returns:
        An 8-character lowercase hex string (``secrets.token_hex(4)``).
        This is only the *default* shape a freshly generated id takes --
        :func:`is_valid_run_id` accepts a much broader set, since a
        resumed run may carry forward a checkpoint's id verbatim.
    """
    return secrets.token_hex(4)
