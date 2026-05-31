# Conductor Workflow Examples

Complete, runnable example workflows demonstrating key orchestration patterns.

## Examples

| Example | Topology | Techniques | When to use |
|---------|----------|------------|-------------|
| **implement-and-review.yaml** | Loop | Loop-back routing, structured output, iteration limits | Implement → review → fix cycles |
| **review-branch.yaml** | Fan-out + verify | Parallel groups, for_each, multi-model (Haiku for verification) | Pre-PR review across dimensions |
| **dead-code-sweep.yaml** | Loop-until-dry | Accumulate context, for_each with test validation | Discovery with unknown count |

## Running the examples

```bash
# Implement and review
conductor run implement-and-review.yaml --input task="add rate limiting"

# Review branch
conductor run review-branch.yaml

# Dead code sweep
conductor run dead-code-sweep.yaml
```

## Pattern mapping

### implement-and-review.yaml

**Pattern:** Loop-until-pass (Pattern #4 in `references/patterns.md`)

**Key techniques:**
- Loop-back routing: `fixer` routes back to `reviewer`
- Structured output: `reviewer` returns `{ passed, issues }`
- Iteration limit: `max_iterations: 10` prevents infinite loops
- Conditional routing: `when: "{{ not output.passed }}"`

**Flow:**
```
implementer → reviewer → [passed?] → $end
                ↓ [not passed]
              fixer → reviewer (loop)
```

### review-branch.yaml

**Pattern:** Fan-out + adversarial verification (Patterns #1 + #6)

**Key techniques:**
- Parallel group: 3 reviewers run concurrently
- For-each groups: Verify each finding independently
- Multi-model: Haiku for cheap verification
- Structured output: Findings schema enforced

**Flow:**
```
[review-bugs, review-security, review-tests] (parallel)
    ↓
[verify each finding] (for_each with Haiku)
    ↓
$end (aggregated confirmed findings)
```

### dead-code-sweep.yaml

**Pattern:** Loop-until-dry (Pattern #8 in `references/patterns.md`)

**Key techniques:**
- Accumulate context: Each round sees previous findings
- For-each with validation: Remove + test each symbol
- Loop-back: `remove-group` routes back to `finder`
- Iteration limit: Caps the discovery loop

**Flow:**
```
finder → [items found?] → remove-group (for_each)
   ↑                           ↓
   └───────────────────────────┘ (loop)
```

## Adapting these examples

### Change the model

```yaml
runtime:
  default_model: claude-sonnet-4.5  # Change here

# Or per-agent
agents:
  - name: reviewer
    model: gpt-4o  # Override for this agent
```

### Change concurrency

```yaml
for_each:
  - name: process-group
    max_concurrent: 8  # Default: 4
```

### Change failure handling

```yaml
parallel:
  - name: review-group
    failure_mode: fail_fast  # or continue_on_error, all_or_nothing
```

### Add checkpointing

Checkpoints are automatic on failure. Resume with:

```bash
conductor resume implement-and-review.yaml
```

## Next steps

1. **Read the patterns**: `references/patterns.md` has 10 copy-paste patterns
2. **Read the API reference**: `references/api-reference.md` for complete YAML schema
3. **Start from a template**: `assets/templates/` has starter files for fan-out, pipeline, loop
4. **Use the skill**: Ask Claude to "create a workflow for X" and it will use this skill
