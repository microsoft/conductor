# Retrospective: add-openspec-superpowers-readme

> Written: 2026-05-27 (after verify passed)
> Commit range: `799e5f85..4c4eb2d`
> Worktree: merged to main

---

## 0. Evidence

- **Commit range**: `799e5f85..4c4eb2d` (1 commit)
- **Diff size**: +345 / -0 lines across 1 file
- **Tasks done**: 6/6 (`tasks.md` — all items marked `[x]`)
- **Active hours**: ~0.5h (single documentation pass)
- **Subagent dispatches**: n/a
- **New external dependencies**: none
- **Bugs encountered post-merge**: none
- **OpenSpec validate state at archive**: pass (documentation-only; no YAML modified; `conductor validate examples/openspec-superpowers/openspec-superpowers-pipeline.yaml` confirmed passing)
- **Test coverage signal**: n/a (documentation-only change)

Commit chain (時序):

```
799e5f85 <base — prior state before this change>
4c4eb2d  docs: add README for examples/openspec-superpowers
```

---

## 1. Wins

- [evidence: `4c4eb2d`, `examples/openspec-superpowers/README.md`] All 6 tasks completed in a single commit with zero rework — the structured plan (tasks 1.1–1.6) translated directly into the final document with no iteration needed.
- [evidence: `examples/openspec-superpowers/README.md` lines 19–29] The comparison table decision (D2 from design.md) paid off immediately: nine-row table surfaces base-pipeline gaps at a glance without any prose explanation.
- [evidence: verify.md §4, all D1–D5 rows show "None" gap] Every design decision mapped cleanly to a spec requirement — zero coherence drift between design, specs, and implementation, which is the ideal outcome for a documentation-only change.
- [evidence: tasks.md, 6/6 `[x]`] The micro-step breakdown in `plan.md` (2–5 min steps with explicit acceptance checks) eliminated ambiguity about what "done" meant for each section, enabling clean single-pass execution.
- [evidence: verify.md §5] Only `examples/openspec-superpowers/README.md` was added — no unintended side-effects, no YAML drift, no stray file modifications.

---

## 2. Misses

- 📌 [nit | evidence: verify.md §6] A pre-existing untracked file `docs/superpowers/specs/2026-05-26-git-changelog-design.md` surfaced as a routing-leak candidate during verify. It belongs to a prior unrelated cycle and was correctly flagged as non-blocking, but its presence indicates that prior cycle artifacts are not being cleaned up promptly. This added a verification detour.
- 📌 [nit | evidence: `openspec/changes/add-openspec-superpowers-readme/verify.md` §3] Delta spec sync was deferred to archive time rather than being performed before verify. For documentation-only capabilities this is acceptable, but the verify report had to explicitly document the deferral, adding noise to an otherwise clean verification run.
- 🟡 [painful | evidence: plan.md Task 1.5] The phase walkthrough section (Task 1.5) was estimated at 10–12 minutes and was by far the most time-consuming task — covering all nine phases with purpose, inputs, outputs, and notable behavior for each. The estimate was accurate, but the sheer density of the section means any future update to the superpowers pipeline YAML risks making this section stale. No automated staleness detection exists.

---

## 3. Plan deviations

| Plan task | What changed | Why |
|-----------|--------------|-----|
| 1.4 (Usage commands) | Idempotency explanation kept brief in Usage section and expanded in Notes §Idempotency sub-section | Avoids duplication between Usage and Notes; design D4 grouped cross-cutting concerns in Notes |
| 1.5 (Phase walkthrough) | ASCII sequence diagram was simplified (no box-drawing art) to a plain indented flow block | Markdown rendering of ASCII box art is fragile across viewers; plain indented arrows render consistently everywhere |
| 1.6 commit message | Commit message body matches plan.md exactly | No deviation — noted here for completeness |

---

## 4. Skill / workflow compliance

| Skill                                            | Used |
|--------------------------------------------------|------|
| superpowers:brainstorming                        | ✓    |
| superpowers:writing-plans                        | ✓    |
| superpowers:using-git-worktrees                  | ✗    |
| superpowers:subagent-driven-development          | ✗    |
| (transitive) superpowers:test-driven-development | ✗    |
| (transitive) superpowers:requesting-code-review  | ✗    |
| superpowers:finishing-a-development-branch       | ✓    |

### Deliberately Skipped Skills

- **`superpowers:using-git-worktrees`**
  - **What was skipped**: Entire skill — no worktree was created; work was done directly on `main`.
  - **Why this cycle**: The change is documentation-only (1 file, 345 lines, 0 code changes). The concrete trigger was that `openspec-ff-change` and the apply phase both operate on `main` when no code isolation is needed; the verify report (`verify.md §5`) confirms only one file was added with no risk of conflicting with in-flight work on other branches. A worktree would have added checkout/merge overhead with zero isolation benefit.
  - **How to prevent recurrence**: `scope-judgment rule` — documentation-only changes (no `.py`, `.ts`, `.yaml` source modifications; diff entirely in `docs/` or `examples/*/README.md`) are a legitimate boundary case where worktree isolation provides no value. This rule should be encoded in the superpowers-bridge schema as a `skip_if` condition on `using-git-worktrees` when `change_type == "documentation"`. Until the schema encodes it, the judgment call is defensible but should be documented explicitly in plan.md rather than left implicit.

- **`superpowers:subagent-driven-development`**
  - **What was skipped**: Entire skill — no subagent was dispatched for implementation.
  - **Why this cycle**: The apply phase for a documentation-only change has no TDD loop, no code under test, and no review gate that requires a separate agent context. The concrete trigger was tasks.md tasks 1.1–1.6 each resolving to a direct file write with an immediate visual acceptance check rather than a compile/test/commit cycle. Dispatching a subagent to write Markdown would have added latency and context overhead without improving output quality.
  - **How to prevent recurrence**: `scope-judgment rule` — `subagent-driven-development` applies when tasks involve code changes with test feedback loops. For documentation-only tasks where the acceptance check is "file exists and reads correctly," the skill is inapplicable by definition. This boundary should be noted in the skill's frontmatter as a `not-applicable-when: change_type == "documentation"`  condition.

- **`(transitive) superpowers:test-driven-development`**
  - **What was skipped**: Entire skill — no tests were written or run.
  - **Why this cycle**: Documentation-only change. No source code was modified; there are no test fixtures for Markdown content in this repository. The concrete trigger was the explicit statement in `design.md` Goals/Non-Goals: "Adding automated tests for documentation content" is a listed Non-Goal.
  - **How to prevent recurrence**: `scope-judgment rule` — TDD is inapplicable when the change produces no executable artifact. Same boundary condition as `subagent-driven-development` above.

- **`(transitive) superpowers:requesting-code-review`**
  - **What was skipped**: Entire skill — no code review was requested.
  - **Why this cycle**: Documentation-only change with a single commit adding one Markdown file. The concrete trigger was that verify.md performed a structured 7-check review (design coherence, task completion, delta spec sync state, implementation signal, routing leak detection) which is the semantic equivalent of a code review for documentation. No human reviewer flag was raised in verify.
  - **How to prevent recurrence**: `skill description tightening` — the `requesting-code-review` skill description should clarify whether it applies to documentation-only changes or only to source code changes. If the intent is "always request review," the verify phase's structured checks should be listed as a valid substitute to avoid ambiguity.

---

## 5. Surprises

- Assumed the comparison table would require careful phase-by-phase research of both pipeline YAMLs; in practice the brainstorm.md decision chain had already enumerated all differences, making the table a direct transcription rather than a research task.
- Assumed Task 1.5 (phase walkthrough) would be the hardest section to get right on a first pass; the structured plan micro-steps (purpose / inputs / outputs / notable behavior per phase) made it mechanical rather than creative, which was faster than expected.
- The verify phase surfaced a pre-existing untracked file (`docs/superpowers/specs/2026-05-26-git-changelog-design.md`) as a routing-leak candidate — unexpected overhead for a documentation change, since routing-leak detection is primarily designed for code changes that might write to the wrong output directory.

---

## 6. Promote candidates → long-term learning

- [ ] 🟡 **Documentation-only changes should explicitly skip worktree + subagent skills in the schema** → **Promote to schema** (`openspec-schemas/superpowers-bridge/schema.yaml` — add `skip_if: change_type == "documentation"` on `using-git-worktrees`, `subagent-driven-development`, `test-driven-development`, `requesting-code-review`)
  > **Why**: This cycle required four "Deliberately Skipped Skills" entries in the retrospective, each with the same root cause: the skills assume code changes. Without schema-level `skip_if` conditions, every documentation cycle will produce the same four skip entries, adding retrospective noise and creating a false impression of non-compliance.
  > **How to apply**: When `openspec-propose` or `openspec-ff-change` classifies a change as `documentation-only` (no `.py`/`.ts`/`.yaml` source modifications), the schema should suppress the four code-focused skills from the compliance table automatically.

- [ ] 📌 **Pre-existing untracked artifacts from prior cycles create false routing-leak positives in verify** → **One-off** (記錄即可,不 promote)
  > **Why**: The `docs/superpowers/specs/2026-05-26-git-changelog-design.md` file is a one-off artifact from a specific prior cycle (git-changelog-design) that was never cleaned up. This does not generalize — it is a specific cleanup debt item, not a systemic pattern worth encoding in schema or skills.

- [ ] 🟡 **Phase walkthrough sections in READMEs will drift from pipeline YAML as phases evolve** → **Promote to project CLAUDE.md** (`AGENTS.md` — add a note in the `examples/openspec-superpowers/` section)
  > **Why**: `examples/openspec-superpowers/README.md` now contains a detailed nine-phase walkthrough that must stay synchronized with the actual phase YAML files. There is no automated check. Any PR that renames, adds, or removes a phase in the superpowers pipeline YAML should trigger a README update.
  > **How to apply**: Add to `AGENTS.md` under the examples section: "When modifying any phase YAML in `examples/openspec-superpowers/phases/`, update `examples/openspec-superpowers/README.md` phase walkthrough to match."
