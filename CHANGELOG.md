# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased](https://github.com/microsoft/conductor/compare/v0.1.18...HEAD)

### Fixed

- `human_gate` agents: the dict returned by `prompt_for` text-collection fields
  is no longer spread into the gate's output root, where it could silently
  overwrite the reserved `selected` key (e.g. an option declaring
  `prompt_for: selected` would clobber the chosen option value with whatever
  the user typed). Collected values are now nested under an explicit
  `additional_input` key, matching the shape the `gate_resolved` event already
  used. ([#237](https://github.com/microsoft/conductor/pull/237))

### Changed

- **BREAKING (templates)** — `human_gate` output shape changed.
  - Before: `{{ <gate>.output.<prompt_for_field> }}` (root-level).
  - After: `{{ <gate>.output.additional_input.<prompt_for_field> }}` (nested).
  - Gates without any `prompt_for` now produce `additional_input: {}` rather
    than just `{"selected": ...}` — the key is always present.
  - `<gate>.output.selected` is unchanged.
  - Templates that referenced the old flat path now raise `TemplateError`
    (`StrictUndefined`), so the migration fails loudly rather than rendering
    to empty strings.
  - In `context: explicit` mode, `input:` declarations support
    `<gate>.output.additional_input` (the parent dict) but not the dotted
    shorthand `<gate>.output.additional_input.<field>`. Declare the parent
    and read individual fields via Jinja2 in the consuming agent's prompt or
    output template.

## [0.1.18](https://github.com/microsoft/conductor/compare/v0.1.17...v0.1.18) - 2026-05-28

### Added
- New `type: set` workflow step that evaluates Jinja2 expressions and binds
  the results into the workflow context — no LLM call, no subprocess, no I/O.
  Two surface forms: `value:` (single expression bound as `<step>.output`,
  scalar / list / dict by auto-detection or explicit `output_type:`) and
  `values:` (named bindings rendered in one pass against the pre-step context
  and bound as `<step>.output.<key>`). Type detection defaults to YAML
  auto-parsing with a JSON-safety pass that converts `datetime`/`date`/`time`
  to ISO 8601 strings and raises `ExecutionError` on other non-JSON-safe
  values (including non-string dict keys) so checkpoint round-trips stay
  stable. Explicit `output_type:` (single-`value` only) supports `string`,
  `number`, `integer`, `boolean`, `list`, `dict`. The engine dispatches set
  steps in the main loop, parallel groups, and for-each groups via the
  shared `_run_set_step` helper, emitting `set_started` / `set_completed` /
  `set_failed` and enforcing the `output:` schema (rejected for scalar
  outputs with a friendly suggestion). `WorkflowContext.store` was widened
  to accept any JSON-safe value; `_add_agent_input` returns scalars verbatim
  for `step.output` and raises a clear `KeyError` for `step.output.field`
  shorthand on non-dict outputs. The web dashboard adds a dedicated `SetNode`
  (variable icon, key count / value preview) and `SetDetail` panel showing
  output type, bindings, and rendered value. New `examples/set-step.yaml`
  demonstrates single + multi binding plus a boolean route on the derived
  flag
  ([#226](https://github.com/microsoft/conductor/pull/226),
  closes [#221](https://github.com/microsoft/conductor/issues/221)).
- New `type: wait` workflow step that pauses execution for a parsed
  duration via in-process `asyncio.sleep`. Cross-platform — no shell
  `sleep` dependency. Use for rate-limit cooldowns, polling intervals,
  external-system catch-up, and demos. The `duration:` field accepts
  plain numbers (seconds), suffixed strings (`"500ms"`, `"60s"`,
  `"2.5m"`, `"1h"`), or a Jinja2 template that renders to one of
  those (e.g. `"{{ workflow.input.poll_interval }}s"`). Schema enforces
  `0 < duration <= 24h` and rejects boolean values pre-coercion.
  `Esc` / `Ctrl+G` cancels in-progress waits immediately (the engine
  races the sleep against the interrupt event), and the workflow-level
  `limits.timeout_seconds` also cancels them. Wait steps emit
  `wait_started` / `wait_completed` / `wait_failed` events alongside
  the generic `agent_started` (with `agent_type: "wait"`), so existing
  dashboards keyed on agent lifecycle pick them up automatically. The
  dashboard adds a dedicated `WaitNode` (clock icon) and `WaitDetail`
  panel that show the requested duration, actual elapsed time, reason,
  and an "interrupted" indicator. The public output contract is strict
  — only `{"waited_seconds": float}` is exposed to workflow context;
  extra metadata lives in event payloads. Wait steps count toward
  `limits.max_iterations` (each pause is one step) but are not subject
  to `max_agent_iterations` (per-LLM-agent tool counter). Wait cannot
  be used inside `parallel` or `for_each` groups. New `examples/wait-step.yaml`
  demonstrates a polling pattern with a templated poll interval and
  route loop-back
  ([#224](https://github.com/microsoft/conductor/pull/224),
  closes [#218](https://github.com/microsoft/conductor/issues/218)).
- New `type: terminate` workflow step that explicitly ends the workflow with
  a structured `status` (`success` | `failed`) and Jinja2-rendered `reason`,
  plus an optional `output_template` (`dict[str, str]`) that replaces the
  workflow-level `output:` mapping for that termination path. Reaching a
  terminate step ends the workflow immediately (no routes evaluated after).
  `status: success` returns the rendered output cleanly (CLI exit 0,
  dashboard ✅, emits `workflow_completed { termination_reason, terminated_by,
  is_explicit: true, status: "success" }`); `status: failed` raises a new
  `WorkflowTerminated` exception (`ExecutionError` subclass), gives the CLI a
  non-zero exit code while still printing the rendered output JSON to stdout
  for downstream tooling, and intentionally **skips** the on-failure
  checkpoint save because explicit termination is not a resumable transient
  failure. Inside a sub-workflow, a failed terminate is downgraded at the
  parent boundary to a new `SubworkflowTerminatedError` (also an
  `ExecutionError`) preserving the child's rendered `terminated_output` /
  `terminated_reason` / `terminated_by` as structured attributes, so the
  parent treats it as a normal sub-workflow failure (its own
  `workflow_failed` does NOT inherit `is_explicit: true`) while debugging
  surfaces can still inspect what the child intended to emit. Schema
  validation rejects `routes`, `tools`, `output`, `prompt`, `model`,
  `provider`, and the other agent-only fields on terminate steps, and
  conversely rejects `status` / `reason` / `output_template` on every other
  step type so authors who forget `type: terminate` get a clear error
  instead of silently dropped fields. Terminate cannot be used as a
  parallel-group member or as a `for_each` inline agent — route to one
  from those groups' `routes:` instead. The example workflow lives at
  `examples/terminate.yaml`
  ([#219](https://github.com/microsoft/conductor/issues/219)).
- `runtime.provider` now accepts either the bare string shorthand
  (`provider: copilot`) or a structured `ProviderSettings` object that
  forwards a `ProviderConfig` to the Copilot SDK's
  `create_session(provider=…)` parameter. This lets workflows route the
  Copilot SDK at OpenAI-compatible / Azure / Anthropic endpoints —
  Ollama, vLLM, LM Studio, Azure OpenAI, llamafile, or any other
  OpenAI-compatible REST endpoint — instead of being locked to the
  GitHub Copilot service. The structured form supports `name`, `type`
  (`openai`|`azure`|`anthropic`), `wire_api`
  (`completions`|`responses`), `base_url`, `api_key`, `bearer_token`,
  `headers`, and `azure.api_version`. `api_key` and `bearer_token` are
  Pydantic `SecretStr` (redacted in `model_dump`, dashboard payloads,
  event logs, and checkpoints). Custom routing activates only when YAML
  sets at least one non-`name` field — ambient `OPENAI_*` env vars
  never divert default routing on their own. Once activated, missing
  fields fall back from `COPILOT_PROVIDER_BASE_URL` → `OPENAI_BASE_URL`
  for `base_url`, `COPILOT_PROVIDER_API_KEY` for `api_key`, and
  `COPILOT_PROVIDER_BEARER_TOKEN` for `bearer_token`. Ambient
  `OPENAI_API_KEY` is intentionally NOT consulted as an implicit
  fallback (credential-leak risk); use `api_key: ${OPENAI_API_KEY}`
  YAML interpolation for explicit opt-in. The schema rejects every
  non-`name` field when `name != "copilot"` (structured config for
  Claude / openai-agents is a follow-up), and rejects anchorless or
  empty combinations (`wire_api` / `type` / `headers` / `azure` alone,
  empty `headers`, empty `SecretStr`, empty `azure` block) so silent
  no-ops cannot reach the SDK. Custom routing applies to both agent
  execution and dialog turns so all sessions hit the same endpoint.
  See `examples/copilot-local-llm.yaml` and
  [Configuration → Custom Provider Routing](docs/configuration.md#custom-provider-routing-ollama--vllm--azure-openai)
  ([#225](https://github.com/microsoft/conductor/pull/225),
  [#136](https://github.com/microsoft/conductor/issues/136)).

### Added
- New `output_mode` field on `AgentDef` (`raw` | `envelope`). Setting
  `output_mode: raw` bypasses JSON schema injection and parse-recovery entirely,
  wrapping the model's response as `{"result": "<text>"}`. Useful for agents
  that produce large Markdown, prose, or code output that should not be
  JSON-extracted. `output_mode: raw` is incompatible with `output:` — declaring
  both raises a `ValidationError` at config load time.
- New `max_parse_recovery_attempts` field on `RetryPolicy` (YAML `retry:`
  block, per-agent or workflow-level). Overrides the provider default (Copilot:
  5, Claude: 2) for agents that need tighter or looser in-session parse-recovery
  budgets. Accepts integer 0–10; `0` disables all recovery attempts and lets
  the first parse failure propagate immediately. Threaded through both the
  Copilot and Claude providers.
- New `POST /api/gate-respond` and `GET /api/gate-status` HTTP API endpoints
  on the web dashboard server. `GET /api/gate-status` returns whether a
  `human_gate` agent is currently waiting, and which agent name it is.
  `POST /api/gate-respond` resolves the parked gate by injecting a
  `GateResponse` into the engine's queue. Both endpoints respect an optional
  `CONDUCTOR_GATE_TOKEN` secret for auth; when a token is configured on the
  server any request without a matching `token` field is rejected with HTTP 403.
- New `conductor gate-respond` CLI command for resolving a parked human gate
  from the command line without opening a browser. Accepts `--port`, `--choice`,
  `--agent` (auto-discovered via `/api/gate-status` when omitted), `--input`,
  and `--token` / `CONDUCTOR_GATE_TOKEN` env var. Designed for SSH or headless
  environments where the web dashboard UI is unreachable.

### Changed
- **Breaking (Claude provider):** `ClaudeProvider._extract_text_response` now
  returns `{"result": "<text>"}` instead of `{"text": "<text>"}`. This aligns
  the Claude provider with the Copilot provider (cross-provider parity). Any
  existing Claude workflow that references `{{ <agent>.output.text }}` must be
  updated to `{{ <agent>.output.result }}`. Workflows that declare an `output:`
  schema are unaffected (the schema fields take precedence). See the new
  `output_mode: raw` feature if you need to consume unstructured text output
  reliably across both providers.

### Fixed
- `_verbose_console` is now silent-aware at the source: a `_SilentAwareConsole`
  subclass no-ops every `.print(...)` when `is_verbose()` is False, so the
  remaining `conductor --silent` stderr leaks (dashboard-failed-to-start and
  log-file-open warnings, workflow-hash mismatch, "Press Esc to interrupt",
  "Event log written to…", "Log written to…", `_print_resume_instructions`,
  and the replay command's "Press Ctrl+C to exit" / "Replay stopped"
  banners) no longer reach stderr. The app-wide `console` remains
  unchanged because it carries real error messages; the two replay prints
  are gated per-call. `conductor --silent replay <log>` now produces zero
  bytes on stderr
  ([#223](https://github.com/microsoft/conductor/pull/223),
  closes [#209](https://github.com/microsoft/conductor/issues/209)).
- Parse-exhaustion `ProviderError` (after all in-session recovery attempts
  are spent) is now marked `is_retryable=False` in both Copilot and Claude
  providers. Previously Copilot marked it `is_retryable=True`, causing the
  outer retry loop to re-run the entire agent up to 3× on deterministic
  parse failures — burning tokens with no chance of success.
- Parse-exhaustion error messages now include the first 500 characters of the
  model's response (up from 200) and suggest `output_mode: raw` as a fix.
- `parse_json_output` and the Copilot provider's `_extract_json` now use a
  two-stage fenced-block extraction (non-greedy `re.findall` + per-candidate
  try-parse, then a greedy single-capture fallback) so JSON whose string
  fields contain triple-backtick substrings no longer matches prematurely
  and falls into parse-recovery loops, while responses with multiple
  fenced JSON blocks still pick the first valid one. Resolves a recurring
  failure mode for agents emitting Markdown-bearing JSON
  (external-workflow-friction Issue #1)
  ([#232](https://github.com/microsoft/conductor/pull/232)).
- `conductor run --web-bg` and `conductor resume --web-bg` now abort before
  forking when the workflow contains a `human_gate` agent (including gates
  nested in `for_each.agent`) and `--skip-gates` is not set, with a message
  listing the four supported options. `resume --web-bg` also recovers the
  workflow path from the checkpoint when invoked without an explicit
  workflow argument so the guard still fires. Previously the detached
  child crashed with `EOFError` and the parent only reported
  "Background process exited immediately with code 1" (Issue #8).

### Documentation
- New "Choosing whether to declare `output:`" section in
  [docs/workflow-syntax.md](docs/workflow-syntax.md) describing when to declare
  a schema versus consuming raw `<agent>.output.result` for prose or large
  JSON. Closes a documentation gap that contributed to misconfiguration of
  agents emitting large payloads (Issue #2).
- `docs/cli-reference.md` `--web-bg` section now documents the `human_gate`
  incompatibility and the new pre-fork validation behavior.

## [0.1.17](https://github.com/microsoft/conductor/compare/v0.1.16...v0.1.17) - 2026-05-21

### Added
- Script agents can now declare an `output:` schema using the same
  OutputField syntax as LLM agents. When declared, the engine parses
  stdout as JSON and validates it against the schema before emitting
  `script_completed`; missing fields, wrong types, non-JSON stdout,
  empty stdout, and JSON arrays/scalars all raise `ValidationError` and
  emit `script_failed` (with stdout/stderr/exit_code) instead of
  completing. Validation runs on the **merged** output dict so declared
  `stdout` / `stderr` / `exit_code` fields validate the value
  downstream actually sees (matching the PR #122 shadowing contract).
  An explicit `output: {}` opts into strict JSON-object mode with zero
  required fields. Without a declared schema, the legacy best-effort
  JSON-stdout auto-merge from PR #122 is fully preserved, so this is
  purely additive. Routing conditions can now reference declared fields
  (e.g. `when: "phase == 'planning'"`) rather than opaque exit codes
  ([#206](https://github.com/microsoft/conductor/pull/206),
  [#118](https://github.com/microsoft/conductor/issues/118)).
- `conductor validate` now warns on undeclared `agent.output` references
  and field-level mismatches in `explicit` context mode, closing two
  follow-up gaps left by PR #125 that still produced the runtime
  `TemplateError: 'dict object' has no attribute 'X'` from issue #105.
  The validator now tracks declared fields per agent root (`a.output.foo`
  vs `a.output.bar`), so a prompt that references an undeclared field on
  an otherwise-declared agent surfaces a warning instead of a runtime
  failure; the same logic applies to static parallel groups
  (`pg.outputs.member.field`). Output-vs-error namespaces are tracked
  independently so `input: ["pg.errors"]` no longer silently suppresses
  warnings for `{{ pg.outputs.* }}` references, and the AST walker now
  filters inner-link `Getattr` nodes (no more spurious whole-output
  refs from `{{ a.output.bar }}` chains), detects method-call nodes
  (`{% for k,v in a.output.items() %}` registers as a whole-output ref),
  and degrades gracefully on `TemplateAssertionError`. For-each groups
  remain skipped (whole-member copy makes field precision a false
  positive); `human_gate` is now correctly excluded from `agent.output`
  warnings since the engine renders gate prompts in accumulate mode
  ([#208](https://github.com/microsoft/conductor/pull/208), refs #105).

### Changed
- Copilot provider verbose log lines (tool calls, reasoning, processing
  indicators, idle/parse recovery) are now prefixed with the originating
  agent name in parallel and for-each runs, eliminating the
  un-attributable interleaved output that made the for-each case
  unreadable (every iteration previously shared the same agent name).
  An optional `agent_name` parameter is plumbed through
  `_execute_sdk_call` → `_send_and_wait` → `_log_event_verbose` and
  rendered as a magenta `[agent_name]` tag between the tree icon and
  event content (continuation lines tagged too). For-each iterations
  additionally get a `model_copy()` of the per-iteration agent with
  `name = f"{name}[{key}]"` so each iteration produces a distinct tag;
  the original `AgentDef` is untouched and context lookups still use
  the unqualified name. Static parallel groups are unaffected — each
  agent already has a unique name. The `_item_callback` merge order is
  flipped so the wrapper's `agent_name`/`item_key` win over any
  qualified name the provider emits, preserving the dashboard/JSONL
  event contract (`agent_name` = for-each group name; `item_key`
  disambiguates iterations). Backward compatible: `agent_name` defaults
  to `None` for sequential agents
  ([#207](https://github.com/microsoft/conductor/pull/207), closes #16).

### Fixed
- `conductor resume … --web` and `--web-bg` no longer open an empty
  dashboard. Checkpoints now record the original `run_id` and JSONL
  `event_log_path`. On resume the dashboard's history is seeded BEFORE
  it accepts clients: the CLI prepends a fresh `workflow_started` event
  built from the current YAML (so historical events apply to the
  correct topology), then replays the original JSONL log line-by-line
  (or, when no log file is available, synthesises minimal
  `*_started`/`*_completed` pairs from the restored execution history).
  The resumed engine's own `workflow_started` emit is suppressed so the
  dashboard sees exactly one root start — no `wfDepth` double-counting.
  Root-level `workflow_completed` / `workflow_failed` /
  `checkpoint_saved` events from the original run are filtered out on
  replay; subworkflow lifecycle events are preserved so the frontend's
  context tracking stays balanced. The resumed `EventLogSubscriber`
  appends to the original log, preserving `run_id` across resume
  generations so log/timeline correlation tools see one continuous run
  (#167).
- `--web-bg` startup crashes on Windows are no longer silent
  ([#116](https://github.com/microsoft/conductor/issues/116)). Three
  changes work together to make any crash forensically traceable:
  - `conductor.cli.bg_runner` now captures the detached child's stdout
    and stderr to log files in `$TMPDIR/conductor/` (named to match the
    existing `.events.jsonl` filename) instead of discarding them with
    `subprocess.DEVNULL`. A Python traceback or `faulthandler` dump from
    the child now survives the parent's exit. The captured stderr path
    is printed alongside the dashboard URL and is included in every
    background-launch failure message so users always know where to
    look.
  - `conductor/__init__.py` enables `faulthandler` at import time
    (writing to `sys.__stderr__`), so a native crash — segfault, abort,
    fatal Python error — dumps a Python-level stack trace into the
    captured stderr log.
  - `WorkflowEngine._execute_loop` now catches `BaseException` (in
    addition to the existing `KeyboardInterrupt` / `ConductorError` /
    `Exception` arms) and emits a `workflow_failed` event with
    `is_base_exception: true` before re-raising. A bare `SystemExit` or
    other non-`Exception` failure between `agent_started` and
    `agent_prompt_rendered` now leaves a structured failure event in the
    JSONL log instead of an unexplained two-event truncation. An
    explicit `except asyncio.CancelledError: raise` arm sits in front of
    it so a normal dashboard-stop or parent cancellation is not
    mis-reported as an unexpected failure.
  Two new env vars (`CONDUCTOR_RUN_ID`, `CONDUCTOR_BG_STDERR_LOG`,
  `CONDUCTOR_BG_STDOUT_LOG`) propagate the parent-chosen run id and log
  paths to the child so the bg log files and the child's events JSONL
  share an 8-hex run id in their filenames, and `workflow_started`
  system metadata surfaces both bg log paths to the dashboard. The root
  cause of the underlying intermittent Windows crash is still pending —
  this change makes it diagnosable rather than invisible.

- Workflows that configure `reasoning.effort` (or workflow-wide
  `runtime.default_reasoning_effort`) on the Copilot provider were broken
  for **every named Copilot model** when running against
  `github-copilot-sdk` 0.3.0. The SDK's `models.list` response includes a
  `billing` object on every model, but none of them currently ship the
  `multiplier` field that the SDK's `ModelBilling.from_dict` parser
  treats as required — so every model in the response triggers
  `ValueError("Missing required field 'multiplier' in ModelBilling")`,
  which kills the entire `list_models()` call. The error then leaked
  through the narrow `except` tuple in
  `_validate_reasoning_effort_for_model` (and `get_max_prompt_tokens`),
  poisoned the retry loop, and surfaced as `Dialog turn failed: …` after
  three wasted attempts. (`get_max_prompt_tokens` was rescued by the
  engine's outer `except Exception`, so context-window metadata was
  silently unavailable rather than fatal.)
  Both metadata methods now catch any `Exception` raised at the SDK
  boundary and treat the failure as "metadata unavailable" — validation
  is skipped permissively and the configured `reasoning_effort` is
  forwarded to `create_session` as before.
  `asyncio.CancelledError`/`KeyboardInterrupt`/`SystemExit` (all
  `BaseException` subclasses) still propagate.
- `conductor resume --web-bg` (and `--web`) no longer exit silently when
  a workflow exceeds `max_iterations`. The bg child was forked with
  `--no-interactive` and `stdin=subprocess.DEVNULL`, so when the engine
  hit the limit, `IntPrompt.ask` raised `EOFError`, got coerced to `0`
  (stop), and the workflow ended with no way to recover. The
  max-iterations gate can now be resolved from the dashboard. New
  resolution policy: `skip_gates` auto-stops (unchanged); no web
  dashboard uses the legacy CLI prompt (unchanged); web dashboard +
  bg/non-TTY stdin uses a **web-only** wait (the CLI prompt is
  deliberately NOT raced because it would synchronously `EOFError` and
  win every dashboard click), with `dashboard.wait_for_stop()` racing
  so `POST /api/stop` can terminate the wait when no dashboard tab is
  open; web dashboard + TTY foreground races CLI vs web. Each
  `iteration_limit_reached` payload carries a uuid4 `gate_id` that the
  dashboard must echo back in `iteration_limit_response`, and the
  server matches/discards stale responses so a delayed double-click
  cannot be misapplied to a later gate. `iteration_limit_resolved`
  includes the same `gate_id` so subscribers can correlate the pair.
  New top-level `IterationLimitModal` (parallel-group gates can't
  attach to a per-agent panel) shows iteration count, recent agent
  history, and number input; it is hidden when `skip_gates` is true
  and does not close on Escape so the workflow can't be accidentally
  orphaned ([#202](https://github.com/microsoft/conductor/pull/202),
  fixes #198).
- `conductor run --web-bg` and `conductor resume --web-bg` no longer
  get killed within ~10 seconds when launched from a shell wrapper
  that runs commands inside a Windows job object with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (GitHub Actions runners, VS Code
  integrated terminal, JetBrains IDE terminals, GitHub Copilot CLI
  shell tool). The detached child previously inherited the parent's
  job and died with it; users saw a dashboard URL but the workflow
  never made progress. The `Popen` call now requests
  `CREATE_BREAKAWAY_FROM_JOB` in addition to
  `CREATE_NEW_PROCESS_GROUP` so the child fully detaches. In hardened
  CI environments that clear `JOB_OBJECT_LIMIT_BREAKAWAY_OK`,
  `CreateProcess` raises `ERROR_ACCESS_DENIED`; in that case a visible
  stderr warning is emitted (so the user understands bg mode may not
  survive shell exit) and the spawn is retried without the breakaway
  flag. Other `OSError`s propagate unchanged so the existing
  `RuntimeError` wrapper still surfaces them cleanly. Refactors the
  two near-identical detachment+Popen blocks in `launch_background`
  and `launch_background_resume` into a single `_spawn_detached`
  helper; constants are resolved via `getattr` so the module remains
  importable on POSIX hosts and tests can patch `sys.platform` to
  `"win32"` from Linux/macOS
  ([#200](https://github.com/microsoft/conductor/pull/200)).
- `conductor run --web-bg --log-file auto` now produces a log file
  with a real provider-side trace. `bg_runner.launch_background()` /
  `launch_background_resume()` already redirect the child's
  stdout/stderr/stdin to `subprocess.DEVNULL`, so silence is enforced
  at the OS level — but they also passed `--silent` to the child,
  which flipped `verbose_mode=False` and gated more than console
  prints (the Copilot provider's `_log_event_verbose()`,
  `_log_parse_recovery()`, and `_log_recovery_attempt()` all became
  no-ops, dropping events from the log file too). Both synthesized
  commands now omit `--silent`; console output still goes to DEVNULL
  via the Popen kwargs. Side benefit: the synthesized command is now
  reproducible by hand without learning that `--silent` was being
  injected behind the scenes
  ([#199](https://github.com/microsoft/conductor/pull/199),
  [#196](https://github.com/microsoft/conductor/issues/196)).
- `--web-bg` and other `--silent` invocations no longer leak the
  dashboard URL banner to stdout. Several `console.print` /
  `typer.echo` calls in `cli/run.py` were unconditionally writing the
  bg-launch URL, stderr log path, and `conductor stop` hint even with
  `--silent` / `is_verbose() == False`. Remaining unguarded URL prints
  are now gated behind `is_verbose()` so `--silent` is honored end to
  end ([#203](https://github.com/microsoft/conductor/pull/203),
  [#211](https://github.com/microsoft/conductor/pull/211)).
- `conductor validate <registry-workflow>` now succeeds for workflows
  that `conductor run` already executed successfully. The validator's
  `_resolve_subworkflow_ref_for_validation` was missing the step that
  the engine's `_resolve_subworkflow_path` already had: when a parent
  workflow lives inside a registry SHA cache and references a sibling
  via a relative path (e.g. `../document-review/workflow.yaml`), the
  engine auto-fetches the sibling from the same registry+SHA cache via
  `auto_fetch_relative_workflow`. The validator only checked the
  filesystem and reported "sub-workflow file not found". Validation and
  execution now agree on which refs are resolvable
  ([#197](https://github.com/microsoft/conductor/pull/197)).
- Registry cache now mirrors the source repository layout so
  repo-relative references between workflows in the same registry repo
  resolve correctly. Previously each workflow was isolated under
  `<base>/<registry>/<workflow_name>/<sha[:12]>/<filename>`, so
  `sdd-plan/plan.yaml` referencing `../document-review/workflow.yaml`
  resolved to a path that never existed in the cache and forced manual
  workarounds. The cache now stores workflows from the same
  registry+SHA under a shared per-SHA root
  (`<base>/<registry>/<sha[:12]>/<repo_path>`); metadata lives in a
  sibling `_meta/<sha[:12]>/` tree so it can never collide with real
  repo paths (e.g. a repo's own `.conductor/` directory). Per-workflow
  readiness sentinels are written **last** so readers never observe a
  partially populated workflow; per-file `os.replace()` stays
  intra-filesystem for atomic promotion; `_safe_repo_path()` rejects
  `..`, absolute paths, NUL bytes, and empty paths from any
  index/sibling entry; `_resolve_within()` adds defense-in-depth that
  resolved targets stay under the SHA root; `source.json` carries
  `cache_layout_version`, `registry_type`, `source`, and `full_sha` so
  cache hits require all four to match (stale metadata triggers
  re-fetch); the registry index is cached on disk so cache hits avoid
  a network round-trip. Sub-workflow refs from the same registry are
  auto-fetched when not yet present (gated to file-path-looking
  candidates with no `@`). `add_registry()` now rejects names
  containing `/`, `\`, the empty string, or the reserved `_adhoc` /
  `_meta` namespaces
  ([#194](https://github.com/microsoft/conductor/pull/194)).

## [0.1.16](https://github.com/microsoft/conductor/compare/v0.1.15...v0.1.16) - 2026-05-14

### Added
- `type: workflow` agents now accept registry references
  (`workflow[@registry][#ref]`) in the `workflow:` field, not just local file
  paths. Resolution prefers a local file when one exists relative to the
  parent workflow directory (preserves backward compatibility for
  extensionless local refs); otherwise the value is parsed as a registry
  reference, fetched via the registry cache, and executed from the cached
  location. `conductor validate` now recursively validates fetched
  sub-workflows with cycle detection (inode-based identity, so case-variant
  paths on macOS/Windows collapse correctly) and a depth cap of 10 — when
  the cap is hit a warning surfaces so users know validation was truncated
  rather than silently clean. Mutable registry refs (`name@registry#main`,
  or no `#ref`) may resolve to a different commit on `conductor resume` if
  the upstream branch has moved; pinned tags or commit SHAs guarantee
  deterministic resume
  ([#188](https://github.com/microsoft/conductor/pull/188)).
- Conductor now ships as a Claude Code plugin marketplace at the repo root.
  Users can install the conductor skill directly from `microsoft/conductor`
  with `/plugin marketplace add microsoft/conductor` followed by
  `/plugin install conductor@conductor`. The plugin ships markdown only
  (no `bin/`, hooks, MCP servers, or executables), keeping the trust
  surface minimal. The same `SKILL.md` remains usable via
  `gh skill install microsoft/conductor conductor` for Copilot CLI users.
  The previous `.claude/skills/conductor` location was removed — the
  plugin is now the single home for the skill; for local development on
  the skill itself, use `claude --plugin-dir plugins/conductor`
  ([#186](https://github.com/microsoft/conductor/pull/186)).

### Changed
- The bundled Conductor skill (`SKILL.md` + references) was refreshed to
  reflect the current CLI, schema, and feature set: `show` / `replay` /
  `--metadata` / `--workspace-instructions` quick-reference entries; new
  `type: workflow`, `dialog`, `retry`, `hooks`, `metadata`, `instructions`,
  `timeout_seconds`, and `openai-agents` provider concepts; corrected
  `update` behavior (default prints the install-script one-liner,
  `--apply` launches the installer); `CONDUCTOR_NO_UPDATE_CHECK`;
  registry `latest = branch HEAD` and `#ref` syntax; sub-workflow agents
  and dialog mode authoring guidance; script JSON-stdout auto-merge;
  `workflow.dir` / `workflow.file` template variables; and unknown-fields
  rejection in schema validation
  ([#187](https://github.com/microsoft/conductor/pull/187)).
- README "Why Conductor?" rewritten around three pillars — repeatable
  execution, deterministic routing, and version-controlled YAML
  workflows — and now leads with the real differentiator (zero-token
  orchestration) using concrete use-case examples
  ([#185](https://github.com/microsoft/conductor/pull/185)).

## [0.1.15](https://github.com/microsoft/conductor/compare/v0.1.14...v0.1.15) - 2026-05-13

### Added
- Per-agent `timeout_seconds` field for hard wall-clock timeouts on agent
  execution. Wraps execution in `asyncio.wait_for()` at the engine level so a
  slow agent no longer blocks the rest of the workflow. Effective timeout is
  `min(agent.timeout_seconds, remaining_workflow_timeout)` — when the workflow
  timeout is stricter it owns the error so attribution is never mislabeled.
  Raises a new `AgentTimeoutError` (subclass of `TimeoutError`) honored by
  existing `fail_fast` / `continue_on_error` semantics in parallel and
  for-each groups, and emits an `agent_timeout` event (with elapsed time
  and limit) for console + dashboard subscribers. Scoped to provider-backed
  agents; rejected on `script`, `human_gate`, and `workflow` types
  ([#150](https://github.com/microsoft/conductor/pull/150)).
- Auto-discovery of `.github/instructions/**/*.instructions.md` workspace
  conventions, matching GitHub Copilot's documented semantics. Files marked
  `applyTo: "**"` in their frontmatter are loaded into the workspace preamble
  alongside `AGENTS.md` / `CLAUDE.md` / `.github/copilot-instructions.md`;
  scoped (`applyTo: "<glob>"`) and absent-`applyTo` files are skipped per
  the convention's manual-attach default. The internal
  `CONVENTION_FILES: list[str]` table is refactored to a polymorphic
  `CONVENTIONS: list[Convention]` (`ConventionFile | ConventionDirectory`)
  so adding new conventions (Cursor rules, Cline rules, etc.) becomes one
  filter function plus one list entry; a `CONVENTION_FILES` module-level
  alias preserves backward compatibility for downstream imports
  ([#169](https://github.com/microsoft/conductor/pull/169)).

### Fixed
- `agent.system_prompt` is now rendered and forwarded to providers. The
  executor was rendering `agent.system_prompt` only to discard the result
  (`_ = self.renderer.render(...)`), so providers that forward
  `system_prompt` — notably the Copilot provider, which concatenates it
  into the prompt — received the un-rendered Jinja template. Agents whose
  instructions lived in `system_prompt` sent literal `{{ ... }}` placeholders
  to the model and got back "the prompt template contains unfilled variables"
  refusals. Also adds a `conductor validate` warning for agents that define
  `system_prompt` but no `prompt:` (a portability hazard since the Claude
  provider drops `system_prompt` entirely, and almost always a missing-`prompt:`
  typo) ([#179](https://github.com/microsoft/conductor/pull/179)).
- `conductor update` on Windows no longer attempts an in-process self-upgrade.
  The previous flow tried to re-install into the same venv the running
  `python.exe` lives in, producing "Access is denied" failures that earlier
  mitigations only papered over. `conductor update` now checks for a newer
  version and prints the OS-appropriate `install.ps1` / `install.sh`
  one-liner, and the install scripts become the single upgrade path: they
  detect other running conductor processes (auto-stopping under `-Yes`),
  sweep stale `*.exe.old` files, retry with backoff (2s / 5s / 10s), and —
  when uv can't remove the `conductor-cli` tool dir because of file locks —
  rename the whole dir aside and retry. `install.sh` reaches parity with
  `--yes` / `--force` / `--source` flags, retry-with-backoff, running-process
  detection, and a post-install `conductor --version` verify
  ([#171](https://github.com/microsoft/conductor/pull/171)).
- `install.ps1` is now stored without a UTF-8 BOM. The documented one-liner
  `irm https://aka.ms/conductor/install.ps1 | iex` returns the script body
  as a single string with the BOM surviving as U+FEFF at index 0; PowerShell's
  in-memory `iex` parser then trips on the `[CmdletBinding()]` attribute with
  `Unexpected attribute 'CmdletBinding'`. Both fresh installs via `irm | iex`
  and `conductor update --apply` (which re-runs the same command in a
  spawned console) now succeed. Direct `powershell.exe -File install.ps1`
  invocations were unaffected, which is why prior file-based integration
  tests didn't catch it ([#178](https://github.com/microsoft/conductor/pull/178)).
- `conductor stop` (including `--all` and `--port`) no longer crashes on
  Windows when a PID file exists in `~/.conductor/runs/`. The Unix idiom
  `os.kill(pid, 0)` for liveness probing is *not* a no-op on Windows — any
  signal other than `CTRL_C_EVENT` / `CTRL_BREAK_EVENT` routes through
  `TerminateProcess` and can raise `OSError` subclasses outside
  `ProcessLookupError` / `PermissionError` (e.g. `WinError 11 /
  ERROR_BAD_FORMAT`), and even "successful" calls would actually terminate
  the target with exit code 0. `_is_process_alive()` now dispatches to a
  Windows-specific implementation using `OpenProcess` +
  `GetExitCodeProcess` for a truly non-destructive liveness check
  ([#176](https://github.com/microsoft/conductor/pull/176)).

## [0.1.14](https://github.com/microsoft/conductor/compare/v0.1.13...v0.1.14) - 2026-05-06

### Fixed
- `conductor update` no longer reports its own launching shim as another
  running Conductor process. On Windows the `conductor.exe` shim is a
  separate process from the Python interpreter that runs the update
  command, so excluding only `os.getpid()` caused a false "1 other
  Conductor process is running" warning. The check now walks the full
  ancestor PID chain (via `wmic` on Windows, `ps` elsewhere) and excludes
  every process along the way, falling back to `{getpid(), getppid()}`
  if the parent map cannot be built.
  [#164](https://github.com/microsoft/conductor/pull/164)

## [0.1.13](https://github.com/microsoft/conductor/compare/v0.1.12...v0.1.13) - 2026-05-06

### Added
- `conductor resume` is now at flag parity with `conductor run`. New flags:
  `--provider` / `-p` (runtime provider override), `--metadata` / `-m` (CLI
  metadata merged on top of YAML metadata), `--web` (real-time dashboard for
  the resumed run), `--web-port`, and `--web-bg` (fork a detached resume +
  dashboard process). `--web` and `--web-bg` are mutually exclusive, matching
  `run`. The dashboard only shows events from the resumed agent forward —
  agent runs that completed before the checkpoint were emitted in the original
  process and are not replayed. `--input`, `--workspace-instructions`,
  `--instructions`, and `--dry-run` are intentionally not mirrored
  ([#158](https://github.com/microsoft/conductor/pull/158)).
- Reasoning effort (`low` / `medium` / `high` / `xhigh`) is now displayed in
  the web dashboard under each agent's metadata, right after `Model`. Effective
  value is per-agent `reasoning.effort` if set, otherwise
  `runtime.default_reasoning_effort`, otherwise omitted. Backed by a new
  `reasoning_effort` field on the `workflow_started` event payload, so older
  event log JSONL files replay gracefully (the row simply doesn't render)
  ([#160](https://github.com/microsoft/conductor/pull/160)).
- New `iteration_limit_reached` and `iteration_limit_resolved` events are
  emitted when a workflow hits its `max_iterations` cap. Previously the
  console showed an interactive `IntPrompt` while the web dashboard went
  silently dark; the dashboard now renders the prompt state and the chosen
  resolution. The `iteration_limit_reached` payload includes a `possible_loop`
  heuristic flag (set when the last 3 history entries are the same agent) so
  subscribers can call out stuck review loops
  ([#162](https://github.com/microsoft/conductor/pull/162)).

### Changed
- Workflow registry references now resolve `latest` (and bare `name@registry`
  refs) to the **default branch HEAD** instead of the newest git tag.
  Previously, the moment a registry repo got its first tag, bare references
  silently froze at that tag and stopped picking up commits to `main`. Tags
  remain first-class — pin explicitly via `workflow#v1.2.3` for releases. Also
  saves one GitHub API call on the hot path of bare-name fetches
  ([#157](https://github.com/microsoft/conductor/pull/157)).

### Fixed
- Schema validation now rejects unknown fields on `AgentDef`, `ParallelGroup`,
  `ForEachDef`, and `WorkflowConfig` instead of silently dropping them.
  Misnesting `parallel:` or `for_each:` inside an `agents:` item — or typos
  like `prmpt:` — used to fall through to a runtime
  `Model "gpt-4o" is not available` error three layers downstream. They now
  fail at parse time with a clear Pydantic error pointing at the offending
  location. `conductor validate` also gained "Parallel Groups" and "For-each
  Groups" rows in its summary table so missing groups are immediately visible
  ([#159](https://github.com/microsoft/conductor/pull/159)).
- Tool arguments and results are now pretty-printed in dashboard / JSONL /
  verbose-console events. Copilot tool results no longer leak the full
  `Result(content=..., contents=None, detailed_content=..., kind=None)` repr
  with literal `\\n` escapes and doubled `\\\\` Windows paths, and tool
  arguments render as JSON (`{"k": "v"}`) instead of Python dict repr
  (`{'k': 'v'}`). Both providers share a new
  `src/conductor/providers/_event_format.py` helper for parity
  ([#161](https://github.com/microsoft/conductor/pull/161)).
- `install.ps1` on Windows now captures full `uv tool install` stdout AND
  stderr via `Start-Process -RedirectStandardOutput -RedirectStandardError`
  to temp files. Previously, with `$ErrorActionPreference = 'Stop'`,
  PowerShell treated uv's stderr as a terminating error and threw before
  the assignment completed, so install failures showed `(no output captured)`
  with no way to diagnose them
  ([#156](https://github.com/microsoft/conductor/pull/156)).

## [0.1.12](https://github.com/microsoft/conductor/compare/v0.1.11...v0.1.12) - 2026-05-05

### Added
- Unified `reasoning.effort` configuration for per-agent and workflow-wide
  control of model reasoning / extended-thinking effort. Set
  `runtime.default_reasoning_effort` (`low` | `medium` | `high` | `xhigh`) for a
  workflow-wide default, or override per agent with a `reasoning.effort` block.
  Translates to `reasoning_effort` on the Copilot session and to extended
  `thinking` budget on Claude (low=2048, medium=8192, high=16384, xhigh=32768
  tokens, with `temperature` coerced to 1.0 and `max_tokens` bumped to fit).
  Validates against each model's supported efforts/capabilities and surfaces
  thinking content via `agent_reasoning` events. See
  [`examples/reasoning-effort.yaml`](examples/reasoning-effort.yaml)
  ([#152](https://github.com/microsoft/conductor/pull/152)).
- Tag-based versioning for the workflow registry. Versions are now
  auto-discovered from git tags instead of being explicitly listed in
  `registry.yaml`, and refs accept any tag, branch, or SHA via the new
  `workflow#ref` syntax (e.g. `sdd/plan#v3.0.0`, `sdd/plan#main`,
  `sdd/plan#abc1234`). Stale CDN content is bypassed via cache-busting
  query parameters so registry updates are visible immediately
  ([#151](https://github.com/microsoft/conductor/pull/151)).

### Fixed
- `conductor update` reliability on Windows. Adds a pre-flight check for
  other running Conductor processes (which hold file locks on
  `%LOCALAPPDATA%\uv\tools\conductor-cli\` and cause `uv tool install
  --force` to fail with "Access is denied"), retries the install up to 3
  times to absorb transient Windows Defender failures, surfaces full uv
  stdout AND stderr on failure with Defender-exclusion guidance, broadens
  the Windows entrypoint rename to cover the uv tool venv `Scripts/`
  directory in `%LOCALAPPDATA%` and `%APPDATA%`, and adds a new
  `conductor update --force` flag to skip the pre-flight check
  ([#155](https://github.com/microsoft/conductor/pull/155)).
- Dashboard layout for workflows with `human_gate` options or multiple
  loop-back routes (e.g. revision loops). The `workflow_started` event now
  emits routes from `human_gate` `options[].route` so gate edges aren't
  silently dropped, and the frontend pre-classifies back-edges via DFS from
  `$start` and feeds them to Dagre in reversed direction so cycles no
  longer scramble rank assignment. Workflows like `sdd/plan-v3.yaml` now
  render as a coherent top-to-bottom DAG instead of disconnected columns
  with long diagonal edges
  ([#153](https://github.com/microsoft/conductor/pull/153)).
- Windows install failures now surface useful diagnostics. `install.ps1`
  prints captured `uv` stdout/stderr on failure instead of swallowing it,
  and uses the correct Microsoft Defender cmdlet so the install path is
  exclusion-friendly ([#149](https://github.com/microsoft/conductor/pull/149)).

## [0.1.11](https://github.com/microsoft/conductor/compare/v0.1.10...v0.1.11) - 2026-05-04

### Added
- `metadata` dict on workflow definitions, settable statically in YAML or
  dynamically via `--metadata` / `-m` CLI flags. Merged metadata is
  included in the `workflow_started` event for downstream consumers
  ([#107](https://github.com/microsoft/conductor/pull/107)).
- `input_mapping` field on `type: workflow` agents, enabling Jinja2-templated
  per-call inputs to sub-workflows evaluated against the parent context.
  When omitted, the parent's `workflow.input.*` is forwarded as before
  ([#109](https://github.com/microsoft/conductor/pull/109)).
- `type: workflow` agents are now allowed inside `for_each` groups, enabling
  dynamic fan-out to sub-workflows with per-iteration `input_mapping`. Each
  iteration emits its own `subworkflow_started` / `subworkflow_completed`
  events ([#110](https://github.com/microsoft/conductor/pull/110)).
- Self-referential sub-workflows are now allowed; depth is bounded by the
  global `MAX_SUBWORKFLOW_DEPTH` plus an optional per-agent `max_depth`
  field on `AgentDef` ([#111](https://github.com/microsoft/conductor/pull/111)).
- `workflow.dir`, `workflow.file`, and `workflow.name` template variables are
  now available in all agent contexts (regardless of context mode). Lets
  registry-hosted workflows reference co-located scripts and assets without
  depending on the caller's working directory
  ([#121](https://github.com/microsoft/conductor/pull/121)).
- Script agent stdout that is valid JSON is auto-parsed and merged into
  the agent's output dict alongside `stdout`, `stderr`, and `exit_code`,
  enabling field-based `when:` route conditions instead of opaque exit-code
  matching ([#122](https://github.com/microsoft/conductor/pull/122)).
- `conductor validate` now performs semantic validation in addition to
  YAML schema checks, catching stale agent references, missing workflow
  inputs, and undeclared explicit-mode dependencies before runtime in
  `prompt`, `system_prompt`, `command`, `args`, `working_dir`,
  `input_mapping`, parallel-group inputs, and workflow `output:`
  templates ([#125](https://github.com/microsoft/conductor/pull/125)).
- Web dashboard: breadcrumb navigation, double-click dive-in to
  sub-workflow graphs, isolated subworkflow contexts (no node-status
  bleed across repeated runs), and reliable Stop button during
  subworkflows ([#113](https://github.com/microsoft/conductor/pull/113),
  follow-up fixes in [#146](https://github.com/microsoft/conductor/pull/146)).
- Dialog mode for agents: multi-turn conversational interactions
  driven by a `dialog` gate with conditional transitions, full
  Copilot and Claude provider support, and dedicated dashboard UI
  (`DialogDetail`, `DialogEngagementPrompt`, `DialogOverlay`)
  ([#130](https://github.com/microsoft/conductor/pull/130)).
- Markdown rendering and auto-linkification in human gate prompts.
  Gate prompts render through Rich Markdown in the terminal and as
  GitHub-Flavored Markdown in the dashboard. Bare file paths and URLs
  in gate prompts are converted to clickable links; relative paths
  open a sandboxed `FileViewer` modal served via a path-traversal-safe
  `GET /api/files/{path}` endpoint
  ([#131](https://github.com/microsoft/conductor/pull/131)).
- Workspace instructions support: `--workspace-instructions` and
  `--instructions` CLI flags plus a YAML-level `instructions:` field on
  the workflow. Auto-discovers `AGENTS.md`, `CLAUDE.md`, and
  `.github/copilot-instructions.md` by walking from CWD to the git root,
  prepends them to every agent's prompt, inherits into sub-workflows,
  and persists in checkpoints
  ([#141](https://github.com/microsoft/conductor/pull/141)).

### Changed
- The dashboard's "context window remaining" bar now sources
  `context_window_max` from each provider's SDK at runtime instead of a
  hand-maintained static table. Values now reflect the actual cap the SDK
  enforces (e.g. `claude-opus-4.6` reports 200K rather than the theoretical
  1M; `gpt-5.x` reports 128K rather than 400K). The `context_window` field
  on `ModelPricing` has been removed; pricing data continues to be
  hand-maintained for cost calculation only
  ([#144](https://github.com/microsoft/conductor/pull/144)).

### Fixed
- Pass `streaming=True` to the Copilot SDK's `create_session` to prevent
  silent truncation of large tool-call arguments. In non-streaming mode
  the model's per-turn output budget is exhausted mid-JSON for large
  arguments (e.g., `create` with multi-KB `file_text`), the CLI executes
  the partial tool call, and the agent loops on the broken call until
  the wall-clock session limit fires ([#129](https://github.com/microsoft/conductor/pull/129)).
- Build the Copilot prompt schema recursively from nested `output:`
  definitions instead of flattening to top-level fields only. Nested object
  properties, required keys, and array item schemas are now included in the
  prompt-facing schema used for initial guidance and parse recovery
  ([#100](https://github.com/microsoft/conductor/pull/100)).
- Coerce Python literal `"True"` / `"False"` / `"None"` strings produced by
  Jinja's default `str(bool)` rendering into native Python types when
  building workflow output. Previously, `output: { matched: "{{ a == b }}" }`
  produced the string `"False"` (truthy), causing downstream `when:`
  comparisons against `false` to silently misbehave
  ([#139](https://github.com/microsoft/conductor/pull/139)).
- Pricing fuzzy match no longer silently inherits values across model
  families. Names sharing a textual prefix with a known key (e.g.
  `claude-opus-4.7` previously matched `claude-opus-4`) now require a `-`
  delimiter; non-matching names return `None` and the dashboard hides the
  cost field. A one-time warning is emitted per requested name on any
  non-exact match ([#143](https://github.com/microsoft/conductor/pull/143)).
- Run `uv tool update-shell` after `uv tool install` in both `install.ps1`
  and `install.sh` so `conductor` is available on PATH in new shells, CI
  agents, and IDE extensions after a fresh install
  ([#142](https://github.com/microsoft/conductor/pull/142)).
- In explicit context mode, `workflow.input` is now always available to
  `script` and `type: workflow` agent templates regardless of the agent's
  declared `input:` list. The explicit-mode contract still applies to LLM
  agents (no undeclared inputs in prompts to control token cost)
  ([#119](https://github.com/microsoft/conductor/pull/119)).
- Optional workflow inputs without an explicit `default:` now resolve to
  type-appropriate zero values (`""`, `0`, `false`, `[]`, `{}`) instead of
  Python `None`, so templates like
  `{{ workflow.input.optional | default("fallback") }}` render the fallback
  rather than the literal string `"None"`
  ([#123](https://github.com/microsoft/conductor/pull/123)).
- Web dashboard: events without an engine-supplied `subworkflow_path`
  stamp (e.g., `for_each_item_started` for a parent for_each over
  `type: workflow` agents) now route strictly to the root context
  instead of falling back to the user's currently-viewed path. This
  fixes two related symptoms: dashboards opened during a run with
  sub-workflows no longer auto-land inside an iteration, and a parent
  for_each panel now displays every iteration rather than silently
  dropping the middle ones into a sibling sub-workflow's context
  ([#148](https://github.com/microsoft/conductor/pull/148)).

## [0.1.10](https://github.com/microsoft/conductor/compare/v0.1.9...v0.1.10) - 2026-04-30

### Added
- Sub-workflow composition support: `workflow`-type agents can now be used
  inside `for_each` groups, with dynamic per-iteration `input_mapping`
  ([#101](https://github.com/microsoft/conductor/pull/101), [#102](https://github.com/microsoft/conductor/pull/102)).

### Changed
- Bumped `github-copilot-sdk` to `>=0.3.0`. The SDK ships a bundled `copilot`
  CLI binary used for JSON-RPC `session.create` calls; `0.2.2` bundled CLI
  `1.0.21`, which rejected newer model IDs locally with
  `JSON-RPC -32603: Model "<id>" is not available`. `0.3.0` bundles CLI
  `1.0.36-0`, which accepts the current Copilot model catalog (including
  `claude-opus-4.7*` variants).

### Fixed
- Suppressed noisy PowerShell stderr output from `uv tool install` during
  Windows self-update ([#99](https://github.com/microsoft/conductor/pull/99)).
