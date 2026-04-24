"""Designer web server for visual workflow editing.

Serves a React SPA and provides REST endpoints for workflow CRUD,
validation, YAML import/export, and file save operations.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from conductor.config.schema import WorkflowConfig
from conductor.designer.exporter import config_to_yaml
from conductor.designer.state import (
    config_to_json,
    json_to_config,
    load_workflow_file,
    new_workflow,
    validate_json,
)
from conductor.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


# ── Request/response models ────────────────────────────────────────


class WorkflowPayload(BaseModel):
    """Wrapper for workflow JSON sent from the frontend."""

    workflow: dict[str, Any]


class YamlPayload(BaseModel):
    """Wrapper for raw YAML text."""

    yaml: str


class SaveRequest(BaseModel):
    """Save request: workflow JSON + optional file path."""

    workflow: dict[str, Any]
    path: str | None = None


# ── Designer server ─────────────────────────────────────────────────


class DesignerServer:
    """FastAPI server for the visual workflow designer.

    Args:
        workflow_path: Optional path to an existing YAML file to open.
        host: Address to bind.
        port: Port to bind (0 = OS auto-select).
    """

    def __init__(
        self,
        *,
        workflow_path: Path | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._host = host
        self._port = port
        self._workflow_path = workflow_path.resolve() if workflow_path else None

        # In-memory state
        self._workflow_json: dict[str, Any] = {}

        # Server internals
        self._server: Any = None
        self._serve_task: asyncio.Task[None] | None = None
        self._actual_port: int | None = None

        # Load initial state
        if self._workflow_path and self._workflow_path.exists():
            try:
                self._workflow_json = load_workflow_file(self._workflow_path)
            except (ConfigurationError, Exception) as exc:
                logger.warning("Failed to load %s: %s", self._workflow_path, exc)
                self._workflow_json = new_workflow()
        else:
            self._workflow_json = new_workflow()

        # Build app
        self._app = self._create_app()

    @property
    def url(self) -> str:
        """Full URL the server is listening on (available after ``start()``)."""
        return f"http://{self._host}:{self._actual_port}"

    def _create_app(self) -> FastAPI:
        """Build the FastAPI application with all routes."""
        server = self

        app = FastAPI(
            title="Conductor Designer",
            docs_url=None,
            redoc_url=None,
        )

        # ── SPA entry point ─────────────────────────────────────────

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(
                _STATIC_DIR / "index.html",
                media_type="text/html",
            )

        # ── Workflow CRUD ───────────────────────────────────────────

        @app.get("/api/workflow")
        async def get_workflow() -> JSONResponse:
            """Return the current workflow state."""
            return JSONResponse(
                content={
                    "workflow": server._workflow_json,
                    "path": str(server._workflow_path) if server._workflow_path else None,
                }
            )

        @app.put("/api/workflow")
        async def put_workflow(payload: WorkflowPayload) -> JSONResponse:
            """Update the workflow state from the frontend."""
            server._workflow_json = payload.workflow
            return JSONResponse(content={"ok": True})

        # ── Validation ──────────────────────────────────────────────

        @app.post("/api/validate")
        async def validate(payload: WorkflowPayload) -> JSONResponse:
            """Validate workflow JSON, returning errors and warnings."""
            result = validate_json(payload.workflow)
            return JSONResponse(content=result)

        # ── Export / Import ─────────────────────────────────────────

        @app.post("/api/export")
        async def export_yaml(payload: WorkflowPayload) -> JSONResponse:
            """Convert workflow JSON to YAML text."""
            try:
                config = json_to_config(payload.workflow)
                yaml_text = config_to_yaml(config)
                return JSONResponse(content={"yaml": yaml_text})
            except ConfigurationError as exc:
                return JSONResponse(
                    status_code=400,
                    content={"error": str(exc)},
                )

        @app.post("/api/import")
        async def import_yaml(payload: YamlPayload) -> JSONResponse:
            """Parse YAML text into workflow JSON."""
            import io

            from ruamel.yaml import YAML as RuamelYAML

            try:
                yaml = RuamelYAML()
                raw = yaml.load(io.StringIO(payload.yaml))
                if not isinstance(raw, dict):
                    return JSONResponse(
                        status_code=400,
                        content={"error": "YAML must be a mapping (dict) at the top level."},
                    )
                config = json_to_config(raw)
                return JSONResponse(content={"workflow": config_to_json(config)})
            except ConfigurationError as exc:
                return JSONResponse(
                    status_code=400,
                    content={"error": str(exc)},
                )
            except Exception as exc:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Failed to parse YAML: {exc}"},
                )

        # ── Save ────────────────────────────────────────────────────

        @app.post("/api/save")
        async def save(req: SaveRequest) -> JSONResponse:
            """Export workflow as YAML and write to disk."""
            try:
                config = json_to_config(req.workflow)
                yaml_text = config_to_yaml(config)

                save_path = Path(req.path) if req.path else server._workflow_path
                if save_path is None:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": (
                                "No file path specified. Use 'path' field or open an existing file."
                            )
                        },
                    )

                save_path = save_path.resolve()
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_text(yaml_text, encoding="utf-8")

                # Update current path
                server._workflow_path = save_path
                server._workflow_json = config_to_json(config)

                return JSONResponse(content={"ok": True, "path": str(save_path)})
            except ConfigurationError as exc:
                return JSONResponse(
                    status_code=400,
                    content={"error": str(exc)},
                )

        # ── Schema ──────────────────────────────────────────────────

        @app.get("/api/schema")
        async def get_schema() -> JSONResponse:
            """Return the JSON Schema for WorkflowConfig."""
            schema = WorkflowConfig.model_json_schema()
            return JSONResponse(content=schema)

        # ── Static assets ───────────────────────────────────────────

        if (_STATIC_DIR / "assets").is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=_STATIC_DIR / "assets"),
                name="designer-assets",
            )

        return app

    # ── Server lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        """Start the uvicorn server as a background task."""
        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)

        # If port=0, bind a socket to discover the actual port
        if self._port == 0:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind((self._host, 0))
            self._actual_port = sock.getsockname()[1]
            sock.close()
            # Reconfigure with actual port
            config = uvicorn.Config(
                self._app,
                host=self._host,
                port=self._actual_port,
                log_level="warning",
            )
            self._server = uvicorn.Server(config)
        else:
            self._actual_port = self._port

        self._serve_task = asyncio.create_task(self._server.serve())
        # Give uvicorn a moment to bind
        await asyncio.sleep(0.3)

    async def stop(self) -> None:
        """Shut down the server."""
        if self._server:
            self._server.should_exit = True
        if self._serve_task:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._serve_task, timeout=5.0)

    async def run_until_cancelled(self) -> None:
        """Run the server until interrupted (Ctrl+C)."""
        await self.start()
        logger.info("Designer running at %s", self.url)
        try:
            # Block until the server task completes (i.e. shutdown signal)
            if self._serve_task:
                await self._serve_task
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()
