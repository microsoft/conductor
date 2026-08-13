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
- Token file: written on start() and round-trips through the reader,
  removed on stop(); mode 0600 on POSIX (Windows cannot express POSIX
  permission bits, so that assertion is skipped there)
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conductor.events import WorkflowEventEmitter
from conductor.web.auth import (
    OriginHostGuard,
    read_token_file,
    resolve_expected_token,
    token_file_path,
)
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


class TestNonAsciiTokenDoesNotCrash:
    """A non-ASCII presented token must 403, never crash the guard (issue #397).

    ``hmac.compare_digest`` raises ``TypeError`` on any ``str`` containing a
    non-ASCII character. ``presented`` is fully attacker-controlled: a
    latin-1-decoded ``Authorization`` header, or a ``?token=`` value that
    already went through ``token_from_scope``'s ``errors="replace"`` decode
    (which manufactures U+FFFD from a malformed percent-escape like
    ``%FF``). Before the fix, both of these turned an unauthenticated 403
    into an unhandled 500 with a traceback on every request.
    """

    def test_non_ascii_bearer_token_rejected_not_crashed(self) -> None:
        _, dashboard = _make_dashboard()
        dashboard._actual_port = TEST_PORT
        client = TestClient(
            dashboard.app,
            base_url=f"http://127.0.0.1:{TEST_PORT}",
            headers={"Content-Type": "application/json"},
        )
        # Raw bytes with the high bit set: httpx.Headers rejects a plain str
        # value it can't ascii-encode, but the wire itself carries bytes --
        # Starlette decodes the raw Authorization header via latin-1, which
        # is exactly what produces a non-ASCII `str` for constant_time_match
        # to compare.
        with client:
            resp = client.post(
                "/api/stop", headers=[(b"Authorization", "Bearer tökén".encode("latin-1"))]
            )
            assert resp.status_code == 403

    def test_percent_encoded_invalid_utf8_query_token_rejected_not_crashed(self) -> None:
        _, dashboard = _make_dashboard()
        dashboard._actual_port = TEST_PORT
        client = TestClient(
            dashboard.app,
            base_url=f"http://127.0.0.1:{TEST_PORT}",
            headers={"Content-Type": "application/json"},
        )
        with client:
            resp = client.post("/api/stop?token=%FF%FE")
            assert resp.status_code == 403

    def test_non_ascii_ws_query_token_rejected_not_crashed(self) -> None:
        _, dashboard = _make_dashboard()
        dashboard._actual_port = TEST_PORT
        bare = TestClient(dashboard.app, base_url=f"http://127.0.0.1:{TEST_PORT}")
        with (
            bare as client,
            pytest.raises(WebSocketDisconnect) as exc_info,
            ws_connect(client, dashboard, path="/ws?token=%FF%FE"),
        ):
            pass
        assert exc_info.value.code == 1008


def _guard_kwargs(dashboard: WebDashboard) -> dict[str, object]:
    """Return the ``OriginHostGuard`` middleware's constructor kwargs.

    Reads them back off the live app (``app.user_middleware``) rather than
    importing the hardcoded route lists from ``server.py``, so this stays a
    genuine regression check on the *registered* middleware rather than a
    second copy of the same list.
    """
    for mw in dashboard.app.user_middleware:
        if mw.cls is OriginHostGuard:
            return dict(mw.kwargs)
    raise AssertionError("OriginHostGuard is not registered on the app")


class TestGuardCoversEveryMutatingRoute:
    """Mutation-proof: the guard's route lists must cover every real route.

    Mutation testing (dropping `/api/gate-respond` / `/api/guidance` from
    `protected_paths` in `server.py`) passed the full suite before this test
    existed, because the parametrizations above hardcode the route list
    rather than deriving it from `app.routes`. These two tests instead read
    the actual FastAPI route table and assert it is a subset of what the
    guard protects, so a newly added mutating route or WebSocket route that
    isn't registered with the guard fails here even if no other test
    exercises it directly.
    """

    def test_every_mutating_http_route_is_registered_with_the_guard(self) -> None:
        _, dashboard = _make_dashboard()
        protected_paths = _guard_kwargs(dashboard)["protected_paths"]
        mutating_methods = {"POST", "PUT", "PATCH", "DELETE"}

        unprotected = []
        for route in dashboard.app.routes:
            if not isinstance(route, APIRoute):
                continue
            if route.methods & mutating_methods and route.path not in protected_paths:
                unprotected.append(route.path)

        assert unprotected == [], f"Mutating routes missing from protected_paths: {unprotected}"

    def test_every_websocket_route_is_registered_with_the_guard(self) -> None:
        _, dashboard = _make_dashboard()
        websocket_paths = _guard_kwargs(dashboard)["websocket_paths"]

        unprotected = [
            route.path
            for route in dashboard.app.routes
            if isinstance(route, APIWebSocketRoute) and route.path not in websocket_paths
        ]

        assert unprotected == [], f"WebSocket routes missing from websocket_paths: {unprotected}"


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

    def test_handshake_rejected_with_wrong_query_param_token(self) -> None:
        """Mutation-proof: weakening constant_time_match must not pass this.

        Weakening `constant_time_match` to `presented is not None` (in
        `auth.py`) passed the full suite before this test existed, because
        the only prior WS token tests covered an *absent* token and the
        *correct* one -- never a wrong one. WS has no per-message auth (only
        the handshake), so this is the one test standing between a typo'd
        comparison and every mutating dashboard action being reachable with
        any non-empty token.
        """
        _, dashboard = _make_dashboard()
        dashboard._actual_port = TEST_PORT
        bare = TestClient(dashboard.app, base_url=f"http://127.0.0.1:{TEST_PORT}")
        with (
            bare as client,
            pytest.raises(WebSocketDisconnect) as exc_info,
            ws_connect(client, dashboard, path="/ws?token=definitely-the-wrong-token"),
        ):
            pass
        assert exc_info.value.code == 1008

    def test_handshake_rejected_with_wrong_authorization_header_token(self) -> None:
        """Same as above, via the `Authorization: Bearer` extraction branch."""
        _, dashboard = _make_dashboard()
        dashboard._actual_port = TEST_PORT
        bare = TestClient(dashboard.app, base_url=f"http://127.0.0.1:{TEST_PORT}")
        with (
            bare as client,
            pytest.raises(WebSocketDisconnect) as exc_info,
            ws_connect(client, dashboard, headers={"Authorization": "Bearer wrong-token"}),
        ):
            pass
        assert exc_info.value.code == 1008

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


class TestAntiClickjackingHeaders:
    """Every HTTP response through the guard carries anti-framing headers.

    Without these, any page can `<iframe>` the dashboard and position it so
    a click lands on Kill/Stop/a gate Approve -- those clicks execute inside
    the dashboard's own origin, so the Host check, Origin check and token
    all pass by construction.
    """

    def test_index_response_has_security_headers(self) -> None:
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.get("/")
            assert resp.headers["x-frame-options"] == "DENY"
            assert resp.headers["content-security-policy"] == "frame-ancestors 'none'"
            assert resp.headers["x-content-type-options"] == "nosniff"
            assert resp.headers["referrer-policy"] == "no-referrer"

    def test_rejected_response_still_has_security_headers(self) -> None:
        """The headers must apply even on a 403 (a guard-rejected iframe load)."""
        _, dashboard = _make_dashboard()
        with make_client(dashboard) as client:
            resp = client.get("/api/state", headers={"Host": "evil.example"})
            assert resp.status_code == 403
            assert resp.headers["x-frame-options"] == "DENY"


class TestTokenFileLifecycle:
    """The token file is written on start(), removed on stop(), and round-trips."""

    async def test_written_on_start(self) -> None:
        emitter, dashboard = _make_dashboard()
        await dashboard.start()
        try:
            path = token_file_path(dashboard.port)
            assert path.exists()
        finally:
            await dashboard.stop()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX mode bits are not implemented on Windows; "
        "os.open's pmode only honours the write bit, so a writable "
        "file always reports 0o666.",
    )
    async def test_written_with_mode_0600(self) -> None:
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

    async def test_stop_does_not_delete_a_newer_runs_token_file(self) -> None:
        """A draining stop() must not delete a *newer* run's file on the same port.

        Mirrors the port-reuse hazard ``cli/pid.py::remove_pid_file_at``
        already guards against for PID files: run A's cleanup must not
        clobber run B's token file after the port has been reused.
        """
        from conductor.web.auth import write_token_file

        emitter, dashboard = _make_dashboard()
        await dashboard.start()
        port = dashboard.port

        # Simulate a newer run overwriting the file on the same port before
        # this dashboard's own stop() cleanup runs.
        write_token_file(port, "a-different-runs-token")

        await dashboard.stop()

        assert read_token_file(port) == "a-different-runs-token"

    async def test_round_trips_through_reader(self) -> None:
        emitter, dashboard = _make_dashboard()
        await dashboard.start()
        try:
            read_back = read_token_file(dashboard.port)
            assert read_back == resolve_expected_token(dashboard._token)
        finally:
            await dashboard.stop()

    async def test_written_token_matches_conductor_gate_token_override(self) -> None:
        """The file must hold the *resolved* token, not the raw minted one.

        Every validation path checks ``resolve_expected_token(self._token)``
        (``CONDUCTOR_GATE_TOKEN`` if set, else the minted token). Writing the
        raw minted value here would mean the file is useless -- and CLI
        auto-discovery guaranteed to 403 -- whenever the env var override is
        set, exactly the scenario this test pins.
        """
        emitter, dashboard = _make_dashboard()
        with patch.dict(os.environ, {"CONDUCTOR_GATE_TOKEN": "env-secret-123"}):
            await dashboard.start()
            try:
                assert read_token_file(dashboard.port) == "env-secret-123"
            finally:
                await dashboard.stop()

    def test_read_missing_token_file_returns_none(self) -> None:
        assert read_token_file(999999) is None
