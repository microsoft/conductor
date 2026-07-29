"""Shared, provider-neutral normalization of wrapper-shaped agent output.

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

logger = logging.getLogger(__name__)

# Only scalar targets are unwrapped. A dict arriving for an ``object`` field is
# plausibly correct, and for ``array`` there is no unambiguous single value.
_UNWRAPPABLE_TYPES = ("string", "number", "boolean")

# Generic keys models use when they wrap a value without echoing the field name.
_GENERIC_VALUE_KEYS = ("value", "result")


def unwrap_scalar_wrappers(
    content: dict[str, Any],
    schema: dict[str, OutputField],
) -> dict[str, Any]:
    """Unwrap single-value objects returned where a scalar was declared.

    Conservative by design: a field is only rewritten when the schema expects
    a scalar, a ``dict`` arrived, and exactly one unambiguous candidate inside
    it already has the expected type. Anything else is left untouched so the
    caller's recovery loop re-prompts rather than guessing.

    Candidates are tried in order:

    1. a key matching the field name (``{"decision": {"decision": "APPROVE"}}``)
    2. a generic ``value`` or ``result`` key
    3. a sole key, whatever it is named

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
        logger.warning(
            "Unwrapped object returned for scalar output field '%s' "
            "(expected %s); using inner value %r",
            field_name,
            field_def.type,
            candidate,
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
        candidate is unambiguous.
    """
    keys_to_try = [field_name, *_GENERIC_VALUE_KEYS]
    if len(wrapper) == 1:
        keys_to_try.append(next(iter(wrapper)))

    for key in keys_to_try:
        if key not in wrapper:
            continue
        value = wrapper[key]
        if _matches_scalar_type(value, expected_type):
            return value

    return _NO_CANDIDATE


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
