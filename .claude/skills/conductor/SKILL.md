---
name: conductor
description: Validate, run, and execute workflows; creating new workflows when explicitly asked. Use when orchestrating AI agents via YAML workflow files, executing an existing workflow, debugging execution, configuring routing between agents, setting up human-in-the-loop gates, or understanding workflow YAML schema. Only create new workflows when explicitly asked.
---

# Conductor

CLI tool for defining and running multi-agent workflows with the GitHub Copilot SDK or Anthropic Claude.

> **DO NOT create new workflow files unless the user explicitly asks you to create one.** Default to running, validating, or debugging existing workflows. If the user's request is ambiguous, assume they want to run or modify an existing workflow rather than create a new one.

## Error Handling

> **CRITICAL — do NOT improvise workarounds.** If `conductor` fails for any reason (installation failure, provider error, missing dependency, platform bug), you MUST:
>
> 1. **Report the exact error** to the user — include the full error message.
> 2. **Stop and wait for user direction.** Do NOT attempt to simulate, replicate, or approximate the multi-agent workflow yourself. The value of this skill is the structured multi-agent orchestration — a single-agent attempt is not an equivalent substitute.

## Setup

Conductor is installed automatically when needed. If a `conductor` command fails with "command not found", install it. For installation instructions, see [references/setup.md](references/setup.md).

## Quick Reference

```bash
conductor run workflow.yaml --input question="Hello"     # Execute (full output by default)
conductor run workflow.yaml -q --input question="Hello"  # Quiet: lifecycle + routing only
conductor run workflow.yaml -s --input question="Hello"  # Silent: JSON result only
conductor run workflow.yaml --log-file auto               # Log full debug output to file
conductor run workflow.yaml --web --input q="Hello"       # Real-time web dashboard
conductor run workflow.yaml --web-bg --input q="Hello"    # Background mode (prints URL, exits)
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

## Minimal Workflow Example

```yaml
workflow:
  name: my-workflow
  entry_point: answerer
  input:
    question: { type: string }

agents:
  - name: answerer
    prompt: "Answer: {{ workflow.input.question }}"
    output:
      answer: { type: string }
    routes:
      - to: $end

output:
  answer: "{{ answerer.output.answer }}"
```

For runtime config, context modes, limits, and cost tracking, see [references/authoring.md](references/authoring.md).

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
| `--web` | Real-time web dashboard with DAG graph, live streaming, in-browser human gates |
| `checkpoint` | Auto-saved on failure; resume with `conductor resume` |

For pattern examples (linear, loop, conditional, parallel, for-each, human gate) and template syntax, see [references/authoring.md](references/authoring.md).
