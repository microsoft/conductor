# Workflow Engine

How a workflow YAML becomes a running loop: config loading and validation, context modes,
routing, the engine-local step types, output validation, checkpoints and resume state, the
`validator:` block, mid-run guidance, and the terminal human-in-the-loop surfaces (gates and the
Esc/Ctrl+G interrupt listener).

## When to read

- Editing `src/conductor/config/schema.py`, `src/conductor/config/loader.py` or
  `src/conductor/config/validator.py` (any new restriction or step field).
- Editing `src/conductor/engine/workflow.py`, `src/conductor/engine/context.py`,
  `src/conductor/engine/router.py`, `src/conductor/engine/limits.py`,
  `src/conductor/engine/checkpoint.py`, `src/conductor/engine/validator.py` or
  `src/conductor/engine/guidance.py`.
- Editing `src/conductor/executor/output.py`, `src/conductor/executor/set_step.py`,
  `src/conductor/executor/wait.py` or `src/conductor/executor/template.py`.
- Adding a field to `LimitEnforcer` or to the checkpoint payload.
- Editing `src/conductor/gates/` or `src/conductor/interrupt/listener.py`.
- Execution backends, bundles and secret resolution are covered in
  [execution backends](execution-backends.md); skills and plugins in
  [plugins and skills](plugins-and-skills.md).

## Required invariants

### Config loading and validation

- `conductor run` and `conductor resume` NEVER call the cross-reference validator
  (`src/conductor/config/validator.py::validate_workflow_config`); they only get Pydantic schema
  validation via `src/conductor/config/loader.py`. Only `conductor validate`
  (`src/conductor/cli/validate.py`) runs the static checks. Therefore every new restriction that
  must hold at run time MUST also be enforced at run time (executor, engine or provider), not
  only in the validator. Existing pairs to copy: `src/conductor/executor/agent.py::AgentExecutor`
  (`_reject_unsupported_skills`, `_reject_discovery_without_native_skills`,
  `_reject_unsupported_plugins`, `_reject_unfilterable_agents`,
  `_reject_unsupported_settings_dir`) and `src/conductor/providers/factory.py` for temperature.
- Steps are a discriminated union (`src/conductor/config/schema.py::StepDef`) and every variant
  is `extra="forbid"`. A field that only makes sense on LLM steps (`validator`, `skills`,
  `plugins`, `session_key`, ...) belongs on `AgentDef` only; the other variants (`human_gate`,
  `questions`, `script`, `mcp`, `wait`, `set`, `terminate`, `workflow`) then reject it
  structurally. Do not add per-type "not allowed here" checks for such fields.
- Executable steps (`ExecutableStepBase`) are the only ones carrying `execution:`; engine-local
  steps reject it via `extra="forbid"`.
- Pydantic skips field validators on an already-built instance (`model_copy` nested in
  `WorkflowConfig`). Invariants that must survive revalidation need an after-model validator, as
  `TerminateStepDef.validate_termination` and `WaitStepDef` do.
- Include provenance (`!file` / `!yamlfile`) is recorded per load; the recorder is reset at the
  same point the loader sets its base dir and file stack
  (`src/conductor/config/loader.py::load_config_with_graph`, `IncludedFilesGraph`). The bundle
  collector depends on this graph.
- Validator-side checks for secrets and Docker profiles live in
  `src/conductor/config/validator.py::_validate_secret_references` and
  `src/conductor/config/validator.py::_validate_docker_profiles`; their semantics are described in
  [execution backends](execution-backends.md).

### Context, routing and tool resolution

- Context modes (`src/conductor/engine/context.py::WorkflowContext`): `accumulate` (default, all
  prior outputs), `last_only` (previous step only), `explicit` (only declared `input:`; a missing
  non-optional input raises `KeyError`).
- `WorkflowContext.store` accepts any JSON-safe value, not just dicts (set steps store scalars and
  lists). `step.output` returns a scalar verbatim; `step.output.field` on a non-dict output MUST
  raise a clear `KeyError` (`_add_agent_input`).
- Routes (`src/conductor/engine/router.py::Router`): the first matching `when` wins; a route with
  no `when` always matches. Conditions are Jinja2 first, then `simpleeval` arithmetic.
- Parallel / for-each `failure_mode`: `fail_fast` (default), `continue_on_error`,
  `all_or_nothing`.
- Tool resolution (`src/conductor/executor/agent.py::resolve_agent_tools`): omitted `tools:` = all
  workflow tools, `[]` = none, a list = that subset (must be declared at workflow level). How each
  provider honors the list is in [providers](providers.md).

### Engine-local step types

- `set` (`src/conductor/executor/set_step.py::SetExecutor`, engine helper
  `WorkflowEngine._run_set_step`): no LLM, no subprocess. `output_type` defaults to `auto`: safe
  YAML parse of the rendered string, then `_to_json_safe` (`datetime`/`date`/`time` to ISO 8601;
  any other non-JSON type, including non-string dict keys, raises `ExecutionError`). Explicit
  `string`/`number`/`integer`/`boolean`/`list`/`dict` is only valid with single `value:`, never
  with `values:` (schema-enforced). `_run_set_step` enforces `output:` schema in all three
  positions (main loop, parallel group, for-each) and emits `set_started` / `set_completed` /
  `set_failed`.
- `wait` (`src/conductor/executor/wait.py::WaitExecutor`): `asyncio.sleep` raced against the
  engine's `interrupt_event` so Esc/Ctrl+G cancels immediately; the workflow timeout cancels it via
  `src/conductor/engine/limits.py::LimitEnforcer.wait_for_with_timeout`. The context output
  contract is exactly `{"waited_seconds": float}` (#218); `requested_seconds`, `reason` and
  `interrupted` go to the event payload only. Do not add keys to the context output.
- `terminate` (`TerminateStepDef`): ends the run immediately, no routes evaluated. It has no
  `routes`/`tools`/`output`/`prompt`/`model` and is refused as a parallel-group member or a
  for-each inline agent (`src/conductor/config/validator.py`); route to it from the group instead.
  `output_template` values are rendered then passed through `_maybe_parse_json`.
  - `status: success`: exit 0, `workflow_completed` with `is_explicit: true`.
  - `status: failed`: raises `src/conductor/exceptions.py::WorkflowTerminated`; the engine emits
    `workflow_failed` with `is_explicit: true` and the rendered `output`, the CLI prints the output
    JSON and exits 1. It MUST NOT save an on-failure checkpoint and MUST delete the run's periodic
    checkpoints (explicit termination is non-resumable).
  - Inside a sub-workflow, a failed terminate is converted at the parent boundary to
    `SubworkflowTerminatedError` carrying `terminated_output` / `terminated_reason` /
    `terminated_by`; the parent treats it as an ordinary sub-workflow failure and its own
    `workflow_failed` does NOT inherit `is_explicit`.

### Output parsing and validation

- `src/conductor/executor/output.py::validate_output` is strict and MUST NOT coerce. It also
  validates `set` and `script` output, where reshaping an authored value would be surprising.
- Response normalization (non-object rejection, scalar-wrapper unwrapping) belongs in
  `src/conductor/providers/_output_shape.py::normalize_agent_output`, called by providers inside
  their recovery loop; see [providers](providers.md).
- `parse_json_output` wraps JSON syntax errors in `ValidationError`, so callers cannot tell
  syntax from schema failures by exception type alone; split them by which call failed.

### Checkpoints

- Failure checkpoints are written from the engine's exception handlers
  (`_save_checkpoint_on_failure`); periodic and failure saves share `_write_checkpoint`, which
  must never raise (it guards provider `get_session_ids()`).
- Periodic checkpoints (`runtime.checkpoint`, `CheckpointConfig`; off unless `every_agent` or
  `every_seconds` is set) are saved ONLY from `_maybe_save_periodic_checkpoint()` at the top of
  `WorkflowEngine._execute_loop`. At that point prior outputs are committed and
  `_current_agent_name` is the step about to run, so resume semantics equal a failure checkpoint.
  Do not add other save sites.
- Periodic checkpoints are root-engine only (`_periodic_checkpoints_active`:
  `_subworkflow_depth == 0` and enabled), skip the first boundary of a fresh run
  (`current_iteration == 0`), and use `_periodic_checkpoint_due` (`every_agent` OR an
  `every_seconds` throttle; the first save always fires).
- A failed periodic save MUST NOT disrupt the run and MUST be surfaced: write + emit + rotate are
  wrapped, and failure calls `_record_periodic_checkpoint_failure`, which emits
  `checkpoint_save_failed` with a `consecutive_failures` count.
- Deletion is scoped to `trigger == "periodic"` AND an exact `run_id`
  (`src/conductor/engine/checkpoint.py::CheckpointManager._delete_periodic_checkpoints`); failure
  checkpoints and other runs are never touched. `rotate_periodic_checkpoints` is a no-op for
  `keep_last < 1` (schema bounds it to 1..100); `cleanup_periodic_for_run` deletes all.
- `_cleanup_run_periodic_checkpoints()` runs only at non-resumable terminal outcomes: clean
  completion of `run()`/`resume()` and an explicit `status: failed` terminate. An unexpected
  failure leaves periodic checkpoints next to the failure checkpoint.
- `trigger` (`CheckpointTrigger = Literal["failure", "periodic"]`) is additive: no
  `CHECKPOINT_VERSION` bump, and a missing/unknown on-disk value normalizes to `"failure"`. Keep
  new checkpoint fields additive the same way.
- `find_latest_checkpoint` returns the newest by microsecond `created_at`, never by filename
  (same-second periodic files would otherwise win).
- Dashboard Stop/Kill can cancel the engine task from
  `src/conductor/cli/run.py::_execute_with_stop_signal`, bypassing the engine's handlers. After
  draining the task the CLI calls `WorkflowEngine.handle_dashboard_stop`, which writes a
  best-effort checkpoint and emits one `workflow_failed` with `stopped_by_user: true` plus
  `checkpoint_path` or `checkpoint_unavailable_reason` (#245). Its idempotence flag is
  `_dashboard_stop_handled`; NEVER key it on `_last_checkpoint_path`, which periodic saves also
  set.
- Any new top-level checkpoint key or `to_dict()` key MUST be added to
  `src/conductor/engine/checkpoint.py::_CHECKPOINT_PROTOCOL_KEYS`, or secret scrubbing can rename
  it and make the checkpoint unloadable (`tests/test_engine/test_checkpoint_redaction.py`).

### LimitEnforcer resume state

- Persistent fields (survive resume) go in both
  `src/conductor/engine/limits.py::LimitEnforcer.to_dict` and `from_dict` deserialization: today
  `current_iteration`, `max_iterations`, `execution_history`.
- Transient fields (fresh each run) are NOT serialized; they are keyword-only, required
  `from_dict` parameters sourced from the current config: today `timeout_seconds`, `budget_usd`,
  `budget_mode`. Do not give them defaults. Update the single call site in
  `src/conductor/cli/run.py::resume_workflow_async`.

### Validator block

- `validator:` (`src/conductor/config/schema.py::ValidatorConfig`, `AgentDef` only) runs
  `src/conductor/engine/validator.py::OutputValidator`: a second LLM call through
  `provider.execute` with `tools=[]`, `suppress_mcp_servers=True` and a `{passed, issues}` schema.
- It is fail-open: a raising or unparseable validator call counts as a pass with a logged warning
  (`ValidationOutcome.errored`). Never make it fail-closed.
- `max_retries` is `Field(default=1, ge=0, le=1)`: hard cap of one re-run; `0` = report only.
  The second output is final; there is no second validation loop.
- On failure with retries left, `WorkflowEngine._apply_validator` re-runs the primary once with a
  `## Validation feedback` section. If the output carries `continuation_state`, it continues that
  conversation with the feedback as the only new user turn (no prompt re-render, no
  skills/instructions prefix); otherwise it rebuilds the prompt statelessly. The provider-side
  contract for `continuation_state` is in [providers](providers.md).
- Wired into the main loop, parallel groups and for-each, each guarded by
  `agent.validator and not output.partial`. For-each passes `usage_label` so usage rows match
  `<group>[<key>]`.
- Usage: the grading call and any discarded first attempt are recorded under
  `"<agent> (validator)"`; the primary row is the effective output.
- Events (through the per-agent `event_callback`, so for-each events carry `item_key`):
  `agent_validator_start`, `agent_validator_complete` (`passed`, `issues`, `errored`, `tokens`,
  `cost_usd`), `agent_validation_failed` (`issues`, `will_retry`). The dashboard renders them; see
  [dashboard](dashboard.md).

### Mid-run guidance

- `src/conductor/engine/guidance.py` is a leaf with no conductor imports, so `web/` can depend on
  it without importing the engine. It owns `GuidanceChannel` (buffer + `asyncio.Event`),
  `MAX_GUIDANCE_CHARS` (10,000) and `validate_guidance_text`, the single bound shared by
  `POST /api/guidance` and `resume --guidance` (`src/conductor/cli/run.py`).
- `WorkflowEngine.add_user_guidance(text, source=...)` is the ONLY place guidance is applied to
  context and `guidance_applied` emitted. The TTY interrupt path, dashboard drains and
  `resume --guidance` all go through it.
- `submit_guidance` only queues (it is the dashboard sink). Sub-workflow engines share the
  parent's channel (`_guidance_channel=`).
- Two drain points:
  1. `_drain_pending_guidance()` at the top of `_execute_loop`, right after the periodic
     checkpoint call, so every step type sees it. Root-only (`_subworkflow_depth == 0`):
     draining at every depth would let concurrent for-each sub-workflows race for one queued
     sentence.
  2. A guidance wait-arm in `_handle_web_pause`, active at every depth. Its event is NOT cleared
     before waiting, so guidance queued before the pause resolves it immediately. `drain()` is
     destructive: only report `with_guidance: true` when this caller actually drained text;
     a sibling that lost the race falls through to plain resume.
- `_handle_web_pause` returns `WebPauseOutcome(handled, guidance)` so the caller can try
  `_send_guidance_followup` before re-executing.
- Parallel and for-each members render with the current `guidance_section`.
- HTTP and CLI surfaces are described in [dashboard](dashboard.md) and
  [CLI and runs](cli-and-runs.md).

### Gates and interrupt

- `src/conductor/gates/human.py::read_multiline_lines(console, *, sentinel)` is the single
  blocking multi-line reader for both the human gate (`MULTILINE_SENTINEL`, `.`) and dialog gate
  (`DIALOG_SUBMIT_SENTINEL`, `/send`). `sentinel` stays keyword-only and required. It returns
  `(text, hit_eof)`: EOF with no text is a dismissal; the sentinel with no text is an empty
  submission. Trailing blank lines are dropped but a whitespace line is kept (pasted code).
- Dialog (`src/conductor/gates/dialog.py`): a tty turn reads multi-line; off a tty it falls back
  to single-line `Prompt.ask`. `_reads_multiline_turn()` is the one predicate for that rule and
  `_display_dialog_start` gates its sentinel hint on it.
- Submission guards compare stripped text (`not text.strip()`), never `== ""`, or whitespace is
  dispatched as a turn and Ctrl-D becomes a submission.
- `src/conductor/interrupt/listener.py::KeyboardListener` reads stdin on a daemon thread
  (delivering via `call_soon_threadsafe`, not abandoned executor futures), disambiguates bare Esc
  from escape sequences with a 50 ms read-ahead, captures the original tty settings once per
  process, and restores them from its SIGTERM handler (#290). Tests:
  `tests/test_interrupt/test_listener.py`, `tests/test_interrupt/test_listener_pty.py`.
- Gate UI goes through `src/conductor/console.py` primitives; see
  [console output](console-output.md).

## Change checklist

- [ ] New config restriction: added to `src/conductor/config/validator.py` AND enforced at run
      time; tests cover both paths.
- [ ] New LLM-only field: declared on `AgentDef` only, relying on `extra="forbid"` elsewhere.
- [ ] New invariant on a step model: after-model validator if it must survive revalidation.
- [ ] New `LimitEnforcer` field classified persistent or transient; `to_dict`/`from_dict`,
      `resume_workflow_async` and `_CHECKPOINT_PROTOCOL_KEYS` updated as needed.
- [ ] New checkpoint field is additive (no `CHECKPOINT_VERSION` bump) and has a load default.
- [ ] Periodic checkpoint behavior changed only at the loop-top choke point; root-only gate kept.
- [ ] Validator change keeps fail-open, the retry cap of 1, and the three wiring sites in sync;
      frontend event types updated if payloads change ([dashboard](dashboard.md)).
- [ ] Guidance change routes through `add_user_guidance`; bounds stay in
      `validate_guidance_text`.
- [ ] Wait output contract unchanged (`{"waited_seconds": float}`).
- [ ] Run `uv run pytest tests/test_engine tests/test_executor tests/test_config tests/test_gates`.

## Rationale and evidence

**Run never validates.** Because `validate_workflow_config` is only reached from
`conductor validate`, several features shipped restrictions that held at validate time and were
silently ignored at run time (skills on unsupported providers, discovery on eager providers). The
paired run-time refusals in `AgentExecutor` exist for that reason.

**Structural rejection.** The `StepDef` union with `extra="forbid"` per variant makes "this field
is not valid on a script step" a schema error with no extra code, and keeps the published JSON
schema honest (`tests/test_config/test_step_def_union.py`).

**Strict output validation.** `validate_output` is shared by deterministic step types; coercion
there would silently reshape authored values. Provider-shaped fixes live in `_output_shape.py`
so they run inside the provider's recovery loop, where a re-prompt is still possible (#343).

**Periodic checkpoints (#244).** A single loop-top choke point guarantees the same
`current_agent` semantics as a failure checkpoint, so resume needs no special case. Sub-workflow
state is not independently resumable, hence root-only. `checkpoint_save_failed` exists because a
user who opted in for recovery must not be left silently without checkpoints. Scoping deletion
by trigger and `run_id` protects failure checkpoints and concurrent runs.
`find_latest_checkpoint` sorts by `created_at` because several periodic files can share a second.
Tests: `tests/test_engine/test_periodic_checkpoint.py`, `tests/test_engine/test_checkpoint.py`.

**Dashboard stop (#245).** Cancelling the engine task unwinds through the
`asyncio.CancelledError` arm, which by design saves nothing. Without `handle_dashboard_stop` a
Stop/Kill lost all progress. The dedicated idempotence flag exists because
`_last_checkpoint_path` is also set by periodic saves.

**Terminate.** A failed terminate is an intended outcome, so a failure checkpoint would invite
resuming something the author ended on purpose. Converting it at the sub-workflow boundary keeps
the parent's failure handling uniform while preserving the child's rendered output.

**LimitEnforcer.** Transient parameters were previously defaulted, which let non-CLI callers
silently disable the budget on resume; they are now keyword-only and required
(`tests/test_engine/test_limits.py`, `tests/test_engine/test_resume.py`).

**Validator (#220).** One retry is the cap by design: beyond that the problem is prompt design.
Fail-open keeps a flaky grader from blocking a valid run. The grader runs with
`suppress_mcp_servers=True` because `tools=[]` alone does not stop MCP servers attaching on
MCP-capable providers
(`tests/test_engine/test_validator_integration.py::TestValidatorRunsWithoutMcpServers`).

**Guidance (#400).** Before the channel, only the TTY interrupt could inject guidance and the
dashboard/JSONL never saw it. One application entry point keeps every source observable. The
root-only loop drain avoids for-each races; the uncleared pause arm avoids requiring a second
submission. Tests: `tests/test_engine/test_guidance.py` (`TestRootOnlyDrain`,
`TestHandleWebPauseGuidanceArm`, `TestParallelAndForEachReceiveGuidance`).

**Gates.** A pasted block used to become one dialog turn per line; the shared reader and the
stripped-text guards close that and the Ctrl-D-as-submission trap
(`tests/test_gates/test_human.py`, `tests/test_gates/test_dialog.py`).

## Related references

- [Workflow syntax: context modes](../workflow-syntax.md#context-modes),
  [routes](../workflow-syntax.md#routes), [wait steps](../workflow-syntax.md#wait-steps),
  [set steps](../workflow-syntax.md#set-steps),
  [terminate steps](../workflow-syntax.md#terminate-steps),
  [validator](../workflow-syntax.md#validator),
  [periodic checkpoints](../workflow-syntax.md#periodic-checkpoints)
- [CLI reference: conductor resume](../cli-reference.md#conductor-resume),
  [conductor guide](../cli-reference.md#conductor-guide)
- [Execution backends](execution-backends.md) (secrets, redaction, environments, bundles)
- [Plugins and skills](plugins-and-skills.md)
- [Providers](providers.md), [CLI and runs](cli-and-runs.md), [dashboard](dashboard.md),
  [console output](console-output.md), [testing](testing.md)
