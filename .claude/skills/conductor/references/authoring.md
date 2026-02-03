# Workflow Authoring Guide

Complete reference for creating and modifying Conductor workflow YAML files.

## Workflow Configuration

```yaml
workflow:
  name: my-workflow              # Required: unique identifier
  description: What it does      # Optional
  version: "1.0.0"               # Optional
  entry_point: first_agent       # Required: starting agent
  
  runtime:
    provider: copilot            # copilot, claude, openai-agents
    default_model: gpt-5.2         # Default model for agents
  
  input:                         # Define workflow inputs
    param_name:
      type: string               # string, number, boolean, array, object
      required: true
      default: "value"
      description: What it is

  context:
    mode: accumulate             # accumulate, last_only, explicit

  limits:
    max_iterations: 10           # Max agent executions (default: 10)
    timeout_seconds: 600         # Total timeout (default: 600)
```

## Agent Definition

```yaml
agents:
  - name: my_agent               # Required: unique identifier
    type: agent                  # agent (default) or human_gate
    description: What it does
    model: gpt-5.2                 # Override workflow default
    
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
    
    routes:                      # Where to go next
      - to: next_agent
```

## Routing Patterns

### Linear

```yaml
routes:
  - to: next_agent
```

### Conditional

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

## Human Gates

Pause workflow for user decisions:

```yaml
agents:
  - name: approval_gate
    type: human_gate
    prompt: |
      Review the design:
      {{ designer.output.design }}
    options:
      approve:
        label: "Approve"
        description: "Accept the design"
        routes:
          - to: $end
      revise:
        label: "Request Changes"
        description: "Send back for revision"
        routes:
          - to: designer
    output:
      decision:
        type: string
      feedback:
        type: string
```

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

Only specified inputs available:

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

## Template Variables (Jinja2)

| Variable | Description |
|----------|-------------|
| `{{ workflow.input.param }}` | Workflow input |
| `{{ workflow.name }}` | Workflow name |
| `{{ agent_name.output.field }}` | Agent output |
| `{{ output.field }}` | Current agent output (in routes) |

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

### HTTP server

```yaml
runtime:
  mcp_servers:
    s360:
      type: http
      url: https://mcp.server.example.com/
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
        API_KEY: "${API_KEY}"
      tools: ["*"]
```

### Selective tool access

```yaml
tools: ["search", "fetch"]  # Only these tools available
```

## Output Schema

Map agent outputs to workflow output:

```yaml
output:
  answer: "{{ answerer.output.answer }}"
  summary: "{{ reviewer.output.summary }}"
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

### Multi-Agent Pipeline

```yaml
agents:
  - name: researcher
    routes:
      - to: analyzer

  - name: analyzer
    routes:
      - to: writer

  - name: writer
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
    options:
      approve:
        routes:
          - to: $end
      revise:
        routes:
          - to: designer
```

## Validation Rules

- `entry_point` must reference a valid agent
- All agents must be reachable from entry_point
- All paths must eventually reach `$end`
- Route `when` conditions must be valid Jinja2
- Agent names must be unique
- Non-gate agents require at least one route
