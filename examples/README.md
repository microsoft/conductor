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

## Set Step Examples

### set-step.yaml

Derive named values from Jinja2 expressions without spending an LLM call. Demonstrates:

- Single `value:` binding (scalar output accessible as `step.output`)
- Multi `values:` binding (dict output accessible as `step.output.<key>`)
- Conditional routing on a derived boolean (`when: "{{ output.is_breaking }}"`)
- Combining derived values with downstream script-step consumers

```bash
# Breaking-change branch
conductor run examples/set-step.yaml \
  --input org=microsoft --input repo=conductor --input severity=high

# Safe-change branch with a custom model
conductor run examples/set-step.yaml \
  --input org=acme --input repo=widget --input severity=low \
  --input model=claude-haiku-4.5
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

## Reasoning Effort

### reasoning-effort.yaml

A two-stage workflow that demonstrates configuring model reasoning / extended-thinking effort. Demonstrates:
- Workflow-wide default via `runtime.default_reasoning_effort`
- Per-agent override via `reasoning.effort` (wins over the default)
- Conditional routing on a structured boolean output
- The unified field translates to each provider's native API: `reasoning_effort` on the Copilot session, or extended `thinking` budget on Claude

```bash
conductor run examples/reasoning-effort.yaml \
  --input topic="how the Raft consensus algorithm handles leader election"
```

See [Reasoning Effort](../docs/configuration.md#reasoning-effort) for the per-provider translation, supported models, and validation rules.

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

## Step Types

### script-step.yaml

Script step with shell command, JSON output parsing, and `exit_code`-based routing.
Demonstrates:
- `type: script` agents (cross-platform shell command execution)
- Capturing stdout/stderr/exit_code
- Routing on `exit_code` (`when: "exit_code == 0"`)
- Passing script output to downstream LLM agents

```bash
conductor run examples/script-step.yaml
```

### wait-step.yaml

Polling pattern with a wait step and a routing loop-back. Demonstrates:
- `type: wait` agents (pure `asyncio.sleep`, cross-platform — no shell `sleep` dependency)
- Templated `duration` (`"{{ workflow.input.poll_interval_seconds }}s"`)
- Loop-back from wait → script for polling
- `reason` field surfaced in the dashboard

```bash
conductor run examples/wait-step.yaml \
  --input poll_interval_seconds=2 --input max_attempts=3
```

### wait-smoke.yaml

Minimal wait-only workflow — no LLM, no scripts, no provider required.
Useful as a smoke test for installation, dashboard rendering, and
interrupt/timeout behavior. Three sequential wait steps demonstrate
the three duration syntaxes (suffixed string, templated, plain numeric).

```bash
conductor run examples/wait-smoke.yaml

# Watch wait nodes animate in the dashboard:
conductor run examples/wait-smoke.yaml --web

# Trigger the workflow timeout cancelling an in-flight wait:
conductor run examples/wait-smoke.yaml --input middle_duration_ms=10000
```

## Planning and Implementation

### plan.yaml

A comprehensive design and implementation planning workflow. Demonstrates:
- Architect agent for creating solution designs with implementation plans
- Reviewer agent for quality assessment of design and actionability
- Loop-back pattern until quality threshold is met (score >= 85)
- Structured output following design document and implementation plan templates
- MCP server integration (web-search, context7) for research
- Traceability between requirements and implementation tasks

```bash
conductor run examples/plan.yaml --input purpose="Build a user authentication system with OAuth2"

# Resume from existing plan
conductor run examples/plan.yaml --input purpose="..." --input existing_plan="path/to/plan.md"

# With verbose output
conductor -V run examples/plan.yaml --input purpose="..."
```

### implement.yaml

An epic-based implementation workflow with multi-tier review. Demonstrates:
- Coder agent (Opus 4.5) for deep research, analysis, and implementation
- Epic reviewer agent (Opus 4.5) for per-epic quality assessment
- Committer agent (Sonnet) for git commits and plan updates
- Plan reviewer agent (Opus 4.5) for holistic review of all changes
- Fixer agent (Opus 4.5) for addressing plan-level issues
- Iterative epic-by-epic implementation with automatic plan tracking
- Two-tier review: epic-level (fast) and plan-level (thorough)

```bash
conductor run examples/implement.yaml --input plan="path/to/implementation.plan.md"

# With specific epic
conductor run examples/implement.yaml --input plan="..." --input epic="EPIC-001"

# With verbose output
conductor -V run examples/implement.yaml --input plan="..."
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

## Web Dashboard

### web-dashboard-test.yaml

A multi-pattern workflow for testing the web dashboard. Demonstrates:
- Real-time DAG visualization with live node state updates
- Agent detail panel with streaming reasoning and tool calls
- Sequential, parallel, and script step patterns in a single workflow

```bash
# Foreground dashboard (keeps running after workflow completes)
conductor run examples/web-dashboard-test.yaml --web --input topic="Python async programming"

# Background mode (prints URL and exits immediately)
conductor run examples/web-dashboard-test.yaml --web-bg --input topic="Rust vs Go"
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
