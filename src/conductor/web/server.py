"""Web dashboard server for real-time workflow visualization.

This module provides the ``WebDashboard`` class that runs a FastAPI+uvicorn
server in-process as an asyncio task.  It subscribes to the
``WorkflowEventEmitter``, accumulates event history for late-joiners,
broadcasts events to connected WebSocket clients, and serves the
single-file Cytoscape.js frontend.

Example::

    emitter = WorkflowEventEmitter()
    dashboard = WebDashboard(emitter, host="127.0.0.1", port=0, bg=False)
    await dashboard.start()
    print(dashboard.url)  # http://127.0.0.1:<actual-port>
    ...
    await dashboard.stop()
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from conductor.events import WorkflowEvent, WorkflowEventEmitter

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# Grace period (seconds) before auto-shutdown in --web-bg mode
_BG_GRACE_SECONDS = 30


class WebDashboard:
    """Real-time web dashboard for workflow visualization.

    Subscribes to a ``WorkflowEventEmitter``, accumulates event history,
    and broadcasts events over WebSocket to connected browsers.  Serves
    a single-file HTML frontend at ``GET /``.

    Args:
        emitter: The event emitter to subscribe to.
        host: Address to bind the server to.
        port: Port to bind (0 = OS auto-select).
        bg: If True, enable auto-shutdown after workflow completion and
            all WebSocket clients disconnect (with grace period).
    """

    def __init__(
        self,
        emitter: WorkflowEventEmitter,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        bg: bool = False,
    ) -> None:
        self._emitter = emitter
        self._host = host
        self._port = port
        self._bg = bg

        # State
        self._event_history: list[dict[str, Any]] = []
        self._connections: set[WebSocket] = set()
        self._workflow_completed = False
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # Auto-shutdown support (--web-bg)
        self._bg_event = asyncio.Event()
        self._grace_task: asyncio.Task[None] | None = None

        # Server internals
        self._server: Any = None
        self._serve_task: asyncio.Task[None] | None = None
        self._broadcast_task: asyncio.Task[None] | None = None
        self._actual_port: int | None = None

        # Build FastAPI app
        self._app = self._create_app()

        # Subscribe to emitter
        self._emitter.subscribe(self._on_event)

    def _create_app(self) -> FastAPI:
        """Create the FastAPI application with all routes.

        Uses a lifespan context manager to start/stop the broadcaster
        task, ensuring it runs both in production and under TestClient.
        """
        dashboard = self

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
            task = asyncio.create_task(dashboard._broadcaster())
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        app = FastAPI(
            title="Conductor Dashboard",
            docs_url=None,
            redoc_url=None,
            lifespan=lifespan,
        )

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(
                _STATIC_DIR / "index.html",
                media_type="text/html",
            )

        @app.get("/api/state")
        async def get_state() -> JSONResponse:
            return JSONResponse(content=self._event_history)

        @app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket) -> None:
            await ws.accept()
            self._connections.add(ws)
            # Cancel any pending grace timer on new connection
            if self._grace_task is not None:
                self._grace_task.cancel()
                self._grace_task = None
            try:
                while True:
                    # Keep-alive: wait for any client message (ping/pong)
                    await ws.receive_text()
            except WebSocketDisconnect:
                pass
            finally:
                self._connections.discard(ws)
                self._maybe_start_grace_timer()

        return app

    # ------------------------------------------------------------------
    # Event subscriber callback (sync — called from emitter)
    # ------------------------------------------------------------------

    def _on_event(self, event: WorkflowEvent) -> None:
        """Handle an event from the emitter.

        Serializes the event, appends to history, and enqueues for
        broadcast.  Safe to call from the same OS thread as the
        asyncio event loop (``put_nowait``).

        .. note::
            ``put_nowait()`` is not thread-safe across OS threads. In the
            current single-threaded asyncio architecture this is fine. If
            real OS threads are introduced, switch to
            ``loop.call_soon_threadsafe(queue.put_nowait, event_dict)``.
        """
        event_dict = event.to_dict()
        self._event_history.append(event_dict)
        self._queue.put_nowait(event_dict)

        if event.type in ("workflow_completed", "workflow_failed"):
            self._workflow_completed = True

    # ------------------------------------------------------------------
    # Async broadcaster
    # ------------------------------------------------------------------

    async def _broadcaster(self) -> None:
        """Read events from the queue and broadcast to all WebSocket clients."""
        while True:
            event_dict = await self._queue.get()
            failed: list[WebSocket] = []
            for ws in list(self._connections):
                try:
                    await ws.send_json(event_dict)
                except Exception:
                    failed.append(ws)
            for ws in failed:
                self._connections.discard(ws)
                self._maybe_start_grace_timer()

    # ------------------------------------------------------------------
    # Auto-shutdown (--web-bg)
    # ------------------------------------------------------------------

    def _maybe_start_grace_timer(self) -> None:
        """Start the grace timer if conditions are met for auto-shutdown."""
        if not self._bg:
            return
        if not self._workflow_completed:
            return
        if self._connections:
            return
        if self._grace_task is not None:
            return
        self._grace_task = asyncio.create_task(self._grace_countdown())

    async def _grace_countdown(self) -> None:
        """Wait the grace period then signal auto-shutdown."""
        try:
            await asyncio.sleep(_BG_GRACE_SECONDS)
            self._bg_event.set()
        except asyncio.CancelledError:
            pass

    async def wait_for_clients_disconnect(self) -> None:
        """Block until the auto-shutdown signal fires.

        For ``--web-bg`` mode: after workflow completes and all clients
        disconnect, a 30-second grace period starts.  This method awaits
        that signal.

        Raises:
            RuntimeError: If called when ``bg=False`` (the event would
                never be set, causing an infinite block).
        """
        if not self._bg:
            raise RuntimeError("wait_for_clients_disconnect() requires bg=True")
        await self._bg_event.wait()

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the uvicorn server as an asyncio task.

        The broadcaster is started automatically via the FastAPI lifespan.
        Waits until the server socket is bound and the actual port is
        known before returning.
        """
        import uvicorn

        config = uvicorn.Config(
            app=self._app,
            host=self._host,
            port=self._port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)

        # Launch server (broadcaster starts via app lifespan)
        self._serve_task = asyncio.create_task(self._server.serve())

        # Wait for server to bind — poll until .started is set
        while not self._server.started:
            if self._serve_task.done():
                if self._serve_task.cancelled():
                    raise RuntimeError("Server task was cancelled before starting")
                exc = self._serve_task.exception()
                raise RuntimeError(f"Server failed to start: {exc}") from exc
            await asyncio.sleep(0.05)

        # Extract actual port from bound sockets
        for server in self._server.servers:
            for socket in server.sockets:
                addr = socket.getsockname()
                self._actual_port = addr[1]
                break
            if self._actual_port is not None:
                break

        if self._actual_port is None:
            self._actual_port = self._port

    async def stop(self) -> None:
        """Shut down the server gracefully.

        The broadcaster is stopped automatically via the FastAPI lifespan
        when the server shuts down.
        """
        if self._grace_task is not None:
            self._grace_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._grace_task
            self._grace_task = None

        if self._server is not None:
            self._server.should_exit = True

        if self._serve_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._serve_task
            self._serve_task = None

        # Close remaining WebSocket connections
        for ws in list(self._connections):
            with contextlib.suppress(Exception):
                await ws.close()
        self._connections.clear()

        # Unsubscribe from emitter
        self._emitter.unsubscribe(self._on_event)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        """Return the dashboard URL (e.g., ``http://127.0.0.1:8080``)."""
        port = self._actual_port if self._actual_port is not None else self._port
        return f"http://{self._host}:{port}"

    @property
    def app(self) -> FastAPI:
        """Return the FastAPI application (useful for testing)."""
        return self._app
