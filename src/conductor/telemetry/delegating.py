"""Global tracer provider that dispatches spans to the active workflow run."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.util._decorator import _agnosticcontextmanager
from opentelemetry.util.types import Attributes

from conductor.telemetry import guards


class _DelegatingTracerProvider(trace.TracerProvider):
    """Return tracers that resolve the active provider only when a span starts."""

    def get_tracer(
        self,
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
        attributes: Attributes | None = None,
    ) -> trace.Tracer:
        """Create a tracer that preserves its instrumentation scope between runs."""
        return _DelegatingTracer(
            instrumenting_module_name,
            instrumenting_library_version,
            schema_url,
            attributes,
        )


class _DelegatingTracer(trace.Tracer):
    """Forward span creation to the provider latched for the current run."""

    def __init__(
        self,
        instrumenting_module_name: str,
        instrumenting_library_version: str | None,
        schema_url: str | None,
        attributes: Attributes | None,
    ) -> None:
        self._instrumenting_module_name = instrumenting_module_name
        self._instrumenting_library_version = instrumenting_library_version
        self._schema_url = schema_url
        self._attributes = attributes

    def start_span(
        self,
        name: str,
        context: Context | None = None,
        kind: trace.SpanKind = trace.SpanKind.INTERNAL,
        attributes: Attributes | None = None,
        links: Sequence[trace.Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
    ) -> trace.Span:
        """Start a span from the provider active at this exact moment."""
        return self._active_tracer().start_span(
            name,
            context,
            kind,
            attributes,
            links,
            start_time,
            record_exception,
            set_status_on_exception,
        )

    @_agnosticcontextmanager
    def start_as_current_span(
        self,
        name: str,
        context: Context | None = None,
        kind: trace.SpanKind = trace.SpanKind.INTERNAL,
        attributes: Attributes | None = None,
        links: Sequence[trace.Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
        end_on_exit: bool = True,
    ) -> Iterator[trace.Span]:
        """Activate a span from the provider active when the context opens."""
        with self._active_tracer().start_as_current_span(
            name,
            context,
            kind,
            attributes,
            links,
            start_time,
            record_exception,
            set_status_on_exception,
            end_on_exit,
        ) as span:
            yield span

    def _active_tracer(self) -> trace.Tracer:
        """Return the run-local tracer or an inert tracer outside a workflow run."""
        provider = guards.current_tracer_provider()
        if provider is None:
            return trace.NoOpTracer()
        return provider.get_tracer(
            self._instrumenting_module_name,
            self._instrumenting_library_version,
            self._schema_url,
            self._attributes,
        )
