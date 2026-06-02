"""Registry-specific exceptions."""

from __future__ import annotations

from conductor.exceptions import ConductorError


class RegistryError(ConductorError):
    """Error related to workflow registry operations.

    Raised for registry not found, config parse errors,
    and duplicate registry names.
    """


class RegistryNotFoundError(RegistryError):
    """A registry resource (file, ref, etc.) was not found (HTTP 404 or equivalent).

    Subclass of ``RegistryError`` so existing ``except RegistryError`` handlers
    keep working, but callers that need to distinguish "not found" from other
    failures (auth, rate-limit, network) can catch this specifically.
    """

    pass
