---
name: conductor
description: Create, validate, and run workflows. Use when orchestrating AI agents via YAML workflow files, creating new workflows, debugging execution, configuring routing between agents, setting up human-in-the-loop gates, or understanding workflow YAML schema.
---

# Conductor

CLI tool for defining and running workflows.

## Quick Reference

```bash
conductor -V run workflow.yaml --input question="Hello"  # Execute (verbose)
conductor validate workflow.yaml                          # Validate only
conductor init my-workflow --template simple              # Create from template
```

**Always use `-V` (verbose) mode** to see agent execution progress and debug issues.

## When to Use Each Guide

**Creating or modifying workflows?** → See [references/authoring.md](references/authoring.md)
- Agent definitions and prompts
- Routing patterns
- Human gates
- Context modes

**Running or debugging workflows?** → See [references/execution.md](references/execution.md)
- CLI options
- Debugging techniques
- Error troubleshooting
- Environment setup

**Need complete YAML schema?** → See [references/yaml-schema.md](references/yaml-schema.md)
- All configuration fields
- Type definitions
- Validation rules

## Workflow Structure Overview

```yaml
workflow:
  name: my-workflow
  entry_point: first_agent
  input:
    question:
      type: string

agents:
  - name: first_agent
    prompt: |
      Answer: {{ workflow.input.question }}
    output:
      answer:
        type: string
    routes:
      - to: $end

output:
  answer: "{{ first_agent.output.answer }}"
```

## Key Concepts

| Concept | Description |
|---------|-------------|
| `entry_point` | First agent to execute |
| `routes` | Where agent goes next (`$end` to finish) |
| `human_gate` | Pauses for user decision |
| `context.mode` | How agents share data (accumulate, last_only, explicit) |
| `limits` | Safety bounds (max_iterations, timeout) |

## Common Patterns

**Linear pipeline**: `agent1 → agent2 → agent3 → $end`

**Loop until quality**: 
```yaml
routes:
  - to: $end
    when: "{{ output.score >= 90 }}"
  - to: self
```

**Conditional branching**:
```yaml
routes:
  - to: success_path
    when: "{{ output.approved }}"
  - to: failure_path
```

## Template Variables (Jinja2)

```jinja2
{{ workflow.input.param }}           # Input parameter
{{ agent_name.output.field }}        # Agent output
{{ output.field }}                   # Current output (in routes)
{% if agent is defined %}...{% endif %}  # Conditional
```
