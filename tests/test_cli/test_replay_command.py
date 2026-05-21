"""Tests for the replay CLI command.

Tests cover:
- Help text
- Missing file error
- Invalid file format error
- Successful invocation (mocked server)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_log_file(tmp_path: Path, events: list[dict] | None = None) -> Path:
    """Write a sample event log file."""
    if events is None:
        events = [
            {"type": "workflow_started", "timestamp": 1.0, "data": {"name": "test"}},
            {"type": "agent_started", "timestamp": 2.0, "data": {"agent_name": "a1"}},
            {"type": "agent_completed", "timestamp": 3.0, "data": {"agent_name": "a1"}},
        ]
    log_path = tmp_path / "test-log.json"
    log_path.write_text(json.dumps(events))
    return log_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReplayCommand:
    """Tests for the replay CLI command."""

    def test_help(self) -> None:
        """Replay command shows help text."""
        result = runner.invoke(app, ["replay", "--help"])
        assert result.exit_code == 0
        clean = _ANSI_RE.sub("", result.output)
        assert "Replay a recorded workflow" in clean

    def test_missing_file(self, tmp_path: Path) -> None:
        """Replay with a nonexistent file shows error."""
        result = runner.invoke(app, ["replay", str(tmp_path / "nonexistent.json")])
        assert result.exit_code != 0

    def test_invalid_file_format(self, tmp_path: Path) -> None:
        """Replay with an invalid file shows error."""
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not valid json at all garbage")
        result = runner.invoke(app, ["replay", str(bad_file)])
        assert result.exit_code != 0

    @patch("asyncio.run")
    def test_invocation_calls_asyncio_run(self, mock_run: AsyncMock, tmp_path: Path) -> None:
        """Replay command calls asyncio.run with a coroutine."""
        log_path = _write_log_file(tmp_path)
        runner.invoke(app, ["replay", str(log_path)])
        # asyncio.run is called; the mock prevents actual server start
        mock_run.assert_called_once()

    def test_custom_port_option(self) -> None:
        """Replay command accepts --web-port option."""
        result = runner.invoke(app, ["replay", "--help"])
        clean = _ANSI_RE.sub("", result.output)
        assert "--web-port" in clean


class TestReplaySilentCompliance:
    """Regression tests for issue #209.

    PR #211 gated the dashboard URL print in ``_run_replay`` behind
    ``is_verbose()`` but missed the ``Press Ctrl+C to exit`` and
    ``Replay stopped`` prints. Under ``--silent`` those messages still
    leaked to stderr, violating the ``--silent`` contract ("No progress
    output. Only JSON result on stdout.").
    """

    def test_silent_replay_suppresses_all_stderr(self, tmp_path: Path) -> None:
        """``conductor --silent replay <log>`` must not write anything to stderr.

        Mocks the dashboard so ``start``/``stop`` are cheap, and patches
        ``asyncio.Event`` so the ``await ... .wait()`` raises
        ``CancelledError`` immediately (otherwise the test would hang
        on the infinite wait the replay command uses to keep the
        dashboard alive).
        """
        import asyncio

        log_path = _write_log_file(tmp_path)

        mock_dashboard = MagicMock()
        mock_dashboard.url = "http://127.0.0.1:9999"
        mock_dashboard.start = AsyncMock()
        mock_dashboard.stop = AsyncMock()

        mock_event = MagicMock()
        mock_event.wait = AsyncMock(side_effect=asyncio.CancelledError())

        with (
            patch(
                "conductor.web.replay.ReplayDashboard", return_value=mock_dashboard
            ) as mock_dashboard_cls,
            patch("asyncio.Event", return_value=mock_event),
        ):
            result = runner.invoke(app, ["--silent", "replay", str(log_path)])

        assert result.exit_code == 0, result.output
        assert mock_dashboard_cls.called
        clean = _ANSI_RE.sub("", result.output)
        # All three messages from `_run_replay` must be suppressed.
        assert "Replay dashboard" not in clean
        assert "Press Ctrl+C" not in clean
        assert "http://127.0.0.1:9999" not in clean

    def test_verbose_replay_still_prints_dashboard_info(self, tmp_path: Path) -> None:
        """Sanity check: without ``--silent`` the messages still appear.

        Guards against an over-eager gate that would also suppress the
        prints when the user actually wants them.
        """
        import asyncio

        log_path = _write_log_file(tmp_path)

        mock_dashboard = MagicMock()
        mock_dashboard.url = "http://127.0.0.1:9999"
        mock_dashboard.start = AsyncMock()
        mock_dashboard.stop = AsyncMock()

        mock_event = MagicMock()
        mock_event.wait = AsyncMock(side_effect=asyncio.CancelledError())

        with (
            patch("conductor.web.replay.ReplayDashboard", return_value=mock_dashboard),
            patch("asyncio.Event", return_value=mock_event),
        ):
            result = runner.invoke(app, ["replay", str(log_path)])

        assert result.exit_code == 0, result.output
        clean = _ANSI_RE.sub("", result.output)
        assert "Replay dashboard" in clean
        assert "http://127.0.0.1:9999" in clean
        assert "Press Ctrl+C" in clean
