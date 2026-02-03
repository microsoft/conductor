# Checkpointing & Resume Brainstorm

## Overview

Workflow checkpointing enables saving execution state after each agent completion, allowing workflows to be resumed from the last successful checkpoint rather than starting over after failures.

## Motivating Use Case

From production usage: A 10-agent workflow runs for 5 minutes, processes 8 agents successfully, then encounters a rate limit error on agent 9. Currently, the entire workflow must be restarted, re-running agents 1-8 unnecessarily.

**Current behavior (no checkpoint):**
```
Agent 1 ✓ → Agent 2 ✓ → ... → Agent 8 ✓ → Agent 9 ✗ (rate limit)
                                              ↓
                                    WORKFLOW FAILED
                                    (must restart from Agent 1)
```

**Desired behavior (with checkpoint):**
```
Agent 1 ✓ → Agent 2 ✓ → ... → Agent 8 ✓ → [CHECKPOINT] → Agent 9 ✗
                                              ↓
                                    conductor resume <id>
                                              ↓
                                    Agent 9 ✓ → Agent 10 ✓ → $end
```

## Use Cases

1. **Long-running workflows**: Multi-agent analysis taking 10+ minutes
2. **Rate limit recovery**: Resume after 429 errors without re-running completed agents
3. **Network failures**: Connectivity drops during execution
4. **Manual interruption**: Ctrl+C during development, resume later
5. **Time-travel debugging**: Inspect state at any checkpoint, replay from there
6. **Parallel group recovery**: Resume after one agent in a parallel group fails

## Design Decisions

### 1. Checkpoint Granularity

| Option | Pros | Cons | Decision |
|--------|------|------|----------|
| Per-agent (after each agent completes) | Fine-grained recovery, minimal re-work | Storage overhead | ✅ **Selected** |
| Per-parallel-group | Matches natural boundaries | Loses partial parallel progress | Consider for v2 |
| User-specified points | Maximum control | Requires workflow changes | Future enhancement |

**Decision**: Checkpoint after every agent/parallel-group completion. Storage is cheap, user time is expensive.

### 2. Checkpoint Storage Backend

| Backend | Pros | Cons | Decision |
|---------|------|------|----------|
| SQLite | Zero config, portable, single file | Single-node only | ✅ **Default** |
| PostgreSQL | Scalable, concurrent access | Requires server | Optional |
| File-based (JSON) | Human-readable, git-friendly | Harder to query | Future option |

**Decision**: SQLite default with abstraction layer for future PostgreSQL support.

### 3. Intent + Completion Pattern (Critical)

**The Wrong Way** (data loss on crash):
```python
result = agent.run()   # Agent executes
checkpoint(result)     # Crash here = agent ran but not recorded
                       # On resume: agent runs AGAIN (double execution)
```

**The Right Way**:
```python
state['in_progress'] = agent.name
checkpoint()           # Mark INTENT before execution

result = agent.run()   # Agent executes

state['completed'].add(agent.name)
state['outputs'][agent.name] = result
checkpoint()           # Mark COMPLETION after execution
```

**On resume**: If `in_progress` set but not in `completed`, skip to next agent (or prompt user for re-run).

### 4. Checkpoint Retention Policy

```yaml
checkpoint:
  retention:
    max_checkpoints: 10        # Per workflow, oldest pruned first
    max_age_days: 7            # Prune older than this
    keep_successful: true      # Always keep last successful run
```

## YAML Syntax

### Basic Usage
```yaml
workflow:
  name: long-analysis
  checkpoint:
    enabled: true               # Default: false
    storage: sqlite             # sqlite | postgres
    path: .conductor/checkpoints/
```

### Full Configuration
```yaml
workflow:
  name: long-analysis
  checkpoint:
    enabled: true
    storage: sqlite
    path: .conductor/checkpoints/${workflow.name}/
    on_failure: pause           # pause | continue | rollback
    retention:
      max_checkpoints: 10
      max_age_days: 7
      keep_successful: true
```

### Resume Behavior Options

| `on_failure` | Behavior |
|--------------|----------|
| `pause` | Save checkpoint, exit with resume instructions |
| `continue` | Log error, continue to next agent (if routes allow) |
| `rollback` | Delete checkpoint, clean exit (for testing) |

## CLI Commands

```bash
# Normal run (creates checkpoints if enabled)
conductor run workflow.yaml --input question="..."

# Resume from last checkpoint
conductor resume <workflow-name>
conductor resume --checkpoint-id <id>

# List checkpoints
conductor checkpoints list
conductor checkpoints list --workflow my-analysis

# Show checkpoint details
conductor checkpoints show <id>

# Prune old checkpoints
conductor checkpoints prune --older-than 7d

# Clear all checkpoints for a workflow
conductor checkpoints clear my-analysis
```

## Implementation Components

### 1. Schema Extensions (`config/schema.py`)

```python
class CheckpointConfig(BaseModel):
    enabled: bool = False
    storage: Literal["sqlite", "postgres"] = "sqlite"
    path: str = ".conductor/checkpoints/"
    on_failure: Literal["pause", "continue", "rollback"] = "pause"
    retention: RetentionConfig = Field(default_factory=RetentionConfig)

class WorkflowDef(BaseModel):
    # ... existing fields
    checkpoint: CheckpointConfig = Field(default_factory=CheckpointConfig)
```

### 2. Checkpoint Store Abstraction (`engine/checkpoint.py`)

```python
@dataclass
class CheckpointData:
    checkpoint_id: str
    workflow_name: str
    workflow_inputs: dict[str, Any]
    current_agent: str
    in_progress: str | None
    completed_agents: list[str]
    agent_outputs: dict[str, Any]
    context_snapshot: dict[str, Any]
    iteration: int
    created_at: datetime
    updated_at: datetime

class CheckpointStore(ABC):
    @abstractmethod
    async def save(self, data: CheckpointData) -> None: ...

    @abstractmethod
    async def load(self, checkpoint_id: str) -> CheckpointData | None: ...

    @abstractmethod
    async def list_checkpoints(self, workflow_name: str | None = None) -> list[CheckpointData]: ...

    @abstractmethod
    async def delete(self, checkpoint_id: str) -> None: ...

    @abstractmethod
    async def prune(self, max_age_days: int, max_count: int) -> int: ...

class SQLiteCheckpointStore(CheckpointStore):
    def __init__(self, db_path: Path): ...
    # Implementation with aiosqlite
```

### 3. WorkflowEngine Integration (`engine/workflow.py`)

```python
class WorkflowEngine:
    def __init__(self, config, provider, checkpoint_store=None):
        self.checkpoint_store = checkpoint_store or self._create_checkpoint_store()
        self.checkpoint_data: CheckpointData | None = None

    async def run(self, inputs: dict[str, Any], resume_from: str | None = None):
        if resume_from:
            await self._resume_from_checkpoint(resume_from)
        else:
            await self._initialize_checkpoint(inputs)

        while True:
            agent = self._find_agent(current_agent_name)

            # Mark intent BEFORE execution
            await self._checkpoint_intent(agent.name)

            try:
                output = await self.executor.execute(agent, agent_ctx)
            except Exception as e:
                await self._handle_failure(agent.name, e)
                raise

            # Mark completion AFTER execution
            await self._checkpoint_completion(agent.name, output)

            # ... routing logic
```

### 4. CLI Commands (`cli/checkpoints.py`)

```python
@app.command()
def resume(
    workflow_or_id: str,
    checkpoint_id: Optional[str] = None,
):
    """Resume workflow from checkpoint."""
    ...

@app.command()
def checkpoints(
    action: Literal["list", "show", "prune", "clear"],
    ...
):
    """Manage workflow checkpoints."""
    ...
```

## Storage Schema (SQLite)

```sql
CREATE TABLE checkpoints (
    id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    workflow_inputs TEXT NOT NULL,  -- JSON
    current_agent TEXT NOT NULL,
    in_progress TEXT,               -- NULL if not mid-execution
    completed_agents TEXT NOT NULL, -- JSON array
    agent_outputs TEXT NOT NULL,    -- JSON
    context_snapshot TEXT NOT NULL, -- JSON
    iteration INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_workflow_name ON checkpoints(workflow_name);
CREATE INDEX idx_updated_at ON checkpoints(updated_at);
```

## Files Affected

### New Files
- `src/conductor/engine/checkpoint.py` - CheckpointStore and implementations
- `src/conductor/cli/checkpoints.py` - CLI commands
- `tests/test_engine/test_checkpoint.py` - Unit tests
- `tests/test_integration/test_checkpoint_workflows.py` - Integration tests

### Modified Files
- `src/conductor/config/schema.py` - Add CheckpointConfig
- `src/conductor/engine/workflow.py` - Integrate checkpointing
- `src/conductor/cli/app.py` - Add checkpoint commands
- `src/conductor/cli/run.py` - Add resume option

## Open Questions

1. **Parallel group checkpoint granularity**: Checkpoint before/after entire group, or track individual agent completion within group?

2. **For-each group handling**: Store all item results as they complete, or wait for entire group?

3. **Human gate state**: Save gate selection, or require re-selection on resume?

4. **Context size limits**: What if checkpoint data exceeds reasonable storage? Compress? Truncate?

## Future Enhancements

- PostgreSQL backend for team/production use
- Checkpoint diff visualization
- Automatic checkpoint-based retry on transient failures
- Export/import checkpoints across machines
- Integration with OpenTelemetry for trace continuity
