"""Tests for ``conductor gate-respond`` CLI command.

Covers:
- Happy path with mock HTTP server
- Unreachable port returns clear error
- Token passed via Authorization header from --token flag
- Token read from CONDUCTOR_GATE_TOKEN env var
- Auto-discovery of agent name via /api/gate-status
- No gate waiting error
- Gate not waiting / agent mismatch (409) error
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import httpx
from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()


def _mock_response(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = json.dumps(json_data or {})
    resp.json.return_value = json_data or {}
    return resp


class TestGateRespondHappyPath:
    """Happy path: gate-respond with all required args."""

    @patch("httpx.post")
    def test_basic_resolve(self, mock_post: MagicMock) -> None:
        """gate-respond --port 8080 --choice approve --agent review-gate succeeds."""
        mock_post.return_value = _mock_response(200, {"status": "accepted"})

        result = runner.invoke(
            app, ["gate-respond", "--port", "8080", "--choice", "approve", "--agent", "review-gate"]
        )
        assert result.exit_code == 0
        assert "Gate resolved" in result.output

        # Verify the POST was called with correct body
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["agent_name"] == "review-gate"
        assert body["selected_value"] == "approve"

    @patch("httpx.post")
    def test_with_input_text(self, mock_post: MagicMock) -> None:
        """--input flag is forwarded as additional_input."""
        mock_post.return_value = _mock_response(200, {"status": "accepted"})

        result = runner.invoke(
            app,
            [
                "gate-respond",
                "--port",
                "8080",
                "--choice",
                "approve",
                "--agent",
                "g1",
                "--input",
                "LGTM",
            ],
        )
        assert result.exit_code == 0

        body = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert body["additional_input"] == "LGTM"


class TestGateRespondUnreachablePort:
    """Unreachable port produces a clear error."""

    @patch("httpx.post")
    def test_connect_error(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = httpx.ConnectError("connection refused")

        result = runner.invoke(
            app,
            ["gate-respond", "--port", "9999", "--choice", "approve", "--agent", "g1"],
        )
        assert result.exit_code == 1
        assert "Cannot connect" in result.output


class TestGateRespondTokenHandling:
    """Token auth via --token flag and CONDUCTOR_GATE_TOKEN env var."""

    @patch("httpx.post")
    def test_token_from_flag(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(200, {"status": "accepted"})

        result = runner.invoke(
            app,
            [
                "gate-respond",
                "--port",
                "8080",
                "--choice",
                "approve",
                "--agent",
                "g1",
                "--token",
                "my-secret",
            ],
        )
        assert result.exit_code == 0

        headers = mock_post.call_args.kwargs.get("headers") or mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer my-secret"
        # Token must NOT be sent in the JSON body.
        body = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert "token" not in body

    @patch("httpx.post")
    def test_token_from_env(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(200, {"status": "accepted"})

        with patch.dict(os.environ, {"CONDUCTOR_GATE_TOKEN": "env-token"}):
            result = runner.invoke(
                app,
                ["gate-respond", "--port", "8080", "--choice", "approve", "--agent", "g1"],
            )
        assert result.exit_code == 0

        headers = mock_post.call_args.kwargs.get("headers") or mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer env-token"

    @patch("httpx.post")
    def test_flag_token_overrides_env(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(200, {"status": "accepted"})

        with patch.dict(os.environ, {"CONDUCTOR_GATE_TOKEN": "env-token"}):
            result = runner.invoke(
                app,
                [
                    "gate-respond",
                    "--port",
                    "8080",
                    "--choice",
                    "approve",
                    "--agent",
                    "g1",
                    "--token",
                    "flag-token",
                ],
            )
        assert result.exit_code == 0

        headers = mock_post.call_args.kwargs.get("headers") or mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer flag-token"

    @patch("httpx.post")
    def test_no_auth_header_when_no_token(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(200, {"status": "accepted"})

        env = {k: v for k, v in os.environ.items() if k != "CONDUCTOR_GATE_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            result = runner.invoke(
                app,
                ["gate-respond", "--port", "8080", "--choice", "approve", "--agent", "g1"],
            )
        assert result.exit_code == 0

        headers = mock_post.call_args.kwargs.get("headers") or mock_post.call_args[1]["headers"]
        assert "Authorization" not in headers

    @patch("httpx.post")
    def test_403_error_message(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(403, {"error": "Invalid or missing token"})

        result = runner.invoke(
            app,
            ["gate-respond", "--port", "8080", "--choice", "approve", "--agent", "g1"],
        )
        assert result.exit_code == 1
        assert "Authentication failed" in result.output

    @patch("httpx.post")
    def test_409_error_message(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(
            409, {"error": "No human gate is currently waiting for a response"}
        )

        result = runner.invoke(
            app,
            ["gate-respond", "--port", "8080", "--choice", "approve", "--agent", "g1"],
        )
        assert result.exit_code == 1
        assert "waiting" in result.output.lower()


class TestGateRespondAutoDiscovery:
    """Auto-discovery of agent name via /api/gate-status."""

    @patch("httpx.post")
    @patch("httpx.get")
    def test_auto_discover_agent(self, mock_get: MagicMock, mock_post: MagicMock) -> None:
        mock_get.return_value = _mock_response(200, {"waiting": True, "agent_name": "auto-gate"})
        mock_post.return_value = _mock_response(200, {"status": "accepted"})

        result = runner.invoke(
            app,
            ["gate-respond", "--port", "8080", "--choice", "approve"],
        )
        assert result.exit_code == 0
        assert "auto-gate" in result.output

        body = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert body["agent_name"] == "auto-gate"

    @patch("httpx.get")
    def test_no_gate_waiting(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(200, {"waiting": False, "agent_name": None})

        result = runner.invoke(
            app,
            ["gate-respond", "--port", "8080", "--choice", "approve"],
        )
        assert result.exit_code == 1
        assert "No gate is currently waiting" in result.output

    @patch("httpx.get")
    def test_auto_discover_connect_error(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = httpx.ConnectError("refused")

        result = runner.invoke(
            app,
            ["gate-respond", "--port", "9999", "--choice", "approve"],
        )
        assert result.exit_code == 1
        assert "Cannot connect" in result.output
