# Conductor Schema Reference

Complete reference for all YAML configuration options. Derived from the Pydantic models in `src/conductor/config/schema.py`.

## Top-Level Structure

```yaml
workflow: WorkflowDef              # Required: workflow configuration
tools: [string]                    # Optional: workflow-level tool names
agents: [AgentDef]                 # Required: agent definitions
parallel: [ParallelGroup]         # Optional: static parallel groups
for_each: [ForEachDef]            # Optional: dynamic parallel groups
output: {field: template}         # Optional: final output templates
```

## Workflow Schema

```yaml
workflow:
  # Required fields
  name: string                      # Unique workflow identifier
  entry_point: string               # Name of first agent, parallel group, or for-each group

  # Optional fields
  description: string               # Human-readable description
  version: string                   # Semantic version (e.g., "1.0.0")

  # Runtime configuration
  runtime:
    provider: string | object       # "copilot" (default), "claude", or "openai-agents"
                                    # — or a ProviderSettings object (see below)
    default_model: string           # Default model for all agents
    temperature: float              # 0.0-1.0, controls randomness (optional)
    max_tokens: integer             # Max OUTPUT tokens per response, 1-200000 (optional)
    timeout: float                  # Per-request timeout in seconds (optional, default: 600)
    max_agent_iterations: integer   # Max tool-use roundtrips per agent (1-500, optional)
    max_session_seconds: float      # Wall-clock timeout per agent session in seconds (optional)
    default_reasoning_effort: string # Workflow-wide reasoning/thinking effort: low, medium, high, xhigh (optional)
    mcp_servers:                    # MCP server configurations
      <server_name>:
        type: string                # "stdio" (default), "http", or "sse"
        command: string             # Command to run (required for stdio)
        args: [string]              # Command arguments (stdio only)
        url: string                 # Server URL (required for http/sse)
        headers: {string: string}   # HTTP headers (http/sse only)
        timeout: integer            # Timeout in milliseconds (optional)
        tools: [string]             # Tool whitelist, ["*"] for all (default: ["*"])
        env: {string: string}       # Environment variables (stdio only)

  # Input parameters
  input:
    <param_name>:
      type: string                  # "string", "number", "boolean", "array", "object"
      required: boolean             # Default: true
      default: any                  # Default value (must match declared type)
      description: string           # Parameter description

  # Context management
  context:
    mode: string                    # "accumulate" (default), "last_only", "explicit"
    max_tokens: integer             # Maximum context tokens (optional)
    trim_strategy: string           # "truncate", "drop_oldest", "summarize"

  # Safety limits
  limits:
    max_iterations: integer         # Max agent executions (default: 10, range: 1-500)
    timeout_seconds: integer        # Total workflow timeout in seconds (optional, no default)

  # Cost tracking
  cost:
    show_per_agent: boolean         # Show cost per agent in verbose output (default: true)
    show_summary: boolean           # Show cost summary at end (default: true)
    pricing:                        # Custom pricing overrides
      <model_name>:
        input_per_mtok: float       # Cost per million input tokens (USD)
        output_per_mtok: float      # Cost per million output tokens (USD)
        cache_read_per_mtok: float  # Cost per million cache read tokens (default: 0.0)
        cache_write_per_mtok: float # Cost per million cache write tokens (default: 0.0)

  # Lifecycle hooks
  hooks:
    on_start: string                # Template executed at workflow start
    on_complete: string             # Template executed on success
    on_error: string                # Template executed on failure

  # Arbitrary metadata for downstream tooling (dashboards, work-item trackers)
  # Surfaced verbatim in the workflow_started event.
  metadata: {string: any}           # Optional. Merged with --metadata / -m CLI flags (CLI wins).

  # Workspace context prepended to every agent prompt
  # Each entry is either a !file include or an inline string.
  # For workflows distributed via registry, prefer the --workspace-instructions
  # CLI flag for runtime auto-discovery.
  instructions:
    - !file ../AGENTS.md
    - "Always respond in English."
```

## Agent Schema

```yaml
agents:
  - # Required fields
    name: string                    # Unique agent identifier

    # Optional fields
    type: string                    # "agent" (default), "human_gate", "script", "workflow", "wait", or "terminate"
    description: string             # What this agent does
    model: string                   # Override default_model
    provider: string                # Per-agent provider override ("copilot" or "claude")

    # Input specification (for explicit context mode)
    input:
      - string                      # Reference paths, e.g., "workflow.input.question"
                                    # Use "?" suffix for optional: "other_agent.output?"

    # Prompt templates
    system_prompt: string           # System message (always included, optional)
    prompt: string                  # Jinja2 template for agent instructions

    # Output schema
    output:
      <field_name>:
        type: string                # "string", "number", "boolean", "array", "object"
        description: string         # Field description
        items:                      # For array types: schema of items
          type: string
        properties:                 # For object types: schema of properties
          <prop_name>:
            type: string
            description: string

    # Routing rules (evaluated in order, first match wins)
    routes:
      - to: string                  # Target: agent name, parallel group, for-each group, "$end", or "self"
        when: string                # Optional Jinja2 condition
        output: {string: string}    # Optional output transformation templates

    # Agent-level tools
    tools:                          # null = all workflow tools, [] = none, [list] = subset
      - string

    # Agent-level limits (override workflow runtime defaults)
    max_agent_iterations: integer   # Max tool-use roundtrips for this agent (1-500, optional)
    max_session_seconds: float      # Soft wall-clock timeout per session (checked between iterations)
    timeout_seconds: float          # Hard wall-clock timeout (>=1.0); engine wraps in asyncio.wait_for().
                                    # Effective limit = min(timeout_seconds, remaining_workflow_timeout).
                                    # Raises AgentTimeoutError; non-retryable.
                                    # Forbidden on script (use 'timeout' instead), human_gate, workflow, wait.

    # Per-agent reasoning effort (overrides runtime.default_reasoning_effort)
    # Not allowed for script, human_gate, workflow, or wait agent types.
    reasoning:
      effort: string                # low, medium, high, or xhigh

    # Per-agent retry policy (optional, not allowed for script, human_gate, workflow, or wait agents)
    retry:
      max_attempts: integer         # Max attempts including first (1-10, default: 1 = no retry)
      backoff: string               # "exponential" (default) or "fixed"
      delay_seconds: float          # Base delay in seconds (0-300, default: 2.0)
      retry_on:                     # Error categories to retry (default: ["provider_error", "timeout"])
        - string                    # "provider_error" (API 500s, rate limits) or "timeout"

    # Conditional dialog mode (optional, only on provider-backed agents)
    dialog:
      trigger_prompt: string        # Criteria evaluated against agent output by an LLM gate

    # Sub-workflow fields (type: workflow)
    workflow: string                # Path to sub-workflow YAML (relative to parent), required
    input_mapping:                  # Optional Jinja2 expressions per sub-workflow input parameter
      <param_name>: string          # e.g. "{{ task_manager.output.current_issue_id }}"
    max_depth: integer              # Optional per-agent recursion cap (1-10).
                                    # Bounded additionally by global MAX_SUBWORKFLOW_DEPTH = 10.

    # Script-only fields (type: script)
    command: string                 # Command to execute (Jinja2 templated)
    args: [string]                  # Command arguments (each Jinja2 templated)
    env: {string: string}           # Extra environment variables
    working_dir: string             # Working directory (Jinja2 templated)
    timeout: integer                # Per-script timeout in seconds

    # Set-only fields (type: set) — exactly one of value: / values: required
    value: string                   # Single Jinja2 expression (typed result)
    values:                         # Multi-binding: each key gets its own typed result
      <key>: string
    output_type: string             # auto|string|number|integer|boolean|list|dict
                                    # (single value: only; per-key typing on values: not supported)

    # Terminate-only fields (type: terminate)
    status: string                  # Required: "success" or "failed"
    reason: string                  # Required: Jinja2-templated termination reason
    output_template:                # Optional: replaces workflow-level output: for this path
      <key>: string                 # Each value Jinja2-templated, then JSON-coerced
                                    # ("true" -> True, "42" -> 42, JSON literals parsed)
```

**Script agent restrictions:** Cannot have `prompt`, `provider`, `model`, `tools`, `output`, `system_prompt`, `options`, `retry`, `reasoning`, `dialog`, `max_session_seconds`, `max_agent_iterations`, `timeout_seconds` (use `timeout`), `input_mapping`, or `max_depth`. Output is always `{stdout, stderr, exit_code}`. If `stdout` is valid JSON, its top-level keys are auto-merged into the output dict.

**Set agent restrictions:** Cannot have `prompt`, `provider`, `model`, `tools`, `system_prompt`, `options`, `command`, `args`, `env`, `working_dir`, `timeout`, `workflow`, `input_mapping`, `max_depth`, `retry`, `dialog`, `reasoning`, `timeout_seconds`, `max_session_seconds`, or `max_agent_iterations`. Requires exactly one of `value:` or `values:`. `output_type:` is forbidden with `values:` (per-key typing not yet supported). `output:` schema validation is permitted only when the rendered output is a dict (always for `values:`, sometimes for `value:`); a scalar with a declared schema raises `ValidationError`. Set agents are allowed inside `parallel` groups and as `for_each` inline agents, and count toward `limits.max_iterations` like any other step.

**Workflow agent restrictions (`type: workflow`):** Cannot have `prompt`, `model`, `provider`, `tools`, `system_prompt`, `command`, `options`, `retry`, `reasoning`, `dialog`, `max_session_seconds`, `max_agent_iterations`, or `timeout_seconds`. Requires `workflow:` path. Supports `input_mapping` and `max_depth`. Allowed inside `for_each` groups for dynamic fan-out.

**Terminate agent restrictions (`type: terminate`):** Requires `status` (`success` | `failed`) and a non-empty `reason`. Cannot have `routes`, `tools`, `output`, `prompt`, `model`, `provider`, `system_prompt`, `command`, `args`, `env`, `working_dir`, `timeout`, `timeout_seconds`, `max_session_seconds`, `max_agent_iterations`, `max_depth`, `retry`, `dialog`, `reasoning`, `workflow`, `input_mapping`, or `options`. Cannot be used as a parallel-group member or as a `for_each` inline agent — route to a terminate step from those groups' `routes:` instead. Reaching a terminate step ends the workflow immediately (no routes evaluated after) and produces a distinguishable event payload: `workflow_completed` (for `success`) or `workflow_failed` (for `failed`) with `termination_reason`, `terminated_by`, `is_explicit: true`, and `status`. `status: failed` raises `WorkflowTerminated` (an `ExecutionError` subclass), gives the CLI a non-zero exit code, and is intentionally NOT resumable (no on-failure checkpoint saved). Inside a sub-workflow, a `status: failed` terminate is downgraded at the parent boundary to `SubworkflowTerminatedError` (also an `ExecutionError`), preserving the child's rendered `terminated_output`/`terminated_reason`/`terminated_by` as attributes on the wrapper.

**Reasoning effort:** `reasoning.effort` (and `runtime.default_reasoning_effort`) accepts `low`, `medium`, `high`, or `xhigh`. Per-agent value overrides the runtime default. Each provider translates the unified value to its native API:

- **Copilot**: forwards `reasoning_effort` to the session. Validated against the model's advertised `supported_reasoning_efforts` (when available); raises `ValidationError` for unsupported combinations.
- **Claude**: enables extended thinking via `thinking={"type":"enabled","budget_tokens":N}` with mapping low=2048, medium=8192, high=16384, xhigh=32768. Auto-coerces `temperature=1.0` (Anthropic API requirement) and bumps `max_tokens` to fit `budget+4096` (capped at 64000). Only valid on thinking-capable models (Claude 3.7+, Opus/Sonnet/Haiku 4.x); raises `ValidationError` otherwise.

Both providers continue to surface reasoning content via `agent_reasoning` events visible in the dashboard, JSONL logs, and console at `-vv`.

Forbidden on agent types: `script`, `human_gate`, `workflow`, `wait`.

## Script Agent Schema

Script agents run shell commands instead of LLM prompts:

```yaml
agents:
  - name: string
    type: script                    # Required
    description: string             # Optional
    command: string                 # Required: command to run (Jinja2 templated)
    args: [string]                  # Optional: arguments (each Jinja2 templated)
    env: {string: string}           # Optional: extra environment variables
    working_dir: string             # Optional: working directory (Jinja2 templated)
    timeout: integer                # Optional: timeout in seconds
    input: [string]                 # Optional: context dependencies
    routes:                         # Required: routing rules
      - to: string
        when: string                # Can use exit_code (simpleeval syntax)
```

### Script Output

Script agents always produce:

```jinja2
{{ script_name.output.stdout }}     # Captured standard output
{{ script_name.output.stderr }}     # Captured standard error
{{ script_name.output.exit_code }}  # Process exit code (0 = success)
```

## Wait Agent Schema

Wait agents pause workflow execution for a parsed duration via in-process `asyncio.sleep`. Cross-platform — no shell `sleep` dependency. Use for rate-limit cooldowns, polling intervals, and external-system catch-up.

```yaml
agents:
  - name: string
    type: wait                      # Required
    description: string             # Optional
    duration: string | number       # Required: see "Duration format" below
    reason: string                  # Optional: human-readable reason (shown in dashboard)
    input: [string]                 # Optional: context dependencies
    routes:                         # Required: routing rules
      - to: string
        when: string                # May reference waited_seconds
```

### Duration Format

`duration` accepts:

- A plain `int` or `float` (interpreted as seconds): `duration: 60`, `duration: 1.5`
- A string with a unit suffix — `ms`, `s`, `m`, `h`: `"500ms"`, `"60s"`, `"2.5m"`, `"1h"`
- A Jinja2 template rendering to one of the above: `"{{ workflow.input.interval }}s"`
  (templates defer literal validation to runtime)

The resolved duration must be **> 0 and ≤ 24h** (`86400s`). Booleans are rejected.

### Wait Output

Wait agents produce a single, strict field:

```jinja2
{{ wait_name.output.waited_seconds }}  # Actual seconds slept (may be < requested on interrupt)
```

### Wait Restrictions

Forbidden fields: `prompt`, `model`, `provider`, `tools`, `system_prompt`, `options`, `command`, `args`, `env`, `working_dir`, `timeout`, `workflow`, `input_mapping`, `max_depth`, `max_session_seconds`, `max_agent_iterations`, `retry`, `dialog`, `reasoning`, `timeout_seconds`, `output`. Wait steps cannot be used inside `parallel` or `for_each` groups.

`Esc` / `Ctrl+G` cancels in-progress waits. Workflow-level `limits.timeout_seconds` also cancels them.
## Set Agent Schema

Set agents evaluate Jinja2 expressions and bind typed values into the workflow context — no LLM call, no subprocess:

```yaml
agents:
  # Single binding form — output is the typed scalar / list / dict.
  - name: string
    type: set                       # Required
    description: string             # Optional
    value: string                   # Required (when not using values:): Jinja2 expression
    output_type: string             # Optional: auto|string|number|integer|boolean|list|dict
    input: [string]                 # Optional: context dependencies
    routes:                         # Optional: routing rules
      - to: string

  # Multi-binding form — output is a dict, each key gets its own typed result.
  - name: string
    type: set
    values:                         # Required (when not using value:): named bindings
      <key>: string                 # Jinja2 expression per key
    input: [string]
    routes:
      - to: string
        when: string                # e.g. "{{ output.<key> }}" (Jinja2) or "<key>" (simpleeval)
```

Exactly one of `value:` or `values:` must be present. `output_type:` only applies to single `value:`.

### Set Output

```jinja2
# Single value:
{{ step.output }}                  # The typed scalar / list / dict directly

# Multi values:
{{ step.output.is_breaking }}      # Each declared key
{{ step.output.target_branch }}
```

### Type Detection (auto)

Default (`output_type` unset or `auto`) uses safe YAML loading (equivalent to `yaml.safe_load`):

- Booleans, numbers, lists, dicts → native Python types
- Parse failures and pure-comment renders → raw string
- Empty / whitespace-only renders → `""` (not `None`)
- `datetime` / `date` / `time` (e.g. from `"2024-01-02"`) → ISO 8601 string (keeps checkpoints JSON-safe)
- Any other non-JSON-safe Python value → `ExecutionError`

### Routing on Set Output

Routes attached to a set step evaluate against the bound value directly:

```yaml
# Dict-shaped output → access fields via Jinja2 or simpleeval flattening.
routes:
  - to: hot_path
    when: "{{ output.is_breaking }}"     # Jinja2
  - to: hot_path
    when: "is_breaking"                  # simpleeval (flattened)

# Scalar output → use {{ output }}.
routes:
  - to: hot_path
    when: "{{ output }}"
```

### Set Step Composition

- Allowed inside `parallel` groups (each member publishes to context). Templates cannot reference sibling group members — the validator catches this at config time.
- Allowed as the inline agent of a `for_each` group (one bound value per item).
- Each invocation emits `set_started` / `set_completed` / `set_failed` events with `output_type`, `output_keys`, and a 512-char-truncated `value_repr`.

## File Includes (`!file` Tag)

Include external file content anywhere in YAML:

```yaml
agents:
  - name: analyzer
    system_prompt: !file prompts/system.md    # Included as string
    prompt: !file prompts/analyze.md          # Included as string
    output: !file schemas/analyzer-output.yaml # Included as YAML structure
```

- Paths resolve **relative to the YAML file's directory**
- Plain text files (Markdown, etc.) are included as strings
- YAML files are parsed and included as data structures
- Supports **recursive includes** (included YAML files can use `!file`)
- Circular references are detected and raise `ConfigurationError`

## Human Gate Schema

Human gates use a **list-based** options format:

```yaml
agents:
  - name: string
    type: human_gate
    prompt: string                  # Jinja2 template shown to user

    options:                        # List of choices (required for human_gate)
      - label: string               # Display text for the option
        value: string               # Value stored when selected
        route: string               # Agent to route to when selected
        prompt_for: string          # Optional: field name to collect text input from user

    output:                         # Captured automatically (do not declare in YAML)
      selected:                     # The chosen option's `value`
        type: string
      additional_input:             # Dict of values collected from `prompt_for` fields.
        type: dict                  # Always present; `{}` when no `prompt_for` is set
                                    # or when the selected option has no `prompt_for`.
                                    # Access fields via templates as:
                                    #   {{ <gate>.output.additional_input.<field> }}
                                    # In `context: explicit` mode, `input:` declarations
                                    # support `<gate>.output.additional_input` (the whole
                                    # dict) but not the dotted shorthand
                                    # `<gate>.output.additional_input.<field>` — declare
                                    # the parent and traverse in Jinja2.
```

## Parallel Group Schema

Static parallel groups execute a fixed list of agents concurrently:

```yaml
parallel:
  - name: string                    # Unique group identifier
    description: string             # Optional description
    agents:                         # At least 2 agent names required
      - string
    failure_mode: string            # "fail_fast" (default), "continue_on_error", "all_or_nothing"
    routes:                         # Routes after group completes
      - to: string
        when: string
```

### Accessing Parallel Outputs

```jinja2
{{ group_name.outputs.agent_name.field }}      # Successful agent output
{{ group_name.errors.agent_name.message }}      # Error details (continue_on_error)
```

## For-Each (Dynamic Parallel) Schema

For-each groups spawn N agent instances at runtime from an array:

```yaml
for_each:
  - name: string                    # Unique group identifier
    type: for_each                  # Required discriminator
    description: string             # Optional description
    source: string                  # Array reference (e.g., "finder.output.items")
                                    # Must be dotted path with at least 3 parts
    as: string                      # Loop variable name (must be valid identifier)
                                    # Reserved: workflow, context, output, _index, _key
    max_concurrent: integer         # Concurrent limit per batch (default: 10, range: 1-100)
    failure_mode: string            # "fail_fast" (default), "continue_on_error", "all_or_nothing"
    key_by: string                  # Optional: path to extract key for dict-based outputs

    agent:                          # Inline agent definition (template for each item)
      name: string
      model: string
      prompt: string                # Has access to {{ <as_var> }}, {{ _index }}, {{ _key }}
      output:
        <field>: {type: string}

    routes:
      - to: string
        when: string
```

### Loop Variables

| Variable | Description |
|----------|-------------|
| `{{ <as_var> }}` | Current item from the source array |
| `{{ _index }}` | Zero-based index of current item |
| `{{ _key }}` | Extracted key value (only when `key_by` is set) |

### Accessing For-Each Outputs

```jinja2
# Without key_by (array access)
{{ group_name.outputs[0].field }}
{{ group_name.outputs | length }}
{% for result in group_name.outputs %}...{% endfor %}

# With key_by (dict access)
{{ group_name.outputs["key_value"].field }}

# Errors
{{ group_name.errors }}                        # Dict of failed items
{{ group_name.errors | length }}
```

## Output Schema

```yaml
output:
  <field_name>: string              # Jinja2 template referencing agent outputs
                                    # e.g., "{{ agent_name.output.field }}"
```

## Type System

### Supported Types

| Type | Description | Example Values |
|------|-------------|----------------|
| `string` | Text | `"hello"`, `"multi\nline"` |
| `number` | Integer or float | `42`, `3.14` |
| `boolean` | True/false | `true`, `false` |
| `array` | List of items | `["a", "b", "c"]` |
| `object` | Key-value pairs | `{"key": "value"}` |

### Array Type Definition

```yaml
output:
  items:
    type: array
    description: List of items
    items:
      type: string
```

### Object Type Definition

```yaml
output:
  result:
    type: object
    description: Structured result
    properties:
      name:
        type: string
        description: Item name
      count:
        type: number
        description: Item count
```

## Template Syntax

### Variable Access

```jinja2
{{ workflow.input.param_name }}    # Workflow input
{{ workflow.name }}                 # Workflow name
{{ workflow.description }}          # Workflow description
{{ workflow.dir }}                  # Directory containing the workflow YAML (all context modes)
{{ workflow.file }}                 # Absolute path to the workflow YAML
{{ agent_name.output.field }}       # Agent output field
{{ output.field }}                  # Current agent output (in routes)
```

### Conditionals

```jinja2
{% if condition %}
  content
{% elif other_condition %}
  other content
{% else %}
  fallback content
{% endif %}
```

### Checking for Defined Variables

```jinja2
{% if agent_name is defined and agent_name.output %}
  {{ agent_name.output.field }}
{% endif %}
```

### Loops

```jinja2
{% for item in agent_name.output.items %}
  - {{ item }}
{% endfor %}
```

### Filters

```jinja2
{{ value | upper }}                 # Uppercase
{{ value | lower }}                 # Lowercase
{{ value | default("fallback") }}   # Default value
{{ value | length }}                # Length
{{ value | join(", ") }}            # Join array
{{ value | json }}                  # JSON serialize
```

## Route Conditions

### Comparison Operators

```yaml
when: "{{ output.score >= 90 }}"      # Greater than or equal
when: "{{ output.score < 50 }}"       # Less than
when: "{{ output.status == 'done' }}" # Equality
when: "{{ output.status != 'error' }}"# Inequality
```

### Logical Operators

```yaml
when: "{{ output.score >= 90 and output.approved }}"
when: "{{ output.retry or output.force }}"
when: "{{ not output.failed }}"
```

### String Operations

```yaml
when: "{{ 'error' in output.message }}"
when: "{{ output.status.startswith('success') }}"
```

### simpleeval Syntax (legacy)

```yaml
when: "status == 'success'"          # Without Jinja2 braces
when: "score > 5 and valid"
```

## MCP Server Examples

### Stdio Server

```yaml
runtime:
  mcp_servers:
    web-search:
      command: sh
      args: ["-c", "MODE=stdio DEFAULT_SEARCH_ENGINE=bing exec npx -y open-websearch@latest"]
      tools: ["*"]
```

### HTTP Server

```yaml
runtime:
  mcp_servers:
    remote-api:
      type: http
      url: https://mcp.server.example.com/
      headers:
        Authorization: "Bearer ${API_TOKEN}"
      tools: ["*"]
```

### SSE Server

```yaml
runtime:
  mcp_servers:
    streaming:
      type: sse
      url: https://sse.server.example.com/
      tools: ["*"]
```

### With Environment Variables

```yaml
runtime:
  mcp_servers:
    custom:
      command: node
      args: ["./server.js"]
      env:
        API_KEY: "${API_KEY}"
        DEBUG: "true"
      tools: ["*"]
```

### Selective Tool Access

```yaml
runtime:
  mcp_servers:
    web-search:
      command: npx
      args: ["-y", "open-websearch@latest"]
      tools: ["search", "fetch"]   # Only these tools (not ["*"])
```

## Custom Provider Routing (Ollama / vLLM / Azure OpenAI)

`runtime.provider` accepts either the bare string shorthand
(`provider: copilot`) or a structured `ProviderSettings` object that
routes the Copilot SDK at OpenAI-compatible / Azure / Anthropic
endpoints (Ollama, vLLM, LM Studio, Azure OpenAI, etc.).

### Schema

```yaml
runtime:
  provider:
    name: string                  # "copilot" (default), "claude", "openai-agents"
    type: string                  # "openai" | "azure" | "anthropic" (Copilot-only)
    wire_api: string              # "completions" | "responses" (Copilot-only)
    base_url: string              # Endpoint base URL
    api_key: string               # SecretStr; redacted in dumps. Prefer ${OPENAI_API_KEY}.
    bearer_token: string          # SecretStr; takes precedence over api_key.
    headers: {string: string}     # Extra HTTP headers (Copilot-only)
    azure:                        # Azure-specific options (requires type: azure)
      api_version: string         # e.g. "2024-10-21"
```

### Local OpenAI-compatible endpoint (Ollama)

```yaml
runtime:
  provider:
    name: copilot
    type: openai
    wire_api: completions
    base_url: http://localhost:11434/v1
    api_key: ${OPENAI_API_KEY:-ollama}
  default_model: llama3.1
```

### Azure OpenAI

```yaml
runtime:
  provider:
    name: copilot
    type: azure
    base_url: https://<resource>.openai.azure.com
    api_key: ${AZURE_OPENAI_API_KEY}
    azure:
      api_version: "2024-10-21"
  default_model: gpt-4o
```

### Activation and env-var fallbacks

Custom routing activates **only** when YAML sets at least one
non-`name` field. Ambient env vars alone never divert default
routing. Once activated, missing fields fall back from env:

| Field | Env-var chain |
|---|---|
| `base_url` | `COPILOT_PROVIDER_BASE_URL` → `OPENAI_BASE_URL` |
| `api_key` | `COPILOT_PROVIDER_API_KEY` *(only)* |
| `bearer_token` | `COPILOT_PROVIDER_BEARER_TOKEN` *(only)* |

Ambient `OPENAI_API_KEY` is **not** an implicit fallback (would leak
OpenAI credentials to arbitrary `base_url`); use the
`${OPENAI_API_KEY}` YAML interpolation for explicit opt-in.

### Validator rules

The schema rejects these misconfigurations at config-load time:

- `name != "copilot"` with any non-`name` field set
- `type: azure` without `azure: { api_version: ... }` (or vice versa)
- Anchorless fields: `wire_api`, `type`, `headers`, `azure` alone
  without `base_url` / `api_key` / `bearer_token`
- Empty containers / `SecretStr`: `headers: {}`, `api_key: ""`,
  `bearer_token: ""`, `azure: { api_version: null }`

When custom routing activates but every resolved field ends up empty,
the Copilot provider raises `ProviderError` rather than silently
falling back to default routing.

## Validation Rules

### Workflow Validation

- `name` must be present and non-empty
- `entry_point` must reference a valid agent, parallel group, or for-each group
- All referenced agents/groups must be defined
- Input parameter names must be valid identifiers
- Unknown fields on `WorkflowConfig`, `AgentDef`, `ParallelGroup`, and `ForEachDef` are **rejected** (not silently dropped)

### Agent Validation

- `name` must be unique within workflow
- `routes` required for type `agent` (not for `human_gate`)
- All route targets must be valid agent names, group names, `$end`, or `self`
- `when` conditions must be valid Jinja2 expressions
- `human_gate` agents require `options` and `prompt`
- `wait` agents require `duration`; literal values must be `> 0` and `≤ 86400s` (24h)

### Parallel Group Validation

- Must contain at least 2 agents
- All referenced agents must exist
- Route targets must be valid
- `script`, `workflow`, and `wait` steps cannot be used inside parallel groups

### For-Each Validation

- `source` must be dotted path with at least 3 parts (e.g., `agent.output.field`)
- `as` must be a valid Python identifier, not a reserved name
- `max_concurrent` must be 1-100
- Nested for-each groups are not allowed
- `script` and `wait` steps cannot be used as inline agents in for-each groups

### Routing Validation

- At least one route must be reachable (not all conditional)
- Circular routes are allowed but require `max_iterations`
- All agents must be reachable from `entry_point`
- All paths must eventually reach `$end`

## Error Messages

| Error | Cause | Solution |
|-------|-------|----------|
| `Missing entry_point` | No `entry_point` in workflow | Add `entry_point: agent_name` |
| `Unknown agent: X` | Route targets non-existent agent/group | Check names match |
| `Unreachable agent: X` | Agent not reachable from entry | Add route to agent or remove |
| `No terminal route` | No path reaches `$end` | Add `$end` route |
| `Invalid condition` | Malformed `when` clause | Check Jinja2 syntax |
| `Parallel needs 2+ agents` | Parallel group has < 2 agents | Add more agents |
| `Invalid source format` | For-each source path invalid | Use `agent.output.field` format |
| `Reserved loop variable` | `as` uses reserved name | Choose different variable name |
