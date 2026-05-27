# Git Changelog Generator — Design Document

**Date:** 2026-05-26  
**Status:** Approved  
**Type:** Conductor workflow

---

## Problem

Generating a well-structured CHANGELOG from git history is tedious to do manually. Commit messages vary in quality and style; sorting them into categories (Added / Fixed / Changed / Removed / Security) and writing polished prose takes time that automation can reclaim.

## Goal

A conductor workflow (`examples/git-changelog.yaml`) that:
1. Reads all commits between the last git tag and HEAD (zero-config)
2. Uses an LLM to classify and summarise them into Keep-a-Changelog format
3. Writes the result to `CHANGELOG-draft.md` in the working directory

---

## Approach

**Two-agent pipeline: Classifier → Writer**, preceded and followed by script steps for data gathering and file I/O.

This split was chosen over a single-agent approach because classification and prose-writing are distinct cognitive tasks. Separating them produces higher-quality output, keeps each agent prompt focused, and makes the workflow easier to debug or extend.

---

## Architecture

```
collect_commits (script)
    │  typed JSON: { since_tag, commits }
    ▼
classifier (LLM — haiku)
    │  JSON array: [{ category, message }, ...]
    ▼
writer (LLM — haiku)
    │  string: polished CHANGELOG markdown section
    ▼
save_output (script)
    │  writes CHANGELOG-draft.md
    ▼
  $end
```

Error path: `collect_commits` exit_code != 0 → `error_handler` agent → `$end`

---

## Components

### 1. `collect_commits` (script step)

Runs a `bash -c` command that:
1. Calls `git describe --tags --abbrev=0` to find the most recent tag (falls back to empty string if none exist)
2. Runs `git log <TAG>..HEAD --pretty=format:"%h %s" --no-merges` (or `git log HEAD ...` if no tag)
3. Emits a typed JSON object to stdout

**Typed output schema:**
```yaml
output:
  since_tag:
    type: string
    description: The tag used as the start of the range, or "initial" if no tags exist
  commits:
    type: string
    description: Newline-separated commit lines in format "HASH subject"
  today:
    type: string
    description: Today's date in YYYY-MM-DD format (emitted by the script via Python's datetime)
```

**Routes:**
- `exit_code == 0 and commits != ""` → `classifier`
- `exit_code == 0 and commits == ""` → `no_commits_handler`
- `exit_code != 0` → `error_handler`

### 2. `classifier` (LLM agent)

**Model:** `claude-haiku-4.5` (fast and sufficient for classification)

**Context mode:** `explicit` (only receives `collect_commits` output)

**Prompt contract:** Given a list of raw commit lines, output a JSON array only — no prose, no markdown fences. Each element:
```json
{ "category": "Fixed|Added|Changed|Removed|Security|Other", "message": "<cleaned one-line description>" }
```

The agent is instructed to:
- Normalise commit messages (strip conventional-commit prefixes like `feat:`, `fix:`)
- Merge near-duplicate commits into one entry
- Classify `Other` for commits that are clearly internal/tooling (e.g. CI config, dependency bumps)

**Output schema:**
```yaml
output:
  classified:
    type: string
    description: JSON array of classified commit entries
```

**Routes:** `to: writer` (always)

### 3. `writer` (LLM agent)

**Model:** `claude-haiku-4.5`

**Context mode:** `explicit` (receives `collect_commits.output.since_tag` and `classifier.output.classified`)

**Prompt contract:** Given the classified JSON and the since_tag, write a single CHANGELOG.md section following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions:

```markdown
## [Unreleased] - YYYY-MM-DD

### Added
- ...

### Fixed
- ...
```

Rules enforced in prompt:
- Omit sections with no entries
- Use today's date from `{{ collect_commits.output.today }}` (emitted by the script as an ISO date string)
- Write entries as imperative-mood bullet points
- Do not include a preamble or trailing commentary

**Output schema:**
```yaml
output:
  changelog_content:
    type: string
    description: Complete CHANGELOG section in markdown format
```

**Routes:** `to: save_output` (always)

### 4. `save_output` (script step)

A Python one-liner that writes `{{ writer.output.changelog_content }}` to the destination file and prints a confirmation message.

Default destination: `CHANGELOG-draft.md` in the current working directory.  
Override via: `--input output_file=path/to/file.md`

### 5. `no_commits_handler` (LLM agent — lightweight)

Reached when `collect_commits` returns exit_code 0 but empty commits. Prints a friendly message:
> "No commits found since `<since_tag>`. Nothing to summarise."

Routes to `$end`.

### 6. `error_handler` (LLM agent — lightweight)

Reached when `collect_commits` fails (non-zero exit). Receives `collect_commits.output.stderr` and explains the git error in plain language.

Routes to `$end`.

---

## Workflow Inputs

All inputs are optional — zero config required.

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `since` | string | auto (last tag) | Override the start tag/commit (e.g. `v1.2.0`) |
| `output_file` | string | `CHANGELOG-draft.md` | Destination file path |

---

## Workflow Output

```yaml
output:
  file_path: "{{ save_output.output.stdout | trim }}"
  since_tag: "{{ collect_commits.output.since_tag }}"
  entry_count: "{{ classifier.output.classified | from_json | length }}"
```

---

## Error Handling

| Condition | Handling |
|-----------|----------|
| No git tags in repo | `since_tag = "initial"`, all commits included |
| No commits since last tag | Routes to `no_commits_handler`, exits cleanly |
| `git` not on PATH / not a git repo | `error_handler` agent explains the issue |
| LLM returns malformed JSON | Conductor's output validation raises `ExecutionError`; user sees a clear message |

---

## File Location

```
examples/git-changelog.yaml
```

Consistent with all other example workflows in the `examples/` directory.

---

## Usage

```bash
# Zero-config: summarise since last tag
conductor run examples/git-changelog.yaml

# Summarise since a specific tag
conductor run examples/git-changelog.yaml --input since=v1.2.0

# Write to a custom file
conductor run examples/git-changelog.yaml --input output_file=release-notes.md

# Combine inputs
conductor run examples/git-changelog.yaml \
  --input since=v1.2.0 \
  --input output_file=release-notes.md
```

---

## What Is Out of Scope

- Diff/patch analysis (commit messages only, by design)
- Editing or prepending to an existing `CHANGELOG.md` (write to draft file only)
- Automatic git tagging or release creation
- PR-based summarisation (separate workflow if needed)
