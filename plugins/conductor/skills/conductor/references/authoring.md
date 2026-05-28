# Workflow Authoring Guide

Complete reference for creating and modifying Conductor workflow YAML files.

## Workflow Configuration

```yaml
workflow:
  name: my-workflow              # Required: unique identifier
  description: What it does      # Optional
  version: "1.0.0"               # Optional
  entry_point: first_agent       # Required: starting agent, parallel group, or for-each group

  runtime:
    provider: copilot            # copilot (default), claude, hermes, or openai-agents
    default_model: gpt-5.2       # Default model for agents
    temperature: 0.7             # 0.0-1.0 (optional)
    max_tokens: 4096             # Max output tokens per response (optional)
    timeout: 600                 # Per-request timeout in seconds (optional)
    max_agent_iterations: 50     # Max tool-use roundtrips per agent (1-500, optional)
    max_session_seconds: 120     # Wall-clock timeout per agent session (optional)
    default_reasoning_effort: medium  # Workflow-wide reasoning effort: low, medium, high, xhigh (optional)

  input:                         # Define workflow inputs
    param_name:
      type: string               # string, number, boolean, array, object
      required: true
      default: "value"
      description: What it is

  context:
    mode: accumulate             # accumulate, last_only, explicit

  limits:
    max_iterations: 10           # Max agent executions (default: 10, max: 500)
    timeout_seconds: 600         # Total workflow timeout (optional, no default)

  cost:
    show_per_agent: true         # Show cost per agent (default: true)
    show_summary: true           # Show cost summary (default: true)
    pricing:                     # Custom pricing overrides
      custom-model:
        input_per_mtok: 3.0
        output_per_mtok: 15.0

  hooks:                         # Optional lifecycle expressions
    on_start: "..."              # Evaluated when workflow starts
    on_complete: "..."           # Evaluated on success
    on_error: "..."              # Evaluated on failure

  metadata:                      # Optional arbitrary key-values surfaced in workflow_started events
    tracker: ado
    work_item_id: 42
    # Merged with --metadata / -m CLI flags (CLI wins on key collision)

  instructions:                  # Optional workspace context prepended to every agent prompt
    - !file ../AGENTS.md         # !file include
    - "Always respond in English."  # Inline string
    # For workflows distributed via registry, prefer the --workspace-instructions
    # CLI flag (auto-discovers AGENTS.md / CLAUDE.md / .github/copilot-instructions.md
    # / .github/instructions/**/*.instructions.md with applyTo: "**") so target-repo
    # context is loaded at run time instead of being baked into the YAML.
```

## Agent Definition

```yaml
agents:
  - name: my_agent               # Required: unique identifier
    type: agent                  # agent (default), human_gate, script, workflow, wait, or terminate
    description: What it does
    model: gpt-5.2               # Override workflow default
    provider: claude             # Optional: per-agent provider override

    system_prompt: |             # Optional: system message (always included)
      You are a specialized assistant.

    prompt: |
      You are a helpful assistant.

      Input: {{ workflow.input.param }}

      {% if other_agent is defined and other_agent.output %}
      Previous output: {{ other_agent.output.field }}
      {% endif %}

    output:                      # Structured output schema
      field_name:
        type: string
        description: What this field contains

    tools:                       # null = all, [] = none, [list] = subset
      - web_search

    max_agent_iterations: 100    # Override workflow default for this agent (optional)
    max_session_seconds: 60      # Wall-clock timeout for this agent (optional, soft, between iterations)
    timeout_seconds: 120         # Hard wall-clock cancellation for this agent (provider-backed only).
                                 # Engine wraps execution in asyncio.wait_for(); raises AgentTimeoutError.
                                 # Effective limit = min(timeout_seconds, remaining_workflow_timeout).
                                 # Non-retryable. Forbidden on script/human_gate/workflow/wait types.

    retry:                       # Per-agent retry policy (optional, not allowed on script/human_gate/workflow/wait)
      max_attempts: 3            # 1-10, default 1 (no retry)
      backoff: exponential       # exponential (default) or fixed
      delay_seconds: 2.0         # Base delay (0-300, default 2.0)
      retry_on:                  # Default: ["provider_error", "timeout"]
        - provider_error         # API 500s, rate limits
        - timeout                # Agent-level timeout exceeded
                                 # Validation errors are never retried.

    dialog:                      # Optional: conditionally pause for free-form conversation (optional)
      trigger_prompt: |
        Enter dialog if the agent expresses uncertainty about the user's
        intent or needs clarification on ambiguous requirements.

    reasoning:                   # Override runtime.default_reasoning_effort (optional)
      effort: high               # low, medium, high, or xhigh

    routes:                      # Where to go next
      - to: next_agent
```

### Reasoning Effort

`reasoning.effort` (per-agent) and `runtime.default_reasoning_effort` (workflow-wide) accept `low`, `medium`, `high`, or `xhigh`. Per-agent overrides the runtime default. The provider translates the unified value to its native API:

- **Copilot**: forwarded as `reasoning_effort` on the session. Validated against the model's advertised `supported_reasoning_efforts`; raises `ValidationError` for unsupported combinations (skipped in mock-handler mode or when capability metadata is absent).
- **Claude**: enables extended thinking via `thinking={"type": "enabled", "budget_tokens": N}` with mapping `low=2048`, `medium=8192`, `high=16384`, `xhigh=32768`. Auto-coerces `temperature` to `1.0` (logged at INFO) and bumps `max_tokens` to fit `budget + 4096` (capped at 64000, logged at INFO when clamped). Only valid on thinking-capable models (`claude-3-7-*`, `claude-opus-4*`, `claude-sonnet-4*`, `claude-haiku-4*`); raises `ValidationError` otherwise.
- **Hermes**: `reasoning.effort` is accepted for interface parity but has no effect — the hermes library controls model reasoning internally.

Both providers surface reasoning content via `agent_reasoning` events visible in the dashboard, JSONL logs, and the console at `-vv`. Not allowed on `script`, `human_gate`, `workflow`, or `wait` agent types.

```yaml
runtime:
  provider: claude
  default_model: claude-opus-4-20250514
  default_reasoning_effort: medium    # workflow-wide default

agents:
  - name: explainer
    prompt: "Explain this algorithm."
    # inherits 'medium'

  - name: architect
    reasoning:
      effort: high                    # override
    prompt: "Design the system architecture."
```

See `examples/reasoning-effort.yaml` for a complete example.

## Routing Patterns

### Linear

```yaml
routes:
  - to: next_agent
```

### Conditional (first match wins)

```yaml
routes:
  - to: success_agent
    when: "{{ output.status == 'approved' }}"
  - to: failure_agent
    when: "{{ output.status == 'rejected' }}"
  - to: default_agent           # Fallback (no when clause)
```

### Loop-back

```yaml
routes:
  - to: $end
    when: "{{ output.score >= 90 }}"
  - to: self                    # Loop back to same agent
```

### Terminal

```yaml
routes:
  - to: $end                    # End workflow
```

### Route to parallel/for-each group

```yaml
routes:
  - to: parallel_researchers    # Route to a parallel group
  - to: item_processors         # Route to a for-each group
```

## Script Steps

Script steps run shell commands and capture stdout, stderr, and exit_code:

```yaml
agents:
  - name: check_python
    type: script
    description: Check the installed Python version
    command: python3
    args: ["--version"]
    timeout: 30                  # Per-script timeout in seconds (optional)
    working_dir: /tmp            # Working directory (optional, Jinja2 templated)
    env:                         # Extra environment variables (optional)
      MY_VAR: "value"
    routes:
      - to: analyzer
        when: "exit_code == 0"
      - to: error_handler
```

### Script Output

Script steps always produce three fields:

```jinja2
{{ script_name.output.stdout }}     # Captured standard output (string)
{{ script_name.output.stderr }}     # Captured standard error
{{ script_name.output.exit_code }}  # Process exit code (0 = success)
```

If `stdout` is **valid JSON**, its top-level keys are auto-merged into the agent's output dict alongside `stdout`/`stderr`/`exit_code`. This enables structured `when:` route conditions instead of opaque exit-code matching:

```yaml
agents:
  - name: classify
    type: script
    command: python3
    args: ["classify.py"]                # prints e.g. {"category": "bug", "score": 87}
    routes:
      - to: bug_handler
        when: "category == 'bug'"        # field-based, not exit-code-based
      - to: triage
```

### Script Routing

Route conditions use `exit_code` directly (simpleeval syntax):

```yaml
routes:
  - to: next_step
    when: "exit_code == 0"
  - to: error_handler            # Fallback for non-zero exit
```

### Script Restrictions

Script agents **cannot** have: `prompt`, `provider`, `model`, `tools`, `output`, `system_prompt`, `options`, `retry`, `reasoning`, `dialog`, `max_session_seconds`, `max_agent_iterations`, `timeout_seconds` (use `timeout:` instead), `input_mapping`, or `max_depth`.
Command and args support Jinja2 templating for dynamic values.

## Wait Steps (`type: wait`)

Pause workflow execution for a parsed duration via in-process `asyncio.sleep`. Cross-platform — no shell `sleep` dependency. Use for rate-limit cooldowns, polling intervals, and external-system catch-up.

```yaml
agents:
  - name: cooldown
    type: wait
    description: Cool down between API bursts   # Optional
    duration: 60s                               # Required (see "Duration format")
    reason: Avoiding rate limit                 # Optional, shown in dashboard
    routes:
      - to: next_call
```

### Duration Format

- Plain `int`/`float` → seconds (e.g. `60`, `1.5`).
- Suffixed string: `ms`, `s`, `m`, `h` (e.g. `"500ms"`, `"60s"`, `"2.5m"`, `"1h"`).
- Jinja2 template rendering to one of the above (templates defer literal validation to runtime):
  ```yaml
  duration: "{{ workflow.input.poll_interval_seconds }}s"
  ```
- Must resolve to `> 0` and `≤ 86400s` (24h). Booleans are rejected.

### Wait Output

Strict — only one field:

```jinja2
{{ wait_name.output.waited_seconds }}   # Actual seconds slept (may be < requested on interrupt)
```

### Polling Loop-back Pattern

```yaml
agents:
  - name: check_status
    type: script
    command: ./poll-status.sh
    routes:
      - to: process_result
        when: "status == 'ready'"
      - to: wait_then_retry

  - name: wait_then_retry
    type: wait
    duration: "{{ workflow.input.poll_interval_seconds }}s"
    routes:
      - to: check_status                  # loop back

  - name: process_result
    # ...
```

### Wait Cancellation

- `Esc` / `Ctrl+G` cancels in-progress waits immediately (the engine races the sleep against the interrupt event).
- Workflow-level `limits.timeout_seconds` cancels in-flight waits via the standard timeout path.

### Wait Restrictions

Wait agents **cannot** have: `prompt`, `model`, `provider`, `tools`, `system_prompt`, `options`, `command`, `args`, `env`, `working_dir`, `timeout`, `workflow`, `input_mapping`, `max_depth`, `max_session_seconds`, `max_agent_iterations`, `retry`, `dialog`, `reasoning`, `timeout_seconds`, or `output`. They also cannot be used inside `parallel` groups or `for_each` groups.

See `examples/wait-step.yaml` for a complete polling workflow.
## Set Steps

Set steps evaluate one or more Jinja2 expressions and bind the typed results into context. No LLM call, no subprocess, no I/O — these are pure context transformations. Use them when you'd otherwise duplicate a Jinja expression across many prompts, run `echo`-only script steps, or burn a model call on something deterministic.

```yaml
agents:
  # Single binding: output is the typed scalar / list / dict.
  - name: compute_slug
    type: set
    value: "{{ workflow.input.org }}/{{ workflow.input.repo }}"
    routes:
      - to: derive_flags

  # Multi-binding: output is a dict, accessible as step.output.<key>.
  - name: derive_flags
    type: set
    values:
      is_breaking: "{{ research.output.severity in ['high', 'critical'] }}"
      target_branch: "{{ workflow.input.branch or 'main' }}"
      effective_model: "{{ workflow.input.model or 'claude-sonnet-4-5' }}"
    routes:
      - to: breaking_path
        when: "{{ output.is_breaking }}"
      - to: safe_path
```

Exactly one of `value:` / `values:` must be present.

### Type Detection

Default (auto) detection uses safe YAML loading: booleans, numbers, lists, and dicts become native Python types; date-like strings are converted to ISO 8601 to stay JSON-safe; parse failures fall back to the raw string; empty renders become `""` (not `None`). Override with `output_type:` on a single `value:` to force `string`, `number`, `integer`, `boolean`, `list`, or `dict`. Per-key typing on `values:` is not supported — chain steps if you need it.

### Multi-Binding Ordering

Every binding in a single `values:` step renders against the *original* pre-step context. Later bindings cannot reference earlier ones in the same step. Chain multiple set steps for ordered dependencies:

```yaml
- name: step_a
  type: set
  value: "{{ workflow.input.x | upper }}"
- name: step_b
  type: set
  value: "{{ step_a.output }}-suffix"
```

### Routing on Set Output

Routes attached to a set step see the bound value directly. Dict outputs expose `{{ output.<key> }}` (Jinja2) and bare `<key>` (simpleeval); scalar / list outputs expose only `{{ output }}`:

```yaml
# Multi-values: route on a derived dict field.
- name: derive_flags
  type: set
  values:
    is_breaking: "{{ severity == 'high' }}"
  routes:
    - to: hot_path
      when: "{{ output.is_breaking }}"
    - to: safe_path

# Single-value: route on the scalar itself.
- name: flag
  type: set
  value: "{{ workflow.input.severity == 'high' }}"
  routes:
    - to: hi
      when: "{{ output }}"
    - to: lo
```

### Set Step Composition

- Inside `parallel` groups: each member publishes its bound value to context. Templates cannot reference sibling group members (validator-enforced).
- Inside `for_each` as the inline agent: one bound value per item, accessible via `loop.outputs`.
- Output `value:` / `values:` chain naturally — a multi-binding step that publishes `items` can drive a downstream `for_each` whose `source:` is `step.output.items`.

### Set Step Restrictions

Set agents **cannot** have: `prompt`, `provider`, `model`, `tools`, `system_prompt`, `command`, `args`, `env`, `working_dir`, `timeout`, `workflow`, `options`, `input_mapping`, `max_depth`, `retry`, `dialog`, `reasoning`, `timeout_seconds`, `max_session_seconds`, or `max_agent_iterations`. They count toward `limits.max_iterations` like any other step.

`output:` schema validation is permitted only when the rendered output is a dict (always for `values:`, sometimes for `value:`). A single-`value:` step with a declared schema that produces a scalar raises a `ValidationError` pointing to `values:`.

## Sub-Workflow Agents (`type: workflow`)

Reference an external workflow YAML file as a black-box step. The sub-workflow runs with its own engine and inherits the parent's provider configuration.

```yaml
agents:
  - name: deep_research
    type: workflow
    workflow: ./research-pipeline.yaml   # Required: path resolved relative to parent YAML
    input:                               # Optional: explicit input declarations (for explicit context mode)
      - workflow.input.topic
    input_mapping:                       # Optional: per-call inputs to the sub-workflow
      topic: "{{ workflow.input.topic }}"
      depth: "{{ research_planner.output.depth }}"
    max_depth: 3                         # Optional per-agent recursion cap
                                         #   (additionally bounded by global MAX_SUBWORKFLOW_DEPTH = 10)
    output:                              # Optional output schema for validation
      findings:
        type: string
    routes:
      - to: synthesizer
```

**Semantics:**

- `workflow` path is resolved relative to the parent workflow file.
- Sub-workflow inherits the parent's provider configuration.
- When `input_mapping` is omitted, the parent's `workflow.input.*` is forwarded as-is.
- `input_mapping` keys are sub-workflow input names; values are Jinja2 expressions evaluated against the parent's context.
- Recursive composition is supported with a global `MAX_SUBWORKFLOW_DEPTH = 10`. Self-referential workflows are allowed; bound recursion further with `max_depth`.
- Each invocation emits `subworkflow_started` / `subworkflow_completed` events. The dashboard supports breadcrumb navigation and double-click dive-in.
- Sub-workflow output is accessible via `{{ agent_name.output.field }}`.

**Sub-workflows in `for_each` groups** — `type: workflow` agents work inside `for_each` groups for dynamic fan-out, with per-iteration `input_mapping` evaluated against the loop variable:

```yaml
for_each:
  - name: plan_issues
    type: for_each
    source: epic_planner.output.issues
    as: issue
    max_concurrent: 1
    agent:
      type: workflow
      workflow: ./plan-and-review.yaml
      input_mapping:
        work_item_id: "{{ issue.id }}"
        title: "{{ issue.title }}"
```

**Restrictions** — workflow steps cannot have `prompt`, `model`, `provider`, `tools`, `system_prompt`, `command`, `options`, `retry`, `reasoning`, `dialog`, `max_session_seconds`, `max_agent_iterations`, or `timeout_seconds`.

## Terminate Steps (`type: terminate`)

End the workflow with an explicit, structured outcome — distinguishable from a generic crash in CLI exit codes, dashboard state, and event logs. Real workflows have multiple legitimate end states beyond "the last agent finished": early success ("the document is already up to date"), soft abort ("no matching issues found"), hard failure with reason ("upstream service returned unprocessable data"), pre-condition not met ("this PR is from a fork"). With only `$end`, all of these collapse into "workflow completed" downstream. Terminate steps surface the distinction.

```yaml
agents:
  - name: precheck
    prompt: "Is the input safe to process? Return JSON: {safe: bool, reason: string}"
    output:
      safe:   { type: boolean }
      reason: { type: string }
    routes:
      - when: "not precheck.output.safe"
        to: abort_unsafe
      - to: main_pipeline

  - name: abort_unsafe
    type: terminate
    status: failed                     # success | failed (required)
    reason: "{{ precheck.output.reason }}"   # required; Jinja2-templated
    output_template:                   # optional; replaces workflow-level output:
      aborted: "true"                  # rendered then JSON-coerced ("true" -> True)
      stage: precheck
      reason: "{{ precheck.output.reason }}"
```

**Semantics:**

- Reaching a terminate step ends the workflow immediately — no routes evaluated after.
- `status: success` → engine returns the rendered output, CLI exits `0`, dashboard ✅, emits `workflow_completed { termination_reason, terminated_by, is_explicit: true, status: "success" }`. Runs the `on_complete` hook.
- `status: failed` → engine raises `WorkflowTerminated` (subclass of `ExecutionError`), CLI exits `1` (and still prints the rendered output JSON to stdout for downstream tooling), dashboard ❌, emits `workflow_failed { error_type: "WorkflowTerminated", termination_reason, terminated_by, is_explicit: true, status: "failed", output }`. Runs the `on_error` hook. **Intentionally not resumable** — the engine skips the on-failure checkpoint because the author explicitly chose this outcome.
- `output_template:` is a `dict[str, str]` where each value is a Jinja2 expression. The rendered values are passed through the engine's JSON-coercion helper, so `"true"` becomes `True`, `"42"` becomes `42`, and JSON literals (`'{"k":"v"}'`) are parsed. When omitted, the workflow-level `output:` mapping is rendered as on any other terminal path.
- **Sub-workflow boundary** — a `status: failed` terminate inside a child sub-workflow is downgraded to `SubworkflowTerminatedError` (also an `ExecutionError`) at the parent boundary. The parent treats it as a normal sub-workflow failure (its own `workflow_failed` does NOT inherit `is_explicit: true`). The child's rendered output, reason, and terminate-step name are preserved as `terminated_output` / `terminated_reason` / `terminated_by` attributes on the wrapper for `on_error` hooks and debugging surfaces. A `status: success` child terminate returns its rendered output cleanly and the parent continues with its next routes.
- **Branching on a child's termination** — if the parent's routes need to react to a child's outcome, the child should use `status: success` plus an `output_template:` carrying the relevant fields. Failed terminate is an error from the parent's perspective; parent `routes:` are only evaluated after successful steps.

**Restrictions** — terminate steps cannot have `routes`, `tools`, `output`, `prompt`, `model`, `provider`, `system_prompt`, `command`, `args`, `env`, `working_dir`, `timeout`, `timeout_seconds`, `max_session_seconds`, `max_agent_iterations`, `max_depth`, `retry`, `dialog`, `reasoning`, `workflow`, `input_mapping`, or `options`. Cannot appear as a parallel-group member or as a `for_each` inline agent — route to them from those groups' `routes:` instead. Conversely, regular agents cannot have `status`, `reason`, or `output_template` — those fields are rejected at schema validation to catch authors who forgot to add `type: terminate`.

See `examples/terminate.yaml` for a complete example demonstrating success, failure, and pass-through paths.

## Dialog Mode

Dialog mode lets an agent conditionally pause after execution and enter a free-form conversation with the user. A lightweight evaluator LLM call inspects the agent's output against `trigger_prompt` and decides whether to engage. Both Copilot and Claude providers are supported, and the dashboard provides dedicated UI (`DialogDetail`, `DialogEngagementPrompt`, `DialogOverlay`).

```yaml
agents:
  - name: researcher
    prompt: "Research the given topic thoroughly"
    dialog:
      trigger_prompt: |
        Enter dialog if the agent expresses uncertainty about
        the user's intent, encounters ambiguous requirements,
        or needs clarification before proceeding.
        Do NOT trigger for minor uncertainties the agent can resolve on its own.
    routes:
      - to: writer
```

Only valid on provider-backed agents (not `script`, `human_gate`, `workflow`, or `wait`). See `examples/dialog-mode.yaml` for a complete example.

## Workflow Metadata and Workspace Instructions

### Metadata

Attach arbitrary key-value metadata to a workflow for downstream tooling (dashboards, work-item trackers, audit logs). Surfaced in the `workflow_started` event payload.

```yaml
workflow:
  name: implement
  metadata:
    tracker: ado
    template_version: 3
```

CLI metadata is merged on top of YAML metadata (CLI wins on key collision; values stay as strings, no type coercion):

```bash
conductor run workflow.yaml -m work_item_id=1814 -m sprint=Q3
```

### Workspace Instructions

Prepend workspace context to every agent prompt. Three options:

1. **YAML `instructions:`** — first-class field, persisted in checkpoints, inherited into sub-workflows. Best for self-contained workflows where the YAML lives alongside the code.

   ```yaml
   workflow:
     instructions:
       - !file ../AGENTS.md
       - "Always respond in English."
   ```

2. **`--workspace-instructions` CLI flag** — auto-discovers files by walking from CWD to the git root: `AGENTS.md`, `CLAUDE.md`, `.github/copilot-instructions.md`, and `.github/instructions/**/*.instructions.md` (only files with `applyTo: "**"` in YAML frontmatter; scoped or absent-`applyTo` files are skipped per the GitHub Copilot convention). Best for workflows distributed via registry/skills where the YAML lives far from the target repo.

3. **`--instructions PATH` CLI flag** — explicit path to a file (repeatable).

All three sources are concatenated and prepended to every agent's prompt as a workspace preamble.

## File Includes (`!file` Tag)

Include external file content in YAML using the `!file` tag:

```yaml
agents:
  - name: analyzer
    system_prompt: !file prompts/system.md
    prompt: !file prompts/analyze.md
```

- Paths are **relative to the YAML file's directory**
- If the included file is valid YAML, it's parsed as a data structure
- If it's plain text (e.g., Markdown), it's included as a string
- Supports **recursive includes** — included YAML files can use `!file` too
- Circular references are detected and raise an error

## Parallel Groups

Static parallel groups run a fixed set of agents concurrently:

```yaml
parallel:
  - name: parallel_researchers
    description: Research from multiple sources
    agents:
      - web_researcher           # At least 2 agents required
      - academic_researcher
      - news_researcher
    failure_mode: continue_on_error  # fail_fast, continue_on_error, all_or_nothing
    routes:
      - to: synthesizer
```

### Context Isolation

Each parallel agent gets an **immutable snapshot** of context at group start. Agents cannot see each other's outputs during execution.

### Accessing Parallel Outputs

```jinja2
{{ parallel_researchers.outputs.web_researcher.summary }}
{{ parallel_researchers.outputs.academic_researcher.findings }}

# Error access (continue_on_error mode)
{% if parallel_researchers.errors %}
{{ parallel_researchers.errors.news_researcher.message }}
{% endif %}
```

### Failure Modes

| Mode | Behavior |
|------|----------|
| `fail_fast` | Stop immediately on first failure (default) |
| `continue_on_error` | Continue all; proceed if at least one succeeds |
| `all_or_nothing` | Continue all; fail if any agent fails |

## For-Each Groups

Dynamic parallel groups process variable-length arrays at runtime:

```yaml
for_each:
  - name: kpi_analyzers
    type: for_each                 # Required discriminator
    description: Analyze each KPI
    source: finder.output.kpis     # Array reference (dotted path, 3+ parts)
    as: kpi                        # Loop variable name
    max_concurrent: 5              # Batch size (default: 10, max: 100)
    failure_mode: continue_on_error

    agent:                         # Inline agent template
      name: kpi_analyzer
      model: claude-sonnet-4.5
      prompt: |
        Analyze KPI {{ _index + 1 }}: {{ kpi.name }}
        Value: {{ kpi.value }}
      output:
        analysis:
          type: string
        score:
          type: number

    key_by: kpi.kpi_id             # Optional: dict-based outputs

    routes:
      - to: aggregator
```

### Loop Variables

| Variable | Description |
|----------|-------------|
| `{{ kpi }}` | Current item (name from `as`) |
| `{{ _index }}` | Zero-based index (0, 1, 2...) |
| `{{ _key }}` | Extracted key (only with `key_by`) |

### Reserved Variable Names

Cannot use for `as`: `workflow`, `context`, `output`, `_index`, `_key`

### Accessing For-Each Outputs

```jinja2
# Array access (no key_by)
{{ kpi_analyzers.outputs[0].analysis }}
{% for result in kpi_analyzers.outputs %}
- Score: {{ result.score }}
{% endfor %}

# Dict access (with key_by)
{{ kpi_analyzers.outputs["KPI-123"].analysis }}

# Metadata
Total: {{ kpi_analyzers.outputs | length }}
Errors: {{ kpi_analyzers.errors | length }}
```

## Human Gates

Pause workflow for user decisions. Uses **list-based** options:

```yaml
agents:
  - name: approval_gate
    type: human_gate
    prompt: |
      Review the design:
      {{ designer.output.design }}
    options:
      - label: "Approve"
        value: approved
        route: $end
      - label: "Request Changes"
        value: changes
        route: designer
        prompt_for: feedback        # Collects text input from user
      - label: "Reject"
        value: rejected
        route: $end
```

### Gate Output

Human gates automatically capture:
- `output.selected` — the `value` of the chosen option.
- `output.additional_input` — dict of values collected from `prompt_for` fields.
  Always present; `{}` when no `prompt_for` was specified or the selected option
  has no `prompt_for`. Access individual fields via templates as
  `{{ <gate>.output.additional_input.<field> }}` (for example
  `{{ approval_gate.output.additional_input.feedback }}` when an option declares
  `prompt_for: feedback`).

> **`context: explicit` mode note.** `input:` declarations support
> `<gate>.output.additional_input` (the whole dict) but not the dotted shorthand
> `<gate>.output.additional_input.<field>`. Declare the parent key and read
> individual fields via Jinja2 in the agent's prompt or output template.

## Context Modes

### Accumulate (default)

All prior agent outputs available to all agents:

```yaml
context:
  mode: accumulate
```

### Last Only

Only the previous agent's output available:

```yaml
context:
  mode: last_only
```

### Explicit

Only specified inputs available — maximum control, minimal tokens:

```yaml
context:
  mode: explicit

agents:
  - name: agent
    input:
      - workflow.input.question
      - other_agent.output.result   # Required
      - optional_agent.output?      # Optional (? suffix)
```

## Multi-Provider Workflows

Override the provider on individual agents:

```yaml
workflow:
  runtime:
    provider: copilot              # Default provider
    default_model: gpt-5.2

agents:
  - name: fast_classifier
    provider: claude               # Uses Claude for this agent
    model: claude-haiku-4.5
    prompt: "Classify: {{ workflow.input.text }}"

  - name: tool_using_agent
    provider: hermes               # Uses Hermes (NousResearch agent SDK)
    model: anthropic/claude-sonnet-4
    prompt: "Use tools to research: {{ workflow.input.topic }}"

  - name: deep_analyzer
    # Uses default copilot provider
    model: gpt-5.2
    prompt: "Analyze: {{ fast_classifier.output.category }}"
```

## MCP Server Configuration

### Stdio server

```yaml
runtime:
  mcp_servers:
    web-search:
      command: npx
      args: ["-y", "open-websearch@latest"]
      tools: ["*"]
```

### HTTP/SSE server

```yaml
runtime:
  mcp_servers:
    remote:
      type: http                   # or "sse"
      url: https://mcp.server.example.com/
      headers:
        Authorization: "Bearer ${API_TOKEN}"
      tools: ["*"]
```

### With environment variables

```yaml
runtime:
  mcp_servers:
    custom:
      command: node
      args: ["./server.js"]
      env:
        API_KEY: "${API_KEY}"      # Resolved from environment at runtime
      tools: ["*"]
```

### Selective tool access

```yaml
tools: ["search", "fetch"]        # Only these tools available
```

## Template Variables (Jinja2)

| Variable | Description |
|----------|-------------|
| `{{ workflow.input.param }}` | Workflow input |
| `{{ workflow.name }}` | Workflow name |
| `{{ workflow.dir }}` | Directory of the workflow YAML file (always available, all context modes) |
| `{{ workflow.file }}` | Absolute path to the workflow YAML file |
| `{{ agent_name.output.field }}` | Agent output |
| `{{ output.field }}` | Current agent output (in routes) |
| `{{ group.outputs.agent.field }}` | Parallel group output |
| `{{ group.outputs[i].field }}` | For-each output (index) |
| `{{ group.outputs["key"].field }}` | For-each output (key_by) |

### Conditionals

```jinja2
{% if previous_agent is defined and previous_agent.output %}
Previous: {{ previous_agent.output.result }}
{% endif %}
```

### Loops

```jinja2
{% for item in agent.output.items %}
- {{ item }}
{% endfor %}
```

### Filters

```jinja2
{{ value | upper }}                 # Uppercase
{{ value | default("fallback") }}   # Default value
{{ items | join(", ") }}            # Join array
{{ data | json }}                   # JSON serialize
```

## Output Schema

Map agent outputs to workflow output:

```yaml
output:
  answer: "{{ answerer.output.answer }}"
  summary: "{{ reviewer.output.summary }}"
  results: "{{ processors.outputs | json }}"
```

## Output Types

### String

```yaml
output:
  answer:
    type: string
    description: The answer
```

### Number

```yaml
output:
  score:
    type: number
    description: Quality score 0-100
```

### Boolean

```yaml
output:
  approved:
    type: boolean
```

### Array

```yaml
output:
  items:
    type: array
    description: List of items
    items:
      type: string
```

### Object

```yaml
output:
  result:
    type: object
    properties:
      name:
        type: string
      count:
        type: number
```

## Route Conditions

### Comparison operators

```yaml
when: "{{ output.score >= 90 }}"
when: "{{ output.score < 50 }}"
when: "{{ output.status == 'done' }}"
when: "{{ output.status != 'error' }}"
```

### Logical operators

```yaml
when: "{{ output.score >= 90 and output.approved }}"
when: "{{ output.retry or output.force }}"
when: "{{ not output.failed }}"
```

### String operations

```yaml
when: "{{ 'error' in output.message }}"
when: "{{ output.status.startswith('success') }}"
```

## Common Patterns

### Single Agent Q&A

```yaml
workflow:
  name: qa
  entry_point: answerer
  input:
    question:
      type: string
      required: true

agents:
  - name: answerer
    prompt: |
      Answer: {{ workflow.input.question }}
    output:
      answer:
        type: string
    routes:
      - to: $end

output:
  answer: "{{ answerer.output.answer }}"
```

### Iterative Refinement

```yaml
workflow:
  name: refine
  entry_point: creator
  limits:
    max_iterations: 5

agents:
  - name: creator
    prompt: |
      Create content...
      {% if reviewer.output %}
      Feedback: {{ reviewer.output.feedback }}
      {% endif %}
    routes:
      - to: reviewer

  - name: reviewer
    prompt: |
      Review and score 0-100:
      {{ creator.output.content }}
    output:
      score:
        type: number
      feedback:
        type: string
    routes:
      - to: $end
        when: "{{ output.score >= 90 }}"
      - to: creator
```

### Parallel Research Pipeline

```yaml
workflow:
  name: research
  entry_point: planner
  context:
    mode: explicit

parallel:
  - name: researchers
    agents: [web_researcher, academic_researcher]
    failure_mode: continue_on_error
    routes:
      - to: synthesizer

agents:
  - name: planner
    routes:
      - to: researchers

  - name: web_researcher
    input: [planner.output]
    prompt: "Web research on {{ planner.output.topic }}"

  - name: academic_researcher
    input: [planner.output]
    prompt: "Academic research on {{ planner.output.topic }}"

  - name: synthesizer
    input: [researchers.outputs]
    prompt: "Synthesize: {{ researchers.outputs | json }}"
    routes:
      - to: $end
```

### Human Approval Loop

```yaml
agents:
  - name: designer
    routes:
      - to: approval

  - name: approval
    type: human_gate
    prompt: "Review: {{ designer.output.summary }}"
    options:
      - label: Approve
        value: approved
        route: $end
      - label: Revise
        value: changes
        route: designer
        prompt_for: feedback
```

## Validation Rules

- `entry_point` must reference a valid agent, parallel group, or for-each group
- All agents must be reachable from entry_point
- All paths must eventually reach `$end`
- Route `when` conditions must be valid Jinja2
- Agent names must be unique
- Non-gate agents require at least one route
- Parallel groups need at least 2 agents
- For-each `source` must be dotted path with 3+ parts
- For-each `as` cannot use reserved names
