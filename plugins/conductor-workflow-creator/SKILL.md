---
name: conductor-workflow-creator
description: >-
  Author runnable Conductor workflow YAML files — deterministic multi-agent
  orchestration that routes agents through conditional logic, parallel execution,
  and dynamic for-each groups. Use this skill when the user wants to create, write,
  build, scaffold, design, or fix a Conductor workflow: "make a workflow", "create
  a workflow for X", "write a conductor workflow", "turn this into a workflow",
  "scaffold a multi-agent pipeline", or any request to author or edit a .yaml file
  for Conductor. Also use it when the user is confused about Conductor's YAML
  format — agents, routes, context modes, parallel/for_each groups — or when a
  workflow errors and needs debugging. Trigger this even when the user only
  describes a repeatable multi-step or parallel job and seems to want it packaged
  as a workflow, even if they never say the word "workflow". Do NOT use it to
  merely run an existing workflow, or for a one-off single-agent task.
---

# Conductor Workflow Creator

Turn a goal into a **runnable Conductor workflow** — a YAML file that orchestrates
agents deterministically through routing rules, parallel execution, and dynamic
iteration.

A Conductor workflow defines agents, their prompts, and the routing logic between
them in YAML. The engine evaluates routes in order (first matching `when` wins),
executes agents sequentially or in parallel, and builds context according to the
configured mode. Unlike conversational orchestration where Claude decides the next
step, Conductor workflows are **deterministic** — the same inputs follow the same
path every time.

The deep material lives in two reference files — read them when the step says so:

- `references/api-reference.md` — the complete manual: every YAML field, every
  context mode, every limit, what happens at each cap.
- `references/patterns.md` — copy-paste orchestration patterns (fan-out, pipeline,
  loop-until-pass, adversarial verify, judge panel).

Starter files are in `assets/templates/`. Six complete, runnable example workflows
are in `assets/examples/` — `assets/examples/README.md` maps each one to a
topology and the techniques it demonstrates.

---

## Step 0 — Confirm Conductor is available

Check that Conductor is installed and accessible:

```bash
conductor --version
```

If not found, the user needs to install it first. Conductor workflows are YAML
files that live in `.conductor/` (project-local) or anywhere the user specifies.

---

## Step 1 — Decide whether a workflow is the right tool

Do not reach for a workflow by default. Pick deliberately:

| The job | Right tool |
|---|---|
| One agent, one task | A single Copilot/Claude session — no workflow |
| A reusable procedure where **you** pick the steps each run | A Skill or direct prompting |
| Many agents in a **fixed** shape (fan-out / pipeline / loop), same every run, worth checkpointing | A Conductor workflow ✅ |

A workflow earns its cost when **all** of these are true: the work is parallel or
multi-stage; you want the orchestration deterministic and resumable; and you want
the routing logic version-controlled. If you are unsure, say so and offer the
lighter option instead.

---

## Step 2 — Find the shape of the job

Before writing a line of YAML, answer these. The answers pick the topology for you.

1. **What is the unit of work?** The thing one agent does once — review one file,
   research one question, draft one platform. Name it concretely.
2. **How many units, and is the count known up front?** A known list → `for_each`
   over it. An unknown count (discovery, "find all the bugs") → a loop with
   `max_iterations`.
3. **What is the topology?**
   - Independent units, one pass each → **parallel group** or **for_each**.
   - Units flow through ordered stages (review → verify) → **sequential routing**
     or **for_each with chained agents**.
   - Keep going until a condition is met → **loop-back routing** with
     `max_iterations`.
4. **Does any later step need *all* the earlier results at once** — to dedup,
   merge, count, or early-exit on a zero total? If yes, you need a **barrier**
   (parallel group or for_each completion). If no, you do not — prefer sequential
   routing.
5. **Does a step need structured data back** (not free text)? Then that agent
   needs an `output:` schema.

Write these five answers down for the user before coding. They are the design.

---

## Step 3 — Choose context mode and routing strategy

### Context modes

Conductor has three modes for how agents see prior outputs:

- **`accumulate`** (default): All prior agent outputs available in templates.
  Use for workflows where later agents need to reference multiple earlier results.
- **`last_only`**: Only the previous agent's output is available. Use for strict
  pipelines where each stage only needs the immediate predecessor.
- **`explicit`**: Only inputs declared in the agent's `input:` list are available.
  Use for maximum isolation and explicit dependencies.

### Routing

Routes are evaluated **in order**. First matching `when` condition wins. A route
with no `when` always matches.

```yaml
routes:
  - to: fixer
    when: "{{ output.issues | length > 0 }}"  # Jinja2 template
  - to: verifier
    when: "score > 7"  # Arithmetic expression
  - to: $end  # No condition = always matches
```

**Loop-back pattern:** Route back to an earlier agent to create a loop. Always
set `limits.max_iterations` to prevent infinite loops.

---

## Step 4 — Write the YAML

A Conductor workflow has these top-level sections:

```yaml
workflow:
  name: review-and-fix
  description: Review code, fix issues, verify fixes
  entry_point: reviewer
  runtime:
    provider: copilot  # or claude
    default_model: gpt-4o
  context:
    mode: accumulate  # or last_only, explicit
  limits:
    max_iterations: 10
    timeout_seconds: 600

agents:
  - name: reviewer
    model: gpt-4o
    prompt: |
      Review {{ workflow.input.file }} for bugs and security issues.
    output:
      issues:
        type: array
        items:
          type: object
          properties:
            severity: { type: string }
            description: { type: string }
    routes:
      - to: fixer
        when: "{{ output.issues | length > 0 }}"
      - to: $end

  - name: fixer
    prompt: |
      Fix these issues:
      {% for issue in reviewer.output.issues %}
      - {{ issue.description }}
      {% endfor %}
    routes:
      - to: verifier

  - name: verifier
    prompt: "Verify the fixes are correct"
    output:
      passed: { type: boolean }
    routes:
      - to: reviewer
        when: "{{ not output.passed }}"
      - to: $end

output:
  total_issues: "{{ reviewer.output.issues | length }}"
  fixed: "{{ verifier.output.passed }}"
```

### Key YAML sections

**`workflow:`** — Metadata and configuration
- `name`: Workflow identifier
- `entry_point`: First agent to execute
- `runtime.provider`: `copilot` or `claude`
- `context.mode`: `accumulate`, `last_only`, or `explicit`
- `limits`: `max_iterations`, `timeout_seconds`

**`agents:`** — List of agent definitions
- `name`: Unique identifier
- `prompt`: Jinja2 template with access to context
- `output`: JSON Schema for structured output (optional)
- `routes`: Ordered list of routing rules
- `model`: Override the default model (optional)

**`parallel:`** — Static parallel groups (optional)
```yaml
parallel:
  - name: review-all
    agents: [reviewer1, reviewer2, reviewer3]
    failure_mode: continue_on_error  # or fail_fast, all_or_nothing
    routes:
      - to: merger
```

**`for_each:`** — Dynamic iteration over arrays (optional)
```yaml
for_each:
  - name: process-files
    type: for_each
    source: workflow.input.files
    as: item
    agent:
      name: processor
      prompt: "Process {{ item }}"
    max_concurrent: 4
    failure_mode: continue_on_error
    routes:
      - to: $end
```

**`output:`** — Final workflow output (Jinja2 templates)

For full field reference, **read `references/api-reference.md` now.** For
ready-made orchestration shapes, **read `references/patterns.md`** and copy the
one that fits Step 2's answers. Or start from a file in `assets/templates/`, or
adapt a full worked example from `assets/examples/`.

---

## Step 5 — Validate before running

Use Conductor's built-in validator to catch errors before execution:

```bash
conductor validate workflow.yaml
```

It flags: missing required fields, invalid route targets, stale template
references, undeclared dependencies, and schema errors. Fix every error it
reports before running.

---

## Step 6 — Run, watch, iterate

Run the workflow:

```bash
conductor run workflow.yaml --input file=src/api.py
```

Watch progress with `--web` for a real-time dashboard:

```bash
conductor run workflow.yaml --web --input file=src/api.py
```

If the workflow fails, Conductor auto-saves a checkpoint. Resume from the failed
agent:

```bash
conductor resume workflow.yaml
```

Iterate on the YAML, validate, and re-run until it does what you want.

---

## Step 7 — Common patterns and when to use them

### Fan-out (parallel group)
**When:** Independent tasks, all must complete, results aggregated.
```yaml
parallel:
  - name: review-all
    agents: [security, performance, style]
    routes:
      - to: merger
```

### Fan-out (for_each)
**When:** Dynamic list, same operation per item.
```yaml
for_each:
  - name: process-files
    type: for_each
    source: workflow.input.files
    as: item
    agent:
      name: processor
      prompt: "Process {{ item }}"
```

### Pipeline (sequential routing)
**When:** Ordered stages, each depends on the previous.
```yaml
agents:
  - name: stage1
    routes: [{ to: stage2 }]
  - name: stage2
    routes: [{ to: stage3 }]
  - name: stage3
    routes: [{ to: $end }]
```

### Loop-until-pass
**When:** Retry until a condition is met.
```yaml
agents:
  - name: reviewer
    output:
      passed: { type: boolean }
    routes:
      - to: fixer
        when: "{{ not output.passed }}"
      - to: $end
  - name: fixer
    routes: [{ to: reviewer }]

workflow:
  limits:
    max_iterations: 10
```

---

## Step 8 — Debugging tips

**Route not taken?** Check the `when` condition syntax. Use `--web` to see which
route matched.

**Context not available?** Check the `context.mode`. In `explicit` mode, declare
dependencies in `input:`.

**Infinite loop?** Set `limits.max_iterations`. The engine stops after N agent
executions.

**Checkpoint not resuming?** The workflow YAML must match the checkpoint. If you
changed the structure, start fresh.

---

## When NOT to use this skill

- **Running an existing workflow** — just use `conductor run workflow.yaml`
- **One-off single-agent task** — use a direct Copilot/Claude session
- **Exploratory work** — workflows are for repeatable, deterministic processes

Use this skill to **author** workflows, not to execute them.
