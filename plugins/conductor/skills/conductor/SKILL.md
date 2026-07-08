---
name: conductor
description: Validate, run, and execute workflows; creating new workflows when explicitly asked. Use when orchestrating AI agents via YAML workflow files, executing an existing workflow, debugging execution, configuring routing between agents, setting up human-in-the-loop gates, or understanding workflow YAML schema. Only create new workflows when explicitly asked.
---

# Conductor

CLI tool for defining and running multi-agent workflows with the GitHub Copilot SDK, Anthropic Claude, Claude Agent SDK, or Hermes (NousResearch, experimental).

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
conductor -q run workflow.yaml --input question="Hello"  # Quiet: lifecycle + routing only
conductor -s run workflow.yaml --input question="Hello"  # Silent: JSON result only
conductor run workflow.yaml --log-file auto               # Log full debug output to file
conductor run workflow.yaml --web --input q="Hello"       # Real-time web dashboard
conductor run workflow.yaml --web-bg --input q="Hello"    # Background mode (prints URL, exits)
conductor run workflow.yaml -m tracker=ado -m work_item=42 # Attach metadata (merged on top of YAML)
conductor run workflow.yaml --workspace-instructions     # Auto-discover AGENTS.md / CLAUDE.md / etc.
conductor validate workflow.yaml                         # Validate (schema + semantic checks)
conductor show workflow.yaml                             # Show inputs, agents, and outputs
conductor replay events.jsonl                            # Replay a recorded run in the dashboard
conductor registry add official myorg/workflows --default # Add a registry (--type github|path)
conductor registry list official                          # List registry workflows
conductor run qa-bot@official@1.0.0 --input q="Hello"    # Run from registry
conductor stop                                           # Stop background workflow
conductor update                                         # Check + print install command
conductor update --apply                                 # Check + launch installer (then exit)
conductor resume workflow.yaml                           # Resume from last checkpoint
conductor resume workflow.yaml --web                     # Resume with dashboard (run-flag parity)
conductor checkpoint list                                # List available checkpoints
```

Full output is shown by default. Use `-q` (quiet) for minimal output or `-s` (silent) for JSON-only — both are root-level options and must come *before* the subcommand (`conductor -q run ...`, not `conductor run ... -q`).

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
| `type: script` | Shell command step (captures stdout, stderr, exit_code; JSON stdout is auto-merged) |
| `type: set` | Pure-context step that evaluates Jinja2 expressions and binds typed values (no LLM, no subprocess); supports single `value:` and multi `values:` |
| `type: wait` | Pause via `asyncio.sleep` (cross-platform); duration accepts `Ns/Nm/Nh/Nms` or Jinja2; composes with route loop-backs for polling |
| `type: workflow` | Sub-workflow agent — runs another YAML file as a black box (supports `input_mapping`, `max_depth`) |
| `type: terminate` | Explicit terminal step with `status` (`success`/`failed`), Jinja `reason`, optional `output_template` — controls CLI exit code, dashboard state, and emits `is_explicit: true` in `workflow_completed`/`workflow_failed` |
| `parallel` | Static parallel groups (fixed agent list) |
| `for_each` | Dynamic parallel groups (runtime-determined array; supports `type: workflow` agents) |
| `human_gate` | Pauses for user decision with options (Markdown + auto-linkified paths/URLs) |
| `dialog` | Per-agent trigger for conditional multi-turn conversation with the user |
| `retry` | Per-agent retry policy for transient `provider_error` / `timeout` failures |
| `hooks` | Workflow lifecycle expressions: `on_start`, `on_complete`, `on_error` |
| `metadata` | Arbitrary YAML or `--metadata`/`-m` key-values surfaced in `workflow_started` events |
| `instructions` | Workflow-level workspace context (inline strings or `!file` includes) prepended to every prompt |
| `--workspace-instructions` | CLI flag to auto-discover AGENTS.md / CLAUDE.md / `.github/copilot-instructions.md` / `.github/instructions/**/*.instructions.md` (only `applyTo: "**"` files) |
| `!file` tag | Include external file content in YAML (`prompt: !file prompt.md`) |
| `context.mode` | How agents share data (accumulate, last_only, explicit) |
| `limits` | Safety bounds (max_iterations up to 500, timeout_seconds) |
| `timeout_seconds` (agent) | Hard wall-clock cancellation per agent (provider-backed agents only) |
| `cost` | Token usage and cost tracking configuration |
| `runtime` | Provider (`copilot`, `claude`, `claude-agent-sdk`, `hermes`, `openai-agents`), model, temperature, max_tokens, reasoning effort, MCP servers |
| `--web` | Real-time web dashboard with DAG graph, live streaming, in-browser human gates, sub-workflow dive-in, replay |
| `checkpoint` | Auto-saved on failure; resume with `conductor resume` (run-flag parity: `--provider`, `--metadata`, `--web`, `--web-bg`, `--web-port`) |
| `registry` | Named workflow sources (GitHub repo or local dir); refs accept `name@registry@version` and `workflow#ref` (tag/branch/SHA) |

For pattern examples (linear, loop, conditional, parallel, for-each, human gate) and template syntax, see [references/authoring.md](references/authoring.md).
