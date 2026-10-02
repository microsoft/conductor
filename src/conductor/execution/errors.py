"""Execution-backend specification failures.

This leaf module keeps backend input validation inside the stdlib-only
``conductor.execution`` package.  Callers at a higher architectural layer may
translate these failures into their public configuration-error vocabulary.
"""

from __future__ import annotations


class ExecutionSpecError(ValueError):
    """An execution backend received an invalid or unusable specification."""
