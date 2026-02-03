# Dynamic Parallel Agents Brainstorm

## Overview

Dynamic parallel execution extends the static `parallel:` groups to support runtime-determined parallelism—where one agent's output (a list) spawns N parallel instances of another agent template.

## Motivating Use Case

From `examples/kpi-analysis.yaml`: A finder agent returns a list of KPIs, and we want to run analyzer agents in parallel for each KPI rather than processing them sequentially in a loop.

**Current approach (sequential):**
```yaml
agents:
  - name: finder
    output:
      next_kpi: { type: object }
      all_complete: { type: boolean }
    routes:
      - to: $end
        when: "{{ output.all_complete }}"
      - to: analyzer

  - name: analyzer
    input: [finder.output]
    routes:
      - to: finder  # Loop back to process next KPI
```

**Problem**: Processing 50 KPIs takes 50 sequential iterations.

**Desired approach (dynamic parallel):**
```yaml
agents:
  - name: finder
    output:
      kpis: { type: array }  # Returns ALL KPIs at once

  - name: analyzers
    type: for_each
    source: finder.output.kpis
    as: kpi
    max_concurrent: 5
    agent:
      model: opus-4.5
      prompt: "Analyze {{ kpi.kpi_id }}"
```

## Static vs Dynamic Parallel Comparison

| Aspect | Static Parallel Groups | Dynamic For-Each |
|--------|------------------------|------------------|
| **Agent count** | Known at YAML load time | Resolved at runtime from context |
| **Schema** | `parallel: [{name, agents: [a, b, c]}]` | `type: for_each`, `source: agent.output.items` |
| **Output structure** | `{outputs: {agent1: {...}, agent2: {...}}}` | `{outputs: [...], errors: [...]}` (array) |
| **Validation** | Validate agent names exist | Validate source is array type |
| **Complexity** | Lower—fixed agent references | Higher—template instantiation, array resolution |

## Implementation Approach

**Recommendation**: Implement static parallel first (Epics 1–10 in parallel-agent-execution.plan.md), then add dynamic for-each as Epic 11–15.

### Key Components

1. **Schema Extension** - Add `ForEachDef` to `schema.py`:
   ```python
   class ForEachDef(BaseModel):
       name: str
       type: Literal["for_each"]
       source: str              # Reference to array in context
       as: str                  # Variable name for each item
       agent: AgentDef          # Template agent definition
       max_concurrent: int = 10
       failure_mode: Literal["fail_fast", "continue_on_error", "all_or_nothing"] = "fail_fast"
   ```

2. **Array Resolution** - Add logic in `context.py` to resolve `source` references like `finder.output.kpis` into actual arrays at runtime.

3. **Execution Engine** - Implement `_execute_for_each_group()` in `workflow.py` using the same `asyncio.gather()` + `deepcopy` pattern, but with loop variable injection (`{{ kpi }}`, `{{ _index }}`).

4. **Concurrency Controls** - `max_concurrent` option (default: 10) to prevent spawning unbounded parallel tasks.

5. **Template Rendering** - Support array-based output access (`{{ analyzers.outputs[0].success }}`).

### Code Reuse from Static Parallel

The for-each implementation can reuse ~80% of static parallel infrastructure:
- Context snapshots via `copy.deepcopy()`
- `asyncio.gather()` execution pattern
- Failure mode handling (`fail_fast`, `continue_on_error`, `all_or_nothing`)
- Error aggregation and reporting
- Verbose logging patterns

## Design Decisions

1. **Batching strategy**: Require explicit `max_concurrent` with reasonable default (10). This prevents runaway parallelism while giving users control.

2. **Output indexing**: Support both index-based (`outputs[0]`) and key-based (`outputs["KPI123"]`) access via optional `key_by: kpi.kpi_id` parameter.

3. **Partial results access**: Results only accessible after all instances complete. Simpler model, matches static parallel behavior.

4. **Nested for-each**: Explicitly forbidden, like nested parallel groups. Keeps execution model simple and debuggable.

5. **Empty array handling**: Skip execution, store `{outputs: [], errors: [], count: 0}`. Workflow continues normally—this is not an error condition.

## YAML Syntax

### Primary syntax: Inline agent definition
```yaml
- name: analyzers
  type: for_each
  source: finder.output.kpis
  as: kpi
  max_concurrent: 5
  failure_mode: continue_on_error
  key_by: kpi.kpi_id  # Optional: enables outputs["KPI123"] access
  agent:
    model: opus-4.5
    prompt: "Analyze {{ kpi.kpi_id }}"
    output:
      success: { type: boolean }
      summary: { type: string }
```

### Output access patterns
```yaml
# Index-based access
- name: summarizer
  prompt: |
    First result: {{ analyzers.outputs[0].summary }}
    All results:
    {% for result in analyzers.outputs %}
    - {{ result.summary }}
    {% endfor %}

# Key-based access (when key_by is specified)
- name: summarizer
  prompt: |
    Specific KPI: {{ analyzers.outputs["KPI123"].summary }}
    
# Error handling
- name: summarizer
  prompt: |
    Successful: {{ analyzers.outputs | length }}
    Failed: {{ analyzers.errors | length }}
    {% if analyzers.errors %}
    Errors:
    {% for err in analyzers.errors %}
    - {{ err.key }}: {{ err.message }}
    {% endfor %}
    {% endif %}
```

## Future Enhancements

- **Option B**: Reference existing agent as template (`template: analyzer_template`)
- **Option C**: Template with overrides (`template: base`, `overrides: {model: opus-4.5}`)
- **Streaming results**: Callback/webhook as each instance completes
- **Retry failed items**: Re-run only failed instances from `errors` array
