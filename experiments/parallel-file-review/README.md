# Experiment: Auto-Generated Parallel File Review Workflow

This experiment documents the full end-to-end process of using the
`conductor-workflow-creator` skill to auto-generate a multi-agent workflow,
validate it, and run it against live LLM calls.

---

## What Was Built

A **parallel code review workflow** that:
1. Fans out to N concurrent reviewer agents — one per file
2. Each reviewer reads its file using MCP tools and produces structured output
3. A summarizer agent aggregates all findings into an executive summary
4. Final output is a JSON report

Topology: `for_each(reviewer[0..N]) → summarizer → $end`

---

## Steps to Auto-Generate

### Step 1 — Invoke the Skill

Use the `conductor-workflow-creator` skill inside GitHub Copilot. Describe what
you want in plain English:

> "Create a workflow that reviews multiple source files in parallel and summarizes all findings"

The skill guides you through:
- Choosing a topology (fan-out / for_each was selected)
- Defining agent roles (reviewer per file, summarizer)
- Specifying input/output schemas
- Picking failure mode (`continue_on_error` so one bad file doesn't abort the rest)

### Step 2 — Write the Workflow YAML

The skill produced the YAML in `workflow.yaml` (also at `tmp/review-files.yaml`).

Key design decisions captured during generation:

| Decision | Choice | Reason |
|---|---|---|
| Topology | `for_each` | Need one agent per file, count unknown at author time |
| `source` field | `workflow.input.files` | Dotted-path syntax (not Jinja2) required by schema |
| `as` variable | `file` | Used as `{{ file }}` in per-item prompt |
| `max_concurrent` | 4 | Parallelism cap to avoid rate-limit bursts |
| `failure_mode` | `continue_on_error` | One unreadable file shouldn't kill the whole run |
| output type for counts | `number` | `integer` is NOT a valid Conductor output type |
| summarizer access | `{{ file_reviews.outputs[i].field }}` | for_each results live under `.outputs` array |

### Step 3 — Validate

```bash
uv run conductor validate tmp/review-files.yaml
```

Expected: `✓ Workflow is valid`

### Step 4 — Run

```bash
uv run conductor run tmp/review-files.yaml \
  --input 'files=["src/conductor/engine/router.py","src/conductor/engine/context.py","src/conductor/config/schema.py"]' \
  --no-interactive
```

---

## Execution Results

**Date**: 2026-05-31  
**Model**: `gpt-4.1` (via GitHub Copilot provider)  
**Total duration**: 36.62s  
**Files reviewed**: 3 (all ran concurrently)

### Per-Agent Cost

| Agent | File | Cost | Tokens (in/out) | Time |
|---|---|---|---|---|
| `reviewer[0]` | `router.py` | $0.0368 | ~18,302 in | 15.23s |
| `reviewer[1]` | `context.py` | $0.0489 | ~24,450 in | ~22s |
| `reviewer[2]` | `schema.py` | $0.0562 | ~28,100 in | ~25s |
| `summarizer` | — | $0.0337 | 16,447 in / 100 out | 4.62s |
| **Total** | | **$0.1756** | **87,178 tokens** | **36.62s** |

### Final JSON Output

```json
{
  "files_reviewed": 3,
  "total_issues": 0,
  "highest_severity": "clean",
  "summary": "Three files were reviewed: src/conductor/engine/router.py, src/conductor/engine/context.py, and src/conductor/config/schema.py. All files were found to be clean with no issues detected. The overall code health is excellent. No immediate actions are required. The recommended next step is to proceed with further development or testing as planned."
}
```

---

## Lessons Learned / Schema Gotchas

- **`source` syntax**: Must be `workflow.input.files` (dotted path), NOT `{{ workflow.input.files }}` (Jinja2 not accepted here)
- **`integer` type**: Invalid — use `number` instead in output schemas
- **Accessing for_each results**: Use `{{ group_name.outputs }}` (a list), iterate with `{% for result in file_reviews.outputs %}`
- **`when:` conditions**: Use `{{ output.field == 'value' }}` (Jinja2) for self-referential route guards — bare simpleeval expressions can't resolve agent names

---

## Files in This Experiment

| File | Description |
|---|---|
| `workflow.yaml` | The auto-generated workflow YAML |
| `run.events.jsonl` | Full event log from the execution run |
| `README.md` | This document |
