"""Typed error envelopes and helpers for ``on_error`` routing.

The "envelope" is the runtime representation of a node-level raise:

.. code-block:: python

    {
        "kind":    "external.git.fetch_failed",
        "message": "git fetch origin failed (exit 128)",
        "details": {"exit_code": 128, "stderr_tail": "fatal: ..."},
    }

The on-the-wire shape (what scripts write to ``$CONDUCTOR_ERROR_OUT``
and what agents emit as their JSON response) additionally carries a
``conductor_error: true`` discriminator. :func:`coerce_envelope`
strips the discriminator and validates the structure, producing the
internal :class:`ErrorEnvelope` shape.

Three reserved synthetic kinds are produced by the runtime:

- :func:`make_script_error` → ``internal.script_error`` when a script
  exits non-zero without writing an envelope (and the node opts in via
  ``raises`` or any ``on_error`` route).
- :func:`make_schema_violation` → ``internal.schema_violation`` when an
  agent's output fails its declared ``output:`` schema.
- :func:`wrap_undeclared_kind` → ``internal.undeclared_kind`` when a
  node with ``raises:`` raises a kind not in its declared list. The
  original kind is preserved under ``details.original_kind``.

See ``docs/projects/error-routing/on-error-routing.brainstorm.md``
(D1, D2) for the full design.
"""

from __future__ import annotations

from typing import Any, TypedDict

from conductor.error_kinds import KIND_PATTERN


class ErrorEnvelope(TypedDict):
    """Internal representation of a node-level raise.

    All three fields are present on every envelope after coercion.
    ``details`` may be an empty dict but is never absent — that keeps
    template access like ``{{ failing_node.error.details.foo }}``
    safe even when the author didn't include details.
    """

    kind: str
    message: str
    details: dict[str, Any]


class EnvelopeValidationError(ValueError):
    """Raised when raw envelope input fails structural validation.

    Distinct from :class:`conductor.exceptions.ValidationError` so the
    engine can catch and translate this into an
    ``internal.schema_violation`` or ``internal.script_error``
    synthetic envelope, rather than halting the workflow with a
    generic configuration error.
    """


def coerce_envelope(raw: Any) -> ErrorEnvelope:
    """Validate and normalize raw envelope input into an :class:`ErrorEnvelope`.

    Accepts the on-the-wire shape (with or without the
    ``conductor_error: true`` discriminator) and returns the internal
    shape with the discriminator stripped and ``details`` defaulted to
    ``{}``.

    Args:
        raw: A dict that should describe an envelope. Anything else
            raises :class:`EnvelopeValidationError`.

    Returns:
        A clean :class:`ErrorEnvelope`.

    Raises:
        EnvelopeValidationError: If ``raw`` is not a dict, is missing
            required fields, or has malformed values.
    """
    if not isinstance(raw, dict):
        raise EnvelopeValidationError(f"envelope must be a JSON object, got {type(raw).__name__}")

    kind = raw.get("kind")
    if not isinstance(kind, str) or not kind:
        raise EnvelopeValidationError("envelope.kind must be a non-empty string")
    if not KIND_PATTERN.match(kind):
        raise EnvelopeValidationError(
            f"envelope.kind '{kind}' must be a dotted lowercase identifier "
            "(e.g. 'external.git.fetch_failed')"
        )

    message = raw.get("message")
    if not isinstance(message, str) or not message:
        raise EnvelopeValidationError("envelope.message must be a non-empty string")

    details = raw.get("details", {})
    if details is None:
        details = {}
    if not isinstance(details, dict):
        raise EnvelopeValidationError(
            f"envelope.details must be a JSON object, got {type(details).__name__}"
        )

    return ErrorEnvelope(kind=kind, message=message, details=details)


def make_script_error(
    *,
    exit_code: int,
    stderr_tail: str,
    command: str,
) -> ErrorEnvelope:
    """Synthesize an ``internal.script_error`` envelope.

    Used when a script exits non-zero, does not write an envelope, AND
    the node has opted into error routing (``raises`` or any
    ``on_error`` route present). Without opt-in the engine preserves
    legacy ``exit_code`` routing.

    Args:
        exit_code: The non-zero exit code.
        stderr_tail: Last N characters of stderr (truncated for sanity).
        command: The rendered script command for diagnostic context.
    """
    return ErrorEnvelope(
        kind="internal.script_error",
        message=stderr_tail.strip().splitlines()[-1]
        if stderr_tail.strip()
        else f"script exited with code {exit_code}",
        details={
            "exit_code": exit_code,
            "stderr_tail": stderr_tail,
            "command": command,
        },
    )


def make_schema_violation(
    *,
    node_name: str,
    source: str,
    original_message: str,
    failed_field: str | None = None,
) -> ErrorEnvelope:
    """Synthesize an ``internal.schema_violation`` envelope.

    Used when an agent's output fails ``output:`` schema validation
    (Phase 1) or a script's structured output fails its schema.

    Args:
        node_name: The name of the node whose output failed.
        source: ``"agent"`` or ``"script"``.
        original_message: Message from the underlying
            :class:`conductor.exceptions.ValidationError`.
        failed_field: Optional name of the offending field if known.
    """
    details: dict[str, Any] = {
        "node": node_name,
        "source": source,
        "original_message": original_message,
    }
    if failed_field is not None:
        details["failed_field"] = failed_field
    return ErrorEnvelope(
        kind="internal.schema_violation",
        message=f"{source} '{node_name}' output failed schema validation: {original_message}",
        details=details,
    )


def wrap_undeclared_kind(
    original: ErrorEnvelope,
    *,
    declared: list[str],
) -> ErrorEnvelope:
    """Wrap an envelope whose ``kind`` isn't in the node's ``raises`` list.

    Preserves the original kind under ``details.original_kind`` and the
    original details under ``details.original_details`` so an author
    handling ``internal.undeclared_kind`` can still recover the intent.

    Args:
        original: The envelope as raised by the node.
        declared: The node's ``raises`` list, for diagnostics.

    Returns:
        A new envelope with kind ``internal.undeclared_kind``.
    """
    return ErrorEnvelope(
        kind="internal.undeclared_kind",
        message=(
            f"node raised kind '{original['kind']}' which is not in its declared "
            f"raises list ({', '.join(declared)})"
        ),
        details={
            "original_kind": original["kind"],
            "original_message": original["message"],
            "original_details": original["details"],
            "declared": list(declared),
        },
    )
