# Solution Design: External Workflow Friction — Minimal Fixes

**Status:** PROPOSED
**Source brainstorm:** [external-workflow-friction.brainstorm.md](external-workflow-friction.brainstorm.md)
**Author:** Lucio Tinoco (external contributor) + Copilot review
**Scope decision:** This plan implements an evidence-anchored subset of the brainstorm. The brainstorm proposed nine fixes across three clusters; this plan covers four. See §6 for what is explicitly deferred and why.

---

## 1. Problem Statement

A single external user attempting to run two real-world workflows against Conductor v0.1.16 hit seven failed runs before reaching a successful end-to-end execution. Post-mortem in the [companion brainstorm](external-workflow-friction.brainstorm.md) identified nine candidate issues. Pre-design code review confirmed that four of those issues are real, root-caused, and have a clear minimal fix; the remaining five either could not be reproduced from the cited evidence, propose speculative configurability, or address unobserved failure modes.

This plan ships only the confirmed four. Each fix is justified by a concrete failure mode from the brainstorm's timeline and is gated on a reproducing test that fails on `main` before the fix lands.

---

## 2. Goals and Non-Goals

### Goals

- **G1:** Agents emitting large JSON whose string fields contain triple-backticks no longer fall into parse-recovery loops (brainstorm Issue #1).
- **G2:** Workflows that place `type: script` steps inside a `parallel:` group fail at validate time with a clear message, rather than silently burning tokens (brainstorm Issue #7).
- **G3:** Users discover the "omit `output:` for prose/large JSON" pattern from documentation in the same place they learn about `output:` itself (brainstorm Issue #2, documentation-only slice).
- **G4:** Running `conductor run --web-bg` against a workflow that contains a `human_gate` (without `--skip-gates`) fails at validate time with a clear message, rather than crashing the detached child with `EOFError` (brainstorm Issue #8).

### Non-Goals

- **NG1:** No new `output_mode` field on `AgentDef`. The "raw" behavior already exists as "omit `output:`"; adding a synonym for absence is API bloat (Karpathy #2). Revisit only if the docs-only fix in G3 proves insufficient.
- **NG2:** No env-var resolver rewrite for `${VAR:-default}` with colons (brainstorm Issue #4). Pre-design inspection of [`config/loader.py` line 23](../../../src/conductor/config/loader.py#L23) shows the current regex's default group is `[^}]*`, which already accepts colons. The reported failure is more likely YAML quoting or PowerShell expansion. Pending a Python-only reproduction, no fix.
- **NG3:** No Windows path normalization in the script executor (Issue #3). Pending a deterministic reproduction independent of shell environment.
- **NG4:** No configurable parse-recovery retry budget (Issue #6). No observed user demand.
- **NG5:** No dashboard WebSocket keepalive, CLI gate-resolution command, dashboard auth, or webhooks (Issue #5). Out of scope by user decision; revisit in a follow-up plan.
- **NG6:** No `command:` vs `args:` expansion parity audit (Issue #9). Author labeled it anecdotal; convert to a one-line investigation task, not a planned fix.
- **NG7:** No work on the four "fragile patterns flagged" in the brainstorm. By the author's own statement, not observed in production.

---

## 3. Requirements

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `parse_json_output` in [`executor/output.py`](../../../src/conductor/executor/output.py) successfully extracts JSON from a fence-wrapped response whose string fields contain triple-backtick substrings. |
| FR-2 | `_extract_json` in [`providers/copilot.py`](../../../src/conductor/providers/copilot.py) behaves identically to FR-1. |
| FR-3 | `parse_json_output` continues to raise `ValidationError` for genuinely malformed JSON (no regression). |
| FR-4 | `config/validator.py` rejects workflows that reference a `type: script` agent from a `parallel:` group's `agents:` list. Error names both the parallel group and the offending agent. |
| FR-5 | `docs/workflow-syntax.md` contains a section titled "Choosing whether to declare `output:`" that describes the read-`<agent>.output.result` pattern for prose/large-JSON agents, with a cross-link from the `output:` reference. |
| FR-6 | `conductor run --web-bg <yaml>` exits non-zero, before forking the child, when the workflow contains any `type: human_gate` agent and `--skip-gates` is not set. Error lists the four documented options (use `--web`, add `--skip-gates`, remove gate, wait for follow-up CLI work). |

### Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR-1 | Every functional requirement is covered by at least one unit/integration test that fails on `main` before the fix lands and passes after. |
| NFR-2 | No test introduced by this plan depends on wall-clock timing, network access, subprocess output races, or live LLM calls. |
| NFR-3 | `make check` (lint + typecheck) and `uv run pytest` (full suite) both pass after each item lands. |
| NFR-4 | No file is modified outside the declared scope of its item (Karpathy #3). |

---

## 4. Solution Architecture

This plan introduces no new abstractions. Each item is a localized change inside an existing module.

### 4.1 Item 1 — Fence-extraction robustness

**Files touched:** [`src/conductor/executor/output.py`](../../../src/conductor/executor/output.py), [`src/conductor/providers/copilot.py`](../../../src/conductor/providers/copilot.py).

**Current behavior:** Both call sites use `r"```(?:json)?\s*\n?(.*?)\n?```"` (non-greedy). The first inner triple-backtick — common in JSON whose string values quote Markdown or shell snippets — closes the match prematurely. The extracted substring is invalid JSON, triggering parse recovery.

**Change:**

1. In `parse_json_output` (output.py): try `json.loads` directly first; if it fails, search for a fenced block with a **greedy** capture (`r"```(?:json)?\s*\n(.*)\n```"`). With `re.DOTALL` this greedy `.*` terminates at the last `` ``` `` in the string, which is the correction we want. If parse still fails, fall back to the existing first-`{`/`[` heuristic. The existing `json.loads` at the end is the canonical failure point — its error message is unchanged.
2. In `_extract_json` (copilot.py:1100–1118): mirror the same regex change. The existing brace-match fallback already in this method stays as the final attempt. The mirror is verified by visual diff against output.py and by the executor tests in §5 (both providers route their fallback path through `parse_json_output`).

**Why this shape:** the doc proposed a brace-balanced walker. That's ~30 LOC of stateful parsing for failures no one has hit yet. Greedy regex + the existing brace fallback handles every case in the brainstorm timeline (Karpathy #2).

### 4.2 Item 2 — Validator rejects scripts inside parallel groups

**Files touched:** [`src/conductor/config/validator.py`](../../../src/conductor/config/validator.py).

**Current behavior:** `_execute_parallel_group` ([workflow.py:3520](../../../src/conductor/engine/workflow.py#L3520)) calls `_get_executor_for_agent` for every member, which returns the LLM executor regardless of `agent.type`. Script agents inside parallel groups are dispatched to the LLM provider with no prompt, burning tokens on nothing.

**Change:** in the existing validator pass, iterate parallel groups; for each agent name in `group.agents`, resolve the `AgentDef`; if `agent.type == "script"`, raise `ValidationError` with the suggestion to move the step to a sequential chain. ~15 LOC.

**Why validator and not engine:** failing fast at load is cheaper and clearer than handling the case at dispatch. The brainstorm proposed both options A (validator) and B (full support); A is the minimal fix for the observed failure (Karpathy #2). B can come later if real users ask.

### 4.3 Item 3 — Documentation for the omit-`output:` pattern

**Files touched:** [`docs/workflow-syntax.md`](../../../docs/workflow-syntax.md) and a cross-link from [`docs/configuration.md`](../../../docs/configuration.md) if `output:` is referenced there.

**Change:** add a section "Choosing whether to declare `output:`" that:

- States the trade-off in one sentence: declaring `output:` injects a schema instruction to the model AND parses the response as structured JSON.
- Lists the two clear cases:
  - **Declare `output:`** when the agent emits small, strictly-structured JSON whose individual fields will be referenced downstream.
  - **Omit `output:`** when the agent emits prose, Markdown, or large/nested JSON. Downstream agents read `<agent>.output.result`.
- Shows the exact YAML for both side by side.
- Cross-links from the `output:` reference subsection.

**No code change.** Verification is by review of the diff.

### 4.4 Item 4 — Validate `--web-bg` against `human_gate` workflows

**Files touched:** [`src/conductor/cli/run.py`](../../../src/conductor/cli/run.py) (the `run` command entry point that branches into `bg_runner.launch_background`).

**Current behavior:** `--web-bg` forks the child via `bg_runner.launch_background`. The child detaches stdin. When the workflow reaches a `human_gate`, `Prompt.ask()` reads `sys.stdin` and raises `EOFError`, crashing the detached process.

**Change:** before calling `launch_background`, inspect the loaded `WorkflowConfig`. If `skip_gates` is false AND any agent in `config.agents` has `type == "human_gate"`, abort with `typer.BadParameter` (or equivalent) and print the four-option message from the brainstorm:

```
--web-bg is incompatible with workflows that contain human_gate steps
because the detached process has no stdin to prompt on.

Options:
  1. Use --web (foreground) instead of --web-bg
  2. Add --skip-gates to auto-accept the first option
  3. Remove human_gate steps from the workflow
  4. Wait for CLI gate-resolution support (planned follow-up)
```

The same check applies to `resume --web-bg` for symmetry with the run/resume parity rule documented in `AGENTS.md`. ~20 LOC including the parity wiring on `resume`.

**Why pre-fork:** crashing the child after fork loses observability; pre-fork validation produces a single clear error visible on the user's terminal.

---

## 5. Test Strategy

All tests use existing fixtures and helpers (`tests/conftest.py`, `tests/test_cli/` Typer `CliRunner` pattern, `tests/test_executor/test_output.py` style). No new test infrastructure.

### 5.1 Item 1 tests — `tests/test_executor/test_output.py`

| Test | Pre-fix expected | Post-fix expected |
|---|---|---|
| `test_parse_json_with_triple_backticks_inside_string` — input: ` ```json\n{"code": "use ```fenced``` blocks", "n": 1}\n``` ` | Raises `ValidationError` | Returns `{"code": "use ```fenced``` blocks", "n": 1}` |
| `test_parse_json_fence_with_prose_around_it` — `"Here is the result:\n\`\`\`json\n{...}\n\`\`\`\nDone."` | Passes today | Passes (regression guard for the greedy-match change) |
| `test_parse_json_malformed_raises` — `"{not valid"` | Raises | Raises (unchanged message) |

The 80 KB-payload-without-backticks test from an earlier draft was dropped: size alone never reproduced a failure in the brainstorm timeline, and the triple-backtick test already exercises the fix. The raw-no-fence and malformed cases are kept because they cover branches the greedy change touches.

The copilot.py mirror in `_extract_json` is verified by visual diff against output.py (same regex change in the same fallback position) plus the executor tests above, which exercise the shared `parse_json_output` code path both providers fall through to. No separate provider-specific test file is introduced (Karpathy #2: no abstractions or files for single-use code).

### 5.3 Item 2 tests — `tests/test_config/test_validator.py`

| Test | Pre-fix expected | Post-fix expected |
|---|---|---|
| `test_parallel_group_rejects_script_agents` — workflow with a `parallel:` group whose `agents:` contains a script-type agent | Loads silently | Raises `ValidationError` naming both the group and the script agent |
| `test_parallel_group_accepts_agent_type_agents` — workflow with normal parallel agents | Passes today | Passes |

### 5.4 Item 3 verification

No automated test. Acceptance criterion: a reviewer can answer "should I declare `output:` for an agent producing 80 KB JSON?" by reading one section of `workflow-syntax.md`. Verified during PR review.

### 5.5 Item 4 tests — `tests/test_cli/test_bg_runner.py`

**Pre-fix behavior observed on 2026-05-26 against current `main`:** running `conductor run --web-bg <gate-yaml>` produces exit code 1 with the parent CLI message `"Background process exited immediately with code 1. Check logs or run without --web-bg for details."` The child has already forked and crashed (presumably on `EOFError` from `Prompt.ask`) before the parent emits this message. The error never mentions `human_gate`, `--web-bg` incompatibility, or `--skip-gates`.

| Test | Pre-fix expected (observed) | Post-fix expected |
|---|---|---|
| `test_web_bg_with_human_gate_aborts_before_fork` — `CliRunner` invokes `conductor run --web-bg <gate-yaml>` (no `--skip-gates`) | Exit code 1; stderr contains `"Background process exited immediately"`; child has already forked (PID file may briefly exist) | Exit code != 0; stderr contains the four-option message naming `human_gate`; no fork attempted (no PID file ever created) |
| `test_web_bg_with_human_gate_and_skip_gates_proceeds` — same workflow with `--skip-gates` | N/A — proves we don't over-block | Reaches the launch path (verified by mocking `launch_background` and asserting it was called) |
| `test_resume_web_bg_with_human_gate_aborts_before_fork` — parity check for `resume` | Same EOFError-after-fork story | Same shape as the run-side test |

**Non-flakiness:** all CLI tests run via `CliRunner` in-process with `launch_background` mocked. No subprocess, no sockets, no timing. The PID-file check is filesystem-only and deterministic in the test's `tmp_path`.

### 5.6 Cross-cutting verification

After each item:

1. `make check` — lint + typecheck.
2. `uv run pytest -m "not performance"` — full suite minus perf tests.

The canonical "did we actually fix the lived problem" check (re-running the brainstorm's Phase A workflow with a fence-wrapped, backtick-containing payload) is an author acceptance step in §7, not a CI test, because it requires live LLM calls and would violate NFR-2.

---

## 6. Explicitly Deferred (with reason)

This section is the asymmetric mirror of §2's Non-Goals — it names the brainstorm items NOT in this plan and the evidence threshold that would justify reopening them.

| Brainstorm item | Reason for deferral | Threshold to reopen |
|---|---|---|
| Issue #2 `output_mode` field | Docs-only fix (Item 3 here) likely sufficient; field is a synonym for "absence of `output:`" | New user trips the same pattern after Item 3 ships |
| Issue #3 Windows path separator | No Python-only reproduction; likely shell/YAML environmental | Deterministic repro that doesn't depend on PowerShell or YAML quoting |
| Issue #4 env-var regex rewrite | Inspection shows current regex already handles colons in defaults (the default group is `[^}]*`); the reported failure is more likely YAML quoting or PowerShell variable expansion, not the resolver | Failing pytest case calling `resolve_env_vars` directly with the reported input |
| Issue #5 dashboard keepalive + CLI gate cmd + webhooks + auth | Out of scope per user decision on 2026-05-26 | Separate follow-up plan |
| Issue #6 configurable retry budget | No observed demand | Real user request with use case |
| Issue #9 `command:` vs `args:` parity | Author labels anecdotal/unconfirmed | Failing parity test demonstrating divergence |
| Fragile patterns (unbounded events, parallel race, identical recovery prompt, dashboard startup timeout) | Author says "not yet hit in production" | An actual production incident |

---

## 7. Execution Sequence

```
Phase 1 (independent, parallelizable as separate PRs):
  Item 1 → write repro test (must fail on main) → fix → tests pass → make check
  Item 2 → write repro test (must fail on main) → fix → tests pass → make check
  Item 3 → docs PR, code review only

Phase 2 (depends on stable main from Phase 1):
  Item 4 → write repro test (must fail on main) → fix → tests pass → make check
```

Order is partial — Items 1, 2, 3 are independent. Item 4 follows for review-cluster cohesion, not technical dependency.

**Author acceptance step** (once, before opening the PR cluster, not part of CI): re-run the brainstorm's Phase A workflow with the original `output:`-on-synthesizer setup against a real Copilot session. Pre-Items: trips parse recovery loop. Post-Items: succeeds in one shot. This is gut-check validation, not a gate.

---

## 8. Open Questions

1. **`typer.BadParameter` vs custom `ConfigurationError` for Item 4?** Need to read existing CLI error patterns before deciding. Resolve during implementation; not blocking.

Item 4 changes both `run` and `resume` per the run/resume parity rule in `AGENTS.md`; both get tests by necessity, not as an open question.
