# PR Description Generator — Design Document

**Date:** 2026-05-27  
**Status:** Approved  
**Type:** Conductor workflow

---

## Problem

Writing a good PR description from a branch's commit history is repetitive and often skipped. Commits are a reliable source of truth for what changed, but turning a list of terse commit subjects into a readable narrative with a clear summary, key changes, and a breaking-change callout takes more effort than most engineers spend.

## Goal

A conductor workflow (`examples/pr-description.yaml`) that:

1. Auto-detects commits on the current branch that are not yet in `main` (or `master`)
2. Uses an LLM to analyze themes and extract key changes
3. Writes a polished PR description in Markdown to stdout — ready to pipe to `gh pr create` or paste into a PR form

---

## Approach

**Two-agent pipeline: Analyzer → Writer**, preceded by a script step for data collection.

A single-agent approach (collect → write in one prompt) tends to produce lower-quality output because the model must simultaneously reason about what changed and compose polished prose. Separating those into distinct agents with focused prompts produces better output and makes each stage easier to debug or tune. The collect step also captures `git diff --stat` to give the LLM file-level context without the token risk of a full diff.

---

## Architecture

```
collect_commits (script)
    │  typed JSON: { base_branch, branch, commits, diff_stat, commit_count }
    ▼
analyzer (LLM — haiku, explicit context)
    │  JSON: { themes, key_changes, breaking_changes, scope }
    ▼
writer (LLM — haiku, explicit context)
    │  string: polished PR description markdown
    ▼
  $end  (output printed to stdout)
```

**Error paths:**
- `collect_commits` exits non-zero → `error_handler` agent → `$end`
- `commit_count == 0` (branch is already up-to-date with base) → `no_commits_handler` agent → `$end`

---

## Components

### 1. `collect_commits` (script step)

Runs a `bash -c` block that:

1. Resolves the base branch: tries `main`, falls back to `master`, then accepts a user-supplied `base_branch` workflow input as final fallback.
2. Runs `git log <base>..HEAD --oneline --no-merges` for commit subjects.
3. Runs `git diff --stat <base>..HEAD` for file-level context.
4. Captures the current branch name (`git rev-parse --abbrev-ref HEAD`) and commit count.
5. Emits a typed JSON object to stdout.

**Typed output schema:**

| Field | Type | Description |
|---|---|---|
| `base_branch` | string | The resolved base branch (e.g., `main`) |
| `branch` | string | Current branch name |
| `commits` | string | Newline-separated commit subjects with short hash |
| `diff_stat` | string | Output of `git diff --stat` |
| `commit_count` | integer | Number of commits ahead of base |

**Routes:**

| Condition | Next step |
|---|---|
| `exit_code == 0 and commit_count > 0` | `analyzer` |
| `exit_code == 0 and commit_count == 0` | `no_commits_handler` |
| `exit_code != 0` | `error_handler` |

---

### 2. `analyzer` (LLM agent)

**Model:** Copilot default (haiku-class)  
**Context mode:** `explicit` — receives only `collect_commits` outputs  
**Tools:** none

Analyzes the commits and diff stat to produce a structured understanding of the change set.

**Output schema (JSON):**

| Field | Type | Description |
|---|---|---|
| `themes` | array[string] | 2–4 high-level themes (e.g., "auth refactor", "performance fixes") |
| `key_changes` | array[string] | 3–6 specific notable changes suitable for bullet points |
| `breaking_changes` | boolean | Whether any commit signals a breaking change |
| `scope` | string | Primary component or area affected (e.g., "API", "CLI", "database") |

---

### 3. `writer` (LLM agent)

**Model:** Copilot default (haiku-class)  
**Context mode:** `explicit` — receives analyzer output + branch name  
**Tools:** none

Writes a PR description in this format:

```markdown
## Summary
<2–3 sentence narrative describing what changed and why>

## Changes
- <key change bullet>
- <key change bullet>
- ...

## Notes
⚠️ Breaking change: <description>   ← only rendered when breaking_changes == true
```

The branch name is included in the prompt so the writer can infer intent from naming conventions (e.g., `fix/auth-token-expiry` suggests a bug fix).

---

### 4. `error_handler` (LLM agent) / `no_commits_handler` (script step)

`error_handler` (LLM agent): Receives the script's stderr (captured automatically by conductor's script step) and exit code. Emits a human-readable error message explaining what went wrong (not a git repo, base branch not found, etc.).

`no_commits_handler` (script step): Emits a static message: "No commits found ahead of `<base_branch>`. The branch may already be up-to-date." No LLM call needed — the message is deterministic.

Both steps output to stdout via the workflow `output:` section so the consumer gets a meaningful message regardless of path.

---

## Usage

```bash
# Auto-detect base branch (tries main, then master)
uv run conductor run examples/pr-description.yaml

# Override base branch
uv run conductor run examples/pr-description.yaml --input base_branch=develop

# Pipe directly to gh CLI
uv run conductor run examples/pr-description.yaml | gh pr create --body-file -

```

The workflow targets the Copilot provider (default). It can be run with `--provider claude` via conductor's standard provider flag, but Claude is not a primary target and no Claude-specific tuning is done.

---

## Context Mode Strategy

| Agent | Context mode | Rationale |
|---|---|---|
| `analyzer` | `explicit` | Only needs commits + diff_stat; avoids polluting prompt with unrelated state |
| `writer` | `explicit` | Only needs analyzer output + branch name; keeps prompt tight and focused |
| `error_handler` | `explicit` | Only needs script stderr + exit_code |
| `no_commits_handler` | `explicit` | Script step — no LLM call needed for a static message |

---

## Error Handling

| Scenario | Behavior |
|---|---|
| Not a git repository | `error_handler` outputs a clear message |
| Neither `main` nor `master` exists, no `base_branch` input | Script exits non-zero with message; `error_handler` surfaces it |
| No commits ahead of base | `no_commits_handler` outputs a short informational message |
| LLM output fails schema validation | Conductor's built-in output validation retries; fails with a clear error after max retries |

---

## Out of Scope

- Writing the PR directly via GitHub API (out of scope — stdout + pipe covers this cleanly)
- Supporting GitLab MR descriptions (same approach, different CLI; not in scope for this workflow)
- A new `conductor pr-summary` subcommand (conductor workflow covers the use case without CLI changes)
- Full `git diff` context (token/cost risk not worth it for typical PR descriptions)
