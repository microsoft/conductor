"""Shared, provider-neutral normalization of agent output before validation.

Models sometimes satisfy a scalar output field by returning a small object
around the value (``{"decision": {"decision": "APPROVE"}}``) instead of the
bare scalar. The response is well-formed JSON and only the *shape* is wrong,
so it is recoverable without a second model round-trip.

This module is deliberately provider-side rather than part of
:func:`~conductor.executor.output.validate_output`. That function also
validates ``set`` and ``script`` step output, where silently reshaping a
value the workflow author wrote by hand would be surprising. Validation stays
strict everywhere; only provider responses are normalized, and only on the
narrow cases below.
"""

from __future__ import annotations

import logging
from typing import Any

from conductor.config.schema import OutputField
from conductor.exceptions import ValidationError

logger = logging.getLogger(__name__)

# Only scalar targets are unwrapped. A dict arriving for an ``object`` field is
# plausibly correct, and for ``array`` there is no unambiguous single value.
_UNWRAPPABLE_TYPES = ("string", "number", "boolean")

# Generic keys models use when they wrap a value without echoing the field name.
_GENERIC_VALUE_KEYS = ("value", "result")


def normalize_agent_output(
    content: Any,
    schema: dict[str, OutputField],
) -> dict[str, Any]:
    """Prepare a parsed agent response for schema validation.

    Guarantees the caller a ``dict``. Handed a bare scalar, ``validate_output``
    either raises ``TypeError`` from a membership test (numbers, booleans,
    ``null``) or reports a misleading "missing required field" (strings and
    arrays, where ``in`` is a valid substring or element test). Neither is
    actionable, so a non-object response is raised as :class:`ValidationError`
    and re-prompted like any other wrong shape.

    Args:
        content: Parsed JSON from the model. Any type.
        schema: Declared output schema for the agent.

    Returns:
        The content as a dict, with wrapper-shaped scalars unwrapped.

    Raises:
        ValidationError: If ``content`` is not a JSON object.
    """
    if not isinstance(content, dict):
        raise ValidationError(
            f"Agent returned a JSON {type(content).__name__}, not an object",
            suggestion=(
                f"Return a JSON object with the declared fields: {list(schema)}. "
                "Arrays and bare scalars are not accepted."
            ),
        )
    return unwrap_scalar_wrappers(content, schema)


def unwrap_scalar_wrappers(
    content: dict[str, Any],
    schema: dict[str, OutputField],
) -> dict[str, Any]:
    """Unwrap single-value objects returned where a scalar was declared.

    Conservative by design. A field is only rewritten when the schema expects
    a scalar, a ``dict`` arrived, and **exactly one** candidate key inside it
    holds a value of the expected type. Candidate keys are the field's own
    name and the generic ``value`` / ``result`` keys — nothing else, so an
    object like ``{"error": "I could not complete the task"}`` is left alone
    and re-prompted rather than laundered into a plausible-looking answer.

    Two or more matching candidates count as ambiguous and are also left
    alone: guessing between them is how a wrong answer becomes a silent one.

    Args:
        content: Parsed agent output. Not mutated.
        schema: Declared output schema for the agent.

    Returns:
        A new dict when any field was unwrapped, otherwise ``content``
        unchanged (the same object, not a copy).
    """
    if not isinstance(content, dict):
        return content

    unwrapped: dict[str, Any] | None = None
    for field_name, field_def in schema.items():
        if field_def.type not in _UNWRAPPABLE_TYPES:
            continue

        value = content.get(field_name)
        if not isinstance(value, dict) or not value:
            continue

        candidate = _find_scalar_candidate(value, field_name, field_def.type)
        if candidate is _NO_CANDIDATE:
            continue

        if unwrapped is None:
            unwrapped = dict(content)
        unwrapped[field_name] = candidate
        discarded = sorted(k for k in value if value[k] is not candidate)
        logger.warning(
            "Unwrapped object returned for scalar output field '%s' "
            "(expected %s); using inner value %r%s",
            field_name,
            field_def.type,
            candidate,
            f", discarding keys {discarded}" if discarded else "",
        )

    return unwrapped if unwrapped is not None else content


class _NoCandidate:
    """Sentinel type distinguishing "no match" from a legitimate ``None``."""


_NO_CANDIDATE = _NoCandidate()


def _find_scalar_candidate(
    wrapper: dict[str, Any],
    field_name: str,
    expected_type: str,
) -> Any:
    """Find the single unambiguous scalar inside ``wrapper``.

    Args:
        wrapper: The object that arrived where a scalar was expected.
        field_name: Name of the declared field, used for the echo match.
        expected_type: Declared scalar type the candidate must already satisfy.

    Returns:
        The unwrapped value, or the ``_NO_CANDIDATE`` sentinel when no
        candidate matches or more than one does.
    """
    # dict.fromkeys dedupes while preserving order: a field literally named
    # "value" or "result" would otherwise occupy two slots and be rejected as
    # ambiguous against itself.
    candidate_keys = dict.fromkeys((field_name, *_GENERIC_VALUE_KEYS))
    matches = [
        wrapper[key]
        for key in candidate_keys
        if key in wrapper and _matches_scalar_type(wrapper[key], expected_type)
    ]
    if len(matches) != 1:
        return _NO_CANDIDATE
    return matches[0]


def _matches_scalar_type(value: Any, expected: str) -> bool:
    """Check a candidate against the declared scalar type.

    Mirrors :func:`~conductor.executor.output._check_type` for the scalar
    cases so an unwrap never produces a value validation would then reject.

    Args:
        value: Candidate value pulled out of the wrapper object.
        expected: Declared scalar type name.

    Returns:
        True when ``value`` already satisfies ``expected``.
    """
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    # bool is a subclass of int in Python; exclude it from the numeric type.
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return False
