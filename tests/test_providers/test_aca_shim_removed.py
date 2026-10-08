"""The deprecated provider-specific wire import has been removed."""

import importlib.util


def test_aca_shim_removed_from_provider_package() -> None:
    # Requirement: callers must migrate to the canonical runner.protocol import.
    assert importlib.util.find_spec("conductor.providers.aca_protocol") is None
