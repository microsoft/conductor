# Parallel Agent Execution

Conductor supports parallel execution of independent agents, allowing workflows to run multiple agents concurrently when they don't depend on each other's outputs. This can significantly reduce workflow execution time for tasks like parallel research, validation, or data gathering.

## Overview

In traditional sequential workflows, agents execute one after another:

```
Agent A → Agent B → Agent C → ...
```

With parallel execution, independent agents can run simultaneously:

```
         ┌─ Agent B ─┐
Agent A ─┤           ├─ Agent D
         └─ Agent C ─┘
```

## Basic Syntax

Define parallel agent groups using the `parallel:` section at the top level of your workflow YAML:

```yaml
workflow:
  name: my-workflow
  entry_point: start

# Parallel execution groups (top-level)
parallel:
  - name: parallel_group
    description: Execute multiple agents in parallel
    agents:
      - agent_1
      - agent_2
      - agent_3
    failure_mode: fail_fast
    routes:
      - to: next_agent

agents:
  - name: start
    # ... agent definition ...
    routes:
      - to: parallel_group

  - name: agent_1
    # ... agent definition ...

  - name: agent_2
    # ... agent definition ...

  - name: agent_3
    # ... agent definition ...

  - name: next_agent
    # ... agent definition ...
```

## Parallel Group Properties

### Required Properties

- **`name`**: Unique identifier for the parallel group
- **`agents`**: List of agent names to execute in parallel

### Optional Properties

- **`description`**: Human-readable description of the parallel group's purpose
- **`failure_mode`**: How to handle failures (default: `fail_fast`)
  - `fail_fast`: Stop immediately on first agent failure
  - `continue_on_error`: Continue if at least one agent succeeds
  - `all_or_nothing`: All agents must succeed or entire group fails
- **`routes`**: Routing rules evaluated after parallel group execution

## Context Isolation

Each parallel agent receives an **immutable snapshot** of the workflow context at the point when the parallel group starts executing. This ensures:

1. **No race conditions**: Agents cannot interfere with each other
2. **Deterministic behavior**: Results are consistent across runs
3. **Clean isolation**: Each agent sees the same context state

```yaml
agents:
  - name: data_collector
    # Collects initial data
    routes:
      - to: parallel_analysis

  - parallel:
      name: parallel_analysis
      agents:
        - analyzer_1  # Gets snapshot of context
        - analyzer_2  # Gets same snapshot
        - analyzer_3  # Gets same snapshot
```

Parallel agents **cannot** access each other's outputs during execution. They only see:
- Workflow inputs
- Outputs from agents executed before the parallel group
- Their own previous outputs (if the agent loops back)

## Output Aggregation

After parallel execution completes, outputs are aggregated into a structured format:

```json
{
  "parallel_group": {
    "outputs": {
      "agent_1": { "result": "..." },
      "agent_2": { "result": "..." },
      "agent_3": { "result": "..." }
    },
    "errors": {}
  }
}
```

Access parallel outputs in downstream agents:

```yaml
agents:
  - name: aggregator
    input:
      - parallel_group.outputs
    prompt: |
      Analyze the following parallel results:
      
      Agent 1: {{ parallel_group.outputs.agent_1.result }}
      Agent 2: {{ parallel_group.outputs.agent_2.result }}
      Agent 3: {{ parallel_group.outputs.agent_3.result }}
```

## Failure Modes

### fail_fast (Default)

Stops execution immediately when the first agent fails. Other running agents are cancelled.

```yaml
parallel:
  - name: validation
    agents: [checker_1, checker_2, checker_3]
    failure_mode: fail_fast

agents:
  # Define the checker agents here
```

**Use case**: Critical validations where any failure should stop the workflow.

**Behavior**:
- First agent failure cancels remaining agents
- Exception is raised immediately
- No outputs are stored

### continue_on_error

Continues execution even if some agents fail. The workflow proceeds if at least one agent succeeds.

```yaml
parallel:
  - name: multi_source_research
    agents: [source_1, source_2, source_3]
    failure_mode: continue_on_error

agents:
  # Define the source agents here
```

**Use case**: Gathering data from multiple sources where partial success is acceptable.

**Behavior**:
- All agents run to completion
- Successful outputs stored in `outputs` dict
- Failed agents stored in `errors` dict with exception details
- Workflow continues if at least one agent succeeded
- Raises error if all agents fail

**Error structure**:
```json
{
  "parallel_group": {
    "outputs": {
      "source_1": { "data": "..." }
    },
    "errors": {
      "source_2": {
        "error": "ConnectionError",
        "message": "Failed to connect to API",
        "agent": "source_2"
      },
      "source_3": {
        "error": "TimeoutError",
        "message": "Request timed out",
        "agent": "source_3"
      }
    }
  }
}
```

### all_or_nothing

All agents run to completion, but the workflow fails if any agent fails.

```yaml
parallel:
  - name: deployment_checks
    agents: [test_suite, security_scan, performance_check]
    failure_mode: all_or_nothing

agents:
  # Define the check agents here
```

**Use case**: Pre-deployment checks where all must pass but you want to see all results.

**Behavior**:
- All agents run to completion
- All outputs collected
- If any agent failed, raises error after all complete
- Useful for seeing all failures, not just the first one

## Routing from Parallel Groups

Parallel groups support routing just like regular agents:

```yaml
parallel:
  - name: validators
    agents: [validator_1, validator_2]
    failure_mode: continue_on_error
    routes:
      - to: success_handler
        when: "{{ validators.outputs | length >= 1 }}"
      - to: failure_handler

agents:
  # Define validator agents here
```

You can route based on:
- Number of successful agents
- Specific agent outputs
- Error conditions

## Examples

### Parallel Research from Multiple Sources

```yaml
workflow:
  name: parallel-research
  entry_point: planner

parallel:
  - name: parallel_researchers
    description: Research from multiple sources in parallel
    agents:
      - academic_search
      - web_search
      - expert_consultation
    failure_mode: continue_on_error
    routes:
      - to: synthesizer

agents:
  - name: planner
    prompt: "Create research plan for: {{ workflow.input.topic }}"
    output:
      plan: { type: object }
    routes:
      - to: parallel_researchers

  - name: academic_search
    input: [planner.output]
    tools: [scholarly_search]
    prompt: "Search academic sources for: {{ planner.output.plan }}"
    output:
      findings: { type: array }

  - name: web_search
    input: [planner.output]
    tools: [web_search]
    prompt: "Search web sources for: {{ planner.output.plan }}"
    output:
      findings: { type: array }

  - name: expert_consultation
    input: [planner.output]
    tools: [expert_db]
    prompt: "Find expert opinions on: {{ planner.output.plan }}"
    output:
      findings: { type: array }

  - name: synthesizer
    input:
      - planner.output
      - parallel_researchers.outputs
    prompt: |
      Synthesize research from multiple sources:
      
      Academic: {{ parallel_researchers.outputs.academic_search.findings | json }}
      Web: {{ parallel_researchers.outputs.web_search.findings | json }}
      Experts: {{ parallel_researchers.outputs.expert_consultation.findings | json }}
    routes:
      - to: $end
```

### Parallel Validation with All Must Pass

```yaml
workflow:
  name: code-validation
  entry_point: parallel_checks

parallel:
  - name: parallel_checks
    description: Run all validation checks in parallel
    agents:
      - syntax_check
      - security_scan
      - style_check
      - test_coverage
    failure_mode: all_or_nothing
    routes:
      - to: deployment

agents:
  - name: syntax_check
    tools: [linter]
    prompt: "Check syntax of: {{ workflow.input.code }}"
    output:
      passed: { type: boolean }
      issues: { type: array }

  - name: security_scan
    tools: [security_scanner]
    prompt: "Scan for security issues in: {{ workflow.input.code }}"
    output:
      passed: { type: boolean }
      vulnerabilities: { type: array }

  - name: style_check
    tools: [style_checker]
    prompt: "Check code style: {{ workflow.input.code }}"
    output:
      passed: { type: boolean }
      violations: { type: array }

  - name: test_coverage
    tools: [coverage_tool]
    prompt: "Check test coverage for: {{ workflow.input.code }}"
    output:
      passed: { type: boolean }
      coverage_percent: { type: number }

  - name: deployment
    input: [parallel_checks.outputs]
    prompt: "Deploy code - all checks passed"
    routes:
      - to: $end
```

### Conditional Routing Based on Parallel Results

```yaml
parallel:
  - name: quality_gates
    agents: [gate_1, gate_2, gate_3]
    failure_mode: continue_on_error
    routes:
      - to: auto_approve
        when: "{{ quality_gates.outputs | length == 3 }}"
      - to: manual_review
        when: "{{ quality_gates.outputs | length >= 2 }}"
      - to: rejection_handler

agents:
  # Define gate agents here
```

## Best Practices

### 1. Use for Independent Tasks

Only parallelize agents that don't depend on each other:

✅ **Good**: Parallel research from different sources
```yaml
parallel:
  agents: [source_a, source_b, source_c]
```

❌ **Bad**: Agent B depends on Agent A's output
```yaml
parallel:
  agents: [step_1, step_2]  # If step_2 needs step_1, don't parallelize!
```

### 2. Choose the Right Failure Mode

- **`fail_fast`**: Use for critical validation where any failure should stop
- **`continue_on_error`**: Use for data gathering where partial success is acceptable
- **`all_or_nothing`**: Use for checks where you want all results but all must pass

### 3. Handle Partial Failures

When using `continue_on_error`, check for errors in downstream agents:

```yaml
- name: aggregator
  prompt: |
    {% if parallel_group.errors %}
    Warning: Some sources failed:
    {% for agent, error in parallel_group.errors.items() %}
    - {{ agent }}: {{ error.message }}
    {% endfor %}
    {% endif %}
    
    Successful results:
    {% for agent, output in parallel_group.outputs.items() %}
    - {{ agent }}: {{ output | json }}
    {% endfor %}
```

### 4. Keep Agents Focused

Each parallel agent should have a single, focused task:

✅ **Good**: Separate agents for separate tasks
```yaml
parallel:
  agents: [fetch_data, validate_schema, check_permissions]
```

❌ **Bad**: One agent doing everything
```yaml
parallel:
  agents: [do_everything]  # Not utilizing parallelism!
```

### 5. Consider Context Size

Each parallel agent gets a full context snapshot. If your context is large:
- Use `context.mode: explicit` to limit what each agent sees
- Declare only necessary inputs in `input:` arrays

```yaml
agents:
  - name: focused_agent
    input:
      - workflow.input.topic
      - planner.output.plan
    # Won't receive unrelated context
```

### 6. Set Reasonable Timeouts

Parallel execution respects workflow timeout limits:

```yaml
workflow:
  limits:
    timeout_seconds: 300  # 5 minutes total
```

If one slow agent blocks others, consider:
- Adding agent-level timeouts (future feature)
- Using `fail_fast` to cancel slow agents
- Breaking long tasks into smaller agents

## Troubleshooting

### All Agents Fail with "Context not found"

**Problem**: Parallel agents reference context that doesn't exist.

**Solution**: Remember agents only see context from before the parallel group started.

```yaml
# ❌ Won't work - agents can't see each other
parallel:
  - name: p1
    agents: [a, b]

agents:
  - name: a
    input: [b.output]  # ERROR: b hasn't run yet!

# ✅ Works - both see earlier context
parallel:
  - name: p1
    agents: [a, b]

agents:
  - name: a
    input: [planner.output]
  - name: b
    input: [planner.output]
```

### Parallel Group Hangs or Times Out

**Problem**: One agent is taking too long or waiting for input.

**Solutions**:
1. Check agent prompts for open-ended questions
2. Ensure agents don't have `type: human_gate` (not supported in parallel)
3. Review tool configurations for timeouts
4. Use workflow-level timeout to prevent infinite hangs

### Outputs Not Aggregated as Expected

**Problem**: Can't access parallel agent outputs in downstream agents.

**Solution**: Use the correct path: `parallel_group.outputs.agent_name.field`

```yaml
# ✅ Correct
{{ parallel_researchers.outputs.web_search.findings }}

# ❌ Wrong
{{ web_search.findings }}  # Not directly accessible!
```

### Race Conditions / Non-Deterministic Results

**Problem**: Results vary between runs even with same inputs.

**Cause**: This shouldn't happen! Each agent gets an immutable context snapshot.

**Debug steps**:
1. Check if agents use external tools with varying results (e.g., web search)
2. Verify agents aren't using random/time-based logic
3. Check provider model settings for temperature/randomness

### Error: "Nested parallel groups not allowed"

**Problem**: Trying to define a parallel group inside another parallel group.

```yaml
# ❌ Not allowed
- parallel:
    agents: [a, b]
- name: a
  routes:
    - to: nested_parallel
- parallel:
    name: nested_parallel  # ERROR!
```

**Solution**: Flatten the workflow. Execute parallel groups sequentially:

```yaml
# ✅ Works
parallel:
  - name: first_batch
    agents: [a, b]
    routes:
      - to: second_batch
      
  - name: second_batch
    agents: [c, d]

agents:
  # Define agents here
```

### Memory or Performance Issues

**Problem**: Workflow uses too much memory or runs slowly.

**Causes**:
- Large context snapshots for each parallel agent
- Too many agents running in parallel

**Solutions**:
1. Use `context.mode: explicit` to reduce snapshot size
2. Limit context with `max_tokens`
3. Reduce number of parallel agents
4. Split into multiple sequential parallel groups

```yaml
context:
  mode: explicit
  max_tokens: 4000

# Instead of 10 parallel agents:
# parallel: [{ name: big, agents: [1,2,3,4,5,6,7,8,9,10] }]

# Use batches:
parallel:
  - name: batch1
    agents: [1,2,3,4,5]
  - name: batch2
    agents: [6,7,8,9,10]
```

## Limitations

1. **No nested parallelism**: Parallel groups cannot contain other parallel groups
2. **No cross-agent references**: Agents in the same parallel group cannot reference each other
3. **No human gates**: `type: human_gate` agents cannot be used in parallel groups
4. **No dynamic agent lists**: The `agents:` list must be defined at workflow load time
5. **Shared limits**: All parallel agents share the workflow's `max_iterations` and `timeout_seconds` limits

## Performance Considerations

### Expected Speedup

For N independent agents executing in parallel:
- **Best case**: ~N× faster (if agents take similar time)
- **Typical**: ~(N/2)× to ~(N-1)× faster
- **Worst case**: Limited by slowest agent

### Overhead

Parallel execution adds minimal overhead:
- Context snapshot creation (one-time `deepcopy`)
- Asyncio task scheduling (microseconds)
- Output aggregation (negligible)

### When NOT to Use Parallel Execution

- **Sequential dependencies**: When agents depend on each other's outputs
- **Single agent**: No benefit to parallelizing one agent
- **Shared rate-limited resources**: If all agents hit the same rate-limited API
- **Memory constrained**: Large context × many agents = high memory usage

## Advanced Patterns

### Fan-out / Fan-in

```yaml
parallel:
  - name: parallel_workers
    agents: [worker_1, worker_2, worker_3]
    routes:
      - to: combiner

agents:
  - name: splitter
    # Create subtasks
    routes:
      - to: parallel_workers

  - name: combiner
    input: [parallel_workers.outputs]
    # Merge results
```

### Conditional Parallelism

```yaml
parallel:
  - name: parallel_path
    agents: [fast_1, fast_2]

agents:
  - name: decider
    output:
      should_parallelize: { type: boolean }
    routes:
      - to: parallel_path
        when: "{{ output.should_parallelize }}"
      - to: sequential_path
```

### Parallel + Loop

```yaml
parallel:
  - name: validators
    agents: [v1, v2, v3]
    failure_mode: continue_on_error
    routes:
      - to: validators
        when: "{{ validators.outputs | length < 2 and context.iteration < 3 }}"
      - to: $end

agents:
  # Define validator agents
```

## Migration Guide

### Converting Sequential to Parallel

Before (sequential):
```yaml
agents:
  - name: task_a
    routes:
      - to: task_b
  - name: task_b
    routes:
      - to: task_c
  - name: task_c
    routes:
      - to: next
```

After (parallel):
```yaml
parallel:
  - name: parallel_tasks
    agents: [task_a, task_b, task_c]
    routes:
      - to: next

agents:
  - name: task_a
    # Remove routes - handled by parallel group
  - name: task_b
  - name: task_c
  - name: next
```

### Updating Context Dependencies

Review `input:` declarations to ensure agents only reference earlier context:

```yaml
# Before
agents:
  - name: task_b
    input: [task_a.output]  # Sequential dependency

# After - if making parallel
parallel:
  - name: p1
    agents: [task_a, task_b]

agents:
  - name: task_a
    input: [earlier_agent.output]
  - name: task_b
    input: [earlier_agent.output]  # Not task_a!
```

## See Also

- [Workflow YAML Schema](../README.md#workflow-yaml-schema) - Full YAML syntax reference
- [Examples](../examples/) - Complete example workflows
- [Context Management](../README.md#context-modes) - Understanding context modes
- [Routing](../README.md#routing) - Conditional routing patterns
