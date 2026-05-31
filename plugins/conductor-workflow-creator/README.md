# conductor-workflow-creator

A skill that teaches Claude to author **Conductor workflows**: deterministic
multi-agent orchestration defined in YAML with explicit routing, parallel
execution, and dynamic iteration.

A Conductor workflow is a YAML file. The agents, the routing logic, and the
context modes are all declared up front. The engine evaluates routes in order
(first matching `when` wins), executes agents sequentially or in parallel, and
builds context according to the configured mode. The result is multi-agent work
that behaves the same way every run and can be resumed from checkpoints if it
fails partway.

This skill carries the YAML format, the design decisions, and a tested authoring
procedure, so you can just ask Claude to "create a workflow for X" and get a
correct, runnable file back.

## What is in this repo

The repo root is the skill itself. Drop it into your Claude Code or Copilot CLI
skills folder and Claude picks it up automatically.

| Path | What it is |
|------|------------|
| `SKILL.md` | The skill entry point: the procedure Claude follows to design and write a workflow. |
| `references/api-reference.md` | The complete manual: every YAML field, every context mode, every limit. |
| `references/patterns.md` | Copy-paste orchestration patterns (fan-out, pipeline, loop-until-pass, judge panel, and more). |
| `assets/templates/` | Starter files for the three core shapes: fan-out, pipeline, loop. |
| `assets/examples/` | Three complete, runnable example workflows, with a README mapping each one to a technique. |

## Install

### For Claude Code

Copy the skill into your Claude Code skills folder:

```bash
cd /path/to/conductor
mkdir -p ~/.claude/skills
cp -R plugins/conductor-workflow-creator ~/.claude/skills/conductor-workflow-creator
```

That is all. The next time Claude Code starts, the skill is available. Ask Claude
to "create a conductor workflow" and it will trigger.

### For GitHub Copilot CLI

Copy the skill into your Copilot skills folder:

```bash
cd /path/to/conductor
mkdir -p ~/.copilot/skills
cp -R plugins/conductor-workflow-creator ~/.copilot/skills/conductor-workflow-creator
```

## Using it

1. Ensure Conductor is installed:

   ```bash
   conductor --version
   ```

   If not installed, follow the [Conductor installation guide](../../README.md).

2. Ask Claude to build one, for example: "create a conductor workflow that
   reviews my branch across bugs, security, and tests, then verifies each
   finding."

3. Claude uses this skill to write the YAML file, validate it, and tell you how
   to run it.

4. Run the workflow:

   ```bash
   conductor run workflow.yaml --input key=value
   ```

5. Watch live progress with the web dashboard:

   ```bash
   conductor run workflow.yaml --web --input key=value
   ```

## A note on accuracy

The details in this skill (the YAML format, the context modes, the routing rules,
the caps, how to set a model, how structured output works) were checked directly
against the Conductor codebase and documentation, not guessed. The skill reflects
Conductor as of May 2026. If something stops matching as Conductor evolves, open
an issue.

## What makes a good workflow

Do not reach for a workflow by default. Pick deliberately:

| The job | Right tool |
|---|---|
| One agent, one task | A single Copilot/Claude session — no workflow |
| A reusable procedure where **you** pick the steps each run | A Skill or direct prompting |
| Many agents in a **fixed** shape (fan-out / pipeline / loop), same every run, worth checkpointing | A Conductor workflow ✅ |

A workflow earns its cost when **all** of these are true: the work is parallel or
multi-stage; you want the orchestration deterministic and resumable; and you want
the routing logic version-controlled.

## Key differences from Claude Code workflows

| Aspect | Conductor | Claude Code Workflows |
|--------|-----------|----------------------|
| **Who writes the orchestration** | Human (YAML) or Claude (with this skill) | Claude (JavaScript) |
| **Orchestration language** | YAML (declarative) | JavaScript (imperative) |
| **Execution** | Python engine | Claude Code runtime sandbox |
| **Determinism** | Routes evaluated in order | JavaScript control flow |
| **Checkpoints** | Auto-saved on failure, resume across sessions | Resume within same session only |
| **Context isolation** | Per-agent sessions | Fresh context per `agent()` call |

Both achieve deterministic multi-agent orchestration. Conductor workflows are
**human-authored** (or Claude-authored with this skill) and version-controlled.
Claude Code workflows are **LLM-generated** and run in a sandbox.

## Examples

See `assets/examples/` for three complete workflows:

- **implement-and-review.yaml** — Loop-until-pass pattern
- **review-branch.yaml** — Fan-out + adversarial verification
- **dead-code-sweep.yaml** — Loop-until-dry discovery

Each example includes inline comments and a README mapping it to the patterns in
`references/patterns.md`.

## Credits

Ported from [claude-code-workflow-creator](https://github.com/ray-amjad/claude-code-workflow-creator)
by Ray Amjad. The original skill teaches Claude to write JavaScript workflows for
Claude Code. This port teaches Claude to write YAML workflows for Conductor.

## License

MIT License - see [LICENSE](../../LICENSE) for details.
