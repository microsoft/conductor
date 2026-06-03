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
uv run conductor update                # check for updates and print the install-script command
uv run conductor update --apply        # launch the installer automatically (conductor exits to release file locks)

# Resume a failed workflow from checkpoint
uv run conductor resume workflow.yaml                  # resume from latest checkpoint
uv run conductor resume workflow.yaml --web            # resume with dashboard
uv run conductor resume workflow.yaml --web-bg         # resume with background dashboard
uv run conductor resume workflow.yaml --provider copilot
uv run conductor resume workflow.yaml -m tracker=ado
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
  - `bg_runner.py` - Background process forking for `--web-bg` mode. Captures the detached child's stdout/stderr to `$TMPDIR/conductor/conductor-<name>-<ts>-<runid>.bg.{stderr,stdout}.log` so silent crashes (uncaught Python exceptions, `faulthandler` dumps) leave a forensic trail — DEVNULL is **not** used for stdout/stderr. Passes `CONDUCTOR_RUN_ID`, `CONDUCTOR_BG_STDERR_LOG`, and `CONDUCTOR_BG_STDOUT_LOG` to the child via env so the child's `EventLogSubscriber` shares a run id with the bg log files and surfaces both paths in `workflow_started` system metadata. Returns a `BackgroundLaunch` dataclass (`url`, `stderr_log`, `stdout_log`, `run_id`).
  - `pid.py` - PID file utilities for tracking/stopping background processes
  - `update.py` - Update check and version comparison. Upgrades are delegated to the install script (`install.ps1`/`install.sh`); in-process self-upgrade was removed because on Windows the running Python interpreter sits inside the venv `uv tool install --force` is trying to recreate, which fails with "Access is denied". `conductor update` prints the OS-appropriate install-script one-liner; `conductor update --apply` spawns the installer detached (Windows: new console window; POSIX: `os.execvpe` replace) and exits the current process so file locks release. The startup hint is suppressed by `CONDUCTOR_NO_UPDATE_CHECK=1`, `--silent`, `--help`/`--version`, and the `update` subcommand itself.

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
  - `set_step.py` - `SetExecutor` evaluates Jinja2 expressions for `type: set` steps and binds typed values into the workflow context (no LLM, no subprocess). Supports single `value:` and multi `values:` forms with auto / explicit `output_type:` coercion.
  - `wait.py` - `WaitExecutor` pauses workflow execution for a parsed duration via `asyncio.sleep`. Races the sleep against the engine's `interrupt_event` so Esc/Ctrl+G cancels in-flight waits immediately; the workflow-level `limits.timeout_seconds` also cancels it via `LimitEnforcer.wait_for_with_timeout`. Output contract is strictly `{"waited_seconds": float}` per issue #218.
  - `template.py` - Jinja2 template rendering
  - `output.py` - JSON output parsing and schema validation

- **duration.py**: `parse_duration(value)` shared helper. Accepts plain `int`/`float` seconds, or strings with `ms`/`s`/`m`/`h` suffix. Raises `ValueError` (nests cleanly inside Pydantic `ValidationError`). Rejects booleans. Bounds enforcement (e.g. > 0, 24h cap) lives in callers so the parser can be reused.

- **providers/**: SDK provider abstraction
  - `base.py` - `AgentProvider` ABC defining `execute()`, `validate_connection()`, `close()`
  - `copilot.py` - GitHub Copilot SDK implementation
  - `claude.py` - Anthropic Claude API implementation
  - `claude_agent_sdk.py` - Claude Agent SDK implementation (uses `claude-agent-sdk` package)
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
3. Engine loops: find agent/parallel/for-each/script/set/wait → execute → evaluate routes → next
4. Parallel groups execute agents concurrently with context isolation (deep copy snapshot)
5. For-each groups resolve source arrays at runtime, inject loop variables (`{{ item }}`, `{{ _index }}`, `{{ _key }}`)
6. Script steps run shell commands via asyncio subprocess, expose stdout/stderr/exit_code to context
7. Set steps render Jinja2 expressions and bind typed values to context (no LLM, no subprocess) via the shared `WorkflowEngine._run_set_step` helper, which enforces `output:` schema in all three positions (main loop, parallel group, for-each iteration) and emits `set_started` / `set_completed` / `set_failed`
8. Wait steps pause via `asyncio.sleep` (cancellable by interrupt or workflow timeout); expose `{"waited_seconds": float}` to context
9. Routes evaluated via `Router` using Jinja2 or simpleeval expressions
10. Final output built from templates in `output:` section

### Key Patterns

- **Context modes**: `accumulate` (all prior outputs), `last_only` (previous only), `explicit` (only declared inputs)
- **Failure modes** for parallel/for-each: `fail_fast`, `continue_on_error`, `all_or_nothing`
- **Route evaluation**: First matching `when` condition wins; no `when` = always matches
- **Tool resolution**: `null` = all workflow tools, `[]` = none, `[list]` = subset
- **Set step typing**: `output_type` defaults to `auto` (safe YAML parse with `_to_json_safe` normalisation — `datetime`/`date`/`time` → ISO 8601, non-string dict keys and other non-JSON-safe values raise `ExecutionError`). Explicit `string`/`number`/`integer`/`boolean`/`list`/`dict` only valid on single `value:`. `WorkflowContext.store` accepts any JSON-safe value (scalars/lists from `set` steps in addition to the dicts produced by LLM / script / gate / parallel-group outputs); `_add_agent_input` returns the scalar verbatim for `step.output` and raises a clear `KeyError` for `step.output.field` shorthand on non-dict outputs.
- **Reasoning effort**: `runtime.default_reasoning_effort` sets a workflow-wide default; per-agent `reasoning.effort` overrides it. Allowed values: `low`, `medium`, `high`, `xhigh`. Each provider translates the unified value to its native API (Copilot: `reasoning_effort` on the session, validated against the model's `supported_reasoning_efforts`; Claude: extended thinking with budget mapping low=2048, medium=8192, high=16384, xhigh=32768 tokens, with `temperature` coerced to 1.0 and `max_tokens` bumped to fit the budget). See `examples/reasoning-effort.yaml`.
- **Terminate steps** (`type: terminate`): explicit terminal step with `status` (`success` | `failed`), Jinja2 `reason`, and optional `output_template` (a `dict[str, str]` that replaces `workflow.output:` when set; each value is rendered then passed through `_maybe_parse_json` so `"true"` becomes `True`, `"42"` becomes `42`, JSON literals are parsed). Reaching a terminate step ends the workflow immediately (no routes evaluated after). `success` → CLI exit 0, dashboard ✅, `workflow_completed { termination_reason, terminated_by, is_explicit: true, status }`; runs `on_complete` hook. `failed` → CLI exit 1 (with rendered output JSON still printed to stdout for downstream tooling), dashboard ❌, raises `WorkflowTerminated` (subclass of `ExecutionError`), emits `workflow_failed { error_type: "WorkflowTerminated", is_explicit: true, status, output }`, runs `on_error` hook, and **does not** save an on-failure checkpoint (explicit terminations are intentionally non-resumable). Terminate steps cannot have `routes`, `tools`, `output`, `prompt`, `model`, etc.; cannot be used as parallel-group members or as a for_each inline agent (route to one from those groups' `routes:` instead). Inside a sub-workflow, a `status: failed` terminate is downgraded at the parent boundary to `SubworkflowTerminatedError` (also a subclass of `ExecutionError`) preserving the child's rendered `terminated_output` / `terminated_reason` / `terminated_by` as structured attributes — the parent treats it as a normal sub-workflow failure (its own `workflow_failed` does NOT inherit `is_explicit: true`). For more detail see `examples/terminate.yaml`, `docs/workflow-syntax.md` (Terminate Steps section), and `plugins/conductor/skills/conductor/references/authoring.md`.
- **Structured `runtime.provider` (Copilot custom routing)**: `runtime.provider` accepts either the bare string shorthand (`provider: copilot`) or a structured `ProviderSettings` object that routes the Copilot SDK at OpenAI-compatible / Azure / Anthropic endpoints (Ollama, vLLM, LM Studio, Azure OpenAI, etc.). Object fields: `name` (defaults to `copilot`), `type` (`openai`|`azure`|`anthropic`), `wire_api` (`completions`|`responses`), `base_url`, `api_key`, `bearer_token`, `headers`, `azure.api_version`. `api_key` and `bearer_token` are `SecretStr` (redacted in `model_dump` / dashboard / event logs). The model is frozen after construction. Custom routing activates only when at least one non-`name` field is set in YAML — ambient `OPENAI_*` env vars never divert default routing on their own. Once activated, missing fields fall back from env vars in this order: `base_url` ← `COPILOT_PROVIDER_BASE_URL` → `OPENAI_BASE_URL`; `api_key` ← `COPILOT_PROVIDER_API_KEY` (only — ambient `OPENAI_API_KEY` is intentionally NOT a fallback to avoid credential leaks); `bearer_token` ← `COPILOT_PROVIDER_BEARER_TOKEN`. The schema rejects every non-`name` field when `name != "copilot"` (structured config for other providers is a follow-up). It also rejects anchorless / broken combinations that would silently no-op at the SDK boundary: `wire_api` / `type` / `headers` / `azure` cannot stand alone without `base_url` / `api_key` / `bearer_token`; empty `headers`, empty `SecretStr`, and `azure: {api_version: null}` are rejected. The resolver raises `ProviderError` when custom routing is activated but every resolved field is falsy (e.g. expected env vars all unset). Custom routing applies to both agent execution and dialog turns so all sessions hit the same endpoint. `--provider <name>` CLI override replaces the whole `ProviderSettings` (logs a notice when YAML had structured fields). See `examples/copilot-local-llm.yaml`.

### Debugging `--web-bg` failures

When a `conductor run --web-bg` (or `resume --web-bg`) child dies before
the dashboard becomes reachable, or crashes mid-run, look at:

1. The child's captured stderr log, printed alongside the dashboard URL
   on a successful launch and included in every `RuntimeError` message
   on a failed launch. The path is also stamped into the child's
   `workflow_started` event under `system.bg_stderr_log` and surfaced
   in the web dashboard.
2. The matching `.events.jsonl` file in the same directory — same
   timestamp and 8-hex run id in the filename, so the three artefacts
   (`.events.jsonl`, `.bg.stderr.log`, `.bg.stdout.log`) sort together.
3. For an apparent silent crash, search the events JSONL for a
   `workflow_failed` event; the `is_base_exception` flag tells you
   whether the failure escaped the engine's normal `Exception` handling
   (e.g. a `SystemExit` from a misbehaving library).

`faulthandler` is enabled at import time in `conductor/__init__.py`, so
a native crash also dumps a Python stack trace to the captured stderr
log. See issue #116.

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

All providers must maintain feature parity where applicable. Any change to one provider's behavior, contract, or capabilities must be applied to all providers. This includes:

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
- **Reasoning effort**: All providers must accept the unified `reasoning.effort` field (`low` | `medium` | `high` | `xhigh`), translate it to the native API (Copilot `reasoning_effort` on the session; Claude extended `thinking` budget), validate that the selected model supports the requested effort, and raise `ValidationError` with a clear message when it does not. Any reasoning/thinking content the model returns must be surfaced via `agent_reasoning` events so the dashboard, JSONL logger, and console subscriber render it consistently.

When modifying any provider, check all other providers for the same change. The dashboard, JSONL logger, console subscriber, and workflow engine all depend on consistent behavior across providers.

#### `claude_agent_sdk.py` parity notes

The Claude Agent SDK provider (`claude_agent_sdk.py`) is the canonical
**experimental** provider — see the "Experimental Providers" section
below for the carve-out policy. It delegates the agentic loop to the
`claude` CLI via the `claude-agent-sdk` package. This achieves **event
and output parity** but the following are managed by the SDK rather than
Conductor:

- **Retry and error handling**: The `claude-agent-sdk` package does **not** retry API failures (429s, 5xx, network errors) internally — its built-in retry logic covers only filesystem operations. Conductor wraps SDK errors in `ProviderError` and uses `stop_reason` / error subtype to set `is_retryable`, so workflow-level `retry:` configuration drives all retry behavior. Plan for transient failures with explicit `retry:` blocks in your workflow.
- **Tool execution**: Tools and MCP servers are managed by the `claude` CLI's own configuration. The provider rejects workflow-level `runtime.mcp_servers` at the factory and refuses any non-empty per-agent `tools:` list (workflow tool names do not translate to CLI tool IDs). An agent with `tools: []` runs with no tools; omitting `tools:` grants the full `claude_code` preset.
- **Runtime config**: `temperature` and `max_tokens` are rejected at the factory — the CLI controls sampling behavior.

### Experimental Providers

Some providers delegate part of the agentic loop to an upstream SDK or
framework and cannot honor every parity rule above. Rather than reject
them or let parity silently erode, Conductor formalizes an
**experimental tier** with explicit allowed carve-outs and a static
validator that catches workflow ↔ provider mismatches at `conductor
validate` time. See `docs/providers/experimental.md` for the full
stability policy.

**Capability declaration.** Every provider — stable or experimental —
declares a class-level `CAPABILITIES: ProviderCapabilities` attribute
(see `src/conductor/providers/capabilities.py`). The descriptor is a
contract: behavior must match what the provider declares. Lying in the
descriptor undermines the framework.

**Allowed carve-outs** for experimental providers (declared as `False` /
`None` on the descriptor):

- `mcp_tools` — workflow-level `runtime.mcp_servers` is not forwarded
- `workflow_tools_passthrough` — per-agent `tools:` allowlist is not enforced
- `streaming_events` — events emitted only at completion (not incrementally)
- `agent_reasoning_events` — no thinking/reasoning event surfacing
- `reasoning_effort` — provider has no reasoning-effort concept
- `structured_output: "prompt_injection"` — schema enforced via prompt injection only
- `interrupt` — mid-call interrupt not honored (still cancels between iterations)
- `max_session_seconds` — wall-clock session timeout silently ignored
- `checkpoint_resume` — session state does not survive `conductor resume`

**Non-negotiable rules** experimental providers MUST uphold:

- `AgentProvider` lifecycle (`validate_connection` / `execute` / `close`).
- `AgentOutput` shape on every successful execution (fields may be `None`).
- Raise real exceptions on real errors — no silent failure swallowing.
- Declare accurate `ProviderCapabilities` matching observed behavior.
- Provide a smoke test that exercises construct + execute paths against
  a mocked SDK.
- Maintain `concurrent_safe: true`, or fail validation when used in
  parallel/for_each groups with `max_concurrent > 1`.

**Promotion criteria** (experimental → stable) are documented in
`docs/providers/experimental.md` — full parity capabilities, named
maintainer, real-API integration test, ≥6 months stable upstream,
end-to-end example workflow.

### Run / Resume Parity

The `run` and `resume` commands must accept the same flags wherever a flag is meaningful for a resumed run. When adding a new flag to `run`, add it to `resume` too unless there's a specific reason it cannot apply.

Flags that **must** be mirrored on both:

- `--provider` / `-p` — runtime provider override
- `--metadata` / `-m` — CLI metadata merged on top of YAML metadata
- `--skip-gates` — auto-select first option at human gates
- `--log-file` / `-l` — debug log file path (`auto` or explicit)
- `--no-interactive` — disable Esc-to-pause keyboard listener
- `--web` — start the real-time web dashboard
- `--web-port` — dashboard port (0 = auto-select)
- `--web-bg` — fork a detached process running the workflow + dashboard

Flags intentionally **not** mirrored on `resume` (and why):

- `--input` / `-i` — workflow inputs are restored from the checkpoint context; supplying them at resume would conflict.
- `--workspace-instructions`, `--instructions` — the `instructions_preamble` is persisted in the checkpoint and restored verbatim; re-supplying would be ambiguous.
- `--dry-run` — resume executes from a saved point and is incompatible with planning-only output.

Implementation parity rules:

- The async helpers (`run_workflow_async` and `resume_workflow_async` in `cli/run.py`) must wire up the same event emitter, JSONL event log subscriber, console event subscriber, and `WebDashboard` lifecycle.
- The `WorkflowEngine` constructor receives the same kwargs in both paths (`event_emitter`, `web_dashboard`, `run_context`, `interrupt_event`, `keyboard_listener`, `instructions_preamble`).
- Background-process forking lives in `cli/bg_runner.py`. `run --web-bg` calls `launch_background()` and `resume --web-bg` calls `launch_background_resume()`. Both must forward equivalent options and write a PID file via `cli/pid.py`.
- Note: on resume, the dashboard is seeded with prior events before it starts accepting clients. The CLI prepends a fresh `workflow_started` event built from the **current** workflow YAML (via `WorkflowEngine.build_workflow_started_data()`) so historical events apply to the correct topology; it then either replays the original JSONL event log (`WebDashboard.replay_events_from_jsonl()` — when the checkpoint records an `event_log_path` and the file exists) or synthesises minimal `*_started` / `*_completed` pairs from the restored `WorkflowContext` (`replay_synthetic_from_context()`). The resumed engine's own `workflow_started` emit is suppressed via `engine.suppress_workflow_started_emit()` so the dashboard sees exactly one root `workflow_started` (no `wfDepth` double-count). Root-level lifecycle events from the original JSONL (`workflow_started` / `workflow_completed` / `workflow_failed` / `checkpoint_saved`) are filtered out on replay; subworkflow-level lifecycle events are preserved so frontend `wfDepth` stays balanced. The resumed `EventLogSubscriber` opens the original JSONL in append mode (when available) so a multi-resume session produces one continuous log file and `run_id` stays stable for log-correlation tools.
