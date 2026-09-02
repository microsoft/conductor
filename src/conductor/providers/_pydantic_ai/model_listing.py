"""Vendor-agnostic parser for model-listing token limits.

Read what the endpoint advertises; never guess.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelTokenLimits:
    """Token limits advertised for a single model entry in a vendor listing."""

    max_input_tokens: int | None = None
    """Maximum prompt/context size in tokens, if advertised."""

    max_output_tokens: int | None = None
    """Maximum completion/output size in tokens, if advertised."""


def extract_model_token_limits(entry: Any) -> ModelTokenLimits:
    """Extract token limits from a single vendor model-list entry.

    The function is intentionally defensive: it accepts any shape, never
    raises, and returns ``None`` for any field that is not advertised as a
    positive integral value.

    Input/window token priority (first match wins):

    1. ``max_input_tokens``
    2. ``context_length``
    3. ``max_model_len``
    4. ``max_context_length``

    Output token priority:

    1. ``max_output_tokens``
    2. ``top_provider.max_completion_tokens`` (attribute or mapping access)

    Values are accepted only when they are positive integers, or floats that
    represent an integral value greater than zero. Booleans and negative or
    non-numeric values are ignored.

    Args:
        entry: A model-list entry from a vendor API response. Expected to be
            a mapping or object with the fields above, but any value is allowed.

    Returns:
        A :class:`ModelTokenLimits` with the advertised limits (``None`` for
        missing or malformed fields).
    """
    input_limit = _resolve_input_limit(entry)
    output_limit = _resolve_output_limit(entry)
    return ModelTokenLimits(
        max_input_tokens=input_limit,
        max_output_tokens=output_limit,
    )


def _resolve_input_limit(entry: Any) -> int | None:
    """Resolve the input/context token limit from ``entry``."""
    value = _coerce_mapping_value(entry, _INPUT_KEYS)
    return _to_positive_int(value)


def _resolve_output_limit(entry: Any) -> int | None:
    """Resolve the output/completion token limit from ``entry``."""
    value = _coerce_mapping_value(entry, ("max_output_tokens",))
    if value is None:
        top_provider = _coerce_mapping_value(entry, ("top_provider",))
        if top_provider is not None:
            value = _coerce_mapping_value(top_provider, ("max_completion_tokens",))
    return _to_positive_int(value)


#: Priority-ordered keys used to resolve the input/context token limit.
_INPUT_KEYS: tuple[str, ...] = (
    "max_input_tokens",
    "context_length",
    "max_model_len",
    "max_context_length",
)


def _coerce_mapping_value(obj: Any, keys: tuple[str, ...]) -> Any:
    """Return the first available value for ``keys`` from ``obj``.

    ``obj`` may be a mapping or any object exposing the key as an attribute.
    Returns ``None`` when no key is present or ``obj`` is not accessible.
    """
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        for key in keys:
            try:
                is_present = key in obj
            except Exception:  # noqa: BLE001 - vendor metadata parsing must never raise
                continue
            if not is_present:
                continue
            try:
                return obj[key]
            except Exception:  # noqa: BLE001 - vendor metadata parsing must never raise
                continue
        return None
    for key in keys:
        try:
            value = getattr(obj, key)
        except Exception:  # noqa: BLE001 - vendor metadata parsing must never raise
            value = None
        if value is not None:
            return value
    return None


def _to_positive_int(value: Any) -> int | None:
    """Return ``value`` as a positive integer, or ``None`` if invalid.

    Rejects booleans, non-numeric values, and non-positive numbers. Accepts
    floats only when they represent a positive integral value.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value <= 0:
            return None
        return value
    if isinstance(value, float):
        if value <= 0:
            return None
        if not value.is_integer():
            return None
        return int(value)
    return None
