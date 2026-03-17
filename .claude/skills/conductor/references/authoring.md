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
    provider: copilot            # copilot (default) or claude
    default_model: gpt-5.2       # Default model for agents
    temperature: 0.7             # 0.0-1.0 (optional)
    max_tokens: 4096             # Max output tokens per response (optional)
    timeout: 600                 # Per-request timeout in seconds (optional)
    max_agent_iterations: 50     # Max tool-use roundtrips per agent (1-500, optional)
    max_session_seconds: 120     # Wall-clock timeout per agent session (optional)

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
```

## Agent Definition

```yaml
agents:
  - name: my_agent               # Required: unique identifier
    type: agent                  # agent (default), human_gate, or script
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
    max_session_seconds: 60      # Wall-clock timeout for this agent (optional)

    routes:                      # Where to go next
      - to: next_agent
```

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

Script steps always produce three fields (no custom `output` schema):

```jinja2
{{ script_name.output.stdout }}     # Captured standard output
{{ script_name.output.stderr }}     # Captured standard error
{{ script_name.output.exit_code }}  # Process exit code (0 = success)
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

Script agents **cannot** have: `prompt`, `provider`, `model`, `tools`, `output`, `system_prompt`, `options`.
Command and args support Jinja2 templating for dynamic values.

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
- `output.selected` - the `value` of the chosen option
- `output.feedback` - text input from `prompt_for` (if specified)

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
