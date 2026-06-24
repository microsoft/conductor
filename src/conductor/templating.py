"""Jinja template detection, shared across schema validation, the executor's
render step, and the provider resolvers.

Kept as a leaf module (no intra-``conductor`` imports) so it can be imported
from any layer without risking an import cycle.
"""

from __future__ import annotations

from typing import TypeGuard


def is_jinja_template(value: object) -> TypeGuard[str]:
    """Return ``True`` if ``value`` is a string carrying a Jinja expression
    (``{{ ... }}``) or statement (``{% ... %}``) marker.

    Returns a :data:`~typing.TypeGuard` of ``str`` rather than a plain ``bool``
    so a caller that branches on it narrows the value to ``str`` — the
    executor's ``_render_enum_field`` path relies on that narrowing.

    Note: this matches *both* ``{{`` and ``{%``.
    :meth:`AgentDef._validate_wait_duration` deliberately checks only ``{{``
    (it defers expression templates but not statement templates) and is
    intentionally *not* routed through this helper.
    """
    return isinstance(value, str) and ("{{" in value or "{%" in value)
