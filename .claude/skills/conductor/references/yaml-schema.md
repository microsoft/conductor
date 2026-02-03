# Conductor Schema Reference

Complete reference for all YAML configuration options.

## Workflow Schema

```yaml
workflow:
  # Required fields
  name: string                      # Unique workflow identifier (alphanumeric, hyphens, underscores)
  entry_point: string               # Name of first agent to execute
  
  # Optional fields
  description: string               # Human-readable description
  version: string                   # Semantic version (e.g., "1.0.0")
  
  # Runtime configuration
  runtime:
    provider: string                # "copilot" (default), "claude", "openai-agents"
    default_model: string           # Default model for all agents
    mcp_servers:                    # MCP server configurations
      <server_name>:
        type: string                # "stdio" (default) or "http"
        command: string             # Command to run (stdio only)
        args: array                 # Command arguments (stdio only)
        url: string                 # Server URL (http only)
        tools: array                # Tool whitelist or ["*"] for all
        env: object                 # Environment variables
  
  # Input parameters
  input:
    <param_name>:
      type: string                  # "string", "number", "boolean", "array", "object"
      required: boolean             # Default: true
      default: any                  # Default value if not provided
      description: string           # Parameter description
  
  # Context management
  context:
    mode: string                    # "accumulate" (default), "last_only", "explicit"
    max_tokens: number              # Maximum context tokens (optional)
    trim_strategy: string           # "truncate", "drop_oldest", "summarize"
  
  # Safety limits
  limits:
    max_iterations: number          # Max agent executions (default: 10, max: 100)
    timeout_seconds: number         # Total timeout (default: 600, max: 3600)
  
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
    type: string                    # "agent" (default) or "human_gate"
    description: string             # What this agent does
    model: string                   # Override default_model
    
    # Input specification (for explicit context mode)
    input:
      - string                      # Reference paths, e.g., "workflow.input.question"
                                    # Use "?" suffix for optional: "other_agent.output?"
    
    # Prompt template
    prompt: string                  # Jinja2 template for agent instructions
    
    # System prompt (optional)
    system_prompt: string           # System message for the agent
    
    # Output schema
    output:
      <field_name>:
        type: string                # "string", "number", "boolean", "array", "object"
        description: string         # Field description
        items:                      # For array types
          type: string              # Item type
        properties:                 # For object types
          <prop_name>:
            type: string
            description: string
    
    # Routing rules
    routes:
      - to: string                  # Target agent name, "$end", or "self"
        when: string                # Optional Jinja2 condition
    
    # Agent-level tools
    tools:
      - string                      # Tool names available to this agent
```

## Human Gate Schema

```yaml
agents:
  - name: string
    type: human_gate
    
    # Gate prompt shown to user
    prompt: string                  # Jinja2 template
    
    # Available options
    options:
      <option_key>:
        label: string               # Button/option label
        description: string         # Option description
        routes:
          - to: string              # Where to go if selected
            when: string            # Optional additional condition
    
    # Output captured from user
    output:
      decision:
        type: string                # The selected option key
      feedback:
        type: string                # Optional user feedback
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

## MCP Server Examples

### Web Search Server

```yaml
runtime:
  mcp_servers:
    web-search:
      command: sh
      args: ["-c", "MODE=stdio DEFAULT_SEARCH_ENGINE=bing exec npx -y open-websearch@latest"]
      tools: ["*"]
```

### Context7 Server

```yaml
runtime:
  mcp_servers:
    context7:
      command: npx
      args: ["-y", "@upstash/context7-mcp@latest"]
      tools: ["*"]
```

### HTTP-based Server

```yaml
runtime:
  mcp_servers:
    s360:
      type: http
      url: https://mcp.server.example.com/
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

## Validation Rules

### Workflow Validation

- `name` must be present and non-empty
- `entry_point` must reference a valid agent name
- All referenced agents must be defined
- Input parameter names must be valid identifiers

### Agent Validation

- `name` must be unique within workflow
- `routes` required for type `agent` (not for `human_gate`)
- All route targets must be valid agent names, `$end`, or `self`
- `when` conditions must be valid Jinja2 expressions

### Routing Validation

- At least one route must be reachable (not all conditional)
- Circular routes are allowed but require `max_iterations`
- All agents must be reachable from `entry_point`
- All paths must eventually reach `$end`

## Error Messages

| Error | Cause | Solution |
|-------|-------|----------|
| `Missing entry_point` | No `entry_point` in workflow | Add `entry_point: agent_name` |
| `Unknown agent: X` | Route targets non-existent agent | Check agent names match |
| `Unreachable agent: X` | Agent not reachable from entry | Add route to agent or remove |
| `No terminal route` | No path reaches `$end` | Add `$end` route |
| `Invalid condition` | Malformed `when` clause | Check Jinja2 syntax |
