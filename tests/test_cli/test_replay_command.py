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
from typing import Any
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

    Two pairs of tests are needed because there are two stderr-leak
    paths in the ``replay`` command:

    * **Inner path** — prints inside ``_run_replay`` after the dashboard
      starts (``app.py:994-996``). Exercised by faking
      ``asyncio.Event().wait()`` to raise ``CancelledError``.
    * **Outer path** — the ``Replay stopped`` print inside the
      ``except KeyboardInterrupt`` handler that wraps ``asyncio.run``
      (``app.py:1007-1009``). Exercised by making ``asyncio.run`` itself
      raise ``KeyboardInterrupt``.

    Each path also has a verbose counterpart to guard against an
    over-eager gate suppressing the message when the user did NOT pass
    ``--silent``.
    """

    @staticmethod
    def _mock_replay_dashboard() -> MagicMock:
        """Build a dashboard mock with async ``start``/``stop`` no-ops."""
        mock_dashboard = MagicMock()
        mock_dashboard.url = "http://127.0.0.1:9999"
        mock_dashboard.start = AsyncMock()
        mock_dashboard.stop = AsyncMock()
        return mock_dashboard

    @staticmethod
    def _patch_inner_wait_to_cancel() -> Any:
        """Patch ``asyncio.Event`` so ``await ... .wait()`` raises ``CancelledError``.

        Without this the replay command hangs forever on the
        ``await asyncio.Event().wait()`` it uses to keep the dashboard
        alive after start.
        """
        import asyncio

        mock_event = MagicMock()
        mock_event.wait = AsyncMock(side_effect=asyncio.CancelledError())
        return patch("asyncio.Event", return_value=mock_event)

    # ----- inner path: prints inside _run_replay -----

    def test_silent_replay_suppresses_dashboard_messages(self, tmp_path: Path) -> None:
        """``--silent replay`` must not leak the dashboard URL or hint to stderr."""
        log_path = _write_log_file(tmp_path)
        mock_dashboard = self._mock_replay_dashboard()

        with (
            patch(
                "conductor.web.replay.ReplayDashboard", return_value=mock_dashboard
            ) as mock_dashboard_cls,
            self._patch_inner_wait_to_cancel(),
        ):
            result = runner.invoke(app, ["--silent", "replay", str(log_path)])

        assert result.exit_code == 0, result.output
        assert mock_dashboard_cls.called
        stderr = _ANSI_RE.sub("", result.stderr)
        assert "Replay dashboard" not in stderr
        assert "Press Ctrl+C" not in stderr
        assert "http://127.0.0.1:9999" not in stderr

    def test_verbose_replay_prints_dashboard_messages(self, tmp_path: Path) -> None:
        """Sanity counterpart: without ``--silent`` the messages still appear."""
        log_path = _write_log_file(tmp_path)
        mock_dashboard = self._mock_replay_dashboard()

        with (
            patch("conductor.web.replay.ReplayDashboard", return_value=mock_dashboard),
            self._patch_inner_wait_to_cancel(),
        ):
            result = runner.invoke(app, ["replay", str(log_path)])

        assert result.exit_code == 0, result.output
        stderr = _ANSI_RE.sub("", result.stderr)
        assert "Replay dashboard" in stderr
        assert "http://127.0.0.1:9999" in stderr
        assert "Press Ctrl+C" in stderr

    def test_quiet_replay_prints_dashboard_messages(self, tmp_path: Path) -> None:
        """``--quiet`` (MINIMAL) must NOT suppress.

        Locks in the contract from ``app.py:204``
        (``verbose_mode.set(verbosity != ConsoleVerbosity.SILENT)``) — if
        a future refactor changes that line to ``== ConsoleVerbosity.FULL``,
        MINIMAL mode would start swallowing every progress message, which
        is not what ``--quiet`` is supposed to mean.
        """
        log_path = _write_log_file(tmp_path)
        mock_dashboard = self._mock_replay_dashboard()

        with (
            patch("conductor.web.replay.ReplayDashboard", return_value=mock_dashboard),
            self._patch_inner_wait_to_cancel(),
        ):
            result = runner.invoke(app, ["--quiet", "replay", str(log_path)])

        assert result.exit_code == 0, result.output
        stderr = _ANSI_RE.sub("", result.stderr)
        assert "Replay dashboard" in stderr
        assert "http://127.0.0.1:9999" in stderr

    # ----- outer path: print inside the KeyboardInterrupt handler -----

    def test_silent_replay_suppresses_keyboardinterrupt_message(self, tmp_path: Path) -> None:
        """``--silent replay`` must not leak ``Replay stopped`` on Ctrl+C.

        Drives the outer ``except KeyboardInterrupt`` branch by making
        ``asyncio.run`` itself raise — this is robust against future
        refactors of the wait primitive inside ``_run_replay``.
        """
        log_path = _write_log_file(tmp_path)

        with patch("asyncio.run", side_effect=KeyboardInterrupt()):
            result = runner.invoke(app, ["--silent", "replay", str(log_path)])

        assert result.exit_code == 0, result.output
        stderr = _ANSI_RE.sub("", result.stderr)
        assert "Replay stopped" not in stderr

    def test_verbose_replay_prints_keyboardinterrupt_message(self, tmp_path: Path) -> None:
        """Sanity counterpart: without ``--silent`` Ctrl+C still prints the hint."""
        log_path = _write_log_file(tmp_path)

        with patch("asyncio.run", side_effect=KeyboardInterrupt()):
            result = runner.invoke(app, ["replay", str(log_path)])

        assert result.exit_code == 0, result.output
        stderr = _ANSI_RE.sub("", result.stderr)
        assert "Replay stopped" in stderr
