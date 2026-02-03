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
- [Hooks](#hooks)

## Workflow Configuration

The top-level `workflow` section defines metadata and behavior for the entire workflow.

```yaml
workflow:
  name: string                      # Required: Unique workflow identifier
  description: string               # Optional: Human-readable description
  entry_point: string               # Required: Name of first agent to execute
  
  limits:
    max_iterations: 10              # Default: 10, max: 500
    timeout_seconds: 600            # Optional: Maximum wall-clock time (seconds)
  
  hooks:
    on_start: "{{ template }}"      # Optional: Expression evaluated on start
    on_complete: "{{ template }}"   # Optional: Expression evaluated on success
    on_error: "{{ template }}"      # Optional: Expression evaluated on error

  context_mode: accumulate          # accumulate | snapshot | minimal (default: accumulate)
```

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
    type: agent                     # agent | human_gate (default: agent)
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
    
    tools:                          # Optional: Agent-specific tools
      - tool_name
    
    routes:                         # Optional: Routing logic
      - to: next_agent              # Agent name or $end
        when: "{{ condition }}"     # Optional: Route condition
```

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
```

### Iteration Counting

- Each agent execution counts as 1 iteration
- Parallel agents count individually (3 parallel agents = 3 iterations)
- Loop-back patterns increment the counter on each iteration

### Timeout Behavior

- Workflow terminates when `timeout_seconds` is exceeded
- Includes all agent execution time and overhead
- `None` (default) means no timeout

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
- `workflow.name`, `workflow.description`
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
- [Examples](../examples/) - Complete workflow examples
- [README](../README.md) - Getting started and CLI reference
