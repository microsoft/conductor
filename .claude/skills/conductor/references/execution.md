# Workflow Execution Guide

Complete reference for running, validating, and debugging Conductor workflows.

**Best Practice:** Always use `-V` (verbose) mode when running workflows to see execution progress.

## CLI Commands

### conductor run

Execute a workflow:

```bash
conductor -V run <workflow.yaml> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--input`, `-i NAME=VALUE` | Workflow input (repeatable) |
| `--input.NAME=VALUE` | Alternative input syntax |
| `--provider`, `-p PROVIDER` | Override provider |
| `--dry-run` | Show execution plan only |
| `--skip-gates` | Auto-select first option at human gates |
| `--verbose`, `-V` | Show detailed progress (**recommended**) |

**Examples:**

```bash
# Standard run (always use -V)
conductor -V run workflow.yaml --input question="Hello"

# Multiple inputs
conductor -V run workflow.yaml -i topic="AI" -i depth="detailed"

# Skip human gates for automation
conductor -V run workflow.yaml --skip-gates

# Dry run to preview
conductor run workflow.yaml --dry-run
```

### conductor validate

Validate without executing:

```bash
conductor validate <workflow.yaml>
```

Checks:
- YAML syntax
- Required fields
- Agent references
- Route reachability
- Template syntax

### conductor init

Create workflow from template:

```bash
conductor init <name> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--template`, `-t TEMPLATE` | Template to use (default: simple) |
| `--output`, `-o PATH` | Output file path |

**Examples:**

```bash
conductor init my-workflow
conductor init my-workflow --template loop
conductor init review -t human-gate -o ./workflows/review.yaml
```

### conductor templates

List available templates:

```bash
conductor templates
```

| Template | Description |
|----------|-------------|
| `simple` | Single agent with basic I/O |
| `loop` | Iterative refinement pattern |
| `human-gate` | Workflow with approval gate |

## Execution Flow

1. **Load** - Parse YAML and validate structure
2. **Initialize** - Set up provider and MCP servers
3. **Execute** - Run agents following routes
4. **Collect** - Gather outputs per schema
5. **Return** - Output final result as JSON

## Debugging

### Use Verbose Mode

```bash
conductor -V run workflow.yaml --input question="test"
```

Shows:
- Agent execution order
- Prompt content sent
- Output received
- Route decisions

### Dry Run

```bash
conductor run workflow.yaml --dry-run
```

Preview execution plan without running agents.

### Validate First

```bash
conductor validate workflow.yaml
```

Catch configuration errors before execution.

### Check Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Workflow error |
| 2 | Validation error |
| 3 | Timeout |
| 4 | Max iterations exceeded |

## Common Errors

### "Missing required input"

```
Error: Missing required input: question
```

**Fix:** Provide all required inputs:
```bash
conductor run workflow.yaml --input question="value"
```

### "Unknown agent: X"

```
Error: Route references unknown agent: reviewer
```

**Fix:** Check agent name spelling matches in routes.

### "Unreachable agent"

```
Error: Agent 'helper' is not reachable from entry_point
```

**Fix:** Add route to the agent or remove if unused.

### "Max iterations exceeded"

```
Error: Workflow exceeded max_iterations (10)
```

**Fix:** Increase limit or fix loop condition:
```yaml
limits:
  max_iterations: 20
```

### "Timeout"

```
Error: Workflow timed out after 600 seconds
```

**Fix:** Increase timeout:
```yaml
limits:
  timeout_seconds: 1200
```

### Template Errors

```
Error: Undefined variable 'agent_name' in template
```

**Fix:** Check variable exists or use conditional:
```jinja2
{% if agent_name is defined %}
{{ agent_name.output.field }}
{% endif %}
```

## Human Gates

When workflow reaches a human gate:

1. **Display** - Shows prompt and options in terminal
2. **Wait** - Pauses for user selection
3. **Capture** - Records decision and optional feedback
4. **Route** - Continues based on selection

### Skip Gates for Automation

```bash
conductor run workflow.yaml --skip-gates
```

Auto-selects the first option at each gate.

## Provider Options

Override the default provider:

```bash
conductor run workflow.yaml -p claude       # Use Claude
conductor run workflow.yaml -p openai-agents # Use OpenAI
```

## Output Handling

Workflow output is JSON:

```json
{
  "answer": "Python is a programming language...",
  "confidence": 0.95
}
```

### Capture Output

```bash
# Save to file
conductor run workflow.yaml --input q="test" > output.json

# Parse with jq
conductor run workflow.yaml --input q="test" | jq '.answer'
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GITHUB_TOKEN` | GitHub Copilot authentication |
| `ANTHROPIC_API_KEY` | Claude provider API key |
| `OPENAI_API_KEY` | OpenAI provider API key |
| `CONDUCTOR_LOG_LEVEL` | Logging level (debug, info, warning, error) |

## Performance Tips

1. **Use appropriate models** - Smaller models for simple tasks
2. **Limit context** - Use `explicit` mode to reduce tokens
3. **Set timeouts** - Prevent runaway workflows
4. **Batch inputs** - Process multiple items in one agent when possible

## Debugging Checklist

1. [ ] Run `conductor validate workflow.yaml`
2. [ ] Check all agent names match between definition and routes
3. [ ] Verify entry_point exists
4. [ ] Ensure all paths lead to `$end`
5. [ ] Test with `--dry-run` first
6. [ ] Use `--verbose` to trace execution
7. [ ] Check template variables are defined before use
