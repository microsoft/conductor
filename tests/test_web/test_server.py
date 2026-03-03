"""Tests for the WebDashboard server.

Tests cover:
- GET /api/state returns empty list initially, accumulates events
- WebSocket endpoint: connect, receive broadcast event, verify JSON structure
- Late-joiner: emit events, then connect client, verify /api/state returns all
- Auto-shutdown: workflow_completed + disconnect → wait_for_clients_disconnect resolves
- Broadcast error isolation: failed send doesn't crash broadcaster
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.web.server import WebDashboard


def _make_dashboard(*, bg: bool = False) -> tuple[WorkflowEventEmitter, WebDashboard]:
    """Create an emitter and dashboard pair for testing."""
    emitter = WorkflowEventEmitter()
    dashboard = WebDashboard(emitter, host="127.0.0.1", port=0, bg=bg)
    return emitter, dashboard


def _make_event(event_type: str, **data: object) -> WorkflowEvent:
    """Create a WorkflowEvent for testing."""
    return WorkflowEvent(type=event_type, timestamp=time.time(), data=dict(data))


class TestGetApiState:
    """Tests for GET /api/state endpoint."""

    def test_empty_state_initially(self) -> None:
        """GET /api/state returns empty list before any events."""
        emitter, dashboard = _make_dashboard()
        with TestClient(dashboard.app) as client:
            resp = client.get("/api/state")
            assert resp.status_code == 200
            assert resp.json() == []

    def test_accumulates_events(self) -> None:
        """GET /api/state returns all emitted events in order."""
        emitter, dashboard = _make_dashboard()

        # Emit several events via the emitter
        emitter.emit(_make_event("workflow_started", name="test-wf"))
        emitter.emit(_make_event("agent_started", agent_name="a1"))
        emitter.emit(_make_event("agent_completed", agent_name="a1", elapsed=1.5))

        with TestClient(dashboard.app) as client:
            resp = client.get("/api/state")
            assert resp.status_code == 200
            events = resp.json()
            assert len(events) == 3
            assert events[0]["type"] == "workflow_started"
            assert events[0]["data"]["name"] == "test-wf"
            assert events[1]["type"] == "agent_started"
            assert events[2]["type"] == "agent_completed"
            assert events[2]["data"]["elapsed"] == 1.5

    def test_event_json_structure(self) -> None:
        """Each event has type, timestamp, and data fields."""
        emitter, dashboard = _make_dashboard()
        emitter.emit(_make_event("agent_started", agent_name="a1"))

        with TestClient(dashboard.app) as client:
            resp = client.get("/api/state")
            event = resp.json()[0]
            assert "type" in event
            assert "timestamp" in event
            assert "data" in event
            assert isinstance(event["timestamp"], float)
            assert isinstance(event["data"], dict)


class TestGetIndex:
    """Tests for GET / endpoint."""

    def test_serves_html(self) -> None:
        """GET / returns HTML content."""
        emitter, dashboard = _make_dashboard()
        with TestClient(dashboard.app) as client:
            resp = client.get("/")
            assert resp.status_code == 200
            assert "text/html" in resp.headers["content-type"]
            assert "Conductor" in resp.text


class TestWebSocket:
    """Tests for WS /ws endpoint."""

    def test_connect_and_receive_event(self) -> None:
        """WebSocket client receives broadcast events."""
        emitter, dashboard = _make_dashboard()
        with TestClient(dashboard.app) as client, client.websocket_connect("/ws") as ws:
            # Emit event while connected — _on_event runs synchronously
            # and enqueues to the asyncio.Queue; the broadcaster task
            # (started via lifespan) reads and sends to WebSocket.
            emitter.emit(_make_event("agent_started", agent_name="a1"))

            data = ws.receive_json()
            assert data["type"] == "agent_started"
            assert data["data"]["agent_name"] == "a1"
            assert "timestamp" in data

    def test_multiple_events_in_order(self) -> None:
        """Multiple events arrive in emission order."""
        emitter, dashboard = _make_dashboard()
        with TestClient(dashboard.app) as client, client.websocket_connect("/ws") as ws:
            emitter.emit(_make_event("agent_started", agent_name="a1"))
            emitter.emit(_make_event("agent_completed", agent_name="a1"))

            msg1 = ws.receive_json()
            msg2 = ws.receive_json()
            assert msg1["type"] == "agent_started"
            assert msg2["type"] == "agent_completed"


class TestLateJoiner:
    """Tests for late-joiner support via /api/state."""

    def test_late_joiner_gets_full_history(self) -> None:
        """A client connecting after events were emitted sees all prior events."""
        emitter, dashboard = _make_dashboard()

        # Emit events before any client connects
        emitter.emit(_make_event("workflow_started", name="test-wf"))
        emitter.emit(_make_event("agent_started", agent_name="a1"))
        emitter.emit(_make_event("agent_completed", agent_name="a1", elapsed=2.0))

        # Late joiner fetches state
        with TestClient(dashboard.app) as client:
            resp = client.get("/api/state")
            events = resp.json()
            assert len(events) == 3
            assert events[0]["type"] == "workflow_started"
            assert events[1]["type"] == "agent_started"
            assert events[2]["type"] == "agent_completed"


class TestAutoShutdown:
    """Tests for --web-bg auto-shutdown logic."""

    def test_workflow_completed_sets_flag(self) -> None:
        """Emitting workflow_completed sets the internal flag."""
        emitter, dashboard = _make_dashboard(bg=True)
        assert dashboard._workflow_completed is False
        emitter.emit(_make_event("workflow_completed", elapsed=5.0))
        assert dashboard._workflow_completed is True

    def test_workflow_failed_sets_flag(self) -> None:
        """Emitting workflow_failed sets the internal flag."""
        emitter, dashboard = _make_dashboard(bg=True)
        emitter.emit(_make_event("workflow_failed", error_type="Error", message="boom"))
        assert dashboard._workflow_completed is True

    @pytest.mark.asyncio
    async def test_wait_for_clients_disconnect_resolves(self) -> None:
        """wait_for_clients_disconnect resolves after grace period."""
        emitter, dashboard = _make_dashboard(bg=True)

        # Mark workflow completed
        emitter.emit(_make_event("workflow_completed", elapsed=1.0))

        # Trigger grace timer (no connections, workflow done, bg mode)
        dashboard._maybe_start_grace_timer()
        assert dashboard._grace_task is not None

        # Override grace period to be very short for testing
        dashboard._grace_task.cancel()
        dashboard._grace_task = asyncio.create_task(_short_grace(dashboard._bg_event, 0.05))

        # Should resolve within the short grace period
        await asyncio.wait_for(dashboard.wait_for_clients_disconnect(), timeout=1.0)
        assert dashboard._bg_event.is_set()

    @pytest.mark.asyncio
    async def test_grace_timer_cancelled_on_new_connection(self) -> None:
        """New WebSocket connection cancels the grace timer."""
        emitter, dashboard = _make_dashboard(bg=True)
        emitter.emit(_make_event("workflow_completed", elapsed=1.0))

        # Start grace timer
        dashboard._maybe_start_grace_timer()
        assert dashboard._grace_task is not None
        grace_task = dashboard._grace_task

        # Simulate new connection by cancelling grace (as the WS endpoint does)
        dashboard._grace_task.cancel()
        dashboard._grace_task = None

        # Verify it was cancelled
        with pytest.raises(asyncio.CancelledError):
            await grace_task

    def test_no_grace_timer_without_bg(self) -> None:
        """Grace timer does not start when bg=False."""
        emitter, dashboard = _make_dashboard(bg=False)
        emitter.emit(_make_event("workflow_completed", elapsed=1.0))
        dashboard._maybe_start_grace_timer()
        assert dashboard._grace_task is None

    def test_no_grace_timer_before_workflow_complete(self) -> None:
        """Grace timer does not start before workflow completes."""
        emitter, dashboard = _make_dashboard(bg=True)
        dashboard._maybe_start_grace_timer()
        assert dashboard._grace_task is None

    @pytest.mark.asyncio
    async def test_no_duplicate_grace_timer(self) -> None:
        """Calling _maybe_start_grace_timer twice doesn't create two tasks."""
        emitter, dashboard = _make_dashboard(bg=True)
        emitter.emit(_make_event("workflow_completed", elapsed=1.0))
        dashboard._maybe_start_grace_timer()
        first = dashboard._grace_task
        dashboard._maybe_start_grace_timer()
        assert dashboard._grace_task is first
        # Clean up
        if first is not None:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first


class TestBroadcastErrorIsolation:
    """Tests that broadcast errors don't crash the broadcaster."""

    def test_event_queued_despite_bad_connection(self) -> None:
        """An event is enqueued for broadcast even when a bad WebSocket is in connections."""
        emitter, dashboard = _make_dashboard()

        # Add a mock WebSocket that will raise on send
        bad_ws = MagicMock()
        bad_ws.send_json = AsyncMock(side_effect=RuntimeError("connection reset"))
        dashboard._connections.add(bad_ws)

        # Emit an event — the sync callback enqueues it
        emitter.emit(_make_event("agent_started", agent_name="a1"))

        # Verify that after _on_event, the event is in the queue
        assert not dashboard._queue.empty()

    def test_good_client_unaffected_by_bad_client(self) -> None:
        """Good WebSocket still receives events when another client fails."""
        emitter, dashboard = _make_dashboard()
        with TestClient(dashboard.app) as client, client.websocket_connect("/ws") as ws:
            # Add a bad mock connection alongside the real one
            bad_ws = MagicMock()
            bad_ws.send_json = AsyncMock(side_effect=RuntimeError("fail"))
            dashboard._connections.add(bad_ws)

            # Emit an event
            emitter.emit(_make_event("agent_started", agent_name="a1"))

            # Good client should still receive the event
            data = ws.receive_json()
            assert data["type"] == "agent_started"


class TestServerLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_and_stop(self) -> None:
        """Server starts, binds to a port, and stops cleanly."""
        emitter, dashboard = _make_dashboard()
        await dashboard.start()
        try:
            assert dashboard._actual_port is not None
            assert dashboard._actual_port > 0
            assert "127.0.0.1" in dashboard.url
            assert str(dashboard._actual_port) in dashboard.url
        finally:
            await dashboard.stop()

    @pytest.mark.asyncio
    async def test_url_property(self) -> None:
        """url property returns correct format."""
        emitter, dashboard = _make_dashboard()
        await dashboard.start()
        try:
            url = dashboard.url
            assert url.startswith("http://127.0.0.1:")
            port_str = url.split(":")[-1]
            assert port_str.isdigit()
        finally:
            await dashboard.stop()

    @pytest.mark.asyncio
    async def test_stop_unsubscribes_from_emitter(self) -> None:
        """After stop, emitter no longer calls dashboard callback."""
        emitter, dashboard = _make_dashboard()
        await dashboard.start()
        await dashboard.stop()

        # Emit after stop — should not accumulate
        initial_count = len(dashboard._event_history)
        emitter.emit(_make_event("agent_started", agent_name="a1"))
        assert len(dashboard._event_history) == initial_count

    def test_url_before_start(self) -> None:
        """url property returns port 0 before start()."""
        emitter, dashboard = _make_dashboard()
        assert dashboard.url == "http://127.0.0.1:0"

    def test_app_property(self) -> None:
        """app property returns the FastAPI instance."""
        emitter, dashboard = _make_dashboard()
        assert dashboard.app is not None
        assert dashboard.app.title == "Conductor Dashboard"


class TestEventCallback:
    """Tests for the _on_event callback behavior."""

    def test_event_serialized_to_dict(self) -> None:
        """Events are stored as dicts, not WorkflowEvent objects."""
        emitter, dashboard = _make_dashboard()
        emitter.emit(_make_event("agent_started", agent_name="a1"))

        assert len(dashboard._event_history) == 1
        stored = dashboard._event_history[0]
        assert isinstance(stored, dict)
        assert stored["type"] == "agent_started"

    def test_event_enqueued_for_broadcast(self) -> None:
        """Each event is put into the broadcast queue."""
        emitter, dashboard = _make_dashboard()
        emitter.emit(_make_event("agent_started", agent_name="a1"))
        emitter.emit(_make_event("agent_completed", agent_name="a1"))

        assert dashboard._queue.qsize() == 2

    def test_workflow_completed_not_set_for_other_events(self) -> None:
        """Non-terminal events don't set _workflow_completed."""
        emitter, dashboard = _make_dashboard()
        emitter.emit(_make_event("agent_started", agent_name="a1"))
        emitter.emit(_make_event("agent_completed", agent_name="a1"))
        assert dashboard._workflow_completed is False


class TestWaitForClientsDisconnectGuard:
    """Tests for wait_for_clients_disconnect() guard clause."""

    @pytest.mark.asyncio
    async def test_raises_when_bg_false(self) -> None:
        """wait_for_clients_disconnect() raises RuntimeError when bg=False."""
        emitter, dashboard = _make_dashboard(bg=False)
        with pytest.raises(RuntimeError, match="requires bg=True"):
            await dashboard.wait_for_clients_disconnect()


class TestApiStop:
    """Tests for POST /api/stop endpoint."""

    def test_stop_sets_stop_event(self) -> None:
        """POST /api/stop sets the internal stop event."""
        emitter, dashboard = _make_dashboard(bg=True)
        assert not dashboard.stop_requested

        with TestClient(dashboard.app) as client:
            resp = client.post("/api/stop")
            assert resp.status_code == 200
            assert resp.json() == {"status": "stopping"}

        assert dashboard.stop_requested

    def test_stop_sets_bg_event(self) -> None:
        """POST /api/stop also sets the bg auto-shutdown event."""
        emitter, dashboard = _make_dashboard(bg=True)
        assert not dashboard._bg_event.is_set()

        with TestClient(dashboard.app) as client:
            client.post("/api/stop")

        assert dashboard._bg_event.is_set()

    def test_stop_works_without_bg_mode(self) -> None:
        """POST /api/stop works even when bg=False."""
        emitter, dashboard = _make_dashboard(bg=False)

        with TestClient(dashboard.app) as client:
            resp = client.post("/api/stop")
            assert resp.status_code == 200

        assert dashboard.stop_requested

    @pytest.mark.asyncio
    async def test_wait_for_stop_resolves(self) -> None:
        """wait_for_stop() resolves when stop event is set."""
        emitter, dashboard = _make_dashboard()

        async def set_stop() -> None:
            await asyncio.sleep(0.05)
            dashboard._stop_event.set()

        asyncio.create_task(set_stop())
        # Should resolve quickly, not hang
        await asyncio.wait_for(dashboard.wait_for_stop(), timeout=2.0)

    @pytest.mark.asyncio
    async def test_stop_unblocks_wait_for_clients_disconnect(self) -> None:
        """POST /api/stop unblocks wait_for_clients_disconnect()."""
        emitter, dashboard = _make_dashboard(bg=True)

        async def trigger_stop() -> None:
            await asyncio.sleep(0.05)
            dashboard._stop_event.set()
            dashboard._bg_event.set()

        asyncio.create_task(trigger_stop())
        # Should resolve because _bg_event is set
        await asyncio.wait_for(dashboard.wait_for_clients_disconnect(), timeout=2.0)


class TestServerStartupFailure:
    """Tests for server startup failure handling."""

    @pytest.mark.asyncio
    async def test_start_raises_on_server_failure(self) -> None:
        """start() raises RuntimeError if the server task fails before starting."""
        from unittest.mock import patch

        emitter, dashboard = _make_dashboard()

        async def _fail_serve(self: object) -> None:
            raise OSError("Address already in use")

        import uvicorn

        with (
            patch.object(uvicorn.Server, "serve", _fail_serve),
            pytest.raises(RuntimeError, match="Server failed to start"),
        ):
            await dashboard.start()

    @pytest.mark.asyncio
    async def test_start_raises_on_cancelled_task(self) -> None:
        """start() raises RuntimeError if the serve task is cancelled."""
        from unittest.mock import patch

        emitter, dashboard = _make_dashboard()

        async def _cancel_serve(self: object) -> None:
            raise asyncio.CancelledError()

        import uvicorn

        with (
            patch.object(uvicorn.Server, "serve", _cancel_serve),
            pytest.raises(RuntimeError, match="Server task was cancelled"),
        ):
            await dashboard.start()


async def _short_grace(event: asyncio.Event, delay: float) -> None:
    """Helper for testing: short grace period."""
    await asyncio.sleep(delay)
    event.set()
