"""Tests for the OriginHostGuard middleware and dashboard token auth (issue #397).

Covers:
- Origin/Host validation on a GET, on every mutating POST, and on the /ws
  handshake
- Absent Origin allowed (the httpx / curl / conductor gate respond path)
- CONDUCTOR_WEB_ALLOW_ORIGINS dev escape hatch
- Missing/incorrect token -> 403 on every mutating route; correct token -> 200
- WS handshake rejected without a token, accepted with ?token=, and once
  rejected gate_response / dialog_message / dialog_decline /
  iteration_limit_response are all unreachable
- CONDUCTOR_GATE_TOKEN overriding the minted token
- Non-JSON Content-Type on a mutating route -> 415
- GET / injects the token; the replay app's / does not, and the replay app
  still enforces origin/host
- Token file: written on start() with mode 0600, removed on stop(), and
  round-trips through the reader
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conductor.events import WorkflowEventEmitter
from conductor.web.auth import read_token_file, resolve_expected_token, token_file_path
from conductor.web.replay import ReplayDashboard
from conductor.web.server import WebDashboard
from tests.test_web.conftest import TEST_PORT, make_client, make_replay_client, ws_connect


def _make_dashboard() -> tuple[WorkflowEventEmitter, WebDashboard]:
    """Create an emitter and dashboard pair for testing."""
    emitter = WorkflowEventEmitter()
    dashboard = WebDashboard(emitter, host="127.0.0.1", port=0)
    return emitter, dashboard


_MUTATING_NO_BODY_ROUTES = ["/api/stop", "/api/kill", "/api/resume"]
_MUTATING_JSON_ROUTES: dict[str, dict[str, object]] = {
    "/api/gate-respond": {"agent_name": "review-gate", "selected_value": "approve"},
    "/api/guidance": {"text": "hello"},
}


class TestOriginHostGuardHttp:
    """Origin/Host validation for plain HTTP requests."""

    def test_foreign_origin_on_get_rejected(self) -> None:
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.get("/api/state", headers={"Origin": "http://evil.example"})
            assert resp.status_code == 403

    @pytest.mark.parametrize("route", _MUTATING_NO_BODY_ROUTES)
    def test_foreign_origin_on_mutating_post_rejected(self, route: str) -> None:
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.post(route, headers={"Origin": "http://evil.example"})
            assert resp.status_code == 403

    def test_absent_origin_allowed(self) -> None:
        """httpx, curl, and `conductor gate respond` send no Origin header at all."""
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.get("/api/state")
            assert resp.status_code == 200

    def test_foreign_host_rejected(self) -> None:
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.get("/api/state", headers={"Host": "evil.example"})
            assert resp.status_code == 403

    def test_right_host_wrong_port_rejected(self) -> None:
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.get("/api/state", headers={"Host": "127.0.0.1:9999"})
            assert resp.status_code == 403

    def test_allow_origins_env_admits_extra_origin(self) -> None:
        _, dashboard = _make_dashboard()
        with (
            patch.dict(os.environ, {"CONDUCTOR_WEB_ALLOW_ORIGINS": "http://localhost:5173"}),
            make_client(dashboard) as client,
        ):
            resp = client.get("/api/state", headers={"Origin": "http://localhost:5173"})
            assert resp.status_code == 200

    def test_allow_origins_env_does_not_admit_unlisted_origin(self) -> None:
        """Setting the env var extends the allowlist -- it doesn't disable checking."""
        _, dashboard = _make_dashboard()
        with (
            patch.dict(os.environ, {"CONDUCTOR_WEB_ALLOW_ORIGINS": "http://localhost:5173"}),
            make_client(dashboard) as client,
        ):
            resp = client.get("/api/state", headers={"Origin": "http://evil.example"})
            assert resp.status_code == 403


class TestOriginHostGuardWebSocket:
    """The WS handshake gets the same Origin/Host validation as HTTP."""

    def test_foreign_origin_on_handshake_rejected(self) -> None:
        _, dashboard = _make_dashboard()
        with (
            make_client(dashboard) as client,
            pytest.raises(WebSocketDisconnect) as exc_info,
            ws_connect(client, dashboard, headers={"Origin": "http://evil.example"}),
        ):
            pass
        assert exc_info.value.code == 1008

    def test_foreign_host_on_handshake_rejected(self) -> None:
        _, dashboard = _make_dashboard()
        with (
            make_client(dashboard) as client,
            pytest.raises(WebSocketDisconnect) as exc_info,
            ws_connect(client, dashboard, headers={"host": "evil.example"}),
        ):
            pass
        assert exc_info.value.code == 1008


class TestMutatingRouteTokenAuth:
    """Every mutating route requires the resolved token."""

    def _bare_client(self, dashboard: WebDashboard) -> TestClient:
        dashboard._actual_port = TEST_PORT
        return TestClient(
            dashboard.app,
            base_url=f"http://127.0.0.1:{TEST_PORT}",
            headers={"Content-Type": "application/json"},
        )

    @pytest.mark.parametrize("route", _MUTATING_NO_BODY_ROUTES)
    def test_missing_token_rejected(self, route: str) -> None:
        _, dashboard = _make_dashboard()
        with self._bare_client(dashboard) as client:
            resp = client.post(route)
            assert resp.status_code == 403

    @pytest.mark.parametrize("route", _MUTATING_NO_BODY_ROUTES)
    def test_incorrect_token_rejected(self, route: str) -> None:
        _, dashboard = _make_dashboard()
        with self._bare_client(dashboard) as client:
            resp = client.post(route, headers={"Authorization": "Bearer wrong-token"})
            assert resp.status_code == 403

    @pytest.mark.parametrize("route", _MUTATING_NO_BODY_ROUTES)
    def test_correct_token_accepted(self, route: str) -> None:
        _, dashboard = _make_dashboard()
        with self._bare_client(dashboard) as client:
            resp = client.post(route, headers={"Authorization": f"Bearer {dashboard.token}"})
            assert resp.status_code == 200

    @pytest.mark.parametrize("route", list(_MUTATING_JSON_ROUTES))
    def test_missing_token_rejected_json_routes(self, route: str) -> None:
        _, dashboard = _make_dashboard()
        if route == "/api/gate-respond":
            dashboard._gate_waiting_agent = "review-gate"
        else:
            dashboard.set_guidance_sink(lambda text: 1)
        with self._bare_client(dashboard) as client:
            resp = client.post(route, json=_MUTATING_JSON_ROUTES[route])
            assert resp.status_code == 403

    @pytest.mark.parametrize("route", list(_MUTATING_JSON_ROUTES))
    def test_correct_token_accepted_json_routes(self, route: str) -> None:
        _, dashboard = _make_dashboard()
        if route == "/api/gate-respond":
            dashboard._gate_waiting_agent = "review-gate"
        else:
            dashboard.set_guidance_sink(lambda text: 1)
        with self._bare_client(dashboard) as client:
            resp = client.post(
                route,
                json=_MUTATING_JSON_ROUTES[route],
                headers={"Authorization": f"Bearer {dashboard.token}"},
            )
            assert resp.status_code == 200

    def test_conductor_gate_token_overrides_minted_token(self) -> None:
        """CONDUCTOR_GATE_TOKEN, when set, is the token that must be presented."""
        _, dashboard = _make_dashboard()
        with (
            patch.dict(os.environ, {"CONDUCTOR_GATE_TOKEN": "env-token"}),
            self._bare_client(dashboard) as client,
        ):
            # The minted per-run token no longer works once the env var is set.
            resp = client.post(
                "/api/resume", headers={"Authorization": f"Bearer {dashboard._token}"}
            )
            assert resp.status_code == 403

            resp = client.post("/api/resume", headers={"Authorization": "Bearer env-token"})
            assert resp.status_code == 200


class TestWebSocketHandshakeTokenAuth:
    """The /ws handshake authenticates the connection; no per-message auth exists."""

    def test_handshake_rejected_without_token(self) -> None:
        _, dashboard = _make_dashboard()
        dashboard._actual_port = TEST_PORT
        bare = TestClient(dashboard.app, base_url=f"http://127.0.0.1:{TEST_PORT}")
        with (
            bare as client,
            pytest.raises(WebSocketDisconnect) as exc_info,
            ws_connect(client, dashboard),
        ):
            pass
        assert exc_info.value.code == 1008

    def test_handshake_accepted_with_query_param_token(self) -> None:
        _, dashboard = _make_dashboard()
        dashboard._actual_port = TEST_PORT
        bare = TestClient(dashboard.app, base_url=f"http://127.0.0.1:{TEST_PORT}")
        with (
            bare as client,
            ws_connect(client, dashboard, path=f"/ws?token={dashboard.token}") as ws,
        ):
            assert ws is not None

    def test_rejected_handshake_leaves_gate_and_dialog_queues_empty(self) -> None:
        """Once the handshake is rejected the socket never reaches accept(),

        so no message of any type -- gate_response, dialog_message,
        dialog_decline, or iteration_limit_response -- can ever land on its
        queue. The connection never gets the chance to send anything.
        """
        _, dashboard = _make_dashboard()
        dashboard._actual_port = TEST_PORT
        bare = TestClient(dashboard.app, base_url=f"http://127.0.0.1:{TEST_PORT}")
        with (
            bare as client,
            pytest.raises(WebSocketDisconnect),
            ws_connect(client, dashboard),
        ):
            pass

        assert dashboard._gate_response_queue.empty()
        assert dashboard._dialog_response_queue.empty()
        assert dashboard._iteration_limit_response_queue.empty()


class TestContentTypeEnforcement:
    """Mutating routes require Content-Type: application/json."""

    @pytest.mark.parametrize("route", _MUTATING_NO_BODY_ROUTES)
    def test_non_json_content_type_rejected(self, route: str) -> None:
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.post(route, headers={"Content-Type": "text/plain"})
            assert resp.status_code == 415

    def test_missing_content_type_rejected(self) -> None:
        _, dashboard = _make_dashboard()
        dashboard._actual_port = TEST_PORT
        with TestClient(
            dashboard.app,
            base_url=f"http://127.0.0.1:{TEST_PORT}",
            headers={"Authorization": f"Bearer {dashboard.token}"},
        ) as client:
            resp = client.post(
                "/api/stop",
                headers={"Content-Type": ""},
            )
            assert resp.status_code == 415


class TestIndexTokenInjection:
    """GET / injects the token for the dashboard; the replay app does not."""

    def test_dashboard_index_injects_token(self) -> None:
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.get("/")
            assert resp.status_code == 200
            assert "__CONDUCTOR_TOKEN__" in resp.text
            assert dashboard.token in resp.text

    def test_replay_index_does_not_inject_token(self, tmp_path: Path) -> None:
        import json

        log = tmp_path / "log.json"
        log.write_text(json.dumps([{"type": "workflow_started", "timestamp": 1.0, "data": {}}]))
        dashboard = ReplayDashboard(log)
        with make_replay_client(dashboard) as client:
            resp = client.get("/")
            assert resp.status_code == 200
            assert "__CONDUCTOR_TOKEN__" not in resp.text

    def test_replay_app_enforces_origin_host(self, tmp_path: Path) -> None:
        import json

        log = tmp_path / "log.json"
        log.write_text(json.dumps([{"type": "workflow_started", "timestamp": 1.0, "data": {}}]))
        dashboard = ReplayDashboard(log)
        with make_replay_client(dashboard) as client:
            resp = client.get("/api/state", headers={"Host": "evil.example"})
            assert resp.status_code == 403


class TestTokenFileLifecycle:
    """The token file is written on start(), removed on stop(), and round-trips."""

    async def test_written_on_start_with_mode_0600(self) -> None:
        emitter, dashboard = _make_dashboard()
        await dashboard.start()
        try:
            path = token_file_path(dashboard.port)
            assert path.exists()
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o600
        finally:
            await dashboard.stop()

    async def test_removed_on_stop(self) -> None:
        emitter, dashboard = _make_dashboard()
        await dashboard.start()
        port = dashboard.port
        await dashboard.stop()

        assert not token_file_path(port).exists()

    async def test_round_trips_through_reader(self) -> None:
        emitter, dashboard = _make_dashboard()
        await dashboard.start()
        try:
            read_back = read_token_file(dashboard.port)
            assert read_back == resolve_expected_token(dashboard._token)
        finally:
            await dashboard.stop()

    def test_read_missing_token_file_returns_none(self) -> None:
        assert read_token_file(999999) is None
