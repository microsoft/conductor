# Verification Report

> This file is produced by the `openspec-verify-change` skill after the apply phase
> completes. It confirms that the implementation is consistent with specs, design, and tasks.
> Any failing check must be resolved in the corresponding artifact before re-running verify.

**Change**: `<change-name>`
**Verified at**: `YYYY-MM-DD HH:mm`
**Verifier**: `<who / which agent>`

---

## 1. Structural Validation (`openspec validate --all --json`)

- [ ] All items return `"valid": true`

**Result**:

```text
<paste summary output from openspec validate --all>
```

If any items failed, list them:

| Item | Type | Issues |
|---|---|---|
| — | — | — |

---

## 2. Task Completion (`tasks.md`)

- [ ] All `- [ ]` entries have become `- [x]`

**Incomplete tasks** (if any):

| Task | Reason not completed | Blocks archive? |
|---|---|---|
| — | — | — |

---

## 3. Delta Spec Sync State

For each capability directory under `openspec/changes/<name>/specs/`, compare against
`openspec/specs/<capability>/spec.md`:

| Capability | Sync status | Notes |
|---|---|---|
| — | ✓ Already synced / ✗ Needs sync / N/A | — |

---

## 4. Design / Specs Coherence Spot Check

Spot-check whether decisions in `design.md` are reflected in Requirements and Scenarios
in `specs/*.md`:

| Sample item | design.md description | specs counterpart | Drift |
|---|---|---|---|
| — | — | — | — |

**Drift warnings** (non-blocking):

- <list any found; or "none">

---

## 5. Implementation Signal

- [ ] No unstaged files in the worktree
- [ ] All relevant commits have been pushed

**Commit range** (if known): `<from-sha>..<to-sha>`

---

## 6. Front-Door Routing Leak Detector (warning, non-blocking)

Design output should not land in `docs/superpowers/specs/` — the brainstorm artifact's
output redirection routes it to `openspec/changes/<name>/brainstorm.md`.

Detection:

```bash
ls docs/superpowers/specs/*.md 2>/dev/null
```

- [ ] No files found, or any existing files are legitimate pre-schema-install remnants

**Leak list** (if any):

| File | Content captured in change? | Recommended action |
|---|---|---|
| — | — | — |

> Does not block archive. Any leaks produced by a new schema-installed cycle should be
> moved into `openspec/changes/<name>/brainstorm.md` or `design.md`, then deleted.

---

## 7. Deferred Manual Dogfood vs Automated Test Equivalence

For each `[~]` deferred manual dogfood / smoke task in plan.md, list the equivalent
automated test coverage. If no equivalent automated test exists, the item is a **real gap**
— not a legitimate deferral — and should be recorded in the retrospective Misses section.

| Deferred dogfood (plan §) | Equivalent automated test | Coverage assessment | Real gap? |
|---|---|---|---|
| e.g. §11.3 `compose up + curl /actuator/health` | `IntegrationApplicationTests` (Testcontainers) | Spring context boot + Flyway + main bean wiring | ❌ Already covered |
| — | — | — | — |

> **Interpretation rules**:
> - "Equivalent" = the automated test's assertion set is a superset of the manual dogfood's expected assertions
> - "Coverage assessment" = list the layers actually exercised (context / DB schema / wiring / HTTP path / etc.)
> - Any row with "Real gap = ✅" still allows Overall Decision PASS, but requires a follow-up entry in the retrospective

> **When this section may be left blank**: if plan.md has no `[~]` rows at all, this section
> does not need to be filled (blank = PASS).
> If plan.md contains any `[~]` rows, every one must be listed here — otherwise Overall
> Decision must be downgraded to FAIL.

---

## Overall Decision

- [ ] ✅ PASS — ready to proceed to finishing-a-development-branch and archive
- [ ] ⚠️ PASS WITH WARNINGS — may proceed but note: `<description>`
- [ ] ❌ FAIL — return to the failing artifact, fix it, then re-run verify

**Next step**:

<describe the next action>
