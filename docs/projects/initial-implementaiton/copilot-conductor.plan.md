---
id: PLAN-001
title: Conductor - Implementation Plan
version: 1.0
status: DRAFT
date_created: 2026-01-26
last_updated: 2026-01-26
owner: Conductor Team
tags: feature, cli, multi-agent, workflow
---

# Conductor - Implementation Plan

## Related Documents

| Type | Document | Status |
|------|----------|--------|
| Requirements | [conductor.brainstorm.md](./conductor.brainstorm.md) | APPROVED |
| Solution Design | [conductor.design.md](./conductor.design.md) | APPROVED |

---

## 1. Summary

This implementation plan outlines the development of Conductor, a Python CLI tool for orchestrating multi-agent workflows defined in YAML. The implementation follows a phased approach across 4 phases:

- **Phase 1 (Core MVP)**: 2-3 weeks - Project setup, configuration layer, basic workflow execution with Copilot SDK
- **Phase 2 (Routing & Loops)**: 1-2 weeks - Conditional routing, loop-back patterns, safety limits
- **Phase 3 (Advanced Features)**: 2 weeks - Human gates, tools, additional CLI commands
- **Phase 4 (Polish)**: 1-2 weeks - Error handling, observability, distribution, documentation

**Total estimated timeline**: 6-9 weeks for full feature implementation with 80%+ test coverage.

The implementation is broken into 14 EPICs with atomic tasks suitable for execution by AI systems or human developers. Each task is traceable to specific requirements from the design document.

---

## 2. Scope

### In Scope

- Python package `conductor` with CLI interface
- YAML workflow configuration parsing and validation with Pydantic v2
- Jinja2 template rendering for prompts and expressions
- Workflow execution engine with context passing
- Context accumulation modes (accumulate, last_only, explicit)
- Optional input dependencies with `?` suffix
- Conditional routing with `$end` termination and loop-back patterns
- Safety limits (max iterations, timeout)
- Context trimming strategies (summarize, truncate, drop_oldest)
- GitHub Copilot SDK provider integration
- Human-in-the-loop gates with Rich terminal UI
- Tool configuration and SDK pass-through
- CLI commands: `run`, `validate`, `init`, `templates`
- Output schema validation and structured JSON output
- Unit and integration tests with 80%+ coverage
- PyPI distribution via `uvx`/`pipx`

### Out of Scope

| Item | Rationale |
|------|-----------|
| GUI or web interface | CLI-only tool per NON-GOAL-001 |
| Agent training or fine-tuning | Uses SDK-provided models per NON-GOAL-002 |
| Custom model hosting | Relies on provider APIs per NON-GOAL-003 |
| Real-time collaboration | Single-user execution per NON-GOAL-004 |
| Providers beyond Copilot SDK | Initial release focuses on Copilot; abstraction exists for future per NON-GOAL-005 |
| Parallel agent execution | Future enhancement; requires dependency graph analysis |
| Workflow checkpointing | Future enhancement for long-running workflows |
| Streaming output support (--stream flag) | Deferred to a future version. The provider abstraction supports streaming, but CLI streaming output and progress display require additional design work. |

---

## 3. Requirements Traceability

| Requirement | Description | EPIC(s) | Task(s) |
|-------------|-------------|---------|---------|
| REQ-001 | YAML parsing with ruamel.yaml | EPIC-002 | TASK-006, TASK-007 |
| REQ-002 | Pydantic schema validation | EPIC-002 | TASK-008, TASK-009, TASK-010 |
| REQ-003 | Environment variable resolution | EPIC-002 | TASK-011, TASK-012 |
| REQ-004 | Workflow inputs, hooks | EPIC-002, EPIC-012 | TASK-008, TASK-058 |
| REQ-005 | Jinja2 template rendering | EPIC-003 | TASK-013, TASK-014 |
| REQ-006 | Jinja2 json filter | EPIC-003 | TASK-013, TASK-014 |
| REQ-007 | Conditional blocks/loops in templates | EPIC-003 | TASK-013, TASK-014 |
| REQ-008 | StrictUndefined for templates | EPIC-003 | TASK-013, TASK-014 |
| REQ-009 | Agent execution order via routing | EPIC-004, EPIC-006 | TASK-019, TASK-033 |
| REQ-010 | Agent-specific context building | EPIC-004 | TASK-017, TASK-018, TASK-031, TASK-032, TASK-070 |
| REQ-011 | Store agent outputs in context | EPIC-004 | TASK-017, TASK-018 |
| REQ-012 | Route condition evaluation (first match) | EPIC-006 | TASK-033, TASK-034, TASK-035 |
| REQ-013 | `$end` termination | EPIC-006 | TASK-035 |
| REQ-014 | Unconditional routes | EPIC-006 | TASK-033 |
| REQ-015 | Loop-back routing | EPIC-006 | TASK-036 |
| REQ-016 | max_iterations enforcement | EPIC-007 | TASK-039, TASK-040 |
| REQ-017 | timeout_seconds enforcement | EPIC-007 | TASK-041, TASK-042 |
| REQ-018 | Clear limit exceptions | EPIC-007 | TASK-040, TASK-042 |
| REQ-019 | human_gate agent type | EPIC-009 | TASK-048 |
| REQ-020 | Rich formatting for gates | EPIC-009 | TASK-049 |
| REQ-021 | Gate routing with prompt_for | EPIC-009 | TASK-050 |
| REQ-022 | Workflow-level tools | EPIC-010 | TASK-053 |
| REQ-023 | SDK tool pass-through | EPIC-010 | TASK-054 |
| REQ-024 | Output schema validation | EPIC-004 | TASK-020 |
| REQ-025 | Final output template expressions | EPIC-004 | TASK-021 |
| REQ-026 | JSON output | EPIC-005 | TASK-028 |
| REQ-027 | `conductor run` command | EPIC-005 | TASK-024, TASK-025, TASK-026 |
| REQ-028 | `conductor validate` command | EPIC-011 | TASK-055, TASK-056 |
| REQ-029 | `conductor init` command | EPIC-011 | TASK-057 |
| REQ-030 | `--dry-run` flag | EPIC-008 | TASK-045, TASK-046 |
| REQ-031 | `--verbose` flag | EPIC-011 | TASK-058 |
| REQ-032 | `--skip-gates` flag | EPIC-009 | TASK-051 |
| REQ-033 | `conductor templates` command | EPIC-011 | TASK-057 |
| REQ-034 | `--provider` flag | EPIC-005 | TASK-027 |
| NFR-001 | Startup time <500ms | EPIC-013 | TASK-063 |
| NFR-002 | Memory <100MB | EPIC-013 | TASK-063 |
| NFR-003 | Python 3.12+ with type hints | EPIC-001 | TASK-002, TASK-004 |
| NFR-004 | 80%+ test coverage | EPIC-013 | TASK-062 |
| NFR-005 | Documented public APIs | EPIC-014 | TASK-065 |
| NFR-006 | Actionable error messages | EPIC-012 | TASK-059, TASK-060 |
| NFR-007 | Retry logic for SDK failures | EPIC-012, EPIC-003 | TASK-061, TASK-068 |

---

## 4. Files Affected

### New Files

| File Path | Purpose | EPIC |
|-----------|---------|------|
| `pyproject.toml` | Project configuration, dependencies, tool settings | EPIC-001 |
| `src/conductor/__init__.py` | Package root with version | EPIC-001 |
| `src/conductor/__main__.py` | Entry point for `python -m` | EPIC-001 |
| `src/conductor/exceptions.py` | Custom exception hierarchy | EPIC-001 |
| `src/conductor/cli/__init__.py` | CLI module init | EPIC-005 |
| `src/conductor/cli/app.py` | Typer app definition | EPIC-005 |
| `src/conductor/cli/run.py` | `run` command implementation | EPIC-005 |
| `src/conductor/cli/validate.py` | `validate` command | EPIC-011 |
| `src/conductor/cli/init.py` | `init` and `templates` commands | EPIC-011 |
| `src/conductor/config/__init__.py` | Config module init | EPIC-002 |
| `src/conductor/config/loader.py` | YAML parsing, env var resolution | EPIC-002 |
| `src/conductor/config/schema.py` | Pydantic models | EPIC-002 |
| `src/conductor/config/validator.py` | Cross-field validation | EPIC-002 |
| `src/conductor/engine/__init__.py` | Engine module init | EPIC-004 |
| `src/conductor/engine/workflow.py` | Workflow execution loop | EPIC-004 |
| `src/conductor/engine/context.py` | Context management | EPIC-004 |
| `src/conductor/engine/router.py` | Route evaluation | EPIC-006 |
| `src/conductor/engine/limits.py` | Iteration/timeout enforcement | EPIC-007 |
| `src/conductor/executor/__init__.py` | Executor module init | EPIC-003 |
| `src/conductor/executor/agent.py` | Agent execution orchestration | EPIC-004 |
| `src/conductor/executor/template.py` | Jinja2 template rendering | EPIC-003 |
| `src/conductor/executor/output.py` | Output parsing/validation | EPIC-004 |
| `src/conductor/providers/__init__.py` | Providers module init | EPIC-003 |
| `src/conductor/providers/base.py` | AgentProvider ABC | EPIC-003 |
| `src/conductor/providers/factory.py` | Provider factory | EPIC-003 |
| `src/conductor/providers/copilot.py` | Copilot SDK provider | EPIC-003 |
| `src/conductor/gates/__init__.py` | Gates module init | EPIC-009 |
| `src/conductor/gates/human.py` | Human gate handler | EPIC-009 |
| `src/conductor/templates/` | Workflow template files | EPIC-011 |
| `tests/__init__.py` | Tests package init | EPIC-001 |
| `tests/conftest.py` | Pytest fixtures | EPIC-001 |
| `tests/test_config/` | Config layer tests | EPIC-002 |
| `tests/test_engine/` | Engine layer tests | EPIC-004, EPIC-006, EPIC-007 |
| `tests/test_executor/` | Executor layer tests | EPIC-003 |
| `tests/test_providers/` | Provider tests | EPIC-003 |
| `tests/test_cli/` | CLI command tests | EPIC-005 |
| `tests/test_gates/` | Human gate tests | EPIC-009 |
| `tests/fixtures/` | YAML workflow fixtures | EPIC-002 |
| `examples/simple-qa.yaml` | Simple Q&A example | EPIC-014 |
| `examples/design-review.yaml` | Loop with human gate example | EPIC-014 |
| `examples/research-assistant.yaml` | Tool usage example | EPIC-014 |

### Modified Files

| File Path | Modifications | EPIC |
|-----------|---------------|------|
| (None - greenfield project) | | |

### Deleted Files

| File Path | Reason | EPIC |
|-----------|--------|------|
| (None - greenfield project) | | |

---

## 5. Implementation Plan

### EPIC-001: Project Setup and Foundation

**Goal**: Establish project structure, dependencies, and development tooling

**Prerequisites**: None

#### Tasks

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| TASK-001 | IMPL | Create project directory structure with src layout | `src/conductor/`, all `__init__.py` files | S | DONE |
| TASK-002 | IMPL | Create pyproject.toml with dependencies (typer, rich, pydantic, ruamel.yaml, jinja2, simpleeval, github-copilot-sdk) and dev tools (pytest, pytest-asyncio, pytest-cov, ruff, ty) | `pyproject.toml` | S | DONE |
| TASK-003 | IMPL | Create __main__.py entry point | `src/conductor/__main__.py` | S | DONE |
| TASK-004 | IMPL | Create exceptions module with ConductorError hierarchy (ConfigurationError, ValidationError, TemplateError, ProviderError, ExecutionError, MaxIterationsError, TimeoutError, HumanGateError) | `src/conductor/exceptions.py` | S | DONE |
| TASK-005 | TEST | Create conftest.py with basic fixtures and test structure | `tests/__init__.py`, `tests/conftest.py` | S | DONE |

**Acceptance Criteria**:
- [x] `uv sync` installs all dependencies successfully
- [x] `uv run ruff check .` passes with no errors
- [x] `uv run python -m conductor` runs without import errors
- [x] All tests pass (even if empty)

---

### EPIC-002: Configuration Layer

**Goal**: Implement YAML loading, Pydantic schema validation, and environment variable resolution

**Prerequisites**: EPIC-001

#### Tasks

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| TASK-006 | IMPL | Implement YAML parser using ruamel.yaml with error handling and line number tracking | `src/conductor/config/loader.py` | M | DONE |
| TASK-007 | TEST | Unit tests for YAML parsing including malformed YAML handling | `tests/test_config/test_loader.py` | S | DONE |
| TASK-008 | IMPL | Implement Pydantic models for all schema types (InputDef, OutputField, RouteDef, GateOption, ContextConfig, LimitsConfig, HooksConfig, AgentDef, RuntimeConfig, WorkflowDef, WorkflowConfig) | `src/conductor/config/schema.py` | L | DONE |
| TASK-009 | IMPL | Implement cross-field validators (entry_point exists, route targets valid, human_gate has options, tool references valid) | `src/conductor/config/validator.py` | M | DONE |
| TASK-010 | TEST | Unit tests for schema validation covering all models and validators | `tests/test_config/test_schema.py`, `tests/test_config/test_validator.py` | M | DONE |
| TASK-011 | IMPL | Implement environment variable resolution (${ENV:-default} format) with recursive resolution | `src/conductor/config/loader.py` | S | DONE |
| TASK-012 | TEST | Tests for env var resolution including defaults, missing vars, and nested references | `tests/test_config/test_loader.py` | S | DONE |

**Acceptance Criteria**:
- [x] Valid YAML files parse into typed WorkflowConfig objects
- [x] Invalid YAML produces clear error messages with line numbers
- [x] Missing required fields raise ValidationError with field path
- [x] Environment variables are resolved before validation
- [x] All config tests pass with >90% coverage of config module (96% achieved)

---

### EPIC-003: Template and Provider Foundation

**Goal**: Implement Jinja2 template rendering and provider abstraction layer

**Prerequisites**: EPIC-001

#### Tasks

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| TASK-013 | IMPL | Implement TemplateRenderer with StrictUndefined, json filter, default filter, and keep_trailing_newline | `src/conductor/executor/template.py` | M | DONE |
| TASK-014 | TEST | Unit tests for template rendering: simple vars, json filter, conditionals, loops, missing vars, nested access | `tests/test_executor/test_template.py` | M | DONE |
| TASK-015 | IMPL | Implement AgentProvider ABC with execute(), validate_connection(), close() methods and AgentOutput dataclass | `src/conductor/providers/base.py` | S | DONE |
| TASK-016 | IMPL | Implement provider factory with copilot provider instantiation and error handling for unknown providers | `src/conductor/providers/factory.py` | S | DONE |
| TASK-068 | IMPL | Implement validate_connection() integration - call during provider initialization in ProviderFactory, raise ProviderError with actionable message on failure | `src/conductor/providers/factory.py` | S | DONE |

**Acceptance Criteria**:
- [x] Templates render with workflow and agent context
- [x] `{{ value | json }}` produces formatted JSON
- [x] Missing template variables raise TemplateError with variable name
- [x] Provider factory returns CopilotProvider for "copilot" type
- [x] Unknown provider types raise clear error
- [x] validate_connection() called on provider instantiation with clear error on failure

---

### EPIC-004: Core Workflow Engine

**Goal**: Implement workflow execution, context management, and agent execution

**Prerequisites**: EPIC-002, EPIC-003

#### Tasks

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| TASK-017 | IMPL | Implement WorkflowContext dataclass with workflow_inputs, agent_outputs, iteration tracking, execution_history | `src/conductor/engine/context.py` | M | DONE |
| TASK-018 | TEST | Unit tests for WorkflowContext: store, build_for_agent, get_for_template | `tests/test_engine/test_context.py` | S | DONE |
| TASK-019 | IMPL | Implement basic WorkflowEngine with linear execution loop and async support | `src/conductor/engine/workflow.py` | L | DONE |
| TASK-020 | IMPL | Implement output parsing and Pydantic validation against output schema | `src/conductor/executor/output.py` | M | DONE |
| TASK-021 | IMPL | Implement AgentExecutor for single agent execution orchestration | `src/conductor/executor/agent.py` | M | DONE |
| TASK-022 | IMPL | Implement CopilotProvider with GitHub Copilot SDK integration | `src/conductor/providers/copilot.py` | L | DONE |
| TASK-023 | TEST | Integration tests for workflow execution with mock provider | `tests/test_engine/test_workflow.py` | M | DONE |
| TASK-031 | IMPL | Implement context accumulation modes: `accumulate` (all prior outputs), `last_only` (previous agent only), `explicit` (only declared inputs). Mode configured at workflow.context.mode level | `src/conductor/engine/context.py` | M | DONE |
| TASK-032 | IMPL | Implement optional input dependencies with `?` suffix (e.g., `reviewer.feedback?`). Missing optional deps omit from context rather than raising error | `src/conductor/engine/context.py` | S | DONE |

**Acceptance Criteria**:
- [x] Linear workflow executes from entry_point to $end
- [x] Agent outputs are stored in context and accessible to subsequent agents
- [x] Template expressions in prompts are rendered with correct context
- [x] Output schema violations raise ValidationError
- [x] Workflow returns final output as dict
- [x] All three context modes (accumulate, last_only, explicit) work correctly
- [x] Optional dependencies with `?` suffix are handled gracefully

---

### EPIC-005: Basic CLI Implementation

**Goal**: Implement `conductor run` command with input flags and output formatting

**Prerequisites**: EPIC-004

#### Tasks

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| TASK-024 | IMPL | Create Typer app with global options (--version, --help) | `src/conductor/cli/app.py` | S | DONE |
| TASK-025 | IMPL | Implement `run` command with workflow file argument | `src/conductor/cli/run.py` | M | DONE |
| TASK-026 | IMPL | Implement `--input.<name>=<value>` flag parsing with type coercion | `src/conductor/cli/run.py` | M | DONE |
| TASK-027 | IMPL | Implement `--provider` flag for runtime provider override | `src/conductor/cli/run.py` | S | DONE |
| TASK-028 | IMPL | Implement JSON output formatting for workflow results | `src/conductor/cli/run.py` | S | DONE |
| TASK-029 | TEST | CLI tests for run command with various input combinations | `tests/test_cli/test_run.py` | M | DONE |
| TASK-030 | TEST | End-to-end test with example workflow file | `tests/test_cli/test_e2e.py` | M | DONE |

**Acceptance Criteria**:
- [x] `conductor run workflow.yaml` executes workflow
- [x] `--input.name=value` flags are parsed and passed to workflow
- [x] `--provider openai-agents` raises NotImplementedError (expected)
- [x] Output is valid JSON to stdout
- [x] Exit code 0 on success, non-zero on failure

---

### EPIC-006: Routing Engine

**Goal**: Implement conditional routing, $end handling, and loop-back patterns

**Prerequisites**: EPIC-004

#### Tasks

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| TASK-033 | IMPL | Implement Router with condition evaluation using template renderer for boolean expressions | `src/conductor/engine/router.py` | M | DONE |
| TASK-034 | IMPL | Implement RouteResult dataclass with target, output_transform, matched_rule | `src/conductor/engine/router.py` | S | DONE |
| TASK-035 | IMPL | Handle $end termination with final output building using output template expressions | `src/conductor/engine/workflow.py` | S | DONE |
| TASK-036 | IMPL | Support loop-back routing (route to previously executed agent) with iteration tracking | `src/conductor/engine/workflow.py` | S | DONE |
| TASK-037 | TEST | Unit tests for Router: unconditional, conditional, fallthrough, $end | `tests/test_engine/test_router.py` | M | DONE |
| TASK-038 | TEST | Integration tests for loop-back workflows | `tests/test_engine/test_workflow.py` | M | DONE |
| TASK-069 | IMPL | Integrate simpleeval for arithmetic comparisons in `when` clauses (e.g., `score > 7`, `iteration < 5`). Router detects expression type and routes to appropriate evaluator | `src/conductor/engine/router.py` | M | DONE |

**Acceptance Criteria**:
- [x] Routes evaluated in order, first matching `when` wins
- [x] Routes without `when` always match
- [x] `$end` terminates workflow and triggers output building
- [x] Loop-back to previous agent works correctly
- [x] No matching routes raises clear error
- [x] Arithmetic comparisons work in when clauses via simpleeval

---

### EPIC-007: Safety Limits

**Goal**: Implement max_iterations and timeout enforcement with clear exceptions

**Prerequisites**: EPIC-006

#### Tasks

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| TASK-039 | IMPL | Implement LimitEnforcer with iteration tracking | `src/conductor/engine/limits.py` | S | DONE |
| TASK-040 | IMPL | Implement MaxIterationsError with context (current iteration, agent history) | `src/conductor/engine/limits.py` | S | DONE |
| TASK-041 | IMPL | Implement timeout enforcement using asyncio.timeout() | `src/conductor/engine/limits.py` | M | DONE |
| TASK-042 | IMPL | Implement TimeoutError with context (elapsed time, current agent) | `src/conductor/engine/limits.py` | S | DONE |
| TASK-043 | IMPL | Integrate limits into WorkflowEngine execution loop | `src/conductor/engine/workflow.py` | S | DONE |
| TASK-044 | TEST | Unit tests for limit enforcement with edge cases | `tests/test_engine/test_limits.py` | M | DONE |

**Acceptance Criteria**:
- [x] Workflow terminates at max_iterations with MaxIterationsError
- [x] Workflow terminates at timeout with TimeoutError
- [x] Error messages include iteration count and agent history
- [x] Default limits (10 iterations, 600s) apply when not specified
- [x] Limits are configurable via workflow.limits

---

### EPIC-008: Dry-Run Mode

**Goal**: Implement --dry-run flag to show execution plan without running

**Prerequisites**: EPIC-006

#### Tasks

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| TASK-045 | IMPL | Implement ExecutionPlan builder that traces workflow without executing | `src/conductor/engine/workflow.py` | M | DONE |
| TASK-046 | IMPL | Implement Rich-formatted dry-run output showing agent sequence, models, and conditions | `src/conductor/cli/run.py` | M | DONE |
| TASK-047 | TEST | Tests for dry-run mode with various workflow patterns | `tests/test_cli/test_run.py` | S | DONE |

**Acceptance Criteria**:
- [x] `--dry-run` shows execution plan without calling SDK
- [x] Plan shows agent sequence with models
- [x] Conditional routes show possible branches
- [x] Loop patterns are indicated
- [x] No network calls made during dry-run

---

### EPIC-009: Human Gates

**Goal**: Implement human-in-the-loop gates with Rich interactive prompts

**Prerequisites**: EPIC-006

#### Tasks

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| TASK-048 | IMPL | Implement HumanGateHandler with option display and context access | `src/conductor/gates/human.py` | M | DONE |
| TASK-049 | IMPL | Implement Rich-based interactive selection prompt with keyboard navigation | `src/conductor/gates/human.py` | M | DONE |
| TASK-050 | IMPL | Implement prompt_for text input collection and storage in context | `src/conductor/gates/human.py` | S | DONE |
| TASK-051 | IMPL | Implement --skip-gates flag for automation testing (auto-selects first option) | `src/conductor/cli/run.py`, `src/conductor/gates/human.py` | S | DONE |
| TASK-052 | TEST | Unit tests for human gate with mocked terminal input | `tests/test_gates/test_human.py` | M | DONE |

**Acceptance Criteria**:
- [x] Human gates pause workflow for user selection
- [x] Options display with Rich formatting
- [x] Selected option's route is followed
- [x] prompt_for collects text input and stores in context
- [x] --skip-gates auto-selects first option

---

### EPIC-010: Tool Support

**Goal**: Implement workflow-level tool configuration and SDK pass-through

**Prerequisites**: EPIC-004

#### Tasks

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| TASK-053 | IMPL | Implement tool configuration parsing at workflow level (tools list in WorkflowConfig) | `src/conductor/config/schema.py` | S | DONE |
| TASK-054 | IMPL | Pass tool list to CopilotProvider for SDK resolution. Support agent-level tools (all, subset, none) | `src/conductor/providers/copilot.py`, `src/conductor/executor/agent.py` | M | DONE |
| TASK-071 | TEST | Tests for tool configuration and passing | `tests/test_providers/test_copilot.py`, `tests/test_executor/test_agent.py` | S | DONE |

**Acceptance Criteria**:
- [x] Workflow-level tools list is parsed from YAML
- [x] Agent-level tools (subset, all, none) work correctly
- [x] Tools are passed to SDK provider
- [x] Unknown tools produce clear error

---

### EPIC-011: Additional CLI Commands

**Goal**: Implement validate, init, and templates commands with verbose logging

**Prerequisites**: EPIC-005

#### Tasks

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| TASK-055 | IMPL | Implement `conductor validate` command with detailed error reporting | `src/conductor/cli/validate.py` | M | DONE |
| TASK-056 | TEST | Tests for validate command with valid/invalid files | `tests/test_cli/test_validate.py` | S | DONE |
| TASK-057 | IMPL | Implement `conductor init` and `conductor templates` commands with template scaffolding | `src/conductor/cli/init.py`, `src/conductor/templates/` | M | DONE |
| TASK-058 | IMPL | Implement --verbose flag with Rich logging showing context, prompts, responses, timing | `src/conductor/cli/app.py`, `src/conductor/cli/run.py` | M | DONE |

**Acceptance Criteria**:
- [x] `validate` reports all schema errors without execution
- [x] `init <name>` creates workflow file from template
- [x] `templates` lists available templates with descriptions
- [x] `--verbose` shows detailed execution progress
- [x] All commands have `--help` documentation

---

### EPIC-012: Error Handling and Retry Logic

**Goal**: Implement comprehensive error handling with actionable messages and SDK retry logic

**Prerequisites**: EPIC-007

#### Tasks

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| TASK-059 | IMPL | Implement lifecycle hooks (on_start, on_complete, on_error) execution in WorkflowEngine | `src/conductor/engine/workflow.py` | M | DONE |
| TASK-060 | IMPL | Enhance all exceptions with file path, line numbers, suggestions | `src/conductor/exceptions.py` | M | DONE |
| TASK-061 | IMPL | Implement error formatting with Rich output (colored, structured) | `src/conductor/cli/app.py` | S | DONE |
| TASK-062 | IMPL | Implement exponential backoff retry for SDK calls (3 attempts, configurable) | `src/conductor/providers/copilot.py` | M | DONE |
| TASK-070 | IMPL | Implement context trimming strategies: `truncate` (cut oldest content), `drop_oldest` (remove full agent outputs FIFO), `summarize` (use LLM to summarize). Applied when context exceeds max_tokens | `src/conductor/engine/context.py` | L | DONE |
| TASK-072 | TEST | Tests for error handling, retry logic, and context trimming | `tests/test_providers/test_copilot.py`, `tests/test_exceptions.py`, `tests/test_engine/test_context.py` | M | DONE |

**Acceptance Criteria**:
- [x] All errors include what, where, why, and how to fix
- [x] SDK failures retry with exponential backoff (3 attempts)
- [x] Lifecycle hooks are called at appropriate times
- [x] Error output uses Rich formatting for readability
- [x] Non-retryable errors fail immediately with clear message
- [x] All three context trimming strategies work correctly

---

### EPIC-013: Testing and Coverage

**Goal**: Achieve 80%+ test coverage with comprehensive test suite

**Prerequisites**: EPIC-001 through EPIC-012

#### Tasks

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| TASK-063 | TEST | Add missing unit tests to achieve 80% coverage across all modules | All test files | L | DONE |
| TASK-064 | TEST | Performance tests for startup time (<500ms) and memory (<100MB for 10-agent workflow) | `tests/test_performance.py` | M | DONE |
| TASK-065 | TEST | Integration tests with all example workflows | `tests/test_integration/` | M | DONE |

**Acceptance Criteria**:
- [x] `pytest --cov` reports 80%+ coverage (93% achieved)
- [x] Startup time verified under 500ms
- [x] Memory usage verified under 100MB for 10-agent workflow
- [x] All example workflows execute successfully

---

### EPIC-014: Documentation and Distribution

**Goal**: Complete documentation, examples, and PyPI publishing

**Prerequisites**: EPIC-013

#### Tasks

| Task ID | Type | Description | Files | Estimate | Status |
|---------|------|-------------|-------|----------|--------|
| TASK-066 | DOCS | Add docstrings to all public APIs with type hints | All `src/` files | M | DONE |
| TASK-067 | DOCS | Create README.md with quick start, examples, and CLI reference | `README.md` | M | DONE |
| TASK-073 | IMPL | Create example workflow files (simple-qa, design-review, research-assistant) | `examples/*.yaml` | S | DONE |
| TASK-074 | IMPL | Configure GitHub Actions for CI (lint with ruff, type check with ty, test with pytest) | `.github/workflows/ci.yml` | S | DONE |
| TASK-075 | IMPL | Configure PyPI publishing workflow with version tagging | `.github/workflows/publish.yml`, `pyproject.toml` | S | DONE |

**Acceptance Criteria**:
- [x] All public functions have docstrings with type hints
- [x] README has installation, quick start, and CLI reference
- [x] Examples cover: simple Q&A, loop pattern, human gate, tools
- [x] CI runs on every PR
- [x] Package installable via `uvx conductor`

---

## 6. Phase Summary

### Phase 1: Core MVP (EPICs 001-005)

**Duration**: 2-3 weeks

**Deliverables**:
- Project structure with all dependencies
- Configuration loading and validation
- Template rendering
- Basic workflow execution with context management
- Context accumulation modes and optional dependencies
- `conductor run` command
- Unit tests for core components

### Phase 2: Routing & Loops (EPICs 006-008)

**Duration**: 1-2 weeks

**Deliverables**:
- Conditional routing engine with simpleeval integration
- Loop-back pattern support
- Safety limits (max_iterations, timeout)
- Dry-run mode

### Phase 3: Advanced Features (EPICs 009-011)

**Duration**: 2 weeks

**Deliverables**:
- Human-in-the-loop gates with Rich UI
- Tool support and SDK pass-through
- Additional CLI commands (validate, init, templates)
- Verbose logging

### Phase 4: Polish (EPICs 012-014)

**Duration**: 1-2 weeks

**Deliverables**:
- Comprehensive error handling with actionable messages
- Retry logic with exponential backoff
- Context trimming strategies
- Lifecycle hooks
- 80%+ test coverage
- Documentation
- PyPI distribution

---

## 7. Dependency Graph

```
EPIC-001 (Setup)
    │
    ├── EPIC-002 (Config) ──┐
    │                       │
    └── EPIC-003 (Template/Provider)
                            │
                            ▼
                      EPIC-004 (Engine + Context Modes)
                            │
                            ├── EPIC-005 (CLI) ──► EPIC-011 (More CLI)
                            │
                            ├── EPIC-010 (Tools)
                            │
                            └── EPIC-006 (Routing + simpleeval)
                                    │
                                    ├── EPIC-007 (Limits)
                                    │       │
                                    │       └── EPIC-012 (Error/Retry/Trimming)
                                    │
                                    ├── EPIC-008 (Dry-run)
                                    │
                                    └── EPIC-009 (Human Gates)
                            │
                            ▼
                      EPIC-013 (Testing)
                            │
                            ▼
                      EPIC-014 (Docs/Publish)
```

---

## Change Log

| Date | Version | Author | Changes |
|------|---------|--------|---------|
| 2026-01-26 | 1.0 | AI Architect | Initial implementation plan with 14 EPICs and 75 tasks. Reviewed and refined to score 94/100. Added context modes (TASK-031), optional deps (TASK-032), simpleeval (TASK-069), validate_connection (TASK-068), context trimming (TASK-070). Clarified streaming as out of scope. |
