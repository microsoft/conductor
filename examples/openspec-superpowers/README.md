# OpenSpec Superpowers Pipeline

The OpenSpec Superpowers pipeline is an extended, skill-augmented lifecycle that builds on the base `examples/openspec/` pipeline by layering interactive [Superpowers](https://github.com/your-org/superpowers) skills — brainstorming, writing-plans, and subagent-driven-development — into every stage of the spec-driven change process. Where the base pipeline moves directly from an idea to a proposal and then to implementation, the superpowers variant front-loads the change with a guided brainstorming session and wraps implementation with structured planning, verification, and retrospective phases.

The extended lifecycle runs nine phases in sequence:

```
brainstorm → propose → specs → design → tasks → plan → apply → verify → retrospective
```

The primary differences from the base `examples/openspec/` pipeline are:

- **Four additional phases**: `brainstorm` (interactive idea exploration), `plan` (interactive micro-task planning), `verify` (post-implementation evidence check), and `retrospective` (evidence-first reflection).
- **Interactive skill sessions**: the `brainstorm` and `plan` phases use `interactive_input: true`, allowing the embedded Superpowers skills to ask clarifying questions in real time before producing their output artifacts.
- **Bundled skills**: all Superpowers skills are shipped alongside the pipeline under `artifacts/skills/` and wired in via `skill_directories` — no separate Superpowers plugin install is required.

## Comparison with the Base Pipeline

| Phase | `examples/openspec/` | `examples/openspec-superpowers/` |
|---|---|---|
| brainstorm | — (not present) | ✅ `phases/brainstorm.yaml` (interactive skill) |
| propose | ✅ `phases/propose.yaml` | ✅ `phases/propose.yaml` (extracts from brainstorm.md) |
| specs | ✅ `phases/specs.yaml` | ✅ `phases/specs.yaml` |
| design | ✅ `phases/design.yaml` | ✅ `phases/design.yaml` (reads brainstorm.md) |
| tasks | ✅ `phases/tasks.yaml` | ✅ `phases/tasks.yaml` |
| plan | — (not present) | ✅ `phases/plan.yaml` (interactive skill) |
| apply | ✅ `phases/apply.yaml` | ✅ `phases/apply.yaml` (TDD + code-review) |
| verify | — (not present) | ✅ `phases/verify.yaml` |
| retrospective | — (not present) | ✅ `phases/retrospective.yaml` |

Skills are bundled under `artifacts/skills/` — no plugin install is required. The `brainstorm` and `plan` phases set `interactive_input: true`, enabling live Q&A with the embedded Superpowers skills during execution.

## Prerequisites

- **[conductor](https://github.com/fenghaitao/conductor) installed and on your `PATH`**

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

- **A configured provider** — GitHub Copilot or Anthropic Claude:

  ```bash
  # GitHub Copilot (default)
  gh auth login

  # Anthropic Claude
  export ANTHROPIC_API_KEY=your-key-here
  ```

- **No Superpowers plugin install required** — all skills are bundled under `examples/openspec-superpowers/artifacts/skills/` and wired into the pipeline automatically via `skill_directories`. You do not need to install the Superpowers plugin separately.

- **Repository cloned locally** — all `conductor run` commands are run from the repository root:

  ```bash
  git clone https://github.com/fenghaitao/conductor.git
  cd conductor
  ```

## Usage

### Full pipeline (start from scratch)

Run all nine phases from brainstorm through retrospective by supplying a change name and an initial idea:

```bash
conductor run examples/openspec-superpowers/openspec-superpowers-pipeline.yaml \
  --input change="add-oauth-login" \
  --input idea="Add OAuth2 login with GitHub and Google providers"
```

### With real-time dashboard

Open a live browser dashboard that visualises agent progress as the pipeline runs:

```bash
conductor run examples/openspec-superpowers/openspec-superpowers-pipeline.yaml \
  --input change="add-oauth-login" \
  --input idea="Add OAuth2 login with GitHub and Google providers" \
  --web
```

To run the pipeline and dashboard in the background (prints the dashboard URL and exits):

```bash
conductor run examples/openspec-superpowers/openspec-superpowers-pipeline.yaml \
  --input change="add-oauth-login" \
  --input idea="Add OAuth2 login with GitHub and Google providers" \
  --web-bg
```

### Resume after a partial run

If the pipeline was interrupted or a phase failed, resume from the last checkpoint:

```bash
conductor resume examples/openspec-superpowers/openspec-superpowers-pipeline.yaml
```

Resume with the dashboard:

```bash
conductor resume examples/openspec-superpowers/openspec-superpowers-pipeline.yaml --web
```

### Skip brainstorm (proposal already exists)

Every phase is **idempotent** — it checks whether its output artifact already exists and skips itself if so. You can pre-create `brainstorm.md` or `proposal.md` manually (or copy one from a previous run) and place it at the expected path:

```
openspec/changes/<change>/brainstorm.md
openspec/changes/<change>/proposal.md
```

Then run the full pipeline as normal. Phases whose output file is already present will be skipped automatically, and execution will continue from the first pending phase.

### Run a single phase

Invoke any individual phase YAML directly. For example, to run only the brainstorm phase:

```bash
conductor run examples/openspec-superpowers/phases/brainstorm.yaml \
  --input change="add-oauth-login" \
  --input idea="Add OAuth2 login with GitHub and Google providers"
```

Replace `brainstorm.yaml` with any other phase filename (`propose.yaml`, `specs.yaml`, `design.yaml`, `tasks.yaml`, `plan.yaml`, `apply.yaml`, `verify.yaml`, `retrospective.yaml`) to run that phase in isolation.

## Phase Walkthrough

The nine phases run in a fixed sequence. Each phase reads one or more input artifacts and writes a single output artifact. Phases are idempotent: if the output artifact already exists the phase is a no-op.

```
idea (input)
    │
    ▼
brainstorm  → openspec/changes/<change>/brainstorm.md
    │
    ▼
propose     → openspec/changes/<change>/proposal.md
    │
    ▼
specs       → openspec/changes/<change>/specs/<capability>/spec.md
    │
    ▼
design      → openspec/changes/<change>/design.md
    │
    ▼
tasks       → openspec/changes/<change>/tasks.md
    │
    ▼
plan        → openspec/changes/<change>/plan.md
    │
    ▼
apply       → code changes; tasks.md checkboxes marked [x]
    │
    ▼
verify      → openspec/changes/<change>/verify.md
    │
    ▼
retrospective → openspec/changes/<change>/retrospective.md
```

### 1. Brainstorm (`phases/brainstorm.yaml`)

**Purpose:** Deeply explore the change space before committing to a direction.

**Skill:** `superpowers:brainstorming` (interactive)

The brainstorm phase launches an interactive `superpowers:brainstorming` skill session. The skill asks one question at a time to understand the context, constraints, and goals of the change. It then proposes 2–3 design approaches and guides you through selecting one. The session ends by writing a `brainstorm.md` that captures the full decision chain.

- **Input:** `--input idea="…"` (CLI), existing codebase context
- **Output:** `openspec/changes/<change>/brainstorm.md`
- **Notable:** Uses `interactive_input: true` — requires a live terminal.

---

### 2. Propose (`phases/propose.yaml`)

**Purpose:** Distil the brainstorm conversation into a concise, machine-readable proposal.

The propose phase reads `brainstorm.md` and extracts a structured `proposal.md` covering *Why*, *What Changes*, *Capabilities*, and *Impact*. Because brainstorming already explored the problem space, the proposal agent skips exploratory re-analysis and focuses on synthesis.

- **Input:** `openspec/changes/<change>/brainstorm.md`
- **Output:** `openspec/changes/<change>/proposal.md`

---

### 3. Specs (`phases/specs.yaml`)

**Purpose:** Generate one detailed spec per capability listed in the proposal.

The specs phase reads `proposal.md`, identifies every capability listed under *New Capabilities* and *Modified Capabilities*, and generates a `spec.md` for each one concurrently. Each spec describes the capability's scenarios (Given/When/Then) and acceptance criteria.

- **Input:** `openspec/changes/<change>/proposal.md`
- **Output:** `openspec/changes/<change>/specs/<capability>/spec.md` (one file per capability)

---

### 4. Design (`phases/design.yaml`)

**Purpose:** Transform the brainstorm decision log into a structured design document.

The design phase reads `brainstorm.md` and the generated specs to produce a `design.md` that follows the standard sections: *Context*, *Goals / Non-Goals*, *Decisions*, *Risks / Trade-offs*, and *Migration Plan*. Reading `brainstorm.md` gives the design agent access to the full rationale behind each decision without requiring a fresh exploration pass.

- **Input:** `openspec/changes/<change>/brainstorm.md`, `openspec/changes/<change>/specs/`
- **Output:** `openspec/changes/<change>/design.md`

---

### 5. Tasks (`phases/tasks.yaml`)

**Purpose:** Produce a checkbox-based implementation task list.

The tasks phase reads the generated specs and `design.md` and writes a `tasks.md` that breaks the change into small, independently implementable tasks. Each task maps to one or more spec scenarios and cites the relevant design decision.

- **Input:** `openspec/changes/<change>/specs/`, `openspec/changes/<change>/design.md`
- **Output:** `openspec/changes/<change>/tasks.md`

---

### 6. Plan (`phases/plan.yaml`)

**Purpose:** Decompose each task into TDD micro-steps with file paths and commit points.

**Skill:** `superpowers:writing-plans` (interactive)

The plan phase launches an interactive `superpowers:writing-plans` skill session. The skill reads `tasks.md` and collaborates with you to break every `[ ]` task into 2–5 minute micro-steps, each specifying: the file to create or modify, the minimal code change, the test command, and a commit message. The result is a `plan.md` ready for automated implementation.

- **Input:** `openspec/changes/<change>/tasks.md`
- **Output:** `openspec/changes/<change>/plan.md`
- **Notable:** Uses `interactive_input: true` — requires a live terminal.

---

### 7. Apply (`phases/apply.yaml`)

**Purpose:** Implement every pending task in the plan using TDD and code review.

**Skill:** `superpowers:subagent-driven-development`

The apply phase iterates over all `[ ]` tasks in `tasks.md`. For each task it invokes the `subagent-driven-development` skill, which writes failing tests first, implements the feature, verifies tests pass, and runs a self-review. After each task succeeds its checkbox is marked `[x]`. If a task fails the pipeline stops and can be resumed.

- **Input:** `openspec/changes/<change>/tasks.md`, `openspec/changes/<change>/plan.md`
- **Output:** Code changes committed to the repository; `tasks.md` checkboxes updated to `[x]`

---

### 8. Verify (`phases/verify.yaml`)

**Purpose:** Confirm that the implementation matches the specs and design.

**Skill:** `superpowers:openspec-verify-change`

The verify phase first runs script pre-checks (git log for commits, `tasks.md` checkbox count). If pre-checks pass, the LLM runs 7 structured checks — requirements coverage, design adherence, test quality, code quality, security, documentation, and overall coherence — and writes the findings to `verify.md`. A failed pre-check aborts the verification with a clear error.

- **Input:** Code changes, `openspec/changes/<change>/tasks.md`, `openspec/changes/<change>/specs/`, `openspec/changes/<change>/design.md`
- **Output:** `openspec/changes/<change>/verify.md`

---

### 9. Retrospective (`phases/retrospective.yaml`)

**Purpose:** Produce an evidence-first retrospective for team learning and process improvement.

The retrospective phase gathers git log, diff statistics, and task-completion metrics, then asks the LLM to write a `retrospective.md` structured as: §0 Evidence → §1 Wins → §2 Misses → §3 Deviations from plan → §4 Process compliance → §5 Surprises → §6 Candidates to promote to permanent workflow improvements.

- **Input:** Git log, diff stats, `openspec/changes/<change>/tasks.md`, `openspec/changes/<change>/verify.md`
- **Output:** `openspec/changes/<change>/retrospective.md`

## Notes

### skill_directories wiring

Each phase YAML that uses a Superpowers skill contains a `runtime` block with a `skill_directories` field pointing to `../artifacts/skills` (relative to the phase file):

```yaml
runtime:
  skill_directories:
    - ../artifacts/skills
```

When conductor starts a phase, it scans every directory listed under `skill_directories` and registers each subdirectory it finds as a loadable skill. The subdirectory name becomes the skill's identifier under the `superpowers:` namespace. For example:

```
artifacts/skills/
├── brainstorming/        → superpowers:brainstorming
├── writing-plans/        → superpowers:writing-plans
└── subagent-driven-development/  → superpowers:subagent-driven-development
```

Because the skills are bundled inside `examples/openspec-superpowers/artifacts/skills/`, no `superpowers` plugin install is required. The pipeline is self-contained — cloning the repository is sufficient.

---

### interactive_input

Setting `interactive_input: true` on an agent step allows the LLM to pause mid-execution and read input from the terminal. The Superpowers skills use this to ask clarifying questions before producing their output artifacts.

The `brainstorm` and `plan` phases set this flag:

```yaml
agents:
  brainstorm:
    interactive_input: true
    skill: superpowers:brainstorming
```

**Important:** Phases with `interactive_input: true` require a live TTY. Running them in a non-interactive environment (for example a CI pipeline with no terminal attached) will cause the skill to proceed without receiving any user responses. If you need to run the pipeline in CI, pre-create the affected output artifacts (`brainstorm.md`, `plan.md`) and let the idempotency check skip those phases.

---

### superpowers: skill namespace

Skill references in the pipeline YAML use the `superpowers:<skill-name>` prefix convention:

```yaml
skill: superpowers:brainstorming
skill: superpowers:writing-plans
skill: superpowers:subagent-driven-development
```

When conductor resolves a skill reference, it searches the directories registered via `skill_directories` for a subdirectory whose name matches `<skill-name>`. A reference of `superpowers:brainstorming` therefore resolves to `artifacts/skills/brainstorming/`.

The `superpowers:` prefix is a naming convention established by the Superpowers plugin ecosystem. Using it for bundled skills keeps the references consistent with the broader ecosystem while making clear that the skill originates from a Superpowers-compatible source.

---

### Idempotency

Each phase begins with a check for its output artifact. If the artifact already exists the phase exits immediately without invoking the LLM. This means:

- Re-running the full pipeline after a partial failure is always safe.
- You can pre-create any artifact manually (for example by writing your own `brainstorm.md`) and the pipeline will pick up from the next pending phase.
- Phases can be re-run individually when you want to regenerate a single artifact — delete the existing output file and invoke the phase directly.
