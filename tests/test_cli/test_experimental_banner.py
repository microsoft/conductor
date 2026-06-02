"""Experimental-provider banner (#241)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_banner_state(monkeypatch: pytest.MonkeyPatch):
    from conductor.cli import run as run_mod

    run_mod._PRINTED_EXPERIMENTAL_BANNERS.clear()
    yield
    run_mod._PRINTED_EXPERIMENTAL_BANNERS.clear()


def _caps_dict(**overrides: object) -> dict[str, object]:
    base = {
        "tier": "experimental",
        "mcp_tools": False,
        "workflow_tools_passthrough": False,
        "streaming_events": True,
        "agent_reasoning_events": True,
        "reasoning_effort": None,
        "structured_output": "prompt_injection",
        "interrupt": True,
        "max_session_seconds": True,
        "checkpoint_resume": False,
        "usage_tracking": True,
        "concurrent_safe": True,
        "upstream_pin": "claude-agent-sdk>=0.1.0",
        "maintainer": "@lesandiz (best-effort)",
    }
    base.update(overrides)
    return base


def test_banner_prints_for_experimental_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from conductor.cli import run as run_mod

    console_instance = MagicMock()
    monkeypatch.setattr(
        "conductor.cli.run.Console",
        lambda **kw: console_instance,
        raising=False,
    )
    # Patch the rich.console.Console import inside the function
    import rich.console

    monkeypatch.setattr(rich.console, "Console", lambda **kw: console_instance)

    data = {
        "run_id": "abc123",
        "providers": {
            "claude-agent-sdk": {
                "name": "claude-agent-sdk",
                "tier": "experimental",
                "upstream_pin": "claude-agent-sdk>=0.1.0",
                "maintainer": "@lesandiz (best-effort)",
                "capabilities": _caps_dict(),
            },
        },
    }
    run_mod._maybe_print_experimental_banner(data)

    assert console_instance.print.called
    # Inspect the printed Panel content
    args, _kwargs = console_instance.print.call_args
    panel = args[0]
    rendered = panel.renderable
    assert "Experimental provider in use" in rendered
    assert "claude-agent-sdk" in rendered
    assert "claude-agent-sdk>=0.1.0" in rendered
    assert "@lesandiz" in rendered


def test_banner_does_not_print_for_stable_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from conductor.cli import run as run_mod

    console_instance = MagicMock()
    import rich.console

    monkeypatch.setattr(rich.console, "Console", lambda **kw: console_instance)

    data = {
        "run_id": "abc",
        "providers": {
            "copilot": {
                "name": "copilot",
                "tier": "stable",
                "upstream_pin": None,
                "maintainer": "@microsoft/conductor",
                "capabilities": _caps_dict(tier="stable"),
            },
        },
    }
    run_mod._maybe_print_experimental_banner(data)
    assert not console_instance.print.called


def test_banner_does_not_repeat_for_same_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replayed workflow_started events during resume must not double-print."""
    from conductor.cli import run as run_mod

    console_instance = MagicMock()
    import rich.console

    monkeypatch.setattr(rich.console, "Console", lambda **kw: console_instance)

    data = {
        "run_id": "abc123",
        "providers": {
            "claude-agent-sdk": {
                "name": "claude-agent-sdk",
                "tier": "experimental",
                "upstream_pin": "claude-agent-sdk>=0.1.0",
                "maintainer": None,
                "capabilities": _caps_dict(),
            },
        },
    }
    run_mod._maybe_print_experimental_banner(data)
    first_call_count = console_instance.print.call_count
    run_mod._maybe_print_experimental_banner(data)
    assert console_instance.print.call_count == first_call_count


def test_banner_handles_missing_providers_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Older event payloads without ``providers`` must not raise."""
    from conductor.cli import run as run_mod

    console_instance = MagicMock()
    import rich.console

    monkeypatch.setattr(rich.console, "Console", lambda **kw: console_instance)

    run_mod._maybe_print_experimental_banner({"name": "test"})
    assert not console_instance.print.called


def test_banner_lists_declared_limitations(monkeypatch: pytest.MonkeyPatch) -> None:
    from conductor.cli import run as run_mod

    console_instance = MagicMock()
    import rich.console

    monkeypatch.setattr(rich.console, "Console", lambda **kw: console_instance)

    data = {
        "run_id": "x",
        "providers": {
            "p": {
                "name": "p",
                "tier": "experimental",
                "upstream_pin": None,
                "maintainer": None,
                "capabilities": _caps_dict(
                    mcp_tools=False,
                    streaming_events=False,
                    checkpoint_resume=False,
                ),
            },
        },
    }
    run_mod._maybe_print_experimental_banner(data)
    rendered = console_instance.print.call_args[0][0].renderable
    assert "no MCP servers" in rendered
    assert "no streaming events" in rendered
    assert "no checkpoint resume" in rendered


def test_banner_reads_run_id_from_system_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_workflow_started_data() puts run_id at top level AND under system.

    The banner's idempotency key must handle either location so a real
    engine event doesn't trigger re-prints across resume / replay.
    """
    from conductor.cli import run as run_mod

    console_instance = MagicMock()
    import rich.console

    monkeypatch.setattr(rich.console, "Console", lambda **kw: console_instance)

    data = {
        # Top-level run_id absent — only present in the system block.
        "system": {"run_id": "from-system-block"},
        "providers": {
            "claude-agent-sdk": {
                "name": "claude-agent-sdk",
                "tier": "experimental",
                "upstream_pin": None,
                "maintainer": None,
                "capabilities": _caps_dict(),
            },
        },
    }
    run_mod._maybe_print_experimental_banner(data)
    first_count = console_instance.print.call_count
    assert first_count > 0

    # Re-emit the same event (mimics resume replay). Banner must not re-print.
    run_mod._maybe_print_experimental_banner(data)
    assert console_instance.print.call_count == first_count
