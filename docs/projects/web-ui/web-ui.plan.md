# Implementation Plan: Real-Time Web Dashboard for Workflow Visualization

**Status:** Draft  
**Revision:** 3  
**Revision Notes:** Addressed round-2 technical review feedback — see revision notes at end of document.  
**Source Design:** [web-ui.design.md](./web-ui.design.md)

---

## 1. Problem Statement

Conductor's workflow engine produces execution events (agent start/complete, routing decisions, parallel/for-each group lifecycle) that are currently consumed exclusively by Rich console logging functions. This creates three problems:

1. **No structural observability.** Users cannot see the workflow graph, which agents have completed, which are running, or what path execution will take next. Console output is a flat, linear stream of log lines that scrolls off screen.

2. **Agent output inspection is impractical.** Full agent outputs are stored in `WorkflowContext.agent_outputs` but are truncated or lost in console scroll. Users frequently need to inspect complete output of specific agents.

3. **Engine is tightly coupled to console output.** The 13 `_verbose_log_*` wrapper functions in `workflow.py` create a direct dependency from the engine to `cli/run.py`. Adding any new output consumer requires modifying the engine—a violation of the open/closed principle.

This plan implements the solution described in `web-ui.design.md`: a `WorkflowEventEmitter` pub/sub system, engine integration, a FastAPI+uvicorn web server with WebSocket broadcasting, a single-file Cytoscape.js frontend, and CLI wiring via `--web`, `--web-port`, and `--web-bg` flags.

---

## 2. Goals and Non-Goals

### Goals

1. **Introduce a `WorkflowEventEmitter`** pub/sub system that decouples execution events from output rendering, enabling multiple simultaneous consumers.
2. **Deliver a web dashboard** accessible via `--web` flag that shows the workflow DAG with real-time node status updates (pending → running → completed/failed) and full agent output inspection.
3. **Maintain full backward compatibility.** `conductor run workflow.yaml` without `--web` produces identical behavior. The event emitter is opt-in (default `None`).
4. **Zero build step.** The frontend is a single HTML file with CDN-loaded Cytoscape.js—no Node.js, npm, or bundler.
5. **Support late-joining browsers.** A browser opened after execution has started sees complete accumulated state via `GET /api/state`.

### Non-Goals

- Multi-user authentication or authorization (localhost-only tool).
- Persistent storage or replay from disk (in-memory only).
- Streaming agent output chunks (emit complete output on agent completion).
- Remote deployment (binds to `127.0.0.1` by default).
- Replacing console output (web supplements, not replaces, Rich console).
- `conductor resume --web` support (deferred to follow-up).

---

## 3. Requirements

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `WorkflowEventEmitter` supports `subscribe(callback)` and `emit(event)` with synchronous callbacks |
| FR-2 | 21 event types are emitted at corresponding points in the engine execution loop: the 20 types from the design doc event catalog plus `script_failed` added by this plan (see Section 4, Event Catalog Additions) |
| FR-3 | Engine accepts optional `event_emitter` parameter; when `None`, zero overhead (early return in `_emit()`) |
| FR-4 | `--web` flag on `run` command starts FastAPI+uvicorn server in-process as an asyncio task |
| FR-5 | `GET /` serves `index.html`; `GET /api/state` returns event history JSON; `WS /ws` streams live events |
| FR-6 | Frontend renders workflow DAG with Cytoscape.js, updating node colors on state transitions |
| FR-7 | Clicking a node opens detail panel showing full untruncated agent output |
| FR-8 | Late-joining browsers fetch `/api/state` on connect and replay all prior events |
| FR-9 | `--web-bg` mode auto-shuts down server after workflow completes and all WebSocket clients disconnect (30s grace) |
| FR-10 | Default `--web` mode keeps server alive after workflow completion until Ctrl+C |
| FR-11 | If `--web` dependencies are not installed, CLI prints actionable error (`pip install conductor-cli[web]`) and exits with code 1 |
| FR-12 | Dashboard server startup failure is non-fatal: warning printed, workflow continues without dashboard |

### Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR-1 | Without `--web`, zero runtime overhead (`if self._event_emitter is not None` guard) |
| NFR-2 | Event emitter uses `threading.Lock` to protect the subscriber list during `emit()`. **Note:** This protects only the emitter's own state (subscriber list iteration). It does NOT make the `asyncio.Queue` bridge in `WebDashboard` thread-safe — `asyncio.Queue.put_nowait()` is not thread-safe across OS threads. In the current architecture this is fine because everything runs on a single thread (the asyncio event loop). If real OS threads are introduced in the future, the `WebDashboard` callback must be changed to use `loop.call_soon_threadsafe(queue.put_nowait, event)` to safely bridge the thread boundary. |
| NFR-3 | WebSocket broadcast errors never propagate to engine; failed sends silently remove connection |
| NFR-4 | Server binds to `127.0.0.1` by default for security |
| NFR-5 | Port 0 (auto-select) is the default; actual port printed to stderr after bind |
| NFR-6 | All new code passes existing `make lint`, `make typecheck`, and `make test` |

---

## 4. Solution Architecture

### Overview

The solution introduces three new components and modifies two existing ones:

```
┌─────────────────┐         ┌──────────────────┐        ┌───────────────────┐
│ WorkflowEngine  │  emit   │ WorkflowEvent    │ subscribe│  Console Logger  │
│                 ├────────►│ Emitter          ├────────►│  (existing        │
│ _execute_loop() │         │                  │         │   verbose_log_*)  │
│                 │         │  (pub/sub)       │         └───────────────────┘
└─────────────────┘         │                  │
                            │                  │ subscribe┌───────────────────┐
                            │                  ├────────►│  WebDashboard     │
                            └──────────────────┘         │  (FastAPI+uvicorn)│
                                                         │  GET /            │
                                                         │  GET /api/state   │
                                                         │  WS  /ws          │
                                                         └────────┬──────────┘
                                                                  │ WebSocket
                                                         ┌───────▼──────────┐
                                                         │  Browser         │
                                                         │  (Cytoscape.js)  │
                                                         │  index.html      │
                                                         └──────────────────┘
```

### Key Components and Responsibilities

| Component | File | Responsibility |
|-----------|------|----------------|
| Event System | `src/conductor/events.py` | `WorkflowEvent` dataclass + `WorkflowEventEmitter` pub/sub with `emit()` and `subscribe()` |
| Engine Integration | `src/conductor/engine/workflow.py` | Accept optional `event_emitter`, add `_emit()` helper, emit events alongside existing `_verbose_log_*` calls |
| Web Server | `src/conductor/web/server.py` | `WebDashboard` class: FastAPI app, uvicorn async task, WebSocket broadcast, event history, auto-shutdown logic |
| Frontend | `src/conductor/web/static/index.html` | Single-file HTML/CSS/JS with Cytoscape.js graph, detail panel, status bar, WebSocket client |
| CLI Wiring | `src/conductor/cli/app.py` + `cli/run.py` | `--web`, `--web-port`, `--web-bg` flags; emitter creation; dashboard lifecycle; dependency checking |

### Data Flow

1. CLI creates `WorkflowEventEmitter` and optionally `WebDashboard`
2. CLI passes emitter to `WorkflowEngine.__init__(event_emitter=emitter)`
3. Engine calls `self._emit(event_type, data)` at each state transition
4. Emitter invokes all subscriber callbacks synchronously (holding `threading.Lock` during iteration)
5. `WebDashboard` subscriber calls `queue.put_nowait(event_dict)` to bridge sync→async. This is safe because both the emitter callback and the asyncio event loop run on the same OS thread. See NFR-2 for thread safety limitations.
6. Async broadcaster task reads from queue, sends JSON to all connected WebSocket clients
7. Frontend processes events: updates Cytoscape node styles, populates detail panel

### Event Catalog Additions

The design doc ([web-ui.design.md](./web-ui.design.md), lines 115–136) defines **20 event types**. This plan adds **1 additional event type** for completeness:

| Event Type | Payload Fields | Emission Point | Rationale |
|---|---|---|---|
| `script_failed` | `agent_name`, `elapsed`, `error_type`, `message` | `_execute_loop()`: in except block when a script step raises an exception (command not found, non-zero exit with strict mode) | Symmetric with `agent_failed`, `parallel_agent_failed`, `for_each_item_failed`. Without this, a script failure path emits `script_started` → `workflow_failed` with no intermediate event explaining what failed. |

This brings the total to **21 event types**.

**Failure event coverage for max iterations and timeouts:** When `MaxIterationsHandler` triggers (via `_check_iteration_with_prompt`) or `LimitEnforcer.check_timeout()` raises `TimeoutError`, the resulting exception is caught by the except blocks in `_execute_loop()`, which emit `workflow_failed`. The `workflow_failed` event's `error_type` field will contain `"MaxIterationsError"` or `"TimeoutError"`, and `message` will contain the descriptive error text. **Important:** The class defined in `conductor/exceptions.py` (line 397) is `class TimeoutError(ExecutionError)`. The name `ConductorTimeoutError` is merely an import alias used in `limits.py` (`from conductor.exceptions import TimeoutError as ConductorTimeoutError`). Since `type(exc).__name__` returns the actual class name (`"TimeoutError"`), not the alias, all event payloads and frontend matching logic must use `"TimeoutError"`. The frontend should parse `error_type` to display appropriate messaging (e.g., "Workflow exceeded maximum iterations" or "Workflow timed out"). No dedicated event types are needed for these cases since they are terminal failures, not recoverable state transitions.

### API Contracts

**WebSocket message (server → client):**
```json
{
  "type": "agent_completed",
  "timestamp": 1708876543.123,
  "data": {
    "agent_name": "planner",
    "elapsed": 2.34,
    "model": "gpt-4o",
    "tokens": 1523,
    "cost_usd": 0.0045,
    "output": {"plan": "Step 1: ..."},
    "output_keys": ["plan"]
  }
}
```

**`GET /api/state`:** Returns `list[dict]` — all events accumulated since server start.

**`workflow_started` event `data`:**
```json
{
  "name": "research-workflow",
  "entry_point": "planner",
  "agents": [{"name": "planner", "type": "agent", "model": "gpt-4o"}, ...],
  "parallel_groups": [{"name": "team", "agents": ["r1", "r2"]}],
  "for_each_groups": [],
  "routes": [{"from": "planner", "to": "team", "when": null}, ...]
}
```

---

## 5. Dependencies

### External Dependencies (New — `[project.optional-dependencies]` `web` extra)

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | ≥0.115.0 | ASGI web framework with WebSocket support |
| `uvicorn` | ≥0.30.0 | ASGI server for running FastAPI in-process |
| `websockets` | ≥12.0 | WebSocket protocol implementation (required by uvicorn for WebSocket protocol support; not included in bare `uvicorn`, only in `uvicorn[standard]`) |

These are added under `[project.optional-dependencies]` in `pyproject.toml` (not `[dependency-groups]`). The project currently uses `[dependency-groups]` (PEP 735) for dev dependencies, but `pip install conductor-cli[web]` requires the standard `[project.optional-dependencies]` section (PEP 621). The exact TOML syntax is:

```toml
[project.optional-dependencies]
web = [
    "fastapi>=0.115.0",
    "uvicorn>=0.30.0",
    "websockets>=12.0",
]
```

### External CDN Dependency (Frontend, runtime only)

| Library | Source | Purpose |
|---------|--------|---------|
| Cytoscape.js | `unpkg.com/cytoscape` | Graph visualization and layout |
| cytoscape-dagre | `unpkg.com/cytoscape-dagre` | Hierarchical DAG layout plugin |
| dagre | `unpkg.com/dagre` | Layout algorithm (dagre dependency) |

### Internal Dependencies

- `WorkflowEngine` (`engine/workflow.py`): Modified to accept `event_emitter` parameter
- `cli/app.py`: New CLI options on `run` command
- `cli/run.py`: `run_workflow_async()` gains dashboard lifecycle management
- `config/schema.py`: Read-only access for graph structure extraction
- `engine/workflow.py` `ExecutionPlan`/`ExecutionStep`: Used to construct `workflow_started` event data

---

## 6. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Port conflict on specified `--web-port` | Medium | Low | Default port 0 (OS auto-select). Print actual URL after bind. If specific port bind fails, print warning, continue without dashboard. |
| CDN unavailable (no internet in browser) | Low | Medium | Frontend detects CDN `onerror`, displays clear error message. Workflow unaffected. |
| Web server fails to start | Low | Medium | Non-fatal: warning printed to stderr, workflow continues. Dashboard failure must never abort workflow. |
| Event ordering with parallel agents | Medium | Low | Events emitted from asyncio event loop guarantee ordering within a task. Each event carries agent name for disambiguation. |
| Large agent outputs cause WebSocket lag | Low | Medium | Events include output size metadata. Frontend can lazy-load large outputs via `/api/state` in future. |
| uvicorn startup race condition | Low | Medium | `start()` awaits until server socket is bound before returning. URL printed only after port confirmed. |
| WebSocket disconnect during broadcast | Medium | Low | Failed sends silently remove connection from set. Exceptions never propagate to emitter or engine. |
| `[web]` dependencies not installed | High | Low | Lazy import with clear actionable error: `"pip install conductor-cli[web]"`. |
| Threading Lock does not protect Queue bridge | Low | Low | In the current single-threaded asyncio architecture, `asyncio.Queue.put_nowait()` is safe because both the emitter and event loop share the same OS thread. The `threading.Lock` on the emitter protects subscriber list iteration only. If OS threads are introduced in the future, the `WebDashboard` callback must switch to `loop.call_soon_threadsafe()`. See NFR-2. |

---

## 7. Implementation Phases

### Phase 1: Event Foundation (Epic 1)
**Exit Criteria:** `WorkflowEventEmitter` class exists with full test coverage. `emit()` and `subscribe()` work correctly. `threading.Lock` protects subscriber list.

### Phase 2: Engine Integration (Epic 2)
**Exit Criteria:** `WorkflowEngine` accepts `event_emitter` parameter (preserving existing `interrupt_event` parameter). All 21 event types (20 from design doc + `script_failed`) are emitted at correct points. Existing tests pass unchanged. New tests verify event emission.

### Phase 3: Web Server (Epic 3)
**Exit Criteria:** `WebDashboard` class serves HTML, exposes `/api/state` and `/ws` endpoints. Events broadcast to connected WebSocket clients. Late-joiner support works. Auto-shutdown (`--web-bg`) logic works.

### Phase 4: Frontend (Epic 4)
**Exit Criteria:** Single-file `index.html` renders workflow DAG. Nodes update color on state transitions. Detail panel shows full agent output on click. WebSocket reconnection with backoff. Status bar shows failure reasons from `workflow_failed.error_type` (including `MaxIterationsError` and `TimeoutError`).

### Phase 5: CLI Wiring & Dependency Group (Epic 5)
**Exit Criteria:** `--web`, `--web-port`, `--web-bg` flags work on `run` command. `[project.optional-dependencies]` `web` extra in `pyproject.toml`. Missing dependency produces actionable error. End-to-end flow works.

---

## 8. Files Affected

### New Files

| File Path | Purpose |
|-----------|---------|
| `src/conductor/events.py` | `WorkflowEvent` dataclass and `WorkflowEventEmitter` pub/sub class |
| `src/conductor/web/__init__.py` | Web package init |
| `src/conductor/web/server.py` | `WebDashboard` class with FastAPI app, uvicorn server, WebSocket broadcast |
| `src/conductor/web/static/index.html` | Single-file frontend with Cytoscape.js graph visualization |
| `tests/test_events.py` | Unit tests for `WorkflowEventEmitter` |
| `tests/test_engine/test_event_emission.py` | Tests verifying event emission from engine |
| `tests/test_web/__init__.py` | Web test package init |
| `tests/test_web/test_server.py` | Tests for `WebDashboard` server, endpoints, WebSocket |

### Modified Files

| File Path | Changes |
|-----------|---------|
| `src/conductor/engine/workflow.py` | Add `event_emitter` param to `__init__` (after existing `interrupt_event` param), add `_emit()` helper, add ~25 `self._emit()` calls alongside existing `_verbose_log_*` calls |
| `src/conductor/cli/app.py` | Add `--web`, `--web-port`, `--web-bg` options to `run` command; pass to `run_workflow_async()` |
| `src/conductor/cli/run.py` | Modify `run_workflow_async()` to create emitter, start/stop dashboard, handle lifecycle |
| `pyproject.toml` | Add `[project.optional-dependencies]` section with `web` extra containing fastapi, uvicorn, websockets |

### Deleted Files

| File Path | Reason |
|-----------|--------|
| *(none)* | No files are deleted |

---

## 9. Implementation Plan

### Epic 1: Event System Foundation

**Goal:** Create the `WorkflowEventEmitter` pub/sub system with `WorkflowEvent` dataclass, providing the foundation for all event-driven consumers.

**Prerequisites:** None

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E1-T1 | IMPL | Create `WorkflowEvent` dataclass with `type: str`, `timestamp: float`, `data: dict[str, Any]` fields | `src/conductor/events.py` | TO DO |
| E1-T2 | IMPL | Implement `WorkflowEventEmitter` class with `subscribe(callback)`, `unsubscribe(callback)`, and `emit(event)` methods. Use `threading.Lock` to protect subscriber list during iteration in `emit()`. Callbacks are `Callable[[WorkflowEvent], None]`. The Lock protects only the emitter's own subscriber list — it does NOT make downstream consumers (e.g., `asyncio.Queue.put_nowait()`) thread-safe. See NFR-2. | `src/conductor/events.py` | TO DO |
| E1-T3 | TEST | Unit tests: subscribe/emit delivery, multiple subscribers, unsubscribe, emit with no subscribers, thread safety (concurrent emit from multiple threads doesn't corrupt subscriber list), callback exception isolation (one failing callback doesn't prevent others) | `tests/test_events.py` | TO DO |

**Acceptance Criteria:**
- [ ] `WorkflowEvent` dataclass has `type`, `timestamp`, `data` fields
- [ ] `WorkflowEventEmitter.subscribe()` registers callback
- [ ] `WorkflowEventEmitter.emit()` calls all registered callbacks synchronously
- [ ] `threading.Lock` protects subscriber list during iteration
- [ ] One failing callback doesn't prevent other callbacks from executing
- [ ] All tests pass with `uv run pytest tests/test_events.py`

---

### Epic 2: Engine Integration

**Goal:** Wire the `WorkflowEventEmitter` into `WorkflowEngine` so that all 21 event types (20 from the design doc event catalog + `script_failed` added by this plan) are emitted at the correct execution points, alongside existing `_verbose_log_*` calls.

**Prerequisites:** Epic 1 (Event System Foundation)

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E2-T1 | IMPL | Add `event_emitter: WorkflowEventEmitter | None = None` parameter to `WorkflowEngine.__init__()`, placed **after** the existing `interrupt_event: asyncio.Event | None = None` parameter. The complete signature becomes: `__init__(self, config, provider=None, registry=None, skip_gates=False, workflow_path=None, interrupt_event=None, event_emitter=None)`. Store as `self._event_emitter`. **Do NOT remove or reorder the existing `interrupt_event` parameter.** | `src/conductor/engine/workflow.py` | TO DO |
| E2-T2 | IMPL | Add `_emit(self, event_type: str, data: dict[str, Any]) -> None` helper method that creates `WorkflowEvent` and calls `self._event_emitter.emit()` if emitter is not `None`. | `src/conductor/engine/workflow.py` | TO DO |
| E2-T3 | IMPL | Emit `workflow_started` event before the while-loop in `_execute_loop()`. Build data from `self.config` (agents list, parallel groups, for-each groups, routes, entry_point, workflow name). | `src/conductor/engine/workflow.py` | TO DO |
| E2-T4 | IMPL | Emit `agent_started`, `agent_completed`, and `agent_failed` events in `_execute_loop()` alongside existing `_verbose_log_agent_start` and `_verbose_log_agent_complete` calls. Include model, tokens, cost, output, output_keys in completed event. Emit `agent_failed` in the except block for agent execution failures. | `src/conductor/engine/workflow.py` | TO DO |
| E2-T5 | IMPL | Emit `route_taken` events at all 4 routing decision points in `_execute_loop()` (after for-each, parallel, script, and agent routing). | `src/conductor/engine/workflow.py` | TO DO |
| E2-T6 | IMPL | Emit `script_started`, `script_completed`, and `script_failed` events in `_execute_loop()` script handling block. Include stdout, stderr, exit_code in completed event. Emit `script_failed` (new event type, not in design doc) when a script step raises an exception, for symmetry with `agent_failed`. **Note:** The current script handling code (workflow.py lines 949–988) has NO try/except around script execution — exceptions propagate directly to the outer `except ConductorError` handler at line 1056. To emit `script_failed`, you must ADD a try/except wrapper around the `await self._execute_script(agent, agent_context)` call and subsequent processing, catch `ConductorError` (and `Exception`), emit `script_failed` with `agent_name`, `elapsed`, `error_type=type(exc).__name__`, and `message=str(exc)`, then re-raise so the outer handler still fires `workflow_failed`. This is a structural modification to the control flow, not just inserting an emit call. | `src/conductor/engine/workflow.py` | TO DO |
| E2-T7 | IMPL | Emit `gate_presented` and `gate_resolved` events in `_execute_loop()` human gate handling block. | `src/conductor/engine/workflow.py` | TO DO |
| E2-T8 | IMPL | Emit `parallel_started`, `parallel_agent_completed`, `parallel_agent_failed`, `parallel_completed` events in `_execute_parallel_group()`. | `src/conductor/engine/workflow.py` | TO DO |
| E2-T9 | IMPL | Emit `for_each_started`, `for_each_item_started`, `for_each_item_completed`, `for_each_item_failed`, `for_each_completed` events in `_execute_for_each_group()`. | `src/conductor/engine/workflow.py` | TO DO |
| E2-T10 | IMPL | Emit `workflow_completed` event at `$end` (in `_build_final_output` or just before return). Emit `workflow_failed` event in except blocks of `_execute_loop()`. Ensure `workflow_failed.error_type` is the exception class name via `type(exc).__name__` (e.g., `"MaxIterationsError"`, `"TimeoutError"`, `"ExecutionError"`) and `message` is the full error message, so the frontend can display appropriate failure context. **Important:** Use `"TimeoutError"`, NOT `"ConductorTimeoutError"` — the latter is merely an import alias in `limits.py`, but `type(exc).__name__` returns the actual class name `"TimeoutError"`. | `src/conductor/engine/workflow.py` | TO DO |
| E2-T11 | TEST | Test that passing `event_emitter=None` (default) produces zero overhead — existing tests must pass unchanged. | `tests/test_engine/test_event_emission.py` | TO DO |
| E2-T12 | TEST | Test event emission for each event type using a mock subscriber: verify event types, timestamps, and payload fields for `agent_started`, `agent_completed`, `agent_failed`, `route_taken`, `workflow_started`, `workflow_completed`, `workflow_failed`. Verify `workflow_failed.error_type` contains the exception class name. | `tests/test_engine/test_event_emission.py` | TO DO |
| E2-T13 | TEST | Test event emission for parallel group lifecycle: `parallel_started`, `parallel_agent_completed`, `parallel_agent_failed`, `parallel_completed`. | `tests/test_engine/test_event_emission.py` | TO DO |
| E2-T14 | TEST | Test event emission for for-each group lifecycle: `for_each_started`, `for_each_item_started`, `for_each_item_completed`, `for_each_completed`. | `tests/test_engine/test_event_emission.py` | TO DO |
| E2-T15 | TEST | Test `script_failed` event emission: verify that when a script step raises an exception, `script_failed` is emitted with `agent_name`, `elapsed`, `error_type`, `message` fields before `workflow_failed`. | `tests/test_engine/test_event_emission.py` | TO DO |

**Acceptance Criteria:**
- [ ] `WorkflowEngine.__init__` accepts optional `event_emitter` parameter after existing `interrupt_event` parameter
- [ ] `_emit()` helper safely handles `None` emitter (no-op)
- [ ] All 21 event types emitted at correct execution points (20 from design doc + `script_failed`)
- [ ] `workflow_failed.error_type` contains the exception class name (covers max iterations, timeout, and other failures)
- [ ] Existing `_verbose_log_*` calls are untouched — additive only
- [ ] All existing engine tests pass without modification
- [ ] New event emission tests pass with `uv run pytest tests/test_engine/test_event_emission.py`

---

### Epic 3: Web Server (`WebDashboard`)

**Goal:** Implement the FastAPI+uvicorn web server that subscribes to the event emitter, broadcasts events over WebSocket, serves the frontend, and supports late-joiner and auto-shutdown modes.

**Prerequisites:** Epic 1 (Event System Foundation)

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E3-T1 | IMPL | Create `src/conductor/web/__init__.py` package init. | `src/conductor/web/__init__.py` | TO DO |
| E3-T2 | IMPL | Implement `WebDashboard.__init__()`: create FastAPI app, register routes (`/`, `/api/state`, `/ws`), subscribe to event emitter, init state (`_event_history`, `_connections`, `_workflow_completed`, `_bg_event`, `_queue`). The `_queue` is an `asyncio.Queue` — safe for `put_nowait()` from the emitter callback because both run on the same OS thread. | `src/conductor/web/server.py` | TO DO |
| E3-T3 | IMPL | Implement `GET /` endpoint: serve `index.html` from `web/static/` directory using `FileResponse` or inline. | `src/conductor/web/server.py` | TO DO |
| E3-T4 | IMPL | Implement `GET /api/state` endpoint: return `self._event_history` as JSON array. | `src/conductor/web/server.py` | TO DO |
| E3-T5 | IMPL | Implement `WS /ws` endpoint: accept WebSocket, add to `self._connections`, loop receiving (keep-alive), remove on disconnect. Cancel grace timer on new connect. | `src/conductor/web/server.py` | TO DO |
| E3-T6 | IMPL | Implement event subscriber callback: serialize `WorkflowEvent` to dict, append to `_event_history`, call `_queue.put_nowait()`. Set `_workflow_completed` on `workflow_completed`/`workflow_failed` events. | `src/conductor/web/server.py` | TO DO |
| E3-T7 | IMPL | Implement async broadcaster task: read from `_queue`, broadcast to all connections in `self._connections`. Wrap each `send_json()` in try/except, remove failed connections. | `src/conductor/web/server.py` | TO DO |
| E3-T8 | IMPL | Implement `start()` method: create `uvicorn.Config` and `uvicorn.Server`, launch `server.serve()` as asyncio task, wait for socket bind, extract actual port. | `src/conductor/web/server.py` | TO DO |
| E3-T9 | IMPL | Implement `stop()` method: set `server.should_exit = True`, cancel grace timer, await serve task. | `src/conductor/web/server.py` | TO DO |
| E3-T10 | IMPL | Implement auto-shutdown logic for `--web-bg` mode: on WebSocket disconnect, if workflow completed and no connections remain, start 30s grace timer. If timer expires, set `_bg_event`. Implement `wait_for_clients_disconnect()` that awaits `_bg_event`. | `src/conductor/web/server.py` | TO DO |
| E3-T11 | IMPL | Add `url` property returning `http://{host}:{port}`. | `src/conductor/web/server.py` | TO DO |
| E3-T12 | TEST | Test `GET /api/state` returns empty list initially, accumulates events. | `tests/test_web/test_server.py` | TO DO |
| E3-T13 | TEST | Test WebSocket endpoint: connect, receive broadcast event, verify JSON structure. | `tests/test_web/test_server.py` | TO DO |
| E3-T14 | TEST | Test late-joiner: emit events, then connect new client, verify `/api/state` returns all prior events. | `tests/test_web/test_server.py` | TO DO |
| E3-T15 | TEST | Test auto-shutdown: emit `workflow_completed`, disconnect all clients, verify `wait_for_clients_disconnect()` resolves after grace period. | `tests/test_web/test_server.py` | TO DO |
| E3-T16 | TEST | Test broadcast error isolation: verify that a failed WebSocket send doesn't crash the broadcaster or affect other clients. | `tests/test_web/test_server.py` | TO DO |

**Acceptance Criteria:**
- [ ] `WebDashboard` starts uvicorn in-process as asyncio task
- [ ] `GET /` serves the HTML frontend
- [ ] `GET /api/state` returns accumulated event history
- [ ] `WS /ws` streams events to connected clients in real-time
- [ ] Late-joining browsers receive full event history via `/api/state`
- [ ] `--web-bg` auto-shutdown works with 30s grace period
- [ ] Failed WebSocket sends are silently handled
- [ ] All tests pass with `uv run pytest tests/test_web/`

---

### Epic 4: Frontend Dashboard

**Goal:** Create the single-file HTML frontend with Cytoscape.js that renders the workflow DAG, updates node states in real-time, and provides an agent output detail panel.

**Prerequisites:** Epic 3 (Web Server — for serving and testing)

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E4-T1 | IMPL | Create HTML skeleton with two-panel layout (graph left, detail right) and status bar. Include CSS for layout, node state colors (pending=gray, running=blue+pulse, completed=green, failed=red, waiting=amber). | `src/conductor/web/static/index.html` | TO DO |
| E4-T2 | IMPL | Add CDN script tags for Cytoscape.js, dagre, and cytoscape-dagre. Include `onerror` handler that displays fallback error message if CDN fails. | `src/conductor/web/static/index.html` | TO DO |
| E4-T3 | IMPL | Implement graph construction from `workflow_started` event: create nodes for agents, compound nodes for parallel/for-each groups, directed edges for routes. Use dagre layout. | `src/conductor/web/static/index.html` | TO DO |
| E4-T4 | IMPL | Implement event handlers for node state updates: `agent_started` → blue, `agent_completed` → green, `agent_failed` → red, `script_started` → blue, `script_completed` → green, `script_failed` → red, `gate_presented` → amber, `gate_resolved` → green. | `src/conductor/web/static/index.html` | TO DO |
| E4-T5 | IMPL | Implement `route_taken` edge highlighting with brief animation. | `src/conductor/web/static/index.html` | TO DO |
| E4-T6 | IMPL | Implement parallel/for-each group event handlers: update compound node badges, show progress (e.g., "3/5 complete"). | `src/conductor/web/static/index.html` | TO DO |
| E4-T7 | IMPL | Implement node click → detail panel: show agent name, status, elapsed time, model, tokens, cost, and full scrollable output (pre-formatted). | `src/conductor/web/static/index.html` | TO DO |
| E4-T8 | IMPL | Implement WebSocket client with reconnection: connect to `ws://{host}:{port}/ws`, parse JSON events, dispatch to handlers. On close, reconnect with exponential backoff (1s, 2s, 4s, 8s, max 30s). | `src/conductor/web/static/index.html` | TO DO |
| E4-T9 | IMPL | Implement late-joiner logic: on page load, fetch `GET /api/state`, replay all events to build current graph state, then connect WebSocket for live updates. | `src/conductor/web/static/index.html` | TO DO |
| E4-T10 | IMPL | Implement status bar: show workflow name, current iteration, agent completion count, elapsed time, and workflow status (Running/Completed/Failed). On `workflow_failed`, parse `error_type` to display contextual failure reasons (e.g., "Failed: exceeded maximum iterations", "Failed: workflow timed out"). | `src/conductor/web/static/index.html` | TO DO |

**Acceptance Criteria:**
- [ ] Single HTML file with no external build step
- [ ] Cytoscape.js loads from CDN; graceful error if CDN unavailable
- [ ] Workflow DAG renders on `workflow_started` event with dagre layout
- [ ] Node colors update in real-time: pending (gray) → running (blue) → completed (green) / failed (red)
- [ ] `script_failed` event handled (script node turns red)
- [ ] Clicking a node shows full untruncated output in detail panel
- [ ] WebSocket reconnects automatically on disconnect
- [ ] Late-joining browsers see full accumulated state
- [ ] Status bar shows workflow progress and descriptive failure reasons

---

### Epic 5: CLI Wiring & Dependency Group

**Goal:** Add `--web`, `--web-port`, `--web-bg` CLI flags to the `run` command, wire up emitter and dashboard lifecycle in `run_workflow_async()`, and add the `web` optional dependency extra to `pyproject.toml`.

**Prerequisites:** Epic 2 (Engine Integration), Epic 3 (Web Server), Epic 4 (Frontend)

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E5-T1 | IMPL | Add `[project.optional-dependencies]` section to `pyproject.toml` with `web` extra: `web = ["fastapi>=0.115.0", "uvicorn>=0.30.0", "websockets>=12.0"]`. This must be `[project.optional-dependencies]` (PEP 621), NOT `[dependency-groups]` (PEP 735). The `[dependency-groups]` section is already used for dev deps but does not support pip extras syntax (`pip install conductor-cli[web]`). | `pyproject.toml` | TO DO |
| E5-T2 | IMPL | Add `--web` (bool, default False), `--web-port` (int, default 0), `--web-bg` (bool, default False) options to the `run` command in `cli/app.py`. Pass values through to `run_workflow_async()`. | `src/conductor/cli/app.py` | TO DO |
| E5-T3 | IMPL | Update `run_workflow_async()` signature to accept `web`, `web_port`, `web_bg` parameters. | `src/conductor/cli/run.py` | TO DO |
| E5-T4 | IMPL | In `run_workflow_async()`: create `WorkflowEventEmitter`, pass to `WorkflowEngine(event_emitter=emitter)`. | `src/conductor/cli/run.py` | TO DO |
| E5-T5 | IMPL | In `run_workflow_async()`: if `--web`, lazy-import `WebDashboard` with try/except `ImportError` producing actionable error message (`"pip install conductor-cli[web]"`). Instantiate `WebDashboard(emitter, host="127.0.0.1", port=web_port, bg=web_bg)`, call `await dashboard.start()`, print URL to stderr. Wrap `start()` in try/except: on failure, print warning and continue without dashboard. | `src/conductor/cli/run.py` | TO DO |
| E5-T6 | IMPL | In `run_workflow_async()` post-execution: if `--web-bg`, call `await dashboard.wait_for_clients_disconnect()` then `await dashboard.stop()`. If default `--web` (no bg), print "Dashboard running at {url}. Press Ctrl+C to stop." and `await asyncio.Event().wait()`. Always `await dashboard.stop()` in finally block. | `src/conductor/cli/run.py` | TO DO |
| E5-T7 | IMPL | Ensure `--web` URL is printed to stderr regardless of `--silent`/`--quiet` mode (URL is essential, not "progress output"). | `src/conductor/cli/run.py` | TO DO |
| E5-T8 | TEST | Test CLI: `--web` flag is accepted, `--web-port` sets port, `--web-bg` is accepted. Test mutual compatibility with existing flags. | `tests/test_cli/test_web_flags.py` | TO DO |
| E5-T9 | TEST | Test dependency check: mock `ImportError` for `fastapi`, verify actionable error message is printed and exit code is 1. | `tests/test_cli/test_web_flags.py` | TO DO |
| E5-T10 | TEST | Test dashboard startup failure: mock `dashboard.start()` raising `OSError`, verify warning is printed and workflow continues. | `tests/test_cli/test_web_flags.py` | TO DO |

**Acceptance Criteria:**
- [ ] `pyproject.toml` has `[project.optional-dependencies]` section with `web` extra (not `[dependency-groups]`)
- [ ] `pip install conductor-cli[web]` installs fastapi, uvicorn, websockets
- [ ] `conductor run workflow.yaml --web` starts dashboard and prints URL
- [ ] `conductor run workflow.yaml --web --web-port 8080` uses specified port
- [ ] `conductor run workflow.yaml --web --web-bg` auto-shuts down after workflow + client disconnect
- [ ] Missing `fastapi`/`uvicorn` produces clear error: `"pip install conductor-cli[web]"`
- [ ] Dashboard startup failure is non-fatal (warning printed, workflow continues)
- [ ] `--web` with `--silent` still prints dashboard URL to stderr
- [ ] All existing tests pass without modification
- [ ] `make lint && make typecheck && make test` pass

---

## Revision History

### Revision 3 (current)

Addressed round-2 technical review feedback (score: 88/100):

- **Fixed `ConductorTimeoutError` → `TimeoutError` (Critical — Issue 1):** Corrected all 5 locations where `"ConductorTimeoutError"` appeared as an expected `error_type` string value. The actual class name is `TimeoutError` (defined at `exceptions.py` line 397); `ConductorTimeoutError` is merely an import alias in `limits.py`. Since `type(exc).__name__` returns the real class name, all event payloads and frontend matching must use `"TimeoutError"`. Affected: Section 4 failure event coverage paragraph, E2-T10 description, Phase 4 exit criteria. Added explicit warnings in E2-T10 and Section 4.
- **Fixed `websockets` dependency description (Minor — Issue 2):** Changed parenthetical from `(uvicorn dep)` to a more accurate description noting that `websockets` is an optional dependency of uvicorn (included in `uvicorn[standard]` but not bare `uvicorn`), justifying its explicit listing.
- **Added try/except guidance for `script_failed` emission (Minor — Issue 3):** E2-T6 now explicitly documents that the current script handling code (workflow.py lines 949–988) has NO try/except around script execution and that one must be ADDED to emit `script_failed` before re-raising. This is called out as a structural control-flow modification, not just an emit insertion.

### Revision 2

Addressed round-1 technical review feedback — PEP 621 vs PEP 735 pyproject.toml mechanism, WorkflowEngine.__init__ signature accuracy, threading.Lock/asyncio.Queue distinction, `script_failed` event addition.
