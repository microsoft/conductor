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
    provider: string                # "copilot" (default) or "claude"
    default_model: string           # Default model for all agents
    temperature: float              # 0.0-1.0, controls randomness (optional)
    max_tokens: integer             # Max OUTPUT tokens per response, 1-200000 (optional)
    timeout: float                  # Per-request timeout in seconds (optional, default: 600)
    max_agent_iterations: integer   # Max tool-use roundtrips per agent (1-500, optional)
    max_session_seconds: float      # Wall-clock timeout per agent session in seconds (optional)
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
```

## Agent Schema

```yaml
agents:
  - # Required fields
    name: string                    # Unique agent identifier

    # Optional fields
    type: string                    # "agent" (default), "human_gate", or "script"
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
    max_session_seconds: float      # Wall-clock timeout for this agent session (optional)

    # Per-agent retry policy (optional, not allowed for script agents)
    retry:
      max_attempts: integer         # Max attempts including first (1-10, default: 1 = no retry)
      backoff: string               # "exponential" (default) or "fixed"
      delay_seconds: float          # Base delay in seconds (0-300, default: 2.0)
      retry_on:                     # Error categories to retry (default: all)
        - string                    # "provider_error" (API 500s, rate limits) or "timeout"

    # Script-only fields (type: script)
    command: string                 # Command to execute (Jinja2 templated)
    args: [string]                  # Command arguments (each Jinja2 templated)
    env: {string: string}           # Extra environment variables
    working_dir: string             # Working directory (Jinja2 templated)
    timeout: integer                # Per-script timeout in seconds
```

**Script agent restrictions:** Cannot have `prompt`, `provider`, `model`, `tools`, `output`, `system_prompt`, `options`, `retry`. Output is always `{stdout, stderr, exit_code}`.

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

    output:                         # Captured automatically
      selected:                     # The selected option value
        type: string
      feedback:                     # Text from prompt_for (if used)
        type: string
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

## Validation Rules

### Workflow Validation

- `name` must be present and non-empty
- `entry_point` must reference a valid agent, parallel group, or for-each group
- All referenced agents/groups must be defined
- Input parameter names must be valid identifiers

### Agent Validation

- `name` must be unique within workflow
- `routes` required for type `agent` (not for `human_gate`)
- All route targets must be valid agent names, group names, `$end`, or `self`
- `when` conditions must be valid Jinja2 expressions
- `human_gate` agents require `options` and `prompt`

### Parallel Group Validation

- Must contain at least 2 agents
- All referenced agents must exist
- Route targets must be valid

### For-Each Validation

- `source` must be dotted path with at least 3 parts (e.g., `agent.output.field`)
- `as` must be a valid Python identifier, not a reserved name
- `max_concurrent` must be 1-100
- Nested for-each groups are not allowed

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
