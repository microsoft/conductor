"""Duration parsing for Conductor workflow steps.

This module provides ``parse_duration`` for converting user-supplied
duration values (plain numbers or suffixed strings like ``"5m"``,
``"500ms"``, ``"1h"``) into a float seconds value. Used by the
``wait`` step type and shared as a primitive for other duration-aware
features.

The parser raises :class:`ValueError` on invalid input so it nests
cleanly inside Pydantic ``ValidationError`` when called from schema
validators. Bounds checks (e.g., > 0, 24h cap) are intentionally left
to callers so the same parser can be reused for different policies.
"""

from __future__ import annotations

import re

_DURATION_PATTERN = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|m|h)?\s*$")

_UNIT_TO_SECONDS: dict[str, float] = {
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}


def parse_duration(value: str | int | float) -> float:
    """Parse a duration value into seconds.

    Accepts:
        * Plain ``int`` or ``float`` — interpreted as seconds.
        * Strings matching ``<number><unit>`` where unit is one of
          ``ms``, ``s``, ``m``, ``h``. Whitespace around the value and
          between number/unit is tolerated. Omitting the unit defaults
          to seconds.

    Examples::

        parse_duration(60)        # 60.0
        parse_duration(1.5)       # 1.5
        parse_duration("60")      # 60.0
        parse_duration("60s")     # 60.0
        parse_duration("5m")      # 300.0
        parse_duration("1h")      # 3600.0
        parse_duration("500ms")   # 0.5
        parse_duration("2.5m")    # 150.0

    Args:
        value: The duration to parse.

    Returns:
        Duration in seconds as a ``float``.

    Raises:
        ValueError: If ``value`` is not a recognized duration. The
            message is suitable for surfacing directly to a user in a
            Pydantic ``ValidationError``.
    """
    # Reject bool explicitly — Pydantic v2 / Python treat ``True`` as ``int(1)``
    # in many contexts, but accepting it here is almost certainly a mistake.
    if isinstance(value, bool):
        raise ValueError(f"duration must be a number or duration string, not boolean: {value!r}")

    if isinstance(value, (int, float)):
        return float(value)

    if not isinstance(value, str):
        raise ValueError(
            f"duration must be a number or string like '60s'/'5m'/'1h', got {type(value).__name__}"
        )

    match = _DURATION_PATTERN.match(value)
    if match is None:
        raise ValueError(
            f"duration {value!r} is not a valid duration; expected a number "
            "or a value like '60s', '5m', '1h', '500ms'"
        )

    number = float(match.group("value"))
    unit = match.group("unit") or "s"
    return number * _UNIT_TO_SECONDS[unit]
