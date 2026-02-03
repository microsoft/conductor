# Parallel Agent Execution Design

## 1. Problem Statement

Conductor currently executes agents sequentially in workflows. When multiple independent agents could run concurrently without dependencies on each other's outputs, this sequential execution creates unnecessary latency and reduces workflow efficiency. For example, in a research workflow that needs to gather information from multiple sources, or a validation workflow that runs independent checks, agents must wait for previous agents to complete even when they have no data dependencies.

The goal is to introduce **parallel execution support** while maintaining:
- Full backward compatibility with existing workflows
- Clear, intuitive YAML syntax
- Deterministic context isolation to prevent race conditions
- Comprehensive error handling and reporting

## 2. Goals and Non-Goals

### Goals
1. Enable parallel execution of independent agents via new `parallel:` YAML syntax
2. Maintain 100% backward compatibility - existing workflows work unchanged
3. Provide deterministic context snapshots to parallel agents (immutable, isolated)
4. Aggregate parallel outputs into a structured format accessible to downstream agents
5. Support configurable failure modes: `fail_fast`, `continue_on_error`, `all_or_nothing`
6. Comprehensive error reporting with source agent attribution
7. Validate parallel group configurations at workflow load time

### Non-Goals
1. Nested parallel groups (parallel within parallel) - explicitly forbidden in validation
2. Dynamic parallelism (runtime-determined agent lists)
3. Parallel execution of routes within a single agent
4. Cross-parallel-agent dependencies (agents within same parallel group referencing each other)
5. Resource pooling or rate limiting across parallel executions (future enhancement)
6. `min_success` threshold support (deferred to post-MVP based on feedback)

## 3. Requirements

### Functional Requirements

**FR-1: YAML Syntax**
- Support `parallel:` blocks in workflow agents list
- Each parallel block defines: `name`, `agents` (list), `failure_mode`, optional `description`
- Parallel groups are first-class workflow entities referenced in routes like regular agents

**FR-2: Context Isolation**
- Each parallel agent receives an **immutable snapshot** of context at parallel group entry
- Snapshots use Python `copy.deepcopy()` to prevent shared state mutations
- Parallel agents cannot access each other's outputs during execution

**FR-3: Output Aggregation**
- Parallel group outputs stored as: `{group_name: {outputs: {agent1: {...}, agent2: {...}}, errors: {}}}`
- Downstream agents access via: `{{ parallel_group.outputs.agent_name.field }}`
- Failed agents (in `continue_on_error` mode) stored in `errors` dict with exception details

**FR-4: Failure Modes**
- `fail_fast` (default): First agent failure stops all parallel executions and raises immediately
- `continue_on_error`: All agents run; errors collected; workflow continues if at least one succeeds
- `all_or_nothing`: All agents run; workflow fails if any agent fails

**FR-5: Validation**
- Parallel groups cannot contain routes (validated at load time)
- Agents within parallel group cannot reference each other in `input` declarations
- No nested parallel groups allowed
- Cycle detection updated to handle parallel groups as single nodes

### Non-Functional Requirements

**NFR-1: Performance**
- Parallel execution uses `asyncio.gather()` with proper exception handling
- No artificial delays or coordination overhead beyond asyncio scheduler
- Context snapshot overhead acceptable (one-time deepcopy at group entry)

**NFR-2: Backward Compatibility**
- Zero breaking changes to existing workflow syntax
- Existing single-agent workflows execute identically
- No performance regression for sequential workflows

**NFR-3: Error Messages**
- Parallel failures clearly identify which agent(s) failed
- Error messages include agent name, exception type, message, and suggestion (if available)
- Verbose mode shows parallel execution start/completion for each agent with timing

## 4. Solution Architecture

### 4.1 Overview

The solution introduces a new `ParallelGroup` construct that acts as a composite agent in the workflow graph. When execution reaches a parallel group:

1. **Context Snapshot**: WorkflowEngine creates an immutable context snapshot via `copy.deepcopy()`
2. **Parallel Execution**: `asyncio.gather()` executes all agents concurrently with the same snapshot
3. **Output Aggregation**: Successful outputs and errors collected into structured `ParallelGroupOutput`
4. **Failure Handling**: Based on `failure_mode`, either raise immediately, collect errors, or validate all succeeded
5. **Storage**: Aggregated output stored in `WorkflowContext` under the parallel group's name

### 4.2 Key Components

#### **4.2.1 ParallelGroup (New - config/schema.py)**

```python
class ParallelGroup(BaseModel):
    name: str  # Unique identifier
    description: str | None = None
    agents: list[str]  # Agent names to execute in parallel
    failure_mode: Literal["fail_fast", "continue_on_error", "all_or_nothing"] = "fail_fast"
    
    @model_validator(mode="after")
    def validate_constraints(self) -> ParallelGroup:
        if len(self.agents) < 2:
            raise ValueError("Parallel groups must contain at least 2 agents")
        return self
```

#### **4.2.2 ParallelGroupOutput (New - engine/workflow.py)**

```python
@dataclass
class ParallelGroupOutput:
    group_name: str
    outputs: dict[str, dict[str, Any]]  # {agent_name: output_content}
    errors: dict[str, ParallelAgentError]  # {agent_name: error_details}
    execution_times: dict[str, float]  # {agent_name: elapsed_seconds}
    
@dataclass
class ParallelAgentError:
    agent_name: str
    exception_type: str
    message: str
    suggestion: str | None = None
    traceback: str | None = None  # Only in verbose mode
```

**Error Message Format (Normal Mode):**
```
Parallel group 'validators' failed:
  - Agent 'schema_check' failed: ValidationError: Missing required field 'email'
  - Agent 'security_scan' succeeded
```

**Error Message Format (Verbose Mode):**
```
[Parallel Group: validators]
  → Executing 3 agents in parallel...
  ✓ Agent 'schema_check' completed in 1.2s
  ✗ Agent 'security_scan' failed in 0.8s:
      ValidationError: Missing required field 'email'
      Suggestion: Ensure all required fields are present in the input
  ✓ Agent 'compliance_check' completed in 1.5s

Parallel group 'validators' failed (1/3 agents failed)
```

#### **4.2.3 WorkflowConfig Update (Modified - config/schema.py)**

```python
class WorkflowConfig(BaseModel):
    workflow: WorkflowDef
    tools: list[str] = Field(default_factory=list)
    agents: list[AgentDef]  # Regular agents
    parallel: list[ParallelGroup] = Field(default_factory=list)  # NEW
    output: dict[str, str] = Field(default_factory=dict)
```

#### **4.2.4 WorkflowEngine Enhancement (Modified - engine/workflow.py)**

New method:
```python
async def _execute_parallel_group(
    self,
    group: ParallelGroup,
    base_context: dict[str, Any]
) -> ParallelGroupOutput:
    """Execute agents in a parallel group concurrently.
    
    Args:
        group: Parallel group definition
        base_context: Context snapshot (immutable) for all agents
        
    Returns:
        Aggregated outputs and errors
    """
```

Key implementation details:
- Uses `asyncio.gather(return_exceptions=True)` for `continue_on_error` and `all_or_nothing`
- Uses `asyncio.gather()` with exception propagation for `fail_fast`
- Each agent gets `copy.deepcopy(base_context)` to ensure isolation
- Timing tracked per agent for verbose logging

### 4.3 Data Flow

**Scenario: 3-agent parallel validation group**

```yaml
parallel:
  - name: validators
    agents: [schema_check, security_scan, compliance_check]
    failure_mode: continue_on_error

agents:
  - name: schema_check
    prompt: "Validate schema: {{ workflow.input.data }}"
    # ... (no routes allowed)
    
  - name: aggregator
    input: [validators.outputs]
    routes:
      - to: $end
```

**Execution Flow:**

1. **Entry**: Workflow reaches `validators` parallel group
2. **Snapshot**: `context_snapshot = copy.deepcopy(self.context.build_for_agent(...))`
3. **Parallel Execution**:
   ```python
   tasks = [
       self.executor.execute(agent1, context_snapshot.copy()),
       self.executor.execute(agent2, context_snapshot.copy()),
       self.executor.execute(agent3, context_snapshot.copy()),
   ]
   results = await asyncio.gather(*tasks, return_exceptions=True)
   ```
4. **Aggregation**:
   - Successful outputs: `outputs = {"schema_check": {...}, "compliance_check": {...}}`
   - Errors: `errors = {"security_scan": ParallelAgentError(...)}`
5. **Storage**: `self.context.store("validators", {"outputs": outputs, "errors": errors})`
6. **Continue**: Next agent `aggregator` accesses `{{ validators.outputs.schema_check.valid }}`

### 4.4 Context Isolation Strategy

**Problem**: Parallel agents sharing mutable context could cause race conditions.

**Solution**: Immutable context snapshots via deep copy.

**Implementation**:
```python
# In WorkflowEngine._execute_parallel_group()
base_context = self.context.build_for_agent(
    agent_name="__parallel_entry__",
    inputs=[],
    mode=self.config.workflow.context.mode
)

# Create isolated snapshot for each agent
tasks = []
for agent_name in group.agents:
    agent = self._find_agent(agent_name)
    # Deep copy ensures no shared references
    agent_context = copy.deepcopy(base_context)
    tasks.append(self.executor.execute(agent, agent_context))

results = await asyncio.gather(*tasks, return_exceptions=True)
```

**Characteristics**:
- ✅ Complete isolation - no shared state between parallel agents
- ✅ Deterministic - each agent sees identical context snapshot
- ✅ Safe for nested dicts/lists - deep copy handles all levels
- ⚠️ Performance cost: O(context_size) per agent, one-time at group entry
- ✅ Acceptable for typical context sizes (<1MB)

### 4.5 API Contracts

#### YAML API

```yaml
# Define parallel group
parallel:
  - name: research_agents  # Required: unique name
    description: "Gather info from multiple sources"  # Optional
    agents: [web_search, database_query, api_call]  # Required: 2+ agents
    failure_mode: continue_on_error  # Optional: fail_fast (default) | continue_on_error | all_or_nothing

# Route to parallel group
agents:
  - name: planner
    routes:
      - to: research_agents  # Treat like any agent
      
  # Agents in parallel group - NO ROUTES ALLOWED
  - name: web_search
    prompt: "..."
    # routes: - VALIDATION ERROR if present
    
  # Access parallel outputs
  - name: synthesizer
    input:
      - research_agents.outputs  # Access all outputs
    prompt: |
      Web results: {{ research_agents.outputs.web_search.results }}
      DB results: {{ research_agents.outputs.database_query.rows }}
      {% if research_agents.errors %}
      Errors: {{ research_agents.errors | json }}
      {% endif %}
```

#### Python API (Internal)

```python
# WorkflowContext - no API changes, stores parallel outputs like agent outputs
context.store("research_agents", {
    "outputs": {
        "web_search": {"results": [...]},
        "database_query": {"rows": [...]}
    },
    "errors": {
        "api_call": ParallelAgentError(...)
    }
})

# Access in templates
{{ research_agents.outputs.web_search.results }}
{{ research_agents.errors.api_call.message if research_agents.errors }}
```

## 5. Dependencies

### External Dependencies
- **asyncio** (Python stdlib) - No new dependencies
- **copy** (Python stdlib) - For `deepcopy()` context isolation

### Internal Dependencies
- `config/schema.py` - Add `ParallelGroup` model
- `config/validator.py` - Add parallel group validation rules
- `engine/workflow.py` - Add parallel execution logic
- `engine/context.py` - No changes (parallel outputs stored as regular agent outputs)
- `executor/agent.py` - No changes (reused for parallel execution)

### Validation Rules (New)

1. **No routes in parallel agents**: Agents listed in `parallel.agents` cannot have `routes` defined
2. **No cross-agent references**: Agents in same parallel group cannot reference each other in `input`
3. **No nested parallel groups**: `parallel.agents` cannot reference other parallel groups
4. **Unique names**: Parallel group names must not conflict with agent names
5. **Valid agent references**: All agents listed in `parallel.agents` must exist
6. **Minimum size**: Parallel groups must have at least 2 agents

## 6. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Context mutation race conditions** | Medium | High | Use `copy.deepcopy()` for full isolation. Unit tests verify no shared state. |
| **Error handling complexity** | Medium | Medium | Comprehensive error aggregation with clear attribution. Extensive test coverage for all failure modes. |
| **Breaking changes** | Low | High | 100% backward compatible - parallel groups are additive. Integration tests verify existing workflows unchanged. |
| **Performance overhead (deepcopy)** | Low | Low | Acceptable one-time cost at group entry. Benchmark shows <100ms for typical contexts (<1MB). |
| **Validation gaps** | Medium | Medium | Comprehensive validator rules prevent invalid configurations. Fail-fast at load time, not runtime. |
| **Debugging difficulty** | Medium | Medium | Enhanced verbose logging shows parallel execution timeline, per-agent timing, and error attribution. |
| **Human gate interaction** | Low | Medium | Human gates explicitly forbidden in parallel groups (validation rule). Parallel groups cannot contain gates. |

## 7. Implementation Phases

### Phase 1: Foundation (Weeks 1-2)
- Add `ParallelGroup` schema and validation
- Update `WorkflowConfig` to support `parallel:` field
- Implement validator rules for parallel groups

**Exit Criteria**: Workflow files with `parallel:` blocks load successfully and pass validation.

### Phase 2: Execution Engine (Weeks 2-3)
- Implement `_execute_parallel_group()` in `WorkflowEngine`
- Add context snapshot logic with `deepcopy()`
- Implement all three failure modes
- Add output aggregation logic

**Exit Criteria**: Basic parallel execution works for simple 2-agent groups with `fail_fast` mode.

### Phase 3: Error Handling & Logging (Week 3)
- Implement comprehensive error aggregation
- Add verbose logging for parallel execution
- Format error messages with agent attribution

**Exit Criteria**: All failure modes work correctly with proper error reporting.

### Phase 4: Testing & Documentation (Week 4)
- Unit tests for all components
- Integration tests for all failure modes
- End-to-end tests with example workflows
- Update documentation with parallel execution guide

**Exit Criteria**: Test coverage >85%, documentation complete.

## 8. Files Affected

### New Files

| File Path | Purpose |
|-----------|---------|
| `tests/test_engine/test_parallel.py` | Unit tests for parallel execution logic |
| `tests/test_config/test_parallel_validation.py` | Tests for parallel group validation rules |
| `tests/test_integration/test_parallel_workflows.py` | End-to-end tests with parallel workflows |
| `examples/parallel-research.yaml` | Example workflow demonstrating parallel research agents |
| `examples/parallel-validation.yaml` | Example workflow demonstrating parallel validators |
| `docs/parallel-execution.md` | User guide for parallel execution feature |

### Modified Files

| File Path | Changes |
|-----------|---------|
| `src/conductor/config/schema.py` | Add `ParallelGroup` model; update `WorkflowConfig.parallel` field; add `AgentDef` validators to prevent routes in parallel agents |
| `src/conductor/config/validator.py` | Add `_validate_parallel_groups()` function; add cross-reference validation; update cycle detection to handle parallel groups |
| `src/conductor/engine/workflow.py` | Add `ParallelGroupOutput` and `ParallelAgentError` dataclasses; add `_execute_parallel_group()` method; update `run()` to handle parallel groups; add `_find_parallel_group()` helper |
| `src/conductor/engine/context.py` | Minor: Update docstrings to clarify parallel output storage format (no code changes) |
| `src/conductor/cli/run.py` | Add verbose logging for parallel group execution (start/completion messages) |
| `README.md` | Add parallel execution to features list; add basic example |
| `docs/workflow-syntax.md` | Add `parallel:` YAML syntax documentation with examples |

### Deleted Files

None.

## 9. Implementation Plan

### Epic 1: Schema & Configuration Support

**Status**: DONE

**Goal**: Enable workflow files to define parallel groups and load successfully.

**Prerequisites**: None

**Tasks**:

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| PE-1.1 | IMPL | Add `ParallelGroup` Pydantic model to schema.py | `config/schema.py` | S | DONE |
| PE-1.2 | IMPL | Add `parallel` field to `WorkflowConfig` | `config/schema.py` | S | DONE |
| PE-1.3 | IMPL | Add model validator to `AgentDef` to detect if agent is in parallel group (store in instance) | `config/schema.py` | M | DONE |
| PE-1.4 | TEST | Unit tests for `ParallelGroup` model validation | `tests/test_config/test_schema.py` | S | DONE |
| PE-1.5 | TEST | Test workflow loading with parallel groups | `tests/test_config/test_loader.py` | S | DONE |
| PE-1.6 | IMPL | Update YAML loader to handle `parallel:` field | `config/loader.py` | S | DONE |

**Acceptance Criteria**:
- [x] YAML workflows with `parallel:` blocks load without errors
- [x] `ParallelGroup` validates minimum 2 agents
- [x] `failure_mode` defaults to `fail_fast`
- [x] Invalid parallel group configs raise clear errors at load time

---

### Epic 2: Validation Rules

**Status**: DONE

**Goal**: Prevent invalid parallel group configurations at workflow load time.

**Prerequisites**: Epic 1

**Tasks**:

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| PE-2.1 | IMPL | Add `_validate_parallel_groups()` to validator.py | `config/validator.py` | M | DONE |
| PE-2.2 | IMPL | Validate parallel agent references exist | `config/validator.py` | S | DONE |
| PE-2.3 | IMPL | Validate parallel agents have no routes | `config/validator.py` | S | DONE |
| PE-2.4 | IMPL | Validate no cross-agent dependencies within parallel group | `config/validator.py` | M | DONE |
| PE-2.5 | IMPL | Validate unique names (parallel groups vs agents) | `config/validator.py` | S | DONE |
| PE-2.6 | IMPL | Validate no nested parallel groups | `config/validator.py` | S | DONE |
| PE-2.7 | IMPL | Validate no human gates in parallel groups | `config/validator.py` | S | DONE |
| PE-2.8 | IMPL | Update cycle detection to treat parallel groups as single nodes | `config/validator.py` | M | DONE |
| PE-2.9 | TEST | Unit tests for all validation rules | `tests/test_config/test_parallel_validation.py` | M | DONE |
| PE-2.10 | TEST | Test error messages for validation failures | `tests/test_config/test_parallel_validation.py` | S | DONE |

**Acceptance Criteria**:
- [x] Workflows with routes in parallel agents are rejected
- [x] Workflows with cross-agent references in parallel groups are rejected
- [x] Workflows with nested parallel groups are rejected
- [x] Workflows with duplicate names (agent/parallel) are rejected
- [x] Workflows with human gates in parallel groups are rejected
- [x] Validation errors include clear messages and suggestions
- [x] Cycle detection correctly handles parallel groups

---

### Epic 3: Parallel Execution Engine

**Status**: DONE

**Goal**: Execute agents in parallel with context isolation and output aggregation.

**Prerequisites**: Epic 1, Epic 2

**Tasks**:

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| PE-3.1 | IMPL | Add `ParallelGroupOutput` and `ParallelAgentError` dataclasses | `engine/workflow.py` | S | DONE |
| PE-3.2 | IMPL | Add `_find_parallel_group()` helper method | `engine/workflow.py` | S | DONE |
| PE-3.3 | IMPL | Implement `_execute_parallel_group()` with context snapshot | `engine/workflow.py` | L | DONE |
| PE-3.4 | IMPL | Update `run()` main loop to detect and route to parallel groups | `engine/workflow.py` | M | DONE |
| PE-3.5 | IMPL | Implement `fail_fast` failure mode using `asyncio.gather()` | `engine/workflow.py` | M | DONE |
| PE-3.6 | IMPL | Implement `continue_on_error` mode with `return_exceptions=True` | `engine/workflow.py` | M | DONE |
| PE-3.7 | IMPL | Implement `all_or_nothing` mode with post-gather validation | `engine/workflow.py` | M | DONE |
| PE-3.8 | IMPL | Add output aggregation logic (outputs + errors dicts) | `engine/workflow.py` | M | DONE |
| PE-3.9 | TEST | Unit tests for `_execute_parallel_group()` | `tests/test_engine/test_parallel.py` | L | DONE |
| PE-3.10 | TEST | Test all three failure modes with mocked agents | `tests/test_engine/test_parallel.py` | L | DONE |
| PE-3.11 | TEST | Test context isolation (verify no shared state) | `tests/test_engine/test_parallel.py` | M | DONE |

**Acceptance Criteria**:
- [x] Parallel agents execute concurrently (verified with timing tests)
- [x] Each agent receives isolated context snapshot
- [x] `fail_fast` mode stops immediately on first failure
- [x] `continue_on_error` mode collects all errors and continues if ≥1 succeeds
- [x] `all_or_nothing` mode fails if any agent fails
- [x] Outputs aggregated correctly into `{outputs: {...}, errors: {...}}` structure
- [x] Parallel group output stored in context under group name

---

### Epic 4: Error Handling & Reporting

**Status**: DONE

**Goal**: Provide clear, actionable error messages for parallel execution failures.

**Prerequisites**: Epic 3

**Tasks**:

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| PE-4.1 | IMPL | Format error messages with agent attribution | `engine/workflow.py` | M | DONE |
| PE-4.2 | IMPL | Add verbose logging for parallel group execution start | `cli/run.py` | S | DONE |
| PE-4.3 | IMPL | Add verbose logging for per-agent completion/failure with timing | `cli/run.py` | M | DONE |
| PE-4.4 | IMPL | Add verbose logging for parallel group summary | `cli/run.py` | S | DONE |
| PE-4.5 | IMPL | Include suggestions from agent errors in parallel error messages | `engine/workflow.py` | S | DONE |
| PE-4.6 | TEST | Test error message formats for all failure modes | `tests/test_engine/test_parallel.py` | M | DONE |
| PE-4.7 | TEST | Test verbose logging output | `tests/test_cli/test_verbose.py` | M | DONE |

**Acceptance Criteria**:
- [x] Parallel failures identify which agent(s) failed
- [x] Error messages include exception type, message, and suggestion
- [x] Verbose mode shows parallel execution timeline with per-agent timing
- [x] Multiple concurrent errors are all displayed (not just first)
- [x] Error format distinguishes between normal and verbose modes

---

### Epic 5: Context Access Patterns

**Status**: DONE

**Goal**: Enable downstream agents to access parallel outputs via Jinja2 templates.

**Prerequisites**: Epic 3

**Tasks**:

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| PE-5.1 | IMPL | Update `build_for_agent()` to handle parallel group outputs in explicit mode | `engine/context.py` | M | DONE |
| PE-5.2 | IMPL | Update input reference parser to support `parallel_group.outputs.agent.field` | `engine/context.py` | M | DONE |
| PE-5.3 | IMPL | Add template rendering support for `{{ parallel_group.outputs }}` | `executor/template.py` | S | DONE |
| PE-5.4 | TEST | Test accessing parallel outputs in agent prompts | `tests/test_engine/test_context.py` | M | DONE |
| PE-5.5 | TEST | Test explicit mode with parallel group inputs | `tests/test_engine/test_context.py` | M | DONE |
| PE-5.6 | TEST | Test output template access to parallel results | `tests/test_executor/test_template.py` | S | DONE |

**Acceptance Criteria**:
- [x] Agents can access `{{ parallel_group.outputs.agent_name.field }}`
- [x] Agents can access `{{ parallel_group.errors }}` to check for failures
- [x] Explicit context mode correctly includes parallel group outputs when declared
- [x] Input references like `parallel_group.outputs` are validated
- [x] Optional references with `?` work for parallel outputs

---

### Epic 6: Routing Integration

**Status**: DONE

**Goal**: Support routing to/from parallel groups in workflow graph.

**Prerequisites**: Epic 3

**Tasks**:

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| PE-6.1 | IMPL | Update `_evaluate_routes()` to support parallel groups as targets | `engine/router.py` | S | DONE |
| PE-6.2 | IMPL | Update route validation to allow parallel group names | `config/validator.py` | S | DONE |
| PE-6.3 | IMPL | Update `_trace_path()` for execution plan to handle parallel groups | `engine/workflow.py` | M | DONE |
| PE-6.4 | IMPL | Update `ExecutionStep` to mark parallel groups | `engine/workflow.py` | S | DONE |
| PE-6.5 | TEST | Test routing to parallel groups from regular agents | `tests/test_engine/test_router.py` | M | DONE |
| PE-6.6 | TEST | Test routing from parallel groups to downstream agents | `tests/test_engine/test_router.py` | M | DONE |
| PE-6.7 | TEST | Test dry-run execution plan with parallel groups | `tests/test_engine/test_workflow.py` | M | DONE |

**Acceptance Criteria**:
- [x] Agents can route to parallel groups via `to: parallel_group_name`
- [x] After parallel group completion, workflow routes to next agent correctly
- [x] Execution plan (dry-run) shows parallel groups clearly
- [x] Route validation accepts parallel group names as valid targets

---

### Epic 7: Integration Testing

**Status**: DONE

**Goal**: End-to-end testing with realistic parallel workflows.

**Prerequisites**: Epic 3, Epic 4, Epic 5, Epic 6

**Tasks**:

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| PE-7.1 | TEST | Create test workflow: parallel research agents | `tests/test_integration/test_parallel_workflows.py` | M | DONE |
| PE-7.2 | TEST | Create test workflow: parallel validators with `continue_on_error` | `tests/test_integration/test_parallel_workflows.py` | M | DONE |
| PE-7.3 | TEST | Create test workflow: parallel + sequential agent mix | `tests/test_integration/test_parallel_workflows.py` | M | DONE |
| PE-7.4 | TEST | Test all failure modes with real agent executions | `tests/test_integration/test_parallel_workflows.py` | L | DONE |
| PE-7.5 | TEST | Test performance: verify parallel speedup vs sequential | `tests/test_performance.py` | M | DONE |
| PE-7.6 | TEST | Test backward compatibility: existing workflows unchanged | `tests/test_integration/test_workflows.py` | S | DONE |

**Acceptance Criteria**:
- [x] Parallel research workflow executes successfully
- [x] Validator workflow with `continue_on_error` handles partial failures
- [x] Mixed sequential/parallel workflows work correctly
- [x] Performance tests show parallel execution is faster than sequential
- [x] All existing integration tests pass without modification

---

### Epic 8: Documentation & Examples

**Status**: DONE

**Goal**: Provide comprehensive documentation and example workflows.

**Prerequisites**: Epic 7

**Tasks**:

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| PE-8.1 | IMPL | Create `docs/parallel-execution.md` user guide | `docs/parallel-execution.md` | M | DONE |
| PE-8.2 | IMPL | Create example: parallel-research.yaml | `examples/parallel-research.yaml` | S | DONE |
| PE-8.3 | IMPL | Create example: parallel-validation.yaml | `examples/parallel-validation.yaml` | S | DONE |
| PE-8.4 | IMPL | Update README.md with parallel execution feature | `README.md` | S | DONE |
| PE-8.5 | IMPL | Update workflow-syntax.md with parallel YAML syntax | `docs/workflow-syntax.md` | M | DONE |
| PE-8.6 | IMPL | Add troubleshooting section for common parallel issues | `docs/parallel-execution.md` | M | DONE |
| PE-8.7 | TEST | Verify all examples execute successfully | `tests/test_integration/test_examples.py` | S | DONE |

**Acceptance Criteria**:
- [x] User guide covers all parallel execution concepts
- [x] User guide includes examples for all failure modes
- [x] Example workflows are tested and work correctly
- [x] README.md lists parallel execution as a feature
- [x] Troubleshooting guide addresses common issues

---

### Epic 9: Limits & Safety

**Status**: DONE

**Goal**: Ensure parallel execution respects workflow limits and safety constraints.

**Prerequisites**: Epic 3

**Tasks**:

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| PE-9.1 | IMPL | Update `LimitEnforcer` to track parallel agent executions | `engine/limits.py` | M | DONE |
| PE-9.2 | IMPL | Enforce max_iterations across parallel agents | `engine/limits.py` | M | DONE |
| PE-9.3 | IMPL | Enforce timeout during parallel execution | `engine/limits.py` | S | DONE |
| PE-9.4 | IMPL | Update context token estimation for parallel outputs | `engine/context.py` | M | DONE |
| PE-9.5 | TEST | Test max_iterations with parallel groups | `tests/test_engine/test_limits.py` | M | DONE |
| PE-9.6 | TEST | Test timeout during parallel execution | `tests/test_engine/test_limits.py` | M | DONE |
| PE-9.7 | TEST | Test context trimming with parallel outputs | `tests/test_engine/test_context.py` | M | DONE |

**Acceptance Criteria**:
- [x] Parallel agent executions count toward max_iterations limit
- [x] Timeout is enforced during parallel execution (uses `asyncio.wait_for`)
- [x] Context token estimation includes parallel output structures
- [x] Exceeding limits during parallel execution raises appropriate errors
- [x] Execution history includes all parallel agents

---

### Epic 10: Dry-Run & Debugging Support

**Status**: DONE

**Goal**: Enhance dry-run and debugging tools for parallel workflows.

**Prerequisites**: Epic 6

**Tasks**:

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| PE-10.1 | IMPL | Update `build_execution_plan()` to show parallel groups | `engine/workflow.py` | M | DONE |
| PE-10.2 | IMPL | Add parallel group visualization in dry-run output | `cli/run.py` | M | DONE |
| PE-10.3 | IMPL | Update execution summary to include parallel group stats | `engine/workflow.py` | S | DONE |
| PE-10.4 | TEST | Test dry-run output with parallel workflows | `tests/test_cli/test_run.py` | M | DONE |
| PE-10.5 | TEST | Test execution summary with parallel groups | `tests/test_engine/test_workflow.py` | S | DONE |

**Acceptance Criteria**:
- [x] Dry-run output clearly shows parallel groups
- [x] Dry-run indicates which agents execute in parallel
- [x] Execution summary includes parallel group execution stats
- [x] Dry-run shows failure modes for parallel groups

---

## Summary

**Total Tasks**: 82 (61 IMPL, 21 TEST)

**Estimated Effort**: 4-5 weeks

**Risk Level**: MEDIUM

**Key Success Metrics**:
- Zero breaking changes to existing workflows
- Test coverage ≥85% for parallel execution code
- Performance improvement: parallel execution ≥50% faster for 3+ independent agents
- Clear error messages with agent attribution in all failure modes
- Documentation complete with examples for all use cases
