# Solution Design: Script Execution Steps

**Revision:** 2.0 — Revised per technical review feedback  
**Source Spec:** `docs/projects/planned-features.md` § 4  
**Status:** DONE
**Completed:** 2026-02-24

---

## 1. Problem Statement

Conductor workflows currently support only two step types: `agent` (LLM-powered) and `human_gate` (interactive). Many real-world workflows require deterministic shell command execution—running tests, linting, building artifacts, fetching data—before, after, or between agent steps. Today, users must perform these operations externally and pipe results in via workflow inputs, breaking the workflow's orchestration model.

Adding `type: script` as a first-class workflow step type allows shell commands to participate directly in the execution graph: their stdout is captured as context, their exit codes drive conditional routing, and they share the same YAML syntax and routing semantics as agent steps.

---

## 2. Goals and Non-Goals

### Goals

- **G1:** Users can define `type: script` steps in YAML with `command`, `args`, `env`, `working_dir`, and `timeout` fields.
- **G2:** Script stdout is captured as text and stored in workflow context identically to agent outputs (accessible as `{{ script_name.output.stdout }}`).
- **G3:** Script `exit_code` is available in route `when` conditions for conditional branching.
- **G4:** `command` and `args` support Jinja2 template rendering for context injection.
- **G5:** Script steps are validated at load time: `command` required when `type == "script"`; `prompt`, `provider`, `model` forbidden.
- **G6:** Script steps participate in the existing iteration limit, timeout, and context accumulation systems.
- **G7:** Script stderr is captured alongside stdout for debugging visibility.

### Non-Goals

- **NG1:** Interactive/stdin-driven scripts (no stdin piping).
- **NG2:** Streaming stdout in real-time to the console (captured in bulk after completion).
- **NG3:** Script steps inside `parallel` groups or `for_each` groups (future work; validation rejects this in v1).
- **NG4:** Shell expansion / piping (`command` is exec'd directly, not via `sh -c`). Users who need shell features can use `command: sh` with `args: ["-c", "pipeline | here"]`.
- **NG5:** JSON-parsing of script output (stdout is always stored as a raw string).

---

## 3. Requirements

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `AgentDef.type` accepts `"script"` as a valid literal value. |
| FR-2 | When `type == "script"`, `command` field is required and must be a non-empty string. |
| FR-3 | When `type == "script"`, `prompt`, `provider`, `model`, `tools`, `output`, `system_prompt`, and `options` fields must not be set (validation error if present). |
| FR-4 | `args` field is a list of strings, default empty. Each element supports Jinja2 templating. |
| FR-5 | `env` field is a dict of string→string, default empty. Values support `${VAR:-default}` env var syntax via the existing config loader (resolved before reaching the executor). |
| FR-6 | `working_dir` field is an optional string specifying the working directory for the subprocess. |
| FR-7 | `timeout` field is an optional positive integer specifying per-script timeout in seconds (separate from workflow-level timeout). |
| FR-8 | `ScriptExecutor` runs the command via `asyncio.create_subprocess_exec()` and captures stdout and stderr. |
| FR-9 | Script output is stored in context as `{"stdout": <text>, "stderr": <text>, "exit_code": <int>}`. |
| FR-10 | Route conditions can reference `exit_code` via simpleeval (`exit_code == 0`, no braces) or Jinja2 (`{{ output.exit_code == 0 }}`). See §4.4 for details. |
| FR-11 | `command` field supports Jinja2 template rendering with workflow context. |
| FR-12 | Script steps count as one iteration toward `max_iterations`. |
| FR-13 | Script steps respect workflow-level `timeout_seconds` enforcement. |
| FR-14 | Validator rejects script steps in `parallel` groups and `for_each` inline agents. |

### Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR-1 | Script execution must not block the event loop (achieved via `asyncio.create_subprocess_exec`). |
| NFR-2 | Per-script timeout enforced via `asyncio.wait_for()` with `asyncio.TimeoutError` conversion. |
| NFR-3 | Script stderr is logged at verbose level for debugging, not injected into agent prompts. |
| NFR-4 | No new external dependencies required. |

---

## 4. Solution Architecture

### 4.1 Overview

The solution adds a third execution path to the main workflow loop alongside the existing `agent` and `human_gate` paths. When the engine encounters a step with `type == "script"`, it delegates to a new `ScriptExecutor` class instead of the `AgentExecutor`. The script executor renders the command/args templates, spawns the subprocess, captures output, and returns a structured result that the engine stores in context.

#### 4.1.1 Design Rationale: Why Extend `AgentDef`?

Several approaches were considered for adding script execution:

| Approach | Pros | Cons |
|----------|------|------|
| **A: Extend `AgentDef` with `type: script`** (chosen) | Reuses existing routing, context, and iteration infrastructure. Minimal engine changes. Scripts participate in the execution graph like any other step. | Overloads `AgentDef` with fields irrelevant to LLM agents. |
| **B: Separate `scripts:` top-level key** | Clean separation of concerns. No field pollution on `AgentDef`. | Requires duplicating routing, context storage, and iteration tracking logic. Major engine refactor. |
| **C: Shell mode via `sh -c` as default** | More familiar to shell users. Supports pipes/redirects natively. | Security risk from implicit shell interpretation. Inconsistent with `MCPServerDef` exec pattern. Cross-platform issues (Windows `cmd.exe` vs Unix `sh`). |

**Decision:** Approach A was chosen because the workflow engine's main loop already dispatches on `agent.type` (see `human_gate` at line 699), the routing and context systems work unchanged with arbitrary output dicts, and the `MCPServerDef` model (lines 423–467) establishes a precedent for `command`/`args`/`env` fields. The field pollution is mitigated by the model validator that enforces mutual exclusivity between script and agent fields.

Approach C (shell mode) is available to users who need it via `command: sh` with `args: ["-c", "pipeline | here"]`, without making it the default and inheriting its security risks.

### 4.2 Key Components

#### 4.2.1 Schema Extension (`src/conductor/config/schema.py`)

**Changes to `AgentDef`:**

```python
class AgentDef(BaseModel):
    type: Literal["agent", "human_gate", "script"] | None = None
    
    # New fields for script type
    command: str | None = None
    """Command to execute (required for script type). Supports Jinja2 templating."""
    
    args: list[str] = Field(default_factory=list)
    """Command-line arguments. Each supports Jinja2 templating."""
    
    env: dict[str, str] = Field(default_factory=dict)
    """Environment variables for the subprocess."""
    
    working_dir: str | None = None
    """Working directory for subprocess execution."""
    
    timeout: int | None = None
    """Per-script timeout in seconds."""
```

**Model validator addition:**

```python
@model_validator(mode="after")
def validate_agent_type(self) -> AgentDef:
    if self.type == "human_gate":
        # ... existing validation ...
    elif self.type == "script":
        if not self.command:
            raise ValueError("script agents require 'command'")
        if self.prompt:
            raise ValueError("script agents cannot have 'prompt'")
        if self.provider:
            raise ValueError("script agents cannot have 'provider'")
        if self.model:
            raise ValueError("script agents cannot have 'model'")
        if self.tools is not None:
            raise ValueError("script agents cannot have 'tools'")
        if self.output:
            raise ValueError("script agents cannot have 'output' schema (output is always stdout/stderr/exit_code)")
        if self.system_prompt:
            raise ValueError("script agents cannot have 'system_prompt'")
        if self.options:
            raise ValueError("script agents cannot have 'options'")
    return self
```

The new fields follow the exact same pattern as `MCPServerDef` (lines 423–467 in schema.py): `command: str | None`, `args: list[str]`, `env: dict[str, str]`.

#### 4.2.2 Script Executor (`src/conductor/executor/script.py`)

New file providing `ScriptExecutor`:

```python
@dataclass
class ScriptOutput:
    stdout: str
    stderr: str
    exit_code: int

class ScriptExecutor:
    def __init__(self) -> None:
        self.renderer = TemplateRenderer()
    
    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
    ) -> ScriptOutput:
        """Execute a script step."""
        # 1. Render command and args with Jinja2
        rendered_command = self.renderer.render(agent.command, context)
        rendered_args = [self.renderer.render(arg, context) for arg in agent.args]
        
        # 2. Build environment (merge os.environ + agent.env)
        # Note: ${VAR:-default} patterns in agent.env are already resolved
        # by the config loader (_resolve_env_vars_recursive) during YAML parsing.
        # The executor only needs to merge with the current process environment.
        env = {**os.environ, **agent.env} if agent.env else None
        
        # 3. Create subprocess
        process = await asyncio.create_subprocess_exec(
            rendered_command, *rendered_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=agent.working_dir,
            env=env,
        )
        
        # 4. Wait with timeout
        timeout = agent.timeout  # per-script timeout
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise ExecutionError(
                f"Script '{agent.name}' timed out after {timeout}s",
                agent_name=agent.name,
            )
        
        # 5. Return structured output
        # IMPORTANT: process.returncode is guaranteed non-None after communicate().
        # Do NOT use `process.returncode or 0` — 0 is falsy in Python, so that
        # expression would always return 0, breaking exit_code-based routing.
        return ScriptOutput(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            exit_code=process.returncode,
        )
```

#### 4.2.3 Workflow Engine Integration (`src/conductor/engine/workflow.py`)

In the main `run()` loop (around line 687–778), add a new branch before the regular agent execution:

```python
if agent is not None:
    # Check iteration limit
    await self._check_iteration_with_prompt(current_agent_name)
    iteration = self.limits.current_iteration + 1
    _verbose_log_agent_start(current_agent_name, iteration)
    self._trim_context_if_needed()
    
    if agent.type == "script":
        # Script execution path
        agent_context = self.context.build_for_agent(
            agent.name, agent.input,
            mode=self.config.workflow.context.mode,
        )
        _script_start = _time.time()
        script_output = await self._execute_script(agent, agent_context)
        _script_elapsed = _time.time() - _script_start
        
        # Verbose log
        _verbose_log_agent_complete(agent.name, _script_elapsed)
        
        # Store in context as dict
        output_content = {
            "stdout": script_output.stdout,
            "stderr": script_output.stderr,
            "exit_code": script_output.exit_code,
        }
        self.context.store(agent.name, output_content)
        self.limits.record_execution(agent.name)
        self.limits.check_timeout()
        
        # Evaluate routes with exit_code in output
        route_result = self._evaluate_routes(agent, output_content)
        # ... routing logic (same as agent) ...
        
    elif agent.type == "human_gate":
        # ... existing human_gate logic ...
    else:
        # ... existing agent logic ...
```

A private method `_execute_script()` wraps `ScriptExecutor.execute()` with workflow-level timeout enforcement via `self.limits.wait_for_with_timeout()`.

**Non-zero exit with no routes:** When a script exits with a non-zero code and has no routes defined, the engine defaults to `$end` (line 1762–1764), treating the workflow as complete. This is an intentional design choice: non-zero exit codes are informational, not errors. Script output (including `exit_code`) is stored in context and available in the workflow's final output. Users who need error handling must define explicit routes with `when` conditions. This mirrors how agent steps work—an agent producing unexpected output doesn't fail the workflow unless routes enforce it.

#### 4.2.4 Validation Extension (`src/conductor/config/validator.py`)

Add validation to reject script steps in parallel groups and for_each inline agents:

```python
# In _validate_parallel_groups():
if agent.type == "script":
    errors.append(
        f"Agent '{agent_name}' in parallel group '{pg.name}' is a script step. "
        "Script steps cannot be used in parallel groups."
    )
```

```python
# In validate_workflow_config(), for_each validation:
for for_each_group in config.for_each:
    if for_each_group.agent.type == "script":
        errors.append(
            f"For-each group '{for_each_group.name}' uses a script step as its "
            "inline agent. Script steps cannot be used in for_each groups."
        )
```

### 4.3 Data Flow

```
YAML parsed → AgentDef (type="script") validated by Pydantic
    ↓
WorkflowEngine.run() main loop
    ↓
agent.type == "script" branch
    ↓
Build context (same as agent steps)
    ↓
ScriptExecutor.execute(agent, context)
    ↓
Render command + args via Jinja2 TemplateRenderer
    ↓
asyncio.create_subprocess_exec(command, *args)
    ↓
Capture stdout, stderr, exit_code
    ↓
Store {"stdout": ..., "stderr": ..., "exit_code": ...} in context
    ↓
Router.evaluate() with exit_code in output dict
    ↓
Route to next step or $end
```

### 4.4 Route Condition Syntax for `exit_code`

The Router supports two expression styles. For script steps, `exit_code` routing works as follows:

**simpleeval (bare expression, no braces):**

```yaml
routes:
  - to: success_handler
    when: "exit_code == 0"
  - to: failure_handler
```

This works because `Router._flatten_context()` promotes `output.*` keys to top-level for simpleeval. Since the script output dict is `{"stdout": ..., "stderr": ..., "exit_code": 0}`, the flattened context includes `exit_code` as a top-level name.

**Jinja2 (with `{{ }}` braces):**

```yaml
routes:
  - to: success_handler
    when: "{{ output.exit_code == 0 }}"
  - to: failure_handler
```

The `output` prefix is required in Jinja2 mode because `Router.evaluate()` places the current output dict under the `output` key in the eval context. Using `{{ exit_code == 0 }}` (without `output.`) would raise a Jinja2 `UndefinedError`.

**Recommendation:** Use the simpleeval form (`exit_code == 0`) for simplicity. It is shorter and avoids the `output.` prefix requirement.

### 4.5 Context Access Patterns

After a script step named `run_tests` executes:

| Pattern | Context | Works? |
|---------|---------|--------|
| `{{ run_tests.output.stdout }}` | Agent prompts | ✅ |
| `{{ run_tests.output.stderr }}` | Agent prompts | ✅ |
| `{{ run_tests.output.exit_code }}` | Agent prompts | ✅ |
| `exit_code == 0` | Route `when` (simpleeval) | ✅ |
| `{{ output.exit_code == 0 }}` | Route `when` (Jinja2) | ✅ |
| `{{ exit_code == 0 }}` | Route `when` (Jinja2) | ❌ `UndefinedError` |

**Known limitation — hyphenated agent names:** Agent names containing hyphens (e.g., `run-tests`) cannot be accessed via Jinja2 dot notation because Jinja2 parses `run-tests` as the expression `run` minus `tests`. There is no hyphen-to-underscore normalization in `TemplateRenderer`, `WorkflowContext.store()`, or `build_for_agent()`. This is a pre-existing codebase limitation, not specific to script steps. Users should use underscore-separated names (e.g., `run_tests`) for agents whose output needs to be referenced in templates.

---

## 5. Dependencies

### Internal Dependencies

| Component | Dependency |
|-----------|-----------|
| `ScriptExecutor` | `TemplateRenderer` (Jinja2 rendering for command/args) |
| `ScriptExecutor` | `asyncio` (subprocess management) |
| `ScriptExecutor` | `ExecutionError` from `conductor.exceptions` |
| `WorkflowEngine` | `ScriptExecutor` (new import) |
| `AgentDef` | No new imports (uses existing Pydantic/Literal) |
| Config Loader | No changes needed (existing `_resolve_env_vars_recursive()` already handles `${VAR:-default}` in all YAML strings including `env` values) |

### External Dependencies

**None.** All required functionality (`asyncio.create_subprocess_exec`, `os.environ`) is in the Python standard library. Jinja2 is already a dependency.

---

## 6. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| **Security: arbitrary command execution** | High | High | Document that script steps run with the same permissions as the Conductor process. No sandboxing in v1—this is intentional (same trust model as MCP stdio servers). |
| **Subprocess hangs without timeout** | Medium | High | Require `timeout` field documentation; apply workflow-level `timeout_seconds` as a safety net; `process.kill()` on timeout. |
| **Large stdout fills memory** | Low | Medium | Future: add `max_output_bytes` field. v1: document that very large outputs should be written to files. |
| **Cross-platform path issues** | Medium | Low | `working_dir` is passed directly to subprocess; document that paths should be OS-appropriate. |
| **Template injection in command** | Medium | Medium | The command is rendered from workflow YAML (trusted input), not from user-supplied runtime input. Same trust model as prompt templates. |
| **Breaking schema changes** | Low | High | New fields are all optional with defaults; `type` adds a new literal value to an existing union—fully backward compatible. |
| **Silent success on script failure** | Medium | Medium | Explicitly documented: non-zero exit with no routes defaults to `$end`. Users must define routes for error handling. Documented in §4.2.3 and examples. |

---

## 7. Implementation Phases

### Phase 1: Schema & Validation
- Extend `AgentDef` type literal and add script fields
- Add model validator for script type constraints
- Update cross-reference validator for parallel group and for_each restrictions
- **Exit Criteria:** All schema tests pass; existing workflows unaffected.

### Phase 2: Script Executor
- Create `ScriptExecutor` class with `asyncio.create_subprocess_exec`
- Handle timeout, env merging, output capture
- **Exit Criteria:** Unit tests for executor pass covering success, failure, timeout, env, args.

### Phase 3: Engine Integration
- Add script dispatch branch to `WorkflowEngine.run()` main loop
- Wire up context storage, iteration tracking, route evaluation
- Update `ExecutionStep.agent_type` to recognize `"script"`
- **Exit Criteria:** Integration tests with script workflows pass.

### Phase 4: Documentation & Examples
- Add script step documentation
- Create example workflow YAML
- Update CLI help text if needed
- **Exit Criteria:** Example workflows validate and run correctly.

---

## 8. Files Affected

### New Files

| File Path | Purpose |
|-----------|---------|
| `src/conductor/executor/script.py` | `ScriptExecutor` class and `ScriptOutput` dataclass |
| `tests/test_executor/test_script.py` | Unit tests for `ScriptExecutor` |
| `tests/test_config/test_script_schema.py` | Schema validation tests for script type |
| `tests/test_engine/test_script_workflow.py` | Integration tests for script steps in workflows |
| `examples/script-step.yaml` | Example workflow using script steps |

### Modified Files

| File Path | Changes |
|-----------|---------|
| `src/conductor/config/schema.py` | Extend `AgentDef.type` literal to include `"script"`. Add `command`, `args`, `env`, `working_dir`, `timeout` fields. Add model validator for script constraints. |
| `src/conductor/engine/workflow.py` | Add script execution branch in main `run()` loop (~L687). Import `ScriptExecutor`. Add `_execute_script()` helper method. Update `_get_executor_for_agent` or bypass it for scripts. |
| `src/conductor/config/validator.py` | Add validation in `_validate_parallel_groups()` to reject script steps. Add for_each inline agent validation in `validate_workflow_config()`. Skip tool reference validation for script-type agents. |

### Deleted Files

| File Path | Reason |
|-----------|--------|
| *(none)* | |

---

## 9. Implementation Plan

### Epic 1: Schema Extension & Validation

**Goal:** Extend the `AgentDef` Pydantic model to support `type: script` with proper field validation, and update cross-reference validators.

**Prerequisites:** None

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E1-T1 | IMPL | Extend `AgentDef.type` to `Literal["agent", "human_gate", "script"] \| None` | `src/conductor/config/schema.py` | DONE |
| E1-T2 | IMPL | Add `command: str \| None`, `args: list[str]`, `env: dict[str, str]`, `working_dir: str \| None`, `timeout: int \| None` fields to `AgentDef` with appropriate defaults and field validators | `src/conductor/config/schema.py` | DONE |
| E1-T3 | IMPL | Add model validator logic in `validate_agent_type()`: when `type == "script"`, require `command`, forbid `prompt`/`provider`/`model`/`tools`/`output`/`system_prompt`/`options` | `src/conductor/config/schema.py` | DONE |
| E1-T4 | IMPL | Update `_validate_parallel_groups()` to reject `type == "script"` agents in parallel groups (same pattern as human_gate rejection at line 344-349) | `src/conductor/config/validator.py` | DONE |
| E1-T5 | IMPL | Add validation in `validate_workflow_config()` to reject `type == "script"` in `ForEachDef.agent` inline agents | `src/conductor/config/validator.py` | DONE |
| E1-T6 | IMPL | Skip tool reference validation for script-type agents in `validate_workflow_config()` (scripts don't use tools) | `src/conductor/config/validator.py` | DONE |
| E1-T7 | TEST | Test: valid script agent definition creates successfully | `tests/test_config/test_script_schema.py` | DONE |
| E1-T8 | TEST | Test: script agent without `command` raises ValidationError | `tests/test_config/test_script_schema.py` | DONE |
| E1-T9 | TEST | Test: script agent with `prompt` raises ValidationError | `tests/test_config/test_script_schema.py` | DONE |
| E1-T10 | TEST | Test: script agent with `provider`/`model`/`tools`/`output`/`system_prompt`/`options` raises ValidationError | `tests/test_config/test_script_schema.py` | DONE |
| E1-T11 | TEST | Test: script agent in parallel group raises ConfigurationError | `tests/test_config/test_script_schema.py` | DONE |
| E1-T12 | TEST | Test: script agent in for_each inline agent raises ConfigurationError | `tests/test_config/test_script_schema.py` | DONE |
| E1-T13 | TEST | Test: existing agent and human_gate definitions still work (backward compatibility) | `tests/test_config/test_script_schema.py` | DONE |
| E1-T14 | TEST | Test: WorkflowConfig with script agent at entry_point validates | `tests/test_config/test_script_schema.py` | DONE |
| E1-T15 | TEST | Test: script agent with routes validates correctly | `tests/test_config/test_script_schema.py` | DONE |
| E1-T16 | TEST | Test: `timeout` field rejects non-positive values | `tests/test_config/test_script_schema.py` | DONE |

**Acceptance Criteria:**
- [x] `AgentDef(name="test", type="script", command="echo")` creates without error
- [x] `AgentDef(name="test", type="script")` raises ValidationError (missing command)
- [x] `AgentDef(name="test", type="script", command="echo", prompt="hi")` raises ValidationError
- [x] Script in for_each inline agent raises ConfigurationError
- [x] Existing `type="agent"` and `type="human_gate"` definitions unchanged
- [x] All existing schema tests pass
- [x] `make lint && make typecheck` pass

---

### Epic 2: Script Executor

**Goal:** Implement the `ScriptExecutor` class that runs shell commands asynchronously, captures output, and handles timeouts.

**Prerequisites:** Epic 1

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E2-T1 | IMPL | Create `ScriptOutput` dataclass with `stdout: str`, `stderr: str`, `exit_code: int` | `src/conductor/executor/script.py` | DONE |
| E2-T2 | IMPL | Create `ScriptExecutor` class with `__init__` initializing `TemplateRenderer` | `src/conductor/executor/script.py` | DONE |
| E2-T3 | IMPL | Implement `ScriptExecutor.execute()`: render command/args via Jinja2, create subprocess with `asyncio.create_subprocess_exec()`, capture stdout/stderr, return `ScriptOutput`. Use `process.returncode` directly (not `process.returncode or 0` — see §4.2.2). | `src/conductor/executor/script.py` | DONE |
| E2-T4 | IMPL | Implement timeout handling: wrap `process.communicate()` with `asyncio.wait_for()`, kill process on timeout, raise `ExecutionError` | `src/conductor/executor/script.py` | DONE |
| E2-T5 | IMPL | Implement environment merging: overlay `agent.env` on `os.environ`. Note: `${VAR:-default}` patterns are already resolved by the config loader before the executor receives them—no additional resolution needed. | `src/conductor/executor/script.py` | DONE |
| E2-T6 | IMPL | Add verbose logging via lazy-import pattern (same as `AgentExecutor`) | `src/conductor/executor/script.py` | DONE |
| E2-T7 | TEST | Test: simple command execution captures stdout correctly | `tests/test_executor/test_script.py` | DONE |
| E2-T8 | TEST | Test: command with args captures output correctly | `tests/test_executor/test_script.py` | DONE |
| E2-T9 | TEST | Test: failing command captures non-zero exit_code (verify exit_code is 1, not 0) | `tests/test_executor/test_script.py` | DONE |
| E2-T10 | TEST | Test: stderr is captured separately from stdout | `tests/test_executor/test_script.py` | DONE |
| E2-T11 | TEST | Test: timeout kills process and raises ExecutionError | `tests/test_executor/test_script.py` | DONE |
| E2-T12 | TEST | Test: custom environment variables are passed to subprocess | `tests/test_executor/test_script.py` | DONE |
| E2-T13 | TEST | Test: working_dir is respected by subprocess | `tests/test_executor/test_script.py` | DONE |
| E2-T14 | TEST | Test: Jinja2 templates in command and args are rendered with context | `tests/test_executor/test_script.py` | DONE |
| E2-T15 | TEST | Test: command not found raises appropriate error | `tests/test_executor/test_script.py` | DONE |

**Acceptance Criteria:**
- [x] `ScriptExecutor` can run `echo hello` and return `ScriptOutput(stdout="hello\n", stderr="", exit_code=0)`
- [x] `ScriptExecutor` running `false` returns `exit_code=1` (not `0`)
- [x] Timeout properly kills hanging processes
- [x] Template variables in command/args are rendered correctly
- [x] All tests pass on macOS and Linux (CI)
- [x] `make lint && make typecheck` pass

---

### Epic 3: Engine Integration

**Goal:** Wire `ScriptExecutor` into the `WorkflowEngine` main loop so script steps execute as part of workflows with full routing, context, and limit support.

**Prerequisites:** Epic 1, Epic 2

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E3-T1 | IMPL | Import `ScriptExecutor` and `ScriptOutput` in `workflow.py` | `src/conductor/engine/workflow.py` | DONE |
| E3-T2 | IMPL | Add `self.script_executor = ScriptExecutor()` to `WorkflowEngine.__init__()` | `src/conductor/engine/workflow.py` | DONE |
| E3-T3 | IMPL | Add `agent.type == "script"` branch in main `run()` loop (before `human_gate` check, after iteration/verbose/trim). Build context, call `ScriptExecutor.execute()`, store output dict `{stdout, stderr, exit_code}` in context, record execution, check timeout, evaluate routes. | `src/conductor/engine/workflow.py` | DONE |
| E3-T4 | IMPL | Add `_execute_script()` helper method wrapping `script_executor.execute()` with `self.limits.wait_for_with_timeout()` for workflow-level timeout enforcement | `src/conductor/engine/workflow.py` | DONE |
| E3-T5 | IMPL | Ensure `_evaluate_routes()` works with script output dict (exit_code available as `output.exit_code` in Jinja2 route conditions, or `exit_code` in simpleeval). No changes needed—Router already handles arbitrary dicts. | `src/conductor/engine/workflow.py` | DONE |
| E3-T6 | IMPL | Verify `_trace_path()` handles `agent_type="script"` in dry-run plan generation (line ~2072) — already handled by `agent.type or "agent"`, just verify. | `src/conductor/engine/workflow.py` | DONE |
| E3-T7 | TEST | Test: linear workflow with script step that succeeds → routes to $end | `tests/test_engine/test_script_workflow.py` | DONE |
| E3-T8 | TEST | Test: script step output accessible in subsequent agent's context | `tests/test_engine/test_script_workflow.py` | DONE |
| E3-T9 | TEST | Test: route branching on exit_code using simpleeval (`exit_code == 0` → success path, else → failure path) | `tests/test_engine/test_script_workflow.py` | DONE |
| E3-T10 | TEST | Test: route branching on exit_code using Jinja2 (`{{ output.exit_code == 0 }}`) | `tests/test_engine/test_script_workflow.py` | DONE |
| E3-T11 | TEST | Test: script step counts toward iteration limit | `tests/test_engine/test_script_workflow.py` | DONE |
| E3-T12 | TEST | Test: script step respects workflow-level timeout | `tests/test_engine/test_script_workflow.py` | DONE |
| E3-T13 | TEST | Test: workflow with mixed agent + script steps executes correctly | `tests/test_engine/test_script_workflow.py` | DONE |
| E3-T14 | TEST | Test: dry-run plan includes script steps with correct agent_type | `tests/test_engine/test_script_workflow.py` | DONE |
| E3-T15 | TEST | Test: script step with Jinja2-templated command using workflow input | `tests/test_engine/test_script_workflow.py` | DONE |
| E3-T16 | TEST | Test: script step with non-zero exit and no routes defaults to $end (no error raised) | `tests/test_engine/test_script_workflow.py` | DONE |

**Acceptance Criteria:**
- [x] A workflow YAML with `type: script` and `command: echo` runs end-to-end
- [x] Script stdout is available to downstream agents via context
- [x] Routes based on `exit_code` work correctly (both simpleeval and Jinja2 forms)
- [x] Non-zero exit with no routes completes workflow (defaults to $end)
- [x] `make test` passes (all existing + new tests)
- [x] `make lint && make typecheck` pass

---

### Epic 4: Example & Documentation

**Goal:** Provide an example workflow YAML demonstrating script steps and update documentation.

**Prerequisites:** Epic 3

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E4-T1 | IMPL | Create example `script-step.yaml` workflow demonstrating: a script step that runs a command, routes on exit_code using simpleeval syntax (`exit_code == 0`), and passes stdout to an agent step | `examples/script-step.yaml` | DONE |
| E4-T2 | TEST | Verify example passes `make validate-examples` | — | DONE |

**Acceptance Criteria:**
- [x] `uv run conductor validate examples/script-step.yaml` succeeds
- [x] Example demonstrates script → route → agent pattern
- [x] Example uses correct route condition syntax (simpleeval `exit_code == 0`, not Jinja2 `{{ exit_code == 0 }}`)
