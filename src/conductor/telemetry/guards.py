"""Optional-SDK checks and process-local telemetry state.

This module is the only import-time probe for ``opentelemetry.sdk``. The
latched state is reset whenever environment-driven activation is unavailable or
initialization fails so one workflow run cannot leak tracing context into the
next run in this process.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

try:
    import opentelemetry.sdk  # noqa: F401
except ImportError:
    OTEL_SDK_AVAILABLE: bool = False
else:
    OTEL_SDK_AVAILABLE: bool = True

_current_tracer_provider: TracerProvider | None = None
_current_run_id: str | None = None
_current_otlp_protocol: str | None = None
_current_otlp_endpoint: str | None = None
_copilot_grpc_warning_emitted_by_run_id: dict[str, bool] = {}


def sdk_disabled() -> bool:
    """Return whether the OpenTelemetry SDK is disabled by environment."""
    return os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true"


def is_telemetry_active() -> bool:
    """Return whether tracing has been initialized for the current run."""
    return _current_tracer_provider is not None


def current_tracer_provider() -> TracerProvider | None:
    """Return the provider latched for the active run, if any."""
    return _current_tracer_provider


def current_run_id() -> str | None:
    """Return the active telemetry run ID without exposing stale state."""
    if not is_telemetry_active():
        return None
    return _current_run_id


def current_otlp_protocol() -> str | None:
    """Return the active run's resolved OTLP protocol without stale state."""
    if not is_telemetry_active():
        return None
    return _current_otlp_protocol


def current_otlp_endpoint() -> str | None:
    """Return the active run's resolved OTLP endpoint without stale state."""
    if not is_telemetry_active():
        return None
    return _current_otlp_endpoint


def capture_span_content() -> bool:
    """Return whether the standard capture policy permits native span content."""
    value = os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "")
    return value.strip().upper() in {"TRUE", "SPAN_ONLY", "SPAN_AND_EVENT"}


def set_current_tracer_provider(provider: TracerProvider | None) -> None:
    """Latch the tracing provider created for the current run."""
    global _current_tracer_provider
    _current_tracer_provider = provider


def set_current_run_id(run_id: str | None) -> None:
    """Latch the run ID associated with the current telemetry provider."""
    global _current_run_id
    _current_run_id = run_id


def set_current_otlp_protocol(protocol: str | None) -> None:
    """Latch the resolved OTLP protocol associated with the current run."""
    global _current_otlp_protocol
    _current_otlp_protocol = protocol


def set_current_otlp_endpoint(endpoint: str | None) -> None:
    """Latch the resolved OTLP endpoint associated with the current run."""
    global _current_otlp_endpoint
    _current_otlp_endpoint = endpoint


def warn_copilot_grpc_once() -> bool:
    """Return whether the active run may emit its Copilot gRPC warning."""
    run_id = current_run_id()
    if run_id is None:
        return False
    if run_id in _copilot_grpc_warning_emitted_by_run_id:
        return False
    _copilot_grpc_warning_emitted_by_run_id[run_id] = True
    return True


def reset_telemetry_context() -> None:
    """Clear all process-local tracing state for a completed or skipped run."""
    set_current_tracer_provider(None)
    set_current_run_id(None)
    set_current_otlp_protocol(None)
    set_current_otlp_endpoint(None)
    _copilot_grpc_warning_emitted_by_run_id.clear()
