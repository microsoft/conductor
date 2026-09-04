"""Public OpenTelemetry tracing primitives for Conductor.

The optional OpenTelemetry SDK is deliberately not imported here. Importing
``conductor.telemetry`` therefore remains safe in installations that have not
opted into tracing.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["TelemetrySubscriber", "guards", "init_tracer_provider"]


def __getattr__(name: str) -> Any:
    """Lazily load optional-SDK entry points and the guards module."""
    if name == "guards":
        return import_module("conductor.telemetry.guards")
    if name == "init_tracer_provider":
        from conductor.telemetry.setup import init_tracer_provider

        return init_tracer_provider
    if name == "TelemetrySubscriber":
        from conductor.telemetry.subscriber import TelemetrySubscriber

        return TelemetrySubscriber
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
