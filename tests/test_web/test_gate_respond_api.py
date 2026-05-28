"""Tests for POST /api/gate-respond and GET /api/gate-status endpoints.

Covers:
- Valid gate-respond request returns 200 and payload lands on queue
- Missing selected_value returns 422
- Token mismatch when CONDUCTOR_GATE_TOKEN is set returns 403
- No token required when env var is unset
- Gate-status returns waiting state correctly
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from starlette.testclient import TestClient

from conductor.events import WorkflowEventEmitter
from conductor.web.server import WebDashboard


def _make_dashboard() -> tuple[WorkflowEventEmitter, WebDashboard]:
    """Create an emitter and dashboard pair for testing."""
    emitter = WorkflowEventEmitter()
    dashboard = WebDashboard(emitter, host="127.0.0.1", port=0)
    return emitter, dashboard


class TestGateRespondValidRequest:
    """POST /api/gate-respond with a valid body returns 200 and queues payload."""

    def test_valid_request_accepted(self) -> None:
        _, dashboard = _make_dashboard()
        with TestClient(dashboard.app) as client:
            resp = client.post(
                "/api/gate-respond",
                json={
                    "agent_name": "review-gate",
                    "selected_value": "approve",
                },
            )
            assert resp.status_code == 200
            assert resp.json() == {"status": "accepted"}

            # Verify payload landed on the queue
            msg = dashboard._gate_response_queue.get_nowait()
            assert msg["type"] == "gate_response"
            assert msg["agent_name"] == "review-gate"
            assert msg["selected_value"] == "approve"

    def test_valid_request_with_additional_input(self) -> None:
        _, dashboard = _make_dashboard()
        with TestClient(dashboard.app) as client:
            resp = client.post(
                "/api/gate-respond",
                json={
                    "agent_name": "review-gate",
                    "selected_value": "approve",
                    "additional_input": "Looks good to me",
                },
            )
            assert resp.status_code == 200

            msg = dashboard._gate_response_queue.get_nowait()
            assert msg["additional_input"] == "Looks good to me"


class TestGateRespondMissingFields:
    """POST /api/gate-respond with missing required fields returns 422."""

    def test_missing_selected_value(self) -> None:
        _, dashboard = _make_dashboard()
        with TestClient(dashboard.app) as client:
            resp = client.post(
                "/api/gate-respond",
                json={"agent_name": "review-gate"},
            )
            assert resp.status_code == 422
            assert "selected_value" in resp.json()["error"]

    def test_missing_agent_name(self) -> None:
        _, dashboard = _make_dashboard()
        with TestClient(dashboard.app) as client:
            resp = client.post(
                "/api/gate-respond",
                json={"selected_value": "approve"},
            )
            assert resp.status_code == 422
            assert "agent_name" in resp.json()["error"]


class TestGateRespondMalformedBody:
    """POST /api/gate-respond with malformed or non-dict JSON body returns 422."""

    def test_invalid_json_body(self) -> None:
        _, dashboard = _make_dashboard()
        with TestClient(dashboard.app) as client:
            resp = client.post(
                "/api/gate-respond",
                content="not json",
                headers={"content-type": "application/json"},
            )
            assert resp.status_code == 422
            assert "Invalid JSON" in resp.json()["error"]

    def test_non_dict_json_body(self) -> None:
        _, dashboard = _make_dashboard()
        with TestClient(dashboard.app) as client:
            resp = client.post(
                "/api/gate-respond",
                content='["a", "b"]',
                headers={"content-type": "application/json"},
            )
            assert resp.status_code == 422
            assert "JSON object" in resp.json()["error"]

    def test_null_json_body(self) -> None:
        _, dashboard = _make_dashboard()
        with TestClient(dashboard.app) as client:
            resp = client.post(
                "/api/gate-respond",
                content="null",
                headers={"content-type": "application/json"},
            )
            assert resp.status_code == 422
            assert "JSON object" in resp.json()["error"]


class TestGateRespondTokenAuth:
    """Token authentication for POST /api/gate-respond."""

    def test_token_mismatch_returns_403(self) -> None:
        _, dashboard = _make_dashboard()
        with (
            patch.dict(os.environ, {"CONDUCTOR_GATE_TOKEN": "correct-token"}),
            TestClient(dashboard.app) as client,
        ):
            resp = client.post(
                "/api/gate-respond",
                json={
                    "agent_name": "review-gate",
                    "selected_value": "approve",
                    "token": "wrong-token",
                },
            )
            assert resp.status_code == 403
            assert "token" in resp.json()["error"].lower()

    def test_missing_token_returns_403_when_required(self) -> None:
        _, dashboard = _make_dashboard()
        with (
            patch.dict(os.environ, {"CONDUCTOR_GATE_TOKEN": "correct-token"}),
            TestClient(dashboard.app) as client,
        ):
            resp = client.post(
                "/api/gate-respond",
                json={
                    "agent_name": "review-gate",
                    "selected_value": "approve",
                },
            )
            assert resp.status_code == 403

    def test_correct_token_accepted(self) -> None:
        _, dashboard = _make_dashboard()
        with (
            patch.dict(os.environ, {"CONDUCTOR_GATE_TOKEN": "correct-token"}),
            TestClient(dashboard.app) as client,
        ):
            resp = client.post(
                "/api/gate-respond",
                json={
                    "agent_name": "review-gate",
                    "selected_value": "approve",
                    "token": "correct-token",
                },
            )
            assert resp.status_code == 200

    def test_no_token_required_when_env_unset(self) -> None:
        _, dashboard = _make_dashboard()
        env = {k: v for k, v in os.environ.items() if k != "CONDUCTOR_GATE_TOKEN"}
        with (
            patch.dict(os.environ, env, clear=True),
            TestClient(dashboard.app) as client,
        ):
            resp = client.post(
                "/api/gate-respond",
                json={
                    "agent_name": "review-gate",
                    "selected_value": "approve",
                },
            )
            assert resp.status_code == 200


class TestGateStatus:
    """GET /api/gate-status endpoint."""

    def test_no_gate_waiting(self) -> None:
        _, dashboard = _make_dashboard()
        with TestClient(dashboard.app) as client:
            resp = client.get("/api/gate-status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["waiting"] is False
            assert data["agent_name"] is None

    def test_gate_waiting(self) -> None:
        _, dashboard = _make_dashboard()
        # Simulate the engine setting the gate waiting state
        dashboard._gate_waiting_agent = "review-gate"
        with TestClient(dashboard.app) as client:
            resp = client.get("/api/gate-status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["waiting"] is True
            assert data["agent_name"] == "review-gate"

    def test_gate_cleared_after_response(self) -> None:
        """wait_for_gate_response clears _gate_waiting_agent on return."""
        _, dashboard = _make_dashboard()

        async def _test() -> None:
            # Pre-queue a matching response
            dashboard._gate_response_queue.put_nowait({"agent_name": "g1", "selected_value": "ok"})
            result = await dashboard.wait_for_gate_response("g1")
            assert result["selected_value"] == "ok"
            assert dashboard._gate_waiting_agent is None

        asyncio.run(_test())
