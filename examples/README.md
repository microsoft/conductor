# Conductor Examples

This directory contains example workflow files demonstrating various features of Conductor.

## Quick Start Examples

### simple-qa.yaml

A minimal workflow with a single agent that answers questions. Demonstrates:
- Basic workflow structure
- Input parameters
- Output schema validation
- Simple routing to `$end`

```bash
conductor run examples/simple-qa.yaml --input question="What is Python?"
```

### simple-qa-claude.yaml

The same simple Q&A workflow configured for Claude provider. Demonstrates:
- Claude provider configuration
- Model selection with `claude-sonnet-4.5`

```bash
export ANTHROPIC_API_KEY=sk-ant-...
conductor run examples/simple-qa-claude.yaml --input question="What is Python?"
```

## Parallel Execution Examples

### for-each-simple.yaml

Dynamic parallel processing with for-each groups. Demonstrates:
- For-each group definition
- Processing variable-length arrays
- Loop variable access (`{{ item }}`, `{{ _index }}`)
- Aggregating parallel outputs

```bash
conductor run examples/for-each-simple.yaml --input items='["apple", "banana", "cherry"]'
```

### parallel-research.yaml

Parallel research from multiple sources. Demonstrates:
- Static parallel groups
- Multiple specialized research agents
- Result aggregation from parallel outputs

```bash
conductor run examples/parallel-research.yaml --input topic="Renewable energy"
```

### parallel-validation.yaml

Parallel code validation checks. Demonstrates:
- Static parallel groups for concurrent validation
- Multiple validation agents (security, performance, style)
- Failure mode configuration

```bash
conductor run examples/parallel-validation.yaml --input code="def hello(): print('world')"
```

## Human-in-the-Loop Examples

### design-review.yaml

An iterative design workflow with human approval. Demonstrates:
- Multiple agents with conditional routing
- Loop-back patterns for refinement
- Human gates for approval decisions
- Context accumulation between iterations
- Safety limits (max_iterations, timeout)

```bash
# Interactive mode
conductor run examples/design-review.yaml --input requirement="Build a REST API"

# Automation mode (auto-approves)
conductor run examples/design-review.yaml --input requirement="Build a REST API" --skip-gates
```

## Multi-Agent Workflows

### research-assistant.yaml

A multi-agent research workflow with tools. Demonstrates:
- Multiple specialized agents
- Tool configuration at workflow and agent levels
- Explicit context mode
- Conditional routing based on coverage
- Complex output schemas

```bash
conductor run examples/research-assistant.yaml --input topic="AI in healthcare"

# With custom depth
conductor run examples/research-assistant.yaml --input topic="Quantum computing" --input depth="comprehensive"
```

### research-assistant-claude.yaml

The research assistant configured for Claude provider. Demonstrates:
- Claude provider with multi-agent workflow
- Model selection per agent

```bash
export ANTHROPIC_API_KEY=sk-ant-...
conductor run examples/research-assistant-claude.yaml --input topic="Machine learning"
```

### multi-provider-research.yaml

Research workflow demonstrating multi-provider patterns. Demonstrates:
- Provider configuration options
- Cross-provider workflow patterns

```bash
conductor run examples/multi-provider-research.yaml --input topic="Cloud computing"
```

## Planning and Implementation

### implementation-plan.yaml

An implementation planning workflow with architect and reviewer agents. Demonstrates:
- Architect agent for creating detailed implementation plans
- Reviewer agent for plan quality assessment
- Loop-back pattern until quality threshold is met
- Structured output with epics, tasks, and file changes
- Traceability between requirements and implementation tasks

```bash
conductor run examples/implementation-plan.yaml --input design="Build a REST API with CRUD operations for users"

# With verbose output
conductor -V run examples/implementation-plan.yaml --input design="./docs/my-feature.design.md"
```

## Running Examples

### Prerequisites

1. Install Conductor:
   ```bash
   uvx conductor
   ```

2. Ensure you have valid credentials for your provider:
   - **Copilot**: GitHub authentication via `gh auth login`
   - **Claude**: Set `ANTHROPIC_API_KEY` environment variable

### Validate Before Running

You can validate a workflow without executing it:

```bash
conductor validate examples/simple-qa.yaml
```

### Dry Run

Preview the execution plan without actually running the workflow:

```bash
conductor run examples/simple-qa.yaml --dry-run
```

### Verbose Mode

See detailed execution progress:

```bash
conductor -V run examples/simple-qa.yaml --input question="Hello"
```

## Creating Your Own Workflows

Use the `init` command to create a new workflow from a template:

```bash
# List available templates
conductor templates

# Create from a template
conductor init my-workflow --template loop
```

## Tips

1. **Start simple**: Begin with a linear workflow and add complexity incrementally.

2. **Validate often**: Use `conductor validate` to catch configuration errors early.

3. **Use dry-run**: Preview execution with `--dry-run` before running expensive workflows.

4. **Explicit context**: Use `context.mode: explicit` for complex workflows to control exactly what context each agent sees.

5. **Safety limits**: Always set appropriate `max_iterations` and `timeout_seconds` for workflows with loops.

6. **Optional dependencies**: Use `?` suffix for optional input references to avoid errors when agents haven't run yet.

## See Also

- [Workflow Syntax Reference](../docs/workflow-syntax.md) - Complete YAML schema
- [CLI Reference](../docs/cli-reference.md) - Full command documentation
- [Parallel Execution](../docs/parallel-execution.md) - Static parallel groups
- [Dynamic Parallel](../docs/dynamic-parallel.md) - For-each groups
