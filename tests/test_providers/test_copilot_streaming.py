"""Tests for streaming kwarg behavior in CopilotProvider session creation.

This module verifies the fix for tool-call argument truncation that occurs
when the Copilot SDK is configured WITHOUT streaming.

Background
----------
The Copilot SDK's ``create_session`` accepts a ``streaming`` parameter that
the conductor provider previously did not set, causing the SDK to default to
non-streaming mode. In non-streaming mode the model must emit its entire
turn (text + tool_use blocks + arguments) under a single per-turn output
budget. For agents that issue large tool-call arguments (e.g., ``create``
with multi-KB ``file_text``), that budget is exhausted mid-JSON and the CLI
silently executes the partial tool call (e.g. ``{"path": "..."}`` with
``file_text`` missing). The model sees the tool succeed with no content,
retries the same broken call, and loops indefinitely until the wall clock
fires.

The interactive ``copilot`` CLI defaults to streaming, which is why the
same model + tool combination works there but not via the SDK without
this flag.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import AgentDef
from conductor.providers.copilot import CopilotProvider


def _make_agent(name: str = "writer") -> AgentDef:
    return AgentDef(name=name, model="claude-opus-4.7-1m-internal", prompt="Write a file")


def _build_mocked_provider() -> tuple[CopilotProvider, AsyncMock]:
    """Construct a CopilotProvider with a mocked SDK client.

    Returns the provider and the mock client (so callers can inspect the
    kwargs passed to ``create_session``).
    """
    mock_session = AsyncMock()
    mock_session.session_id = "sess-test"
    mock_session.disconnect = AsyncMock()

    def _fake_on(callback: Any) -> None:
        mock_session._callback = callback

    mock_session.on = _fake_on

    async def _fake_send(_msg: Any) -> None:
        # Resolve the session immediately so _send_and_wait returns.
        evt = MagicMock()
        evt.type = MagicMock()
        evt.type.value = "session.idle"
        mock_session._callback(evt)

    mock_session.send = _fake_send

    mock_client = AsyncMock()
    mock_client.create_session = AsyncMock(return_value=mock_session)
    mock_client.start = AsyncMock()

    provider = CopilotProvider()
    provider._client = mock_client
    provider._started = True
    return provider, mock_client


@pytest.mark.asyncio
async def test_create_session_called_with_streaming_true() -> None:
    """``create_session`` must be invoked with ``streaming=True``.

    Without this kwarg the SDK falls back to non-streaming, which causes
    tool-call argument truncation under output-budget pressure (see module
    docstring).
    """
    provider, mock_client = _build_mocked_provider()

    with (
        patch("conductor.providers.copilot.CopilotProvider._log_event_verbose"),
        patch("conductor.cli.app.is_verbose", return_value=False),
        patch("conductor.cli.app.is_full", return_value=False),
    ):
        await provider.execute(_make_agent(), {}, "Write a comprehensive document")

    mock_client.create_session.assert_called_once()
    kwargs = mock_client.create_session.call_args.kwargs

    assert "streaming" in kwargs, (
        "create_session must be called with an explicit `streaming` kwarg; "
        "without it the SDK falls back to non-streaming mode and large "
        "tool-call arguments get silently truncated."
    )
    assert kwargs["streaming"] is True, (
        f"streaming must be True (got {kwargs['streaming']!r}). Non-streaming "
        "causes the model's per-turn output budget to be exhausted mid-JSON "
        "for large tool calls (e.g. `create` with multi-KB `file_text`)."
    )


@pytest.mark.asyncio
async def test_create_session_preserves_existing_kwargs() -> None:
    """Adding ``streaming`` must not displace other session kwargs."""
    provider, mock_client = _build_mocked_provider()

    with (
        patch("conductor.providers.copilot.CopilotProvider._log_event_verbose"),
        patch("conductor.cli.app.is_verbose", return_value=False),
        patch("conductor.cli.app.is_full", return_value=False),
    ):
        await provider.execute(_make_agent(), {}, "Write a comprehensive document")

    kwargs = mock_client.create_session.call_args.kwargs
    # These three are the existing required kwargs; the streaming fix
    # must not regress them.
    assert kwargs.get("model") == "claude-opus-4.7-1m-internal"
    assert "on_permission_request" in kwargs
    assert "working_directory" in kwargs
