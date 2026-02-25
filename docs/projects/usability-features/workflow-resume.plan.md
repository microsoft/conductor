# Workflow Resume After Failure — Solution Design

> **Source:** `docs/projects/usability-features/usability-features.brainstorm.md` §5
> **Revision:** 2 — Addresses technical review feedback (score 82/100)

---

## 1. Problem Statement

All Conductor workflow state lives in memory (`WorkflowContext`, `LimitEnforcer`, `WorkflowEngine`) and is lost when an error occurs. The current error handler in `WorkflowEngine.run()` (workflow.py L834-841) fires an `on_error` hook and re-raises, but saves nothing. A 10-minute multi-agent research workflow that fails at the final synthesizer step forces the user to re-run the entire workflow from scratch — re-executing all planner and researcher agents, burning time and API credits.

Most failures (idle recovery exhaustion, max iterations, timeout, network errors, output validation, Ctrl+C) happen while the full `WorkflowContext` is still available in memory. Only ungraceful process kills (SIGKILL, OOM) lose state irrecoverably.

This design implements an **on-failure state dump** that serializes context to a JSON checkpoint file, and a **`conductor resume`** CLI command that reconstructs state and continues execution from the failed agent.

---

## 2. Goals and Non-Goals

### Goals

1. **Save workflow state on failure** — Automatically serialize `WorkflowContext` + failure metadata to a JSON checkpoint file when any error occurs during `WorkflowEngine.run()`.
2. **Resume from checkpoint** — Provide `conductor resume` CLI command that loads a checkpoint, reconstructs state, and re-runs the failed agent with all prior context.
3. **Checkpoint management** — Provide `conductor checkpoints` CLI command to list and inspect available checkpoints.
4. **Workflow integrity check** — Compute SHA-256 hash of the workflow YAML at checkpoint time; warn on resume if the workflow has changed.
5. **Copilot session resume** — Attempt to reuse Copilot SDK sessions via `resume_session()` if session IDs are in the checkpoint; fall back to new sessions gracefully.
6. **Zero overhead on happy path** — No checkpointing during normal execution; serialization only happens on failure.

### Non-Goals

- **Continuous checkpointing** — No periodic state saves during normal execution (would require `--checkpoint` flag, future enhancement).
- **SIGKILL/OOM recovery** — Process dies before handler runs; state is lost.
- **Partial agent output recovery** — If an agent was mid-execution, its output is lost; the agent re-runs from scratch.
- **Automatic workflow migration** — No schema-aware diffing between checkpoint and modified YAML; hash mismatch produces a warning only.
- **Checkpoint encryption or access control** — Checkpoints are plain JSON in the temp directory.

---

## 3. Requirements

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | On any exception in `WorkflowEngine.run()`, serialize `WorkflowContext`, `LimitEnforcer` state, `current_agent_name`, failure metadata, and workflow identity to a JSON checkpoint file. |
| FR-2 | Checkpoint written to `$TMPDIR/conductor/checkpoints/<workflow-name>-<YYYYMMDD-HHMMSS>.json`. |
| FR-3 | Print to stderr: `Workflow state saved to <path>. Resume with: conductor resume <workflow.yaml>` |
| FR-4 | `conductor resume workflow.yaml` loads the most recent checkpoint for that workflow and resumes. |
| FR-5 | `conductor resume --from <path>` loads a specific checkpoint file and resumes. |
| FR-6 | `conductor checkpoints` lists all available checkpoint files with metadata (workflow name, timestamp, failed agent, error type). |
| FR-7 | `conductor checkpoints workflow.yaml` lists checkpoints for a specific workflow only. |
| FR-8 | On resume, compare `workflow_hash` and warn to stderr if the workflow YAML has changed since the checkpoint was created. |
| FR-9 | On resume, reconstruct `WorkflowContext` with `workflow_inputs`, `agent_outputs`, `current_iteration`, `execution_history`. |
| FR-10 | On resume, reconstruct `LimitEnforcer` with restored `current_iteration` and `execution_history`; reset `start_time` (fresh timeout window). |
| FR-11 | On resume, set `current_agent_name` to the failed agent and begin execution from that point in the main loop. |
| FR-12 | `WorkflowContext` provides `to_dict()` and `from_dict()` serialization methods. |
| FR-13 | `LimitEnforcer` provides `to_dict()` and `from_dict()` serialization methods. |
| FR-14 | On resume with Copilot provider, attempt `client.resume_session(session_id)` using stored session IDs; fall back to new session on failure. Session IDs survive `session.destroy()` per SDK design (destroy releases local resources; resume re-attaches to server-side state). |
| FR-15 | Delete the checkpoint file after successful resume completion. |

### Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR-1 | Checkpoint serialization must complete in < 1 second for typical workflows (< 50 agent outputs). |
| NFR-2 | No performance impact on the happy path (normal workflow execution). |
| NFR-3 | Checkpoint format is versioned (version field) for future compatibility. |
| NFR-4 | All checkpoint data must be JSON-serializable (no Python objects, datetimes as ISO strings). |

---

## 4. Solution Architecture

### 4.1 Overview

The solution adds three capabilities to Conductor:

1. **Checkpoint serialization** — A new `CheckpointManager` module handles reading, writing, listing, and validating checkpoint files. `WorkflowEngine.run()` error handlers call the manager to dump state.

2. **State serialization** — `WorkflowContext.to_dict()`/`from_dict()` and `LimitEnforcer.to_dict()`/`from_dict()` convert in-memory state to/from JSON-compatible dicts.

3. **Resume execution** — `WorkflowEngine` gains a `resume()` method (or `run()` accepts a checkpoint parameter) that restores state and re-enters the main loop at the failed agent. Two new CLI commands (`resume`, `checkpoints`) expose this.

### 4.2 Key Components

```
┌─────────────────────────────────────────────────────────┐
│                         CLI Layer                        │
│  app.py: resume command, checkpoints command             │
│  run.py: resume_workflow_async()                         │
└────────────┬──────────────────────────────┬──────────────┘
             │                              │
             ▼                              ▼
┌────────────────────────┐    ┌────────────────────────────┐
│   CheckpointManager    │    │     WorkflowEngine         │
│  (new module)          │    │                            │
│  - save_checkpoint()   │    │  run() — adds checkpoint   │
│  - load_checkpoint()   │    │    dump in except block    │
│  - list_checkpoints()  │    │  resume() — restores state │
│  - validate_checkpoint │    │    and re-enters main loop │
│  - generate_path()     │    │                            │
│  - cleanup()           │    └─────┬──────────────────────┘
└────────────────────────┘          │
                                    ▼
                     ┌──────────────────────────┐
                     │ WorkflowContext / Limits  │
                     │ to_dict() / from_dict()   │
                     └──────────────────────────┘
```

### 4.3 Checkpoint File Format

```json
{
  "version": 1,
  "workflow_path": "/absolute/path/to/workflow.yaml",
  "workflow_hash": "sha256:abc123def456...",
  "created_at": "2026-02-24T15:30:00Z",
  "failure": {
    "error_type": "ProviderError",
    "message": "Session appears stuck after 3 recovery attempts",
    "agent": "synthesizer",
    "iteration": 4
  },
  "inputs": {"topic": "AI in healthcare", "depth": "comprehensive"},
  "current_agent": "synthesizer",
  "context": {
    "workflow_inputs": {"topic": "AI in healthcare", "depth": "comprehensive"},
    "agent_outputs": {
      "planner": {"plan": "...", "summary": "..."},
      "researcher": {"findings": ["..."], "sources": ["..."], "coverage": 85}
    },
    "current_iteration": 3,
    "execution_history": ["planner", "researcher", "researcher"]
  },
  "limits": {
    "current_iteration": 3,
    "max_iterations": 15
  },
  "copilot_session_ids": {}
}
```

### 4.4 Data Flow

#### Save Flow (on failure — engine-level only, see §4.7)

```
WorkflowEngine._execute_loop() raises exception
  → except block catches ConductorError / KeyboardInterrupt / Exception
  → Calls on_error hook
  → Calls CheckpointManager.save_checkpoint(
      workflow_path, context, limits, current_agent_name, error
    )
  → CheckpointManager:
      1. Computes workflow_hash = sha256(workflow_yaml_bytes)
      2. Calls context.to_dict(), limits.to_dict()
      3. Builds checkpoint dict with version, metadata, failure info
      4. Writes JSON to $TMPDIR/conductor/checkpoints/<name>-<timestamp>.json
      5. Returns checkpoint_path (or None if save fails — never raises)
  → Engine stores checkpoint_path on exception for CLI to read
  → Re-raises original exception
CLI layer (run_workflow_async):
  → Catches exception from engine.run()
  → Reads checkpoint_path from engine (or from engine's last_checkpoint_path attribute)
  → Prints resume instructions to stderr
  → Re-raises original exception
```

**Note:** Checkpoint save happens ONLY at the engine level. The CLI layer only prints the user-facing resume message. This avoids duplicate checkpoint files (see §4.7).

#### Resume Flow

```
CLI: conductor resume workflow.yaml [--from <path>]
  → resume_workflow_async():
      1. CheckpointManager.load_checkpoint(path) or find_latest(workflow_path)
      2. Load workflow YAML, compute current hash
      3. Compare hashes — warn if different
      4. WorkflowContext.from_dict(checkpoint["context"])
      5. LimitEnforcer.from_dict(checkpoint["limits"], config.workflow.limits)
      6. Create WorkflowEngine with restored context
      7. engine.resume(current_agent=checkpoint["current_agent"])
      8. On success: CheckpointManager.cleanup(checkpoint_path)
      9. Return result
```

#### Engine.resume() Method

```python
async def resume(self, current_agent_name: str) -> dict[str, Any]:
    """Resume workflow execution from a specific agent.
    
    Assumes self.context and self.limits have been pre-loaded 
    from checkpoint data. Enters the main execution loop at 
    current_agent_name without calling limits.start() (which
    would reset iteration counters).
    """
    # Reset timeout (fresh window for resumed execution)
    self.limits.start_time = time.monotonic()
    
    # Execute on_start hook (signals resume)
    self._execute_hook("on_start")
    
    try:
        async with self.limits.timeout_context():
            while True:
                # ... identical main loop as run() ...
    except ...:
        # ... identical error handling with checkpoint save ...
```

To avoid duplicating the main loop, the implementation will extract the core loop into a private `_execute_loop(current_agent_name)` method that both `run()` and `resume()` call.

### 4.5 API Contracts

#### CheckpointManager (new: `src/conductor/engine/checkpoint.py`)

```python
@dataclass
class CheckpointData:
    """Parsed checkpoint file contents."""
    version: int
    workflow_path: str
    workflow_hash: str
    created_at: str
    failure: dict[str, Any]
    inputs: dict[str, Any]
    current_agent: str
    context: dict[str, Any]
    limits: dict[str, Any]
    copilot_session_ids: dict[str, str]
    file_path: Path  # path where loaded from

class CheckpointManager:
    CHECKPOINT_VERSION = 1
    
    @staticmethod
    def save_checkpoint(
        workflow_path: Path,
        context: WorkflowContext,
        limits: LimitEnforcer,
        current_agent: str,
        error: Exception,
        inputs: dict[str, Any],
        copilot_session_ids: dict[str, str] | None = None,
    ) -> Path:
        """Serialize state to checkpoint file. Returns file path."""
    
    @staticmethod
    def load_checkpoint(checkpoint_path: Path) -> CheckpointData:
        """Load and validate a checkpoint file."""
    
    @staticmethod
    def find_latest_checkpoint(workflow_path: Path) -> Path | None:
        """Find the most recent checkpoint for a workflow."""
    
    @staticmethod
    def list_checkpoints(workflow_path: Path | None = None) -> list[CheckpointData]:
        """List all checkpoints, optionally filtered by workflow."""
    
    @staticmethod
    def compute_workflow_hash(workflow_path: Path) -> str:
        """Compute SHA-256 hash of workflow file contents."""
    
    @staticmethod
    def cleanup(checkpoint_path: Path) -> None:
        """Delete a checkpoint file after successful resume."""
    
    @staticmethod
    def get_checkpoints_dir() -> Path:
        """Return $TMPDIR/conductor/checkpoints/, creating if needed."""
```

#### WorkflowContext Additions

```python
class WorkflowContext:
    def to_dict(self) -> dict[str, Any]:
        """Serialize context to JSON-compatible dict."""
        return {
            "workflow_inputs": self.workflow_inputs,
            "agent_outputs": self.agent_outputs,
            "current_iteration": self.current_iteration,
            "execution_history": list(self.execution_history),
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowContext:
        """Reconstruct context from serialized dict."""
        ctx = cls()
        ctx.workflow_inputs = data["workflow_inputs"]
        ctx.agent_outputs = data["agent_outputs"]
        ctx.current_iteration = data["current_iteration"]
        ctx.execution_history = data["execution_history"]
        return ctx
```

#### LimitEnforcer Additions

```python
class LimitEnforcer:
    def to_dict(self) -> dict[str, Any]:
        """Serialize limit state to JSON-compatible dict."""
        return {
            "current_iteration": self.current_iteration,
            "max_iterations": self.max_iterations,
            "execution_history": list(self.execution_history),
        }
    
    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        timeout_seconds: int | None = None,
    ) -> LimitEnforcer:
        """Reconstruct enforcer from serialized dict.
        
        Uses max_iterations from checkpoint (may have been user-increased)
        and timeout_seconds from the workflow config (fresh timeout window).
        """
        enforcer = cls(
            max_iterations=data["max_iterations"],
            timeout_seconds=timeout_seconds,
        )
        enforcer.current_iteration = data["current_iteration"]
        enforcer.execution_history = data["execution_history"]
        enforcer.start_time = time.monotonic()  # Fresh timeout window
        return enforcer
```

#### New CLI Commands

```python
# conductor resume workflow.yaml
# conductor resume --from /path/to/checkpoint.json
@app.command()
def resume(
    workflow: Path | None = Argument(None),
    from_checkpoint: Path | None = Option(None, "--from"),
    skip_gates: bool = Option(False, "--skip-gates"),
    log_file: str | None = Option(None, "--log-file"),
) -> None: ...

# conductor checkpoints
# conductor checkpoints workflow.yaml
@app.command()
def checkpoints(
    workflow: Path | None = Argument(None),
) -> None: ...
```

### 4.6 Copilot Session Resume

The Copilot SDK (installed as `github-copilot-sdk`) provides the following **verified** session management APIs:

- `CopilotClient.create_session(config: SessionConfig) -> CopilotSession` — creates a new session
- `CopilotClient.resume_session(session_id: str, config: ResumeSessionConfig | None) -> CopilotSession` — resumes a previously created session
- `CopilotClient.list_sessions() -> list[SessionMetadata]` — lists all available sessions with `sessionId`, `startTime`, `modifiedTime`, `summary`, `isRemote`
- `CopilotClient.delete_session(session_id: str)` — permanently deletes a session (cannot be resumed after)
- `CopilotSession.destroy()` — releases local resources but **does not delete the session**; the SDK docstring explicitly states: "To continue the conversation, use `CopilotClient.resume_session` with the session ID."

**Key insight:** The current `_execute_sdk_call()` calls `session.destroy()` in a `finally` block (copilot.py L487). This is **compatible** with session resume because `destroy()` only clears local event/tool handlers and does not call `delete_session()`. The server-side session state (conversation history) persists until `delete_session()` is called. Therefore, session IDs stored in checkpoints remain valid for `resume_session()`.

**Implementation approach:**

1. **During execution:** After `create_session()`, store `{agent_name: session.session_id}` in a `_session_ids` dict on `CopilotProvider`. The `session_id` attribute is public on `CopilotSession`.
2. **On checkpoint save:** Collect session IDs via `provider.get_session_ids()` and include in checkpoint under `copilot_session_ids`.
3. **On resume:** Pass session IDs to `CopilotProvider` via `set_resume_session_ids()`. Before creating a new session, check if a stored session ID exists for the current agent and attempt `self._client.resume_session(session_id)`.
4. **On resume failure:** Catch `RuntimeError` (SDK raises this if session doesn't exist), log a warning, and fall back to `create_session()`.

This is a best-effort optimization — the workflow always works without session resume because `WorkflowContext` provides the full execution history.

### 4.7 Checkpoint Save Ownership

**Single save point: engine level only.** Checkpoint saves happen exclusively in `WorkflowEngine._execute_loop()` except blocks. The CLI layer (`run_workflow_async()`) does NOT perform a separate checkpoint save.

Rationale:
- The engine has direct access to `self.context`, `self.limits`, and `self._current_agent_name` — all required for a complete checkpoint.
- The CLI layer would need to extract these from the engine, creating unnecessary coupling.
- A single save point eliminates the risk of duplicate checkpoint files per failure.
- The engine's `save_checkpoint()` is wrapped in try/except and never raises (logs warning on failure), so there's no need for a CLI-layer "safety net".

The CLI layer's responsibility is limited to: (1) print the resume instructions to stderr after `engine.run()` raises, and (2) handle the `resume` and `checkpoints` commands.

### 4.8 Parallel/For-Each Group Re-Execution on Resume

**Design decision:** When a failure occurs inside a parallel or for-each group, `current_agent_name` points to the group name, and resume re-executes the **entire** group.

**Trade-off analysis:**
- A for-each group iterating over 50 items where item #49 fails will re-run all 50 items on resume.
- A parallel group of 5 agents where agent #4 fails will re-run all 5 agents.
- This is the simplest correct approach because partial group state (completed items within a group) is only committed to `WorkflowContext` atomically after the entire group completes. Mid-group failures leave no partial state in context.

**Why this is acceptable for v1:**
1. Most parallel/for-each groups are small (2-10 items). The brainstorm spec targets common multi-agent research workflows, not batch processing.
2. The alternative (sub-group checkpointing) would require tracking per-item completion state within groups, significantly increasing complexity.
3. Users who hit this can work around it by splitting large for-each groups into smaller batches.

**Future enhancement:** If demand warrants, add `partial_group_state` to the checkpoint format to track completed items within a group, enabling partial group re-execution. This would require changes to `_execute_for_each_group()` and `_execute_parallel_group()` to accept a set of already-completed items.

### 4.9 Dual Iteration Tracking: WorkflowContext vs LimitEnforcer

**Design note:** Both `WorkflowContext` and `LimitEnforcer` independently track `current_iteration` and `execution_history`, and they **diverge** for parallel/for-each groups:

- `WorkflowContext.store()` increments `current_iteration` by 1 per call (once per group, regardless of group size).
- `LimitEnforcer.record_execution()` increments `current_iteration` by `count` (equal to the number of agents/items in the group).

Example: A parallel group of 3 agents produces `WorkflowContext.current_iteration = 1` but `LimitEnforcer.current_iteration = 3`.

**Checkpoint serialization** correctly preserves both independently — `context.current_iteration` and `limits.current_iteration` are separate fields in the checkpoint. On resume, each is restored to its respective object. Implementers must not confuse the two: `WorkflowContext.current_iteration` counts store operations; `LimitEnforcer.current_iteration` counts agent executions for limit enforcement.

---

## 5. Dependencies

### Internal Dependencies

| Component | Dependency | Reason |
|-----------|-----------|--------|
| `CheckpointManager` | `WorkflowContext` | Calls `to_dict()` for serialization |
| `CheckpointManager` | `LimitEnforcer` | Calls `to_dict()` for serialization |
| `WorkflowEngine.run()` | `CheckpointManager` | Saves checkpoint on failure |
| `WorkflowEngine.resume()` | `CheckpointManager` | Loads checkpoint for resume |
| CLI `resume` command | `CheckpointManager` | Finds and loads checkpoints |
| CLI `resume` command | `run_workflow_async` variant | Executes resumed workflow |
| `CopilotProvider` | Session ID tracking | Stores IDs for checkpoint |

### External Dependencies

| Dependency | Version | Purpose |
|-----------|---------|---------|
| Python `json` | stdlib | Checkpoint serialization |
| Python `hashlib` | stdlib | SHA-256 workflow hash |
| Python `tempfile` | stdlib | `$TMPDIR` resolution |
| Python `pathlib` | stdlib | Path operations |
| `typer` | existing | CLI command registration |
| `rich` | existing | Console output formatting |
| Copilot SDK | existing | `CopilotClient.resume_session()`, `CopilotClient.list_sessions()`, `CopilotSession.session_id` — **verified present** in installed SDK |

No new external dependencies required.

---

## 6. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Non-serializable agent outputs** — Agent outputs containing non-JSON types (bytes, custom objects, circular refs) cause checkpoint failure | Medium | High | Use a robust JSON serializer with fallback `str()` conversion for non-serializable values. Add `try/except` around checkpoint save to avoid masking the original error. |
| **Large checkpoint files** — Workflows with large agent outputs (base64 images, long documents) produce multi-MB checkpoints | Low | Medium | Checkpoint size is bounded by agent output size, which is already in memory. Log a warning if checkpoint exceeds 10MB. Future: add truncation option. |
| **Stale Copilot sessions** — Sessions may expire or be garbage-collected server-side between failure and resume, causing `resume_session()` to raise `RuntimeError` | High | Low | Graceful fallback: catch `RuntimeError` from `resume_session()` and fall back to `create_session()`. Workflow context provides full history regardless. `session.destroy()` only clears local resources per SDK docs, so session IDs remain valid unless the server evicts them. |
| **Workflow YAML changes between checkpoint and resume** — User modifies workflow (renames agents, changes routes) and resume breaks | Medium | Medium | Hash comparison warns the user. No automatic migration — the user must decide to proceed. If the `current_agent` no longer exists, raise a clear error with suggestion. |
| **Checkpoint file permissions** — Other users can read checkpoint files in shared `$TMPDIR` | Low | Low | Checkpoints contain workflow I/O which may include sensitive data. Set file permissions to 0o600 (user-only read/write). |
| **Main loop duplication** — Extracting the loop into `_execute_loop()` risks divergence if one path is updated but not the other | Medium | Medium | Refactor: single `_execute_loop()` method called by both `run()` and `resume()`. They differ only in setup (fresh vs. restored state). |
| **Checkpoint compatibility across versions** — Future Conductor versions may change context/limits structure | Low | Medium | Version field in checkpoint. `from_dict()` validates version and raises clear error on mismatch. |
| **Parallel/for-each group re-execution cost** — When failure occurs inside a group, the entire group re-runs on resume (e.g., 50-item for-each fails at item #49 → all 50 re-run) | Medium | Medium | Accepted trade-off for v1: most groups are small (2-10 items), and partial group state would add significant complexity. Document this behavior in CLI output on resume. Future: add sub-group checkpointing (see §4.8). |
| **Iteration count divergence** — `WorkflowContext` and `LimitEnforcer` track `current_iteration` differently (per-store vs per-agent-execution) | Low | High | Serialize both independently in checkpoint. Document the divergence clearly (see §4.9). Unit tests must verify both are correctly restored after round-trip. |

---

## 7. Implementation Phases

### Phase 1: State Serialization
Add `to_dict()`/`from_dict()` to `WorkflowContext` and `LimitEnforcer`. These are pure data transformations with no side effects.

**Exit criteria:** Round-trip serialization tests pass for all context states (empty, single agent, multiple agents, parallel outputs, for-each outputs).

### Phase 2: Checkpoint Manager
Create `CheckpointManager` with save, load, list, validate, and cleanup operations. Standalone module with no engine changes.

**Exit criteria:** Unit tests cover save/load round-trip, file format validation, hash computation, latest-checkpoint lookup, and listing.

### Phase 3: Engine Integration
Modify `WorkflowEngine` to save checkpoints on failure and support `resume()`. Refactor main loop into `_execute_loop()`.

**Exit criteria:** Integration tests verify checkpoint save on error, resume continues from correct agent, and full workflow completion from resumed state.

### Phase 4: CLI Commands
Add `resume` and `checkpoints` commands to the CLI. Wire up to engine and checkpoint manager.

**Exit criteria:** E2E tests verify `conductor resume workflow.yaml`, `conductor resume --from <path>`, and `conductor checkpoints` produce correct output.

### Phase 5: Copilot Session Resume (Optional Enhancement)
Track session IDs in provider, include in checkpoints, attempt session resume on load.

**Exit criteria:** Integration test verifies session ID tracking and graceful fallback on stale sessions.

---

## 8. Files Affected

### New Files

| File Path | Purpose |
|-----------|---------|
| `src/conductor/engine/checkpoint.py` | `CheckpointManager` class and `CheckpointData` dataclass — handles checkpoint file I/O, validation, listing, cleanup. |
| `tests/test_engine/test_checkpoint.py` | Unit tests for `CheckpointManager` — save/load round-trip, file format, hash, listing, edge cases. |
| `tests/test_engine/test_context_serialization.py` | Unit tests for `WorkflowContext.to_dict()`/`from_dict()` round-trip with various context states. |
| `tests/test_engine/test_resume.py` | Integration tests for `WorkflowEngine.resume()` — checkpoint save on failure, resume from checkpoint, continued execution. |
| `tests/test_cli/test_resume_command.py` | CLI tests for `resume` and `checkpoints` commands. |

### Modified Files

| File Path | Changes |
|-----------|---------|
| `src/conductor/engine/context.py` | Add `to_dict()` and `from_dict()` methods to `WorkflowContext`. |
| `src/conductor/engine/limits.py` | Add `to_dict()` and `from_dict()` methods to `LimitEnforcer`. |
| `src/conductor/engine/workflow.py` | (1) Refactor main loop into `_execute_loop()`. (2) Add checkpoint save in `except` blocks. (3) Add `resume()` method. |
| `src/conductor/cli/app.py` | Add `resume` and `checkpoints` CLI commands. |
| `src/conductor/cli/run.py` | Add `resume_workflow_async()` function and resume message printing helper (checkpoint save is engine-only per §4.7). |
| `src/conductor/exceptions.py` | Add `CheckpointError` exception class (for checkpoint I/O failures). |
| `src/conductor/providers/copilot.py` | Track session IDs per agent execution; expose `get_session_ids()` method. (Phase 5 only) |

### Deleted Files

| File Path | Reason |
|-----------|--------|
| (none) | |

---

## 9. Implementation Plan

### Epic 1: State Serialization

**Status:** DONE

**Goal:** Add `to_dict()` and `from_dict()` methods to `WorkflowContext` and `LimitEnforcer` so their state can be serialized to/from JSON-compatible dicts.

**Prerequisites:** None.

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E1-T1 | IMPL | Add `to_dict()` method to `WorkflowContext` — returns dict with `workflow_inputs`, `agent_outputs`, `current_iteration`, `execution_history`. Must handle nested dicts and lists. | `src/conductor/engine/context.py` | DONE |
| E1-T2 | IMPL | Add `from_dict(data)` classmethod to `WorkflowContext` — constructs a `WorkflowContext` from a serialized dict, copying all fields. | `src/conductor/engine/context.py` | DONE |
| E1-T3 | TEST | Unit tests for `WorkflowContext.to_dict()`/`from_dict()`: empty context, single agent output, multiple agents, parallel group output format (`{type: 'parallel', outputs: {...}}`), for-each group output format (`{type: 'for_each', outputs: [...]}`), round-trip equality. | `tests/test_engine/test_context_serialization.py` | DONE |
| E1-T4 | IMPL | Add `to_dict()` method to `LimitEnforcer` — returns dict with `current_iteration`, `max_iterations`, `execution_history`. Exclude transient state (`start_time`, `current_agent`). | `src/conductor/engine/limits.py` | DONE |
| E1-T5 | IMPL | Add `from_dict(data, timeout_seconds)` classmethod to `LimitEnforcer` — constructs enforcer with restored iteration state and fresh `start_time`. Takes `timeout_seconds` from workflow config (not checkpoint) for fresh timeout window. | `src/conductor/engine/limits.py` | DONE |
| E1-T6 | TEST | Unit tests for `LimitEnforcer.to_dict()`/`from_dict()`: default state, mid-execution state, round-trip with iteration/history preserved, fresh start_time on reconstruction, user-increased max_iterations preserved. | `tests/test_engine/test_context_serialization.py` | DONE |

**Acceptance Criteria:**
- [x] `WorkflowContext` round-trips through `to_dict()`/`from_dict()` with identical state
- [x] `LimitEnforcer` round-trips with iteration state preserved and fresh timeout
- [x] All serialized output is JSON-serializable (`json.dumps()` succeeds)
- [x] Tests pass: `uv run pytest tests/test_engine/test_context_serialization.py`
- [x] `make lint && make typecheck` pass

---

### Epic 2: Checkpoint Manager

**Status:** DONE

**Goal:** Create a standalone `CheckpointManager` module that handles all checkpoint file operations: save, load, list, validate, and cleanup.

**Prerequisites:** Epic 1 (state serialization methods).

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E2-T1 | IMPL | Add `CheckpointError` exception to `exceptions.py` — inherits from `ConductorError`, used for checkpoint I/O failures (file not found, invalid format, version mismatch). | `src/conductor/exceptions.py` | DONE |
| E2-T2 | IMPL | Create `CheckpointData` dataclass in `checkpoint.py` — typed container for parsed checkpoint fields (`version`, `workflow_path`, `workflow_hash`, `created_at`, `failure`, `inputs`, `current_agent`, `context`, `limits`, `copilot_session_ids`, `file_path`). | `src/conductor/engine/checkpoint.py` | DONE |
| E2-T3 | IMPL | Implement `CheckpointManager.get_checkpoints_dir()` — returns `Path(tempfile.gettempdir()) / "conductor" / "checkpoints"`, creates directory if not exists. Follow existing `generate_log_path()` pattern from `run.py`. | `src/conductor/engine/checkpoint.py` | DONE |
| E2-T4 | IMPL | Implement `CheckpointManager.compute_workflow_hash(path)` — reads workflow file as bytes, returns `"sha256:<hex_digest>"`. | `src/conductor/engine/checkpoint.py` | DONE |
| E2-T5 | IMPL | Implement `CheckpointManager.save_checkpoint()` — accepts `workflow_path`, `context`, `limits`, `current_agent`, `error`, `inputs`, optional `copilot_session_ids`. Builds checkpoint dict, serializes to JSON with indent=2, writes atomically (write to `.tmp`, then rename). Sets file permissions to 0o600. Returns checkpoint file path. Wraps errors in `CheckpointError` but never raises (logs warning and returns None if save fails, to avoid masking the original error). | `src/conductor/engine/checkpoint.py` | DONE |
| E2-T6 | IMPL | Implement `CheckpointManager.load_checkpoint(path)` — reads JSON, validates version field, returns `CheckpointData`. Raises `CheckpointError` on file not found, invalid JSON, or unsupported version. | `src/conductor/engine/checkpoint.py` | DONE |
| E2-T7 | IMPL | Implement `CheckpointManager.find_latest_checkpoint(workflow_path)` — scans checkpoints dir for files matching `<workflow-name>-*.json`, returns path of the most recent by filename timestamp. Returns `None` if no checkpoints exist. | `src/conductor/engine/checkpoint.py` | DONE |
| E2-T8 | IMPL | Implement `CheckpointManager.list_checkpoints(workflow_path=None)` — lists all checkpoint files, optionally filtered by workflow name. Returns list of `CheckpointData` sorted by `created_at` descending. | `src/conductor/engine/checkpoint.py` | DONE |
| E2-T9 | IMPL | Implement `CheckpointManager.cleanup(path)` — deletes checkpoint file. Logs warning if file doesn't exist (idempotent). | `src/conductor/engine/checkpoint.py` | DONE |
| E2-T10 | IMPL | Add a `_make_json_serializable(obj)` helper — recursively converts non-JSON types to strings (handles bytes, datetime, Path, custom objects via `str()`). Used by `save_checkpoint()` to avoid serialization failures. | `src/conductor/engine/checkpoint.py` | DONE |
| E2-T11 | TEST | Unit tests for `CheckpointManager`: save/load round-trip, file format validation (version, required fields), hash computation, `find_latest_checkpoint` with multiple files, `list_checkpoints` with filtering, `cleanup` idempotent, atomic write (no partial files), file permissions, non-serializable value handling, `save_checkpoint` doesn't raise on failure. | `tests/test_engine/test_checkpoint.py` | DONE |

**Acceptance Criteria:**
- [ ] Checkpoint files are valid JSON matching the documented format
- [ ] `save_checkpoint()` never raises — returns `None` on failure with a logged warning
- [ ] `load_checkpoint()` raises `CheckpointError` with clear messages on invalid files
- [ ] `find_latest_checkpoint()` correctly identifies most recent checkpoint by timestamp
- [ ] `list_checkpoints()` returns sorted results with optional workflow filter
- [ ] File permissions are 0o600 (user-only read/write)
- [ ] Tests pass: `uv run pytest tests/test_engine/test_checkpoint.py`
- [ ] `make lint && make typecheck` pass

---

### Epic 3: Engine Integration

**Status:** DONE

**Goal:** Modify `WorkflowEngine` to save checkpoints on failure and support resuming from a checkpoint. Refactor the main execution loop to avoid duplication.

**Prerequisites:** Epic 1, Epic 2.

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E3-T1 | IMPL | Extract the main execution loop (workflow.py L544-841) into a private `_execute_loop(current_agent_name: str) -> dict[str, Any]` method. Both `run()` and the new `resume()` call this. The loop body is identical — the methods differ only in setup (context initialization vs. restoration). Keep the `try/except` in `_execute_loop()`. | `src/conductor/engine/workflow.py` | DONE |
| E3-T2 | IMPL | Add checkpoint save logic to the `except` blocks in `_execute_loop()`. After calling `on_error` hook, call `CheckpointManager.save_checkpoint()` with current state. Store `current_agent_name` by tracking it as `self._current_agent_name` (instance variable updated at each loop iteration). Store the returned checkpoint path as `self._last_checkpoint_path` so the CLI layer can read it for user-facing messages. | `src/conductor/engine/workflow.py` | DONE |
| E3-T3 | IMPL | Handle `KeyboardInterrupt` in `_execute_loop()` — catch it, save checkpoint, print resume message, re-raise. Currently not caught (L834-841 only catches `ConductorError` and `Exception`). | `src/conductor/engine/workflow.py` | DONE |
| E3-T4 | IMPL | Add `resume(current_agent_name: str) -> dict[str, Any]` method to `WorkflowEngine`. This method: (1) resets `self.limits.start_time` for a fresh timeout window, (2) calls `_execute_loop(current_agent_name)`. Assumes `self.context` and `self.limits` have been pre-populated from checkpoint data by the caller. | `src/conductor/engine/workflow.py` | DONE |
| E3-T5 | IMPL | Add `set_context(context: WorkflowContext)` and `set_limits(limits: LimitEnforcer)` methods to `WorkflowEngine` — allow external restoration of state from checkpoint (used by `resume_workflow_async()`). | `src/conductor/engine/workflow.py` | DONE |
| E3-T6 | IMPL | Store `workflow_path` on `WorkflowEngine` during construction (passed via config or explicitly). Needed by `CheckpointManager.save_checkpoint()` for checkpoint metadata. | `src/conductor/engine/workflow.py` | DONE |
| E3-T7 | TEST | Integration tests for checkpoint save on failure: create a workflow with a mock handler that raises `ProviderError` at a specific agent, verify checkpoint file is created with correct content (current_agent, context, limits, failure metadata). | `tests/test_engine/test_resume.py` | DONE |
| E3-T8 | TEST | Integration tests for resume: create a checkpoint with completed agents, call `engine.resume()`, verify execution continues from the checkpoint agent and produces correct final output. | `tests/test_engine/test_resume.py` | DONE |
| E3-T9 | TEST | Integration test: full round-trip — run a workflow that fails mid-execution, load the saved checkpoint, resume, verify the final output matches what a successful run would produce. | `tests/test_engine/test_resume.py` | DONE |
| E3-T10 | TEST | Test `KeyboardInterrupt` handling — verify checkpoint is saved when user presses Ctrl+C. | `tests/test_engine/test_resume.py` | DONE |
| E3-T11 | TEST | Test checkpoint cleanup — verify checkpoint file is deleted after successful resume. | `tests/test_engine/test_resume.py` | DONE |

**Acceptance Criteria:**
- [x] `run()` and `resume()` use the same `_execute_loop()` — no loop duplication
- [x] Checkpoint is saved on `ConductorError`, `KeyboardInterrupt`, and `Exception`
- [x] `resume()` correctly continues from the specified agent with full prior context
- [x] Checkpoint file is cleaned up after successful resume
- [x] Existing tests still pass (no regression from loop refactor)
- [x] Tests pass: `uv run pytest tests/test_engine/test_resume.py`
- [x] `make check` passes

---

### Epic 4: CLI Commands

**Status:** DONE

**Goal:** Add `conductor resume` and `conductor checkpoints` CLI commands that wire the checkpoint/resume system to user-facing CLI.

**Prerequisites:** Epic 3.

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E4-T1 | IMPL | Add `resume` command to `app.py`. Parameters: `workflow` (optional Path argument), `--from` (optional checkpoint path), `--skip-gates`, `--log-file`. Validates that exactly one of `workflow` or `--from` is provided. Imports and calls `resume_workflow_async()`. Prints JSON result to stdout. | `src/conductor/cli/app.py` | DONE |
| E4-T2 | IMPL | Add `checkpoints` command to `app.py`. Parameters: `workflow` (optional Path argument). Calls `CheckpointManager.list_checkpoints()` and displays a formatted table (Rich) with columns: workflow name, timestamp, failed agent, error type, file path. | `src/conductor/cli/app.py` | DONE |
| E4-T3 | IMPL | Implement `resume_workflow_async()` in `run.py`. Steps: (1) Load checkpoint via `CheckpointManager`, (2) Load workflow YAML, (3) Compare hashes — warn if different, (4) Reconstruct `WorkflowContext.from_dict()` and `LimitEnforcer.from_dict()`, (5) Create `ProviderRegistry` and `WorkflowEngine`, (6) Set engine context and limits, (7) Call `engine.resume()`, (8) On success: cleanup checkpoint. | `src/conductor/cli/run.py` | DONE |
| E4-T4 | IMPL | Add resume message printing in `run_workflow_async()` — when `engine.run()` raises, print the checkpoint path and resume instructions to stderr (the checkpoint itself is saved by the engine in E3-T2; the CLI only prints the user-facing message). | `src/conductor/cli/run.py` | DONE |
| E4-T5 | TEST | CLI tests for `resume` command: test with `--from` path, test with workflow path (finds latest), test missing arguments error, test nonexistent checkpoint error. Use `typer.testing.CliRunner`. | `tests/test_cli/test_resume_command.py` | DONE |
| E4-T6 | TEST | CLI tests for `checkpoints` command: test with no checkpoints, test with multiple checkpoints, test filtered by workflow path. | `tests/test_cli/test_resume_command.py` | DONE |
| E4-T7 | TEST | Test workflow hash mismatch warning: modify workflow after checkpoint, resume, verify warning printed to stderr. | `tests/test_cli/test_resume_command.py` | DONE |

**Acceptance Criteria:**
- [x] `conductor resume workflow.yaml` finds latest checkpoint and resumes
- [x] `conductor resume --from <path>` loads specific checkpoint and resumes
- [x] `conductor checkpoints` lists all checkpoints in a readable table
- [x] `conductor checkpoints workflow.yaml` filters to that workflow's checkpoints
- [x] Hash mismatch warning is printed when workflow changes between checkpoint and resume
- [x] JSON result is printed to stdout on successful resume
- [x] Tests pass: `uv run pytest tests/test_cli/test_resume_command.py`
- [x] `make check` passes

---

### Epic 5: Copilot Session Resume (Optional Enhancement)

**Status:** DONE

**Goal:** Track Copilot SDK session IDs during execution and attempt session resume on workflow resume, falling back to new sessions gracefully.

**Prerequisites:** Epic 3 (engine integration complete).

**SDK API verification (confirmed in installed `github-copilot-sdk`):**
- `CopilotSession.session_id` — public str attribute, available immediately after `create_session()`
- `CopilotClient.resume_session(session_id, config=None)` — returns `CopilotSession`, raises `RuntimeError` if session doesn't exist
- `CopilotSession.destroy()` — clears local handlers only; SDK docstring confirms: "To continue the conversation, use `CopilotClient.resume_session` with the session ID"
- `CopilotClient.delete_session(session_id)` — permanently removes session (we do NOT call this)

**Session lifecycle compatibility:** The current `_execute_sdk_call()` calls `session.destroy()` in a `finally` block (copilot.py L487). This is **compatible** with resume because `destroy()` releases local Python resources but does NOT delete the server-side session. No changes to the existing `destroy()` call are needed.

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E5-T1 | IMPL | Add `_session_ids: dict[str, str]` field to `CopilotProvider.__init__()`. In `_execute_sdk_call()`, after `session = await self._client.create_session(session_config)`, store `self._session_ids[agent.name] = session.session_id`. Add `get_session_ids() -> dict[str, str]` method that returns a copy. | `src/conductor/providers/copilot.py` | DONE |
| E5-T2 | IMPL | Add `set_resume_session_ids(ids: dict[str, str])` method to `CopilotProvider`. Stores `_resume_session_ids`. In `_execute_sdk_call()`, before `create_session()`, check `_resume_session_ids.get(agent.name)`. If present, attempt `session = await self._client.resume_session(session_id)`. Catch `RuntimeError` (SDK's error for non-existent sessions) and `Exception`, log warning, fall back to `create_session()`. | `src/conductor/providers/copilot.py` | DONE |
| E5-T3 | IMPL | Wire session ID collection into `WorkflowEngine` — after execution completes (or on failure), collect session IDs from the provider via `provider.get_session_ids()` (if provider has the method — duck-type check) and pass to `CheckpointManager.save_checkpoint()`. | `src/conductor/engine/workflow.py` | DONE |
| E5-T4 | IMPL | Wire session ID restoration in `resume_workflow_async()` — pass `copilot_session_ids` from checkpoint to provider via `set_resume_session_ids()` (if provider has the method) before calling `engine.resume()`. | `src/conductor/cli/run.py` | DONE |
| E5-T5 | TEST | Unit test for session ID tracking: mock `CopilotClient.create_session()` to return a session with a known `session_id`, verify `get_session_ids()` returns `{agent_name: session_id}`. | `tests/test_providers/test_copilot_resume.py` | DONE |
| E5-T6 | TEST | Unit test for session resume fallback: mock `CopilotClient.resume_session()` to raise `RuntimeError`, verify fallback to `create_session()` succeeds with warning logged. | `tests/test_providers/test_copilot_resume.py` | DONE |

**Acceptance Criteria:**
- [x] Session IDs are tracked per agent during execution via `session.session_id`
- [x] Session IDs are included in checkpoint files
- [x] On resume, Copilot provider attempts `client.resume_session(session_id)` before `client.create_session()`
- [x] Failed session resume (RuntimeError) falls back gracefully with a logged warning
- [x] No changes to existing `session.destroy()` calls (confirmed compatible)
- [x] Tests pass: `uv run pytest tests/test_providers/test_copilot_resume.py`
- [x] `make check` passes
