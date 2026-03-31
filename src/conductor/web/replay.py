"""Replay dashboard for visualizing recorded workflow event logs.

Loads events from a JSON or JSONL file and serves the same React frontend
with a replay-mode API that the frontend uses to render a timeline slider.

Example::

    dashboard = ReplayDashboard(Path("conductor-logs.json"))
    await dashboard.start()
    print(dashboard.url)  # http://127.0.0.1:<actual-port>
    ...
    await dashboard.stop()
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


def _load_events(log_path: Path) -> list[dict[str, Any]]:
    """Load events from a JSON array or JSONL file.

    Tries JSON array first; if that fails, falls back to JSONL
    (one JSON object per line, blank lines skipped).

    Args:
        log_path: Path to the event log file.

    Returns:
        List of event dicts.

    Raises:
        ValueError: If the file cannot be parsed as JSON or JSONL.
    """
    text = log_path.read_text(encoding="utf-8")

    # Try JSON array first
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # Try JSONL
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                events.append(obj)
            else:
                errors.append(f"line {lineno}: expected object, got {type(obj).__name__}")
        except json.JSONDecodeError as exc:
            errors.append(f"line {lineno}: {exc}")

    if events:
        if errors:
            logger.warning("Skipped %d malformed line(s) in %s", len(errors), log_path)
        return events

    raise ValueError(
        f"Cannot parse {log_path} as JSON array or JSONL. "
        f"First error: {errors[0] if errors else 'file is empty'}"
    )


class ReplayDashboard:
    """Replay dashboard for visualizing recorded workflow event logs.

    Loads events from a JSON or JSONL file and serves the same React frontend
    with a replay-mode API that the frontend uses to render a timeline slider.

    Args:
        log_path: Path to the event log file (JSON array or JSONL).
        host: Address to bind the server to.
        port: Port to bind (0 = OS auto-select).
    """

    def __init__(
        self,
        log_path: Path,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._log_path = log_path
        self._host = host
        self._port = port

        self._events = _load_events(log_path)
        logger.info("Loaded %d events from %s", len(self._events), log_path)

        # Server internals
        self._server: Any = None
        self._serve_task: asyncio.Task[None] | None = None
        self._actual_port: int | None = None

        self._app = self._create_app()

    def _create_app(self) -> FastAPI:
        """Create the FastAPI application with replay routes."""
        app = FastAPI(
            title="Conductor Replay Dashboard",
            docs_url=None,
            redoc_url=None,
        )

        events = self._events

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(
                _STATIC_DIR / "index.html",
                media_type="text/html",
            )

        @app.get("/favicon.svg")
        async def favicon() -> FileResponse:
            return FileResponse(
                _STATIC_DIR / "favicon.svg",
                media_type="image/svg+xml",
            )

        @app.get("/api/state")
        async def get_state() -> JSONResponse:
            """Return all events for the frontend to render."""
            return JSONResponse(content=events)

        @app.get("/api/replay/info")
        async def replay_info() -> JSONResponse:
            """Return replay metadata for the frontend timeline."""
            start_time: float | None = None
            end_time: float | None = None
            workflow_name: str | None = None

            if events:
                start_time = events[0].get("timestamp")
                end_time = events[-1].get("timestamp")

            for ev in events:
                if ev.get("type") == "workflow_started":
                    data = ev.get("data", {})
                    workflow_name = data.get("workflow_name") or data.get("name")
                    break

            return JSONResponse(
                content={
                    "mode": "replay",
                    "totalEvents": len(events),
                    "startTime": start_time,
                    "endTime": end_time,
                    "workflowName": workflow_name,
                }
            )

        @app.get("/api/logs")
        async def download_logs() -> JSONResponse:
            """Download the full event log as a JSON file."""
            return JSONResponse(
                content=events,
                headers={
                    "Content-Disposition": 'attachment; filename="conductor-logs.json"',
                },
            )

        # Mount static assets (Vite build output: hashed JS/CSS bundles)
        assets_dir = _STATIC_DIR / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        return app

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the uvicorn server as an asyncio task.

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

        self._serve_task = asyncio.create_task(self._server.serve())

        # Wait for server to bind
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
        """Shut down the server gracefully."""
        if self._server is not None:
            self._server.should_exit = True

        if self._serve_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._serve_task
            self._serve_task = None

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
