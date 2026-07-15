"""Unit tests for per-agent working_directory support in the Copilot provider.

Tests cover:
- ``session_kwargs["working_directory"]`` reflects the engine-resolved
  ``agent.working_dir`` (falling back to ``os.getcwd()`` when unset).
- Stdio/local MCP server configs are stamped with ``working_directory`` per
  execution without mutating the shared ``self._mcp_servers`` dict.
- HTTP/SSE MCP server configs are never stamped.
- Session resume forwards ``working_directory`` + stamped ``mcp_servers``.
- A changed working directory skips resume and creates a fresh session.
- ``get_session_cwds`` / ``set_resume_session_cwds`` tracking used by
  checkpoint persistence.
"""

from __future__ import annotations

import copy
import logging
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import AgentDef
from conductor.providers.copilot import CopilotProvider


def _make_agent(name: str = "test_agent", working_dir: str | None = None) -> AgentDef:
    """Create a minimal AgentDef for testing."""
    return AgentDef(name=name, model="gpt-4o", prompt="Test prompt", working_dir=working_dir)


def _make_idle_session(session_id: str) -> AsyncMock:
    """Build a fake SDK session that resolves _send_and_wait immediately."""
    mock_session = AsyncMock()
    mock_session.session_id = session_id
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
    return mock_session


def _make_provider_with_client(
    create_session: AsyncMock | None = None,
    resume_session: AsyncMock | None = None,
    mcp_servers: dict[str, Any] | None = None,
) -> CopilotProvider:
    """Build a provider wired to a fake SDK client (SDK path, no mock_handler)."""
    provider = CopilotProvider(mcp_servers=mcp_servers)
    mock_client = AsyncMock()
    mock_client.create_session = create_session or AsyncMock()
    mock_client.resume_session = resume_session or AsyncMock()
    provider._client = mock_client
    provider._started = True
    return provider


async def _execute(provider: CopilotProvider, agent: AgentDef) -> None:
    """Run provider.execute with verbose helpers patched out."""
    with (
        patch("conductor.cli.app.is_verbose", return_value=False),
        patch("conductor.cli.app.is_full", return_value=False),
    ):
        await provider.execute(agent, {}, "Do work")


def _capture_create() -> tuple[AsyncMock, dict[str, Any]]:
    """Return (create_session mock, captured-kwargs dict)."""
    captured: dict[str, Any] = {}

    async def _create(**kwargs: Any) -> AsyncMock:
        captured.update(kwargs)
        return _make_idle_session("sess-new")

    return AsyncMock(side_effect=_create), captured


# ---------------------------------------------------------------------------
# Session working_directory
# ---------------------------------------------------------------------------


class TestSessionWorkingDirectory:
    """session_kwargs["working_directory"] must follow the resolved agent cwd."""

    @pytest.mark.asyncio
    async def test_working_directory_uses_agent_working_dir(self, tmp_path: Any) -> None:
        """Requirement: session working_directory equals resolved agent.working_dir."""
        create_session, captured = _capture_create()
        provider = _make_provider_with_client(create_session=create_session)

        await _execute(provider, _make_agent("a1", working_dir=str(tmp_path)))

        create_session.assert_called_once()
        assert captured["working_directory"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_working_directory_falls_back_to_os_getcwd(self) -> None:
        """Requirement: working_directory is os.getcwd() when agent.working_dir is None."""
        create_session, captured = _capture_create()
        provider = _make_provider_with_client(create_session=create_session)

        await _execute(provider, _make_agent("a1", working_dir=None))

        assert captured["working_directory"] == os.getcwd()

    @pytest.mark.asyncio
    async def test_session_cwd_tracked_for_checkpoint(self, tmp_path: Any) -> None:
        """Requirement: provider tracks resolved cwd per agent for checkpoint persistence."""
        create_session, _ = _capture_create()
        provider = _make_provider_with_client(create_session=create_session)

        await _execute(provider, _make_agent("a1", working_dir=str(tmp_path)))

        assert provider.get_session_cwds() == {"a1": str(tmp_path)}


# ---------------------------------------------------------------------------
# MCP server stamping
# ---------------------------------------------------------------------------


class TestMcpServerWorkingDirectoryStamping:
    """Per-execution copies of stdio MCP configs get working_directory stamped."""

    @pytest.mark.asyncio
    async def test_stdio_server_stamped_with_working_directory(self, tmp_path: Any) -> None:
        """Requirement: stdio MCP config carries working_directory == resolved cwd."""
        mcp_servers = {
            "fs": {"type": "stdio", "command": "npx", "args": ["server"], "tools": "*"},
        }
        create_session, captured = _capture_create()
        provider = _make_provider_with_client(
            create_session=create_session, mcp_servers=mcp_servers
        )

        await _execute(provider, _make_agent("a1", working_dir=str(tmp_path)))

        stamped = captured["mcp_servers"]["fs"]
        assert stamped["working_directory"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_http_and_sse_servers_not_stamped(self, tmp_path: Any) -> None:
        """Requirement: http/sse MCP configs must NOT receive working_directory."""
        mcp_servers = {
            "remote_http": {"type": "http", "url": "https://example.com/mcp", "tools": "*"},
            "remote_sse": {"type": "sse", "url": "https://example.com/sse", "tools": "*"},
            "local": {"type": "stdio", "command": "server", "tools": "*"},
        }
        create_session, captured = _capture_create()
        provider = _make_provider_with_client(
            create_session=create_session, mcp_servers=mcp_servers
        )

        await _execute(provider, _make_agent("a1", working_dir=str(tmp_path)))

        assert "working_directory" not in captured["mcp_servers"]["remote_http"]
        assert "working_directory" not in captured["mcp_servers"]["remote_sse"]
        assert captured["mcp_servers"]["local"]["working_directory"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_local_type_server_stamped(self, tmp_path: Any) -> None:
        """Requirement: type "local" (SDK alias for stdio) is stamped as well."""
        mcp_servers = {
            "fs": {"type": "local", "command": "server", "tools": "*"},
        }
        create_session, captured = _capture_create()
        provider = _make_provider_with_client(
            create_session=create_session, mcp_servers=mcp_servers
        )

        await _execute(provider, _make_agent("a1", working_dir=str(tmp_path)))

        assert captured["mcp_servers"]["fs"]["working_directory"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_shared_mcp_servers_dict_not_mutated(self, tmp_path: Any) -> None:
        """Requirement: self._mcp_servers must remain unchanged after execute (deep-equal)."""
        mcp_servers = {
            "fs": {"type": "stdio", "command": "npx", "args": ["server"], "tools": "*"},
            "remote": {"type": "http", "url": "https://example.com/mcp", "tools": "*"},
        }
        snapshot = copy.deepcopy(mcp_servers)
        create_session, _ = _capture_create()
        provider = _make_provider_with_client(
            create_session=create_session, mcp_servers=mcp_servers
        )

        await _execute(provider, _make_agent("a1", working_dir=str(tmp_path)))
        await _execute(provider, _make_agent("a2", working_dir=str(tmp_path)))

        assert provider._mcp_servers == snapshot
        assert mcp_servers == snapshot


# ---------------------------------------------------------------------------
# Resume path
# ---------------------------------------------------------------------------


class TestResumeWorkingDirectory:
    """resume_session receives working_directory + stamped mcp_servers."""

    @pytest.mark.asyncio
    async def test_resume_passes_working_directory_and_stamped_mcp(self, tmp_path: Any) -> None:
        """Requirement: resume_session is called with resolved cwd and stamped stdio configs."""
        mcp_servers = {
            "fs": {"type": "stdio", "command": "server", "tools": "*"},
            "remote": {"type": "http", "url": "https://example.com/mcp", "tools": "*"},
        }
        resumed = _make_idle_session("sess-resumed")
        resume_session = AsyncMock(return_value=resumed)
        create_session = AsyncMock()
        provider = _make_provider_with_client(
            create_session=create_session,
            resume_session=resume_session,
            mcp_servers=mcp_servers,
        )
        provider.set_resume_session_ids({"a1": "sid-old"})
        provider.set_resume_session_cwds({"a1": str(tmp_path)})

        await _execute(provider, _make_agent("a1", working_dir=str(tmp_path)))

        resume_session.assert_called_once()
        call = resume_session.call_args
        assert call.args[0] == "sid-old"
        assert call.kwargs["working_directory"] == str(tmp_path)
        assert call.kwargs["mcp_servers"]["fs"]["working_directory"] == str(tmp_path)
        assert "working_directory" not in call.kwargs["mcp_servers"]["remote"]
        create_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_changed_cwd_skips_resume_and_creates_new_session(
        self, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Requirement: resolved cwd != session-creation cwd => no resume, new session + warning."""
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        resume_session = AsyncMock()
        create_session, _ = _capture_create()
        provider = _make_provider_with_client(
            create_session=create_session,
            resume_session=resume_session,
        )
        provider.set_resume_session_ids({"a1": "sid-old"})
        provider.set_resume_session_cwds({"a1": str(tmp_path)})

        with caplog.at_level(logging.WARNING, logger="conductor.providers.copilot"):
            await _execute(provider, _make_agent("a1", working_dir=str(other_dir)))

        resume_session.assert_not_called()
        create_session.assert_called_once()
        assert any("working directory changed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_unknown_previous_cwd_resumes_by_id(self, tmp_path: Any) -> None:
        """Requirement: legacy checkpoints have no cwd record — resume by session id as before."""
        resumed = _make_idle_session("sess-resumed")
        resume_session = AsyncMock(return_value=resumed)
        create_session = AsyncMock()
        provider = _make_provider_with_client(
            create_session=create_session,
            resume_session=resume_session,
        )
        provider.set_resume_session_ids({"a1": "sid-old"})
        # No set_resume_session_cwds call — simulates a pre-cwd checkpoint.

        await _execute(provider, _make_agent("a1", working_dir=str(tmp_path)))

        resume_session.assert_called_once()
        create_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_cwd_matches_tracked_cwd_resumes(self) -> None:
        """Requirement: when resolved cwd equals the tracked cwd, resume proceeds."""
        resumed = _make_idle_session("sess-resumed")
        resume_session = AsyncMock(return_value=resumed)
        provider = _make_provider_with_client(resume_session=resume_session)
        provider.set_resume_session_ids({"a1": "sid-old"})
        provider.set_resume_session_cwds({"a1": os.getcwd()})

        await _execute(provider, _make_agent("a1", working_dir=None))

        resume_session.assert_called_once()
        assert resume_session.call_args.kwargs["working_directory"] == os.getcwd()


# ---------------------------------------------------------------------------
# Session cwd tracking API
# ---------------------------------------------------------------------------


class TestSessionCwdTracking:
    """get/set resume cwd mapping used by checkpoint persistence."""

    def test_set_resume_session_cwds_stores_copy(self) -> None:
        """Requirement: set_resume_session_cwds stores a copy of the mapping."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"r": 1})
        cwds = {"a1": "/repo/a"}
        provider.set_resume_session_cwds(cwds)
        cwds["a2"] = "/repo/b"
        assert provider.get_session_cwds() == {}

    def test_get_session_cwds_returns_copy(self) -> None:
        """Requirement: get_session_cwds returns a copy; mutations don't leak."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"r": 1})
        provider._session_cwds["a1"] = "/repo/a"
        result = provider.get_session_cwds()
        result["a2"] = "/repo/b"
        assert "a2" not in provider._session_cwds
