# Brainstorm: Web UI for Workflow Visualization (`--web`)

## Context

Currently all Conductor output goes to the console via Rich formatting (verbose logging in `cli/run.py`). The workflow engine (`engine/workflow.py`) calls `_verbose_log_*()` functions directly during execution — there is no event/observer system. This plan adds a `--web` flag that launches a real-time web dashboard showing the workflow as an interactive graph, with live streaming of agent output.

## Architecture Overview

```
┌──────────────┐     events      ┌──────────────┐    WebSocket    ┌──────────┐
│ WorkflowEngine├───────────────►│  WebServer    ├───────────────►│ Browser  │
│ (existing)   │                 │  (FastAPI +   │                │ (graph   │
│              │                 │   uvicorn)    │                │  UI)     │
└──────────────┘                 └──────────────┘                └──────────┘
```

**Key decisions:**
- **Frontend**: Single self-contained HTML file (no build step). Uses Cytoscape.js (CDN) for graph rendering
- **Backend**: FastAPI + uvicorn (lightweight, async-native, WebSocket support built in)
- **Transport**: WebSockets for real-time bidirectional communication
- **Event system**: New `WorkflowEventEmitter` decouples the engine from output consumers (console, web, file)
- **Lifecycle**: In-process by default (stops with workflow); `--web-persist` keeps server alive after completion

## New Dependencies

Add to `pyproject.toml`:
```
"fastapi>=0.115.0",
"uvicorn>=0.30.0",
"websockets>=12.0",
```

## Files to Create

### 1. `src/conductor/events.py` — Event System

A simple pub/sub event emitter that the engine publishes to and consumers subscribe to.

```python
@dataclass
class WorkflowEvent:
    type: str                  # e.g. "workflow_started", "agent_started", "agent_completed"
    timestamp: float
    data: dict[str, Any]

class WorkflowEventEmitter:
    def subscribe(self, callback: Callable[[WorkflowEvent], None]) -> None
    def emit(self, event: WorkflowEvent) -> None
```

**Event types:**
| Event | Data |
|---|---|
| `workflow_started` | `{name, entry_point, agents: [...], parallel_groups: [...], for_each_groups: [...], routes: [...]}` |
| `agent_started` | `{agent_name, iteration, agent_type}` |
| `agent_output_chunk` | `{agent_name, chunk: str}` (for streaming) |
| `agent_completed` | `{agent_name, elapsed, model, tokens, cost_usd, output, output_keys}` |
| `agent_failed` | `{agent_name, elapsed, error_type, message}` |
| `route_taken` | `{from_agent, to_agent}` |
| `parallel_started` | `{group_name, agents: [...]}` |
| `parallel_completed` | `{group_name, success_count, failure_count, elapsed}` |
| `for_each_started` | `{group_name, item_count, max_concurrent}` |
| `for_each_item_completed` | `{group_name, item_key, elapsed}` |
| `for_each_completed` | `{group_name, success_count, failure_count, elapsed}` |
| `workflow_completed` | `{elapsed, output, usage_summary}` |
| `workflow_failed` | `{error_type, message, agent_name}` |

### 2. `src/conductor/web/` — Web Server Package

#### `src/conductor/web/__init__.py`

#### `src/conductor/web/server.py` — FastAPI Application

```python
class WebDashboard:
    def __init__(self, event_emitter: WorkflowEventEmitter, host: str, port: int):
        self.app = FastAPI()
        self.emitter = event_emitter
        self.connections: set[WebSocket] = set()
        # Register routes and subscribe to events

    async def start(self) -> None:
        """Start uvicorn in a background asyncio task."""

    async def stop(self) -> None:
        """Shutdown the server."""

    @property
    def url(self) -> str:
        """Return the URL for the web dashboard."""
```

**Endpoints:**
- `GET /` — Serves the single-page HTML dashboard
- `GET /api/state` — Returns current workflow state (for late-joining browsers)
- `WS /ws` — WebSocket for real-time event streaming

**Behavior:**
- On event received from emitter → JSON-serialize → broadcast to all WebSocket connections
- Accumulates all events in memory so `/api/state` can replay them for late joiners

#### `src/conductor/web/static/index.html` — Dashboard UI

Single HTML file with embedded CSS and JS. CDN-loads Cytoscape.js.

**Layout:**
```
┌────────────────────────────────────────────────────┐
│  Conductor - workflow-name                    v0.1 │
├─────────────────────────────┬──────────────────────┤
│                             │                      │
│      Graph View             │   Agent Detail Panel │
│   (Cytoscape.js)            │                      │
│                             │   - Agent name       │
│   [planner] ──► [parallel]  │   - Status/timing    │
│                  / | \      │   - Full prompt       │
│          [a1] [a2] [a3]     │   - Full output      │
│                  \ | /      │     (streaming)       │
│            [synthesizer]    │   - Tokens/cost       │
│                  │          │                       │
│              [$end]         │                       │
│                             │                       │
├─────────────────────────────┴──────────────────────┤
│  Status bar: iteration 3/10 | 2 agents complete    │
└────────────────────────────────────────────────────┘
```

**Graph rendering:**
- Build graph from `workflow_started` event data (agents as nodes, routes as edges)
- Parallel groups rendered as compound/parent nodes containing child agent nodes
- For-each groups rendered similarly with a badge showing item count
- Node colors: gray=pending, blue-pulse=running, green=completed, red=failed
- Clicking a node opens the detail panel on the right
- Active edges highlighted/animated when a route is taken

**Detail panel:**
- Shows full, untruncated agent output (the user's key requirement)
- If agent is currently running, streams output chunks in real-time via WebSocket
- Displays rendered prompt, model, tokens, cost, timing
- Scrollable, monospace output area

**Status bar:**
- Iteration counter, elapsed time, total cost so far
- Overall workflow status (running/completed/failed)

## Files to Modify

### 3. `src/conductor/engine/workflow.py` — Emit Events

Modify `WorkflowEngine.__init__` to accept an optional `event_emitter: WorkflowEventEmitter | None = None`.

Replace the `_verbose_log_*()` calls in `_execute_loop()` and related methods with `self._emit(event_type, data)` calls. The `_emit` method:
- Calls the event emitter if present
- Still calls the existing `_verbose_log_*()` functions for console output (backward compatible)

Key insertion points in `_execute_loop()` (all within `engine/workflow.py`):
- Before entering the while loop → emit `workflow_started` with full graph structure
- Before agent execution → emit `agent_started`
- After agent execution → emit `agent_completed` with full output (untruncated)
- On route evaluation → emit `route_taken`
- Before parallel group → emit `parallel_started`
- After each parallel agent → emit `agent_completed` or `agent_failed`
- After parallel group → emit `parallel_completed`
- Same pattern for for-each groups
- On `$end` → emit `workflow_completed`
- In except blocks → emit `workflow_failed`

### 4. `src/conductor/cli/app.py` — Add `--web` Flag

Add CLI options to the `run` command:
```python
web: bool = typer.Option(False, "--web", help="Launch web dashboard for visualization.")
web_port: int = typer.Option(0, "--web-port", help="Port for web dashboard (0=auto).")
web_persist: bool = typer.Option(False, "--web-persist", help="Keep web server running after workflow completes.")
```

### 5. `src/conductor/cli/run.py` — Wire Up Web Server

In `run_workflow_async()`:
1. Create `WorkflowEventEmitter`
2. If `--web`: create `WebDashboard`, start it, print URL to stderr
3. Pass emitter to `WorkflowEngine`
4. After workflow completes:
   - If `--web-persist`: print "Dashboard still running at ... Press Ctrl+C to stop" and `await` indefinitely
   - If not: stop the web server
5. Subscribe console verbose logging as another event consumer (so the existing console output still works)

### 6. `src/conductor/executor/agent.py` — Emit Output Chunks

If an event emitter is available and the provider supports streaming, emit `agent_output_chunk` events as output arrives. This requires passing the emitter through to the executor.

_Note: Initial implementation can emit the full output on completion rather than streaming chunks. Streaming can be added later if providers support it._

### 7. `pyproject.toml` — Add Dependencies

Add `fastapi`, `uvicorn`, and `websockets` to dependencies list.

## Implementation Order

1. **Event system** (`events.py`) — foundation everything else builds on
2. **Engine integration** (`workflow.py`) — emit events from the execution loop
3. **Web server** (`web/server.py`) — FastAPI app with WebSocket broadcasting
4. **Dashboard UI** (`web/static/index.html`) — graph view with Cytoscape.js
5. **CLI wiring** (`app.py`, `run.py`) — `--web`, `--web-port`, `--web-persist` flags
6. **Tests** — event emitter unit tests, web server integration tests

## Existing Code to Reuse

- `WorkflowEngine.build_execution_plan()` in `engine/workflow.py` (line 2108) — already traces all paths through the workflow graph; reuse its logic to build the initial graph structure for the `workflow_started` event
- `ExecutionStep` dataclass (line 311) — has `agent_name`, `agent_type`, `routes`, `parallel_agents` — perfect for describing graph nodes
- `verbose_log_*()` functions in `cli/run.py` — keep as-is for console output; the event emitter is an additional consumer, not a replacement
- `WorkflowContext.agent_outputs` — full untruncated output is already stored here; emit it directly in events
- `UsageTracker` / `get_execution_summary()` — reuse for the usage/cost data shown in the dashboard

## Verification

1. **Unit tests**: Test `WorkflowEventEmitter` subscribe/emit, event serialization
2. **Web server tests**: Test WebSocket connection, event broadcasting, `/api/state` replay
3. **Manual end-to-end test**:
   ```bash
   # Run with web dashboard
   conductor run examples/parallel-research.yaml --web --input topic="AI safety"

   # Verify:
   # - URL printed to stderr (e.g., http://localhost:8234)
   # - Browser shows graph with nodes for all agents
   # - Nodes update status in real-time as agents execute
   # - Clicking a node shows full output
   # - Parallel group shown as compound node
   # - Status bar shows iteration count and elapsed time

   # Test persist mode
   conductor run examples/simple-qa.yaml --web --web-persist --input question="Hello"
   # Verify: server stays running after workflow completes
   # Ctrl+C stops it
   ```
4. **Backward compatibility**: `conductor run examples/simple-qa.yaml` without `--web` should work exactly as before (no regressions in console output)
5. **Run existing tests**: `make test` should pass — event emitter is opt-in, no behavioral changes without `--web`
