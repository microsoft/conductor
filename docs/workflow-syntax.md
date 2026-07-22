# Workflow Syntax Reference

This document provides a comprehensive reference for the Conductor workflow YAML syntax.

## Table of Contents

- [Workflow Configuration](#workflow-configuration)
- [Agents](#agents)
- [Parallel Groups](#parallel-groups)
- [Routes](#routes)
- [Inputs and Outputs](#inputs-and-outputs)
- [Limits and Safety](#limits-and-safety)
- [Tools](#tools)
- [External File References](#external-file-references)
- [Hooks](#hooks)

## Workflow Configuration

The top-level `workflow` section defines metadata and behavior for the entire workflow.

```yaml
workflow:
  name: string                      # Required: Unique workflow identifier
  description: string               # Optional: Human-readable description
  entry_point: string               # Required: Name of first agent to execute

  metadata:                         # Optional: free-form key/value metadata
    tracker: ado                    # surfaced in the workflow_started event
    project_url: https://...        # CLI --metadata / -m can add or override

  instructions:                     # Optional: extra instruction files (paths)
    - ./docs/conventions.md         # prepended to every agent prompt
    - ./AGENTS.md                   # also auto-discoverable via
                                    # --workspace-instructions (see CLI ref)

  limits:
    max_iterations: 10              # Default: 10, max: 500
    timeout_seconds: 600            # Optional: Maximum wall-clock time (seconds)
    budget_usd: 5.00                # Optional: Cost cap in USD (no tracking when unset)
    budget_mode: audit              # audit (default) | enforce

  hooks:
    on_start: "{{ template }}"      # Optional: Expression evaluated on start
    on_complete: "{{ template }}"   # Optional: Expression evaluated on success
    on_error: "{{ template }}"      # Optional: Expression evaluated on error

  context_mode: accumulate          # accumulate | snapshot | minimal (default: accumulate)

  runtime:
    provider: copilot               # copilot | claude | hermes | claude-agent-sdk
                                      # Structured object form (e.g. custom routing,
                                      # or the experimental `aca` sandbox provider)
                                      # is also accepted — see docs/configuration.md
                                      # and docs/providers/aca.md.
    default_model: gpt-5.2
    temperature: 0.7
    max_tokens: 4096
    default_reasoning_effort: medium  # Optional: low | medium | high | xhigh | max
                                      # Workflow-wide default for reasoning /
                                      # extended-thinking effort. Inherited by
                                      # every provider-backed agent unless it
                                      # declares its own `reasoning.effort`.
                                      # See docs/configuration.md#reasoning-effort.

    checkpoint:                       # Optional: periodic checkpoints (off by default)
      every_agent: true               # Save after each step boundary (governs alone when true)
      every_seconds: 300              # Throttle: save at most this often (used only when every_agent is false)
      keep_last: 5                    # Retain this many periodic checkpoints per run

    default_context_tier: default     # Optional: default | long_context (Copilot only)
                                      # Workflow-wide default for the model's
                                      # context-window tier. Inherited by every
                                      # provider-backed agent unless it declares
                                      # its own `context_tier`.
                                      # See docs/configuration.md#context-tier.

    working_dir: "/path/to/cwd"       # Optional: global default working directory for LLM agents
                                      # and their MCP servers. Relative paths resolve against the
                                      # parent directory of the workflow YAML file.
```

**Workflow metadata** is included verbatim in the `workflow_started` event and lets downstream consumers (dashboards, queue runners, observability tools) adapt without parsing the YAML. CLI `--metadata key=value` flags merge on top of YAML metadata (CLI wins on conflicts).

**Instructions files** are loaded once and prepended to every agent's rendered prompt. They are inherited by sub-workflows and persisted in checkpoints so resume continues to use the same instructions. Use the YAML `instructions:` list for workflow-pinned context, or pass `--workspace-instructions` on the CLI to auto-discover `AGENTS.md`, `CLAUDE.md`, `.github/copilot-instructions.md`, and `.github/instructions/**/*.instructions.md` (recursive; only files marked `applyTo: "**"` in YAML frontmatter are loaded — see the [Workspace Instructions section in the CLI reference](cli-reference.md#workspace-instructions) for full details) by walking from CWD up to the git root.

### Context Modes

- **`accumulate`** (default): Agents see all previous agent outputs
- **`snapshot`**: Agents see only the context at workflow start
- **`minimal`**: Agents see only their direct dependencies

## Agents

Agents are defined in the `agents` list. Each agent represents a unit of work.

```yaml
agents:
  - name: string                    # Required: Unique agent identifier
    description: string             # Optional: Purpose description
    type: agent                     # agent | human_gate | script | workflow | wait | terminate (default: agent)
    model: string                   # Optional: Model identifier (e.g., 'claude-sonnet-4.5')
    
    prompt: |                       # Required for type=agent: Agent instructions
      Multi-line prompt with Jinja2 templates
      {{ workflow.input.field }}
      {{ previous_agent.output.field }}
    
    input:                          # Optional: Explicit input declarations
      field_name:
        from: "{{ expression }}"
        type: string                # string | number | boolean | array | object
        required: true
    
    output:                         # Optional: Output schema for validation
      field_name:
        type: string
        description: "Field purpose"

    output_mode: raw                # Optional: raw | envelope (default: inferred)
                                    # raw: skip JSON extraction, wrap response
                                    #   as {"result": "<text>"}. Cannot be
                                    #   combined with output:.
                                    # envelope: explicit opt-in to structured
                                    #   output pipeline (same as default when
                                    #   output: is declared).
    
    tools:                          # Optional: Agent-specific tools
      - tool_name

    reasoning:                      # Optional: per-agent reasoning override
      effort: high                  # low | medium | high | xhigh | max
                                    # Overrides runtime.default_reasoning_effort.
                                    # Only valid on type=agent (rejected on
                                    # script, human_gate, workflow).
                                    # See docs/configuration.md#reasoning-effort.

    retry:                          # Optional: per-agent retry policy
      max_attempts: 3               # 1-10 (default 1 = no retry)
      backoff: exponential          # exponential | fixed
      delay_seconds: 2              # base delay before first retry
      retry_on:                     # error categories that trigger retry
        - provider_error
        - timeout
      max_parse_recovery_attempts: 3  # 0-10; omit for provider default

    context_tier: long_context      # Optional: per-agent context-tier override
                                    # default | long_context (Copilot only)
                                    # Overrides runtime.default_context_tier.
                                    # Composes with reasoning. Only valid on
                                    # type=agent (rejected on script,
                                    # human_gate, workflow).
                                    # See docs/configuration.md#context-tier.

    routes:                         # Optional: Routing logic
      - to: next_agent              # Agent name or $end
        when: "{{ condition }}"     # Optional: Route condition
```

### Retry Policy

Per-agent retry controls how an agent retries on transient failures. The `retry:` block is optional; when omitted the agent makes a single attempt with no retries.

```yaml
agents:
  - name: analyzer
    prompt: "Analyze the input"
    output:
      summary:
        type: string
    retry:
      max_attempts: 3
      backoff: exponential
      delay_seconds: 2
      retry_on:
        - provider_error
        - timeout
      max_parse_recovery_attempts: 0   # disable parse recovery for this agent
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_attempts` | `1-10` | `1` | Total attempts including the first. `1` = no retry. |
| `backoff` | `exponential \| fixed` | `exponential` | Backoff strategy between retries. |
| `delay_seconds` | `0.0-300.0` | `2.0` | Base delay in seconds before the first retry. |
| `retry_on` | list | `[provider_error, timeout]` | Error categories that trigger a retry. |
| `max_parse_recovery_attempts` | `0-10` | Provider default | In-session parse-recovery attempts before giving up. See below. |

#### `max_parse_recovery_attempts`

When an agent declares `output:` (structured JSON), the provider tries to parse JSON from the model's response. If parsing fails, a correction prompt is sent in the same session asking the model to fix its response format. This field controls how many correction prompts to send.

- **Omit** (default): Use the provider default (Copilot=5, Claude=2).
- **`0`**: Disable parse recovery entirely — fail immediately on bad JSON.
- **`1-10`**: Custom limit.

This is useful when you know an agent's output is simple and a single attempt should suffice, or when you want to fail fast instead of burning tokens on recovery loops.

### Choosing whether to declare `output:`

Declaring `output:` does two things at once: it asks the model to return JSON matching the schema, and it parses the response as structured JSON. For some agents that's what you want. For others it produces parse-recovery loops and burns tokens.

**Declare `output:`** when the agent emits small, strictly-structured JSON whose individual fields will be referenced downstream:

```yaml
agents:
  - name: classifier
    prompt: "Classify the input. Return {category, confidence}."
    output:
      category:
        type: string
      confidence:
        type: number
  - name: router
    prompt: |
      Category was {{ classifier.output.category }}.
      Confidence was {{ classifier.output.confidence }}.
```

**Omit `output:`** when the agent emits prose, Markdown, or large/nested JSON. Without a schema, conductor stores the full raw response as a single string under `.output.result`, and downstream agents read it directly:

```yaml
agents:
  - name: synthesizer
    prompt: |
      Produce a comprehensive Markdown report of the findings.
      The report may contain code blocks, tables, and quoted examples.
    # No output: declared — response is captured verbatim.
  - name: reviewer
    prompt: |
      Review the following report:

      {{ synthesizer.output.result }}
```

Why this matters: when an `output:` schema is declared, the model is asked to wrap its response in JSON. Large or prose-heavy responses tend to come back inside Markdown code fences, and any triple-backticks in the content can confuse the JSON-extraction step. Omitting `output:` for these agents avoids that whole class of failure and lets the model write naturally.

### `output_mode`

The `output_mode` field gives you explicit control over how the provider handles the agent's response. It accepts two values:

| `output_mode` | `output:` declared? | Behavior |
|---|---|---|
| *(not set)* | yes | Default structured-output pipeline: schema injected, JSON parsed and validated |
| *(not set)* | no | Raw response captured as `{"result": "<text>"}` |
| `raw` | no | Same as above, but makes intent explicit — useful for agents that must *never* attempt JSON extraction |
| `raw` | yes | **ValidationError** — these options are incompatible |
| `envelope` | yes | Same as the default structured pipeline (explicit opt-in) |
| `envelope` | no | Raw response captured as `{"result": "<text>"}` |

**Use `output_mode: raw`** when an agent produces large Markdown reports, code, or free-form prose. This bypasses JSON extraction entirely — no schema instructions are injected, no parse-recovery loop runs, and the model's full response is available as `{{ agent.output.result }}`:

```yaml
agents:
  - name: report_writer
    output_mode: raw
    prompt: |
      Write a detailed analysis report. Include code examples,
      tables, and any formatting you need.
    # No output: block — output_mode: raw is incompatible with output:
  - name: reviewer
    prompt: |
      Review the following report:

      {{ report_writer.output.result }}
```

**Use `output_mode: envelope`** when you want to make the structured-output intent explicit (equivalent to the default when `output:` is declared):

```yaml
agents:
  - name: classifier
    output_mode: envelope
    prompt: "Classify the input."
    output:
      category:
        type: string
      confidence:
        type: number
```

`output_mode` is only valid on provider-backed agents (the default type). It cannot be set on `script`, `human_gate`, or `workflow` agents.

### Working Directory

Regular LLM agents (provider-backed agents) and their MCP servers run in a specific working directory:

```yaml
agents:
  - name: repository_analyst
    working_dir: "./my-project-repo"     # Optional: working directory (Jinja2 template)
    prompt: |
      Examine the repository files and list any issues.
```

The `working_dir` field can be defined globally in `workflow.runtime.working_dir` or overridden on individual agents.

#### Precedence and Path Resolution

1. **Precedence:** The agent-level `working_dir` overrides the global `workflow.runtime.working_dir`. If neither is configured, the current directory of the parent process (`os.getcwd()`) is used.
2. **Jinja2 Rendering:** Both agent-level and runtime-level configurations support Jinja2 template rendering. This allows dynamic paths, such as directories derived from previous steps: `working_dir: "{{ find_repo.output.path }}"`.
3. **Relative Paths:** Relative paths are resolved against the directory containing the workflow YAML file. When the workflow file location is unknown, relative paths resolve against the current process directory.
4. **Lexical Normalization:** Paths are normalized lexically using `os.path.normpath`. The engine does not resolve symlinks dynamically.

#### Symlink Semantics

Because paths are normalized lexically instead of resolving to their real paths:
- Different symlink aliases pointing to the same folder are treated as distinct paths.
- For the Claude provider, distinct paths trigger separate MCP manager connections. This spawns separate MCP server subprocesses for each unique path alias.

#### Key Restrictions and Exclusions

- **Rejected Step Types:** The `working_dir` field is strictly rejected on `wait`, `set`, `terminate`, `human_gate`, and `workflow` (sub-workflow) step types. Defining `working_dir` on these steps raises a `ValidationError` at load time.
- **Script Steps:** `script` steps honor only their own `working_dir` field, rendered as a Jinja2 template. `workflow.runtime.working_dir` is not applied; relative paths are passed to the subprocess as-is and therefore resolve against the Conductor process cwd, not the workflow file directory; missing directories surface as subprocess startup `ExecutionError`s rather than the LLM-agent pre-provider working-dir check.
- **Dialog Turns:** The working directory isn't applied to dialog turns in the current version. Multi-turn interactions run in the process default directory.
- **Sub-Workflows:** A sub-workflow doesn't inherit the parent's working directory configuration. Instead, any relative paths in the child workflow resolve against the child workflow's own file directory.

> ⚠️ **Warning: Working directory is NOT a sandbox**
> Setting `working_dir` doesn't restrict the model's filesystem access. The model can still read and write files outside this directory if it uses absolute paths or parent directory traversals (e.g., `../`). Avoid relying on this configuration to sandbox untrusted model execution.

### Sandbox Configuration (ACA)

The optional per-agent `sandbox:` block overrides settings for the
experimental `aca` (Azure Container Apps) sandbox provider — the one
provider that *does* isolate an agent's execution off the host, running it
inside an Azure Container Apps dynamic-sessions custom-container pool
instead. See [`docs/providers/aca.md`](./providers/aca.md) for the full
provider documentation, architecture, and workflow-level
`runtime.provider: {name: aca, ...}` configuration.

```yaml
workflow:
  runtime:
    provider:
      name: aca
      pool_endpoint: "https://my-agent-pool.<region>.azurecontainerapps.io"
      api_version: "2025-07-01"
      inner_provider: copilot
      identifier_scope: agent       # workflow | agent | item | none (default: agent)
      egress: enabled               # enabled | disabled (advisory; pool governs). The
                                     # inner Copilot call always needs outbound network
                                     # access, so this is effectively always `enabled`.
      lifecycle: timed              # timed | on_container_exit (advisory)
      auth: azure_default           # only supported strategy

agents:
  - name: implement
    sandbox:                        # Optional: aca-only per-agent overrides
      identifier_scope: item        # overrides runtime.provider.identifier_scope
      working_dir: /workspace       # container-relative — NOT a host path
```

| Field | Type | Description |
|-------|------|-------------|
| `identifier_scope` | `workflow \| agent \| item \| none` | Overrides the workflow-wide `identifier_scope` for this agent's session identifier. `None` (default) inherits the workflow setting. |
| `working_dir` | `string` | Working directory **inside the sandbox session filesystem**. Unlike the top-level `agent.working_dir` above (a *host* path resolved against the workflow file's directory), this is interpreted container-relative — a path inside the remote ACA session, never resolved against the host. Defaults to the runner's own working directory when unset. **The directory must already exist when the session starts** (e.g. baked into the runner image, or a parent directory an earlier turn in the same reused session created) — a path that doesn't exist yet is a runtime error, not a silent fallback. See [`examples/aca-coding-agent.yaml`](../examples/aca-coding-agent.yaml) for the pattern of pointing `working_dir` at an image-provisioned parent directory and having the agent itself create a subdirectory (e.g. `git clone` into it) on first run. |


`sandbox:` is only meaningful when the agent's effective provider is
`aca` — the fields validate structurally regardless of provider (so
`conductor validate` still checks types), but are otherwise ignored by
every other provider.

### Human Gates

Human gates pause workflow execution for user input:

```yaml
agents:
  - name: approval_gate
    type: human_gate
    description: "Approve the proposed changes"
    
    options:                        # Required: List of choices
      - name: approve
        description: "Approve and proceed"
      - name: revise
        description: "Request revisions"
      - name: reject
        description: "Reject the proposal"
    
    routes:
      - to: implementer
        when: "{{ approval_gate.choice == 'approve' }}"
      - to: reviser
        when: "{{ approval_gate.choice == 'revise' }}"
      - to: $end
        when: "{{ approval_gate.choice == 'reject' }}"
```

#### Markdown in Gate Prompts

Gate prompts support full **Markdown formatting**. In the terminal, prompts are rendered with Rich Markdown (headings, bold, lists, code blocks). In the web dashboard, prompts render as styled HTML with interactive features:

- **Headings, bold, lists, code blocks** — all standard Markdown syntax is rendered
- **Tables** — GitHub Flavored Markdown (GFM) pipe tables are supported
- **File links** — relative file paths in the prompt (e.g., `./src/plan.md`) are auto-detected and rendered as clickable links that open in VS Code
- **URLs** — bare `http://` and `https://` URLs are auto-linked

```yaml
agents:
  - name: review_gate
    type: human_gate
    description: "Review the generated plan"
    prompt: |
      ## Review Required

      The planner produced the following artifacts:

      | File | Purpose |
      |------|---------|
      | ./output/plan.md | Implementation plan |
      | ./output/timeline.md | Delivery timeline |

      Please review the files above and choose how to proceed.
      See also: https://wiki.example.com/review-guidelines

    options:
      - name: approve
        description: "Looks good — proceed"
      - name: revise
        description: "Needs changes"
```

The auto-linkify processor is Markdown-aware: it skips fenced code blocks, inline code spans, and existing markdown links. File paths are validated against the workflow root directory (path traversal is blocked).

### Script Steps

Script steps run shell commands as workflow steps, capturing stdout, stderr, and exit code. Use them to integrate shell scripts, run tests, or invoke external tools without an AI agent.

```yaml
agents:
  - name: run_tests
    type: script
    description: "Run the test suite"           # Optional
    command: pytest                             # Required: command to execute (Jinja2 template)
    args:                                       # Optional: list of arguments (each Jinja2 template)
      - "{{ workflow.input.test_path }}"
      - "--verbose"
    env:                                        # Optional: environment variables for subprocess
      CI: "true"
      PYTHONPATH: "/app/src"
    working_dir: "/app"                         # Optional: working directory (Jinja2 template)
    timeout: 120                                # Optional: per-step timeout in seconds
    stdin: "{{ planner.output | tojson }}"      # Optional: payload piped to the child's stdin (Jinja2 template)
    routes:
      - to: analyzer
        when: "exit_code == 0"
      - to: error_handler
```

**Output structure** — script step output is always available in context as:

| Field | Type | Description |
|-------|------|-------------|
| `stdout` | string | Captured standard output |
| `stderr` | string | Captured standard error |
| `exit_code` | integer | Process exit code (0 = success) |

**JSON stdout auto-parsing** — if `stdout` is valid JSON _and_ the parsed value is an object, its fields are merged into the agent's output dict alongside `stdout`/`stderr`/`exit_code`. This lets you route on parsed fields directly instead of opaque exit codes:

```yaml
# Script writes to stdout: {"route": "planning", "issue_count": 3}
agents:
  - name: detector
    type: script
    command: pwsh
    args: ["-File", "{{ workflow.dir }}/scripts/detect.ps1"]
    routes:
      - to: planner
        when: "route == 'planning'"          # parsed field
      - to: scaler
        when: "issue_count > 100"            # parsed field
      - to: $end
```

JSON arrays and scalars are ignored (only objects merge). Non-JSON stdout is unchanged. Parsed fields shadow `stdout`/`stderr`/`exit_code` if a script outputs those as JSON keys.

**Declared output schema (strict mode)** — script steps can also declare an `output:` schema using the same syntax as LLM agents. When declared, conductor enforces a strict contract: stdout must be a single JSON object, the JSON gets merged onto the `{stdout, stderr, exit_code}` baseline, and the **merged dict** is validated against the schema. If any check fails the workflow aborts with a `ValidationError`:

```yaml
agents:
  - name: detector
    type: script
    command: pwsh
    args: ["-File", "{{ workflow.dir }}/scripts/detect.ps1"]
    output:
      route:
        type: string
        description: Which phase to enter next
      issue_count:
        type: number
    routes:
      - to: planner
        when: "route == 'planning'"
      - to: scaler
        when: "issue_count > 100"
      - to: $end
```

Strict-mode semantics:

- **stdout must be a single JSON object.** Non-JSON, empty stdout, JSON arrays, JSON scalars, and JSON followed by additional text (e.g. log lines) all fail validation with the underlying JSON parser error surfaced for diagnostics. Reserve stdout for the JSON payload and write logs to `stderr`.
- **Missing or wrong-typed fields fail validation.** Extra fields beyond the schema are kept in the output dict (the validator only enforces declared fields — the same loose-extras policy that LLM-agent structured outputs use).
- **Validation runs on the merged dict, not the raw JSON.** The `stdout`/`stderr`/`exit_code` built-ins are always present in the dict, with parsed JSON keys overlaid on top. Declaring `exit_code: { type: number }` asserts the built-in matches; if the script emits a shadowing JSON key (e.g. `{"exit_code": "ok"}`), the schema validates the shadowed value.
- **Failure semantics.** On schema-validation failure, the engine emits `script_failed` (not `script_completed`) and aborts the workflow. The failure event carries the captured stdout, stderr, and exit_code so dashboards and logs can show what the script actually wrote.
- **`output: {}` opts into strict mode with zero required fields** — useful when you want the JSON-object enforcement without listing fields yet.

Note: this is **structural** parity with LLM agents — the script must emit clean JSON to stdout. The JSON-recovery heuristics LLM agents use (extracting JSON from code fences, wrapping non-object payloads) intentionally do not apply to scripts, which are deterministic.

Omit `output:` to keep the lenient auto-merge behavior described above.

Access in downstream agents:

```yaml
prompt: |
  The test run produced:
  {{ run_tests.output.stdout }}
  Exit code: {{ run_tests.output.exit_code }}
```

**Routing on exit code** — use `exit_code` in route conditions to branch on success or failure:

```yaml
routes:
  - to: success_handler
    when: "exit_code == 0"           # simpleeval syntax
  - to: failure_handler
    when: "{{ output.exit_code != 0 }}"  # Jinja2 syntax
  - to: $end
```

**Restrictions** — script steps cannot have `prompt`, `model`, `provider`, `tools`, `system_prompt`, `options`, or `validator`. Script steps also cannot be used inside `parallel` groups or `for_each` groups.

**Environment variable note** — values in `env` are passed as-is to the subprocess (they are not rendered as Jinja2 templates). Use `${VAR}` syntax in the workflow YAML loader if you need environment variable substitution in env values.

**Passing payloads via stdin** — set `stdin:` to pipe a rendered payload to the script's standard input instead of (or in addition to) command-line `args`. This is the cross-platform way to hand large or structured data to a script: command-line arguments are subject to OS length limits (notably Windows, where the total command line is capped at ~32 KB), but stdin is not. Reach for `stdin:` whenever a script consumes an upstream agent's structured output.

```yaml
agents:
  - name: analyze
    type: script
    command: python3
    args: ["scripts/analyze.py"]
    stdin: "{{ evaluator.output.evaluations | tojson }}"   # JSON payload via the tojson filter
    routes:
      - to: $end
```

- **`stdin:` is a Jinja2 string template**, rendered against the workflow context and written to the child as UTF-8.
  - For JSON, use the built-in `tojson` filter: `stdin: "{{ data | tojson }}"`. Plain `{{ data }}` renders a Python `repr` (single-quoted), which is **not** valid JSON.
  - For arbitrary text — a diff, CSV, or a prompt — use it directly: `stdin: "{{ patch }}"`.
  - The script reads it like any stdin source: `data = json.load(sys.stdin)` (Python), or pipe into `jq` / `cat` (shell).
- **Omitting `stdin`** keeps the legacy behavior — the child inherits the parent's stdin.
- **An explicit empty string** (`stdin: ""`) still pipes, sending the child immediate EOF (distinct from omitting it).
- **`stdin` and `args` are orthogonal.** When both are set, `args` are passed on the command line *and* `stdin` is piped — there is no precedence conflict. Keep flags in `args` and put the bulky/structured payload in `stdin`.

This replaces the older pattern of writing large structured arguments to a temp file and passing `--something-file <path>`; the engine pipes the payload directly, so there is no temp file to manage or clean up.

### Wait Steps

Wait steps pause workflow execution for a parsed duration via in-process `asyncio.sleep`. Use them for rate-limit cooldowns, polling intervals, and external-system catch-up — cross-platform, no shell `sleep` dependency.

```yaml
agents:
  - name: cooldown
    type: wait
    description: "Cool down between API bursts"     # Optional
    duration: 60s                                   # Required: see "Duration format" below
    reason: "Avoiding rate limit"                   # Optional: shown in dashboard
    routes:
      - to: next_step
```

**Duration format** — `duration` accepts:

- A plain `int` or `float` (seconds): `duration: 60`, `duration: 1.5`.
- A string with a unit suffix: `ms` (milliseconds), `s` (seconds), `m` (minutes), `h` (hours). Examples: `"500ms"`, `"60s"`, `"2.5m"`, `"1h"`.
- A Jinja2 template that renders to one of the above. Templated durations defer literal validation to runtime:

  ```yaml
  duration: "{{ workflow.input.poll_interval_seconds }}s"
  ```

The resolved duration must be **greater than 0 and no more than 24 hours** (`86400s`). Longer pauses should reconsider `workflow.limits.timeout_seconds` first.

**Output structure** — wait step output is strict — only `waited_seconds` is exposed:

| Field | Type | Description |
|-------|------|-------------|
| `waited_seconds` | `number` | Wall-clock seconds actually slept (may be less than requested on interrupt) |

Access in templates: `{{ cooldown.output.waited_seconds }}`.

**Polling pattern** — wait composes with routing loop-backs to build polling workflows without writing any Python:

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
      - to: check_status                           # loop back

  - name: process_result
    # ...
```

**Cancellation** — `Esc` / `Ctrl+G` cancels an in-progress wait immediately (the engine races the sleep against the interrupt event). The workflow-level `limits.timeout_seconds` also cancels in-flight waits via the standard timeout path.

**Iteration counting** — wait steps count toward `workflow.limits.max_iterations` (each pause is one step). They are not subject to `max_agent_iterations`, which counts per-LLM-agent tool iterations.

**Restrictions** — wait steps cannot have `prompt`, `model`, `provider`, `tools`, `system_prompt`, `options`, `command`, `args`, `env`, `working_dir`, `timeout`, `workflow`, `input_mapping`, `max_depth`, `max_session_seconds`, `max_agent_iterations`, `retry`, `dialog`, `reasoning`, `validator`, `timeout_seconds`, or `output`. Wait steps also cannot be used inside `parallel` groups or `for_each` groups.

See [`examples/wait-step.yaml`](../examples/wait-step.yaml) for a complete polling workflow.
### Set Steps

Set steps evaluate one or more Jinja2 expressions and bind the typed results into the workflow context. No LLM call, no subprocess, no I/O — they're pure context transformations. Use them to combine inputs, derive flags from prior outputs, compute defaults, or normalise a value once for many downstream prompts to share.

```yaml
agents:
  # Single binding — output is the typed scalar / list / dict.
  - name: compute_slug
    type: set
    value: "{{ workflow.input.org }}/{{ workflow.input.repo }}"
    # accessible as: compute_slug.output  (a string)
    routes:
      - to: derive_flags

  # Multi-binding — output is a dict, accessible as step.output.<key>.
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

Exactly one of `value:` or `values:` must be present.

**Type detection** — by default, the rendered string is parsed with safe YAML (equivalent to `yaml.safe_load`); booleans, numbers, lists, and dicts are returned as native types. Parse failures and pure-comment renders fall back to the raw string. Empty / whitespace-only renders become `""`, not `None`. `yaml.safe_load` produces `datetime`/`date`/`time` objects from strings like `"2024-01-02"`; these are converted to their ISO 8601 string form so checkpoint round-trips and dashboard payloads stay JSON-safe. Any other non-JSON-safe Python value raises `ExecutionError`.

**Explicit `output_type:`** (single `value:` only) forces a specific coercion:

| Value | Behaviour |
|-------|-----------|
| `auto` (default) | YAML safe-load with the rules above |
| `string` | Keep the raw rendered string verbatim |
| `number` | Try `int` then `float`; raise on failure |
| `integer` | `int`; raise on failure |
| `boolean` | Case-insensitive `true`/`false`/`1`/`0`/`yes`/`no`/`y`/`n`/`on`/`off` |
| `list` | Parse via YAML; assert the result is a list |
| `dict` | Parse via YAML; assert the result is a dict |

Per-key typing on multi `values:` is not supported.

**Multi-binding ordering** — every binding in a single `values:` step renders against the *original* pre-step context. Later bindings cannot reference earlier ones in the same step. If you need ordered dependencies, chain multiple set steps:

```yaml
- name: step_a
  type: set
  value: "{{ workflow.input.x | upper }}"
- name: step_b
  type: set
  value: "{{ step_a.output }}-suffix"
```

**Routing on set output** — routes attached to a set step evaluate against the bound value directly. Dict outputs expose `{{ output.<key> }}` (Jinja2) and bare `<key>` (simpleeval); scalar / list outputs expose only `{{ output }}`:

```yaml
# Multi-values step — route on a derived dict field.
- name: derive_flags
  type: set
  values:
    is_breaking: "{{ severity == 'high' }}"
  routes:
    - to: breaking_path
      when: "{{ output.is_breaking }}"
    - to: safe_path

# Single-value step — route on the scalar itself.
- name: flag
  type: set
  value: "{{ workflow.input.severity == 'high' }}"
  routes:
    - to: hi
      when: "{{ output }}"
    - to: lo
```

**Optional output schema** — set steps support the same `output:` schema as LLM and script agents, but only when the rendered value is a dict (which is always the case for multi `values:`, and may be the case for single `value:`). If a single-`value:` step declares `output:` but produces a scalar / list, the engine raises a friendly `ValidationError` pointing to `values:` as the intended shape.

**Composition** — set steps are allowed inside `parallel` groups (each member publishes its bound value to context) and as the inline agent of a `for_each` group (one bound value per item). Inside a parallel group, set templates cannot reference sibling group members (the validator catches this at config time, since the engine renders against a pre-group snapshot).

**Restrictions** — set agents cannot have `prompt`, `model`, `provider`, `tools`, `system_prompt`, `command`, `args`, `env`, `working_dir`, `timeout`, `workflow`, `options`, `input_mapping`, `max_depth`, `retry`, `dialog`, `reasoning`, `validator`, `timeout_seconds`, `max_session_seconds`, or `max_agent_iterations`. They count toward `limits.max_iterations` like any other step.

**Events** — set steps emit `set_started` / `set_completed` / `set_failed` (mirroring the script-step lifecycle) in all three positions: linear main loop, parallel group member, and for-each iteration. The `set_completed` payload carries `output_type`, `output_keys` (sorted, empty for scalars), and `value_repr` (a JSON-safe preview, truncated at 512 chars).

### Sub-Workflow Steps

Sub-workflow steps reference external workflow YAML files, enabling composable and reusable workflow building blocks. The sub-workflow runs as a black box — its internal agents are not visible to the parent.

```yaml
agents:
  - name: deep_research
    type: workflow
    workflow: ./research-pipeline.yaml   # Required: path to sub-workflow YAML
    input:                               # Optional: explicit input declarations
      - workflow.input.topic
    input_mapping:                       # Optional: per-call inputs to the sub-workflow
      topic: "{{ workflow.input.topic }}"
      depth: "{{ research_planner.output.depth }}"
    max_depth: 3                         # Optional: per-agent recursion cap
                                         #   (additionally bounded by global
                                         #   MAX_SUBWORKFLOW_DEPTH = 10)
    output:                              # Optional: output schema for validation
      findings:
        type: string
    routes:
      - to: synthesizer
```

**Key semantics:**

- The `workflow` field can be:
  - A local file path: `./research-pipeline.yaml` (resolved relative to the parent)
  - A configured registry reference: `qa-bot@team#v1.2.3` (see [Workflow Registry](design/registry.md))
  - An ad-hoc GitHub reference: `analysis@myorg/team-a#main` (owner/repo fetched directly from GitHub)
- Sub-workflow inherits the parent's provider configuration
- Sub-workflow output is stored in context and accessible via `{{ agent_name.output.field }}`
- Recursive composition is supported (sub-workflows can reference other sub-workflows) with a global depth limit of `MAX_SUBWORKFLOW_DEPTH = 10`
- Self-referential sub-workflows (a workflow referencing itself) are allowed; depth is bounded by the global cap and the optional per-agent `max_depth` field
- `input_mapping` keys are sub-workflow input names; each value is a Jinja2 expression evaluated against the parent's context. When `input_mapping` is omitted, the parent's `workflow.input.*` is forwarded to the sub-workflow as before

**Access sub-workflow output in downstream agents:**

```yaml
prompt: |
  The research findings were:
  {{ deep_research.output.findings }}
```

**Workflow reference types** — the `workflow` field supports three forms:

```yaml
agents:
  # Local file path (relative to parent workflow)
  - name: local_pipeline
    type: workflow
    workflow: ./shared/research-pipeline.yaml

  # Configured registry reference
  - name: registry_pipeline
    type: workflow
    workflow: qa-bot@team#v1.2.3

  # Ad-hoc GitHub reference (no registry setup required)
  - name: adhoc_pipeline
    type: workflow
    workflow: analysis@myorg/team-a#main
    input_mapping:
      data: "{{ workflow.input.raw_data }}"
```

The ad-hoc form (`workflow@owner/repo[#ref]`) allows cross-team workflow
composition without pre-configuring registries. See
[Ad-hoc References](design/registry.md#ad-hoc-references) in the registry design
doc for details on caching, authentication, and ref resolution.

**Sub-workflows in `for_each` groups** — `type: workflow` agents can be used inside `for_each` groups to fan out one sub-workflow run per item in the source array. Each iteration receives its own `input_mapping` evaluated against the loop variable, and emits its own `subworkflow_started` / `subworkflow_completed` events:

```yaml
parallel:
  - name: plan_issues
    for_each:
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

**Restrictions** — workflow steps cannot have `prompt`, `model`, `provider`, `tools`, `system_prompt`, `command`, `options`, or `validator`.

### Terminate Steps

Terminate steps end the workflow with an explicit `status` (`success` or `failed`) and a structured `reason`. Reaching a terminate step ends execution immediately — no routes are evaluated after — and produces a CLI exit code, dashboard state, and event payload that downstream tooling can distinguish from a generic crash.

```yaml
agents:
  - name: precheck
    type: script
    command: bash
    args: ["-c", "echo '{\"action\":\"abort\",\"reason\":\"unsafe input\"}'"]
    output:
      action:  { type: string }
      reason:  { type: string }
    routes:
      - when: "action == 'abort'"
        to: abort_unsafe
      - when: "action == 'noop'"
        to: noop_exit
      - to: main_pipeline

  # Soft success — workflow ends cleanly, exit 0, dashboard ✅.
  - name: noop_exit
    type: terminate
    status: success
    reason: "Document already up to date; no edits needed."

  # Hard failure with reason — workflow ends, exit 1, dashboard ❌.
  - name: abort_unsafe
    type: terminate
    status: failed
    reason: "{{ precheck.output.reason }}"
    output_template:                  # optional; replaces workflow.output
      aborted: "true"                 # rendered then JSON-coerced to True
      stage: precheck
      reason: "{{ precheck.output.reason }}"
```

**Behaviour**

| `status` | CLI exit code | Dashboard | Event | Resumable? |
|----------|---------------|-----------|-------|------------|
| `success` | `0` | ✅ | `workflow_completed { termination_reason, terminated_by, is_explicit: true, status: "success" }` | n/a (clean exit) |
| `failed`  | `1` | ❌ | `workflow_failed { error_type: "WorkflowTerminated", termination_reason, terminated_by, is_explicit: true, status: "failed", output }` | **No** — explicit terminations skip the on-failure checkpoint |

**Final output** — when `output_template:` is set, it *replaces* the workflow-level `output:` mapping for this termination path. Each rendered value is passed through the same JSON-coercion helper used elsewhere in the engine, so `"true"` becomes `True`, `"42"` becomes `42`, and JSON literals are parsed. When `output_template:` is omitted, the workflow-level `output:` is rendered as on any other terminal path.

**Restrictions** — terminate steps cannot have `routes`, `tools`, `output`, `prompt`, `model`, `provider`, `system_prompt`, `command`, `args`, `env`, `working_dir`, `timeout`, `timeout_seconds`, `max_session_seconds`, `max_agent_iterations`, `max_depth`, `retry`, `dialog`, `reasoning`, `validator`, `workflow`, `input_mapping`, or `options`. They cannot appear as members of a parallel group or as a `for_each` inline agent — route to them from those groups' `routes:` instead.

**Sub-workflow boundary** — a `status: failed` terminate inside a sub-workflow is downgraded to a `SubworkflowTerminatedError` (subclass of `ExecutionError`) at the parent boundary so the parent treats it as a normal sub-workflow failure (its own `workflow_failed` does NOT inherit `is_explicit: true`). The child's rendered output, reason, and terminate step name are preserved on the wrapper as `terminated_output`, `terminated_reason`, and `terminated_by` for `on_error` hooks and debugging surfaces. A `status: success` terminate inside a sub-workflow returns its rendered output cleanly and the parent continues with its next routes.

See [`examples/terminate.yaml`](../examples/terminate.yaml) for a complete worked example with all three paths.

### Dialog Mode

Dialog mode allows agents to conditionally pause after execution and enter a free-form conversation with the user. An LLM evaluator examines the agent's output against user-defined criteria and decides whether to initiate a dialog.

```yaml
agents:
  - name: researcher
    prompt: "Research the given topic thoroughly"
    dialog:
      trigger_prompt: |
        Enter dialog if the agent expresses uncertainty about
        the user's intent, encounters ambiguous requirements,
        or needs clarification before proceeding.
    routes:
      - to: writer
```

When triggered, the user is presented with a choice:
1. **Discuss** — engage in a multi-turn conversation with the agent
2. **Do your best and continue** — skip the dialog and let the agent proceed

After the conversation, the agent re-executes with the dialog transcript as additional context, producing a refined output.

**Configuration:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `dialog.trigger_prompt` | string | Yes | Criteria for the LLM evaluator to decide when dialog is needed |

**Behavior notes:**
- Dialog is supported on regular `agent` type only (not `human_gate`, `script`, `workflow`, or `wait`)
- In web dashboard mode, the dialog temporarily replaces the graph area with a chat interface
- When `--skip-gates` is set (e.g., CI/automation), dialogs are automatically skipped
- The evaluator prompt should describe *when* to trigger dialog, not *what* to ask — the evaluator generates the opening question from the agent's output context
- After dialog, the agent sees the full conversation transcript and produces updated output

### Validator

A `validator:` block runs a **second LLM call** after a provider-backed agent completes, grading its output against a user-defined rubric. If validation fails, the agent is re-run **once** with the validator's feedback appended. This is distinct from `retry:` (transient failures, same prompt) and the `output:` schema (shape/type, not content quality) — it catches output that is structurally valid but semantically wrong, incomplete, or off-rubric.

```yaml
agents:
  - name: code_reviewer
    model: claude-sonnet-4-5
    prompt: "Review the diff for bugs.\n{{ workflow.input.diff }}"
    output:
      summary: { type: string }
      issues:  { type: array }
    validator:
      model: claude-sonnet-4-5   # optional; defaults to the agent's model
      criteria: |
        Verify the review identifies all null-safety issues, every suggestion
        is actionable, and no function names are fabricated.
      max_retries: 1
```

**Mechanics:**
1. The primary agent runs and produces output.
2. The validator runs a second LLM call that receives the agent's rendered prompt, its output, and the `criteria`, and must answer `{ "passed": bool, "issues": [str, ...] }`.
3. If `passed` is true, the output flows downstream unchanged.
4. If `passed` is false and `max_retries > 0`, the agent re-runs once with a `## Validation feedback` section (the issues) appended to its prompt. The second output is taken as final — there is no second validation loop.

**Configuration:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `validator.criteria` | string | Yes | The rubric the output is graded against. Describe what a *good* output looks like (the checks to perform). |
| `validator.model` | string | No | Model for the validator call. Defaults to the primary agent's model. |
| `validator.max_retries` | int | No | Re-runs on failure. Default `1`, **hard-capped at 1**. `0` = validate-and-report without re-running. |

**Behavior notes:**
- Supported on provider-backed `agent` steps only (not `human_gate`, `script`, `workflow`, `wait`, `set`, or `terminate`). Works in the main loop, parallel groups, and for-each loops.
- The validator uses the primary agent's provider; only `model` is overridable.
- **Fail-open:** if the validator call errors or returns unparseable output, it is treated as a pass (with a logged warning) so a flaky grader never blocks the workflow.
- The validator sees only the agent's prompt + output + criteria, not other agents' outputs — keeping validation focused and cheap.
- Validator (and any discarded first attempt) token cost is reported as a separate `<agent> (validator)` row in the usage summary.
- Emits `agent_validator_start`, `agent_validator_complete`, and `agent_validation_failed` events, surfaced in the web dashboard and `--verbose` console output.

## Parallel Groups

Parallel groups execute multiple agents concurrently for improved performance.

### Static Parallel Groups

Execute a fixed list of agents in parallel:

```yaml
parallel:
  - name: string                    # Required: Group identifier
    description: string             # Optional: Purpose description
    
    agents:                         # Required: Agents to run in parallel
      - agent_name_1
      - agent_name_2
      - agent_name_3
    
    failure_mode: fail_fast         # Required: Error handling strategy
                                    # Options: fail_fast | continue_on_error | all_or_nothing
    
    routes:                         # Optional: Routes after parallel execution
      - to: next_agent
        when: "{{ condition }}"
```

### Dynamic Parallel (For-Each) Groups

Execute an agent template for each item in an array determined at runtime:

```yaml
for_each:
  - name: string                    # Required: Group identifier
    type: for_each                  # Required: Marks this as for-each group
    description: string             # Optional: Purpose description
    
    source: string                  # Required: Reference to array in context
                                    # Example: "finder.output.items"
    
    as: string                      # Required: Loop variable name
                                    # Available in templates as {{ <var> }}
                                    # Reserved names: workflow, context, output, _index, _key
    
    agent:                          # Required: Inline agent definition
      model: string                 # Optional: Model override
      prompt: |                     # Required: Template with {{ <var> }}
        Process {{ item }}
        Index: {{ _index }}         # Zero-based item index
        {% if _key is defined %}
        Key: {{ _key }}             # Extracted key (if key_by specified)
        {% endif %}
      output:                       # Optional: Output schema
        result: { type: string }
    
    max_concurrent: 10              # Optional: Concurrent execution limit
                                    # Default: 10
    
    failure_mode: fail_fast         # Optional: Error handling strategy
                                    # Default: fail_fast
    
    key_by: string                  # Optional: Path for dict-based outputs
                                    # Example: "item.id" → outputs["123"]
    
    routes:                         # Optional: Routes after execution
      - to: next_agent
```

**Loop Variables:**

For-each agents have access to special loop variables in addition to the custom loop variable defined by `as`:

- `{{ <var_name> }}` - Current item from array (e.g., `{{ kpi }}`, `{{ item }}`)
- `{{ _index }}` - Zero-based index of current item (0, 1, 2, ...)
- `{{ _key }}` - Extracted key value (only if `key_by` is specified)

**Reserved Variable Names:**

The following names cannot be used for the `as` parameter:
- `workflow` - Reserved for workflow inputs
- `context` - Reserved for execution metadata
- `output` - Reserved for agent outputs
- `_index` - Reserved for item index
- `_key` - Reserved for extracted key

### Failure Modes

- **`fail_fast`** (recommended): Stop immediately on first agent failure
- **`continue_on_error`**: Run all agents; proceed if at least one succeeds
- **`all_or_nothing`**: Run all agents; fail if any agent fails

### Accessing Parallel Outputs

Downstream agents can access parallel group outputs using Jinja2 templates:

#### Static Parallel Groups

```yaml
agents:
  - name: summarizer
    prompt: |
      Summarize the research findings:
      
      Web research: {{ parallel_researchers.outputs.web_researcher.summary }}
      Academic research: {{ parallel_researchers.outputs.academic_researcher.summary }}
      News research: {{ parallel_researchers.outputs.news_researcher.summary }}
```

Structure:
- `{{ group_name.outputs.agent_name.field }}` - Access successful agent output
- `{{ group_name.errors.agent_name.message }}` - Access error details (if `continue_on_error` mode)

#### For-Each Groups

```yaml
agents:
  - name: aggregator
    prompt: |
      Process these results:
      
      # Index-based access (when key_by not specified)
      First result: {{ processors.outputs[0].result }}
      Second result: {{ processors.outputs[1].result }}
      
      # Key-based access (when key_by is specified)
      KPI-123 result: {{ analyzers.outputs["KPI-123"].analysis }}
      
      # Iterate over all outputs
      {% for result in processors.outputs %}
      - {{ result | json }}
      {% endfor %}
      
      # Access loop metadata
      Total processed: {{ processors.outputs | length }}
      
      # Check for errors
      {% if processors.errors %}
      Failed items: {{ processors.errors | length }}
      {% endif %}
```

Structure:
- **Without `key_by`**: `{{ group_name.outputs[index].field }}` - Array access
- **With `key_by`**: `{{ group_name.outputs["key"].field }}` - Dict access
- `{{ group_name.errors }}` - Dict of failed items (if `continue_on_error` or `all_or_nothing`)

## Routes

Routes define workflow control flow. Routes are evaluated in order, and the first matching route is taken.

### Basic Route

```yaml
routes:
  - to: next_agent                  # Agent name or $end
```

### Conditional Route

```yaml
routes:
  - to: approver
    when: "{{ quality_score >= 8 }}"
  - to: reviser
    when: "{{ quality_score < 8 }}"
  - to: $end                        # Default fallback
```

### Route Expressions

Routes support Jinja2 templates and simpleeval expressions:

```yaml
# Jinja2 syntax (recommended)
when: "{{ agent.output.status == 'success' }}"
when: "{{ agent.output.score > 5 and agent.output.valid }}"

# simpleeval syntax (legacy)
when: "status == 'success'"
when: "score > 5 and valid"
```

### Special Destinations

- `$end` - Terminate workflow successfully
- Agent names must match an existing agent or parallel group name

## Inputs and Outputs

### Workflow Inputs

Define expected inputs in the `input` section:

```yaml
input:
  question:
    type: string
    required: true
    description: "The question to answer"
  
  context:
    type: string
    required: false
    default: "No additional context provided"
```

Access in agents: `{{ workflow.input.question }}`

**Optional inputs without an explicit `default`** resolve to type-appropriate zero values rather than `None`, so templates render cleanly:

| Input `type` | Zero value |
|---|---|
| `string` | `""` |
| `number` | `0` |
| `boolean` | `false` |
| `array` | `[]` |
| `object` | `{}` |

This means `{{ workflow.input.optional_msg | default("fallback") }}` correctly renders `"fallback"` when `optional_msg` is omitted, instead of the literal string `"None"`.

### Workflow Metadata Variables

In addition to `workflow.input.*`, every agent has access to:

| Variable | Description |
|---|---|
| `workflow.name` | Workflow name from the YAML |
| `workflow.description` | Workflow description from the YAML |
| `workflow.dir` | Absolute path to the directory containing the workflow YAML |
| `workflow.file` | Absolute path to the workflow YAML file |

These are available in **all** context modes (they're metadata, not inputs). `workflow.dir` is particularly useful for registry-hosted workflows that need to reference co-located scripts or assets without depending on the caller's working directory:

```yaml
agents:
  - name: detector
    type: script
    command: pwsh
    args:
      - "-File"
      - "{{ workflow.dir }}/scripts/detect-state.ps1"
```

### Workflow Outputs

Define the final workflow output:

```yaml
output:
  answer: "{{ answerer.output.answer }}"
  confidence: "{{ answerer.output.confidence }}"
  sources: "{{ researcher.output.sources }}"
```

### Agent Outputs

Define expected output schema for validation:

```yaml
agents:
  - name: analyzer
    output:
      score:
        type: number
        description: "Quality score 1-10"
      summary:
        type: string
        description: "Brief summary"
      recommendations:
        type: array
        description: "List of recommendations"
```

## Limits and Safety

Configure safety limits to prevent runaway workflows:

```yaml
workflow:
  limits:
    max_iterations: 50              # Maximum agent executions (1-500, default: 10)
    timeout_seconds: 1800           # Maximum wall-clock time in seconds (optional)
    budget_usd: 5.00                # Cumulative cost cap in USD (optional)
    budget_mode: audit              # audit | enforce (default: audit)
```

### Iteration Counting

- Each agent execution counts as 1 iteration
- Parallel agents count individually (3 parallel agents = 3 iterations)
- Loop-back patterns increment the counter on each iteration
- Script steps and wait steps each count as 1 iteration

### Timeout Behavior

- Workflow terminates when `timeout_seconds` is exceeded
- Includes all agent execution time and overhead
- `None` (default) means no timeout

### Cost Budget

- `budget_usd` caps cumulative LLM cost across the run. When unset (default), no
  budget tracking occurs.
- `budget_mode: audit` (default) emits a `budget_exceeded` event and logs a
  warning on first overshoot, but the workflow continues — use this to discover
  cost profiles before enforcing.
- `budget_mode: enforce` emits a `budget_exceeded` event, saves a checkpoint,
  and stops the workflow with `BudgetExceededError`. Resuming with
  `conductor resume <workflow.yaml>` starts a fresh budget window (cumulative
  spend resets to $0); raising `budget_usd` first is optional.
- Sub-workflow (`type: workflow`) spend is merged into the parent's budget, so
  a parent-level budget accounts for cost incurred by delegated workflows.
- Recommended graduation path:
  1. Run without `budget_usd` to observe costs in the summary
  2. Add `budget_usd` in `audit` mode to track overshoots non-disruptively
  3. Switch to `enforce` once the cost profile is understood

See [configuration.md](configuration.md#limits) for the budget
configuration reference and notes on how budget tracking integrates with the
provider usage callbacks.

### Model Pricing & Cost Reporting

The end-of-run summary reports per-agent and total cost. Pricing for a model is
resolved in this order (first hit wins):

1. **Workflow `cost.pricing` override** — per-model rates you supply in the
   workflow file (highest precedence; treated as intent).
2. **Provider hook** — a provider that knows its own rates supplies them at
   runtime. The **Copilot** provider derives live pricing from the SDK's
   per-model billing metadata, so newly released models are priced without a
   table update. Providers whose SDK exposes no pricing (e.g. the Anthropic API)
   skip this step.
3. **Built-in table** — a static `DEFAULT_PRICING` table of common models.
4. **Unavailable** — if none of the above match, the agent is **unpriced**.

**Unpriced agents are surfaced, not silently dropped.** When a run mixes priced
and unpriced agents, the total is shown as a partial (e.g. `Total: ~$0.4200
(2 agents unpriced: model-a, model-b)`) rather than a clean-looking number that
hides missing spend. The web dashboard shows the same `~$X (N unpriced)` marker.
When *no* model can be priced, the summary reads `Cost data unavailable`.

To price an unknown model yourself, add a `cost.pricing` override:

```yaml
workflow:
  cost:
    pricing:
      my-custom-model:
        input_per_mtok: 3.00      # USD per million input tokens
        output_per_mtok: 15.00    # USD per million output tokens
        cache_read_per_mtok: 0.30 # optional
        cache_write_per_mtok: 3.75 # optional
```

### Periodic Checkpoints

By default Conductor writes a checkpoint **only when a workflow fails** with an
exception. A long run that *stalls* (a provider hang, an MCP deadlock, a network
blip, a sub-agent that never returns) produces no recoverable state, so
`conductor resume` has nothing to resume.

Enable **periodic checkpoints** to make stalled or hard-killed runs resumable:

```yaml
workflow:
  runtime:
    checkpoint:
      every_seconds: 300    # Save at most once every 5 minutes (throttle)
      keep_last: 5          # Retain this many periodic checkpoints per run (1-100)
      # every_agent: true   # Alternative: save after EVERY step boundary
```

- **`every_agent`** (default `false`) — save at every step boundary (after each
  agent, parallel group, for-each group, gate, script, set, wait, or sub-workflow
  step). When `true` it governs on its own and `every_seconds` is ignored.
- **`every_seconds`** (default `null`) — a throttle: save at the first step
  boundary reached after this many seconds have elapsed since the last
  checkpoint. The first periodic checkpoint of a run fires at the first
  boundary; the interval only throttles subsequent saves.
- Set either trigger (or both — a save fires when **either** is met).
- **`keep_last`** (default `5`) — older periodic checkpoints for the run are
  rotated away after each save; **failure checkpoints are never rotated**.

How it works:

- Checkpoints are evaluated at **step boundaries**, where all prior step outputs
  are already committed. The checkpoint points at the step that was *about to
  run*, so `conductor resume` continues forward and re-runs only that step.
- There is no background timer. If a single step runs longer than
  `every_seconds`, the recovery point is the boundary checkpoint taken **before**
  that step started — which is exactly what you resume from after killing a
  stalled run.
- Periodic checkpoints are written by the **root** workflow only (sub-workflow
  state is re-run from scratch on resume) and are **deleted automatically when
  the run reaches a terminal, non-resumable outcome** (clean completion or an
  explicit `status: failed` terminate). On an unexpected failure they are kept
  alongside the on-failure checkpoint.
- If a periodic save itself fails (e.g. the disk fills), the run is not
  interrupted; the failure is surfaced via a `checkpoint_save_failed` event and
  a console warning so you know recovery may be unavailable.

Recover a stalled run by killing the process (e.g. `conductor stop` for a
`--web-bg` run) and then:

```bash
conductor checkpoint list workflow.yaml   # list checkpoints (Trigger column shows periodic/failure)
conductor resume workflow.yaml          # resume from the latest checkpoint
```

See `examples/periodic-checkpoints.yaml` for a complete example.

### Tool Output Limits

To prevent large tool results from overloading the context window, Conductor supports limiting the character size of individual MCP tool responses:

```yaml
workflow:
  runtime:
    tool_output:
      enabled: true          # Default: true. Set false to disable output limiting.
      max_chars: 50000       # Default: 50000. Retained character count (minimum: 1000).
      spill_to_file: true    # Default: true. Write full raw output to a temp file.
      spill_dir: null        # Default: null. Custom spill directory (defaults to OS temp dir).
```

* **Per-Result Cap:** The `max_chars` limit is a **per-result** cap applied to each tool result independently, not a cumulative context window budget. Multiple truncated tool results, combined with prompt and conversation history, can still exceed the model's context window. Users should tune this via `max_chars` or `max_agent_iterations` if needed.
* **Spill files:** Spill files are written to the directory specified by `spill_dir` (resolving to `<tempfile.gettempdir()>/conductor/tool-output` if `null`). These files contain raw tool output (which may include secrets) and are not deleted by Conductor.
* **Provider Support:** The Copilot provider maps this limit directly to bytes in the native SDK's `large_output` configuration. For Claude, the provider handles truncation conductor-side. This option is ignored by `claude-agent-sdk` (managed via native CLI `MAX_MCP_OUTPUT_TOKENS`) and is not applicable to `hermes` (no MCP tools).

See `examples/tool-output-limits.yaml` for a complete example.

## Tools

Tools can be configured at workflow or agent level.

### Workflow-level Tools

Available to all agents:

```yaml
tools:
  - web_search
  - calculator
```

### Agent-level Tools

Override or extend workflow tools:

```yaml
agents:
  - name: researcher
    tools:
      - web_search
      - arxiv_search
```

**Note**: Tool implementation depends on your provider. See provider documentation for available tools.

### MCP Servers

Tools are typically provided by [MCP servers](mcp-tools.md) configured in the `workflow.runtime.mcp_servers` section. MCP tools are automatically made available to agents and can be filtered using the `tools` field above.

```yaml
workflow:
  runtime:
    mcp_servers:
      web-search:
        command: npx
        args: ["-y", "open-websearch@latest"]
        tools: ["*"]

agents:
  - name: researcher
    tools:
      - web-search__search    # Use specific MCP tool (server__tool format)
    prompt: "Research the topic"
```

For full MCP configuration details, see the [MCP Tools guide](mcp-tools.md).

## External File References

The `!file` YAML tag lets you reference external files from any YAML field value. The file content is transparently inlined during loading, keeping workflow files concise and enabling reuse of prompts, schemas, and configuration across workflows.

### Syntax

Use the `!file` tag followed by a file path:

```yaml
field_name: !file path/to/file
```

The tag can be used on any scalar YAML value — string fields, output schemas, tool lists, or any other field.

### Content-Type Detection

The content of the referenced file is handled based on its structure:

- **YAML dict or list** — If the file content parses as a YAML mapping or sequence, it is returned as structured data (dict or list). This is useful for output schemas, tool lists, or any structured configuration.
- **Scalar or non-YAML** — If the file contains a YAML scalar (e.g., a plain string), is not valid YAML, or is a non-YAML format like Markdown, the raw file content is returned as a string.

### Path Resolution

File paths are resolved **relative to the directory containing the YAML file** that uses the `!file` tag, not relative to the current working directory.

```
project/
├── workflows/
│   └── review.yaml        # prompt: !file ../prompts/review.md
├── prompts/
│   └── review.md           # ← resolved relative to workflows/
└── schemas/
    └── output.yaml
```

When using `load_string()` programmatically:
- If `source_path` is provided, paths resolve relative to `source_path.parent`
- If `source_path` is not provided, paths resolve relative to the current working directory

### Usage Examples

#### Prompt from a Markdown File

Keep long prompts in separate Markdown files for easier editing:

```yaml
# workflow.yaml
agents:
  - name: reviewer
    model: gpt-4
    prompt: !file prompts/review-prompt.md
    routes:
      - to: $end
```

```markdown
# prompts/review-prompt.md
You are a code review expert.

Please analyze the following code and provide:
- A summary of what the code does
- Any bugs or issues found
- Suggestions for improvement
```

#### Structured Output Schema from YAML

Extract output schemas into reusable files:

```yaml
# workflow.yaml
agents:
  - name: analyzer
    model: gpt-4
    prompt: "Analyze the input data"
    output: !file schemas/analysis-output.yaml
    routes:
      - to: $end
```

```yaml
# schemas/analysis-output.yaml
summary:
  type: string
  description: A brief summary of the analysis
score:
  type: number
  description: A confidence score from 1 to 10
```

#### Tool List from External File

Share tool configurations across agents:

```yaml
# workflow.yaml
agents:
  - name: researcher
    model: gpt-4
    prompt: "Research the topic"
    tools: !file tools/research-tools.yaml
    routes:
      - to: $end
```

```yaml
# tools/research-tools.yaml
- web_search
- arxiv_search
- calculator
```

#### Nested Inclusion

Included YAML files can themselves contain `!file` tags. Each nested reference resolves relative to its own file's directory:

```yaml
# workflow.yaml
agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    output: !file schemas/nested.yaml
    routes:
      - to: $end
```

```yaml
# schemas/nested.yaml
summary:
  type: string
  description: !file ../descriptions/summary-desc.md
```

```markdown
# descriptions/summary-desc.md
A comprehensive summary of the analysis results.
```

### Jinja Includes in Prompt Files

When a prompt or system_prompt is loaded via `!file`, the directory of that file becomes the search root for Jinja template loading. This allows statements like `{% include "_shared.md" %}`, `{% import "_macros.md" as m %}`, and `{% extends "_base.md" %}` to resolve relative to the prompt file's directory rather than the workflow's directory or the current working directory.

Only `prompt: !file` and `system_prompt: !file` support this behavior. Other fields that use `!file` (such as command, stdin, value, schemas, or tool lists) don't have include loader support. Inline prompts defined as plain strings don't support loader-dependent Jinja tags. If you attempt to use them inline, the system raises a template rendering error suggesting you switch to a file-backed prompt:

```
Template rendering failed: loader-dependent Jinja constructs ({% include %}, {% import %}, {% extends %}) require a file-backed prompt via prompt: !file ...
```

If an include file is missing, the error identifies the missing template name and the searched directory:

```
Template not found: '<name>'. Searched in: <dir>
```

Note: Prompts for `human_gate` steps loaded via `!file` also support these include features because they use the same shared renderer.

#### Environment Variables in Partials

Included, imported, and base template files go through the same environment variable resolution (`${VAR}` / `${VAR:-default}`) as the root prompt file, applied when Jinja loads each partial at render time. An unset **required** variable (no default) inside a partial fails the run with the standard configuration error:

```
ConfigurationError: Required environment variable 'X' is not set
  💡 Suggestion: Set the environment variable 'X' or provide a default value using the syntax: ${X:-default_value}
```

#### Missing Prompt Source File

The prompt file must still exist when the workflow runs — relative includes resolve against its directory. If the file was deleted or became inaccessible after the workflow was loaded, rendering fails immediately with an explicit error instead of silently treating the prompt as inline:

```
TemplateError: File-backed prompt source is no longer available: '<path>' (loaded via !file).
  💡 Suggestion: Restore the prompt file or fix the !file reference — relative Jinja includes/imports/extends resolve against that file's directory.
```

#### Example

A workflow using a file-backed prompt:

```yaml
# workflow.yaml
workflow:
  name: review-workflow
  description: A workflow that uses Jinja includes in its prompt
  entry_point: reviewer

agents:
  - name: reviewer
    model: gpt-4
    prompt: !file prompts/review.md.jinja
    routes:
      - to: $end
```

The prompt file referencing a partial file:

```markdown
# prompts/review.md.jinja
You are a code review assistant.

{% include "_checklist.md.jinja" %}

Please review the provided code according to the checklist.
```

The partial file:

```markdown
# prompts/_checklist.md.jinja
Check for:
1. Proper error handling
2. Clear variable names
```

During validation, `conductor validate` doesn't scan inside included files to check template references. It only verifies that the direct `!file` targets exist.

### Environment Variables

Environment variable references (`${VAR}` or `${VAR:-default}`) inside included files are resolved after inclusion, during the standard environment variable resolution pass. This means you can use env vars in external files just as you would inline:

```markdown
# prompts/greeting.md
Hello ${USER_NAME:-User}, welcome to the system.
```

### Error Handling

#### Missing Files

If a referenced file does not exist, a `ConfigurationError` is raised with the file path and a suggestion:

```
ConfigurationError: File not found: 'prompts/missing.md' (resolved to '/absolute/path/prompts/missing.md')
  💡 Suggestion: Check the file path is correct relative to the workflow file directory.
```

#### Circular References

If `!file` tags form a cycle (e.g., file A includes file B which includes file A), a `ConfigurationError` is raised:

```
ConfigurationError: Circular file reference detected: 'a.yaml'
  File inclusion chain: /path/main.yaml → /path/a.yaml → /path/b.yaml → /path/a.yaml
  💡 Suggestion: Remove the circular !file reference.
```

#### Encoding Errors

Only UTF-8 text files are supported. Non-UTF-8 files produce a `ConfigurationError` with encoding guidance.

### Limitations

- **UTF-8 only** — Only UTF-8 encoded text files are supported
- **No glob patterns** — Wildcards like `!file prompts/*.md` are not supported
- **No URLs** — Remote references like `!file https://...` are not supported
- **No conditional includes** — File references cannot be parameterized or conditional
- **No caching** — Each `!file` reference reads the file independently
- **Jinja includes search root**: Relative template includes (`{% include %}`, etc.) resolve only against the prompt file's own directory, with no fallback to the workflow directory or current working directory.

## Hooks

Lifecycle hooks execute template expressions at key workflow events:

```yaml
workflow:
  hooks:
    on_start: "{{ 'Starting workflow: ' + workflow.name }}"
    on_complete: "{{ 'Workflow completed in ' + str(workflow.execution_time) + 's' }}"
    on_error: "{{ 'Workflow failed: ' + workflow.error.message }}"
```

### Available Hook Contexts

**`on_start`**:
- `workflow.name`, `workflow.description`, `workflow.dir`, `workflow.file`
- `workflow.input.*` (all input values)

**`on_complete`**:
- All agent outputs
- `workflow.execution_time` (total seconds)
- `workflow.iteration_count` (total iterations)

**`on_error`**:
- `workflow.error.message` (error message)
- `workflow.error.agent` (agent that failed)
- Partial agent outputs (agents that completed before failure)

## Complete Example

```yaml
workflow:
  name: code-review
  description: Multi-stage code review with parallel validation
  entry_point: analyzer
  
  limits:
    max_iterations: 20
    timeout_seconds: 600
  
  context_mode: accumulate

input:
  code:
    type: string
    required: true
  language:
    type: string
    required: true

tools:
  - static_analyzer

agents:
  - name: analyzer
    model: claude-sonnet-4.5
    prompt: |
      Analyze this {{ workflow.input.language }} code for issues:
      {{ workflow.input.code }}
    output:
      issues:
        type: array
    routes:
      - to: parallel_validators

parallel:
  - name: parallel_validators
    agents:
      - security_check
      - performance_check
      - style_check
    failure_mode: continue_on_error
    routes:
      - to: summarizer

agents:
  - name: security_check
    prompt: "Check for security vulnerabilities: {{ analyzer.output.issues }}"
    output:
      security_issues:
        type: array
  
  - name: performance_check
    prompt: "Check for performance issues: {{ analyzer.output.issues }}"
    output:
      performance_issues:
        type: array
  
  - name: style_check
    prompt: "Check for style violations: {{ analyzer.output.issues }}"
    output:
      style_issues:
        type: array
  
  - name: summarizer
    prompt: |
      Summarize findings:
      Security: {{ parallel_validators.outputs.security_check.security_issues }}
      Performance: {{ parallel_validators.outputs.performance_check.performance_issues }}
      Style: {{ parallel_validators.outputs.style_check.style_issues }}
    output:
      summary:
        type: string
    routes:
      - to: $end

output:
  summary: "{{ summarizer.output.summary }}"
  all_issues: "{{ analyzer.output.issues }}"
```

## See Also

- [Parallel Execution Guide](./parallel-execution.md) - Detailed parallel execution patterns
- [ACA Provider](./providers/aca.md) - Experimental Azure Container Apps sandbox provider (`sandbox:` block, `runtime.provider: {name: aca}`)
- [Examples](../examples/) - Complete workflow examples
- [README](../README.md) - Getting started and CLI reference
