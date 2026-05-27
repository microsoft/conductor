# Plan: add-openspec-superpowers-readme

## Problem & Approach

The `examples/openspec-superpowers/` directory has no README, leaving users unable to understand what the pipeline does, how it differs from the base `examples/openspec/` pipeline, or how to run it. This plan creates `examples/openspec-superpowers/README.md` — a single documentation file with no code changes — modelled on the structure of `examples/openspec/README.md`.

All tasks are documentation-only. There is nothing to test in a TDD sense, but each micro-step has a clear acceptance check (visual review of the rendered section).

---

## Tasks

### Task 1.1 — Create the file and write the Purpose section
**Estimated time:** 3–4 minutes  
**File:** `examples/openspec-superpowers/README.md` (create new)

**Micro-steps:**
1. Create `examples/openspec-superpowers/README.md`.
2. Add a level-1 heading: `# OpenSpec Superpowers Pipeline`.
3. Write a 3–4 sentence introductory paragraph explaining that this is an *extended* OpenSpec pipeline that layers Superpowers skills (brainstorming, writing-plans, subagent-driven-development) on top of the base spec-driven lifecycle. Mention the nine-phase sequence: `brainstorm → propose → specs → design → tasks → plan → apply → verify → retrospective`.
4. State that the primary difference from the base pipeline is the addition of four phases (brainstorm, plan, verify, retrospective) and the use of interactive Superpowers skill sessions.

**Acceptance check:** Open the file; the first screen explains what the pipeline is and why it exists.

---

### Task 1.2 — Add the comparison table
**Estimated time:** 4–5 minutes  
**File:** `examples/openspec-superpowers/README.md`

**Micro-steps:**
1. Add a level-2 section: `## Comparison with the Base Pipeline`.
2. Insert a Markdown table with columns **Phase**, **examples/openspec/**, **examples/openspec-superpowers/**. Rows:
   - brainstorm | — (not present) | ✅ `phases/brainstorm.yaml` (interactive skill)
   - propose | ✅ `phases/propose.yaml` | ✅ `phases/propose.yaml` (extracts from brainstorm.md)
   - specs | ✅ `phases/specs.yaml` | ✅ `phases/specs.yaml`
   - design | ✅ `phases/design.yaml` | ✅ `phases/design.yaml` (reads brainstorm.md)
   - tasks | ✅ `phases/tasks.yaml` | ✅ `phases/tasks.yaml`
   - plan | — (not present) | ✅ `phases/plan.yaml` (interactive skill)
   - apply | ✅ `phases/apply.yaml` | ✅ `phases/apply.yaml` (TDD + code-review)
   - verify | — (not present) | ✅ `phases/verify.yaml`
   - retrospective | — (not present) | ✅ `phases/retrospective.yaml`
3. After the table, add a short paragraph noting that skills are bundled under `artifacts/skills/` (no plugin install required) and that `interactive_input: true` is used in the brainstorm and plan phases to enable live Q&A.

**Acceptance check:** The table renders cleanly in a Markdown viewer; all nine superpowers phases are visible; base-pipeline gaps are obvious at a glance.

---

### Task 1.3 — Add the Prerequisites section
**Estimated time:** 3 minutes  
**File:** `examples/openspec-superpowers/README.md`

**Micro-steps:**
1. Add a level-2 section: `## Prerequisites`.
2. List prerequisites as a bullet list:
   - `conductor` installed and on `PATH` — link to install commands (same as base README).
   - A configured provider: GitHub Copilot (`gh auth login`) or Anthropic Claude (`ANTHROPIC_API_KEY`).
   - No Superpowers plugin install required — skills are bundled under `examples/openspec-superpowers/artifacts/skills/` and wired automatically via `skill_directories`.
   - Repository cloned locally (all `conductor run` commands are run from the repository root).
3. Include copy-pasteable install commands for conductor (curl / PowerShell) and provider auth, mirroring the base README.

**Acceptance check:** A new user can satisfy all prerequisites without reading any other document.

---

### Task 1.4 — Add the Usage Commands section
**Estimated time:** 4 minutes  
**File:** `examples/openspec-superpowers/README.md`

**Micro-steps:**
1. Add a level-2 section: `## Usage`.
2. Add sub-section `### Full pipeline (start from scratch)` with the primary command:
   ```bash
   conductor run examples/openspec-superpowers/openspec-superpowers-pipeline.yaml \
     --input change="add-oauth-login" \
     --input idea="Add OAuth2 login with GitHub and Google providers"
   ```
3. Add sub-section `### With real-time dashboard` showing the `--web` variant.
4. Add sub-section `### Resume after a partial run`:
   ```bash
   conductor resume examples/openspec-superpowers/openspec-superpowers-pipeline.yaml
   ```
5. Add sub-section `### Skip brainstorm (proposal already exists)` with a note that every phase is idempotent — it skips itself if its output file already exists — so you can pre-create `brainstorm.md` or `proposal.md` manually and the pipeline will pick up from the next pending phase.
6. Add sub-section `### Run a single phase` showing how to invoke any individual phase YAML directly (example: `phases/brainstorm.yaml`).

**Acceptance check:** All commands are copy-pasteable; `--web` and `--resume` variants are covered; idempotency is mentioned.

---

### Task 1.5 — Add the Phase Walkthrough section
**Estimated time:** 10–12 minutes  
**File:** `examples/openspec-superpowers/README.md`

**Micro-steps:**
1. Add a level-2 section: `## Phase Walkthrough`.
2. Add a sequence diagram (ASCII or code-block) showing the nine-phase data flow:
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
3. Write one sub-section per phase (level-3 heading), in order:
   - **1. Brainstorm** (`phases/brainstorm.yaml`) — interactive `superpowers:brainstorming` skill session; explores context, asks one question at a time, proposes 2–3 design approaches, writes `brainstorm.md`. Uses `interactive_input: true`.
   - **2. Propose** (`phases/propose.yaml`) — extracts a concise `proposal.md` from `brainstorm.md` (skips full exploration since brainstorming already covered it).
   - **3. Specs** (`phases/specs.yaml`) — generates one `spec.md` per capability listed in `proposal.md` (concurrent, each to a distinct file).
   - **4. Design** (`phases/design.yaml`) — transforms the raw `brainstorm.md` decision log into a structured `design.md` (Context / Goals / Decisions / Risks / Migration).
   - **5. Tasks** (`phases/tasks.yaml`) — reads specs + `design.md` to produce `tasks.md` with checkbox-based implementation tasks.
   - **6. Plan** (`phases/plan.yaml`) — interactive `superpowers:writing-plans` skill session; decomposes `tasks.md` items into 2–5 minute TDD micro-steps with file paths, code snippets, test commands, and commit points. Writes `plan.md`. Uses `interactive_input: true`.
   - **7. Apply** (`phases/apply.yaml`) — implements all pending `[ ]` tasks sequentially; each task uses the `subagent-driven-development` skill (TDD + code-review); marks tasks `[x]` when done.
   - **8. Verify** (`phases/verify.yaml`) — script pre-checks confirm implementation evidence (commits + all tasks done); LLM runs 7 structured checks using `openspec-verify-change`; writes `verify.md`.
   - **9. Retrospective** (`phases/retrospective.yaml`) — gathers git log, diff stats, task metrics; LLM writes `retrospective.md` (§0 Evidence → §1 Wins → §2 Misses → §3 Deviations → §4 Compliance → §5 Surprises → §6 Promote candidates).
4. For each phase sub-section, include: purpose, input artifact(s), output artifact(s), and any notable behavior (interactive, skill used, idempotency note).

**Acceptance check:** All nine phases are present in order; each has a purpose and I/O listed; the sequence diagram renders correctly.

---

### Task 1.6 — Add the Notes section
**Estimated time:** 5 minutes  
**File:** `examples/openspec-superpowers/README.md`

**Micro-steps:**
1. Add a level-2 section: `## Notes`.
2. Add sub-section `### skill_directories wiring`:
   - Explain that `skill_directories` is a `runtime` field in each phase YAML that points to `../artifacts/skills` (relative to the phase file).
   - Explain that conductor loads all skill subdirectories found there and makes them available under the `superpowers:` namespace (e.g. `superpowers:brainstorming`, `superpowers:writing-plans`).
   - Confirm that no `superpowers` plugin install is needed — all skills are bundled in the repository.
3. Add sub-section `### interactive_input`:
   - Explain that `interactive_input: true` on an agent step allows the LLM to pause and read from the terminal during execution.
   - Note that the brainstorm and plan phases set this flag so the Superpowers skills can ask clarifying questions in real time.
   - Warn that running these phases in a non-interactive environment (e.g. CI with no TTY) will cause the skill to proceed without user responses.
4. Add sub-section `### superpowers: skill namespace`:
   - Explain the `superpowers:<skill-name>` prefix convention.
   - Describe how conductor resolves a skill reference: it looks up the skill name in any directory listed under `skill_directories`, matching the subdirectory name.
   - Give a concrete example: `superpowers:brainstorming` → `artifacts/skills/brainstorming/`.
5. Optionally add a brief `### Idempotency` note explaining that each phase checks for its output artifact before doing work — re-running the full pipeline after a partial run is always safe.

**Commit point:** After this step, the README is complete.

```bash
git add examples/openspec-superpowers/README.md
git commit -m "docs: add README for examples/openspec-superpowers

Covers purpose, comparison table, prerequisites, usage commands,
nine-phase walkthrough, and technical notes (skill_directories,
interactive_input, superpowers: namespace).

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

**Acceptance check:**
- `examples/openspec-superpowers/README.md` exists.
- All nine phases appear in the walkthrough in correct order.
- The comparison table has nine rows matching the pipeline YAML agents.
- The Notes section explains `skill_directories`, `interactive_input`, and `superpowers:` namespace.
- `conductor validate examples/openspec-superpowers/openspec-superpowers-pipeline.yaml` still passes (no YAML was modified).

---

## Verification Checklist

After completing all tasks, confirm:

- [ ] `examples/openspec-superpowers/README.md` exists and is readable
- [ ] Purpose section explains what the pipeline does and how it differs from `examples/openspec/`
- [ ] Comparison table lists both pipelines with all nine phases
- [ ] Prerequisites section lists all required tools and configurations
- [ ] Usage section contains copy-pasteable commands for full pipeline, `--web`, resume, and single-phase invocation
- [ ] Phase walkthrough section has nine sub-sections in order (brainstorm → retrospective)
- [ ] Notes section explains `skill_directories`, `interactive_input`, and `superpowers:` namespace
- [ ] No workflow YAML, schema, or source code was modified
- [ ] `conductor validate examples/openspec-superpowers/openspec-superpowers-pipeline.yaml` passes
