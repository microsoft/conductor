import pytest


@pytest.fixture(autouse=True)
def _reset_baseline_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: the module-level baseline cache must never leak between
    # tests running in the same pytest process (issue #290). raising=False
    # keeps this a no-op until the implementation adds the attribute.
    monkeypatch.setattr(
        "conductor.interrupt.listener._CAPTURED_BASELINE_SETTINGS",
        None,
        raising=False,
    )
