"""Legacy ACA banner emission through the run-scoped provider latch."""

from unittest.mock import MagicMock

import pytest


def test_aca_cli_notice_emits_once_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: replaying workflow_started cannot repeat the legacy ACA CLI notice.
    from conductor.cli import run as run_module

    console = MagicMock()
    monkeypatch.setattr(run_module, "_verbose_console", console)
    run_module._PRINTED_EXPERIMENTAL_BANNERS.clear()
    try:
        event = {"run_id": "run-one", "providers": {"aca": {"tier": "experimental"}}}
        run_module._maybe_print_experimental_banner(event)
        run_module._maybe_print_experimental_banner(event)
        assert console.print.call_count == 1
        run_module._maybe_print_experimental_banner({**event, "run_id": "run-two"})
        assert console.print.call_count == 2
    finally:
        run_module._PRINTED_EXPERIMENTAL_BANNERS.clear()
