# Conductor Workflow YAML — Complete Reference

The complete manual for Conductor workflow YAML files. Read this top to bottom the
first time; after that, jump to the section you need.

## Contents

1. [What a Conductor workflow is](#1-what-a-conductor-workflow-is)
2. [File structure](#2-file-structure)
3. [Workflow configuration](#3-workflow-configuration)
4. [Agents](#4-agents)
5. [Routing](#5-routing)
6. [Context modes](#6-context-modes)
7. [Parallel groups](#7-parallel-groups)
8. [For-each groups](#8-for-each-groups)
9. [Sub-workflows](#9-sub-workflows)
10. [Inputs and outputs](#10-inputs-and-outputs)
11. [Limits and safety](#11-limits-and-safety)
12. [Checkpoints and resume](#12-checkpoints-and-resume)

---

## 1. What a Conductor workflow is

A Conductor workflow is a **YAML file that orchestrates agents deterministically**.
You define agents, their prompts, and the routing logic between them. The engine
executes agents in order, evaluates routes (first matching `when` wins), and
builds context according to the configured mode.

The word that matters is **deterministic**. Unlike conversational orchestration
where Claude decides the next step, Conductor workflows follow explicit routing
rules. The same inputs follow the same path every time.

**Why determinism matters:** Workflows are version-controlled, repeatable, and
resumable. If a workflow fails, Conductor auto-saves a checkpoint and you can
resume from the failed agent. The routing logic is code, not conversation.

---

## 2. File structure

A Conductor workflow is a single YAML file with these top-level sections:

```yaml
workflow:           # Required: metadata and configuration
  name: my-workflow
  entry_point: first-agent
  runtime:
    provider: copilot

agents:             # Required: list of agent definitions
  - name: first-agent
    prompt: "..."
    routes:
      - to: second-agent

parallel:           # Optional: static parallel groups
  - name: review-all
    agents: [...]

for_each:           # Optional: dynamic iteration
  - name: process-files
    source: "{{ workflow.input.files }}"
    agent: { ... }

output:             # Optional: final workflow output
  result: "{{ last_agent.output }}"
```

---

## 3. Workflow configuration

The `workflow:` section defines metadata and behavior for the entire workflow.

```yaml
workflow:
  name: string                      # Required: unique identifier
  description: string               # Optional: human-readable description
  entry_point: string               # Required: first agent to execute

  metadata:                         # Optional: free-form key/value pairs
    tracker: ado                    # surfaced in workflow_started event
    project_url: https://...

  instructions:                     # Optional: instruction files
    - ./docs/conventions.md         # prepended to every agent prompt
    - ./AGENTS.md

  runtime:
    provider: copilot               # copilot | claude
    default_model: gpt-4o           # model for all agents (unless overridden)
    temperature: 0.7                # Optional: 0.0-1.0
    max_tokens: 4096                # Optional: max output tokens
    default_reasoning_effort: medium # Optional: low | medium | high | xhigh

  context:
    mode: accumulate                # accumulate | last_only | explicit
    max_tokens: 100000              # Optional: context window limit
    trim_strategy: drop_oldest      # drop_oldest | truncate | summarize

  limits:
    max_iterations: 10              # Default: 10, max: 500
    timeout_seconds: 600            # Optional: wall-clock timeout

  hooks:
    on_start: "{{ template }}"      # Optional: evaluated on start
    on_complete: "{{ template }}"   # Optional: evaluated on success
    on_error: "{{ template }}"      # Optional: evaluated on error
```

### Provider and model

- **`provider`**: `copilot` (GitHub Copilot SDK) or `claude` (Anthropic API)
- **`default_model`**: Model identifier (e.g., `gpt-4o`, `claude-sonnet-4.5`)
- Per-agent `model:` overrides the default

### Reasoning effort

- **`default_reasoning_effort`**: Workflow-wide default for reasoning/thinking
- Values: `low`, `medium`, `high`, `xhigh`
- Per-agent `reasoning.effort:` overrides the default
- Copilot: maps to `reasoning_effort` on the session
- Claude: maps to extended thinking budget (low=2048, medium=8192, high=16384, xhigh=32768 tokens)

---

## 4. Agents

Agents are defined in the `agents:` list. Each agent is a unit of work.

```yaml
agents:
  - name: string                    # Required: unique identifier
    type: agent                     # agent | human_gate | script | workflow
    model: string                   # Optional: override default model
    
    prompt: |                       # Required for type=agent
      Multi-line prompt with Jinja2 templates.
      Access context: {{ workflow.input.field }}
      Previous agent: {{ previous_agent.output.field }}
    
    system_prompt: |                # Optional: system-level instructions
      You are a code reviewer.
    
    input:                          # Optional: explicit dependencies (explicit mode)
      - workflow.input.file
      - previous_agent.output.result
    
    output:                         # Optional: structured output schema
      field_name:
        type: string                # string | number | boolean | array | object
        description: "Field purpose"
        required: true
    
    tools: null                     # null = all, [] = none, [list] = subset
    
    reasoning:                      # Optional: per-agent reasoning override
      effort: high                  # low | medium | high | xhigh
    
    max_session_seconds: 300        # Optional: per-agent timeout
    max_agent_iterations: 20        # Optional: tool-use iteration limit
    
    routes:                         # Required: routing logic
      - to: next_agent
        when: "{{ condition }}"
```

### Agent types

- **`agent`** (default): LLM-backed agent (Copilot or Claude)
- **`human_gate`**: Pauses for human input
- **`script`**: Runs a shell command
- **`workflow`**: Executes a sub-workflow

### Structured output

When `output:` is defined, the agent must return a JSON object matching the schema.
The engine validates the output and makes it available to downstream agents.

```yaml
agents:
  - name: reviewer
    prompt: "Review the code"
    output:
      passed:
        type: boolean
        required: true
      issues:
        type: array
        items:
          type: object
          properties:
            severity: { type: string }
            description: { type: string }
```

Access in templates: `{{ reviewer.output.passed }}`, `{{ reviewer.output.issues[0].severity }}`

---

## 5. Routing

Routes are evaluated **in order**. First matching `when` wins. A route with no
`when` always matches.

```yaml
routes:
  - to: fixer
    when: "{{ output.issues | length > 0 }}"  # Jinja2 template
  - to: verifier
    when: "score > 7"                         # Arithmetic expression
  - to: $end                                  # No condition = always matches
```

### Route conditions

Two syntaxes are supported:

1. **Jinja2 templates**: `{{ expression }}` — full Jinja2 with filters
2. **Arithmetic expressions**: `score > 7`, `iteration < 5` — simpleeval

### Special route targets

- **`$end`**: Terminates the workflow
- **Agent name**: Routes to that agent (can loop back)

### Loop-back routing

Route back to an earlier agent to create a loop. Always set `limits.max_iterations`
to prevent infinite loops.

```yaml
agents:
  - name: reviewer
    output:
      passed: { type: boolean }
    routes:
      - to: fixer
        when: "{{ not output.passed }}"
      - to: $end
  
  - name: fixer
    routes:
      - to: reviewer  # Loop back

workflow:
  limits:
    max_iterations: 10
```

### Output transforms

Routes can transform the final output:

```yaml
routes:
  - to: $end
    output:
      status: success
      result: "{{ output.value }}"
```

---

## 6. Context modes

Context modes control what data is available to agents in their prompts.

### `accumulate` (default)

All prior agent outputs are available. Later agents can reference any earlier agent.

```yaml
workflow:
  context:
    mode: accumulate

agents:
  - name: agent1
    prompt: "Step 1"
  - name: agent2
    prompt: "Step 2, using {{ agent1.output }}"
  - name: agent3
    prompt: "Step 3, using {{ agent1.output }} and {{ agent2.output }}"
```

### `last_only`

Only the previous agent's output is available. Strict pipeline.

```yaml
workflow:
  context:
    mode: last_only

agents:
  - name: agent1
    prompt: "Step 1"
  - name: agent2
    prompt: "Step 2, using {{ agent1.output }}"
  - name: agent3
    prompt: "Step 3, using {{ agent2.output }}"  # agent1 not available
```

### `explicit`

Only inputs declared in the agent's `input:` list are available. Maximum isolation.

```yaml
workflow:
  context:
    mode: explicit

agents:
  - name: agent1
    prompt: "Step 1"
  - name: agent2
    input:
      - workflow.input.file
      - agent1.output.result
    prompt: "Step 2, using {{ workflow.input.file }} and {{ agent1.output.result }}"
```

### Context trimming

When context exceeds `max_tokens`, the engine applies `trim_strategy`:

- **`drop_oldest`**: Remove oldest agent outputs first
- **`truncate`**: Truncate long strings
- **`summarize`**: Use LLM to summarize old outputs (requires provider)

---

## 7. Parallel groups

Parallel groups execute multiple agents concurrently. All agents in the group
start at the same time and the group completes when all finish (or fail according
to `failure_mode`).

```yaml
parallel:
  - name: review-all
    agents:
      - security-reviewer
      - performance-reviewer
      - style-reviewer
    failure_mode: continue_on_error  # fail_fast | continue_on_error | all_or_nothing
    routes:
      - to: merger

agents:
  - name: security-reviewer
    prompt: "Review for security issues"
  - name: performance-reviewer
    prompt: "Review for performance issues"
  - name: style-reviewer
    prompt: "Review for style issues"
  - name: merger
    prompt: |
      Merge findings:
      Security: {{ review-all.security-reviewer.output }}
      Performance: {{ review-all.performance-reviewer.output }}
      Style: {{ review-all.style-reviewer.output }}
```

### Failure modes

- **`fail_fast`**: Stop immediately on first agent failure
- **`continue_on_error`**: Continue even if some agents fail
- **`all_or_nothing`**: Fail the group if any agent fails (after all complete)

### Accessing parallel outputs

Parallel group outputs are namespaced: `{{ group_name.agent_name.output }}`

Errors are available: `{{ group_name.errors.agent_name }}`

---

## 8. For-each groups

For-each groups iterate over an array, executing an agent for each item. The
array is resolved at runtime from a Jinja2 template.

```yaml
for_each:
  - name: process-files
    type: for_each
    source: workflow.input.files    # Dotted path reference to array
    as: item                        # Loop variable name
    agent:
      name: processor
      prompt: "Process {{ item }}"
      output:
        result: { type: string }
    max_concurrent: 4                     # Optional: parallel execution limit
    failure_mode: continue_on_error       # fail_fast | continue_on_error | all_or_nothing
    routes:
      - to: aggregator

agents:
  - name: aggregator
    prompt: |
      Aggregate results:
      {% for result in process-files.outputs %}
      - {{ result.result }}
      {% endfor %}
```

### Loop variables

Inside the for-each agent, these variables are available:

- **`{{ item }}`**: Current item value
- **`{{ _index }}`**: Zero-based index
- **`{{ _key }}`**: Key (if iterating over dict items)

### Accessing for-each outputs

- **`{{ group_name.outputs }}`**: List of all successful outputs
- **`{{ group_name.outputs[0] }}`**: First output
- **`{{ group_name.errors }}`**: Dict of errors by item key/index
- **`{{ group_name.count }}`**: Total items processed

### Key-based output

Use `key_by` to create a dict instead of a list:

```yaml
for_each:
  - name: process_kpis
    type: for_each
    source: workflow.input.kpis
    as: kpi
    key_by: kpi_id       # Dotted path navigated from each item (not a Jinja2 template)
    agent:
      name: processor
      prompt: "Process KPI {{ kpi.name }}"
```

Access: `{{ process_kpis.outputs['KPI-123'] }}`

---

## 9. Sub-workflows

Agents with `type: workflow` execute another workflow as a black-box step.

```yaml
agents:
  - name: sub-task
    type: workflow
    workflow: ./sub-workflow.yaml  # Path to workflow file
    input_mapping:                 # Optional: map parent context to sub-workflow inputs
      file: "{{ workflow.input.file }}"
      threshold: "5"
    max_depth: 3                   # Optional: nesting limit (default: 10)
    routes:
      - to: next-agent
```

The sub-workflow's final output becomes the agent's output:
`{{ sub-task.output.result }}`

### Input mapping

`input_mapping` is a dict of Jinja2 templates. Each key becomes a workflow input
in the sub-workflow.

### Nesting limit

Sub-workflows can nest up to `max_depth` levels (default: 10, global max: 10).

---

## 10. Inputs and outputs

### Workflow inputs

Inputs are passed via CLI `--input` flags:

```bash
conductor run workflow.yaml --input file=src/api.py --input threshold=5
```

Access in templates: `{{ workflow.input.file }}`, `{{ workflow.input.threshold }}`

### Input schema

Define expected inputs in the workflow:

```yaml
workflow:
  input:
    file:
      type: string
      required: true
    threshold:
      type: number
      default: 10
```

### Workflow output

The `output:` section defines the final workflow output using Jinja2 templates:

```yaml
output:
  total_issues: "{{ reviewer.output.issues | length }}"
  passed: "{{ verifier.output.passed }}"
  summary: |
    Found {{ reviewer.output.issues | length }} issues.
    Verification: {{ 'passed' if verifier.output.passed else 'failed' }}
```

---

## 11. Limits and safety

### Iteration limit

`max_iterations` caps the total number of agent executions (including loops):

```yaml
workflow:
  limits:
    max_iterations: 10  # Default: 10, max: 500
```

When the limit is reached, the workflow pauses and prompts for continuation (CLI)
or waits for user input (web dashboard).

### Timeout

`timeout_seconds` sets a wall-clock limit:

```yaml
workflow:
  limits:
    timeout_seconds: 600  # 10 minutes
```

When exceeded, the workflow fails with a `TimeoutError`.

### Per-agent limits

Agents can override the workflow-level limits:

```yaml
agents:
  - name: slow-agent
    max_session_seconds: 300      # 5 minutes for this agent
    max_agent_iterations: 20      # Tool-use iteration limit
```

---

## 12. Checkpoints and resume

Conductor auto-saves a checkpoint when a workflow fails. Resume from the failed
agent:

```bash
conductor resume workflow.yaml
```

### What's saved

- Workflow context (all agent outputs)
- Execution limits (iteration count, elapsed time)
- Current agent name
- Provider session IDs (for Copilot resume)

### What's NOT saved

- In-flight agent state (partial outputs)
- Temporary files
- Environment variables

### Resume behavior

- The workflow resumes from the failed agent
- All prior agent outputs are restored
- Iteration count continues from the checkpoint
- Provider sessions are resumed (Copilot) or recreated (Claude)

### Checkpoint location

Checkpoints are saved in `~/.conductor/checkpoints/<workflow-name>/`:

```bash
conductor checkpoints  # List all checkpoints
```

---

## Quick reference

### Workflow structure

```yaml
workflow:
  name: my-workflow
  entry_point: first-agent
  runtime: { provider: copilot, default_model: gpt-4o }
  context: { mode: accumulate }
  limits: { max_iterations: 10 }

agents:
  - name: first-agent
    prompt: "{{ workflow.input.task }}"
    output: { result: { type: string } }
    routes: [{ to: second-agent }]

output:
  result: "{{ first-agent.output.result }}"
```

### Context access

- `{{ workflow.input.field }}` — workflow inputs
- `{{ agent_name.output.field }}` — agent outputs
- `{{ workflow.dir }}` — workflow directory
- `{{ workflow.file }}` — workflow file path
- `{{ context.iteration }}` — current iteration count

### Jinja2 filters

- `{{ list | length }}` — list length
- `{{ list | join(', ') }}` — join list
- `{{ dict | tojson }}` — serialize to JSON
- `{{ value | default('fallback') }}` — default value
- `{{ text | upper }}` — uppercase
- `{{ text | lower }}` — lowercase

### Route conditions

- `{{ output.passed }}` — boolean
- `{{ output.issues | length > 0 }}` — list length
- `score > 7` — arithmetic
- `iteration < 5` — iteration count
- `{{ not output.passed }}` — negation

---

## Caps and limits

| Limit | Default | Max | What happens |
|-------|---------|-----|--------------|
| `max_iterations` | 10 | 500 | Workflow pauses, prompts for continuation |
| `timeout_seconds` | None | None | Workflow fails with `TimeoutError` |
| `max_depth` (sub-workflows) | 10 | 10 | Workflow fails with `ExecutionError` |
| `max_concurrent` (for_each) | 10 | 100 | Batches execution |
| Context `max_tokens` | None | None | Applies `trim_strategy` |

---

## Common patterns

See `references/patterns.md` for copy-paste orchestration patterns:
- Fan-out (parallel / for_each)
- Pipeline (sequential routing)
- Loop-until-pass
- Adversarial verify
- Judge panel
- Nested workflows
