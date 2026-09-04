from __future__ import annotations

import logging
from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from conductor.telemetry import guards
from conductor.telemetry import setup as telemetry_setup
from conductor.telemetry.setup import init_tracer_provider


@pytest.fixture(autouse=True)
def reset_telemetry_context() -> Generator[None]:
    guards.reset_telemetry_context()
    yield
    guards.reset_telemetry_context()


def test_otlp_endpoint_activates_with_environment_resource_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import ProxyTracerProvider

    # Given: an endpoint and standard OpenTelemetry resource variables.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment=testing")
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    with (
        patch.object(trace, "get_tracer_provider", return_value=ProxyTracerProvider()),
        patch.object(trace, "set_tracer_provider"),
        patch(
            "conductor.telemetry.setup._create_otlp_exporter",
            return_value=InMemorySpanExporter(),
        ),
    ):
        # When: the run initializes tracing without workflow configuration.
        provider = init_tracer_provider(run_id="run-123")

    # Then: the SDK merges ambient resource attributes with Conductor's identity.
    assert provider is not None
    assert provider.resource.attributes["service.name"] == "conductor"
    assert provider.resource.attributes["deployment.environment"] == "testing"
    assert provider.resource.attributes["conductor.run_id"] == "run-123"
    provider.shutdown()


def test_cached_global_tracer_delegates_to_two_sequential_run_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import ProxyTracerProvider

    exporter_one = InMemorySpanExporter()
    exporter_two = InMemorySpanExporter()
    global_provider: trace.TracerProvider = ProxyTracerProvider()

    def replace_global_provider(provider: trace.TracerProvider) -> None:
        nonlocal global_provider
        global_provider = provider

    # Given: a process without a host-owned global provider and two enabled runs.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    with (
        patch.object(trace, "get_tracer_provider", side_effect=lambda: global_provider),
        patch.object(trace, "set_tracer_provider", side_effect=replace_global_provider) as install,
        patch(
            "conductor.telemetry.setup._create_otlp_exporter",
            side_effect=[exporter_one, exporter_two],
        ),
    ):
        first_provider = init_tracer_provider(run_id="run-one")
        global_tracer = global_provider.get_tracer("test.instrumentation")

        # When: one tracer starts spans across two run-local providers.
        with global_tracer.start_as_current_span("first-run"):
            pass
        assert first_provider is not None
        first_provider.shutdown()
        guards.reset_telemetry_context()

        second_provider = init_tracer_provider(run_id="run-two")
        global_tracer.start_span("second-run").end()
        assert second_provider is not None
        second_provider.force_flush()

    # Then: the global delegator remains installed while both exporters receive their run's span.
    assert [span.name for span in exporter_one.get_finished_spans()] == ["first-run"]
    assert [span.name for span in exporter_two.get_finished_spans()] == ["second-run"]
    install.assert_called_once()
    second_provider.shutdown()


def test_host_global_provider_is_preserved_and_warned_once(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # Given: a host provider and two telemetry-enabled Conductor runs.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setattr(telemetry_setup, "_host_provider_warning_emitted", False)
    host_provider = TracerProvider()
    caplog.set_level(logging.WARNING, logger="conductor.telemetry.setup")
    with (
        patch.object(trace, "get_tracer_provider", return_value=host_provider),
        patch.object(trace, "set_tracer_provider") as install,
        patch(
            "conductor.telemetry.setup._create_otlp_exporter",
            return_value=InMemorySpanExporter(),
        ),
    ):
        # When: each run initializes its own tracing provider.
        first_provider = init_tracer_provider(run_id="run-one")
        second_provider = init_tracer_provider(run_id="run-two")

    # Then: the host stays global and the conflict is reported only once.
    assert install.call_count == 0
    assert sum("already configured by the host" in record.message for record in caplog.records) == 1
    assert first_provider is not None
    assert second_provider is not None
    first_provider.shutdown()
    second_provider.shutdown()
    host_provider.shutdown()


def test_exporter_failure_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: an endpoint and a failing exporter constructor.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    with patch(
        "conductor.telemetry.setup._create_otlp_exporter",
        side_effect=RuntimeError("collector unavailable"),
    ):
        # When: tracing initialization reaches the exporter.
        provider = init_tracer_provider(run_id="run-123")

    # Then: tracing is disabled while the caller receives no exporter exception.
    assert provider is None
    assert guards.is_telemetry_active() is False


def test_sdk_unavailable_warning_includes_install_command_once(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: OTLP configuration without the optional SDK.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setattr(guards, "OTEL_SDK_AVAILABLE", False)
    monkeypatch.setattr(telemetry_setup, "_sdk_unavailable_warning_emitted", False)
    caplog.set_level(logging.WARNING, logger="conductor.telemetry.setup")
    with patch("conductor.telemetry.setup.install_command", return_value="install telemetry"):
        # When: two runs request telemetry in one process.
        init_tracer_provider(run_id="run-one")
        init_tracer_provider(run_id="run-two")

    # Then: the operator sees one actionable optional-dependency warning.
    assert sum("install telemetry" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize(
    ("raw_protocol", "expected_protocol"),
    [
        (None, "grpc"),
        ("HTTP/PROTOBUF", "http/protobuf"),
        ("  Http/Json  ", "http/json"),
    ],
)
def test_resolve_otlp_protocol_normalizes_environment_value(
    monkeypatch: pytest.MonkeyPatch,
    raw_protocol: str | None,
    expected_protocol: str,
) -> None:
    # Requirement: OTLP protocol resolution defaults to gRPC and normalizes user input.
    # Given: a missing or variably formatted standard protocol environment variable.
    if raw_protocol is None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    else:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", raw_protocol)

    # When: the telemetry initializer resolves the protocol.
    protocol = telemetry_setup._resolve_otlp_protocol()

    # Then: the stable protocol value is selected.
    assert protocol == expected_protocol


def test_typo_protocol_uses_http_exporter_and_latches_normalized_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # Requirement: every non-gRPC protocol keeps the HTTP exporter selection semantics.
    # Given: an OTLP endpoint and an unrecognized protocol spelling.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "  Typo/Protocol  ")
    exporter = InMemorySpanExporter()
    exporter_module = SimpleNamespace(OTLPSpanExporter=lambda **_: exporter)
    with patch("conductor.telemetry.setup.import_module", return_value=exporter_module) as importer:
        # When: the run initializes native tracing.
        provider = init_tracer_provider(run_id="run-http-fallback")

    # Then: the HTTP exporter path and normalized run latch agree on the captured protocol.
    assert provider is not None
    importer.assert_called_once_with("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    assert guards.current_otlp_protocol() == "typo/protocol"
    provider.shutdown()


def test_otlp_latches_are_hidden_until_initialization_and_capture_resolved_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # Requirement: OTLP endpoint and protocol are visible only for an active telemetry run.
    # Given: an inactive process followed by a configured tracing run.
    assert guards.current_otlp_protocol() is None
    assert guards.current_otlp_endpoint() is None
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "  http://localhost:4317  ")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "HTTP/JSON")
    with patch(
        "conductor.telemetry.setup._create_otlp_exporter",
        return_value=InMemorySpanExporter(),
    ):
        # When: tracing initialization succeeds.
        provider = init_tracer_provider(run_id="run-latched")

    # Then: both normalized values belong to the active run.
    assert provider is not None
    assert guards.current_otlp_protocol() == "http/json"
    assert guards.current_otlp_endpoint() == "http://localhost:4317"
    provider.shutdown()


def test_otlp_latches_do_not_follow_environment_changes_after_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # Requirement: OTLP configuration is immutable for the lifetime of one run.
    # Given: a successfully initialized run with known OTLP settings.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector-one:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    with patch(
        "conductor.telemetry.setup._create_otlp_exporter",
        return_value=InMemorySpanExporter(),
    ):
        provider = init_tracer_provider(run_id="run-stable-latches")

    # When: ambient OTLP settings change after initialization.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector-two:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")

    # Then: consumers still receive the original run-scoped settings.
    assert provider is not None
    assert guards.current_otlp_protocol() == "grpc"
    assert guards.current_otlp_endpoint() == "http://collector-one:4317"
    provider.shutdown()


def test_otlp_exporter_and_latches_share_values_captured_before_environment_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # Requirement: exporter construction and telemetry latches use one atomic environment snapshot.
    # Given: an initial OTLP configuration whose environment changes during exporter construction.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector-one:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    captured_values: list[tuple[str, str]] = []

    def flip_environment(protocol: str, endpoint: str) -> InMemorySpanExporter:
        captured_values.append((protocol, endpoint))
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector-two:4318")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json")
        return InMemorySpanExporter()

    with patch("conductor.telemetry.setup._create_otlp_exporter", side_effect=flip_environment):
        # When: telemetry initializes and exporter construction flips the ambient variables.
        provider = init_tracer_provider(run_id="run-atomic-snapshot")

    # Then: exporter and latches retain the same values captured before construction.
    assert provider is not None
    assert captured_values == [("grpc", "http://collector-one:4317")]
    assert guards.current_otlp_protocol() == "grpc"
    assert guards.current_otlp_endpoint() == "http://collector-one:4317"
    provider.shutdown()


def test_copilot_grpc_warning_latch_resets_for_each_initialized_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # Requirement: Copilot gRPC warning is emitted once per initialized run, not once per process.
    # Given: two sequential telemetry runs using the same OTLP setup.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    with patch(
        "conductor.telemetry.setup._create_otlp_exporter",
        side_effect=[InMemorySpanExporter(), InMemorySpanExporter()],
    ):
        # When: each run requests its warning before telemetry context is reset.
        first_provider = init_tracer_provider(run_id="run-one")
        assert guards.warn_copilot_grpc_once() is True
        assert guards.warn_copilot_grpc_once() is False
        assert first_provider is not None
        first_provider.shutdown()
        guards.reset_telemetry_context()

        second_provider = init_tracer_provider(run_id="run-two")
        second_warning = guards.warn_copilot_grpc_once()

    # Then: reset restores one warning opportunity for the next run.
    assert second_provider is not None
    assert second_warning is True
    assert guards.warn_copilot_grpc_once() is False
    second_provider.shutdown()
