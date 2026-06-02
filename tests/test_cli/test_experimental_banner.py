"""Experimental-provider banner (#241)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_banner_state(monkeypatch: pytest.MonkeyPatch):
    from conductor.cli import run as run_mod
    from conductor.cli.app import verbose_mode

    run_mod._PRINTED_EXPERIMENTAL_BANNERS.clear()
    # The banner routes through _verbose_console, which is gated on
    # is_verbose(). Default is True, but force it explicitly so the
    # test does not depend on global contextvar state from a previous test.
    token = verbose_mode.set(True)
    yield
    verbose_mode.reset(token)
    run_mod._PRINTED_EXPERIMENTAL_BANNERS.clear()


def _patch_console(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace _verbose_console with a Mock and return it.

    The banner now routes through the module-level ``_verbose_console``
    instance (the _SilentAwareConsole wrapper) instead of constructing a
    fresh ``Console`` per call, so tests must monkey-patch the instance.
    """
    from conductor.cli import run as run_mod

    mock_console = MagicMock()
    monkeypatch.setattr(run_mod, "_verbose_console", mock_console)
    return mock_console


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

    mock_console = _patch_console(monkeypatch)

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

    assert mock_console.print.called
    args, _kwargs = mock_console.print.call_args
    panel = args[0]
    rendered = panel.renderable
    assert "Experimental provider in use" in rendered
    assert "claude-agent-sdk" in rendered
    assert "claude-agent-sdk>=0.1.0" in rendered
    assert "@lesandiz" in rendered


def test_banner_does_not_print_for_stable_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from conductor.cli import run as run_mod

    mock_console = _patch_console(monkeypatch)

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
    assert not mock_console.print.called


def test_banner_does_not_repeat_for_same_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replayed workflow_started events during resume must not double-print."""
    from conductor.cli import run as run_mod

    mock_console = _patch_console(monkeypatch)

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
    first_call_count = mock_console.print.call_count
    run_mod._maybe_print_experimental_banner(data)
    assert mock_console.print.call_count == first_call_count


def test_banner_handles_missing_providers_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Older event payloads without ``providers`` must not raise."""
    from conductor.cli import run as run_mod

    mock_console = _patch_console(monkeypatch)

    run_mod._maybe_print_experimental_banner({"name": "test"})
    assert not mock_console.print.called


def test_banner_lists_declared_limitations(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limitations line is auto-generated from the resolved capabilities."""
    from conductor.cli import run as run_mod
    from conductor.providers.capabilities import ProviderCapabilities

    mock_console = _patch_console(monkeypatch)

    # Banner re-resolves capabilities from the provider name. Patch the
    # resolver to return a descriptor with several limitations so we can
    # assert the generated text.
    fake_caps = ProviderCapabilities(
        tier="experimental",
        mcp_tools=False,
        workflow_tools_passthrough=True,
        streaming_events=False,
        agent_reasoning_events=True,
        reasoning_effort=("low", "medium", "high", "xhigh"),
        structured_output="native",
        interrupt=True,
        max_session_seconds=True,
        checkpoint_resume=False,
        usage_tracking=True,
        concurrent_safe=True,
    )
    monkeypatch.setattr(
        "conductor.providers.capabilities.get_capabilities",
        lambda name: fake_caps,
    )

    data = {
        "run_id": "x",
        "providers": {
            "p": {
                "name": "p",
                "status": "ok",
                "tier": "experimental",
                "upstream_pin": None,
                "maintainer": None,
            },
        },
    }
    run_mod._maybe_print_experimental_banner(data)
    rendered = mock_console.print.call_args[0][0].renderable
    assert "no MCP servers" in rendered
    assert "no streaming events" in rendered
    assert "no checkpoint resume" in rendered


def test_banner_reads_run_id_from_system_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_workflow_started_data() puts run_id at top level AND under system.

    The banner's idempotency key must handle either location so a real
    engine event doesn't trigger re-prints across resume / replay.
    """
    from conductor.cli import run as run_mod

    mock_console = _patch_console(monkeypatch)

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
    first_count = mock_console.print.call_count
    assert first_count > 0

    # Re-emit the same event (mimics resume replay). Banner must not re-print.
    run_mod._maybe_print_experimental_banner(data)
    assert mock_console.print.call_count == first_count


def test_banner_suppressed_when_verbose_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``verbose_mode`` is False (``--silent``), the banner does not reach stderr.

    The banner routes through ``_verbose_console`` (a ``_SilentAwareConsole``),
    whose ``.print`` is a no-op when ``is_verbose()`` is False. This test
    exercises the real instance via its underlying ``Console.print`` to
    confirm the gating wires through end-to-end.
    """
    from conductor.cli import run as run_mod
    from conductor.cli.app import verbose_mode

    # Override the autouse fixture's verbose=True for this test only.
    token = verbose_mode.set(False)
    try:
        # Replace the base-class ``Console.print`` (what the silent-aware
        # subclass would call when verbose is True) with a tracker.
        printed: list[object] = []
        monkeypatch.setattr(
            "rich.console.Console.print",
            lambda self, *args, **kwargs: printed.append(args),
        )
        data = {
            "run_id": "silent-test",
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
    finally:
        verbose_mode.reset(token)

    # _SilentAwareConsole.print early-returns when is_verbose() is False,
    # so the underlying Console.print is never reached.
    assert printed == []


def test_banner_prints_once_per_unique_experimental_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow with multiple experimental providers prints one banner each."""
    from conductor.cli import run as run_mod
    from conductor.providers.capabilities import ProviderCapabilities

    mock_console = _patch_console(monkeypatch)

    # Stub the resolver — banner re-resolves capabilities by name now that
    # the wire payload no longer ships the full descriptor.
    fake_caps = ProviderCapabilities(
        tier="experimental",
        mcp_tools=True,
        workflow_tools_passthrough=True,
        streaming_events=True,
        agent_reasoning_events=True,
        reasoning_effort=None,
        structured_output="native",
        interrupt=True,
        max_session_seconds=True,
        checkpoint_resume=True,
        usage_tracking=True,
        concurrent_safe=True,
    )
    monkeypatch.setattr(
        "conductor.providers.capabilities.get_capabilities",
        lambda name: fake_caps,
    )

    data = {
        "run_id": "multi",
        "providers": {
            "exp-one": {
                "name": "exp-one",
                "status": "ok",
                "tier": "experimental",
                "upstream_pin": None,
                "maintainer": None,
            },
            "exp-two": {
                "name": "exp-two",
                "status": "ok",
                "tier": "experimental",
                "upstream_pin": None,
                "maintainer": None,
            },
            "stable-one": {
                "name": "stable-one",
                "status": "ok",
                "tier": "stable",
                "upstream_pin": None,
                "maintainer": None,
            },
        },
    }
    run_mod._maybe_print_experimental_banner(data)
    # Two banners (one per experimental provider); stable-one is silent.
    assert mock_console.print.call_count == 2

    # Verify each banner mentions the right provider name.
    printed_names = []
    for call in mock_console.print.call_args_list:
        panel = call[0][0]
        printed_names.append(panel.renderable)
    assert any("exp-one" in p for p in printed_names)
    assert any("exp-two" in p for p in printed_names)
    assert not any("stable-one" in p for p in printed_names)
