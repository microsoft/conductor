---
name: conductor
description: Create, validate, and run workflows. Use when orchestrating AI agents via YAML workflow files, creating new workflows, debugging execution, configuring routing between agents, setting up human-in-the-loop gates, or understanding workflow YAML schema.
---

# Conductor

CLI tool for defining and running multi-agent workflows with the GitHub Copilot SDK or Anthropic Claude.

## Quick Reference

```bash
conductor run workflow.yaml --input question="Hello"     # Execute (full output by default)
conductor run workflow.yaml -q --input question="Hello"  # Quiet: lifecycle + routing only
conductor run workflow.yaml -s --input question="Hello"  # Silent: JSON result only
conductor run workflow.yaml --log-file auto               # Log full debug output to file
conductor validate workflow.yaml                         # Validate only
conductor init my-workflow --template simple              # Create from template
conductor templates                                      # List templates
conductor stop                                           # Stop background workflow
conductor update                                         # Check for and install latest version
conductor resume workflow.yaml                           # Resume from last checkpoint
conductor checkpoints                                    # List available checkpoints
```

Full output is shown by default. Use `-q` (quiet) for minimal output or `-s` (silent) for JSON-only.

## When to Use Each Guide

**Creating or modifying workflows?** → See [references/authoring.md](references/authoring.md)
- Agent definitions, prompts, and output schemas
- Routing patterns (linear, conditional, loop-back)
- Parallel and for-each groups
- Human gates
- Context modes and MCP servers
- Cost tracking configuration

**Running or debugging workflows?** → See [references/execution.md](references/execution.md)
- CLI options and flags (run, resume, checkpoints, stop, update)
- Debugging techniques
- Error troubleshooting
- Checkpoint/resume after failures
- Environment setup and providers

**Need complete YAML schema?** → See [references/yaml-schema.md](references/yaml-schema.md)
- All configuration fields with types and defaults
- Validation rules
- Type definitions

## Workflow Structure Overview

```yaml
workflow:
  name: my-workflow
  entry_point: first_agent
  runtime:
    provider: copilot               # or claude
    default_model: gpt-5.2
    temperature: 0.7
  input:
    question:
      type: string
  context:
    mode: accumulate                # accumulate, last_only, explicit
  limits:
    max_iterations: 10
  cost:
    show_summary: true

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
| `entry_point` | First agent/group to execute |
| `routes` | Where agent goes next (`$end` to finish, `self` to loop) |
| `type: script` | Shell command step (captures stdout, stderr, exit_code) |
| `parallel` | Static parallel groups (fixed agent list) |
| `for_each` | Dynamic parallel groups (runtime-determined array) |
| `human_gate` | Pauses for user decision with options |
| `!file` tag | Include external file content in YAML (`prompt: !file prompt.md`) |
| `context.mode` | How agents share data (accumulate, last_only, explicit) |
| `limits` | Safety bounds (max_iterations up to 500, timeout_seconds) |
| `cost` | Token usage and cost tracking configuration |
| `runtime` | Provider, model, temperature, max_tokens, MCP servers |
| checkpoint | Auto-saved on failure; resume with `conductor resume` |

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

**Parallel execution**:
```yaml
parallel:
  - name: researchers
    agents: [web_researcher, academic_researcher]
    failure_mode: continue_on_error
    routes:
      - to: synthesizer
```

**For-each (dynamic parallel)**:
```yaml
for_each:
  - name: processors
    type: for_each
    source: finder.output.topics
    as: item
    max_concurrent: 5
    agent:
      prompt: "Process {{ item }} (index: {{ _index }})"
      output:
        result: { type: string }
    routes:
      - to: aggregator
```

**Script step** (shell command):
```yaml
agents:
  - name: check_version
    type: script
    command: python3
    args: ["--version"]
    routes:
      - to: analyzer
        when: "exit_code == 0"
      - to: error_handler
```

**File include** (`!file` tag):
```yaml
agents:
  - name: analyzer
    system_prompt: !file prompts/system.md
    prompt: !file prompts/analyze.md
```

**Human gate**:
```yaml
- name: review
  type: human_gate
  prompt: "Review: {{ designer.output.summary }}"
  options:
    - label: Approve
      value: approved
      route: $end
    - label: Revise
      value: changes
      route: designer
      prompt_for: feedback
```

## Template Variables (Jinja2)

```jinja2
{{ workflow.input.param }}                  # Input parameter
{{ workflow.name }}                         # Workflow name
{{ agent_name.output.field }}               # Agent output
{{ output.field }}                          # Current output (in routes)
{{ group.outputs.agent.field }}             # Parallel group outputs
{{ group.outputs[0].field }}                # For-each outputs (index)
{{ group.outputs["key"].field }}            # For-each outputs (key_by)
{% if agent is defined %}...{% endif %}     # Conditional
{% for item in list %}...{% endfor %}       # Loop
{{ value | default("fallback") }}           # Filter
```

## Providers

| Provider | Auth | MCP Support |
|----------|------|-------------|
| `copilot` | `GITHUB_TOKEN` | ✅ Full |
| `claude` | `ANTHROPIC_API_KEY` | ✅ Full |

Per-agent provider override: `provider: claude` on any agent definition for multi-provider workflows.
