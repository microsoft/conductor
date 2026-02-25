# Solution Design: Real-Time Web Dashboard for Workflow Visualization

**Status:** Draft  
**Author:** Architecture Team  
**Revision:** 2 (addressing technical review feedback)  

---

## Executive Summary

This document proposes adding a real-time web dashboard to Conductor, activated via a `--web` CLI flag, that visualizes workflow execution as an interactive graph. The system introduces three new components: a `WorkflowEventEmitter` pub/sub system decoupling the engine from output consumers, a FastAPI + uvicorn web server broadcasting events over WebSocket, and a single-file Cytoscape.js frontend rendering the workflow DAG with live status updates. The dashboard provides full, untruncated agent output inspection—something the console cannot practically offer—while maintaining full backward compatibility with existing CLI behavior.

---

## Background

### Current Architecture

Conductor's execution pipeline flows as: CLI (`cli/run.py`) → Config loader → `WorkflowEngine` → `AgentExecutor` → `AgentProvider`. The engine orchestrates sequential/parallel/for-each agent execution following routing rules, accumulating outputs in `WorkflowContext`.

All user-facing output is currently produced by 14 `verbose_log_*()` functions in `cli/run.py` (lines 129–630), called from `engine/workflow.py` via lazy-import wrapper functions (`_verbose_log`, `_verbose_log_agent_start`, etc., lines 36–183). This creates a **direct coupling** between the engine and Rich console output: the engine calls specific logging functions at known points across multiple methods—the main execution loop (`_execute_loop`, lines 642–961), as well as sub-methods `_execute_parallel_group` (lines 1333–1600) and `_execute_for_each_group` (lines 1613–1900). There is no observer pattern, event bus, or hook system for execution events.

Key existing structures relevant to this design:

- **`ExecutionStep` / `ExecutionPlan`** (workflow.py, lines 311–366): Dataclasses that describe the static workflow graph for `--dry-run` mode. `build_execution_plan()` (line 2108) traces all paths through the workflow. These provide the data model for initial graph construction.
- **`UsageTracker` / `WorkflowUsage`** (usage.py): Tracks per-agent token counts and costs. `get_execution_summary()` (line 2050) aggregates this data.
- **`WorkflowContext.agent_outputs`**: Stores full, untruncated agent outputs keyed by agent name—exactly what the dashboard needs to display.
- **`_verbose_log_*` wrappers** (workflow.py, lines 36–183): 13 lazy-import wrapper functions that bridge engine → CLI logging: `_verbose_log`, `_verbose_log_timing`, `_verbose_log_agent_start`, `_verbose_log_agent_complete`, `_verbose_log_route`, `_verbose_log_parallel_start`, `_verbose_log_parallel_agent_complete`, `_verbose_log_parallel_agent_failed`, `_verbose_log_parallel_summary`, `_verbose_log_for_each_start`, `_verbose_log_for_each_item_complete`, `_verbose_log_for_each_item_failed`, and `_verbose_log_for_each_summary`. These identify every point where the engine produces observable state changes.

### Motivation

The Rich console output, while functional, has inherent limitations: it is linear and ephemeral (scrolls off screen), truncates long outputs, and cannot show the workflow graph structure visually. Users running complex workflows with parallel groups, for-each loops, and conditional routing struggle to understand execution flow. A graphical dashboard addresses these gaps while also serving as the foundation for future capabilities (remote monitoring, collaboration, replay).

---

## Problem Statement

1. **No observability into workflow structure at runtime.** Users cannot see the workflow graph, which agents have completed, which are running, or what path execution will take next. The console output is a flat stream of log lines.

2. **Agent output inspection is impractical.** Full agent outputs (which can be lengthy) are stored in `WorkflowContext.agent_outputs` but are either truncated or lost in the console scroll. Users frequently need to inspect the complete output of a specific agent.

3. **Engine is tightly coupled to console output.** The 13 `_verbose_log_*` wrapper functions in `workflow.py` create a direct dependency from the engine to `cli/run.py`. Adding any new output consumer (web dashboard, file logger, telemetry) requires modifying the engine or adding more wrapper functions—a violation of the open/closed principle.

---

## Goals and Non-Goals

### Goals

1. **Introduce a `WorkflowEventEmitter`** pub/sub system in the engine that decouples execution events from output rendering, enabling multiple simultaneous consumers.
2. **Deliver a web dashboard** accessible via `--web` flag that shows the workflow graph with real-time node status updates (pending → running → completed/failed) and full agent output inspection.
3. **Maintain full backward compatibility.** Running `conductor run workflow.yaml` without `--web` must produce identical behavior. The event emitter is opt-in.
4. **Zero build step.** The frontend must be a single HTML file served directly, requiring no Node.js, npm, or bundler.
5. **Support late-joining browsers.** A browser opened after execution has started must see the complete state accumulated so far.

### Non-Goals

- **Multi-user authentication or authorization.** The dashboard is a local development tool bound to localhost.
- **Persistent storage or replay from disk.** Event history is held in memory for the duration of the server's lifetime only.
- **Streaming agent output chunks.** The initial implementation emits complete output on agent completion. Streaming can be added later if providers expose token-by-token callbacks.
- **Remote deployment.** The dashboard is designed for local use (`127.0.0.1` by default).
- **Replacing console output.** The web dashboard supplements, not replaces, the existing Rich console output.

---

## Proposed Design

### Architecture Overview

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
                                                         │                  │
                                                         │  GET /           │
                                                         │  GET /api/state  │
                                                         │  WS  /ws         │
                                                         └────────┬─────────┘
                                                                  │ WebSocket
                                                         ┌───────▼──────────┐
                                                         │  Browser         │
                                                         │  (Cytoscape.js)  │
                                                         │  index.html      │
                                                         └──────────────────┘
```

The architecture introduces an intermediary event bus (`WorkflowEventEmitter`) between the engine and all output consumers. The engine emits typed events; consumers subscribe to them. This is a classic observer/pub-sub pattern that decouples production from consumption.

### Key Components

#### 1. Event System — `src/conductor/events.py`

**Responsibility:** Define the event data model and provide a synchronous pub/sub mechanism for in-process event distribution.

```python
@dataclass
class WorkflowEvent:
    type: str                  # Event type identifier
    timestamp: float           # time.time() when emitted
    data: dict[str, Any]       # Event-specific payload

class WorkflowEventEmitter:
    def subscribe(self, callback: Callable[[WorkflowEvent], None]) -> None: ...
    def emit(self, event: WorkflowEvent) -> None: ...
```

**Event catalog:**

| Event Type | Payload Fields | Emission Point |
|---|---|---|
| `workflow_started` | `name`, `entry_point`, `agents[]`, `parallel_groups[]`, `for_each_groups[]`, `routes[]` | Before `_execute_loop` while-loop |
| `agent_started` | `agent_name`, `iteration`, `agent_type` | `_execute_loop`: before `executor.execute()` (~line 828) |
| `agent_completed` | `agent_name`, `elapsed`, `model`, `tokens`, `cost_usd`, `output`, `output_keys` | `_execute_loop`: after `executor.execute()` (~line 916) |
| `agent_failed` | `agent_name`, `elapsed`, `error_type`, `message` | `_execute_loop`: in except blocks |
| `route_taken` | `from_agent`, `to_agent` | `_execute_loop`: after `_evaluate_routes()` (~lines 739, 812, 886, 940) |
| `script_started` | `agent_name`, `iteration` | `_execute_loop`: before `_execute_script()` (~line 862) |
| `script_completed` | `agent_name`, `elapsed`, `stdout`, `stderr`, `exit_code` | `_execute_loop`: after `_execute_script()` (~line 873) |
| `gate_presented` | `agent_name`, `options[]`, `prompt` | `_execute_loop`: before `gate_handler.handle_gate()` (~line 834) |
| `gate_resolved` | `agent_name`, `selected_option`, `route`, `additional_input` | `_execute_loop`: after `gate_handler.handle_gate()` (~line 843) |
| `parallel_started` | `group_name`, `agents[]` | `_execute_parallel_group`: line 1355 |
| `parallel_agent_completed` | `group_name`, `agent_name`, `elapsed`, `model`, `tokens`, `cost_usd` | `_execute_parallel_group`: line 1402 |
| `parallel_agent_failed` | `group_name`, `agent_name`, `elapsed`, `error_type`, `message` | `_execute_parallel_group`: line 1417 |
| `parallel_completed` | `group_name`, `success_count`, `failure_count`, `elapsed` | `_execute_parallel_group`: lines 1470, 1505, 1557 |
| `for_each_started` | `group_name`, `item_count`, `max_concurrent`, `failure_mode` | `_execute_for_each_group`: line 1650 |
| `for_each_item_started` | `group_name`, `item_key`, `index` | `_execute_for_each_group`: inside `execute_single_item()` (~line 1681) |
| `for_each_item_completed` | `group_name`, `item_key`, `elapsed`, `tokens`, `cost_usd` | `_execute_for_each_group`: line 1710 |
| `for_each_item_failed` | `group_name`, `item_key`, `elapsed`, `error_type`, `message` | `_execute_for_each_group`: line 1722 |
| `for_each_completed` | `group_name`, `success_count`, `failure_count`, `elapsed` | `_execute_for_each_group`: line 1863 |
| `workflow_completed` | `elapsed`, `output`, `usage_summary` | At `$end` / `_build_final_output()` |
| `workflow_failed` | `error_type`, `message`, `agent_name` | In except blocks of `_execute_loop()` |

**Design rationale:** The emitter uses synchronous callbacks (not `async`) because all current consumers (console logging, in-process web server) can handle events synchronously. The web server's callback uses `queue.put_nowait()` (not `await queue.put()`) to enqueue events onto an `asyncio.Queue` since the callback is invoked synchronously from the emitter. An async broadcaster task then reads from the queue and sends to WebSocket clients. This avoids requiring `await` at every emit site in the engine.

#### 2. Engine Integration — `src/conductor/engine/workflow.py`

**Responsibility:** Accept an optional `WorkflowEventEmitter` and emit events at each state transition in the execution loop.

**Changes to `WorkflowEngine.__init__`:**

```python
def __init__(
    self,
    config: WorkflowConfig,
    provider: AgentProvider | None = None,
    registry: ProviderRegistry | None = None,
    skip_gates: bool = False,
    workflow_path: Path | None = None,
    event_emitter: WorkflowEventEmitter | None = None,  # NEW
) -> None:
    ...
    self._event_emitter = event_emitter
```

**New helper method:**

```python
def _emit(self, event_type: str, data: dict[str, Any]) -> None:
    if self._event_emitter is not None:
        self._event_emitter.emit(WorkflowEvent(
            type=event_type,
            timestamp=time.time(),
            data=data,
        ))
```

**Integration pattern:** At each point where a `_verbose_log_*` wrapper is currently called, add a corresponding `self._emit()` call. The existing `_verbose_log_*` calls remain unchanged—the event emitter is additive, not a replacement. This preserves backward compatibility unconditionally.

Because the event emitter is stored as `self._event_emitter` on the `WorkflowEngine` instance, it is accessible from all methods that emit events—including `_execute_loop`, `_execute_parallel_group`, and `_execute_for_each_group`—without needing to pass it as a parameter. The `_emit()` helper is an instance method on `WorkflowEngine`.

**Complete mapping of existing log calls to new events:**

**In `_execute_loop()` (lines 642–961):**

| Current Call (workflow.py) | Line(s) | New Event |
|---|---|---|
| `_verbose_log(... "Executing for-each group" ...)` | ~683 | *(informational, no event—`for_each_started` emitted inside sub-method)* |
| `_verbose_log_timing(... "For-each group completed" ...)` | ~702 | *(covered by `for_each_completed` in sub-method)* |
| `_verbose_log_route(target)` | ~739 | `route_taken` (after for-each routing) |
| `_verbose_log(... "Executing parallel group" ...)` | ~757 | *(informational, no event—`parallel_started` emitted inside sub-method)* |
| `_verbose_log_timing(... "Parallel group completed" ...)` | ~776 | *(covered by `parallel_completed` in sub-method)* |
| `_verbose_log_route(target)` | ~812 | `route_taken` (after parallel routing) |
| `_verbose_log_agent_start(name, iteration)` | ~828 | `agent_started` |
| *(no existing log call—human gate path)* | ~834 | `gate_presented` |
| *(no existing log call—human gate path)* | ~843 | `gate_resolved` |
| *(no existing log call—script path)* | ~862 | `script_started` |
| `_verbose_log_agent_complete(name, elapsed)` | ~873 | `script_completed` (with stdout/stderr/exit_code) |
| `_verbose_log_route(target)` | ~886 | `route_taken` (after script routing) |
| `_verbose_log_agent_complete(name, elapsed, ...)` | ~916 | `agent_completed` |
| `_verbose_log_route(target)` | ~940 | `route_taken` (after agent routing) |

**In `_execute_parallel_group()` (lines 1333–1600):**

| Current Call (workflow.py) | Line(s) | New Event |
|---|---|---|
| `_verbose_log_parallel_start(name, count)` | 1355 | `parallel_started` |
| `_verbose_log_parallel_agent_complete(name, elapsed, ...)` | 1402 | `parallel_agent_completed` |
| `_verbose_log_parallel_agent_failed(name, elapsed, ...)` | 1417 | `parallel_agent_failed` |
| `_verbose_log_parallel_summary(...)` | 1470, 1505, 1557 | `parallel_completed` |

**In `_execute_for_each_group()` (lines 1613–1900):**

| Current Call (workflow.py) | Line(s) | New Event |
|---|---|---|
| `_verbose_log(... "Empty array, skipping" ...)` | 1641 | *(informational only)* |
| `_verbose_log_for_each_start(name, count, ...)` | 1650 | `for_each_started` |
| *(no existing log call)* | ~1681 | `for_each_item_started` (new, at start of `execute_single_item`) |
| `_verbose_log_for_each_item_complete(key, elapsed, ...)` | 1710 | `for_each_item_completed` |
| `_verbose_log_for_each_item_failed(key, elapsed, ...)` | 1722 | `for_each_item_failed` |
| `_verbose_log(... "Batch N/M" ...)` | 1751 | *(informational only)* |
| `_verbose_log_for_each_summary(...)` | 1863 | `for_each_completed` |

The `workflow_started` event has no existing log-call equivalent. It is emitted once before the while-loop, constructed from the workflow config (`config.agents`, `config.parallel`, `config.for_each` and route definitions). This provides the full graph structure to the frontend.

#### 3. Web Server — `src/conductor/web/server.py`

**Responsibility:** Serve the dashboard UI, maintain WebSocket connections, and broadcast events.

```python
class WebDashboard:
    def __init__(self, event_emitter: WorkflowEventEmitter, host: str, port: int,
                 bg: bool = False) -> None: ...
    async def start(self) -> None: ...                    # Start uvicorn as background asyncio task
    async def stop(self) -> None: ...                     # Graceful shutdown
    async def wait_for_clients_disconnect(self) -> None:  # Block until auto-shutdown triggers
    @property
    def url(self) -> str: ...                             # e.g., "http://127.0.0.1:8234"
```

**Endpoints:**

| Method | Path | Description |
|---|---|---|
| GET | `/` | Serves `index.html` (the single-page dashboard) |
| GET | `/api/state` | Returns JSON array of all events accumulated so far (for late joiners) |
| WS | `/ws` | WebSocket endpoint for real-time event streaming |

**Server lifecycle:**

1. `WebDashboard.__init__`: Creates FastAPI app, registers routes, subscribes to event emitter. Initializes auto-shutdown state: `self._workflow_completed = False`, `self._bg_event = asyncio.Event()`, `self._grace_timer_task: asyncio.Task | None = None`.
2. `start()`: Creates `uvicorn.Config` pointing to the FastAPI app instance, creates `uvicorn.Server`, launches `server.serve()` as an `asyncio.Task`. Port 0 means auto-select; the actual port is read from the server's socket after bind.
3. Event flow: Emitter callback receives `WorkflowEvent` → serializes to JSON → calls `self._queue.put_nowait(event_dict)` to push to an internal `asyncio.Queue` (must be `put_nowait()`, not `await queue.put()`, since the emitter callback is synchronous) → broadcaster task reads from queue → sends to all connected `WebSocket` instances in `self.connections: set[WebSocket]`. If the event is `workflow_completed` or `workflow_failed`, sets `self._workflow_completed = True` and starts auto-shutdown evaluation.
4. **Connection tracking (`--web-bg` mode):** The WebSocket endpoint handler adds connections to `self.connections` on connect (cancelling any active grace timer) and removes them on disconnect. On disconnect, if `self._workflow_completed and len(self.connections) == 0`, starts a 30-second grace timer task. If the timer completes without interruption, sets `self._bg_event`.
5. `wait_for_clients_disconnect()`: Awaits `self._bg_event.wait()`. Called by the CLI after `engine.run()` when `--web-bg` is active.
6. `stop()`: Calls `server.should_exit = True`, cancels any grace timer task, awaits the serve task, cleans up.

**WebSocket broadcast error handling:** When broadcasting to connected clients, each `websocket.send_json()` call is wrapped in a try/except. If a send fails (e.g., client disconnected mid-broadcast), the connection is removed from `self.connections` and the exception is silently discarded. Failed sends must never propagate exceptions back to the emitter or the engine—a misbehaving browser must not crash the workflow.

**Late-joiner support:** The server maintains `self._event_history: list[dict]` accumulating all serialized events. When `GET /api/state` is called, it returns this list. The browser client fetches `/api/state` on connect to replay history, then switches to the WebSocket for live updates.

**Port selection:** When `--web-port` is 0 (default), the server binds to port 0, letting the OS choose. The actual port is extracted from `server.servers[0].sockets[0].getsockname()[1]` after startup. This avoids port conflicts.

#### 4. Dashboard Frontend — `src/conductor/web/static/index.html`

**Responsibility:** Render the workflow graph and provide agent detail inspection.

**Technology:** Single HTML file with embedded CSS/JS. Loads Cytoscape.js from CDN (`https://unpkg.com/cytoscape/dist/cytoscape.min.js`). If the CDN load fails (e.g., no internet access), the page displays a clear error message: "Failed to load Cytoscape.js from CDN. Please check your internet connection." with the workflow name and event count still visible in the status bar.

**Layout:**

```
┌────────────────────────────────────────────────────┐
│  Conductor - workflow-name                    v0.1 │
├─────────────────────────────┬──────────────────────┤
│                             │                      │
│      Graph View             │   Agent Detail Panel │
│   (Cytoscape.js DAG)        │                      │
│                             │   - Agent name       │
│   [planner] ──► [parallel]  │   - Status/timing    │
│                  / | \      │   - Full output      │
│          [a1] [a2] [a3]     │     (scrollable)     │
│                  \ | /      │   - Tokens/cost      │
│            [synthesizer]    │                       │
│                  │          │                       │
│              [$end]         │                       │
│                             │                       │
├─────────────────────────────┴──────────────────────┤
│  Status bar: iteration 3/10 | 2 agents complete    │
└────────────────────────────────────────────────────┘
```

**Graph construction:** On `workflow_started` event, the frontend builds the Cytoscape graph:
- Each agent → node (labeled with agent name)
- Each route → directed edge
- Parallel groups → compound/parent nodes containing child agent nodes
- For-each groups → compound nodes with item count badge

**Node styling by state:**
- `pending`: Gray fill
- `running`: Blue fill with pulse animation (CSS)
- `completed`: Green fill
- `failed`: Red fill

**Event-driven updates:**
- `agent_started` → set node to `running`
- `agent_completed` → set node to `completed`, store output data
- `agent_failed` → set node to `failed`
- `script_started` → set script node to `running`
- `script_completed` → set script node to `completed`, store stdout/stderr/exit_code
- `gate_presented` → set gate node to `waiting` (amber/yellow, distinct from running)
- `gate_resolved` → set gate node to `completed`, store selected option
- `route_taken` → highlight/animate edge
- `for_each_item_started` → update for-each badge (e.g., "3/10 running")
- Click node → populate detail panel with full untruncated output

**Connection logic:**
1. On page load: fetch `GET /api/state` to get event history, replay all events to build current state.
2. Open `WebSocket` to `ws://{host}:{port}/ws` for live updates.
3. On WebSocket message: parse JSON event, dispatch to appropriate handler.
4. On WebSocket close: attempt reconnection with exponential backoff.

#### 5. CLI Integration — `src/conductor/cli/app.py` + `src/conductor/cli/run.py`

**New CLI options on `run` command (app.py):**

```python
web: bool = typer.Option(False, "--web", help="Launch web dashboard for visualization.")
web_port: int = typer.Option(0, "--web-port", help="Port for web dashboard (0=auto).")
web_bg: bool = typer.Option(False, "--web-bg", help="Auto-stop server when all browsers disconnect after workflow completes.")
```

**Behavior modes:**

- **`--web`** (default): The dashboard server stays running after workflow completion. The CLI prints `"Dashboard running at {url}. Press Ctrl+C to stop."` and blocks via `await asyncio.Event().wait()` until the user presses Ctrl+C. This is the interactive mode—users can browse results, inspect agent outputs, and share the URL with teammates on the same machine for as long as they need.

- **`--web --web-bg`**: The dashboard server automatically shuts down after workflow completion once all WebSocket clients have disconnected. This is the fire-and-forget mode—users open the dashboard, watch the workflow execute, close the browser tab, and the CLI exits cleanly without requiring Ctrl+C. See "WebSocket-Based Background Auto-Shutdown" below for the shutdown mechanism.

**Wiring in `run_workflow_async` (run.py):**

1. Create `WorkflowEventEmitter` instance (always, regardless of `--web`—it's cheap).
2. If `--web`: instantiate `WebDashboard(emitter, host="127.0.0.1", port=web_port, bg=web_bg)`, call `await dashboard.start()`, print URL to stderr.
   - **Startup failure handling:** If `start()` raises (e.g., address already in use when a specific `--web-port` is given), print a warning to stderr (`"Warning: Web dashboard failed to start: {error}. Continuing without dashboard."`) and proceed with the workflow. The workflow must not abort due to a dashboard failure. If port 0 (auto-select) is used, bind failure is effectively impossible since the OS picks an available port.
3. Pass emitter to `WorkflowEngine(config, ..., event_emitter=emitter)`.
4. After `engine.run()` completes:
   - If `--web-bg`: call `await dashboard.wait_for_clients_disconnect()`, which blocks until the auto-shutdown logic triggers (see below), then call `await dashboard.stop()`.
   - Otherwise (default `--web`): print "Dashboard running at {url}. Press Ctrl+C to stop." and `await asyncio.Event().wait()` (blocks until interrupt).
5. Console verbose logging continues to work via existing `_verbose_log_*` functions—no change needed.

**WebSocket-Based Background Auto-Shutdown:**

When `bg=True`, the `WebDashboard` tracks WebSocket connection lifecycle to determine when to shut down after the workflow has finished:

1. The dashboard maintains `self._workflow_completed: bool = False` and `self._bg_event: asyncio.Event`.
2. When the `workflow_completed` (or `workflow_failed`) event is received, `self._workflow_completed` is set to `True`.
3. On each WebSocket disconnect, if `self._workflow_completed` is `True` and `len(self.connections) == 0`, start a **grace timer** (30 seconds by default, configurable via `--web-bg-timeout` if needed in the future).
4. If a new WebSocket client connects during the grace period, cancel the timer.
5. If the grace timer expires with no active connections, set `self._bg_event`, which unblocks `wait_for_clients_disconnect()`.
6. If no client has ever connected by the time the workflow completes, the grace timer starts immediately—the dashboard won't hang forever waiting for a browser that will never arrive.

The grace period prevents premature shutdown during brief disconnections (e.g., browser refresh, tab switch, network hiccup). The 30-second default is generous enough for a page reload but short enough that the CLI exits promptly after the user is done.

### Data Flow

**Normal execution with `--web`:**

```
User runs: conductor run workflow.yaml --web

1. CLI creates WorkflowEventEmitter
2. CLI creates WebDashboard(emitter), starts it → prints URL
3. CLI creates WorkflowEngine(config, emitter=emitter)
4. User opens browser to URL
5. Browser: GET /api/state → [] (empty, nothing happened yet)
6. Browser: WS /ws → connected
7. Engine: emit("workflow_started", {graph structure})
   → WebDashboard: push to queue → broadcast to WS → Browser builds graph
8. Engine: emit("agent_started", {name: "planner"})
   → Browser: node "planner" turns blue
9. Engine: emit("agent_completed", {name: "planner", output: {...}})
   → Browser: node "planner" turns green
10. Engine: emit("route_taken", {from: "planner", to: "synthesizer"})
    → Browser: edge animates
11. ... repeat for each agent ...
12. Engine: emit("workflow_completed", {output: {...}})
    → Browser: status bar shows "Completed"
13. Default --web: server stays up, prints "Press Ctrl+C to stop."
    With --web-bg: server monitors WebSocket connections.
      → User closes browser tab → connection count drops to 0
      → 30-second grace timer starts
      → If no reconnection: server shuts down, CLI exits
```

**Late-joiner flow:**

```
1. Workflow is already running, agents A and B have completed
2. User opens browser
3. Browser: GET /api/state → [workflow_started, agent_started(A),
   agent_completed(A), route_taken(A→B), agent_started(B), agent_completed(B), ...]
4. Browser replays all events: builds graph, colors nodes
5. Browser: WS /ws → connected, receives live events from here on
```

### API Contracts

**WebSocket message format (server → client):**

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

**GET /api/state response:**

```json
[
  {"type": "workflow_started", "timestamp": 1708876540.0, "data": {...}},
  {"type": "agent_started", "timestamp": 1708876541.0, "data": {...}},
  ...
]
```

**`workflow_started` event data structure:**

```json
{
  "name": "research-workflow",
  "entry_point": "planner",
  "agents": [
    {"name": "planner", "type": "agent", "model": "gpt-4o"},
    {"name": "researcher", "type": "agent", "model": "gpt-4o"},
    {"name": "synthesizer", "type": "agent", "model": "gpt-4o"}
  ],
  "parallel_groups": [
    {"name": "research-team", "agents": ["researcher-1", "researcher-2"]}
  ],
  "for_each_groups": [],
  "routes": [
    {"from": "planner", "to": "research-team", "when": null},
    {"from": "research-team", "to": "synthesizer", "when": null},
    {"from": "synthesizer", "to": "$end", "when": null}
  ]
}
```

### Design Decisions

#### D1: Synchronous emitter callbacks with async queue bridge

**Decision:** The `WorkflowEventEmitter.emit()` method calls subscriber callbacks synchronously. The `WebDashboard` subscriber calls `queue.put_nowait()` to push events to an `asyncio.Queue`, which is consumed by an async broadcaster task.

**Rationale:** The engine's `_execute_loop` is async but the emit points are interleaved with `await` calls. Making `emit()` synchronous avoids requiring `await self._emit(...)` at every call site (20+ locations across three methods), keeping the diff minimal. The `put_nowait()` call is non-blocking and safe from synchronous context since the queue is unbounded (events are small and bounded by workflow duration). The queue bridge naturally handles the sync→async boundary without blocking the event loop.

#### D2: Event emitter as opt-in engine parameter, not global singleton

**Decision:** `WorkflowEventEmitter` is passed to `WorkflowEngine.__init__` as an optional parameter, not accessed via module-level global or context variable.

**Rationale:** Follows the existing pattern of `WorkflowEngine.__init__` accepting optional capabilities (`provider`, `registry`, `skip_gates`, `workflow_path`). Avoids hidden global state and makes testing straightforward—tests can pass a mock emitter or `None`.

#### D3: Single HTML file with CDN dependencies (no build step)

**Decision:** The entire frontend is a single `index.html` file with inline CSS and JS, loading Cytoscape.js from unpkg CDN.

**Rationale:** Conductor is a CLI tool targeting developers who install it via pip. Introducing a frontend build pipeline (Node.js, webpack, etc.) would be a disproportionate complexity increase. A single file is trivially served, easy to modify, and has zero build requirements. The CDN dependency is acceptable for a local development tool.

#### D4: In-process uvicorn server (not subprocess)

**Decision:** The web server runs as an `asyncio.Task` within the same process as the engine, not as a separate subprocess.

**Rationale:** Sharing the process means the event emitter can use simple in-memory callbacks—no IPC, serialization, or socket overhead. uvicorn's `Server.serve()` is designed to run as an async task. The engine's event loop and uvicorn's event loop are the same, so there's no coordination complexity.

#### D5: Event history in memory for late-joiner support

**Decision:** The web server accumulates all emitted events in an in-memory list. The `/api/state` endpoint returns this list for late-joining browsers.

**Rationale:** For a local development tool processing a single workflow, memory is not a concern (even a complex workflow produces at most hundreds of events, each a few KB). This approach is simpler than SSE Last-Event-ID or event sourcing. The list is naturally bounded by workflow duration.

#### D6: `--web` stays alive by default, `--web-bg` uses WebSocket disconnect for lifecycle

**Decision:** `--web` keeps the server running after workflow completion until the user presses Ctrl+C. `--web-bg` automatically shuts down the server (and exits the CLI) when all WebSocket clients disconnect after workflow completion, with a 30-second grace period.

**Rationale:** The dashboard's primary value is post-execution inspection—users want to browse agent outputs, explore the graph, and understand what happened. Shutting down immediately after workflow completion (the original default behavior) would defeat this purpose. Making "stay alive" the default aligns with the user's most common need.

The `--web-bg` mode addresses the fire-and-forget use case: run a workflow, glance at the dashboard, close the tab, and have the CLI exit cleanly. Using WebSocket connection tracking (rather than a fixed timeout or requiring Ctrl+C) provides a natural lifecycle signal—the server shuts down when the user is demonstrably done with it. The 30-second grace period prevents premature shutdown during browser refreshes or momentary disconnections.

Alternatives considered for bg:
- **Fixed timeout after workflow completion:** Too arbitrary—some workflows need minutes of inspection, others seconds.
- **Subprocess/daemon model:** Would require IPC for event passing and complicate the architecture significantly for marginal UX benefit.
- **HTTP polling-based heartbeat:** More complex than WebSocket tracking and WebSocket connections are already maintained.

---

## Alternatives Considered

### A1: Server-Sent Events (SSE) vs. WebSocket

**SSE pros:** Simpler protocol, auto-reconnection built into `EventSource` API, works over HTTP/1.1.  
**SSE cons:** Unidirectional (server→client only), no binary support, some proxy limitations.  
**WebSocket pros:** Bidirectional (enables future features like pause/step), lower per-message overhead, widely supported.  
**WebSocket cons:** Slightly more complex connection management.

**Decision:** WebSocket chosen. While the initial implementation is unidirectional, bidirectional capability enables future features (pause, step-through, input injection) without protocol changes. FastAPI has first-class WebSocket support.

### A2: Refactoring verbose_log to use the event emitter vs. additive emit calls

**Option A:** Replace all `_verbose_log_*` calls with event emission, and make console logging a subscriber.  
**Option B:** Keep `_verbose_log_*` calls as-is, add `self._emit()` calls alongside them.

**Decision:** Option B (additive). Option A is architecturally cleaner but has a much larger blast radius—it changes the console output path, risks regressions in the well-tested verbose logging, and makes the PR harder to review. Option B is strictly additive: the existing code is untouched, new emit calls are added next to existing log calls. This can be refactored to Option A later.

### A3: Optional dependency group vs. always-installed

**Option A:** Add `fastapi`, `uvicorn`, `websockets` to a `[web]` optional dependency group (`pip install conductor[web]`).  
**Option B:** Add them to core dependencies.

**Decision:** Option A (optional `[web]` extra). The existing `pyproject.toml` has no optional dependency groups, so this introduces the pattern, but the benefits outweigh the friction: (1) the base install stays lean for users who never use `--web`; (2) it follows the convention of CLI tools with optional features (e.g., `httpie[socks]`, `rich[jupyter]`). When `--web` is used without the dependencies installed, the CLI must produce a clear, actionable error: `"The --web flag requires additional dependencies. Install them with: pip install conductor[web]"` and exit with code 1. The lazy import pattern ensures that `import fastapi` only happens when `--web` is actually used.

---

## Dependencies

### External Dependencies (New)

| Package | Version | Purpose | Size Impact |
|---|---|---|---|
| `fastapi` | ≥0.115.0 | ASGI web framework with WebSocket support | ~1MB |
| `uvicorn` | ≥0.30.0 | ASGI server for running FastAPI | ~1MB |
| `websockets` | ≥12.0 | WebSocket protocol implementation (uvicorn dependency) | ~0.5MB |

FastAPI, uvicorn, and websockets are mature, widely-used packages with active maintenance. FastAPI is created and maintained by Sebastián Ramírez (tiangolo). Uvicorn was originally created by Tom Christie (Encode) and is now primarily maintained by Marcelo Trylesinski (Kludex). Despite being separate projects with different maintainers, they are designed to work together and are the standard ASGI stack in the Python ecosystem.

These packages are added as an optional dependency group (`[web]` extra) in `pyproject.toml`, not as core dependencies. See Design Decision A3 for rationale.

### External CDN Dependency (Frontend)

| Library | Source | Purpose |
|---|---|---|
| Cytoscape.js | `unpkg.com/cytoscape` | Graph visualization and layout |

Loaded at runtime in the browser. No impact on the Python package. Requires internet access in the browser (acceptable for a local dev tool).

### Internal Dependencies

- **`WorkflowEngine`**: Modified to accept and use `WorkflowEventEmitter`. Change is additive (new optional parameter).
- **`cli/app.py`**: New `--web`, `--web-port`, `--web-bg` options on the `run` command.
- **`cli/run.py`**: `run_workflow_async()` gains dashboard lifecycle management.

### Sequencing

1. `events.py` must be implemented first (foundation for all other components).
2. Engine integration must come next (events must be emitted before they can be consumed).
3. Web server and frontend can proceed in parallel after step 2.
4. CLI wiring is last (connects everything).

---

## Impact Analysis

### Components Affected

| Component | Change Type | Risk |
|---|---|---|
| `engine/workflow.py` | Additive (`__init__` param + `_emit()` calls across `_execute_loop`, `_execute_parallel_group`, `_execute_for_each_group`) | Low—no existing behavior modified |
| `cli/app.py` | Additive (new CLI options) | Low—new options only |
| `cli/run.py` | Modified (`run_workflow_async` gains dashboard lifecycle, bg logic + dependency check) | Medium—changes to core async flow |
| `pyproject.toml` | Modified (new `[web]` optional dependency group) | Low |
| `events.py` | New file | None—no existing code affected |
| `web/` package | New package | None—no existing code affected |

### Backward Compatibility

**Full backward compatibility is maintained.** The event emitter is `None` by default. Without `--web`, no emitter is created, no events are emitted, no web server starts. All existing tests pass without modification. The `_verbose_log_*` wrappers in `workflow.py` are not modified.

### Performance Implications

- **Without `--web`:** Zero overhead. The `_emit()` method checks `if self._event_emitter is not None` and returns immediately.
- **With `--web`:** Negligible overhead per event (~microseconds for dict creation and queue push). The uvicorn server runs as a lightweight async task. WebSocket broadcast to a handful of local connections adds negligible latency.
- **Memory:** Event history grows linearly with workflow execution. For a typical workflow (10-50 agent executions), this is a few hundred KB at most.

---

## Security Considerations

### Local-Only Binding

The web server binds to `127.0.0.1` by default, restricting access to the local machine. This prevents network exposure of workflow data (which may include prompts, agent outputs, and API usage information).

### No Authentication

The dashboard does not implement authentication. This is acceptable for a local development tool bound to localhost. If the dashboard is ever exposed on `0.0.0.0`, authentication would need to be added—but that is a non-goal for this design.

### Agent Output Exposure

The `agent_completed` event includes the full, untruncated agent output. This is by design (the primary feature request). Users should be aware that anyone with access to the dashboard can see all agent outputs. Since the server is localhost-only, this is equivalent to the console output visibility.

---

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Port conflict on specified port | Medium | Low | Default to port 0 (OS auto-select). Print actual URL to stderr. With a specific `--web-port`, print warning and continue without dashboard if bind fails. |
| CDN unavailable (no internet) | Low | Medium | The frontend detects CDN load failure via `onerror` on the script tag and displays a clear error message with instructions. The workflow itself is unaffected. Future enhancement: bundle Cytoscape.js as a fallback within the Python package. |
| Web server fails to start | Low | Medium | The workflow continues execution without the dashboard. A warning is printed to stderr. This is explicitly not a fatal error—see CLI integration section. |
| Event ordering issues with parallel agents | Medium | Low | Events are emitted from the engine's event loop; asyncio guarantees ordering within a single task. Parallel agent events may interleave but each carries the agent name, which is sufficient for disambiguation. |
| Large agent outputs cause WebSocket performance issues | Low | Medium | Browser handles JSON parsing; very large outputs (>1MB) may cause brief UI freezes. Mitigate by adding output size to event metadata so the frontend can lazy-load large outputs via a separate HTTP endpoint if needed. |
| uvicorn startup race condition | Low | Medium | `start()` method awaits until the server socket is bound before returning. The URL is only printed after the port is confirmed. |
| WebSocket client disconnects during broadcast | Medium | Low | Failed sends silently remove the connection from `self.connections`. Exceptions never propagate to the emitter or engine. |

---

## Open Questions

1. **Graph layout algorithm.** Cytoscape.js supports multiple layout algorithms (dagre, klay, breadthfirst, etc.). Which layout best represents Conductor workflows? **Recommendation:** Use `dagre` (hierarchical top-to-bottom DAG layout) as the default, with a layout toggle in the UI. Dagre is the most natural fit for sequential workflows with branching.

2. **`--web` interaction with `--silent` mode.** If `--silent` suppresses all console output, should `--web` still print its URL to stderr? **Recommendation:** Yes—the URL is essential for using the feature and is not "progress output." Always print the URL to stderr when `--web` is active, regardless of verbosity mode.

3. **Resume command support.** Should `conductor resume` also support `--web`? **Recommendation:** Yes, but defer to a follow-up. The wiring is identical to `run`—pass the emitter to the engine. The `workflow_started` event would need to reconstruct the graph from the checkpoint's execution history.

4. **Event emitter thread safety.** If future providers use threading (e.g., for MCP server communication), should the emitter be thread-safe? **Recommendation:** Use a threading `Lock` in the emitter from the start. The cost is negligible and prevents subtle bugs if threading is introduced later.

---

## References

- [Brainstorm document](../brainstorm/web-ui.md) — Original brainstorm with implementation details
- [FastAPI WebSocket docs](https://fastapi.tiangolo.com/advanced/websockets/) — WebSocket endpoint patterns
- [Cytoscape.js](https://js.cytoscape.org/) — Graph visualization library
- [uvicorn programmatic usage](https://www.uvicorn.org/) — Running uvicorn as async task via `Server.serve()`
- [`engine/workflow.py` `_execute_loop()`](../../src/conductor/engine/workflow.py) — Core execution loop (line 642)
- [`engine/workflow.py` `build_execution_plan()`](../../src/conductor/engine/workflow.py) — Static graph analysis (line 2108)
- [`cli/run.py` `run_workflow_async()`](../../src/conductor/cli/run.py) — CLI workflow execution entry point (line 824)
