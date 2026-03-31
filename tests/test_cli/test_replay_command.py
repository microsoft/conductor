"""Tests for the replay CLI command.

Tests cover:
- Help text
- Missing file error
- Invalid file format error
- Successful invocation (mocked server)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()


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
        assert "Replay a recorded workflow" in result.output

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
        assert "--web-port" in result.output
