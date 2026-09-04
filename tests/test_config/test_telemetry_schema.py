from __future__ import annotations

import importlib

import pytest
from pydantic import ValidationError

from conductor.config.schema import RuntimeConfig


class TestRuntimeTelemetryConfig:
    def test_does_not_export_telemetry_config(self) -> None:
        # Requirement: the YAML telemetry model is not part of the public API.
        # Given: the telemetry package.
        # When: its public attributes are inspected.
        telemetry = importlib.import_module("conductor.telemetry")

        # Then: callers cannot construct the removed configuration model.
        assert not hasattr(telemetry, "TelemetryConfig")

    def test_rejects_legacy_telemetry_key(self) -> None:
        # Requirement: legacy runtime telemetry configuration gives migration guidance.
        # Given: a runtime block using the removed telemetry key.
        # When: it crosses the runtime configuration boundary.
        with pytest.raises(
            ValidationError,
            match=(
                "runtime.telemetry was removed; tracing is enabled via the "
                "OTEL_EXPORTER_OTLP_ENDPOINT environment variable"
            ),
        ):
            RuntimeConfig(telemetry={"enabled": True})
