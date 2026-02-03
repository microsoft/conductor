# CLI Reference

Complete command-line reference for Conductor.

## Table of Contents

- [`conductor run`](#conductor-run)
- [`conductor validate`](#conductor-validate)
- [`conductor init`](#conductor-init)
- [`conductor templates`](#conductor-templates)

## `conductor run`

Execute a workflow from a YAML file.

```bash
conductor run <workflow.yaml> [OPTIONS]
```

### Options

| Option | Short | Description |
|--------|-------|-------------|
| `--input NAME=VALUE` | `-i` | Workflow input (repeatable) |
| `--input.NAME=VALUE` | | Alternative input syntax |
| `--provider PROVIDER` | `-p` | Override provider (copilot, claude) |
| `--dry-run` | | Show execution plan without running |
| `--skip-gates` | | Auto-select first option at human gates |
| `--verbose` | `-V` | Show detailed execution progress |

### Examples

#### Basic Execution

```bash
# Run with a single input
conductor run workflow.yaml --input question="What is AI?"

# Run with multiple inputs
conductor run workflow.yaml -i question="Hello" -i context="Greeting"

# Alternative input syntax
conductor run workflow.yaml --input.question="What is AI?"
```

#### Provider Override

```bash
# Override the workflow's default provider
conductor run workflow.yaml --provider claude

# Use Copilot instead of Claude
conductor run workflow.yaml -p copilot
```

#### Dry Run and Debugging

```bash
# Preview execution plan without running
conductor run workflow.yaml --dry-run

# Verbose output for debugging
conductor -V run workflow.yaml --input question="Test"

# Combine dry-run with verbose
conductor -V run workflow.yaml --dry-run
```

#### Automation Mode

```bash
# Skip human gates (auto-select first option)
conductor run workflow.yaml --skip-gates

# Useful for CI/CD pipelines
conductor run workflow.yaml --skip-gates --input question="Automated test"
```

#### Complex Inputs

```bash
# JSON array input
conductor run workflow.yaml --input items='["item1", "item2", "item3"]'

# JSON object input
conductor run workflow.yaml --input config='{"key": "value", "count": 5}'

# Multi-line input (use quotes)
conductor run workflow.yaml --input text="Line 1
Line 2
Line 3"
```

## `conductor validate`

Validate a workflow file without executing it. Checks YAML syntax, schema compliance, and cross-references (agent names, routes, parallel groups).

```bash
conductor validate <workflow.yaml>
```

### Examples

```bash
# Validate a single workflow
conductor validate my-workflow.yaml

# Validate with full path
conductor validate ./workflows/production/main.yaml

# Validate all examples (using shell expansion)
for f in examples/*.yaml; do conductor validate "$f"; done
```

### Validation Checks

- YAML syntax errors
- Schema compliance (required fields, types)
- Agent name references in routes
- Parallel group agent references
- For-each source references
- Circular dependency detection
- Input/output schema validation

## `conductor init`

Create a new workflow file from a template.

```bash
conductor init <name> [OPTIONS]
```

### Options

| Option | Short | Description |
|--------|-------|-------------|
| `--template TEMPLATE` | `-t` | Template to use (default: simple) |
| `--output PATH` | `-o` | Output file path |

### Examples

```bash
# Create a simple workflow (default template)
conductor init my-workflow

# Create from a specific template
conductor init my-workflow --template loop

# Specify output path
conductor init my-workflow -t human-gate -o ./workflows/review.yaml

# Create a parallel workflow
conductor init research --template parallel -o research.yaml
```

### Available Templates

Use `conductor templates` to see the full list. Common templates:

- `simple` - Single agent, basic Q&A (default)
- `loop` - Agent with loop-back pattern
- `parallel` - Static parallel execution
- `human-gate` - Workflow with human approval gate
- `for-each` - Dynamic parallel processing

## `conductor templates`

List available workflow templates with descriptions.

```bash
conductor templates
```

### Example Output

```
Available templates:

  simple      Single agent workflow for basic Q&A
  loop        Agent with conditional loop-back
  parallel    Static parallel agent execution
  human-gate  Workflow with human approval gate
  for-each    Dynamic parallel (for-each) processing

Use: conductor init <name> --template <template>
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | API key for Claude provider |
| `GITHUB_TOKEN` | Token for Copilot provider (if not using GitHub CLI auth) |
| `CONDUCTOR_LOG_LEVEL` | Logging level: DEBUG, INFO, WARNING, ERROR |

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Workflow execution error |
| 2 | Validation error |
| 3 | Configuration error |
| 130 | User interrupt (Ctrl+C) |

## See Also

- [Workflow Syntax Reference](./workflow-syntax.md) - Complete YAML syntax
- [Examples](../examples/) - Example workflows
- [Providers](./providers/) - Provider-specific documentation
