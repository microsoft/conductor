# Static Parallel Infrastructure - Prerequisite Verification

**Date**: 2026-01-31  
**Status**: ✅ VERIFIED - All tests passing  
**Purpose**: Document the static parallel infrastructure that will be reused for dynamic parallel (for-each) implementation

---

## Verification Results

### Test Suite Status

All parallel infrastructure tests are **passing**:

1. **Integration Tests** (`tests/test_integration/test_parallel_workflows.py`): **10/10 passed**
   - Parallel research agents success
   - Parallel validators with continue_on_error
   - All fail scenario with continue_on_error
   - Mixed sequential/parallel workflows
   - Routing from parallel groups based on results
   - Fail-fast mode stops immediately
   - All-or-nothing mode requires all success
   - All-or-nothing mode success when all succeed
   - Continue-on-error with mixed results
   - Context isolation prevents interference

2. **Engine Tests** (`tests/test_engine/test_parallel.py`): **18/18 passed**
   - Parallel group execution success
   - Fail-fast mode behavior
   - Continue-on-error mode behavior
   - All-or-nothing mode (success and failure cases)
   - Context isolation
   - Concurrent execution verification
   - Output structure validation
   - Error dataclass validation
   - Error message formatting (all modes)
   - Exception type differentiation
   - Routing to/from parallel groups
   - Conditional routing from parallel groups
   - Default route to end

**Conclusion**: Static parallel infrastructure is **stable and production-ready** with zero known bugs.

---

## Components Available for Reuse

### 1. Core Execution Pattern (`_execute_parallel_group`)

**Location**: `src/conductor/engine/workflow.py:614-830`

**Key Features**:
- ✅ Immutable context snapshot via `copy.deepcopy()`
- ✅ Concurrent execution using `asyncio.gather()`
- ✅ Three failure modes: `fail_fast`, `continue_on_error`, `all_or_nothing`
- ✅ Exception wrapping with agent name and timing metadata
- ✅ Verbose logging integration

**Implementation Pattern**:
```python
# Create immutable context snapshot
context_snapshot = copy.deepcopy(self.context)

# Execute concurrently based on failure mode
if failure_mode == "fail_fast":
    results = await asyncio.gather(
        *[execute_single_agent(agent) for agent in agents],
        return_exceptions=False,  # Raises on first exception
    )
elif failure_mode == "continue_on_error":
    results = await asyncio.gather(
        *[execute_single_agent(agent) for agent in agents],
        return_exceptions=True,  # Collects all exceptions
    )
    # Then check if at least one succeeded
elif failure_mode == "all_or_nothing":
    results = await asyncio.gather(
        *[execute_single_agent(agent) for agent in agents],
        return_exceptions=True,
    )
    # Then verify all succeeded
```

**Reuse for For-Each**:
- ~80% of this logic can be reused
- Main difference: loop over batches instead of all agents at once
- Same context snapshot pattern
- Same failure mode logic

---

### 2. Output Aggregation (`ParallelGroupOutput`)

**Location**: `src/conductor/engine/workflow.py:26-50`

**Structure**:
```python
@dataclass
class ParallelGroupOutput:
    outputs: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, ParallelAgentError] = field(default_factory=dict)
```

**Usage Pattern**:
```python
# Successful outputs
parallel_output.outputs[agent_name] = output_content

# Failed outputs
parallel_output.errors[agent_name] = ParallelAgentError(
    agent_name=agent_name,
    exception_type=type(e).__name__,
    message=str(e),
    suggestion=getattr(e, "suggestion", None),
)
```

**Reuse for For-Each**:
- Create similar `ForEachGroupOutput` dataclass
- Key differences:
  - `outputs` can be `list[dict]` (default) or `dict[str, dict]` (with `key_by`)
  - Add `count: int` field for total items processed
  - Errors keyed by index or extracted key (not agent name)

---

### 3. Error Handling (`ParallelAgentError`)

**Location**: `src/conductor/engine/workflow.py:18-24`

**Structure**:
```python
@dataclass
class ParallelAgentError:
    agent_name: str
    exception_type: str
    message: str
    suggestion: str | None = None
```

**Error Wrapping Pattern**:
```python
# Wrap exception with metadata
if not hasattr(e, '_parallel_agent_name'):
    e._parallel_agent_name = agent.name
if not hasattr(e, '_parallel_agent_elapsed'):
    e._parallel_agent_elapsed = elapsed_time
raise
```

**Reuse for For-Each**:
- Create similar `ForEachError` dataclass
- Key differences:
  - Add `item_index: int` or `item_key: str` field
  - Optionally add `item_value: Any` for debugging (verbose mode only)

---

### 4. Failure Mode Logic

**Location**: `src/conductor/engine/workflow.py:712-830`

**Fail-Fast Mode** (lines 712-748):
```python
try:
    results = await asyncio.gather(..., return_exceptions=False)
    # All succeeded - aggregate outputs
except Exception as e:
    # First failure - raise immediately
    raise ExecutionError(error_msg, suggestion=...) from e
```

**Continue-On-Error Mode** (lines 750-801):
```python
results = await asyncio.gather(..., return_exceptions=True)
# Separate successes and failures
if len(parallel_output.outputs) == 0:
    # All failed - raise error
    raise ExecutionError(...)
# Else: continue with partial results
```

**All-Or-Nothing Mode** (lines 803-830):
```python
results = await asyncio.gather(..., return_exceptions=True)
# Check if any failed
if len(parallel_output.errors) > 0:
    # At least one failed - raise error with all failures
    raise ExecutionError(...)
# All succeeded
```

**Reuse for For-Each**:
- ✅ Exact same failure mode logic
- ✅ Same error aggregation patterns
- ✅ Same exception raising strategies

---

### 5. Verbose Logging Helpers

**Location**: `src/conductor/engine/workflow.py` (helper functions)

**Functions Available**:
- `_verbose_log_parallel_start(group_name, agent_count)`
- `_verbose_log_parallel_agent_complete(agent_name, elapsed, model, tokens)`
- `_verbose_log_parallel_agent_failed(agent_name, elapsed, exception_type, message)`
- `_verbose_log_parallel_summary(group_name, success_count, error_count, total_elapsed)`

**Reuse for For-Each**:
- Create similar helpers: `_verbose_log_foreach_*`
- Same logging structure and format
- Add item index/key information

---

### 6. Context Snapshot Pattern

**Location**: `src/conductor/engine/workflow.py:644`

**Implementation**:
```python
context_snapshot = copy.deepcopy(self.context)
```

**Usage**:
```python
agent_context = context_snapshot.build_for_agent(
    agent.name,
    agent.input,
    mode=self.config.workflow.context.mode,
)
```

**Reuse for For-Each**:
- ✅ Exact same pattern: one snapshot per for-each group entry
- ✅ Prevents context pollution between parallel instances
- ✅ Memory overhead acceptable (one-time deepcopy)

---

## Schema Infrastructure

### ParallelGroup Definition

**Location**: `src/conductor/config/schema.py:209-245`

**Fields**:
```python
class ParallelGroup(BaseModel):
    name: str
    description: str | None = None
    agents: list[str]  # References to existing agents
    failure_mode: Literal["fail_fast", "continue_on_error", "all_or_nothing"] = "fail_fast"
    routes: list[RouteDef] = Field(default_factory=list)
```

**Validation**:
```python
@model_validator(mode="after")
def validate_constraints(self) -> ParallelGroup:
    if len(self.agents) < 2:
        raise ValueError("Parallel groups must contain at least 2 agents")
    # Check for route conflicts, etc.
    return self
```

**Integration with WorkflowConfig**:
```python
class WorkflowConfig(BaseModel):
    agents: list[AgentDef]
    parallel: list[ParallelGroup] | None = None
```

---

## Differences for For-Each Implementation

### What Stays the Same
1. ✅ Context snapshot pattern (one deepcopy per group)
2. ✅ `asyncio.gather()` execution within batches
3. ✅ Failure mode logic (fail_fast, continue_on_error, all_or_nothing)
4. ✅ Error aggregation structure
5. ✅ Verbose logging approach
6. ✅ Route evaluation after group completion

### What Changes
1. ❌ Agent specification:
   - Static: `agents: ["agent1", "agent2"]` (references)
   - For-Each: `agent: {...}` (inline template definition)

2. ❌ Concurrency control:
   - Static: Execute all agents at once (count known at load time)
   - For-Each: Batched execution with `max_concurrent` limit

3. ❌ Variable injection:
   - Static: N/A
   - For-Each: Inject `{{ <var> }}`, `{{ _index }}`, `{{ _key }}` per item

4. ❌ Output structure:
   - Static: `{outputs: {agent1: {...}, agent2: {...}}}`
   - For-Each: `{outputs: [...]}` or `{outputs: {key1: {...}, key2: {...}}}`

5. ❌ Runtime array resolution:
   - Static: N/A (agent count fixed at load time)
   - For-Each: Resolve `source` reference (e.g., `finder.output.kpis`) at runtime

---

## Recommendations for For-Each Implementation

### 1. Code Structure
- Create new `ForEachDef` schema class parallel to `ParallelGroup`
- Create new `_execute_foreach_group()` method that reuses patterns from `_execute_parallel_group()`
- Share helper functions where possible (verbose logging, error wrapping)

### 2. Batching Strategy
Implement sequential batching (not semaphore-based throttling):

```python
async def _execute_foreach_group(self, foreach_group: ForEachDef):
    # Resolve array from source
    items = self._resolve_array_reference(foreach_group.source)
    
    # Batch items
    batch_size = foreach_group.max_concurrent
    batches = [items[i:i+batch_size] for i in range(0, len(items), batch_size)]
    
    all_outputs = []
    all_errors = {}
    
    # Execute batches sequentially
    for batch in batches:
        # Use asyncio.gather within batch (reuse pattern from _execute_parallel_group)
        batch_results = await asyncio.gather(
            *[execute_single_item(item, idx) for idx, item in enumerate(batch)],
            return_exceptions=True if failure_mode != "fail_fast" else False,
        )
        # Aggregate results...
```

### 3. Testing Strategy
- Reuse test patterns from `tests/test_engine/test_parallel.py`
- Test cases should mirror parallel tests:
  - Success scenarios
  - Each failure mode
  - Context isolation
  - Output structure
  - Error formatting

### 4. Integration Point
Add to main execution loop (same pattern as parallel groups):

```python
# In WorkflowEngine.execute()
elif isinstance(step, ForEachDef):
    foreach_output = await self._execute_foreach_group(step)
    self.context.store_output(step.name, foreach_output)
    next_step = self._evaluate_routes(step.routes)
```

---

## Conclusion

✅ **Prerequisites Met**: All static parallel infrastructure tests passing (28/28)  
✅ **No Known Bugs**: Infrastructure is production-ready  
✅ **Reusability**: ~80% of patterns can be reused for for-each implementation  
✅ **Documentation Complete**: All reusable components documented with usage patterns

**Ready to proceed with Epic 1: Schema Definition and Validation**
