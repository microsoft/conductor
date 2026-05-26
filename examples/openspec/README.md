# OpenSpec Example Pipeline

The OpenSpec pipeline is a spec-driven, full-lifecycle workflow that turns a one-sentence idea into implemented code. It automates the journey from proposal through specs, design, task breakdown, and finally implementation — all via conductor sub-workflows.

## Pipeline Structure

The pipeline runs five sequential phases, each implemented as a standalone conductor sub-workflow:

```
propose_phase → specs_phase → design_phase → tasks_phase → apply_phase
```

| Phase | File | Artifact | Purpose |
|-------|------|----------|---------|
| **1. Propose** | `phases/propose.yaml` | `proposal.md` | Define WHY and WHAT of the change |
| **2. Specs** | `phases/specs.yaml` | `specs/<capability>/spec.md` | Write a spec per capability listed in the proposal |
| **3. Design** | `phases/design.yaml` | `design.md` | Capture technical decisions and architecture |
| **4. Tasks** | `phases/tasks.yaml` | `tasks.md` | Break down implementation into checkbox tasks |
| **5. Apply** | `phases/apply.yaml` | Code changes | Implement each pending task sequentially |

### Sequence and Data Flow

Phases execute **strictly in order**, with each phase consuming the artifacts produced by earlier phases:

```
idea (string)
    │
    ▼
┌─────────────┐
│   Propose   │  → openspec/changes/<change>/proposal.md
└──────┬──────┘
       │  reads: idea
       ▼
┌─────────────┐
│    Specs    │  → openspec/changes/<change>/specs/<capability>/spec.md  (one per capability)
└──────┬──────┘
       │  reads: proposal.md
       ▼
┌─────────────┐
│   Design    │  → openspec/changes/<change>/design.md
└──────┬──────┘
       │  reads: proposal.md + all spec files
       ▼
┌─────────────┐
│    Tasks    │  → openspec/changes/<change>/tasks.md
└──────┬──────┘
       │  reads: proposal.md + all spec files + design.md
       ▼
┌─────────────┐
│    Apply    │  → code changes in repository; tasks.md checkboxes marked [x]
└─────────────┘
       │  reads: tasks.md (unchecked items only)
```

All artifacts for a given `change` are written under `openspec/changes/<change>/` in the repository root.

### Design Principles

- **Spec-driven**: requirements (specs) drive design and implementation, not the reverse.
- **Idempotent phases**: each phase checks whether its output artifact already exists and skips itself automatically — re-running the pipeline never duplicates work.
- **Artifact-dependency chain**: a phase only reads outputs from preceding phases, so you can edit any artifact by hand and the next phase will pick up your edits.
- **Sequential apply, concurrent specs**: the Apply phase runs tasks one at a time (preventing concurrent writes to shared files), while the Specs phase generates multiple spec files concurrently (safe because each targets a distinct file).

## Prerequisites

- [conductor](https://github.com/fenghaitao/conductor) installed and on your `PATH`
- A configured provider (Copilot or Claude) — set via `--provider` or `runtime.provider` in the YAML
- No separate `openspec` CLI required — all schemas and templates are bundled under `examples/openspec/artifacts/`

## Step-by-Step Execution

Follow these steps to run the full pipeline from idea to implementation:

**Step 1 — Install conductor** (if not already installed)

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/fenghaitao/conductor/main/install.sh | bash

# Windows (PowerShell)
iwr https://raw.githubusercontent.com/fenghaitao/conductor/main/install.ps1 | iex
```

Verify the installation:

```bash
conductor --version
```

**Step 2 — Ensure your provider is configured**

For GitHub Copilot (default), sign in via the GitHub CLI or ensure `GITHUB_TOKEN` is set:

```bash
gh auth login          # GitHub Copilot
```

For Anthropic Claude, export your API key:

```bash
export ANTHROPIC_API_KEY=your-key-here
```

**Step 3 — Clone this repository** (if running from source)

```bash
git clone https://github.com/fenghaitao/conductor.git
cd conductor
```

> **Note:** All `conductor run` commands below assume you are in the repository root (`conductor/`). The pipeline YAML paths are relative to this directory.

**Step 4 — Run the pipeline**

Provide a kebab-case `change` name and a one-sentence `idea`:

```bash
conductor run examples/openspec/openspec-pipeline.yaml \
  --input change="add-oauth-login" \
  --input idea="Add OAuth2 login with GitHub and Google providers"
```

The pipeline runs all five phases in sequence and writes artifacts under `openspec/changes/add-oauth-login/`.

**Step 5 — Review generated artifacts**

After the pipeline completes, inspect the artifacts:

```
openspec/changes/add-oauth-login/
├── proposal.md          # Phase 1 — WHY and WHAT
├── specs/               # Phase 2 — per-capability spec files
│   └── <capability>/
│       └── spec.md
├── design.md            # Phase 3 — technical decisions
└── tasks.md             # Phase 4 — implementation task list
```

Code changes from the Apply phase (Phase 5) are written directly to your source files in the repository. No separate output directory is created — changes land exactly where they belong in your project.

**Step 6 — Resume if interrupted**

If the pipeline is stopped or fails mid-run, resume from the last checkpoint:

```bash
conductor resume examples/openspec/openspec-pipeline.yaml
```

No data is lost — completed phases and tasks are not re-run.

## Running the Pipeline

### Start from scratch

```bash
conductor run examples/openspec/openspec-pipeline.yaml \
  --input change="add-oauth-login" \
  --input idea="Add OAuth2 login with GitHub and Google providers"
```

- `change`: kebab-case name for the change (used as a directory name for artifacts)
- `idea`: one-sentence description of what to build (only needed when no `proposal.md` exists yet)

### Resume after a partial run

```bash
conductor resume examples/openspec/openspec-pipeline.yaml
```

Conductor checkpoints after each phase (and after each task in the apply phase), so you can resume from exactly where the pipeline stopped.

### Run with the real-time dashboard

```bash
conductor run examples/openspec/openspec-pipeline.yaml --web \
  --input change="add-oauth-login" \
  --input idea="Add OAuth2 login with GitHub and Google providers"
```

### Run in the background

```bash
conductor run examples/openspec/openspec-pipeline.yaml --web-bg \
  --input change="add-oauth-login" \
  --input idea="Add OAuth2 login with GitHub and Google providers"
```

Prints the dashboard URL and returns immediately. Use `conductor stop` to halt a running background workflow.

### Run a single phase standalone

Each phase YAML can be run independently:

```bash
# Only generate proposal.md
conductor run examples/openspec/phases/propose.yaml \
  --input change="add-oauth-login" \
  --input idea="Add OAuth2 login with GitHub and Google providers"

# Only generate specs (proposal.md must exist)
conductor run examples/openspec/phases/specs.yaml \
  --input change="add-oauth-login"

# Only generate design.md (proposal.md + specs must exist)
conductor run examples/openspec/phases/design.yaml \
  --input change="add-oauth-login"

# Only generate tasks.md (proposal.md + specs + design.md must exist)
conductor run examples/openspec/phases/tasks.yaml \
  --input change="add-oauth-login"

# Only implement pending tasks (tasks.md must exist)
conductor run examples/openspec/phases/apply.yaml \
  --input change="add-oauth-login"
```

## Phase Descriptions

### Phase 1 — Propose (`phases/propose.yaml`)

**Purpose:** Generates `proposal.md`, the high-level description of the change.

**Inputs:** `change` (required), `idea` (required when starting fresh)

**Output:** `openspec/changes/<change>/proposal.md`

**Steps:**
1. `get_instructions` — loads the proposal template and instructions from `artifacts/schema.yaml`
2. `write_proposal` — LLM drafts the full proposal
3. `save_proposal` — Python script writes the file to disk

**Idempotent:** skips immediately if `proposal.md` already exists.

---

### Phase 2 — Specs (`phases/specs.yaml`)

**Purpose:** Generates one `spec.md` file per capability listed in the proposal's *New Capabilities* section.

**Inputs:** `change`

**Output:** `openspec/changes/<change>/specs/<capability>/spec.md` for each capability

**Steps:**
1. `get_pending_capabilities` — parses `proposal.md` and returns capability objects (name, kind, existing spec path) for capabilities that still need spec files; new capabilities get an empty `existing_spec_path`, modified capabilities get the path to their existing main spec
2. `resolve_unmatched` *(conditional)* — only runs when a modified capability could not be matched to any existing spec folder by the Python heuristics; an LLM makes a semantic judgement call from the real folder list
3. `apply_resolved_mappings` — merges any agent-resolved mappings back into the capabilities list; always runs to give `spec_writers` a stable, final source regardless of whether `resolve_unmatched` ran
4. `spec_writers` (for_each, up to 2 concurrent) — runs `write-spec.yaml` for each pending capability

**Idempotent:** capabilities that already have a spec file are filtered out before generation.

---

### Phase 3 — Design (`phases/design.yaml`)

**Purpose:** Generates `design.md` — technical decisions, architecture choices, and a migration plan.

**Inputs:** `change`

**Output:** `openspec/changes/<change>/design.md`

**Steps:**
1. `get_instructions` — loads the design template
2. `read_context` — concatenates `proposal.md` and all spec files for LLM context
3. `write_design` — LLM generates `design.md`
4. `save_design` — Python writes the file

**Idempotent:** skips immediately if `design.md` already exists.

---

### Phase 4 — Tasks (`phases/tasks.yaml`)

**Purpose:** Generates `tasks.md` — a grouped, checkbox-based list of implementation tasks.

**Inputs:** `change`

**Output:** `openspec/changes/<change>/tasks.md`

**Steps:**
1. `get_instructions` — loads the tasks template
2. `read_context` — concatenates `proposal.md`, all specs, and `design.md`
3. `write_tasks` — LLM generates `tasks.md`
4. `save_tasks` — Python writes the file

**Idempotent:** skips immediately if `tasks.md` already exists.

---

### Phase 5 — Apply (`phases/apply.yaml`)

**Purpose:** Implements every pending task in `tasks.md` by running `implement-task.yaml` for each unchecked item.

**Inputs:** `change`

**Output:** Code changes committed to the repository; `tasks.md` checkboxes marked `[x]` as tasks complete

**Steps:**
1. `get_pending_tasks` — parses `tasks.md` and returns unchecked (`- [ ]`) tasks
2. `task_implementers` (for_each, **sequential**) — runs `implement-task.yaml` for each pending task, one at a time to prevent concurrent writes

**Idempotent:** tasks already marked `[x]` are excluded in step 1. Re-running the pipeline after a partial apply continues from the first unchecked task.

## Artifacts Directory

`artifacts/schema.yaml` contains the bundled templates and instructions used by each phase. You can inspect or customise these templates to change how each artifact is generated — without modifying the phase workflows themselves.

## Onboarding Guide (New Users)

If this is your first time using the OpenSpec pipeline, follow this path:

### Quick Start (5 minutes)

1. **Install conductor** — see [Prerequisites](#prerequisites) and [Step-by-Step Execution](#step-by-step-execution) above.
2. **Pick a small, well-scoped idea** — e.g. `"Add a --quiet flag that suppresses progress output"`. Avoid multi-system ideas on your first run.
3. **Run the pipeline** (from the repository root):
   ```bash
   conductor run examples/openspec/openspec-pipeline.yaml \
     --input change="my-first-change" \
     --input idea="Your one-sentence idea here"
   ```
4. **Watch it live** — add `--web` to open the browser dashboard and observe each phase in real time:
   ```bash
   conductor run examples/openspec/openspec-pipeline.yaml --web \
     --input change="my-first-change" \
     --input idea="Your one-sentence idea here"
   ```
5. **Inspect the output** — when it finishes, look at `openspec/changes/my-first-change/` and read through `proposal.md`, `design.md`, and `tasks.md` to see what the pipeline produced.

### Understanding the Workflow

The pipeline is fully automated but **not a black box**. Each phase writes a plain Markdown file that you can read, edit, or discard before the next phase runs:

- Don't like the proposal? Edit `proposal.md` before the Specs phase reads it.
- Want fewer or different tasks? Edit `tasks.md` before Apply begins.
- Stopped mid-run? Run `conductor resume examples/openspec/openspec-pipeline.yaml` — completed phases are never re-run.

> **What does "idempotent" mean here?** It means each phase checks for its output file before doing any work. If `proposal.md` already exists, the Propose phase exits immediately. You can safely re-run the full pipeline at any time without duplicating or overwriting existing artifacts.

### Common First-Run Questions

| Question | Answer |
|----------|--------|
| What model / provider is used? | The default provider is GitHub Copilot. Pass `--provider claude` to switch to Anthropic Claude. |
| Where do generated files go? | `openspec/changes/<change>/` in the repository root. |
| Can I re-run without losing work? | Yes — every phase is idempotent: it skips itself if its output file already exists. |
| What if a phase produces bad output? | Delete the artifact (`proposal.md`, `design.md`, etc.) and re-run that phase or the full pipeline. |
| Is anything committed to git automatically? | No — conductor writes files to disk; you decide what to commit. |

---

## Usage Guide (Existing Users)

### Adapting the Pipeline to Your Project

The pipeline is designed to be forked and customised. Key extension points:

#### 1. Change the LLM templates

All prompts and schema templates live in `artifacts/schema.yaml`. Edit entries there to change how each artifact is written — without touching the phase YAML files.

#### 2. Add or remove phases

Each phase is a standalone conductor sub-workflow (`phases/*.yaml`). To add a phase:
1. Create `phases/my-phase.yaml`.
2. Add a sub-workflow call to `openspec-pipeline.yaml` after the phase it depends on.
3. Document the new phase in this README under [Phase Descriptions](#phase-descriptions).

To remove a phase, delete its entry from `openspec-pipeline.yaml` and remove its YAML file. Downstream phases that read its artifact will need to be updated accordingly.

#### 3. Change the concurrency of Specs or Apply

- **Specs phase** (`phases/specs.yaml`): the `for_each` block has `max_concurrent: 2`. Increase this to generate spec files faster when there are many capabilities.
- **Apply phase** (`phases/apply.yaml`): tasks run sequentially (`max_concurrent: 1`) to prevent concurrent writes to shared source files. Only increase this if your tasks write to disjoint files.

#### 4. Switch providers per phase

Each phase inherits the provider from `openspec-pipeline.yaml`, but you can override per phase by setting `runtime.provider` in the phase YAML. For example, use a cheaper model for the Propose phase and a more capable one for Apply.

#### 5. Use the pipeline in CI

Run the pipeline in a CI job and commit the generated artifacts automatically:

```bash
conductor run examples/openspec/openspec-pipeline.yaml \
  --input change="${CHANGE_NAME}" \
  --input idea="${CHANGE_IDEA}" \
  --no-interactive \
  --provider copilot

git add openspec/changes/"${CHANGE_NAME}"
git commit -m "chore: add openspec artifacts for ${CHANGE_NAME}"
```

### Maintaining This README

> **Important:** This README is the primary onboarding and reference document for the OpenSpec pipeline. It is part of the pipeline's codebase and must be treated as a living document — **update it whenever the pipeline changes**.

When the pipeline evolves, keep these in sync:

- [ ] Update the phase table in [Pipeline Structure](#pipeline-structure) when phases are added, renamed, or removed.
- [ ] Update [Phase Descriptions](#phase-descriptions) to reflect any changed inputs, outputs, or steps.
- [ ] Update `artifacts/schema.yaml` comments when templates change.
- [ ] Run `conductor validate examples/openspec/openspec-pipeline.yaml` after editing any YAML to catch schema errors early.
- [ ] Review this README for accuracy whenever a phase YAML, template, or pipeline orchestration file changes — stale documentation is worse than no documentation.

Assign ownership of README updates as part of your team's definition of done for pipeline changes: a pipeline PR is not complete until this README reflects the new behavior.

### Tips

- Phases are self-contained sub-workflows — upgrade or replace a single phase without touching the others.
- The Apply phase runs tasks **sequentially** by design to avoid race conditions on shared source files.
- Keep this README up to date as the pipeline evolves. When you add or rename a phase, update the table and phase descriptions above.
