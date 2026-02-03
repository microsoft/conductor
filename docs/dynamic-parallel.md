# Dynamic Parallel Execution (For-Each)

Conductor supports **dynamic parallel execution** via for-each groups, which allow workflows to process arrays of items with parallel agent instances spawned at runtime. This is perfect for tasks like analyzing multiple KPIs, processing datasets, or performing batch operations.

## Overview

**Static parallel** groups run a fixed set of named agents:
```
parallel_group: [agent_a, agent_b, agent_c]  # Known at workflow definition
```

**Dynamic parallel (for-each)** groups run N copies of an agent template, where N is determined at runtime:
```
for_each(items):  # N = len(items), determined when workflow runs
  agent_template(item)
```

### When to Use For-Each

✅ **Use for-each when:**
- Processing variable-length arrays (e.g., list of KPIs, files, user requests)
- Each item requires the same processing logic
- The number of items is unknown at workflow definition time
- You want to parallelize batch operations

❌ **Use static parallel when:**
- Running distinct agents with different purposes
- Agent count is fixed and known at definition time
- Agents have different prompts or configurations

## Basic Syntax

Define for-each groups at the top level of your workflow YAML:

```yaml
workflow:
  name: kpi-analyzer
  entry_point: kpi_finder

for_each:
  - name: kpi_processors
    type: for_each
    source: kpi_finder.output.kpis  # Array reference
    as: kpi                          # Loop variable name
    max_concurrent: 5                # Parallel execution limit
    failure_mode: continue_on_error  # Error handling
    
    agent:
      model: claude-sonnet-4.5
      prompt: |
        Analyze this KPI:
        Name: {{ kpi.name }}
        Value: {{ kpi.value }}
        
        Provide insights and recommendations.
      output:
        analysis:
          type: string
        recommendations:
          type: array
    
    routes:
      - to: aggregator

agents:
  - name: kpi_finder
    # ... finds KPIs ...
    routes:
      - to: kpi_processors
  
  - name: aggregator
    # ... aggregates results ...
```

## For-Each Properties

### Required Properties

- **`name`**: Unique identifier for the for-each group
- **`type`**: Must be `"for_each"` to mark as dynamic parallel
- **`source`**: Reference to array in context (dotted path notation)
- **`as`**: Loop variable name (accessible in templates)
- **`agent`**: Inline agent definition used as template

### Optional Properties

- **`description`**: Human-readable purpose description
- **`max_concurrent`**: Maximum concurrent executions per batch (default: 10)
- **`failure_mode`**: Error handling strategy (default: `fail_fast`)
- **`key_by`**: Path to extract keys for dict-based outputs
- **`routes`**: Routing rules evaluated after for-each execution

## Source References

The `source` field uses **dotted path notation** to reference arrays in the workflow context:

```yaml
# Reference agent output fields
source: finder.output.items
source: analyzer.output.results.data_points

# Reference workflow input
source: workflow.input.tasks

# Reference parallel group outputs
source: parallel_fetchers.outputs.data_collector.items
```

**At runtime**, the source path is resolved to an array. If the path:
- **Doesn't exist**: Error is raised with clear message
- **Not an array**: Error is raised (must be list type)
- **Empty array**: For-each completes immediately with empty outputs

## Loop Variables

Three special variables are available in agent templates:

### `{{ <var> }}` - Current Item

The loop variable (specified in `as:`) contains the current array item:

```yaml
for_each:
  - name: processors
    source: finder.output.items
    as: item
    agent:
      prompt: |
        Current item: {{ item }}
        
        # If items are objects:
        Item ID: {{ item.id }}
        Item name: {{ item.name }}
```

### `{{ _index }}` - Zero-Based Index

The index of the current item in the source array:

```yaml
agent:
  prompt: |
    Processing item {{ _index + 1 }} of {{ total_items }}
    Item data: {{ item }}
```

### `{{ _key }}` - Extracted Key (Optional)

When `key_by` is specified, `{{ _key }}` contains the extracted key:

```yaml
for_each:
  - name: analyzers
    source: finder.output.kpis
    as: kpi
    key_by: kpi.kpi_id  # Extract kpi.kpi_id as key
    agent:
      prompt: |
        Analyzing KPI: {{ _key }}  # The kpi_id value
        Full KPI data: {{ kpi | json }}
```

**Reserved names**: The following cannot be used as loop variable names:
- `workflow`, `context`, `output`, `_index`, `_key`

## Batching and Concurrency

For-each groups process items in **sequential batches** controlled by `max_concurrent`:

```yaml
for_each:
  - name: processors
    source: finder.output.items  # Suppose this resolves to 25 items
    max_concurrent: 10
    agent:
      # ... agent definition ...
```

**Execution flow:**
1. **Batch 1**: Items 0-9 execute in parallel (10 items)
2. Wait for batch 1 to complete
3. **Batch 2**: Items 10-19 execute in parallel (10 items)
4. Wait for batch 2 to complete
5. **Batch 3**: Items 20-24 execute in parallel (5 items)
6. Complete

**Why batching?**
- Prevents unbounded parallelism (e.g., 1000 items → 1000 concurrent agents)
- Controls memory usage and API rate limits
- Provides progress feedback between batches

**Setting `max_concurrent`:**
- **Default: 10** - Good balance for most use cases
- **Higher (20-50)**: Fast APIs, small items, high rate limits
- **Lower (3-5)**: Rate-limited APIs, large contexts, memory constraints
- **1**: Sequential processing (rarely needed)

## Failure Modes

For-each groups support three failure modes:

### fail_fast (Default)

Stop immediately when the first item fails. Remaining items are cancelled.

```yaml
for_each:
  - name: validators
    source: inputs.output.data
    as: item
    failure_mode: fail_fast
    agent:
      prompt: "Validate {{ item }}"
```

**Use case**: Critical validation where any failure should halt the workflow.

**Behavior:**
- First item failure → entire for-each fails immediately
- Items in the current batch may complete before cancellation
- No outputs are stored
- Exception is raised with error details

### continue_on_error

Continue processing all items. Workflow proceeds if **at least one item succeeds**.

```yaml
for_each:
  - name: fetchers
    source: sources.output.urls
    as: url
    failure_mode: continue_on_error
    agent:
      prompt: "Fetch data from {{ url }}"
```

**Use case**: Data gathering where partial success is acceptable (e.g., fetching from multiple sources).

**Behavior:**
- All items run to completion
- Successful outputs stored in `outputs`
- Failed items stored in `errors` with exception details
- Workflow continues if **any** item succeeded
- Raises error if **all** items fail

**Error structure:**
```json
{
  "fetchers": {
    "outputs": {
      "0": {"data": "..."},
      "2": {"data": "..."}
    },
    "errors": {
      "1": {
        "error": "TimeoutError",
        "message": "Request timed out",
        "index": 1,
        "item": "https://slow-api.com"
      }
    }
  }
}
```

### all_or_nothing

Process all items to completion. Fail if **any item fails**.

```yaml
for_each:
  - name: checks
    source: tasks.output.items
    as: task
    failure_mode: all_or_nothing
    agent:
      prompt: "Check {{ task }}"
```

**Use case**: Pre-deployment checks where all must pass but you want to see all failures.

**Behavior:**
- All items run to completion
- All outputs collected
- Raises error if any item failed (after all complete)
- Useful for seeing all failures, not just the first one

## Output Aggregation

After for-each execution completes, outputs are aggregated based on whether `key_by` is specified:

### Index-Based Outputs (Default)

When `key_by` is **not** specified, outputs are a **list** indexed by position:

```yaml
for_each:
  - name: processors
    source: finder.output.items
    as: item
    agent:
      output:
        result: { type: string }
```

**Output structure:**
```json
{
  "processors": {
    "outputs": [
      {"result": "processed item 0"},
      {"result": "processed item 1"},
      {"result": "processed item 2"}
    ],
    "errors": {}
  }
}
```

**Accessing in downstream agents:**
```yaml
agents:
  - name: aggregator
    prompt: |
      First result: {{ processors.outputs[0].result }}
      Second result: {{ processors.outputs[1].result }}
      
      All results:
      {% for result in processors.outputs %}
      - {{ result.result }}
      {% endfor %}
```

### Key-Based Outputs (With `key_by`)

When `key_by` is specified, outputs are a **dictionary** keyed by extracted values:

```yaml
for_each:
  - name: analyzers
    source: finder.output.kpis
    as: kpi
    key_by: kpi.kpi_id  # Extract kpi.kpi_id as key
    agent:
      output:
        analysis: { type: string }
```

**Output structure:**
```json
{
  "analyzers": {
    "outputs": {
      "KPI-123": {"analysis": "..."},
      "KPI-456": {"analysis": "..."},
      "KPI-789": {"analysis": "..."}
    },
    "errors": {}
  }
}
```

**Accessing in downstream agents:**
```yaml
agents:
  - name: aggregator
    prompt: |
      KPI-123 analysis: {{ analyzers.outputs["KPI-123"].analysis }}
      KPI-456 analysis: {{ analyzers.outputs["KPI-456"].analysis }}
      
      All analyses:
      {% for kpi_id, output in analyzers.outputs.items() %}
      {{ kpi_id }}: {{ output.analysis }}
      {% endfor %}
```

**Error structure with key_by:**

When using `key_by` with `continue_on_error` or `all_or_nothing` failure modes, errors are keyed using the same extracted keys (or indices if key extraction fails):

```json
{
  "analyzers": {
    "outputs": {
      "KPI-123": {"analysis": "..."},
      "KPI-789": {"analysis": "..."}
    },
    "errors": {
      "KPI-456": {
        "error": "ValidationError",
        "message": "Missing required field: metric",
        "index": 1,
        "key": "KPI-456"
      }
    }
  }
}
```

**Accessing errors in templates:**
```yaml
agents:
  - name: reporter
    prompt: |
      {% if analyzers.errors is defined and analyzers.errors %}
      Failed KPIs:
      {% for kpi_id, error in analyzers.errors.items() %}
      - {{ kpi_id }}: {{ error.message }}
      {% endfor %}
      {% endif %}
```

### Key Extraction Fallback

If key extraction fails for any item, it **falls back to index**:

```yaml
key_by: item.id  # Some items might not have 'id' field
```

**Behavior:**
- Items with valid keys: Use extracted key
- Items with missing/invalid keys: Use index (0, 1, 2, ...)
- Mixed dict: `{"key1": {...}, "0": {...}, "key2": {...}}`

**When keys conflict**, later items overwrite earlier ones.

## Empty Arrays

For-each groups handle empty arrays gracefully:

```yaml
source: finder.output.items  # Resolves to []
```

**Behavior:**
- For-each completes immediately
- No agent executions
- **Without `key_by`**: `outputs = []` (empty list)
- **With `key_by`**: `outputs = {}` (empty dict)
- No errors
- Routes are evaluated normally

Downstream agents can check for empty outputs:

```yaml
agents:
  - name: aggregator
    prompt: |
      {% if processors.outputs | length == 0 %}
      No items to process.
      {% else %}
      Processing {{ processors.outputs | length }} results...
      {% endif %}
```

## Context Isolation

Each for-each agent instance receives an **immutable context snapshot** plus injected loop variables:

```yaml
for_each:
  - name: processors
    source: finder.output.items
    as: item
    agent:
      input:
        - workflow.input.config
        - finder.output.metadata
      prompt: |
        Config: {{ workflow.input.config }}
        Metadata: {{ finder.output.metadata }}
        Current item: {{ item }}
        Index: {{ _index }}
```

**Context includes:**
- Workflow inputs (all inputs if using default context mode, or only declared inputs if using `context: mode: explicit`)
- Outputs from agents executed before the for-each group
- Injected loop variables: `{{ item }}`, `{{ _index }}`, `{{ _key }}`

**Context excludes:**
- Outputs from other items in the same for-each group
- Outputs from agents after the for-each group

**Note on workflow context modes:**
- By default, workflows use `accumulate` mode where all previous agent outputs are available
- You can use `context: mode: explicit` at the workflow level to require agents to declare their inputs
- With explicit mode, each for-each agent must list its inputs (as shown in the example above)
- Explicit mode can improve clarity and performance for workflows with many agents

This ensures:
- **No race conditions**: Items cannot interfere with each other
- **Deterministic behavior**: Results are consistent across runs
- **Clean isolation**: Each item processes independently

## Routing from For-Each Groups

For-each groups support routing just like regular agents:

```yaml
for_each:
  - name: processors
    source: finder.output.items
    as: item
    failure_mode: continue_on_error
    agent:
      # ... agent definition ...
    routes:
      - to: success_handler
        when: "{{ processors.outputs | length >= 5 }}"
      - to: partial_handler
        when: "{{ processors.outputs | length > 0 }}"
      - to: failure_handler
```

**Available in route conditions:**
- `{{ group_name.outputs }}` - Aggregated outputs
- `{{ group_name.errors }}` - Errors (if `continue_on_error` or `all_or_nothing`)
- All previous agent outputs

## Examples

### Example 1: KPI Analysis with Key-Based Outputs

```yaml
workflow:
  name: kpi-analysis
  entry_point: kpi_finder
  runtime:
    provider: copilot
    default_model: claude-sonnet-4.5

for_each:
  - name: kpi_analyzers
    type: for_each
    description: Analyze each KPI in parallel
    source: kpi_finder.output.kpis
    as: kpi
    max_concurrent: 5
    failure_mode: continue_on_error
    key_by: kpi.kpi_id
    
    agent:
      model: claude-opus-4.5
      prompt: |
        You are a KPI analyst. Analyze this KPI:
        
        KPI ID: {{ kpi.kpi_id }}
        KPI Name: {{ kpi.name }}
        Current Value: {{ kpi.value }}
        Target: {{ kpi.target }}
        
        Provide:
        1. Status assessment (on track, at risk, off track)
        2. Trend analysis
        3. Recommendations for improvement
      output:
        status:
          type: string
          description: "on track | at risk | off track"
        trend:
          type: string
        recommendations:
          type: array
        confidence:
          type: number
    
    routes:
      - to: aggregator

agents:
  - name: kpi_finder
    prompt: |
      Find all KPIs for Q4 2024.
      Return as a structured list with kpi_id, name, value, target.
    output:
      kpis:
        type: array
    routes:
      - to: kpi_analyzers
  
  - name: aggregator
    input:
      - kpi_finder.output
      - kpi_analyzers.outputs
      - kpi_analyzers.errors
    prompt: |
      Create an executive summary of KPI analysis results:
      
      Total KPIs: {{ kpi_finder.output.kpis | length }}
      Analyzed: {{ kpi_analyzers.outputs | length }}
      Failed: {{ kpi_analyzers.errors | length }}
      
      {% if kpi_analyzers.errors %}
      Failed KPIs:
      {% for kpi_id, error in kpi_analyzers.errors.items() %}
      - {{ kpi_id }}: {{ error.message }}
      {% endfor %}
      {% endif %}
      
      Successful Analyses:
      {% for kpi_id, analysis in kpi_analyzers.outputs.items() %}
      {{ kpi_id }}: {{ analysis.status }} - {{ analysis.trend }}
      {% endfor %}
      
      Provide:
      1. Overall health score
      2. Critical issues requiring immediate attention
      3. Positive trends to highlight
    output:
      summary:
        type: string
      health_score:
        type: number
    routes:
      - to: $end

output:
  summary: "{{ aggregator.output.summary }}"
  health_score: "{{ aggregator.output.health_score }}"
  total_kpis: "{{ kpi_finder.output.kpis | length }}"
  analyzed: "{{ kpi_analyzers.outputs | length }}"
```

### Example 2: Simple Data Processing

```yaml
workflow:
  name: batch-processor
  entry_point: data_loader
  runtime:
    provider: copilot

for_each:
  - name: item_processors
    type: for_each
    source: data_loader.output.items
    as: item
    max_concurrent: 10
    failure_mode: all_or_nothing
    
    agent:
      prompt: |
        Process this item:
        {{ item | json }}
        
        Extract and transform the data.
      output:
        processed_data:
          type: object
    
    routes:
      - to: $end

agents:
  - name: data_loader
    prompt: "Load the dataset from {{ workflow.input.source }}"
    output:
      items:
        type: array
    routes:
      - to: item_processors

output:
  results: "{{ item_processors.outputs | json }}"
  total_processed: "{{ item_processors.outputs | length }}"
```

### Example 3: Conditional Processing with Routing

```yaml
for_each:
  - name: validators
    type: for_each
    source: checker.output.items
    as: item
    failure_mode: continue_on_error
    
    agent:
      prompt: "Validate {{ item }}"
      output:
        valid: { type: boolean }
    
    routes:
      - to: success_path
        when: "{{ validators.outputs | length == (checker.output.items | length) }}"
      - to: partial_path
        when: "{{ validators.outputs | length > 0 }}"
      - to: failure_path
```

## Best Practices

### 1. Choose Appropriate `max_concurrent`

Consider your constraints:

```yaml
# High throughput, fast API
max_concurrent: 20

# Rate-limited API (e.g., 10 requests/second)
max_concurrent: 5

# Large context or memory constrained
max_concurrent: 3

# Need sequential processing (rare)
max_concurrent: 1
```

### 2. Use `key_by` for Stable Identifiers

When items have unique IDs, use `key_by` for clearer output access:

```yaml
# ✅ Good: Access by meaningful ID
key_by: kpi.kpi_id
# Access: {{ analyzers.outputs["KPI-123"] }}

# ❌ Avoid: Access by index requires knowing order
# Access: {{ analyzers.outputs[0] }}  # Which KPI is this?
```

### 3. Handle Partial Failures Gracefully

Use `continue_on_error` for resilient data gathering:

```yaml
for_each:
  - name: fetchers
    failure_mode: continue_on_error
    # ...

agents:
  - name: aggregator
    prompt: |
      {% if fetchers.errors %}
      Warning: {{ fetchers.errors | length }} sources failed
      {% endif %}
      
      Processing {{ fetchers.outputs | length }} successful results...
```

### 4. Validate Source Arrays

Ensure source arrays are well-formed:

```yaml
agents:
  - name: finder
    # Add validation to output schema
    output:
      items:
        type: array
        description: "Must be non-null array"
    # Check in prompt
    prompt: |
      Find items. Return as an array, even if empty: []
```

### 5. Keep Agent Templates Focused

Each for-each agent should do one specific task:

```yaml
# ✅ Good: Focused task
agent:
  prompt: "Analyze KPI {{ kpi.kpi_id }}"

# ❌ Avoid: Multiple unrelated tasks
agent:
  prompt: |
    Analyze {{ kpi.kpi_id }}
    Also check database consistency
    And send notifications
    And update the dashboard
```

### 6. Set Reasonable Batch Sizes

For large arrays (100+ items), consider breaking into multiple for-each groups:

```yaml
# Instead of processing 500 items in one for-each:
# agents: [splitter, batch_1, batch_2, batch_3, merger]

# Option 1: Use higher max_concurrent
for_each:
  - name: processors
    max_concurrent: 50  # Process 10 batches of 50

# Option 2: Split in workflow logic
agents:
  - name: splitter
    # Create batches
  - name: batch_processor_1
    # Process first batch
  - name: batch_processor_2
    # Process second batch
```

## Troubleshooting

### Source Array Not Found

**Error**: `"Array reference 'finder.output.items' not found in context"`

**Solution**: Ensure the source agent has completed and produced the expected output:

```yaml
# Check agent output schema
agents:
  - name: finder
    output:
      items:  # Must match source reference
        type: array
```

### Source Not an Array

**Error**: `"Source 'finder.output.result' resolved to <class 'str'>, expected list"`

**Solution**: Ensure the source path points to an array field:

```yaml
# ❌ Wrong: points to string field
source: finder.output.summary

# ✅ Correct: points to array field
source: finder.output.items
```

### Loop Variable Conflicts

**Error**: `"Loop variable 'workflow' conflicts with reserved name"`

**Solution**: Use a different variable name. Reserved: `workflow`, `context`, `output`, `_index`, `_key`

```yaml
# ❌ Invalid
as: workflow

# ✅ Valid
as: item
as: kpi
as: task
```

### Key Extraction Failures

**Issue**: Some items don't have the key field specified in `key_by`

**Behavior**: System automatically falls back to index

```yaml
key_by: item.id

# Item 0: has item.id="abc" → outputs["abc"]
# Item 1: missing item.id → outputs["1"]  (fallback)
# Item 2: has item.id="def" → outputs["def"]
```

**Solution**: If you want strict key extraction, validate in the finder agent:

```yaml
agents:
  - name: finder
    prompt: "Ensure every item has an 'id' field"
    output:
      items:
        type: array
        description: "Each item must have 'id' field"
```

### All Items Failing

**Error**: `"All items failed in for-each group 'processors'"`

**Solution**:
1. Check agent prompt is valid for the item structure
2. Verify items match expected schema
3. Test with a small sample first
4. Check verbose logs: `conductor run --verbose`

### Memory Issues with Large Arrays

**Problem**: Workflow uses too much memory with 1000+ items

**Solutions**:
1. Reduce `max_concurrent` to limit parallel executions
2. Break into multiple for-each groups
3. Use explicit context mode to reduce snapshot size
4. Consider pagination in the finder agent

```yaml
context:
  mode: explicit  # Reduce context size

for_each:
  - name: processors
    max_concurrent: 5  # Lower concurrency
```

## Limitations

1. **No nested for-each**: For-each groups cannot contain other for-each or parallel groups
2. **Source must be array**: Only list types are supported; dict iteration not supported
3. **Inline agent only**: Agents must be defined inline, cannot reference existing agents
4. **No dynamic `max_concurrent`**: Batch size is fixed at workflow definition time
5. **Key conflicts**: When using `key_by`, duplicate keys cause overwrites

## Performance Considerations

### Expected Speedup

For N items with `max_concurrent=M`:
- **Best case**: ~M× faster than sequential
- **Typical**: Depends on item processing time variance
- **Worst case**: Limited by slowest item in each batch

### Overhead

- Context snapshot per item (deep copy)
- Asyncio task scheduling
- Output aggregation

For large arrays (1000+ items), total overhead is typically <5% of execution time.

## Migration from Static Parallel

Converting static parallel to for-each:

**Before (static):**
```yaml
parallel:
  - name: researchers
    agents: [researcher_1, researcher_2, researcher_3]

agents:
  - name: researcher_1
    prompt: "Research {{ topics[0] }}"
  - name: researcher_2
    prompt: "Research {{ topics[1] }}"
  - name: researcher_3
    prompt: "Research {{ topics[2] }}"
```

**After (for-each):**
```yaml
for_each:
  - name: researchers
    type: for_each
    source: topic_finder.output.topics
    as: topic
    agent:
      prompt: "Research {{ topic }}"

agents:
  - name: topic_finder
    # Finds topics dynamically
```

## See Also

- [Workflow YAML Syntax](./workflow-syntax.md) - Complete syntax reference
- [Parallel Execution Guide](./parallel-execution.md) - Static parallel groups
- [Examples](../examples/) - Complete workflow examples
- [Context Management](../README.md#context-modes) - Understanding context modes
