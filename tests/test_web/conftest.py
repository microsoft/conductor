"""Shared fixtures and helpers for ``tests/test_web/``.

The ``OriginHostGuard`` middleware (issue #397) rejects any request whose
``Host`` header doesn't name the dashboard's bound host:port, and rejects
any mutating request or WebSocket handshake that doesn't carry the
resolved token. ``starlette.testclient.TestClient`` defaults to
``base_url="http://testserver"`` and sends no ``Authorization`` header, so
a bare ``TestClient(dashboard.app)`` now gets a 403 on almost everything.
Tests in this package should build clients via :func:`make_client` /
:func:`make_replay_client` instead.

Run-registry isolation (``rundir.runs_dir()`` -> a temp directory) is
provided globally by the autouse ``_isolated_runs_dir`` fixture in
``tests/conftest.py``.
"""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient, WebSocketTestSession

from conductor.web.replay import ReplayDashboard
from conductor.web.server import WebDashboard

# Fixed port used by tests that don't care about a specific value.
# WebDashboard.port only reflects a real bound port once start() runs (which
# most tests here never call), so tests instead pin _actual_port to this.
TEST_PORT = 18080


def make_client(dashboard: WebDashboard, *, port: int = TEST_PORT) -> TestClient:
    """Build a ``TestClient`` that is host-matched and pre-authenticated.

    Pins ``dashboard._actual_port`` so ``OriginHostGuard`` has a concrete
    port to check the request's ``Host`` header against — in production
    that field is set by ``start()`` once the socket binds, which none of
    these tests call. The default ``Authorization`` header carries the
    dashboard's resolved token, so ordinary requests (including through
    ``client.post(...)``) authenticate without further ceremony.

    Args:
        dashboard: The dashboard under test.
        port: The port to pretend the dashboard is bound to.

    Returns:
        A ``TestClient`` ready to use as a context manager.
    """
    dashboard._actual_port = port
    return TestClient(
        dashboard.app,
        base_url=f"http://127.0.0.1:{port}",
        headers={
            "Authorization": f"Bearer {dashboard.token}",
            "Content-Type": "application/json",
        },
    )


def make_replay_client(dashboard: ReplayDashboard, *, port: int = TEST_PORT) -> TestClient:
    """Build a host-matched ``TestClient`` for a ``ReplayDashboard``.

    No token is needed: the replay app is read-only and GET-only.
    """
    dashboard._actual_port = port
    return TestClient(dashboard.app, base_url=f"http://127.0.0.1:{port}")


def ws_connect(
    client: TestClient, dashboard: WebDashboard, path: str = "/ws", **kwargs: Any
) -> WebSocketTestSession:
    """Open a WebSocket connection with the ``Host`` header the guard expects.

    ``starlette.testclient.TestClient.websocket_connect`` builds its request
    URL via ``urljoin("ws://testserver", url)`` **regardless of the
    client's own** ``base_url``, so the ``Host`` header must be set
    explicitly on every call — the client's default ``Authorization``
    header (set by :func:`make_client`) merges in automatically since that
    one *is* honored by the underlying ``httpx.Client``.

    Args:
        client: A client built by :func:`make_client`.
        dashboard: The dashboard the client targets (for its bound port).
        path: The WebSocket path to connect to.
        **kwargs: Forwarded to ``TestClient.websocket_connect``.

    Returns:
        The WebSocket test session.
    """
    headers = kwargs.pop("headers", {})
    headers.setdefault("host", f"127.0.0.1:{dashboard.port}")
    return client.websocket_connect(path, headers=headers, **kwargs)
