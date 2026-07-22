"""Web dashboard server for real-time workflow visualization.

This module provides the ``WebDashboard`` class that runs a FastAPI+uvicorn
server in-process as an asyncio task.  It subscribes to the
``WorkflowEventEmitter``, accumulates event history for late-joiners,
broadcasts events to connected WebSocket clients, and serves the
React frontend built from ``frontend/`` into ``static/``.

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
import hmac
import json
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.executor.linkify import LINKABLE_EXTENSIONS

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# Grace period (seconds) before auto-shutdown in --web-bg mode
_BG_GRACE_SECONDS = 30

# File API: max file size (extension allowlist is LINKABLE_EXTENSIONS from linkify)
_FILE_MAX_SIZE = 1 * 1024 * 1024  # 1 MB


class WebDashboard:
    """Real-time web dashboard for workflow visualization.

    Subscribes to a ``WorkflowEventEmitter``, accumulates event history,
    and broadcasts events over WebSocket to connected browsers.  Serves
    a React frontend at ``GET /`` with hashed JS/CSS assets.

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
        workflow_root: Path | None = None,
    ) -> None:
        self._emitter = emitter
        self._host = host
        self._port = port
        self._bg = bg
        self._workflow_root = workflow_root.resolve() if workflow_root else None

        # State
        self._event_history: list[dict[str, Any]] = []
        self._connections: set[WebSocket] = set()
        self._workflow_completed = False
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # Gate response channel (web client → engine)
        self._gate_response_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # Gate waiting state — set/cleared by the engine so the HTTP API
        # can report whether a gate is currently waiting for a response.
        self._gate_waiting_agent: str | None = None

        # Dialog response channel (web client → engine)
        self._dialog_response_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # Iteration-limit response channel (web client → engine). When the
        # engine reaches ``max_iterations`` and a dashboard is connected, the
        # user resolves the gate from the modal in the dashboard and the
        # response is delivered here. See issue #198.
        self._iteration_limit_response_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # Auto-shutdown support (--web-bg)
        self._bg_event = asyncio.Event()
        self._grace_task: asyncio.Task[None] | None = None

        # Stop signal — set by POST /api/stop (fallback) or POST /api/kill
        # to cancel the running workflow via _run_with_stop_signal.
        self._stop_event = asyncio.Event()

        # Kill signal — set by POST /api/kill while an agent is paused.
        # Cleared at the start of each pause cycle in _handle_web_pause
        # so it doesn't permanently poison subsequent pause cycles.
        self._kill_event = asyncio.Event()

        # Resume signal — set by POST /api/resume after an agent is paused
        self._resume_event = asyncio.Event()

        # Disconnect signal — set when all WebSocket clients disconnect.
        # Used by _handle_web_pause to avoid blocking forever.
        self._disconnect_event = asyncio.Event()

        # Interrupt event — shared with engine for POST /api/stop to abort agent
        self._interrupt_event: asyncio.Event | None = None

        # Pending-stop latch — set by POST /api/stop when it arrives during the
        # startup window before the engine has bound the interrupt event (via
        # set_interrupt_event). Draining it there honors the Stop gracefully
        # instead of falling back to a hard cancel that loses progress (#245).
        self._pending_stop = False

        # Server internals
        self._server: Any = None
        self._serve_task: asyncio.Task[None] | None = None
        self._broadcast_task: asyncio.Task[None] | None = None
        self._actual_port: int | None = None
        self._original_exception_handler: Any = None

        # Build FastAPI app
        self._app = self._create_app()

        # Subscribe to emitter
        self._emitter.subscribe(self._on_event)

    @property
    def port(self) -> int:
        """Resolved TCP port the dashboard is listening on."""
        return self._actual_port if self._actual_port is not None else self._port

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
            # Serve index.html with `no-cache` so browsers always revalidate
            # it with the server before reusing a cached copy (this is a
            # plain FileResponse, not StaticFiles, so it doesn't honor
            # conditional requests -> every load is a fresh 200, not a cheap
            # 304). index.html references version-hashed /assets/* bundles;
            # without this, a browser can keep serving a stale index.html
            # after a `conductor update`, pinning the dashboard to the
            # previous build's bundle. The hashed asset files under /assets
            # are unaffected by this header and get no explicit Cache-Control
            # here; that's safe because a content change always produces a
            # new filename, so a browser holding onto a stale hash is
            # harmless.
            return FileResponse(
                _STATIC_DIR / "index.html",
                media_type="text/html",
                headers={"Cache-Control": "no-cache"},
            )

        @app.get("/favicon.svg")
        async def favicon() -> FileResponse:
            return FileResponse(
                _STATIC_DIR / "favicon.svg",
                media_type="image/svg+xml",
            )

        @app.get("/api/state")
        async def get_state() -> JSONResponse:
            return JSONResponse(content=self._event_history)

        @app.get("/api/info")
        async def get_info() -> JSONResponse:
            """Return run identity for dashboard linking."""
            # Extract from first workflow_started event
            info: dict[str, Any] = {}
            for event in self._event_history:
                if event.get("type") == "workflow_started":
                    data = event.get("data", {})
                    info = {
                        "run_id": data.get("run_id", ""),
                        "workflow_name": data.get("name", ""),
                        "started_at": event.get("timestamp", 0),
                        "metadata": data.get("metadata", {}),
                        "conductor_version": data.get("system", {}).get("conductor_version", ""),
                    }
                    break
            return JSONResponse(content=info)

        @app.get("/api/logs")
        async def download_logs() -> JSONResponse:
            """Download the full event history as a JSON file."""
            return JSONResponse(
                content=self._event_history,
                headers={
                    "Content-Disposition": 'attachment; filename="conductor-logs.json"',
                },
            )

        @app.post("/api/stop")
        async def stop_workflow() -> JSONResponse:
            # Abort the current agent via interrupt (not kill workflow)
            if self._interrupt_event is not None:
                self._interrupt_event.set()
                return JSONResponse({"status": "stopping"})
            # Startup race: the engine hasn't bound the interrupt event yet.
            # Queue the Stop instead of hard-cancelling — set_interrupt_event
            # honors it the moment the engine binds the event, so the run takes
            # the graceful interrupt/pause path and a checkpoint is written
            # rather than the progress-losing hard stop (issue #245).
            logger.info("POST /api/stop: interrupt_event not bound yet; queuing stop")
            self._pending_stop = True
            return JSONResponse({"status": "stopping", "queued": True})

        @app.post("/api/kill")
        async def kill_workflow() -> JSONResponse:
            """Hard-stop the workflow (no resume possible)."""
            self._stop_event.set()
            self._kill_event.set()
            self._bg_event.set()
            return JSONResponse({"status": "killing"})

        @app.post("/api/resume")
        async def resume_agent() -> JSONResponse:
            """Resume a paused agent after it was interrupted by ``POST /api/stop``."""
            self._resume_event.set()
            return JSONResponse({"status": "resuming"})

        @app.get("/api/gate-status")
        async def gate_status() -> JSONResponse:
            """Return whether a human gate is currently waiting for a response."""
            agent = self._gate_waiting_agent
            return JSONResponse({"waiting": agent is not None, "agent_name": agent})

        @app.post("/api/gate-respond")
        async def gate_respond_api(request: Request) -> JSONResponse:
            """Resolve a parked human gate via HTTP POST.

            Body: ``{"agent_name": str, "selected_value": str,
            "additional_input": str?}``

            When the ``CONDUCTOR_GATE_TOKEN`` environment variable is set,
            the request must carry a matching token in the
            ``Authorization: Bearer <token>`` header.
            """
            try:
                body = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                return JSONResponse({"error": "Invalid JSON body"}, status_code=422)
            if not isinstance(body, dict):
                return JSONResponse(
                    {"error": "Request body must be a JSON object"}, status_code=422
                )

            # Validate token if CONDUCTOR_GATE_TOKEN is set. The token is read
            # from the Authorization header (not the JSON body) and compared in
            # constant time to avoid leaking it via timing or request logs.
            if not self._gate_token_ok(request.headers.get("authorization")):
                return JSONResponse({"error": "Invalid or missing token"}, status_code=403)

            # Validate required fields
            if not body.get("agent_name"):
                return JSONResponse(
                    {"error": "Missing required field: agent_name"}, status_code=422
                )
            if not body.get("selected_value"):
                return JSONResponse(
                    {"error": "Missing required field: selected_value"}, status_code=422
                )

            # Validate the gate is actually waiting for this agent. Without this
            # check a mismatched agent_name would be accepted here (200) and then
            # silently discarded by wait_for_gate_response, parking the workflow
            # forever while the CLI reports success.
            target_error = self._validate_gate_target(body["agent_name"])
            if target_error is not None:
                return JSONResponse({"error": target_error}, status_code=409)

            # Put onto gate response queue (same path as WebSocket handler)
            self._gate_response_queue.put_nowait(
                {
                    "type": "gate_response",
                    "agent_name": body["agent_name"],
                    "selected_value": body["selected_value"],
                    "additional_input": body.get("additional_input"),
                }
            )
            return JSONResponse({"status": "accepted"})

        @app.get("/api/files/{file_path:path}")
        async def get_file(file_path: str) -> JSONResponse:
            """Serve a local file relative to the workflow root directory.

            Used by the web dashboard to render files linked in human gate
            Markdown prompts (e.g. ``[plan](./plans/design.md)``).

            Security: rejects absolute paths, path traversal, disallowed
            extensions, and files larger than 1 MB.
            """
            if self._workflow_root is None:
                return JSONResponse(
                    {"error": "No workflow root configured"},
                    status_code=404,
                )

            # Reject absolute, drive-qualified, UNC, and scheme-prefixed paths
            if (
                "://" in file_path
                or PurePosixPath(file_path).is_absolute()
                or PureWindowsPath(file_path).is_absolute()
            ):
                return JSONResponse(
                    {"error": "Absolute paths are not allowed"},
                    status_code=403,
                )

            try:
                target = (self._workflow_root / file_path).resolve(strict=True)
            except (OSError, ValueError):
                return JSONResponse({"error": "File not found"}, status_code=404)

            # Containment check — target must be inside workflow root
            try:
                target.relative_to(self._workflow_root)
            except ValueError:
                return JSONResponse(
                    {"error": "Access denied — path outside workflow directory"},
                    status_code=403,
                )

            # Extension allowlist
            if target.suffix.lower() not in LINKABLE_EXTENSIONS:
                return JSONResponse(
                    {"error": f"File type '{target.suffix}' is not supported"},
                    status_code=403,
                )

            # Size check
            file_size = target.stat().st_size
            if file_size > _FILE_MAX_SIZE:
                return JSONResponse(
                    {"error": f"File too large ({file_size:,} bytes, max {_FILE_MAX_SIZE:,})"},
                    status_code=413,
                )

            # Read as text
            try:
                content = target.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as e:
                return JSONResponse(
                    {"error": f"Cannot read file: {e}"},
                    status_code=422,
                )

            rel_path = str(target.relative_to(self._workflow_root)).replace("\\", "/")
            return JSONResponse(
                {
                    "path": rel_path,
                    "content": content,
                    "size": file_size,
                    "extension": target.suffix.lower(),
                }
            )

        @app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket) -> None:
            await ws.accept()
            self._connections.add(ws)
            self._disconnect_event.clear()
            # Cancel any pending grace timer on new connection
            if self._grace_task is not None:
                self._grace_task.cancel()
                self._grace_task = None
            try:
                while True:
                    # Read messages from client (keep-alive pings or gate responses)
                    raw = await ws.receive_text()
                    try:
                        msg = json.loads(raw)
                        if isinstance(msg, dict) and msg.get("type") == "gate_response":
                            # Apply the same auth + waiting-state checks as the
                            # HTTP endpoint so the WebSocket path cannot bypass
                            # CONDUCTOR_GATE_TOKEN or resolve a non-waiting gate.
                            if not self._gate_token_ok(ws.headers.get("authorization")):
                                logger.warning(
                                    "Rejecting WS gate_response: invalid or missing token"
                                )
                            elif (
                                target_error := self._validate_gate_target(
                                    str(msg.get("agent_name", ""))
                                )
                            ) is not None:
                                logger.warning("Rejecting WS gate_response: %s", target_error)
                            else:
                                self._gate_response_queue.put_nowait(msg)
                        elif isinstance(msg, dict) and msg.get("type") in (
                            "dialog_message",
                            "dialog_decline",
                        ):
                            self._dialog_response_queue.put_nowait(msg)
                        elif (
                            isinstance(msg, dict) and msg.get("type") == "iteration_limit_response"
                        ):
                            self._iteration_limit_response_queue.put_nowait(msg)
                    except (json.JSONDecodeError, TypeError):
                        pass  # Ignore non-JSON messages (keep-alive pings)
            except WebSocketDisconnect:
                pass
            finally:
                self._connections.discard(ws)
                if not self._connections:
                    self._disconnect_event.set()
                self._maybe_start_grace_timer()

        # Mount static assets (Vite build output: hashed JS/CSS bundles)
        assets_dir = _STATIC_DIR / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

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

        # Also arm the grace timer here (not only from the WebSocket-disconnect
        # paths) so an unwatched run — zero clients ever connected — still
        # shuts down instead of blocking forever in
        # ``wait_for_clients_disconnect()`` (issue #318). See ``_is_root_event``
        # and ``_maybe_start_grace_timer`` for the gating/no-op details.
        is_terminal_event = event.type in ("workflow_completed", "workflow_failed")
        if is_terminal_event and self._is_root_event(event_dict):
            self._workflow_completed = True
            self._maybe_start_grace_timer()

    # ------------------------------------------------------------------
    # Replay support (used by ``resume_workflow_async``)
    # ------------------------------------------------------------------

    # Root-level lifecycle events that must be dropped on replay:
    # - ``workflow_started`` is reconstructed from the *current* YAML by
    #   the CLI (via :meth:`WorkflowEngine.build_workflow_started_data`)
    #   and prepended to ``_event_history`` *before* replay; replaying
    #   the stale original here would double-increment frontend
    #   ``wfDepth`` and visualise stale topology.
    # - ``workflow_completed`` / ``workflow_failed`` from the original run
    #   would make the dashboard appear finished before the resumed agent
    #   starts.
    # - ``checkpoint_saved`` from the original run is stale — a fresh one
    #   will be written if the resumed run also fails.
    #
    # Subworkflow-level events of the same types (identified by a
    # non-empty ``data.subworkflow_path`` set by ``WorkflowEngine._emit``)
    # are preserved so frontend ``wfDepth`` and per-context state remain
    # balanced.
    _REPLAY_ROOT_SKIP_TYPES = frozenset(
        {
            "workflow_started",
            "workflow_completed",
            "workflow_failed",
            "checkpoint_saved",
            "checkpoint_save_failed",
        }
    )

    @staticmethod
    def _is_root_event(event_dict: dict[str, Any]) -> bool:
        """Return True when *event_dict* came from the root engine.

        Sub-engine events are stamped with a non-empty ``subworkflow_path``
        list by :meth:`WorkflowEngine._emit`; root events have no such
        stamp (preserving legacy event shape).
        """
        data = event_dict.get("data") or {}
        sub_path = data.get("subworkflow_path") if isinstance(data, dict) else None
        return not (isinstance(sub_path, list) and len(sub_path) > 0)

    def prepend_workflow_started(self, data: dict[str, Any]) -> None:
        """Insert a ``workflow_started`` event at the head of ``_event_history``.

        Used by ``resume_workflow_async`` so the dashboard has correct
        topology (agents, parallel groups, for-each groups, routes) before
        any replayed historical events — without it, the frontend creates
        orphan nodes from ``agent_started``/``parallel_agent_completed``
        replays that arrive before topology is set up. Must be called
        before :meth:`start` so the seeded event is observed by every
        client via ``GET /api/state``.

        Args:
            data: Event payload (matches ``WorkflowEngine.build_workflow_started_data()``).
        """
        if self._serve_task is not None:
            logger.warning(
                "prepend_workflow_started called after dashboard.start(); "
                "already-connected clients may see inconsistent history."
            )
        import time as _time

        self._event_history.insert(
            0, {"type": "workflow_started", "timestamp": _time.time(), "data": data}
        )

    def replay_events_from_jsonl(self, path: Path) -> int:
        """Seed the dashboard's history from an existing JSONL event log.

        Used by ``resume_workflow_async`` so the dashboard can display
        the full timeline of agents that completed before the checkpoint
        was written.

        Events are appended directly to ``_event_history`` — they are
        **not** enqueued on ``_queue``. Late-joining clients pick up the
        historical events via ``GET /api/state`` and the WebSocket
        replay loop, both of which iterate ``_event_history``. Callers
        should invoke this method **before** :meth:`start` so the very
        first ``/api/state`` request returns the populated history.

        Root-level lifecycle events listed in
        ``_REPLAY_ROOT_SKIP_TYPES`` are filtered out — see the comment
        on that constant for the rationale.

        Args:
            path: Path to the original JSONL log file.

        Returns:
            Number of events appended to ``_event_history``.
        """
        if self._serve_task is not None:
            logger.warning(
                "replay_events_from_jsonl called after dashboard.start(); "
                "already-connected clients will not receive the replayed events."
            )
        if not path.exists():
            logger.warning("Replay log path does not exist: %s", path)
            return 0
        if not path.is_file():
            logger.warning("Replay log path is not a regular file: %s", path)
            return 0

        try:
            from conductor.web.replay import _load_events

            events = _load_events(path)
        except (OSError, ValueError) as exc:
            logger.warning("Failed to load replay log %s: %s", path, exc)
            return 0

        count = 0
        for event_dict in events:
            if not isinstance(event_dict, dict):
                continue
            event_type = event_dict.get("type")
            if (
                isinstance(event_type, str)
                and event_type in self._REPLAY_ROOT_SKIP_TYPES
                and self._is_root_event(event_dict)
            ):
                continue
            self._event_history.append(event_dict)
            count += 1

        logger.info("Replayed %d events from %s", count, path)
        return count

    def replay_synthetic_from_context(
        self,
        context: Any,
        config: Any,
        checkpoint_timestamp: float | None = None,
    ) -> int:
        """Seed the dashboard's history from restored workflow context.

        Fallback used when no JSONL event log is available (older
        checkpoints, deleted log files). Emits minimal
        ``*_started`` / ``*_completed`` pairs per entry in
        ``context.execution_history`` so prior nodes at least appear in
        the DAG with their final outputs.

        Like :meth:`replay_events_from_jsonl`, this method appends
        directly to ``_event_history`` and should be invoked **before**
        :meth:`start`.

        Args:
            context: A ``WorkflowContext`` restored from the checkpoint.
            config: The workflow ``WorkflowConfig`` for node-type lookup.
            checkpoint_timestamp: Unix timestamp to use for synthetic
                event timestamps. Defaults to ``time.time()`` if None.

        Returns:
            Number of events appended to ``_event_history``.
        """
        if self._serve_task is not None:
            logger.warning(
                "replay_synthetic_from_context called after dashboard.start(); "
                "already-connected clients will not receive the replayed events."
            )
        import time as _time

        ts = checkpoint_timestamp if checkpoint_timestamp is not None else _time.time()

        agent_defs = {a.name: a for a in (config.agents or [])}
        parallel_groups = {g.name: g for g in (config.parallel or [])}
        for_each_groups = {g.name: g for g in (config.for_each or [])}

        execution_history = list(getattr(context, "execution_history", []) or [])
        agent_outputs = getattr(context, "agent_outputs", {}) or {}
        step_errors = getattr(context, "step_errors", {}) or {}
        last_execution_index = {name: index for index, name in enumerate(execution_history)}

        count = 0
        for index, name in enumerate(execution_history):
            output = agent_outputs.get(name, {})
            if name in parallel_groups:
                started_type, started_data, completed_type, completed_data = self._synth_parallel(
                    name, parallel_groups[name], output
                )
            elif name in for_each_groups:
                started_type, started_data, completed_type, completed_data = self._synth_for_each(
                    name, output
                )
            else:
                started_type, started_data, completed_type, completed_data = (
                    self._synth_agent_or_script(
                        name,
                        agent_defs.get(name),
                        output,
                        step_errors.get(name) if last_execution_index[name] == index else None,
                    )
                )

            self._event_history.append(
                {"type": started_type, "timestamp": ts, "data": started_data}
            )
            self._event_history.append(
                {"type": completed_type, "timestamp": ts, "data": completed_data}
            )
            count += 2

        logger.info(
            "Synthesized %d replay events from %d history entries",
            count,
            len(execution_history),
        )
        return count

    @staticmethod
    def _synth_parallel(
        name: str, pg: Any, output: Any
    ) -> tuple[str, dict[str, Any], str, dict[str, Any]]:
        """Build synthetic (started, completed) event payloads for a parallel group.

        The frontend renders ``parallel_completed`` as failed unless
        ``failure_count === 0`` (workflow-store.ts:1266), so always emit
        zeros — we can't know the original counts from the restored
        context, but assuming success is the closest match to "the engine
        kept going past this group".
        """
        agents = list(getattr(pg, "agents", []) or [])
        output_dict = output if isinstance(output, dict) else {}
        started_data: dict[str, Any] = {
            "group_name": name,
            "agents": agents,
            "synthetic": True,
        }
        completed_data: dict[str, Any] = {
            "group_name": name,
            "outputs": output_dict,
            "success_count": len(agents),
            "failure_count": 0,
            "elapsed": 0.0,
            "synthetic": True,
        }
        return "parallel_started", started_data, "parallel_completed", completed_data

    @staticmethod
    def _synth_for_each(name: str, output: Any) -> tuple[str, dict[str, Any], str, dict[str, Any]]:
        """Build synthetic (started, completed) event payloads for a for-each group.

        The engine stores for-each output as
        ``{"outputs": <list-or-dict>, "errors": {...}, "count": N}`` (see
        ``WorkflowEngine._execute_for_each_group``). Use the authoritative
        ``count`` field when present; only fall back to ``len(outputs)``
        when that field is missing. Naïve ``output.get("outputs") or ...``
        would treat an empty list as missing and use the wrapper dict's
        key count (3) as the item count.
        """
        output_dict = output if isinstance(output, dict) else {}
        item_count = 0
        if isinstance(output_dict.get("count"), int):
            item_count = output_dict["count"]
        elif isinstance(output_dict.get("outputs"), (list, dict)):
            item_count = len(output_dict["outputs"])
        started_data: dict[str, Any] = {"group_name": name, "synthetic": True}
        completed_data: dict[str, Any] = {
            "group_name": name,
            "outputs": output_dict,
            "item_count": item_count,
            "success_count": item_count,
            "failure_count": 0,
            "elapsed": 0.0,
            "synthetic": True,
        }
        return "for_each_started", started_data, "for_each_completed", completed_data

    @staticmethod
    def _synth_agent_or_script(
        name: str, agent_def: Any, output: Any, error: Any = None
    ) -> tuple[str, dict[str, Any], str, dict[str, Any]]:
        """Build synthetic (started, completed) event payloads for an agent/script/wait."""
        agent_type = getattr(agent_def, "type", None) or "agent"
        output_dict = output if isinstance(output, dict) else {}

        if agent_type == "script":
            started_data: dict[str, Any] = {
                "agent_name": name,
                "iteration": 1,
                "synthetic": True,
            }
            completed_data: dict[str, Any] = {
                "agent_name": name,
                "elapsed": 0.0,
                "stdout": output_dict.get("stdout", ""),
                "stderr": output_dict.get("stderr", ""),
                "exit_code": output_dict.get("exit_code", 0),
                "synthetic": True,
            }
            if error is not None:
                error_dict = error.to_dict() if hasattr(error, "to_dict") else error
                completed_data.update(
                    {
                        "error_type": "TypedScriptError",
                        "message": error_dict.get("message", ""),
                        "error": error_dict,
                    }
                )
                return "script_started", started_data, "script_failed", completed_data
            return "script_started", started_data, "script_completed", completed_data

        if agent_type == "wait":
            waited = output_dict.get("waited_seconds", 0.0)
            started_data = {
                "agent_name": name,
                "iteration": 1,
                "duration_seconds": waited,
                "reason": getattr(agent_def, "reason", None),
                "synthetic": True,
            }
            completed_data = {
                "agent_name": name,
                "elapsed": waited,
                "waited_seconds": waited,
                "requested_seconds": waited,
                "reason": getattr(agent_def, "reason", None),
                "interrupted": False,
                "synthetic": True,
            }
            return "wait_started", started_data, "wait_completed", completed_data

        if agent_type == "set":
            # Mirror the live runtime's set_completed payload shape so
            # synthetic replays render identically to live runs. Reuse
            # render_set_value_repr to keep the 512-char truncation marker
            # in sync with the engine emitter.
            from conductor.executor.set_step import render_set_value_repr

            declared_type = getattr(agent_def, "output_type", None) or "auto"
            started_data = {
                "agent_name": name,
                "iteration": 1,
                "synthetic": True,
            }
            completed_data = {
                "agent_name": name,
                "elapsed": 0.0,
                "output_type": declared_type,
                "output_keys": sorted(output_dict.keys()) if output_dict else [],
                "value_repr": render_set_value_repr(output),
                "synthetic": True,
            }
            return "set_started", started_data, "set_completed", completed_data

        started_data = {
            "agent_name": name,
            "iteration": 1,
            "agent_type": agent_type,
            "synthetic": True,
        }
        completed_data = {
            "agent_name": name,
            "elapsed": 0.0,
            "model": "",
            "tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "output": output,
            "output_keys": list(output_dict.keys()),
            "synthetic": True,
        }
        return "agent_started", started_data, "agent_completed", completed_data

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
    # Gate response channel (web client → engine)
    # ------------------------------------------------------------------

    def has_connections(self) -> bool:
        """Check if any WebSocket clients are connected.

        Returns:
            True if at least one web client is connected.
        """
        return len(self._connections) > 0

    def _gate_token_ok(self, auth_header: str | None) -> bool:
        """Return True if the gate token requirement is satisfied.

        When ``CONDUCTOR_GATE_TOKEN`` is unset, gate responses are always
        allowed. Otherwise the header must be ``Authorization: Bearer
        <token>`` matching the env var, compared in constant time so the
        token cannot be recovered via timing.

        Args:
            auth_header: The raw ``Authorization`` header value, or None.

        Returns:
            True if the token check passes (or no token is configured).
        """
        expected_token = os.environ.get("CONDUCTOR_GATE_TOKEN")
        if not expected_token:
            return True
        scheme, _, presented = (auth_header or "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(presented, expected_token)

    def _validate_gate_target(self, agent_name: str) -> str | None:
        """Validate that a gate response targets the currently-waiting gate.

        Args:
            agent_name: The agent name the response is addressed to.

        Returns:
            An error message string if no gate is waiting or the name does
            not match the waiting gate, otherwise None.
        """
        waiting_agent = self._gate_waiting_agent
        if waiting_agent is None:
            return "No human gate is currently waiting for a response"
        if agent_name != waiting_agent:
            return (
                f"Gate response targets agent {agent_name!r} but the "
                f"waiting gate is {waiting_agent!r}"
            )
        return None

    async def wait_for_gate_response(self, agent_name: str) -> dict[str, Any]:
        """Wait for a gate response from a web client.

        Blocks until a ``gate_response`` message is received via WebSocket
        or HTTP POST that matches the given agent name.

        Non-matching messages are discarded with a warning. Because
        conductor only presents one gate at a time, any ``gate_response``
        addressed to a different agent is stale (e.g. a duplicate click
        from a dashboard that missed the first resolution) and cannot be
        delivered — re-queueing would only cause it to be re-examined on
        every subsequent gate with no chance of ever matching.

        Args:
            agent_name: The name of the human_gate agent to wait for.

        Returns:
            The gate response payload dict with keys ``selected_value``
            and optionally ``additional_input``.
        """
        self._gate_waiting_agent = agent_name
        try:
            while True:
                msg = await self._gate_response_queue.get()
                if msg.get("agent_name") == agent_name:
                    # Drain any responses still queued on resolution. Two
                    # concurrent submits for this same gate can both pass the
                    # waiting-state check and enqueue; we consume one here and
                    # the duplicate would otherwise linger and auto-resolve the
                    # next same-named gate reached via loop-back. Clearing it now
                    # (no ``await`` before the queue is empty) prevents that.
                    while not self._gate_response_queue.empty():
                        dup = self._gate_response_queue.get_nowait()
                        logger.warning(
                            "Draining duplicate gate_response for agent %r on resolution",
                            dup.get("agent_name"),
                        )
                    return msg
                logger.warning(
                    "Discarding stale gate_response for agent %r while waiting on %r",
                    msg.get("agent_name"),
                    agent_name,
                )
        finally:
            self._gate_waiting_agent = None

    async def wait_for_dialog_message(self, agent_name: str, dialog_id: str) -> dict[str, Any]:
        """Wait for a dialog message or decline from the web client.

        Blocks until a ``dialog_message`` or ``dialog_decline`` message is
        received via WebSocket that matches both the given agent name and
        dialog id. Messages from a stale or different dialog are dropped so
        a re-entered dialog can't be confused with the previous one.

        Args:
            agent_name: The name of the agent in dialog mode.
            dialog_id: The dialog session identifier.

        Returns:
            The dialog response payload dict with keys ``type``
            (``dialog_message`` or ``dialog_decline``) and optionally
            ``content``.
        """
        while True:
            msg = await self._dialog_response_queue.get()
            if msg.get("agent_name") == agent_name and msg.get("dialog_id") == dialog_id:
                return msg
            logger.warning(
                "Discarding stale dialog message for agent %r / dialog %r "
                "while waiting on agent %r / dialog %r",
                msg.get("agent_name"),
                msg.get("dialog_id"),
                agent_name,
                dialog_id,
            )

    async def wait_for_iteration_limit_response(self, gate_id: str) -> dict[str, Any]:
        """Wait for an iteration-limit response from a web client.

        Blocks until an ``iteration_limit_response`` message is received via
        WebSocket whose ``gate_id`` matches the one passed in. Non-matching
        messages are discarded with a warning — because each
        ``iteration_limit_reached`` event carries a fresh ``gate_id``, a
        stale or duplicated click from a previous gate cannot resolve a
        later gate even when both target the same agent or parallel group.

        Args:
            gate_id: The unique id emitted with the active
                ``iteration_limit_reached`` event.

        Returns:
            The response payload dict with at minimum ``additional_iterations``
            (an int; ``0`` means stop, ``N > 0`` means continue with N more).

        See:
            Issue #198 — ``conductor resume --web-bg`` previously exited
            silently when ``max_iterations`` was reached because the bg
            child has ``stdin=DEVNULL`` and the CLI prompt fell through
            to "stop". This channel lets the dashboard resolve the gate
            without a TTY.
        """
        while True:
            msg = await self._iteration_limit_response_queue.get()
            if msg.get("gate_id") == gate_id:
                return msg
            logger.warning(
                "Discarding stale iteration_limit_response (gate_id=%r) while waiting on %r",
                msg.get("gate_id"),
                gate_id,
            )

    # ------------------------------------------------------------------
    # Auto-shutdown (--web-bg)
    # ------------------------------------------------------------------

    def _maybe_start_grace_timer(self) -> None:
        """Start the grace timer if conditions are met for auto-shutdown.

        Safe to call from any context: if there is no running event loop
        (e.g. a synchronous ``emit()`` in a unit test with no server), this
        no-ops rather than creating an orphan coroutine. See issue #318.
        """
        if not self._bg:
            return
        if not self._workflow_completed:
            return
        if self._connections:
            return
        if self._grace_task is not None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop to shut down; create_task() would raise. Log so
            # an unexpected occurrence outside tests (which would silently
            # reproduce the #318 hang) leaves a trace.
            logger.debug(
                "_maybe_start_grace_timer: no running event loop; skipping grace-timer arm"
            )
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
        that signal.  Also unblocks immediately if a kill was requested
        via the ``/api/kill`` endpoint.

        Raises:
            RuntimeError: If called when ``bg=False`` (the event would
                never be set, causing an infinite block).
        """
        if not self._bg:
            raise RuntimeError("wait_for_clients_disconnect() requires bg=True")
        await self._bg_event.wait()

    @property
    def stop_requested(self) -> bool:
        """Check whether a hard stop has been requested via ``/api/kill``."""
        return self._stop_event.is_set()

    async def wait_for_stop(self) -> None:
        """Block until a hard stop is requested via ``/api/kill``.

        Used by the run loop to race the workflow engine against a
        user-initiated kill from the web dashboard.
        """
        await self._stop_event.wait()

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def _is_proactor_shutdown_race(self, context: dict[str, Any]) -> bool:
        """Check if an exception context matches the proactor accept-loop race.

        On Windows with Python 3.14+, the proactor event loop's accept
        callback can fire after ``Server.close()`` sets ``_sockets = None``,
        causing ``AssertionError`` in ``base_events.py:_attach``.  This is
        benign during shutdown — the server is already closing and does not
        need new connections.

        Returns True only when all of:
        - The exception is ``AssertionError``
        - The uvicorn server is in shutdown state (``should_exit`` is set)
        - The traceback is present and the deepest frame originates from
          asyncio internals
        """
        exc = context.get("exception")
        if not isinstance(exc, AssertionError):
            return False
        if self._server is None or not getattr(self._server, "should_exit", False):
            return False
        # Require an asyncio traceback frame so unrelated AssertionErrors
        # raised during shutdown (e.g., from a workflow callback finishing
        # late) propagate to the default handler instead of being silently
        # swallowed. Issue #145 (I3).
        import traceback as tb_mod

        tb = exc.__traceback__
        if tb is None:
            return False
        frames = tb_mod.extract_tb(tb)
        return bool(frames) and "asyncio" in frames[-1].filename

    def _loop_exception_handler(
        self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        """Custom event-loop exception handler that suppresses the proactor race."""
        if self._is_proactor_shutdown_race(context):
            logger.debug(
                "Suppressed proactor accept-loop race during server shutdown: %s",
                context.get("message", ""),
            )
            return
        # Delegate to the original handler (or the default)
        if self._original_exception_handler is not None:
            self._original_exception_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    async def _guarded_serve(self) -> None:
        """Run ``uvicorn.Server.serve()`` with a guard for the proactor race.

        If ``serve()`` itself raises ``AssertionError`` during shutdown
        (rather than the exception surfacing through a callback), this
        wrapper applies the same asyncio-frame gate used in
        ``_loop_exception_handler`` to avoid swallowing unrelated errors.
        """
        try:
            await self._server.serve()
        except AssertionError as exc:
            ctx: dict[str, Any] = {"exception": exc}
            if self._is_proactor_shutdown_race(ctx):
                logger.debug(
                    "Suppressed proactor accept-loop AssertionError during server shutdown"
                )
            else:
                raise

    async def start(self) -> None:
        """Start the uvicorn server as an asyncio task.

        The broadcaster is started automatically via the FastAPI lifespan.
        Waits until the server socket is bound and the actual port is
        known before returning.

        On Windows with Python 3.14+, installs a custom event-loop
        exception handler to suppress the proactor accept-loop race
        (``AssertionError: self._sockets is not None``) that can fire
        when a new connection is accepted after ``Server.close()`` sets
        ``_sockets = None`` during shutdown.
        """
        import uvicorn

        config = uvicorn.Config(
            app=self._app,
            host=self._host,
            port=self._port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)

        # Install a guarded exception handler to suppress the proactor
        # accept-race AssertionError that occurs on Windows (Python 3.14+)
        # when the server is shutting down.
        loop = asyncio.get_running_loop()
        self._original_exception_handler = loop.get_exception_handler()
        loop.set_exception_handler(self._loop_exception_handler)

        # Launch server (broadcaster starts via app lifespan)
        self._serve_task = asyncio.create_task(self._guarded_serve())

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

        # Restore the original event-loop exception handler
        try:
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(self._original_exception_handler)
        except RuntimeError:
            pass  # No running loop (e.g. during interpreter shutdown)

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

    @property
    def resume_event(self) -> asyncio.Event:
        """The resume event, set when a user clicks Resume in the dashboard."""
        return self._resume_event

    @property
    def kill_event(self) -> asyncio.Event:
        """The kill event, set when a user clicks Kill in the dashboard.

        Cleared at the start of each pause cycle by ``_handle_web_pause``
        so it doesn't permanently poison subsequent pause cycles.
        """
        return self._kill_event

    @property
    def disconnect_event(self) -> asyncio.Event:
        """Event set when all WebSocket clients disconnect.

        Cleared automatically when a new client connects. Used by the
        engine to detect browser disconnection during a pause.
        """
        return self._disconnect_event

    def set_interrupt_event(self, event: asyncio.Event) -> None:
        """Set the interrupt event reference shared with the engine.

        Called during engine setup so POST /api/stop can abort the
        current agent via the same event the engine monitors.

        If a Stop request arrived during the startup window before this was
        called (``_pending_stop``), it is honored immediately by setting the
        event, so the queued Stop takes the graceful interrupt path (#245).

        Note: at root depth the interrupt only produces a visible pause from
        *inside* LLM agent execution. If the workflow's first step is a
        ``script`` / ``set`` / ``wait`` step, a queued startup Stop is consumed
        by the between-step check without pausing — best-effort, matching
        steady-state Stop semantics.
        """
        self._interrupt_event = event
        if self._pending_stop:
            self._pending_stop = False
            event.set()
