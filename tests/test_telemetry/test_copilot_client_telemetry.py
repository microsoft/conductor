from __future__ import annotations

import logging
from collections.abc import Generator
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, call, patch

import pytest

from conductor.config.schema import ProviderSettings
from conductor.providers.copilot import CopilotProvider
from conductor.telemetry import guards
from conductor.telemetry.setup import init_tracer_provider

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider


@pytest.fixture(autouse=True)
def reset_telemetry_context() -> Generator[None]:
    guards.reset_telemetry_context()
    yield
    guards.reset_telemetry_context()


def _activate_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str,
    protocol: str,
) -> TracerProvider:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector-one:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", protocol)
    with patch(
        "conductor.telemetry.setup._create_otlp_exporter",
        return_value=InMemorySpanExporter(),
    ):
        provider = init_tracer_provider(run_id=run_id)
    assert provider is not None
    return provider


@pytest.mark.parametrize("protocol", ["http/protobuf", "http/json"])
def test_build_client_passes_latched_http_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    # Requirement: an active OTLP/HTTP run passes exactly the documented SDK telemetry keys.
    import conductor.providers.copilot as copilot_mod

    telemetry_provider = _activate_telemetry(
        monkeypatch,
        run_id=f"run-{protocol}",
        protocol=protocol,
    )
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector-two:4319")
    client = object()
    client_factory = MagicMock(return_value=client)
    monkeypatch.setattr(copilot_mod, "CopilotClient", client_factory)

    # When: a nested Copilot runtime client is constructed after ambient settings change.
    assert CopilotProvider()._build_client() is client

    # Then: the SDK gets the run-latched endpoint, protocol, and capture policy.
    client_factory.assert_called_once_with(
        telemetry={
            "otlp_endpoint": "http://collector-one:4318",
            "otlp_protocol": protocol,
            "capture_content": True,
        }
    )
    telemetry_provider.shutdown()


def test_build_client_omits_grpc_telemetry_and_warns_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Requirement: Copilot omits unsupported gRPC telemetry and names the HTTP remedy.
    import conductor.providers.copilot as copilot_mod

    telemetry_provider = _activate_telemetry(monkeypatch, run_id="run-grpc", protocol="grpc")
    client_factory = MagicMock(side_effect=[object(), object()])
    monkeypatch.setattr(copilot_mod, "CopilotClient", client_factory)
    caplog.set_level(logging.WARNING, logger="conductor.providers.copilot")

    # When: the provider constructs two nested clients within one telemetry run.
    CopilotProvider()._build_client()
    CopilotProvider()._build_client()

    # Then: neither constructor receives telemetry and the actionable warning occurs once.
    assert client_factory.call_args_list == [call(), call()]
    assert (
        sum(
            "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf" in record.message
            for record in caplog.records
        )
        == 1
    )
    telemetry_provider.shutdown()


def test_build_client_omits_telemetry_when_otel_sdk_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: OTEL_SDK_DISABLED prevents telemetry activation and client kwargs.
    import conductor.providers.copilot as copilot_mod

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert init_tracer_provider(run_id="run-disabled") is None
    client = object()
    client_factory = MagicMock(return_value=client)
    monkeypatch.setattr(copilot_mod, "CopilotClient", client_factory)

    # When: a client is built after telemetry setup declined the disabled SDK.
    assert CopilotProvider()._build_client() is client

    # Then: the default SDK constructor remains argument-free.
    client_factory.assert_called_once_with()


def test_build_client_warns_again_after_telemetry_reset(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Requirement: each initialized run gets one gRPC-to-HTTP guidance warning.
    import conductor.providers.copilot as copilot_mod

    client_factory = MagicMock(side_effect=[object(), object()])
    monkeypatch.setattr(copilot_mod, "CopilotClient", client_factory)
    caplog.set_level(logging.WARNING, logger="conductor.providers.copilot")

    # When: two gRPC telemetry runs initialize with a context reset between them.
    first_provider = _activate_telemetry(monkeypatch, run_id="run-one", protocol="grpc")
    CopilotProvider()._build_client()
    first_provider.shutdown()
    guards.reset_telemetry_context()
    second_provider = _activate_telemetry(monkeypatch, run_id="run-two", protocol="grpc")
    CopilotProvider()._build_client()

    # Then: both runs surface their own warning opportunity.
    assert (
        sum(
            "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf" in record.message
            for record in caplog.records
        )
        == 2
    )
    second_provider.shutdown()


def test_runtime_connection_never_receives_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: external runtimes own telemetry transport and receive no telemetry kwarg.
    import conductor.providers.copilot as copilot_mod

    telemetry_provider = _activate_telemetry(
        monkeypatch,
        run_id="run-external-runtime",
        protocol="http/protobuf",
    )
    connection = object()
    runtime_connection = MagicMock()
    runtime_connection.for_uri.return_value = connection
    client = object()
    client_factory = MagicMock(return_value=client)
    monkeypatch.setattr(copilot_mod, "RuntimeConnection", runtime_connection)
    monkeypatch.setattr(copilot_mod, "CopilotClient", client_factory)
    provider = CopilotProvider(
        provider_settings=ProviderSettings(name="copilot", runtime_url="http://runtime:9000")
    )

    # When: a telemetry-active provider connects to an external Copilot runtime.
    assert provider._build_client() is client

    # Then: connection construction remains the only SDK constructor argument.
    client_factory.assert_called_once_with(connection=connection)
    telemetry_provider.shutdown()
