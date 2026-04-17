"""Registry-specific exceptions."""

from __future__ import annotations

from conductor.exceptions import ConductorError


class RegistryError(ConductorError):
    """Error related to workflow registry operations.

    Raised for registry not found, config parse errors,
    and duplicate registry names.
    """
