# AGENTS.md

## Project Overview

Conductor is a CLI tool for defining and running multi-agent workflows with the GitHub Copilot SDK. Workflows are defined in YAML and support parallel execution, conditional routing, loop-back patterns, and human-in-the-loop gates.

## Common Commands

```bash
# Install dependencies
make install          # or: uv sync
make dev              # install with dev dependencies

# Run tests
make test                                           # all tests
uv run pytest tests/test_engine/test_workflow.py   # single file
uv run pytest -k "test_parallel"                   # pattern match

# Run tests with coverage
make test-cov

# Lint and format
make lint             # check only
make format           # auto-fix and format

# Type check
make typecheck

# Run all checks (lint + typecheck)
make check

# Run a workflow
uv run conductor run workflow.yaml --input question="What is Python?"

# Run with web dashboard
uv run conductor run workflow.yaml --web --input question="What is Python?"

# Run in background (prints dashboard URL and exits)
uv run conductor run workflow.yaml --web-bg --input question="What is Python?"

# Stop a background workflow
uv run conductor stop                  # auto-stop if one running, list if multiple
uv run conductor stop --port 8080      # stop specific port
uv run conductor stop --all            # stop all background workflows

# Update conductor
uv run conductor update                # check for and install latest version

# Resume a failed workflow from checkpoint
uv run conductor resume workflow.yaml  # resume from latest checkpoint
uv run conductor checkpoints           # list available checkpoints

# Validate a workflow
uv run conductor validate examples/simple-qa.yaml
make validate-examples    # validate all examples
```

## Architecture

### Core Package Structure (`src/conductor/`)

- **cli/**: Typer-based CLI with commands `run`, `validate`, `init`, `templates`, `stop`, `update`, `resume`, `checkpoints`
  - `app.py` - Main entry point, defines the Typer application
  - `run.py` - Workflow execution command with verbose logging helpers
  - `bg_runner.py` - Background process forking for `--web-bg` mode
  - `pid.py` - PID file utilities for tracking/stopping background processes
  - `update.py` - Update check, version comparison, and self-upgrade via `uv tool install`

- **config/**: YAML loading and Pydantic schema validation
  - `schema.py` - Pydantic models for all workflow YAML structures (WorkflowConfig, AgentDef, ParallelGroup, ForEachDef, etc.)
  - `loader.py` - YAML parsing with environment variable resolution (${VAR:-default}) and `!file` tag support
  - `validator.py` - Cross-reference validation (agent names, routes, parallel groups)

- **engine/**: Workflow execution orchestration
  - `workflow.py` - Main `WorkflowEngine` class that orchestrates agent execution, parallel groups, for-each groups, and routing
  - `context.py` - `WorkflowContext` manages accumulated agent outputs with three modes: accumulate, last_only, explicit
  - `router.py` - Route evaluation with Jinja2 templates and simpleeval expressions
  - `limits.py` - Safety enforcement (max iterations, timeout)
  - `checkpoint.py` - Automatic checkpoint saving on failure and resume support

- **executor/**: Agent execution
  - `agent.py` - `AgentExecutor` handles prompt rendering, tool resolution, and output validation for single agents
  - `script.py` - `ScriptExecutor` runs shell commands as workflow steps, capturing stdout/stderr/exit_code
  - `template.py` - Jinja2 template rendering
  - `output.py` - JSON output parsing and schema validation

- **providers/**: SDK provider abstraction
  - `base.py` - `AgentProvider` ABC defining `execute()`, `validate_connection()`, `close()`
  - `copilot.py` - GitHub Copilot SDK implementation
  - `claude.py` - Anthropic Claude API implementation
  - `factory.py` - Provider instantiation

- **gates/**: Human-in-the-loop support
  - `human.py` - Rich terminal UI for human gate interactions

- **interrupt/**: Interactive workflow interruption (Esc/Ctrl+G to pause)
  - `listener.py` - Keyboard listener daemon thread for Esc/Ctrl+G detection

- **web/**: Real-time web dashboard for workflow visualization
  - `server.py` - FastAPI + uvicorn server with WebSocket broadcasting, late-joiner state replay, and `POST /api/stop` endpoint
  - `static/index.html` - Single-file Cytoscape.js frontend with DAG graph, agent detail panel, and streaming activity

- **events.py**: Pub/sub event system decoupling workflow execution from rendering (console, web dashboard)

- **exceptions.py**: Custom exception hierarchy (ConductorError, ValidationError, ExecutionError, etc.)

### Workflow Execution Flow

1. CLI parses YAML via `config/loader.py` → `WorkflowConfig`
2. `WorkflowEngine` initializes with config and provider
3. Engine loops: find agent/parallel/for-each/script → execute → evaluate routes → next
4. Parallel groups execute agents concurrently with context isolation (deep copy snapshot)
5. For-each groups resolve source arrays at runtime, inject loop variables (`{{ item }}`, `{{ _index }}`, `{{ _key }}`)
6. Script steps run shell commands via asyncio subprocess, expose stdout/stderr/exit_code to context
7. Routes evaluated via `Router` using Jinja2 or simpleeval expressions
8. Final output built from templates in `output:` section

### Key Patterns

- **Context modes**: `accumulate` (all prior outputs), `last_only` (previous only), `explicit` (only declared inputs)
- **Failure modes** for parallel/for-each: `fail_fast`, `continue_on_error`, `all_or_nothing`
- **Route evaluation**: First matching `when` condition wins; no `when` = always matches
- **Tool resolution**: `null` = all workflow tools, `[]` = none, `[list]` = subset

## Tests Structure

Tests mirror source structure in `tests/`:
- `test_cli/` - CLI command tests, e2e tests
- `test_config/` - Schema validation, loader tests
- `test_engine/` - Workflow, router, context, limits tests
- `test_executor/` - Agent, template, output tests
- `test_providers/` - Provider implementation tests
- `test_integration/` - Full workflow execution tests
- `test_gates/` - Human gate tests

Use `pytest.mark.performance` for performance tests (exclude with `-m "not performance"`).

## Code Style

- Python 3.12+
- Ruff for linting/formatting (line length 100)
- Google-style docstrings
- Type hints required, checked with ty (Red Knot)
- Pydantic v2 for data validation
- async/await for all provider operations

### Provider Parity

All providers (`copilot.py`, `claude.py`) must maintain feature parity. Any change to one provider's behavior, contract, or capabilities must be applied to all providers. This includes:

- **Event callbacks**: Same event types emitted at the same semantic points
  - `agent_turn_start` with `{"turn": "awaiting_model"}` — immediately before each API call
  - `agent_turn_start` with `{"turn": N}` — at the start of each agentic loop iteration
  - `agent_message` — for text content in responses
  - `agent_reasoning` — for reasoning/thinking content
  - `agent_tool_start` / `agent_tool_complete` — around tool executions
- **Retry and error handling**: Same retry semantics, error classification (retryable vs. fatal), and timeout behavior
- **Output contract**: Same `AgentOutput` structure with consistent field population (model, tokens, input_tokens, output_tokens, content)
- **Tool execution**: Same MCP tool calling interface and result handling
- **Session management**: Same lifecycle (`validate_connection()`, `execute()`, `close()`)

When modifying any provider, check all other providers for the same change. The dashboard, JSONL logger, console subscriber, and workflow engine all depend on consistent behavior across providers.
