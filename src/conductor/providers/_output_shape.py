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
from typing import Any, Literal

from conductor.config.schema import OutputField
from conductor.exceptions import ValidationError
from conductor.executor.output import check_type

logger = logging.getLogger(__name__)

ScalarOutputType = Literal["string", "number", "boolean"]
"""The ``OutputField.type`` values a wrapper object can be unwrapped into."""

# Only scalar targets are unwrapped. A dict arriving for an ``object`` field is
# plausibly correct, and for ``array`` there is no unambiguous single value.
_UNWRAPPABLE_TYPES: tuple[ScalarOutputType, ...] = ("string", "number", "boolean")

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
    unwrapped: dict[str, Any] | None = None
    for field_name, field_def in schema.items():
        if field_def.type not in _UNWRAPPABLE_TYPES:
            continue

        value = content.get(field_name)
        if not isinstance(value, dict) or not value:
            continue

        # Two or more matches are ambiguous — see the docstring.
        candidates = _find_scalar_candidates(value, field_name, field_def.type)
        if len(candidates) != 1:
            continue
        candidate = candidates[0]

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


def _find_scalar_candidates(
    wrapper: dict[str, Any],
    field_name: str,
    expected_type: str,
) -> list[Any]:
    """Collect the values in ``wrapper`` that could be the declared scalar.

    Args:
        wrapper: The object that arrived where a scalar was expected.
        field_name: Name of the declared field, used for the echo match.
        expected_type: Declared scalar type a candidate must already satisfy.

    Returns:
        Every matching value. Exactly one means the wrapper is unambiguous;
        anything else means the caller must leave the field alone.
    """
    # dict.fromkeys dedupes while preserving order: a field literally named
    # "value" or "result" would otherwise occupy two slots and be rejected as
    # ambiguous against itself.
    candidate_keys = dict.fromkeys((field_name, *_GENERIC_VALUE_KEYS))
    return [
        wrapper[key]
        for key in candidate_keys
        if key in wrapper and check_type(wrapper[key], expected_type)
    ]
