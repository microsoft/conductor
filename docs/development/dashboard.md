# Web Dashboard

Contributor rules for the real-time dashboard: the FastAPI/uvicorn server in
`src/conductor/web/server.py`, its request guard in `src/conductor/web/auth.py`, the replay app in
`src/conductor/web/replay.py`, and the React frontend in `src/conductor/web/frontend/`. Covers
request-surface hardening, token discovery, control endpoints, resume seeding and replay
filtering, and frontend invariants.

## When to read

- Adding or changing any HTTP route, WebSocket message, or middleware in `src/conductor/web/`.
- Adding a new event type the dashboard must render, or changing the store in
  `src/conductor/web/frontend/src/stores/workflow-store.ts`.
- Changing resume behavior that seeds the dashboard (`src/conductor/cli/run.py`
  `resume_workflow_async`).
- Touching graph layout, camera behavior, reconnect handling, or the built assets in
  `src/conductor/web/static/`.

## Required invariants

### Request surface (#397)

Three independent layers; none is individually load-bearing, so do not weaken one because another
exists.

- `src/conductor/web/auth.py::OriginHostGuard` MUST stay a pure-ASGI middleware registered with
  `app.add_middleware(...)` on BOTH `src/conductor/web/server.py` and `src/conductor/web/replay.py`.
  NEVER use `BaseHTTPMiddleware` or `@app.middleware("http")`: neither sees WebSocket scopes, which
  would leave `/ws` unguarded.
- Host/Origin: `Host` is required and must name the bound machine; a present `Origin` must match
  the allowlist; an ABSENT `Origin` is allowed (httpx, curl, and `conductor gate respond` send
  none, and that path must work with no setup). `CONDUCTOR_WEB_ALLOW_ORIGINS` (comma-separated full
  origins) only ADDS dev origins such as Vite's `http://localhost:5173`.
- Token: a per-run token is minted automatically (`mint_token`) so protection is the default;
  `CONDUCTOR_GATE_TOKEN` overrides it (`resolve_expected_token`). Every mutating route (`/api/stop`,
  `/api/kill`, `/api/resume`, `/api/gate-respond`, `/api/guidance`) and the `/ws` handshake require
  it, compared with `constant_time_match`.
  `src/conductor/web/server.py::WebDashboard._gate_token_ok` always requires a match; there is no
  "allow everything when unset" branch.
- The `/ws` handshake is the single auth point for the socket: a rejected connection is closed
  (code 1008) in reply to `websocket.connect`, before `accept()`, so it can never send any
  message type (`gate_response`, `dialog_message`, `dialog_decline`, `iteration_limit_response`).
  The per-message handler therefore does not re-check the token.
- `Content-Type: application/json` is required on every mutating route, including bodyless
  control POSTs (415 otherwise).
- Read-only routes (`/api/state`, `/api/info`, `/api/logs`, `/api/gate-status`, `/api/files/*`,
  and the whole replay app) need no token; origin/host still apply.
- Every HTTP response the guard passes gets anti-clickjacking/anti-sniffing headers
  (`src/conductor/web/auth.py::_with_security_headers`), so a framed dashboard cannot be
  click-jacked into Kill or gate approval.
- A new mutating or WebSocket route MUST be added to the guard's protected list in
  `src/conductor/web/server.py`.
  `tests/test_web/test_request_guard.py::TestGuardCoversEveryMutatingRoute` derives the route table
  from `app.routes` and fails otherwise.
- Query-string tokens (`?token=` on `/ws`, needed because browsers cannot set handshake headers)
  are acceptable only because both apps run uvicorn with `log_level="warning"` (no access logs).
  Do not raise that log level.

### Token discovery

- `WebDashboard.start()` writes the resolved token to `~/.conductor/runs/dashboard-<port>.token`
  once the real port is known (`write_token_file`: atomic temp file plus `os.replace`, mode
  `0600` on POSIX; Windows relies on the user-profile ACL, #425); `stop()` removes it. Both run
  and resume, foreground and `--web-bg`, go through this.
- CLI callers (`conductor gate respond`, `conductor guide`, the graceful rung of `conductor stop`)
  MUST resolve tokens only via `src/conductor/web/auth.py::resolve_cli_token`
  (`--token` > `CONDUCTOR_GATE_TOKEN` > token file). The token file uses
  `src/conductor/rundir.py::runs_dir` through the module attribute (see
  [CLI and runs](cli-and-runs.md)).
- `GET /` reads `src/conductor/web/static/index.html` and injects `window.__CONDUCTOR_TOKEN__`
  before `</head>`, served with `Cache-Control: no-cache`. The replay app injects nothing; the
  frontend's `src/conductor/web/frontend/src/lib/auth.ts` treats a missing token as normal there.
  Pinned by `tests/test_web/test_request_guard.py::TestIndexTokenInjection` and
  `tests/test_web/test_request_guard.py::TestTokenFileLifecycle`.

### Control endpoints

- `/api/stop` pauses the current agent (the user can then Resume or Kill). A stop that arrives
  before the engine binds its interrupt event is latched in `_pending_stop` and drained by
  `WebDashboard.set_interrupt_event`, so the startup window takes the graceful path.
  `/api/guidance` uses the same pattern (`_pending_guidance`, drained by `set_guidance_sink`).
- Whenever a stop or kill actually terminates the run (cancels the engine task from
  `src/conductor/cli/run.py::_execute_with_stop_signal`), it MUST route through
  `src/conductor/engine/workflow.py::WorkflowEngine.handle_dashboard_stop`, which writes a
  best-effort checkpoint and emits one `workflow_failed` with `stopped_by_user: true` plus
  `checkpoint_path` or `checkpoint_unavailable_reason` (#245). It is idempotent via
  `_dashboard_stop_handled`. Pinned by `tests/test_cli/test_web_kill_checkpoint.py`.
- Guidance semantics (validation, drain points) are in [workflow engine](workflow-engine.md).

### Resume seeding and replay filtering

- On resume the dashboard is seeded BEFORE it accepts clients. The CLI prepends a fresh
  `workflow_started` built from the CURRENT YAML
  (`WorkflowEngine.build_workflow_started_data`), then replays the original JSONL
  (`WebDashboard.replay_events_from_jsonl`) when the checkpoint's `event_log_path` exists, else
  synthesizes `*_started`/`*_completed` pairs from the restored context
  (`replay_synthetic_from_context`). The engine's own emit is suppressed
  (`suppress_workflow_started_emit`) so there is exactly one root `workflow_started`.
- Two DISJOINT skip sets on `WebDashboard` (disjointness is asserted by
  `tests/test_web/test_server.py`):
  - `_REPLAY_ROOT_SKIP_TYPES` (`workflow_started`, `workflow_completed`, `workflow_failed`,
    `checkpoint_saved`, `checkpoint_save_failed`) is filtered ONLY at root depth; sub-workflow
    lifecycle events are kept so the frontend `wfDepth` stays balanced.
  - `_REPLAY_INTERACTIVE_SKIP_TYPES` (`agent_paused`, `agent_resumed`,
    `iteration_limit_reached`, `iteration_limit_resolved`, `dialog_started`, `dialog_completed`,
    `guidance_received`) is filtered at EVERY depth.
- `guidance_received` is filtered but `guidance_applied` MUST NOT be: a still-pending submission
  would otherwise show as pending forever, while applied guidance is restored by
  `WorkflowContext.from_dict` and really is in effect. Pinned by `tests/test_web/test_server.py`
  (`test_preserves_guidance_applied_when_guidance_received_is_skipped`).
- `dialog_message` is left unfiltered only because it is inert once `dialog_started` is filtered:
  its renderers gate on `dialog_active`/`activeDialog`. Replayed dialog transcripts are therefore
  not visible on a resumed dashboard.
- `src/conductor/web/frontend/src/components/layout/Header.tsx` hides all live-control buttons in
  `replayMode`; `src/conductor/web/frontend/src/hooks/use-replay.ts` latches `replayMode` on mount
  (that hook mounts only after replay is confirmed), not when `/api/state` resolves.
- Adding an interactive event that sets a global store latch cleared only by a counterpart event:
  add the opening half to `_REPLAY_INTERACTIVE_SKIP_TYPES`.

### Stuck-reconnect warning (#330)

- The WebSocket client (`src/conductor/web/frontend/src/hooks/use-websocket.ts`) retries forever
  and oscillates between `connecting`/`reconnecting`, so NEVER time "reconnecting for N seconds"
  on a single status. The store's `setWsStatus` sets `wsDisconnectedSince` only on a fresh drop
  from `connected`, preserves it through retries, and clears it on reconnect.
- `src/conductor/web/frontend/src/lib/reconnect.ts::isReconnectStuck` (pure, unit-tested) compares
  it to `RECONNECT_WARNING_THRESHOLD_MS` (60 s), only while the workflow is running and not in
  replay. `src/conductor/web/frontend/src/components/layout/ReconnectWarningBanner.tsx` points at
  `system.bg_stderr_log`/`bg_stdout_log`, else `system.log_file` (the always-on events JSONL),
  else a generic hint. It clears only on an actual reconnect.

### Viewport anchoring and camera authority (#375)

- Layout (`src/conductor/web/frontend/src/components/graph/graph-layout.ts`) normalizes each
  rebuild's bounding box to the origin, so growing one container moves every node. Compensate the
  CAMERA, not the layout: `src/conductor/web/frontend/src/lib/graph-anchor.ts` resolves
  canvas-absolute positions by folding `parentId` chains (`toAbsolutePositions`; React Flow
  stores nested positions relative to the parent) and pans by the negated delta of a node shared
  by both layouts (`anchoredViewport`), at unchanged zoom.
- Anchor preference: the container owning the single toggled expansion key (matched on
  `data.childContextKey` and `data.groupExpansionKey`, on either side of the rebuild), else the
  shared node nearest the pane center, tie-broken by node id. `nextAnchorHint` keeps the last
  clicked container for at most `MAX_STICKY_ANCHOR_REBUILDS` (2) non-toggling rebuilds.
- `anchoredViewport` returning `null` on a zero delta is load-bearing: it suppresses a no-op
  `setViewport` that would interrupt an animation.
- Every animated `fitView` MUST call `claimCameraForAnimation(duration)`
  (`src/conductor/web/frontend/src/lib/camera-authority.ts`), and the rebuild effect yields while
  `isCameraAnimating()`: an instant `setViewport` interrupts a d3-zoom transition and leaves its
  promise unsettled.
- `src/conductor/web/frontend/src/components/graph/WorkflowGraph.tsx` wraps the graph in its own
  `ReactFlowProvider` so the rebuild effect, outside `<ReactFlow>`, can reach `setViewport`.
- Known limitation: collapsing several individually expanded subworkflows at once leaves a small
  accumulated horizontal pan; fit-view clears it.

### Frontend build, tests, and event types

- `src/conductor/web/static/` is GENERATED by `make build-frontend` and committed. NEVER edit it
  by hand; rebuild and commit it with any frontend source change.
- Frontend unit tests: `make test-frontend` (Vitest; tests sit beside sources, for example
  `src/conductor/web/frontend/src/lib/graph-anchor.test.ts`).
- A new event type the dashboard renders MUST be added to
  `src/conductor/web/frontend/src/types/events.ts` and handled in
  `src/conductor/web/frontend/src/stores/workflow-store.ts` (plus any detail component), then the
  assets rebuilt.
- `src/conductor/web/frontend/src/lib/auth.ts` sends the token as an `Authorization` header on
  mutating fetches and as `?token=` on `/ws`. In dev,
  `src/conductor/web/frontend/vite.config.ts` injects
  `CONDUCTOR_GATE_TOKEN` via `transformIndexHtml` (serve only), and the `/ws` proxy needs
  `changeOrigin: true` or the handshake fails the Host check.

## Change checklist

- [ ] New HTTP route: decide mutating vs read-only; mutating routes go in the guard's protected
      list and need a token and JSON content type; extend `tests/test_web/test_request_guard.py`.
- [ ] New WebSocket message type: rely on handshake auth; never add an unauthenticated socket path.
- [ ] New event type: frontend event types + store handler + rebuild `static/`; decide
      whether resume replay must skip it (root-only lifecycle vs every-depth interactive latch).
- [ ] Any camera move: claim camera authority for animations; keep the zero-delta `null`.
- [ ] Run `uv run pytest tests/test_web` and `make test-frontend`; run `make build-frontend` and
      commit `src/conductor/web/static/`.

## Rationale and evidence

**Pure ASGI guard (#397).** Browsers could never satisfy the old per-message token check, and a
decorator middleware silently skipped WebSockets. Authenticating the handshake is strictly
stronger and gives one auth point.

**Replay skip sets (#373, #400).** A run stopped then killed logs `agent_paused` with no
`agent_resumed`; replaying it rendered Resume/Kill on the live resumed run, hiding the graceful
Stop behind a Kill that hard-stops a healthy workflow. The interactive set is depth-independent
because the control channel is always the root dashboard and the store resolves those nodes
without `activeTarget`.

**Viewport anchoring (#375).** The layout was never wrong; the world slid under a fixed camera.
Moving the camera keeps the clicked container under the cursor.

## Related references

- [CLI reference: environment variables](../cli-reference.md#environment-variables) and
  [`conductor gate respond`](../cli-reference.md#conductor-gate-respond)
- [Dashboard deep links](../dashboard-deep-links.md)
- [Web UI design](../projects/web-ui/web-ui.design.md)
- [CLI and runs](cli-and-runs.md), [workflow engine](workflow-engine.md), [testing](testing.md)
