"""Safe optional initialization for Conductor OpenTelemetry tracing."""

from __future__ import annotations

import logging
import os
from importlib import import_module
from typing import TYPE_CHECKING

from conductor.install_hint import install_command
from conductor.telemetry import guards
from conductor.telemetry.delegating import _DelegatingTracerProvider
from conductor.telemetry.semconv import CONDUCTOR_RUN_ID

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SpanExporter

logger = logging.getLogger(__name__)

_DEFAULT_SERVICE_NAME = "conductor"
_DEFAULT_EXPORT_TIMEOUT_SECONDS = 10.0
_delegating_global_provider = _DelegatingTracerProvider()
_host_provider_warning_emitted = False
_sdk_unavailable_warning_emitted = False


def init_tracer_provider(*, run_id: str) -> TracerProvider | None:
    """Create and latch a run-specific tracer provider when OTLP is configured.

    Setup is deliberately best effort: an unavailable SDK, exporter failure, or
    invalid environment configuration leaves the run uninstrumented rather than
    preventing workflow execution.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint or guards.sdk_disabled():
        guards.reset_telemetry_context()
        return None

    if not guards.OTEL_SDK_AVAILABLE:
        guards.reset_telemetry_context()
        _warn_sdk_unavailable_once()
        return None

    protocol = _resolve_otlp_protocol()
    try:
        provider = _build_tracer_provider(run_id, endpoint, protocol)
        _install_delegating_global_provider()
    except Exception:  # noqa: BLE001 -- optional tracing must never stop a workflow.
        guards.reset_telemetry_context()
        logger.warning("OpenTelemetry tracing initialization failed", exc_info=True)
        return None

    guards.set_current_tracer_provider(provider)
    guards.set_current_run_id(run_id)
    guards.set_current_otlp_protocol(protocol)
    guards.set_current_otlp_endpoint(endpoint)
    return provider


def _resolve_otlp_protocol() -> str:
    """Resolve the standard OTLP protocol variable to a stable exporter value."""
    return os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").strip().lower() or "grpc"


def _build_tracer_provider(run_id: str, endpoint: str, protocol: str) -> TracerProvider:
    """Build one run-local provider without changing the process global provider."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # Resource.create automatically merges standard resource attributes from the environment
    # (specifically OTEL_RESOURCE_ATTRIBUTES) with Conductor's run identity.
    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME") or _DEFAULT_SERVICE_NAME,
            CONDUCTOR_RUN_ID: run_id,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(_create_otlp_exporter(protocol, endpoint)))
    return provider


def _install_delegating_global_provider() -> None:
    """Install the permanent delegator unless the host owns the global provider."""
    from opentelemetry import trace
    from opentelemetry.trace import ProxyTracerProvider

    global _delegating_global_provider
    global_provider = trace.get_tracer_provider()
    if isinstance(global_provider, _DelegatingTracerProvider):
        return
    if global_provider is None or isinstance(global_provider, ProxyTracerProvider):
        trace.set_tracer_provider(_delegating_global_provider)
        return
    _warn_host_provider_once()


def _warn_sdk_unavailable_once() -> None:
    """Report a missing optional SDK once per process when OTLP is requested."""
    global _sdk_unavailable_warning_emitted
    if _sdk_unavailable_warning_emitted:
        return
    _sdk_unavailable_warning_emitted = True
    logger.warning(
        "OpenTelemetry tracing requires opentelemetry-sdk. Install it with: %s",
        install_command("telemetry"),
    )


def _warn_host_provider_once() -> None:
    """Report that a host-owned provider remains responsible for global spans."""
    global _host_provider_warning_emitted
    if _host_provider_warning_emitted:
        return
    _host_provider_warning_emitted = True
    logger.warning(
        "OpenTelemetry global tracer provider is already configured by the host; "
        "Conductor will export native spans through its run-local provider only."
    )


def _create_otlp_exporter(protocol: str, endpoint: str) -> SpanExporter:
    """Create the OTLP exporter selected by the captured protocol and endpoint."""
    if protocol == "grpc":
        module_name = "opentelemetry.exporter.otlp.proto.grpc.trace_exporter"
    else:
        module_name = "opentelemetry.exporter.otlp.proto.http.trace_exporter"

    exporter_module = import_module(module_name)
    return exporter_module.OTLPSpanExporter(
        endpoint=endpoint,
        timeout=_DEFAULT_EXPORT_TIMEOUT_SECONDS,
    )
