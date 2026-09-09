"""Typed workflow failure envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from conductor.error_kinds import is_reserved_error_kind, validate_error_kind


class ErrorEnvelopeValidationError(ValueError):
    """Raised when a script publishes an invalid error envelope."""


@dataclass(frozen=True)
class ErrorEnvelope:
    """Engine-owned representation of a routable workflow failure."""

    kind: str
    message: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible envelope shape."""
        return {
            "kind": self.kind,
            "message": self.message,
            "details": self.details,
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        allow_reserved: bool = True,
    ) -> ErrorEnvelope:
        """Validate a wire value and return a typed envelope."""
        if not isinstance(value, dict):
            raise ErrorEnvelopeValidationError("error envelope must be a JSON object")

        kind = value.get("kind")
        if not isinstance(kind, str):
            raise ErrorEnvelopeValidationError(
                "error envelope kind must be a dotted lowercase identifier"
            )
        try:
            validate_error_kind(kind)
        except ValueError as exc:
            raise ErrorEnvelopeValidationError(str(exc)) from exc
        if not allow_reserved and is_reserved_error_kind(kind):
            raise ErrorEnvelopeValidationError(
                f"error kind '{kind}' uses an engine-owned namespace"
            )

        message = value.get("message")
        if not isinstance(message, str) or not message:
            raise ErrorEnvelopeValidationError("error envelope message must be a non-empty string")

        details = value.get("details", {})
        if not isinstance(details, dict):
            raise ErrorEnvelopeValidationError("error envelope details must be a JSON object")

        return cls(kind=kind, message=message, details=details)


def make_script_transport_error(
    *,
    agent_name: str,
    error: Exception,
) -> ErrorEnvelope:
    """Create a routable failure when the script error channel is malformed."""
    return ErrorEnvelope(
        kind="internal.script_error_transport",
        message=f"Script '{agent_name}' published an invalid error envelope",
        details={
            "error_type": type(error).__name__,
            "error": str(error),
        },
    )
