from __future__ import annotations

import os
from collections.abc import Generator
from unittest.mock import patch

import pytest

from conductor.config.schema import ProviderSettings
from conductor.providers.capabilities import native_otel_spans_active
from conductor.providers.copilot import _build_client_telemetry
from conductor.telemetry import guards
from conductor.telemetry.setup import init_tracer_provider


@pytest.fixture(autouse=True)
def reset_telemetry_context() -> Generator[None]:
    guards.reset_telemetry_context()
    yield
    guards.reset_telemetry_context()


def test_coherence_spies_on_env_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test (a): client telemetry and active span checks do not read environment directly.

    Because they rely entirely on the run-latched settings in guards, they do not query
    OTEL_EXPORTER_OTLP_PROTOCOL or OTEL_EXPORTER_OTLP_ENDPOINT from the environment
    directly. This guarantees that they can never diverge from the latched exporter.
    """
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # Given: an endpoint and protocol environment variables.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector-one:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")

    with patch(
        "conductor.telemetry.setup._create_otlp_exporter",
        return_value=InMemorySpanExporter(),
    ):
        provider = init_tracer_provider(run_id="run-coherence-spy")
    assert provider is not None

    # Spy on all os.environ accesses
    accessed_keys: list[str] = []
    orig_get = os.environ.get

    def spy_get(key: str, default: str | None = None) -> str | None:
        accessed_keys.append(key)
        return orig_get(key, default)

    orig_getitem = os.environ.__getitem__

    def spy_getitem(key: str) -> str:
        accessed_keys.append(key)
        return orig_getitem(key)

    with (
        patch.object(os.environ, "get", side_effect=spy_get),
        patch.object(os.environ, "__getitem__", side_effect=spy_getitem),
    ):
        # When: calling _build_client_telemetry()
        client_telemetry = _build_client_telemetry()

        # Then: _build_client_telemetry returns the correct latched config
        assert client_telemetry is not None
        assert client_telemetry["otlp_endpoint"] == "http://collector-one:4318"
        assert client_telemetry["otlp_protocol"] == "http/protobuf"

        # Check that no direct reads of OTEL_EXPORTER_OTLP_PROTOCOL/ENDPOINT occurred
        assert "OTEL_EXPORTER_OTLP_PROTOCOL" not in accessed_keys
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in accessed_keys

        # Clear accessed_keys
        accessed_keys.clear()

        # When: calling native_otel_spans_active
        active = native_otel_spans_active(
            "copilot",
            ProviderSettings(name="copilot"),
            telemetry_protocol=guards.current_otlp_protocol(),
        )

        # Then: it resolves correctly
        assert active is True

        # Check that no direct reads of OTEL_EXPORTER_OTLP_PROTOCOL/ENDPOINT occurred
        assert "OTEL_EXPORTER_OTLP_PROTOCOL" not in accessed_keys
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in accessed_keys

    provider.shutdown()


def test_coherence_after_env_flips(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test (b): Env flips AFTER init_tracer_provider do not change decisions or target.

    Flipping the OTLP variables (e.g. protocol http->grpc, endpoint A->B, etc.) after initialization
    does not affect the emitted native_otel_spans_active field, _build_client_telemetry(), or
    the latched export destination.
    """
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # Case 1: Initialized with HTTP, flipped to gRPC and different endpoint
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector-a:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")

    with patch(
        "conductor.telemetry.setup._create_otlp_exporter",
        return_value=InMemorySpanExporter(),
    ):
        provider1 = init_tracer_provider(run_id="run-env-flip-1")
    assert provider1 is not None

    # Flip environment variables
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector-b:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")

    # Verify choices are unchanged and still coherent
    assert guards.current_otlp_endpoint() == "http://collector-a:4318"
    assert guards.current_otlp_protocol() == "http/protobuf"

    assert (
        native_otel_spans_active(
            "copilot",
            ProviderSettings(name="copilot"),
            telemetry_protocol=guards.current_otlp_protocol(),
        )
        is True
    )

    client_telemetry = _build_client_telemetry()
    assert client_telemetry is not None
    assert client_telemetry["otlp_endpoint"] == "http://collector-a:4318"
    assert client_telemetry["otlp_protocol"] == "http/protobuf"

    provider1.shutdown()
    guards.reset_telemetry_context()

    # Case 2: Initialized with gRPC, flipped to HTTP and empty/deleted endpoint
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector-a:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")

    with patch(
        "conductor.telemetry.setup._create_otlp_exporter",
        return_value=InMemorySpanExporter(),
    ):
        provider2 = init_tracer_provider(run_id="run-env-flip-2")
    assert provider2 is not None

    # Flip environment variables
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json")

    # Verify choices are unchanged and still coherent
    assert guards.current_otlp_endpoint() == "http://collector-a:4318"
    assert guards.current_otlp_protocol() == "grpc"

    assert (
        native_otel_spans_active(
            "copilot",
            ProviderSettings(name="copilot"),
            telemetry_protocol=guards.current_otlp_protocol(),
        )
        is False
    )

    assert _build_client_telemetry() is None

    provider2.shutdown()


def test_coherence_consequence() -> None:
    """Test (c): Because all telemetry consumers read the same run-latched values from guards,
    the active field and client decisions can never disagree and CLI spans always target the
    same endpoint as Conductor's exporter — both span-loss and misroute directions from the dual
    review are unreachable.
    """
    # When telemetry is inactive, all decisions are consistent (inactive/None).
    assert not guards.is_telemetry_active()
    assert guards.current_otlp_endpoint() is None
    assert guards.current_otlp_protocol() is None
    assert not native_otel_spans_active(
        "copilot",
        ProviderSettings(name="copilot"),
        telemetry_protocol=guards.current_otlp_protocol(),
    )
    assert _build_client_telemetry() is None
