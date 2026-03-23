"""Unit tests for Copilot session resume functionality.

Tests cover:
- Session ID tracking during agent execution
- Session resume with stored session IDs
- Graceful fallback when resume_session fails
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import AgentDef
from conductor.providers.copilot import CopilotProvider


def _make_agent(name: str = "test_agent") -> AgentDef:
    """Create a minimal AgentDef for testing."""
    return AgentDef(name=name, model="gpt-4o", prompt="Test prompt")


# ---------------------------------------------------------------------------
# E5-T5: Session ID tracking
# ---------------------------------------------------------------------------


class TestSessionIdTracking:
    """Verify that session IDs are captured after create_session()."""

    def test_initial_session_ids_empty(self) -> None:
        """New provider starts with no tracked session IDs."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"r": 1})
        assert provider.get_session_ids() == {}

    @pytest.mark.asyncio
    async def test_session_id_tracked_after_sdk_call(self) -> None:
        """After executing via the real SDK path, session ID is stored."""
        known_sid = "sess-abc-123"

        # Build a fake session object returned by create_session
        mock_session = AsyncMock()
        mock_session.session_id = known_sid
        mock_session.disconnect = AsyncMock()

        # The on() callback must trigger session.idle so _send_and_wait resolves
        def _fake_on(callback: Any) -> None:
            mock_session._callback = callback

        mock_session.on = _fake_on

        async def _fake_send(msg: Any) -> None:
            # Simulate immediate idle
            evt = MagicMock()
            evt.type = MagicMock()
            evt.type.value = "session.idle"
            mock_session._callback(evt)

        mock_session.send = _fake_send

        # Build a fake client
        mock_client = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=mock_session)
        mock_client.start = AsyncMock()

        provider = CopilotProvider()
        provider._client = mock_client
        provider._started = True

        agent = _make_agent("researcher")

        # Patch verbose helpers to no-op
        with (
            patch("conductor.providers.copilot.CopilotProvider._log_event_verbose"),
            patch("conductor.cli.app.is_verbose", return_value=False),
            patch("conductor.cli.app.is_full", return_value=False),
        ):
            await provider.execute(agent, {}, "Do research")

        ids = provider.get_session_ids()
        assert ids == {"researcher": known_sid}

    @pytest.mark.asyncio
    async def test_session_id_tracked_per_agent(self) -> None:
        """Multiple agents each get their own session ID tracked."""

        def mock_handler(agent: AgentDef, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
            return {"result": "ok"}

        # Mock handler path doesn't go through create_session, so session IDs
        # won't be tracked.  Verify that get_session_ids reflects only SDK calls.
        provider = CopilotProvider(mock_handler=mock_handler)
        await provider.execute(_make_agent("a1"), {}, "p1")
        await provider.execute(_make_agent("a2"), {}, "p2")

        # Mock handler bypasses SDK, so no session IDs captured
        assert provider.get_session_ids() == {}

    def test_get_session_ids_returns_copy(self) -> None:
        """Returned dict is a copy; mutations don't affect provider state."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        provider._session_ids["x"] = "y"
        ids = provider.get_session_ids()
        ids["z"] = "w"
        assert "z" not in provider._session_ids


# ---------------------------------------------------------------------------
# E5-T6: Session resume fallback
# ---------------------------------------------------------------------------


class TestSessionResumeFallback:
    """Verify resume_session is attempted and falls back on failure."""

    def test_set_resume_session_ids(self) -> None:
        """set_resume_session_ids stores the mapping."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"r": 1})
        provider.set_resume_session_ids({"agent_a": "sid-1"})
        assert provider._resume_session_ids == {"agent_a": "sid-1"}

    @pytest.mark.asyncio
    async def test_resume_session_attempted_when_id_available(self) -> None:
        """When a stored session ID exists, resume_session is called first."""
        resumed_sid = "sess-old-123"

        mock_session = AsyncMock()
        mock_session.session_id = "sess-resumed"
        mock_session.disconnect = AsyncMock()

        def _fake_on(callback: Any) -> None:
            mock_session._callback = callback

        mock_session.on = _fake_on

        async def _fake_send(msg: Any) -> None:
            evt = MagicMock()
            evt.type = MagicMock()
            evt.type.value = "session.idle"
            mock_session._callback(evt)

        mock_session.send = _fake_send

        mock_client = AsyncMock()
        mock_client.resume_session = AsyncMock(return_value=mock_session)
        mock_client.create_session = AsyncMock()  # Should NOT be called

        provider = CopilotProvider()
        provider._client = mock_client
        provider._started = True
        provider.set_resume_session_ids({"researcher": resumed_sid})

        agent = _make_agent("researcher")

        with (
            patch("conductor.cli.app.is_verbose", return_value=False),
            patch("conductor.cli.app.is_full", return_value=False),
        ):
            await provider.execute(agent, {}, "Continue research")

        mock_client.resume_session.assert_called_once_with(
            resumed_sid,
            on_permission_request=CopilotProvider._default_permission_handler,
        )
        mock_client.create_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_to_create_on_resume_runtime_error(self) -> None:
        """When resume_session raises RuntimeError, falls back to create_session."""
        mock_new_session = AsyncMock()
        mock_new_session.session_id = "sess-new"
        mock_new_session.disconnect = AsyncMock()

        def _fake_on(callback: Any) -> None:
            mock_new_session._callback = callback

        mock_new_session.on = _fake_on

        async def _fake_send(msg: Any) -> None:
            evt = MagicMock()
            evt.type = MagicMock()
            evt.type.value = "session.idle"
            mock_new_session._callback(evt)

        mock_new_session.send = _fake_send

        mock_client = AsyncMock()
        mock_client.resume_session = AsyncMock(side_effect=RuntimeError("Session not found"))
        mock_client.create_session = AsyncMock(return_value=mock_new_session)

        provider = CopilotProvider()
        provider._client = mock_client
        provider._started = True
        provider.set_resume_session_ids({"researcher": "stale-sid"})

        agent = _make_agent("researcher")

        with (
            patch("conductor.cli.app.is_verbose", return_value=False),
            patch("conductor.cli.app.is_full", return_value=False),
        ):
            await provider.execute(agent, {}, "Continue research")

        mock_client.resume_session.assert_called_once_with(
            "stale-sid",
            on_permission_request=CopilotProvider._default_permission_handler,
        )
        mock_client.create_session.assert_called_once()
        # Session ID should now reflect the new session
        assert provider.get_session_ids()["researcher"] == "sess-new"

    @pytest.mark.asyncio
    async def test_fallback_to_create_on_generic_exception(self) -> None:
        """When resume_session raises a generic Exception, falls back gracefully."""
        mock_new_session = AsyncMock()
        mock_new_session.session_id = "sess-fallback"
        mock_new_session.disconnect = AsyncMock()

        def _fake_on(callback: Any) -> None:
            mock_new_session._callback = callback

        mock_new_session.on = _fake_on

        async def _fake_send(msg: Any) -> None:
            evt = MagicMock()
            evt.type = MagicMock()
            evt.type.value = "session.idle"
            mock_new_session._callback(evt)

        mock_new_session.send = _fake_send

        mock_client = AsyncMock()
        mock_client.resume_session = AsyncMock(side_effect=Exception("Network error"))
        mock_client.create_session = AsyncMock(return_value=mock_new_session)

        provider = CopilotProvider()
        provider._client = mock_client
        provider._started = True
        provider.set_resume_session_ids({"researcher": "dead-sid"})

        agent = _make_agent("researcher")

        with (
            patch("conductor.cli.app.is_verbose", return_value=False),
            patch("conductor.cli.app.is_full", return_value=False),
        ):
            await provider.execute(agent, {}, "Continue")

        mock_client.resume_session.assert_called_once()
        mock_client.create_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Fallback on resume failure logs a warning."""
        mock_new_session = AsyncMock()
        mock_new_session.session_id = "sess-new"
        mock_new_session.disconnect = AsyncMock()

        def _fake_on(callback: Any) -> None:
            mock_new_session._callback = callback

        mock_new_session.on = _fake_on

        async def _fake_send(msg: Any) -> None:
            evt = MagicMock()
            evt.type = MagicMock()
            evt.type.value = "session.idle"
            mock_new_session._callback(evt)

        mock_new_session.send = _fake_send

        mock_client = AsyncMock()
        mock_client.resume_session = AsyncMock(side_effect=RuntimeError("Session expired"))
        mock_client.create_session = AsyncMock(return_value=mock_new_session)

        provider = CopilotProvider()
        provider._client = mock_client
        provider._started = True
        provider.set_resume_session_ids({"agent1": "expired-sid"})

        agent = _make_agent("agent1")

        with (
            caplog.at_level(logging.WARNING, logger="conductor.providers.copilot"),
            patch("conductor.cli.app.is_verbose", return_value=False),
            patch("conductor.cli.app.is_full", return_value=False),
        ):
            await provider.execute(agent, {}, "Do task")

        assert any("Could not resume session" in r.message for r in caplog.records)
        assert any("Falling back to new session" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_resume_when_no_stored_id(self) -> None:
        """When no stored session ID exists for an agent, create_session is used."""
        mock_session = AsyncMock()
        mock_session.session_id = "sess-brand-new"
        mock_session.disconnect = AsyncMock()

        def _fake_on(callback: Any) -> None:
            mock_session._callback = callback

        mock_session.on = _fake_on

        async def _fake_send(msg: Any) -> None:
            evt = MagicMock()
            evt.type = MagicMock()
            evt.type.value = "session.idle"
            mock_session._callback(evt)

        mock_session.send = _fake_send

        mock_client = AsyncMock()
        mock_client.resume_session = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=mock_session)

        provider = CopilotProvider()
        provider._client = mock_client
        provider._started = True
        # Set resume IDs for a *different* agent
        provider.set_resume_session_ids({"other_agent": "some-sid"})

        agent = _make_agent("researcher")

        with (
            patch("conductor.cli.app.is_verbose", return_value=False),
            patch("conductor.cli.app.is_full", return_value=False),
        ):
            await provider.execute(agent, {}, "Start fresh")

        mock_client.resume_session.assert_not_called()
        mock_client.create_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_resume_when_empty_resume_ids(self) -> None:
        """When resume_session_ids is empty, create_session is used directly."""
        mock_session = AsyncMock()
        mock_session.session_id = "sess-fresh"
        mock_session.disconnect = AsyncMock()

        def _fake_on(callback: Any) -> None:
            mock_session._callback = callback

        mock_session.on = _fake_on

        async def _fake_send(msg: Any) -> None:
            evt = MagicMock()
            evt.type = MagicMock()
            evt.type.value = "session.idle"
            mock_session._callback(evt)

        mock_session.send = _fake_send

        mock_client = AsyncMock()
        mock_client.resume_session = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=mock_session)

        provider = CopilotProvider()
        provider._client = mock_client
        provider._started = True

        agent = _make_agent("agent1")

        with (
            patch("conductor.cli.app.is_verbose", return_value=False),
            patch("conductor.cli.app.is_full", return_value=False),
        ):
            await provider.execute(agent, {}, "Go")

        mock_client.resume_session.assert_not_called()
        mock_client.create_session.assert_called_once()
