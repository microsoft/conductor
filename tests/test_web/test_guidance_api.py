"""Tests for POST /api/guidance (issue #400).

Covers:
- Accepted request calls the sink and returns pending count
- Pre-sink latch drained when set_guidance_sink binds late
- 403 with CONDUCTOR_GATE_TOKEN set and a mismatched/missing header
- A token is always required, even when CONDUCTOR_GATE_TOKEN is unset (issue #397)
- 422 for invalid JSON / non-object body / missing / non-string / empty /
  over-length text
- 409 after a root workflow_completed event
- guidance_received reaches GET /api/state
- `paused` reflects agent_paused/agent_resumed
"""

from __future__ import annotations

import os
import time
from unittest.mock import patch

from starlette.testclient import TestClient

from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.web.server import WebDashboard
from tests.test_web.conftest import TEST_PORT, make_client


def _make_dashboard() -> tuple[WorkflowEventEmitter, WebDashboard]:
    """Create an emitter and dashboard pair for testing."""
    emitter = WorkflowEventEmitter()
    dashboard = WebDashboard(emitter, host="127.0.0.1", port=0)
    return emitter, dashboard


class TestGuidanceAccepted:
    """POST /api/guidance with a valid body returns 200 and calls the sink."""

    def test_accepted_calls_sink_and_returns_pending(self) -> None:
        _, dashboard = _make_dashboard()
        calls: list[str] = []
        dashboard.set_guidance_sink(lambda text: calls.append(text) or len(calls))

        with make_client(dashboard) as client:
            resp = client.post("/api/guidance", json={"text": "Be more concise"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "accepted"
            assert body["pending"] == 1
            assert body["paused"] is False

        assert calls == ["Be more concise"]

    def test_text_is_stripped(self) -> None:
        _, dashboard = _make_dashboard()
        calls: list[str] = []
        dashboard.set_guidance_sink(lambda text: calls.append(text) or len(calls))

        with make_client(dashboard) as client:
            resp = client.post("/api/guidance", json={"text": "  spaced out  "})
            assert resp.status_code == 200

        assert calls == ["spaced out"]


class TestGuidancePreSinkLatch:
    """Guidance submitted before set_guidance_sink binds is drained into it."""

    def test_pending_guidance_drained_when_sink_binds(self) -> None:
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.post("/api/guidance", json={"text": "queued before engine ready"})
            assert resp.status_code == 200
            assert resp.json()["pending"] == 1

        assert dashboard._pending_guidance == ["queued before engine ready"]

        calls: list[str] = []
        dashboard.set_guidance_sink(lambda text: calls.append(text) or len(calls))

        assert calls == ["queued before engine ready"]
        assert dashboard._pending_guidance == []


class TestGuidanceTokenAuth:
    """Token authentication for POST /api/guidance mirrors /api/gate-respond.

    Since issue #397, a token is *always* required -- the per-run minted
    token when ``CONDUCTOR_GATE_TOKEN`` is unset, or the env var's value
    when it is set. These tests build a bare client (host-matched, no
    default Authorization header) so each one controls exactly what token
    is presented.
    """

    def _bare_client(self, dashboard: WebDashboard) -> TestClient:
        """A host-matched client presenting no default Authorization header."""
        dashboard._actual_port = TEST_PORT
        return TestClient(
            dashboard.app,
            base_url=f"http://127.0.0.1:{TEST_PORT}",
            headers={"Content-Type": "application/json"},
        )

    def test_token_mismatch_returns_403(self) -> None:
        _, dashboard = _make_dashboard()
        dashboard.set_guidance_sink(lambda text: 1)
        with (
            patch.dict(os.environ, {"CONDUCTOR_GATE_TOKEN": "correct-token"}),
            self._bare_client(dashboard) as client,
        ):
            resp = client.post(
                "/api/guidance",
                json={"text": "hello"},
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert resp.status_code == 403

    def test_missing_token_returns_403_when_required(self) -> None:
        _, dashboard = _make_dashboard()
        dashboard.set_guidance_sink(lambda text: 1)
        with (
            patch.dict(os.environ, {"CONDUCTOR_GATE_TOKEN": "correct-token"}),
            self._bare_client(dashboard) as client,
        ):
            resp = client.post("/api/guidance", json={"text": "hello"})
            assert resp.status_code == 403

    def test_correct_token_accepted(self) -> None:
        _, dashboard = _make_dashboard()
        dashboard.set_guidance_sink(lambda text: 1)
        with (
            patch.dict(os.environ, {"CONDUCTOR_GATE_TOKEN": "correct-token"}),
            self._bare_client(dashboard) as client,
        ):
            resp = client.post(
                "/api/guidance",
                json={"text": "hello"},
                headers={"Authorization": "Bearer correct-token"},
            )
            assert resp.status_code == 200

    def test_minted_token_required_when_env_unset(self) -> None:
        """A token is required even when CONDUCTOR_GATE_TOKEN is unset (issue #397).

        Replaces the pre-#397 ``test_no_token_required_when_env_unset``,
        which encoded the opposite behavior -- "unset env var means no
        auth" -- that this change ends.
        """
        _, dashboard = _make_dashboard()
        dashboard.set_guidance_sink(lambda text: 1)
        env = {k: v for k, v in os.environ.items() if k != "CONDUCTOR_GATE_TOKEN"}
        with (
            patch.dict(os.environ, env, clear=True),
            self._bare_client(dashboard) as client,
        ):
            resp = client.post("/api/guidance", json={"text": "hello"})
            assert resp.status_code == 403

            resp = client.post(
                "/api/guidance",
                json={"text": "hello"},
                headers={"Authorization": f"Bearer {dashboard.token}"},
            )
            assert resp.status_code == 200


class TestGuidanceMalformedBody:
    """POST /api/guidance with malformed or invalid body returns 422."""

    def test_invalid_json_body(self) -> None:
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.post(
                "/api/guidance",
                content="not json",
                headers={"content-type": "application/json"},
            )
            assert resp.status_code == 422
            assert "Invalid JSON" in resp.json()["error"]

    def test_non_dict_json_body(self) -> None:
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.post(
                "/api/guidance",
                content='["a", "b"]',
                headers={"content-type": "application/json"},
            )
            assert resp.status_code == 422
            assert "JSON object" in resp.json()["error"]

    def test_missing_text_field(self) -> None:
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.post("/api/guidance", json={})
            assert resp.status_code == 422
            assert "text" in resp.json()["error"]

    def test_non_string_text_field(self) -> None:
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.post("/api/guidance", json={"text": 42})
            assert resp.status_code == 422

    def test_empty_after_strip_rejected(self) -> None:
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.post("/api/guidance", json={"text": "   "})
            assert resp.status_code == 422
            assert "empty" in resp.json()["error"].lower()

    def test_over_length_text_rejected(self) -> None:
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.post("/api/guidance", json={"text": "x" * 10_001})
            assert resp.status_code == 422
            assert "maximum length" in resp.json()["error"]

    def test_max_length_text_accepted(self) -> None:
        _, dashboard = _make_dashboard()
        dashboard.set_guidance_sink(lambda text: 1)
        with make_client(dashboard) as client:
            resp = client.post("/api/guidance", json={"text": "x" * 10_000})
            assert resp.status_code == 200


class TestGuidanceWorkflowCompleted:
    """POST /api/guidance after workflow completion returns 409."""

    def test_completed_workflow_returns_409(self) -> None:
        _, dashboard = _make_dashboard()
        dashboard._workflow_completed = True
        with make_client(dashboard) as client:
            resp = client.post("/api/guidance", json={"text": "too late"})
            assert resp.status_code == 409
            assert "completed" in resp.json()["error"].lower()


class TestGuidanceEventReachesState:
    """guidance_received lands in _event_history / GET /api/state."""

    def test_guidance_received_in_event_history(self) -> None:
        _, dashboard = _make_dashboard()
        dashboard.set_guidance_sink(lambda text: 1)
        with make_client(dashboard) as client:
            resp = client.post("/api/guidance", json={"text": "note this"})
            assert resp.status_code == 200

            state_resp = client.get("/api/state")
            events = state_resp.json()
            guidance_events = [e for e in events if e["type"] == "guidance_received"]
            assert len(guidance_events) == 1
            assert guidance_events[0]["data"]["text"] == "note this"
            assert guidance_events[0]["data"]["pending"] == 1


class TestGuidancePausedFlag:
    """`paused` in the response reflects agent_paused / agent_resumed events."""

    def test_paused_true_after_agent_paused_event(self) -> None:
        emitter, dashboard = _make_dashboard()
        dashboard.set_guidance_sink(lambda text: 1)
        emitter.emit(
            WorkflowEvent(type="agent_paused", timestamp=time.time(), data={"agent_name": "a"})
        )

        with make_client(dashboard) as client:
            resp = client.post("/api/guidance", json={"text": "hello"})
            assert resp.status_code == 200
            assert resp.json()["paused"] is True

    def test_paused_false_after_agent_resumed_event(self) -> None:
        emitter, dashboard = _make_dashboard()
        dashboard.set_guidance_sink(lambda text: 1)
        emitter.emit(
            WorkflowEvent(type="agent_paused", timestamp=time.time(), data={"agent_name": "a"})
        )
        emitter.emit(
            WorkflowEvent(type="agent_resumed", timestamp=time.time(), data={"agent_name": "a"})
        )

        with make_client(dashboard) as client:
            resp = client.post("/api/guidance", json={"text": "hello"})
            assert resp.status_code == 200
            assert resp.json()["paused"] is False
