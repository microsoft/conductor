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
uv run conductor checkpoint list       # list available checkpoints

# Validate a workflow
uv run conductor validate examples/simple-qa.yaml
make validate-examples    # validate all examples
```

## Releasing

Releases are tag-triggered: pushing a `v*` tag runs
[`.github/workflows/release.yml`](.github/workflows/release.yml), which lints,
typechecks, tests (Python 3.12 + 3.13), builds the package, and creates a GitHub
Release with artifacts and auto-generated notes. The maintainer prepares a
release-prep PR (`chore(release): cut X.Y.Z`) that bumps `version` in
`pyproject.toml`, finalizes `CHANGELOG.md` (Unreleased → versioned section), and
re-locks `uv.lock` (`uv lock`); after it merges, tag the merge commit on `main`
and push the tag. The version lives only in `pyproject.toml` (read at runtime via
`importlib.metadata`); there is no separate `__version__` to edit. The default
bump is the patch ("build") number. See
[`docs/release-checklist.md`](docs/release-checklist.md) for the full
step-by-step checklist.

## Architecture

### Core Package Structure (`src/conductor/`)

- **cli/**: Typer-based CLI. Hot-path verbs stay flat (`run`, `resume`, `validate`, `show`, `stop`, `replay`, `update`, `doctor`); the long tail is grouped under noun sub-apps registered via `app.add_typer(...)` — `registry`, `checkpoint` (→ `list`), and `gate` (→ `respond`) — with `rich_help_panel=` organising the root `--help` into *Run & Recover* / *Author & Inspect* / *Environment* / *Interact* / *State* sections (rendered order; determined by first-occurrence order of commands in the Click command list). The old flat commands `checkpoints` and `gate-respond` remain as **hidden deprecated aliases** that print a stderr deprecation warning (noting removal in a future release) and forward to the new grouped commands via a shared impl (issue #275).
  - `app.py` - Main entry point, defines the Typer application, flat commands, and the hidden `checkpoints`/`gate-respond` deprecation aliases
  - `checkpoint.py` - `checkpoint` group (`checkpoint list`) + shared `_list_checkpoints_impl` (modeled on `registry.py`)
  - `gate.py` - `gate` group (`gate respond`) + shared `_gate_respond_impl` (modeled on `registry.py`)
  - `registry.py` - `registry` group (`list` / `add` / `remove` / `set-default` / `update` / `show`)
  - `doctor.py` - `doctor` diagnostics rendering (thin presentation layer over `providers/diagnostics.py`)
  - `run.py` - Workflow execution command with verbose logging helpers
  - `bg_runner.py` - Background process forking for `--web-bg` mode. Captures the detached child's stdout/stderr to `$TMPDIR/conductor/conductor-<name>-<ts>-<runid>.bg.{stderr,stdout}.log` so silent crashes (uncaught Python exceptions, `faulthandler` dumps) leave a forensic trail — DEVNULL is **not** used for stdout/stderr. Passes `CONDUCTOR_RUN_ID`, `CONDUCTOR_BG_STDERR_LOG`, and `CONDUCTOR_BG_STDOUT_LOG` to the child via env so the child's `EventLogSubscriber` shares a run id with the bg log files and surfaces both paths in `workflow_started` system metadata. Returns a `BackgroundLaunch` dataclass (`url`, `stderr_log`, `stdout_log`, `run_id`).
  - `pid.py` - PID file utilities for tracking/stopping background processes
  - `update.py` - Update check and version comparison. Upgrades are delegated to the install script (`install.ps1`/`install.sh`); in-process self-upgrade was removed because on Windows the running Python interpreter sits inside the venv `uv tool install --force` is trying to recreate, which fails with "Access is denied". `conductor update` prints the OS-appropriate install-script one-liner; `conductor update --apply` spawns the installer detached (Windows: new console window; POSIX: `os.execvpe` replace) and exits the current process so file locks release. The startup hint is suppressed by `CONDUCTOR_NO_UPDATE_CHECK=1`, `--silent`, `--help`/`--version`, and the `update` subcommand itself.

- **config/**: YAML loading and Pydantic schema validation
  - `schema.py` - Pydantic models for all workflow YAML structures (WorkflowConfig, AgentDef, ParallelGroup, ForEachDef, etc.)
  - `loader.py` - YAML parsing with environment variable resolution (${VAR:-default}) and `!file` tag support
  - `validator.py` - Cross-reference validation (agent names, routes, parallel groups)

- **skills/**: Skill registry and loader (opt-in, bundled skill content)
  - `registry.py` - Resolves built-in skill names to on-disk directories (probes editable-install + wheel-install layouts)
  - `loader.py` - Reads `SKILL.md` + `references/*.md` for providers that require eager preamble injection; wraps each skill in `<skill name="...">` tags inside a `<skills>` envelope
  - Built-in skills live under `plugins/conductor/skills/<name>/` (bundled into the wheel via hatchling `force-include`)

- **engine/**: Workflow execution orchestration
  - `workflow.py` - Main `WorkflowEngine` class that orchestrates agent execution, parallel groups, for-each groups, and routing
  - `context.py` - `WorkflowContext` manages accumulated agent outputs with three modes: accumulate, last_only, explicit
  - `router.py` - Route evaluation with Jinja2 templates and simpleeval expressions
  - `limits.py` - Safety enforcement (max iterations, timeout)
  - `checkpoint.py` - Checkpoint save/load/list/cleanup + resume support. `save_checkpoint(error=..., trigger=...)` writes a top-level `trigger` typed `CheckpointTrigger = Literal["failure", "periodic"]`; `error=None` (periodic) writes null `failure.error_type`/`message`. No `CHECKPOINT_VERSION` bump — `trigger` is additive and any unknown/missing on-disk value normalizes to `"failure"` on load. `rotate_periodic_checkpoints` / `cleanup_periodic_for_run` both delegate to `_delete_periodic_checkpoints(..., keep_last, action)` (cleanup == rotate with `keep_last=0`), scoped to `trigger == "periodic"` **and** an exact `run_id` match so failure checkpoints and other runs' files are never touched. `find_latest_checkpoint` returns `list_checkpoints(...)[0]` (newest by microsecond `created_at`, not filename) so resume-latest isn't fooled by same-second periodic checkpoints. The engine saves a checkpoint inside its main-loop exception handlers; for a dashboard Stop/Kill that *cancels* the engine task from the CLI wrapper (bypassing those handlers), `WorkflowEngine.handle_dashboard_stop(message)` is invoked from `cli/run.py::_execute_with_stop_signal` after the cancelled task is drained — it writes a best-effort checkpoint and emits a single `workflow_failed` (flagged `stopped_by_user: true`, plus `checkpoint_path` or `checkpoint_unavailable_reason`). `handle_dashboard_stop` is idempotent via a dedicated `_dashboard_stop_handled` flag (not `_last_checkpoint_path`, which periodic checkpoints also set). Issues #244, #245. The listing command is `conductor checkpoint list` (the flat `conductor checkpoints` is a hidden deprecated alias, issue #275).
  - `validator.py` - `OutputValidator` runs the optional per-agent `validator:` block (issue #220): a second LLM call (synthetic agent via `provider.execute`, no tools, `{passed, issues}` schema) that grades the primary output against `criteria`. Fail-open on error/parse failure. The engine helper `WorkflowEngine._apply_validator` (in `workflow.py`) wires it into the main loop, parallel groups, and for-each loops; emits `agent_validator_start` / `agent_validator_complete` / `agent_validation_failed`; records a separate `"<agent> (validator)"` usage row; and re-runs the primary once with a `## Validation feedback` section on failure (`max_retries` hard-capped at 1).

- **executor/**: Agent execution
  - `agent.py` - `AgentExecutor` handles prompt rendering, tool resolution, and output validation for single agents
  - `script.py` - `ScriptExecutor` runs shell commands as workflow steps, capturing stdout/stderr/exit_code
  - `set_step.py` - `SetExecutor` evaluates Jinja2 expressions for `type: set` steps and binds typed values into the workflow context (no LLM, no subprocess). Supports single `value:` and multi `values:` forms with auto / explicit `output_type:` coercion.
  - `wait.py` - `WaitExecutor` pauses workflow execution for a parsed duration via `asyncio.sleep`. Races the sleep against the engine's `interrupt_event` so Esc/Ctrl+G cancels in-flight waits immediately; the workflow-level `limits.timeout_seconds` also cancels it via `LimitEnforcer.wait_for_with_timeout`. Output contract is strictly `{"waited_seconds": float}` per issue #218.
  - `template.py` - Jinja2 template rendering
  - `output.py` - JSON output parsing and schema validation. `validate_output` is deliberately **strict with no coercion** — it also validates `set` and `script` step output, where silently reshaping an authored value would be surprising. Response normalization belongs in `providers/_output_shape.py` instead. Note `parse_json_output` raises `ValidationError` for JSON *syntax* errors, so callers cannot distinguish syntax from schema failures by exception type alone.

- **duration.py**: `parse_duration(value)` shared helper. Accepts plain `int`/`float` seconds, or strings with `ms`/`s`/`m`/`h` suffix. Raises `ValueError` (nests cleanly inside Pydantic `ValidationError`). Rejects booleans. Bounds enforcement (e.g. > 0, 24h cap) lives in callers so the parser can be reused.

- **providers/**: SDK provider abstraction
  - `base.py` - `AgentProvider` ABC defining `execute()`, `validate_connection()`, `close()`
  - `_output_shape.py` - `normalize_agent_output(content, schema)` — the single entry point providers call before `validate_output` (issue #343). It raises `ValidationError` when the parsed response is not a JSON object (a bare `42`/`null`/array), because `validate_output` would otherwise either raise `TypeError` from a membership test (numbers, booleans, null) or report a misleading "missing required field" (strings, arrays). It then applies `unwrap_scalar_wrappers`: fires only when the schema declares `string`/`number`/`boolean`, a `dict` arrived, and **exactly one** candidate slot has the expected type. Candidate slots are the field's own name plus the generic `value`/`result` keys, deduped so a field literally named `value` or `result` isn't rejected as ambiguous against itself. Two matches count as ambiguous; any other key shape is ignored. Both are left untouched (same object identity) so the caller re-prompts rather than guessing — this is what stops `{"answer": {"error": "..."}}` being laundered into an answer. Every unwrap logs a warning, naming discarded sibling keys when there are any. Kept out of `executor/output.py` on purpose — see the note there.
  - `_recovery_prompt.py` - `build_parse_recovery_prompt(...)` — the plain-text re-prompt shared by Copilot and Hermes (issue #343). Both providers correct an unusable response the same way (error + truncated response + rendered schema, with distinct schema-failure vs syntax-failure wording), and that text is covered by the provider-parity rule, so it lives in one place instead of two copies free to drift. Claude is deliberately not a caller: it re-prompts through its `emit_output` tool and never echoes the schema, so its instruction text stays in `claude.py::_build_recovery_instruction`.
  - `copilot.py` - GitHub Copilot SDK implementation. By default spawns a nested `copilot` runtime via `CopilotClient()` (in `_build_client`, called from `_ensure_client_started`). When a runtime connection is resolved (`runtime.provider.runtime_url` or `COPILOT_PROVIDER_RUNTIME_URL`, optional `runtime_token` / `COPILOT_PROVIDER_RUNTIME_TOKEN`), it instead builds `CopilotClient(connection=RuntimeConnection.for_uri(url, connection_token=token))` to connect to an already-running `copilot --headless` process; the SDK skips spawning for URI connections and its `stop()` leaves the externally-owned server running. `_resolve_runtime_connection()` reads YAML first, then the namespaced env var (env activates on its own — the zero-YAML path for external orchestrators). Runtime transport can be combined with custom model-provider routing.
  - `claude.py` - Anthropic Claude API implementation
  - `claude_agent_sdk.py` - Claude Agent SDK implementation (uses `claude-agent-sdk` package)
  - `factory.py` - Provider instantiation

- **gates/**: Human-in-the-loop support
  - `human.py` - Rich terminal UI for human gate interactions

- **interrupt/**: Interactive workflow interruption (Esc/Ctrl+G to pause)
  - `listener.py` - Keyboard listener daemon thread for Esc/Ctrl+G detection

- **web/**: Real-time web dashboard for workflow visualization
  - `server.py` - FastAPI + uvicorn server with WebSocket broadcasting, late-joiner state replay, and `POST /api/stop` + `POST /api/kill` endpoints. `/api/stop` interrupts/pauses the current agent (a user can then Resume or Kill); if it arrives before the engine binds its interrupt event, it is latched via `_pending_stop` and drained by `set_interrupt_event` so the startup window takes the graceful pause path instead of a progress-losing hard stop. `/api/kill` hard-stops the run. Whenever a stop/kill actually *terminates* the run (cancels the engine task), it routes through `handle_dashboard_stop` so a best-effort checkpoint is written (or its absence explained) — see `handle_dashboard_stop` above (issue #245).
  - `frontend/` - React 19 + TypeScript dashboard source (Vite + Tailwind). Renders the workflow DAG with **React Flow** (`@xyflow/react`) laid out via dagre, a Zustand store (`stores/workflow-store.ts`), an agent detail panel, and streaming activity. Build with `make build-frontend` (outputs to `static/`); unit tests via `make test-frontend` (Vitest). Subworkflow nodes support inline expand/collapse (nested React Flow containers, collapsed by default) in addition to double-click drill-down — see issue #314.
  - `static/` - Built dashboard assets served by `server.py` (generated from `frontend/` by `make build-frontend`; committed to the repo). Not edited by hand.

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
- **Reasoning effort**: `runtime.default_reasoning_effort` sets a workflow-wide default; per-agent `reasoning.effort` overrides it. Allowed values: `low`, `medium`, `high`, `xhigh`, `max`. Each provider translates the unified value to its native API (Copilot: `reasoning_effort` on the session, validated against the model's `supported_reasoning_efforts`; Claude: extended thinking with budget mapping low=2048, medium=8192, high=16384, xhigh=32768, max=59904 tokens, with `temperature` coerced to 1.0 and `max_tokens` bumped to fit the budget). `max` is Copilot/Claude-only — the Hermes provider advertises only the first four levels in `CAPABILITIES.reasoning_effort` and re-checks the resolved effort against that tuple at execute time (in addition to the static `conductor validate` cross-check), so `max` is rejected on Hermes both statically and at runtime, including when it only resolves to `max` after Jinja template rendering. See `examples/reasoning-effort.yaml`.
- **Periodic checkpoints** (`runtime.checkpoint`, issue #244): opt-in `CheckpointConfig` (`every_agent: bool`, `every_seconds: int|None`, `keep_last: int=5`; `is_enabled = every_agent or every_seconds is not None`). Off by default → failure-only behavior preserved. `WorkflowEngine._maybe_save_periodic_checkpoint()` is called once at the **top of `_execute_loop`** (single choke point), where prior outputs are committed and `_current_agent_name` is the step *about to run* — so a periodic checkpoint reuses failure-checkpoint `current_agent` semantics and resume continues forward with no special-casing. Gated via the `_periodic_checkpoints_active` property (**root engine only**, `_subworkflow_depth == 0`, + `is_enabled`) and skips the first iteration (`limits.current_iteration == 0`). The save decision is `_periodic_checkpoint_due(now)` (`every_agent` OR `every_seconds` throttle; first save always fires). `_save_checkpoint_on_failure` and the periodic path share `_write_checkpoint(error, trigger)` (which best-effort-guards provider `get_session_ids()` so it never raises). The periodic save wraps write+emit+rotate; on any failure it calls `_record_periodic_checkpoint_failure()` which emits a **`checkpoint_save_failed`** event (consecutive-failure count; surfaced by `ConsoleEventSubscriber` + JSONL + dashboard) so a recovery-reliant user isn't silently left without checkpoints. After a save the engine calls `rotate_periodic_checkpoints`; at a terminal **non-resumable** outcome (clean completion via `run()`/`resume()`, or an explicit `status: failed` terminate) `_cleanup_run_periodic_checkpoints()` deletes the run's periodic checkpoints (an unexpected failure leaves them in place alongside the failure checkpoint). `conductor checkpoint list` shows a `Trigger` column and `—` for periodic rows' error type. See `examples/periodic-checkpoints.yaml` and `docs/workflow-syntax.md` (Periodic Checkpoints section).
- **Skills**: `runtime.skills: [name, ...]` sets a workflow-wide default list of skills enabled for every provider-backed agent; per-agent `skills: [name, ...]` overrides it (tri-state via list presence: omitted = inherit, `skills: []` = explicit opt-out, `skills: [name, ...]` = explicit set). Skill names must resolve to a registered built-in (currently just `conductor`). The observable contract is the same across providers — *"the agent has access to the named skill"* — but the mechanism differs by provider via `AgentProvider.supports_native_skills`: **Copilot** (`True`) registers the skill directory on the SDK session via `skill_directories`, so the agent discovers and loads skill content natively (progressive disclosure via `SKILL.md` frontmatter); **Claude Agent SDK** (`True`) is also native but goes through the Claude Code *plugin* surface — `providers/claude_agent_sdk.py::_resolve_skill_plugins` maps each resolved skill directory back to the plugin that owns it (`skills/registry.py::resolve_skill_plugin` walks up for `.claude-plugin/plugin.json`), registers that root via `ClaudeAgentOptions.plugins` and enables the skill by its `<plugin>:<skill>` name via `ClaudeAgentOptions.skills`; **Claude** (`False`) eagerly injects every enabled skill's `SKILL.md` plus `references/*.md` into the agent's rendered prompt inside `<skills><skill name="...">...</skill></skills>` tags. Providers also declare `skills: bool` on their `ProviderCapabilities` descriptor so `conductor validate` can catch skills-against-unsupported-provider mismatches. Built-in skills live under `plugins/conductor/skills/<name>/` and are bundled into the wheel via the hatchling `force-include` entries in `pyproject.toml` — both the skill body **and** `plugins/conductor/.claude-plugin/`, because without the manifest no plugin root resolves and every skills-enabled agent on `claude-agent-sdk` fails with a `ProviderError`. Skills are rejected on non-provider-backed step types (script, wait, set, terminate, workflow, human_gate). See `examples/skills-self-improving-workflow.yaml`.
- **Terminate steps** (`type: terminate`): explicit terminal step with `status` (`success` | `failed`), Jinja2 `reason`, and optional `output_template` (a `dict[str, str]` that replaces `workflow.output:` when set; each value is rendered then passed through `_maybe_parse_json` so `"true"` becomes `True`, `"42"` becomes `42`, JSON literals are parsed). Reaching a terminate step ends the workflow immediately (no routes evaluated after). `success` → CLI exit 0, dashboard ✅, `workflow_completed { termination_reason, terminated_by, is_explicit: true, status }`; runs `on_complete` hook. `failed` → CLI exit 1 (with rendered output JSON still printed to stdout for downstream tooling), dashboard ❌, raises `WorkflowTerminated` (subclass of `ExecutionError`), emits `workflow_failed { error_type: "WorkflowTerminated", is_explicit: true, status, output }`, runs `on_error` hook, and **does not** save an on-failure checkpoint (explicit terminations are intentionally non-resumable). Terminate steps cannot have `routes`, `tools`, `output`, `prompt`, `model`, etc.; cannot be used as parallel-group members or as a for_each inline agent (route to one from those groups' `routes:` instead). Inside a sub-workflow, a `status: failed` terminate is downgraded at the parent boundary to `SubworkflowTerminatedError` (also a subclass of `ExecutionError`) preserving the child's rendered `terminated_output` / `terminated_reason` / `terminated_by` as structured attributes — the parent treats it as a normal sub-workflow failure (its own `workflow_failed` does NOT inherit `is_explicit: true`). For more detail see `examples/terminate.yaml`, `docs/workflow-syntax.md` (Terminate Steps section), and `plugins/conductor/skills/conductor/references/authoring.md`.
- **Structured `runtime.provider` (Copilot custom routing)**: `runtime.provider` accepts either the bare string shorthand (`provider: copilot`) or a structured `ProviderSettings` object that routes the Copilot SDK at OpenAI-compatible / Azure / Anthropic endpoints (Ollama, vLLM, LM Studio, Azure OpenAI, etc.). Object fields: `name` (defaults to `copilot`), `type` (`openai`|`azure`|`anthropic`), `wire_api` (`completions`|`responses`), `base_url`, `api_key`, `bearer_token`, `headers`, `azure.api_version`. `api_key` and `bearer_token` are `SecretStr` (redacted in `model_dump` / dashboard / event logs). The model is frozen after construction. Custom routing activates only when at least one non-`name` field is set in YAML — ambient `OPENAI_*` env vars never divert default routing on their own. Once activated, missing fields fall back from env vars in this order: `base_url` ← `COPILOT_PROVIDER_BASE_URL` → `OPENAI_BASE_URL`; `api_key` ← `COPILOT_PROVIDER_API_KEY` (only — ambient `OPENAI_API_KEY` is intentionally NOT a fallback to avoid credential leaks); `bearer_token` ← `COPILOT_PROVIDER_BEARER_TOKEN`. The schema rejects every non-`name` field when `name != "copilot"` (structured config for other providers is a follow-up). It also rejects anchorless / broken combinations that would silently no-op at the SDK boundary: `wire_api` / `type` / `headers` / `azure` cannot stand alone without `base_url` / `api_key` / `bearer_token`; empty `headers`, empty `SecretStr`, and `azure: {api_version: null}` are rejected. The resolver raises `ProviderError` when custom routing is activated but every resolved field is falsy (e.g. expected env vars all unset). Custom routing applies to both agent execution and dialog turns so all sessions hit the same endpoint. `--provider <name>` CLI override replaces the whole `ProviderSettings` (logs a notice when YAML had structured fields). See `examples/copilot-local-llm.yaml`.
- **Connect to an existing Copilot runtime (Copilot)**: `runtime.provider.runtime_url` (Copilot-only) points the provider at an already-running `copilot --headless` process instead of spawning a nested one. Agents share the authenticated runtime process while retaining separate SDK sessions. Optional `runtime_token` (`SecretStr`, redacted, requires `runtime_url`) is the socket connection secret. Both fields fall back to env vars (`COPILOT_PROVIDER_RUNTIME_URL` / `COPILOT_PROVIDER_RUNTIME_TOKEN`) which activate the connection on their own (zero-YAML path for external orchestrators). `has_external_runtime()` is a separate axis from `has_custom_routing()`; the two can be combined because runtime transport and per-session model routing are independent. `has_structured_config()` keeps either mode from collapsing to bare-string serialization. Schema rejects: `runtime_token` without `runtime_url`; empty or whitespace-only runtime values; either field when `name != "copilot"`. Provider layer: `_resolve_runtime_connection()` (YAML then env) and `_build_client()` (in `copilot.py`). See `examples/copilot-existing-runtime.yaml` and `docs/configuration.md` (Connecting to an Existing Copilot Runtime).
- **Validator block** (`validator:` on a provider-backed agent, issue #220): semantic output validation with retry-once. After the primary agent completes, `WorkflowEngine._apply_validator` runs `OutputValidator` (`engine/validator.py`) — a second LLM call (synthetic agent via `provider.execute`, `tools=[]`, `{passed, issues}` output schema) grading the output against `validator.criteria`. Fields: `criteria` (required, non-empty), `model` (defaults to the agent's model), `max_retries` (`Field(1, ge=0, le=1)` — hard-capped at 1; `0` = report-only). On `passed: false` and `max_retries > 0`, the primary re-runs **once** via `executor.execute` with a `## Validation feedback` section (the issues) appended to `guidance_section`; the second output is final (no second validation loop). **Fail-open**: validator errors / unparseable responses → treated as pass with a logged warning. Wired into the main loop, parallel groups, and for-each loops (guarded by `agent.validator and not output.partial`; for-each passes a `usage_label` so the row matches `<group>[<key>]`). Emits `agent_validator_start` / `agent_validator_complete { passed, issues, errored, tokens, cost_usd }` / `agent_validation_failed { issues, will_retry }` through the per-agent `event_callback` (so for-each events carry `item_key`). Cost: the validation call and any discarded first attempt are recorded as a separate `"<agent> (validator)"` usage row (primary row = effective output). Rejected on `script` / `human_gate` / `workflow` / `wait` / `set` / `terminate` types. Frontend: event types in `web/frontend/src/types/events.ts`, store handlers + `NodeData.validator_*` fields in `web/frontend/src/stores/workflow-store.ts`, detail UI in `web/frontend/src/components/detail/ValidatorDetail.tsx` (rebuild with `make build-frontend`). See `examples/validator.yaml` and `docs/workflow-syntax.md` (Validator section).
- **Tool output limits** (`runtime.tool_output`): per-result MCP tool output size limiting. Controls character-size truncation and spill-to-file behavior for individual MCP tool result payloads. Config fields are `enabled` (defaults to `true`), `max_chars` (defaults to `50000`, minimum `1000`), `spill_to_file` (defaults to `true`), and `spill_dir` (defaults to `null` to resolve to OS temp directory `/conductor/tool-output`). Crucially, this limit is a **per-result** cap applied to each tool result independently, not a cumulative context window budget. Multiple truncated results, combined with prompt and conversation history, can still exceed the model's context window. Tuning should be done via `max_chars` or `max_agent_iterations` to keep context consumption in check; cumulative context budgeting is out of scope. Spill files contain the raw tool output (which may include secrets) and are not deleted by Conductor (they persist in the OS temp directory). For Claude, truncation is handled conductor-side; standard file-system tools (like `file_reader` or `grep`) receive a hint path replacement to access the full content. Copilot uses its native SDK `large_output` capability, mapping `max_chars` to bytes (meaning multibyte UTF-8 characters like CJK/emoji may truncate earlier). The `agent_tool_output_truncated` event is Claude-only because the Copilot SDK doesn't expose a truncation hook. This config is ignored by `claude-agent-sdk` (managed via native CLI `MAX_MCP_OUTPUT_TOKENS`) and doesn't apply to `hermes` (no MCP tools). See `examples/tool-output-limits.yaml` and `docs/mcp-tools.md` (Tool output limits section).
- **Dashboard stuck-reconnecting warning** (issue #330): the dashboard's WebSocket client (`web/frontend/src/hooks/use-websocket.ts`) retries forever with exponential backoff on disconnect — it drops through `wsStatus` `'disconnected'` momentarily, then oscillates between `'connecting'`/`'reconnecting'` on every retry cycle, never sitting continuously in any single non-connected status, so a naive "reconnecting for N seconds" timer would reset every cycle. Instead `workflow-store.ts`'s `setWsStatus` tracks `wsDisconnectedSince`: a timestamp set only on a *fresh* drop from `'connected'`, preserved through that churn, and cleared once reconnected. `lib/reconnect.ts`'s pure `isReconnectStuck()` (unit-tested) compares that timestamp against `RECONNECT_WARNING_THRESHOLD_MS` (60s), gated on `workflowStatus === 'running'` and not `replayMode`. `hooks/use-reconnect-warning.ts` ticks this once a second (mirroring `StatusBar`'s `idleSeconds` pattern; itself untested per the existing convention for timer-dependent hooks) and `components/layout/ReconnectWarningBanner.tsx` renders an amber banner telling the user the Conductor process may have silently crashed, pointing at whatever log location is available — `system.bg_stderr_log`/`bg_stdout_log` (captured from the root `workflow_started` event, `--web-bg` runs only) falling back to `system.log_file` (the always-on structured `*.events.jsonl` event log written by `EventLogSubscriber` for every run, unrelated to the separate `--log-file` debug-output flag), falling back to a generic hint to check the launching terminal. The banner only clears on an actual reconnect, not on a timer, so a page refresh isn't the only way to dismiss the stale "running" impression.

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
- `test_skills/` - Skill registry, loader, schema field, and executor-integration tests

Use `pytest.mark.performance` for performance tests (exclude with `-m "not performance"`).

### Test Fixture Patterns

When writing integration tests that construct `WorkflowConfig` programmatically, follow these conventions (see `tests/test_engine/test_limits.py` for canonical examples):

- `AgentDef` uses `prompt=` (not `instructions=`), `output={"key": OutputField(type="string")}` (dict, not list), and `routes=[RouteDef(...)]` (not raw dicts).
- `WorkflowDef` requires `entry_point=` and places `limits=` inside `workflow=`. `agents=` and `output=` are top-level on `WorkflowConfig`.
- The engine entry point is `await engine.run({})` (not `execute`).
- To test with controlled token/cost data, patch `provider.execute` to return a custom `AgentOutput` with explicit `input_tokens`, `output_tokens`, and `model` fields.

### Resume / Checkpoint Parity

When adding new fields to `LimitEnforcer`:

- **Transient fields** (reset each run): add to `from_dict()` as parameters sourced from the current workflow config, like `timeout_seconds`, `budget_usd`, `budget_mode`. Update the call site in `cli/run.py` → `resume_workflow_async()`.
- **Persistent fields** (survive across resume): add to both `to_dict()` and `from_dict()` deserialization, like `max_iterations`, `current_iteration`, `execution_history`.

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
- **Structured-output recovery** (issue #343): every provider with an in-session recovery loop must validate the parsed content against the declared schema **inside** that loop, not after returning. `executor/agent.py:318` is only a backstop — by the time it runs, Copilot has already disconnected its SDK session, so a schema-shape failure there is unrecoverable by construction. The loop catches both JSON syntax errors and `ValidationError`, applies `providers/_output_shape.py::unwrap_scalar_wrappers` before validating, and re-prompts via a schema-specific correction message distinct from the syntax one (Copilot and Hermes share `providers/_recovery_prompt.py::build_parse_recovery_prompt`; Claude has its own tool-oriented wording). On budget exhaustion, re-raise the original `ValidationError` (it names the field and expected type); reserve `ProviderError` for syntax failures. Each attempt emits `agent_parse_recovery` via `providers/_event_format.py::emit_parse_recovery_event`. Two traps: `parse_json_output` wraps *syntax* errors in `ValidationError` too, so the two kinds cannot be told apart by exception type (Hermes splits them by which call failed); and Claude must not validate the `_has_mcp_tool_use` path, which returns to the agentic loop rather than being a final answer.
- **Output contract**: Same `AgentOutput` structure with consistent field population (model, tokens, input_tokens, output_tokens, content)
- **Tool execution**: Same MCP tool calling interface and result handling
- **Session management**: Same lifecycle (`validate_connection()`, `execute()`, `close()`)
- **Reasoning effort**: All providers must accept the unified `reasoning.effort` field (`low` | `medium` | `high` | `xhigh` | `max`), translate it to the native API (Copilot `reasoning_effort` on the session; Claude extended `thinking` budget), validate that the selected model supports the requested effort, and raise `ValidationError` with a clear message when it does not. The one declared exception is Hermes, whose `CAPABILITIES.reasoning_effort` omits `max` (unverified upstream support) — this is not a parity violation because the provider both declares the narrower tuple and enforces it at execute time, matching the "declare the weaker value and honor it" rule in the Experimental Providers section below. Any reasoning/thinking content the model returns must be surfaced via `agent_reasoning` events so the dashboard, JSONL logger, and console subscriber render it consistently.
- **Model pricing hook** (issue #265): `AgentProvider.get_model_pricing(model) -> ModelPricing | None` is an **optional** hook (base default returns `None`) that lets a provider supply live per-model rates. Cost resolution order in `engine/pricing.py::get_pricing` is workflow `cost.pricing` override → provider hook → `DEFAULT_PRICING` → `None`. The engine bridges the async hook to the sync `UsageTracker.record` via `WorkflowEngine._ensure_pricing_resolved(agent, model)` (called before every `record()`; resolves each model once, caches on the tracker). Only Copilot implements it (derives USD from the SDK's `billing.token_prices` in AI Credits, `100 credits = $1` via `_COPILOT_USD_PER_CREDIT`); it must **never raise** (fall back to the table). `WorkflowUsage` exposes `unpriced_agents` / `unpriced_models` / `has_unpriced` so the CLI summary and dashboard surface `~$X (N agents unpriced)` instead of presenting a partial as a complete total.

When modifying any provider, check all other providers for the same change. The dashboard, JSONL logger, console subscriber, and workflow engine all depend on consistent behavior across providers.

#### `claude_agent_sdk.py` parity notes

The Claude Agent SDK provider (`claude_agent_sdk.py`) is the canonical
**experimental** provider — see the "Experimental Providers" section
below for the carve-out policy. It delegates the agentic loop to the
`claude` CLI via the `claude-agent-sdk` package. This achieves **event
and output parity** but the following are managed by the SDK rather than
Conductor:

- **Retry and error handling**: The `claude-agent-sdk` package does **not** retry API failures (429s, 5xx, network errors) internally — its built-in retry logic covers only filesystem operations. Conductor wraps SDK errors in `ProviderError` and uses `stop_reason` / error subtype to set `is_retryable`, so workflow-level `retry:` configuration drives all retry behavior. Plan for transient failures with explicit `retry:` blocks in your workflow.
- **MCP servers** (issue #335): workflow-level `runtime.mcp_servers` **are** supported. `_translate_mcp_servers` maps each `MCPServerDef`-derived dict onto the SDK's `McpStdioServerConfig` / `McpHttpServerConfig` / `McpSSEServerConfig` shapes, and the provider passes them via `ClaudeAgentOptions`. Four details are load-bearing:
  - Translation runs once in `__init__` rather than per `execute` call. Note providers are constructed **lazily** (`ProviderRegistry.get_provider` ← `WorkflowEngine._get_executor_for_agent`), so a bad server config surfaces when the first agent on this provider runs, **not** at `conductor validate` — which does not inspect per-server `tools:` filters at all.
  - The config is written to a `0600` temp file (`_write_mcp_config`) and passed **by path**. Passing the dict would make the SDK serialize it into a `--mcp-config <json>` argv element, publishing resolved stdio `env` values and http/sse `Authorization` headers to anything that can read `/proc/<pid>/cmdline`. The write happens **inside** `execute`'s `try`, so the `finally` reclaims the file on every exit path; the finally also `aclose()`s the SDK iterator first, so the `claude` subprocess is gone before its config file is. The file must use the `{"mcpServers": {...}}` envelope — the CLI rejects a bare mapping.
  - `strict_mcp_config=True` is set **unconditionally**, including when the workflow declares no servers: otherwise the CLI loads project `.mcp.json`, user-global, and plugin-provided servers, and `permission_mode` bypasses approval for whatever they expose.
  - A narrowing per-server `tools:` filter (anything other than the default `["*"]`) is **refused**, not ignored: forwarding the server unfiltered would grant more tools than declared, the same security regression that justifies refusing the per-agent allowlist. A dropped `timeout` only warns, since losing it cannot widen tool access.
- **Tool execution**: Per-agent `tools:` allowlists remain unsupported (`workflow_tools_passthrough=False`). The provider refuses any non-empty per-agent list because workflow tool names do not translate to CLI tool IDs. Note the SDK's `tools` option governs **built-in** tools only, and `allowed_tools` is a permission auto-approve list rather than an availability filter — so honoring an allowlist would require a permission-mode redesign, not just a name mapping. An agent with `tools: []` runs with no built-in tools beyond the `Skill` loader when skills are enabled (MCP servers still attach); omitting `tools:` grants the full `claude_code` preset.
- **Runtime config**: `temperature` and `max_tokens` are rejected at the factory — the CLI controls sampling behavior.
- **Working directory** (issue #348): the engine-resolved `agent.working_dir` / `runtime.working_dir` **is** forwarded, as `ClaudeAgentOptions.cwd`.
  - The SDK applies it as the `claude` subprocess's cwd (`_internal/transport/subprocess_cli.py` as of 0.2.87 passes it to `open_process` and sets `PWD`), so stdio MCP servers pick it up by **inheriting** it from that subprocess. There is deliberately no per-server stamping as in `copilot.py::_mcp_servers_for_cwd`: the SDK's `McpStdioServerConfig` has no cwd field, so `_translate_mcp_servers` is left alone. Inheritance is a property of the CLI binary, not of the SDK, so it is documented rather than asserted by a test.
  - The path is passed **verbatim** — `WorkflowEngine._resolve_agent_working_dir` has already rendered, absolutized, normalised, and existence-checked it, and re-resolving here would collapse the symlink aliases the engine preserves on purpose. The `ClaudeAgentOptions(...)` construction lives **inside** `execute`'s `try` so the `os.getcwd()` fallback can't escape as a bare `OSError` when the process cwd has been deleted (`copilot.py` resolves its cwd inside its try for the same reason).
  - There is no provider-side `is_dir()` guard: a directory that vanishes after the engine's check surfaces as the SDK's `CLIConnectionError("Working directory does not exist: <path>")`, wrapped in `ProviderError`. That is only defensible because `_classify_startup_failure` special-cases it — `CLIConnectionError` otherwise yields firewall/binary advice and `is_retryable=True`, which is wrong for all three launch failures (missing dir; path is a file → `ENOTDIR`; unreadable dir → `EACCES`; the latter two reach the SDK's generic "Failed to start Claude Code" arm, not its dedicated one).
  - Knock-on effects: cwd selects which `CLAUDE.md` and local settings the CLI loads (Conductor never sets `setting_sources`, so the CLI's load-everything default applies) and is the project key for the CLI's on-disk transcript directory. The unconditional `strict_mcp_config=True` still stops a `.mcp.json` in that directory from injecting undeclared servers, but it does **not** cover hooks or instructions — point `working_dir` only at trees you trust. `add_dirs` (the SDK's `--add-dir` passthrough) is a separate axis Conductor does not set.
- **Skills** (issue #352): `supports_native_skills=True`. Skills are enabled through the SDK, not prompt injection, and three options move together in `execute`:
  - `plugins=[{"type": "local", "path": <plugin root>}]` + `skills=["<plugin>:<skill>"]`. The SDK has no skill-*directory* surface, so `_resolve_skill_plugins` maps each directory back to the plugin that owns it via `skills/registry.py::resolve_skill_plugin`. That resolver is deliberately strict, because every one of these mistakes otherwise produces a name the CLI silently resolves to nothing: it bounds the upward walk (`_PLUGIN_SEARCH_DEPTH`), requires the skill to actually live under the candidate's `skills/` directory, requires `SKILL.md` to exist and its frontmatter `name` to equal the directory name (the CLI resolves by frontmatter name; Conductor sends the directory name), and rejects names outside `[A-Za-z0-9_.-]+` since they are joined into a comma-delimited `--allowedTools` value. A skill under no plugin root returns `None`; a plugin that is present but unusable raises `SkillPluginError`, which the provider re-raises as a `ProviderError` carrying the real reason rather than a blanket "not part of a plugin". Two plugins claiming one qualified name are refused too — deduping the clash away would drop a declared skill. All of it is `is_retryable=False`: these never become valid on a retry, and a checkout path containing "connection" would otherwise trip the retryability heuristic. Note providers are constructed lazily, so this surfaces when the first agent on this provider runs, **not** at `conductor validate`.
  - `setting_sources=[]` **unconditionally**, for the same reason `strict_mcp_config=True` is unconditional a few lines away. Left unset, the CLI loads user, project, and local settings, which between them bring in ambient skills, `CLAUDE.md`, and hooks the workflow never declared, varying by machine and launch directory. Conductor surfaces instruction files through its own opt-in `--workspace-instructions`; settings and hooks have no equivalent. `skills=[]` and `skills=None` are **not** interchangeable upstream: `None` means "CLI defaults apply", and setting `skills` while leaving `setting_sources` at `None` makes the SDK re-default it to `["user", "project"]` — so the two options are coupled and dropping either re-opens the issue. The `[]` is also invisible in argv (it travels in the SDK's `initialize` control request), which is why the argv-based tests are paired with options-level assertions. The list is a context filter, not a sandbox: undeclared skills are hidden from the model's listing but their files stay readable.
  - An explicit `tools: []` sends `--tools ""` (empty base tool set), which would leave a declared skill unreachable; `_resolve_tool_config` therefore grants back the single `Skill` tool when skills are enabled. No permission bypass is needed — the SDK auto-allows it via `Skill(<name>)` in `allowed_tools`.
  - The executor→provider seam (`executor/agent.py`) is the only thing carrying the feature now that eager injection no longer backs it up on this provider, so `tests/test_skills/test_executor_integration.py::TestSkillDirectoriesReachTheProvider` asserts directories actually arrive at `execute`. A negative "no `<skills>` in the prompt" assertion cannot tell a working native path from one that dropped the skills entirely.

#### `aca.py` parity notes

The ACA (Azure Container Apps) provider (`aca.py`) is an **experimental**
provider (issue #284) — see the "Experimental Providers" section below for
the carve-out policy. Unlike `claude_agent_sdk.py`, which delegates the
loop to a local CLI subprocess, `aca.py` delegates the **entire agentic
loop to a remote sandbox**: `AcaRuntimeProvider` is a thin host-side
transport shim that derives a session identifier, authenticates via
`DefaultAzureCredential`, and relays NDJSON event frames from an
in-container `conductor-agent-runner` (which itself wraps a real
`CopilotProvider`) verbatim to `event_callback`. Because the runner
re-emits Conductor's own event vocabulary and forwards a real
`CopilotProvider`'s output, this achieves **full event and output
parity** (`mcp_tools`, `streaming_events`, `agent_reasoning_events`, and
`reasoning_effort` are all declared `True`) — with the following carve-outs:

**Inner Copilot credential (DD4).** The sandbox's Copilot session can't do
interactive OAuth, so the host resolves a credential per request and
forwards it in-memory: `COPILOT_PROVIDER_BASE_URL` (BYOK) →
`COPILOT_GITHUB_TOKEN`/`GH_TOKEN`/`GITHUB_TOKEN` → **`gh auth token`** →
`ProviderError`. The `gh` step (`_resolve_gh_cli_token`) is what makes the
zero-setup path work and mirrors the Copilot CLI's own documented chain;
every failure mode (not installed, not signed in, timeout, empty output)
means "no token" and falls through rather than raising. Reading the
Copilot editor plugins' `~/.config/github-copilot/auth.db` is
**deliberately not implemented** — that store belongs to the Copilot
Language Server (not the CLI/SDK the runner drives), has changed format
twice, and is slated for encryption at rest. Note for tests: any test
asserting the "no credential" error **must** stub the `gh` subprocess, or
it will pick up the developer's real token (see
`TestAcaCredentialPrecedence._clear_credential_env`).

- **`workflow_tools_passthrough=False`**: the per-agent `tools:` allowlist
  is forwarded to the runner in the request body, but the in-container
  `CopilotProvider` it wraps never applies that list to the SDK session —
  every tool/MCP server available to the session is callable regardless of
  the declared allowlist. Combined with `mcp_tools=True` (the full
  configured `mcp_servers` set is always forwarded), there is no allowlist
  value the runner can honor — not even `tools: []` — so
  `config/validator.py` rejects **any** explicit `tools:` on an
  `aca`-backed agent, not just a non-empty one (review follow-up, #284
  E7). This mirrors the same declared carve-out on `claude_agent_sdk.py`
  and `hermes.py`. `hermes.py` declares `mcp_tools=False` (nothing is ever
  forwarded regardless of the list), so `tools: []` genuinely disables all
  tools and stays valid for it. `claude_agent_sdk.py` now behaves like
  `aca` whenever the workflow declares `mcp_servers`, and like `hermes`
  when it does not.
- **`working_dir=False`**: this capability field means "applies the
  generic, host-resolved `agent.working_dir` / `runtime.working_dir`" — a
  host filesystem path the engine resolves against the workflow file's
  directory. `aca` never reads that field; it only honors the separate,
  container-relative `sandbox.working_dir` block (documented in
  `docs/providers/aca.md#workflow-configuration`), which has no meaning as
  a host path and is not gated by this capability.
- **`interrupt`/`max_session_seconds` are declared `True` host-side but
  not fully backed by the shipped runner MVP (epic E4)**: the runner has
  no `/interrupt` endpoint yet (the host's in-stream interrupt POST has
  nowhere to land, so the host falls back to a best-effort session-delete
  call, itself unsupported for custom-container pools, before giving up
  waiting — not instantaneous; both cleanup calls use an explicit
  10-second per-call timeout), and `max_session_seconds` enforcement is only a
  best-effort, Copilot-internal timeout (the wrapped `CopilotProvider`'s
  own `IdleRecoveryConfig` check) — there is no independent runner-level
  guard. Stopping an `aca`-backed agent today eventually stops the host
  from *waiting*, not the sandbox from *computing*. See
  `docs/providers/aca.md#known-gaps-runner-mvp`.
- **`checkpoint_resume=False`**: ACA dynamic-sessions sessions are
  ephemeral with no volume mount, so there is nothing in-sandbox for
  `conductor resume` to restore. A resumed workflow re-runs the
  `aca`-backed agent from scratch rather than continuing an interrupted
  sandbox session — the same posture `claude_agent_sdk.py` and `hermes.py`
  declare, but for a different underlying reason (remote ephemeral
  filesystem vs. local CLI process state).

Full architecture, the runner `/execute`/`/health` contract, the NDJSON
frame schema, and the credential/security model are documented in
`docs/providers/aca.md` and the source design at
`docs/projects/aca/aca-provider.design.md`.

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
- Declare `skills` accurately. Skills are **not** an allowed carve-out — a
  provider reaches `skills=True` either natively (`supports_native_skills=True`,
  forwarding the resolved skill directories to its SDK in whatever shape that
  SDK accepts) or via `AgentExecutor`'s eager preamble injection, which is
  provider-agnostic. Declare `False` only when
  neither path can work (e.g. `aca`, where skill directories are host paths the
  in-sandbox runner cannot read). `config/validator.py` cross-checks per-agent
  `skills:` and inherited `runtime.skills` against this flag, so an inaccurate
  `False` turns into a spurious validate error and an inaccurate `True` silently
  drops the skill content at run time.
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
