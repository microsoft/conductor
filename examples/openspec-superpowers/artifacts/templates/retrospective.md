# Retrospective: <change-name>

> Written: <YYYY-MM-DD> (after verify passed)
> Commit range: `<base-sha>..<head-sha>`
> Worktree: <path or "merged to main">

---

## 0. Evidence

> Quantitative front-matter — subsequent Wins / Misses bullets cite this section directly
> to avoid repeating [evidence: ...] on every line.
> Cold-write scenario (retro written some time after the cycle ends): `git log` + `tasks.md` +
> commit messages alone should be sufficient to reconstruct this section.

- **Commit range**: `<base-sha>..<head-sha>` (<n> commits)
- **Diff size**: <+X / -Y lines across N files>
- **Tasks done**: <x>/<y> (`grep -cE '^\s*- \[x\]' tasks.md` → x; regex allows sub-task indentation)
- **Active hours**: <estimate>
- **Subagent dispatches**: <count or "n/a">
- **New external dependencies**: <list with license + version, or "none">
- **Bugs encountered post-merge**: <count, one-line each, or "none">
- **OpenSpec validate state at archive**: <pass / fail / not-run>
- **Test coverage signal**: <e.g. jacoco %, pytest count, vitest count, or "n/a">

Commit chain (chronological):

```
<base-sha> <one-line summary>
...
<head-sha> <archive commit one-line>
```

---

## 1. Wins

- [evidence: <commit/file/test>] <description>

## 2. Misses

- 🔴 [blocking | evidence: ...] <description>
- 🟡 [painful  | evidence: ...] <description>
- 📌 [nit      | evidence: ...] <description>

## 3. Plan deviations

| Plan task | What changed | Why |
|-----------|--------------|-----|
| 1.2       | ...          | ... |

## 4. Skill / workflow compliance

| Skill                                            | Used |
|--------------------------------------------------|------|
| superpowers:brainstorming                        |      |
| superpowers:writing-plans                        |      |
| superpowers:using-git-worktrees                  |      |
| superpowers:subagent-driven-development          |      |
| (transitive) superpowers:test-driven-development |      |
| (transitive) superpowers:requesting-code-review  |      |
| superpowers:finishing-a-development-branch       |      |

> **Default expectation**: all ✓. Every skill is part of the schema's intentional design;
> skipping one is an exceptional situation. Any ✗ must be explained in the
> `### Deliberately Skipped Skills` subsection below with a root cause and prevention plan.

### Deliberately Skipped Skills

> Skipping a skill is a designed escape hatch, not a routine path. Each ✗ must answer
> the three questions below. An empty section (all green) is the expected state.

- **`<skill name>`**
  - **What was skipped**: <the entire skill, or a specific sub-step>
  - **Why this cycle**: <concrete cycle condition — vague reasons ("not needed", "too small",
    "no time", "blocked by external dep", "output looked off") are not acceptable;
    name the actual trigger (specific commit / log line / observed behavior)>
  - **How to prevent recurrence**: how should the next cycle in a similar situation avoid
    the same skip? Choose one:
    - `schema graph fix` — specify which section of schema.yaml to change
    - `skill description tightening` — specify which skill's frontmatter / instruction to update
    - `CLAUDE.md trigger` — specify which rule to add to the adopter CLAUDE.md
    - `scope-judgment rule` — specify how this cycle's scope should have been evaluated
    - `one-off — schema boundary case, no prevention possible` — but must explicitly state
      why it is a boundary (vague labels not accepted)

> **Relationship to §6 Promote candidates**: if multiple cycles skip the same skill with
> the same "How to prevent" answer, that pattern should be promoted to §6 to trigger a
> schema / skill PR directly — it must not accumulate into a "normal" exception.

## 5. Surprises

- <assumption that turned out wrong>

## 6. Promote candidates → long-term learning

Each candidate uses a `- [ ]` checklist item:

- Title: severity emoji (🔴/🟡/📌) + one-sentence learning
- `→ **Promote to** <destination>` (memory / CLAUDE.md / schema / skill / one-off)
- Two body lines (matching the superpowers feedback memory body schema):
  - `> **Why**: <reason; often a past incident or strong preference>`
  - `> **How to apply**: <when/where this guidance kicks in>`

Unchecked `- [ ]` items mean the candidate has not yet been promoted — they can carry
forward to the next cycle's retro for re-evaluation, or be kept as cross-cycle observations.

> **Carry-forward mechanism**: when writing the next cycle's retro, run:
> `grep -A 5 '^- \[ \]' openspec/changes/archive/*/retrospective.md`
> to retrieve prior unchecked candidates and decide per item: carry forward to this cycle's §6,
> promote in place, or mark stale and stop tracking.

Example:

- [ ] 🔴 **<short rule>** → **Promote to memory** (type: feedback)
  > **Why**: <past incident or strong preference that motivated this rule>
  > **How to apply**: <which file / cycle phase / decision moment this kicks in>

- [ ] 🟡 **<another candidate>** → **Promote to project CLAUDE.md** (`<path/to/CLAUDE.md>` section)
  > **Why**: ...
  > **How to apply**: ...

- [ ] 📌 **<third candidate>** → **One-off** (record only, do not promote)
  > **Why**: <why it doesn't generalize>
