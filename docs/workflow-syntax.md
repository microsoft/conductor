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
    type: agent                     # agent | human_gate | script | workflow (default: agent)
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

**Restrictions** — script steps cannot have `prompt`, `model`, `provider`, `tools`, `system_prompt`, `output` schema, or `options`. Script steps also cannot be used inside `parallel` groups or `for_each` groups.

**Environment variable note** — values in `env` are passed as-is to the subprocess (they are not rendered as Jinja2 templates). Use `${VAR}` syntax in the workflow YAML loader if you need environment variable substitution in env values.

### Sub-Workflow Steps

Sub-workflow steps reference external workflow YAML files, enabling composable and reusable workflow building blocks. The sub-workflow runs as a black box — its internal agents are not visible to the parent.

```yaml
agents:
  - name: deep_research
    type: workflow
    workflow: ./research-pipeline.yaml   # Required: path to sub-workflow YAML
    input:                               # Optional: explicit input declarations
      - workflow.input.topic
    output:                              # Optional: output schema for validation
      findings:
        type: string
    routes:
      - to: synthesizer
```

**Key semantics:**

- The `workflow` path is resolved relative to the parent workflow file
- Sub-workflow inherits the parent's provider configuration
- Sub-workflow output is stored in context and accessible via `{{ agent_name.output.field }}`
- Recursive composition is supported (sub-workflows can reference other sub-workflows) with a depth limit of 10
- Circular references (a workflow referencing itself) are detected and rejected

**Access sub-workflow output in downstream agents:**

```yaml
prompt: |
  The research findings were:
  {{ deep_research.output.findings }}
```

**Restrictions** — workflow steps cannot have `prompt`, `model`, `provider`, `tools`, `system_prompt`, `command`, or `options`. Workflow steps also cannot be used inside `parallel` groups or `for_each` groups.

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
