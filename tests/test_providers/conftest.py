"""Test isolation fixtures shared by ``tests/test_providers/``."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_copilot_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the Copilot external-runtime env vars for every test in this package.

    ``CopilotProvider._resolve_runtime_connection()`` falls back to
    ``COPILOT_PROVIDER_RUNTIME_URL`` / ``COPILOT_PROVIDER_RUNTIME_TOKEN`` when
    no YAML ``runtime_url`` is configured. Any developer or CI runner that has
    these documented, supported variables exported would otherwise silently
    flip every bare ``CopilotProvider()`` in this package onto the
    external-runtime code path, failing or vacuously passing tests that
    assume a spawned runtime.
    """
    monkeypatch.delenv("COPILOT_PROVIDER_RUNTIME_URL", raising=False)
    monkeypatch.delenv("COPILOT_PROVIDER_RUNTIME_TOKEN", raising=False)
