"""Tests for the ``conductor guide`` CLI command.

Mirrors ``test_gate.py``. Covers:
- Happy path with explicit --port
- Auto-discovery of port when exactly one background workflow is running
- No background workflow running -> exit 1
- Multiple background workflows running -> list + exit 1
- Token from --token flag and from CONDUCTOR_GATE_TOKEN env var
- 403 / 409 / 422 / connect-error messages
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the token-file lookup at tmp_path (issue #397).

    ``guide`` resolves a token via ``conductor.web.auth.resolve_cli_token``,
    which falls back to reading the dashboard token file for the target port
    when neither ``--token`` nor ``CONDUCTOR_GATE_TOKEN`` is set. Without
    this, tests would read the developer's real ``~/.conductor/runs``.
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr("conductor.rundir.runs_dir", lambda: runs_dir)


def _mock_response(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = json.dumps(json_data or {})
    resp.json.return_value = json_data or {}
    return resp


class TestGuideHappyPath:
    """Happy path: guide --text with explicit --port."""

    @patch("httpx.post")
    def test_basic_send(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(
            200, {"status": "accepted", "pending": 1, "paused": False}
        )

        result = runner.invoke(
            app,
            ["guide", "--port", "8080", "--text", "Prefer Python 3.12 examples"],
        )
        assert result.exit_code == 0
        assert "Guidance sent" in result.output

        body = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert body["text"] == "Prefer Python 3.12 examples"

    @patch("httpx.post")
    def test_paused_response_mentions_resume(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(
            200, {"status": "accepted", "pending": 0, "paused": True}
        )

        result = runner.invoke(
            app,
            ["guide", "--port", "8080", "--text", "Skip the benchmark step"],
        )
        assert result.exit_code == 0
        assert "resume" in result.output.lower()


class TestGuideAutoDiscovery:
    """Auto-discovery of the dashboard port via scan_pid_files()."""

    @patch("httpx.post")
    @patch("conductor.cli.pid.scan_pid_files")
    def test_auto_discover_single_running(self, mock_scan: MagicMock, mock_post: MagicMock) -> None:
        mock_scan.return_value = [{"pid": 123, "port": 9090, "workflow": "wf.yaml"}]
        mock_post.return_value = _mock_response(
            200, {"status": "accepted", "pending": 1, "paused": False}
        )

        result = runner.invoke(app, ["guide", "--text", "hello"])
        assert result.exit_code == 0

        called_args = mock_post.call_args
        url = called_args.args[0] if called_args.args else called_args.kwargs.get("url", "")
        assert "9090" in url

    @patch("conductor.cli.pid.scan_pid_files")
    def test_no_workflows_running(self, mock_scan: MagicMock) -> None:
        mock_scan.return_value = []

        result = runner.invoke(app, ["guide", "--text", "hello"])
        assert result.exit_code == 1
        assert "No background workflows" in result.output

    @patch("conductor.cli.pid.scan_pid_files")
    def test_multiple_workflows_running(self, mock_scan: MagicMock) -> None:
        mock_scan.return_value = [
            {"pid": 1, "port": 9090, "workflow": "a.yaml"},
            {"pid": 2, "port": 9091, "workflow": "b.yaml"},
        ]

        result = runner.invoke(app, ["guide", "--text", "hello"])
        assert result.exit_code == 1
        assert "Multiple" in result.output


class TestGuideTokenHandling:
    """Token auth via --token flag and CONDUCTOR_GATE_TOKEN env var."""

    @patch("httpx.post")
    def test_token_from_flag(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(
            200, {"status": "accepted", "pending": 1, "paused": False}
        )

        result = runner.invoke(
            app,
            [
                "guide",
                "--port",
                "8080",
                "--text",
                "hello",
                "--token",
                "my-secret",
            ],
        )
        assert result.exit_code == 0

        headers = mock_post.call_args.kwargs.get("headers") or mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer my-secret"

    @patch("httpx.post")
    def test_token_from_env(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(
            200, {"status": "accepted", "pending": 1, "paused": False}
        )

        with patch.dict(os.environ, {"CONDUCTOR_GATE_TOKEN": "env-token"}):
            result = runner.invoke(app, ["guide", "--port", "8080", "--text", "hello"])
        assert result.exit_code == 0

        headers = mock_post.call_args.kwargs.get("headers") or mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer env-token"

    @patch("httpx.post")
    def test_no_auth_header_when_no_token_anywhere(self, mock_post: MagicMock) -> None:
        """No flag, no env var, and no token file -> no Authorization header."""
        mock_post.return_value = _mock_response(
            200, {"status": "accepted", "pending": 1, "paused": False}
        )

        env = {k: v for k, v in os.environ.items() if k != "CONDUCTOR_GATE_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            result = runner.invoke(app, ["guide", "--port", "8080", "--text", "hello"])
        assert result.exit_code == 0

        headers = mock_post.call_args.kwargs.get("headers") or mock_post.call_args[1]["headers"]
        assert "Authorization" not in headers

    @patch("httpx.post")
    def test_token_file_used_when_no_flag_or_env(self, mock_post: MagicMock) -> None:
        """The token file for the target port is picked up as a last resort (issue #397)."""
        from conductor.web.auth import write_token_file

        write_token_file(8080, "file-token")
        mock_post.return_value = _mock_response(
            200, {"status": "accepted", "pending": 1, "paused": False}
        )

        env = {k: v for k, v in os.environ.items() if k != "CONDUCTOR_GATE_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            result = runner.invoke(app, ["guide", "--port", "8080", "--text", "hello"])
        assert result.exit_code == 0

        headers = mock_post.call_args.kwargs.get("headers") or mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer file-token"

    @patch("httpx.post")
    def test_flag_token_overrides_token_file(self, mock_post: MagicMock) -> None:
        """--token still wins over a present token file."""
        from conductor.web.auth import write_token_file

        write_token_file(8080, "file-token")
        mock_post.return_value = _mock_response(
            200, {"status": "accepted", "pending": 1, "paused": False}
        )

        result = runner.invoke(
            app,
            ["guide", "--port", "8080", "--text", "hello", "--token", "flag-token"],
        )
        assert result.exit_code == 0

        headers = mock_post.call_args.kwargs.get("headers") or mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer flag-token"

    @patch("httpx.post")
    def test_env_token_overrides_token_file(self, mock_post: MagicMock) -> None:
        """CONDUCTOR_GATE_TOKEN still wins over a present token file."""
        from conductor.web.auth import write_token_file

        write_token_file(8080, "file-token")
        mock_post.return_value = _mock_response(
            200, {"status": "accepted", "pending": 1, "paused": False}
        )

        with patch.dict(os.environ, {"CONDUCTOR_GATE_TOKEN": "env-token"}):
            result = runner.invoke(app, ["guide", "--port", "8080", "--text", "hello"])
        assert result.exit_code == 0

        headers = mock_post.call_args.kwargs.get("headers") or mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer env-token"


class TestGuideErrorResponses:
    """Server error responses surface clear messages."""

    @patch("httpx.post")
    def test_connect_error(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = httpx.ConnectError("connection refused")

        result = runner.invoke(app, ["guide", "--port", "9999", "--text", "hello"])
        assert result.exit_code == 1
        assert "Cannot connect" in result.output

    @patch("httpx.post")
    def test_403_error_message(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(403, {"error": "Invalid or missing token"})

        result = runner.invoke(app, ["guide", "--port", "8080", "--text", "hello"])
        assert result.exit_code == 1
        assert "Authentication failed" in result.output

    @patch("httpx.post")
    def test_409_error_message(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(409, {"error": "Workflow has already completed"})

        result = runner.invoke(app, ["guide", "--port", "8080", "--text", "hello"])
        assert result.exit_code == 1
        assert "completed" in result.output.lower()

    @patch("httpx.post")
    def test_422_error_message(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(422, {"error": "text must not be empty"})

        result = runner.invoke(app, ["guide", "--port", "8080", "--text", "hello"])
        assert result.exit_code == 1
        assert "text must not be empty" in result.output

    @patch("httpx.post")
    def test_unexpected_status_code(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(500, {"error": "internal"})

        result = runner.invoke(app, ["guide", "--port", "8080", "--text", "hello"])
        assert result.exit_code == 1
        assert "Unexpected response" in result.output
        assert "500" in result.output

    @patch("httpx.post")
    def test_post_http_error(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = httpx.HTTPError("boom")

        result = runner.invoke(app, ["guide", "--port", "8080", "--text", "hello"])
        assert result.exit_code == 1
        assert "Request failed" in result.output
