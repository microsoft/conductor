# External Workflow Friction — Findings and Fix Brainstorm

> **Status:** brainstorm — updated 2026-05-27 with validation evidence against Conductor v0.1.17.
> **Author:** Lucio Tinoco (external user contributor)
> **Source of evidence:** real-world execution of the `workiq-coach` Conductor workflow set, then a three-phase validation pass against v0.1.17. See *Validation results* below.
> **Audience:** Conductor maintainers, and any contributor who'd help upstream these fixes.

---

## Why this document exists

A single user trying to run two non-trivial workflows against real WorkIQ + Copilot + Anthropic data hit nine distinct rough edges in Conductor v0.1.16. Each one looked like a one-off the first time it appeared; each turned out to be a real bug or design fragility — or, in two cases, an analysis error on my part.

This document records what was learned so the cost of that session pays back across the codebase rather than being a private tale. It is **not** a request to fix everything at once — it's a structured analysis with a phased implementation plan that maintainers can carve into PR-sized work, reshape, reject, or defer.

The unifying theme: each issue produces a **silent or confusing failure mode** that consumes minutes-to-hours of an external user's time before the actual cause becomes diagnosable. The fixes are mostly about **earlier and clearer errors**, **safer defaults**, and **eliminating brittle implicit behaviors**.

Since the first revision of this doc, Conductor v0.1.17 has shipped fixes that **fully address** three of the nine original items (#1 small-case, #7, #8), **partially address** two more (#5, #6), leave **two open** (#2, #3), and turn out to **not be bugs** for two more (#4 and #9 — closer inspection during validation showed I was wrong about both). A **tenth** issue surfaced during validation (#10, agent-level outer retry). Each section below now carries a status badge reflecting the v0.1.17 state.

---

## Executive summary

| # | Issue | v0.1.17 status |
|:---:|---|:---|
| 1 | Fence-extraction regex non-greedy | ⚠️ **PARTIAL** — greedy regex shipped at `output.py:120-122`; works on small responses (Phase 2 ✓), fails on full-scale ~80 KB responses (Phase 3 ✗). Issue #2 still needed. |
| 2 | `output_mode: raw \| envelope` field on AgentDef | ❌ **STILL OPEN** — Phase 3 empirically validated this as essential, not just nice-to-have. Headline upstream contribution candidate. |
| 3 | Windows subprocess path normalization | ❌ Still open — workaround: set `$env:PYTHON` with backslash form. |
| 4 | `${VAR:-DEFAULT}` colon-in-default parser | ❎ **DEBUNKED** — empirical test against v0.1.17 (and v0.1.16) shows the regex correctly parses `${PYTHON:-C:/Python314/python.exe}`. My original analysis was wrong. This issue does not exist; the section is preserved below as a note for future readers. |
| 5 | Dashboard / gate-resolution resilience | ⚠️ **PARTIAL** — `web/server.py` now handles `gate_response` / `dialog_message` / `iteration_limit_response` messages and has disconnect-event handling; PR #202 brought max-iterations gate resolution into the dashboard. No CLI `conductor gate-respond` command yet. |
| 6 | Per-agent retry budget config | ⚠️ **PARTIAL** — `max_parse_recovery_attempts` is now on an internal `_retry_config` (configurable in code), but not surfaced in the YAML schema. |
| 7 | Reject `type: script` in `parallel:` at validate | ✅ **SHIPPED** — `config/validator.py:489-492` rejects with: *"Script steps cannot be used in parallel groups."* |
| 8 | `--web-bg` + `human_gate` clear error | ✅ **SHIPPED VERBATIM** — `cli/app.py:158-191` defines `_abort_web_bg_if_human_gate` with essentially the brainstorm's proposed error message word-for-word, four-option list intact. |
| 9 | `command:` and `args:` expansion parity | ❎ **NOT A BUG** — confirmed both use the same `self.renderer.render()` path in `executor/script.py`. No divergence to fix. |
| 10 | Agent-level outer retry budget (new finding) | ❌ **STILL OPEN** — Phase 3 surfaced this: when parse-recovery 5/5 exhausts, Conductor retries the whole agent 3 more times. Multiplies sunk cost on doomed agents. Should be capped/configurable. |

**Headline remaining upstream priority:** Issue #2 (the `output_mode` field). This is the architectural fix that the regex patch (Issue #1) cannot substitute for. Phase 3 verified empirically.

**Headline upstream wins already in v0.1.17:** Issues #1 (small-case), #5 (partial), #7, #8. Cluster A from the first revision of this doc is largely shipped.

---

## Source of evidence: the workiq-coach session

A condensed timeline of what we hit, run-by-run, because the *narrative* of how these issues compound is more convincing than the issue list alone. Each numbered issue below references where in this timeline it appeared.

| Run | Setup | Outcome | Time spent |
|---|---|---|---|
| #1 (Phase A) | Default workflow, opus synthesizer | Opus reached for PowerShell to construct JSON; hit Copilot's tool-output ceiling; corrupted its own file; recovery loop. *Cancelled.* | ~30 min |
| #2 (Phase A) | Strengthened synthesizer prompt forbidding tool use | Opus emitted JSON wrapped in ` ```json ` fence; Conductor's fence regex failed; Parse Recovery 1–5; cancelled. *(Issue #1)* | ~25 min |
| #3 (Phase A) | Switched to haiku synthesizer; ran with `--web` | Workflow reached the `human_gate` cleanly. Dashboard parked for ~14 hours overnight. Dashboard server socket died silently; gate became unreachable from browser; no CLI fallback. Process killed. *(Issue #5)* | ~14 hr wallclock |
| #4 (Phase A) | Used `--skip-gates` flag | Same synthesizer parse recovery as run #2, exhausted the 30-min `timeout_seconds` budget. *(Issue #1 + workflow time budget too tight to absorb retries.)* | ~30 min |
| #5 (Phase A) | Removed gates from YAML; bumped timeout to 60 min | Synthesizer produced output, validator rejected for `broadSettings.examples` shape (downstream schema, not Conductor's fault), revise loop didn't converge. Killed. | ~15 min |
| #6 (Phase A) | Extended downstream schema | Same fence-extraction failure as runs #2/#4 — synthesizer hit parse recovery, timed out again. *(Issue #1, deterministically.)* | ~20 min |
| #7 (Phase A) | **Refactored synthesizer to remove `output:` schema** (read `.output.result` instead). | Synthesizer produced clean ~80 KB JSON in one shot, no parse recovery. Validator failed on a different shape issue. Manual extraction from checkpoint, manual stringification of one field, manual write — and observations.json was on disk. *(Issue #2 was the root cause all along.)* | ~10 min |

For Phase B (which already used the no-`output:`-schema pattern from the start), everything worked first try in ~10 minutes total.

**Net loss to friction across runs #1–#7: roughly 3 hours of wall-clock LLM execution + several hours of human diagnostic time, almost all attributable to issues #1, #2, and #5.**

The other six issues (#3, #4, #6, #7, #8, #9) surfaced in supporting fashion — each cost minutes, and each is independently a real bug (except #4 and #9, which the validation pass below proved are not actually bugs).

---

## Validation results (2026-05-27, against Conductor v0.1.17)

After v0.1.17 shipped, a three-phase validation pass tested whether the fixes addressed the original failure modes. Summary above; details:

### Phase 1 — Static survey

Grep + read each cited file:line in v0.1.17. Result: the matrix in the executive summary. Five issues had relevant code changes (three full ships, two partials). Two issues turned out to be analysis errors. Two are still open.

Most striking find: `cli/app.py:158-191` contains the **exact error message** I proposed in this brainstorm for Issue #8 — four-option list intact down to the "Wait for CLI gate-resolution support (planned follow-up)" line. Strong signal that the doc was read and acted on.

### Phase 2 — Minimal repro for Issue #1 (greedy regex)

A 5-line repro YAML with one agent declaring `output: { content: string }` and a prompt that asks the model to emit a string containing triple-backticks. Against v0.1.17:

- Workflow completed in **14.35 seconds**
- **Zero Parse Recovery events**
- Output verified: `"contains_backticks": "True"` (the test was real, not degenerate)
- Cost: $0.0053

The greedy regex at `output.py:120-122` works as designed for small-case nested fences. This **confirms Issue #1 was correctly fixed** for the small-response case.

### Phase 3 — End-to-end against the original workiq-coach config

Restored `phase-a.yaml`'s synthesizer to the **original** `output: { observations_json, pillar_summary }` schema — the config that broke 5+ times against v0.1.16 — and ran against v0.1.17 with `budget_usd: 2.00` in `enforce` mode as a safety net.

Result: **synthesizer still parse-loops at full scale.** Same failure mode as v0.1.16 runs #2/#4/#6:

- 8 workiq_runner agents completed cleanly (each with one transient parse recovery, all recovered)
- Synthesizer step started; first attempt's response hit Parse Recovery 1/5 → 2/5 → 3/5 → 4/5 → 5/5
- Conductor then surfaced a new behavior I hadn't seen before: an **agent-level outer retry budget** (3 attempts total). After parse recovery 5/5, the message was `Agent 'synthesizer' attempt 1/3 failed: Failed to parse structured output from agent response`, and the inner parse-recovery cycle restarted from 1/5 inside outer attempt 2/3.
- Killed at ~50 min runtime, mid outer-attempt 2/3
- Budget enforcement did **not** fire — but only because actual spend stayed under $2 (~$0.70-0.80 estimated). The budget feature is working correctly; haiku is just too cheap for $2 to brake a retry storm of this size.

**Conclusion:** Issue #1's greedy regex fix is verified to work at small scale but **does not address the full-scale failure**. Something else about the synthesizer's ~80KB response trips Conductor's parser — likely model truncation crossing the fence boundary, or prose interleaved with the JSON. Issue #2 (`output_mode: raw | envelope`) remains the architectural fix that this validation pass empirically demands.

Total cost of validation: ~$0.81 across Phase 1 (free), Phase 2 ($0.005), and Phase 3 (~$0.80).

---

## Issue 1 — Fence-extraction regex breaks on large or nested JSON

> **Status (v0.1.17):** ⚠️ PARTIAL. Greedy regex shipped at `executor/output.py:120-122` with a comment that quotes this brainstorm's failure mode. Phase 2 verified the fix on a small response. Phase 3 verified the fix is **insufficient** at full scale (~80 KB synthesizer output). Issue #2 below remains the architectural fix.

### Symptom

When an agent declares an `output:` schema, the provider injects an instruction to "respond with JSON matching this schema." Models — particularly opus and gpt-5.2, and sometimes haiku — wrap large JSON responses in ` ```json ... ``` ` fences. Conductor's extraction regex is non-greedy: it matches the first ` ``` ` it sees after the opening fence. If the JSON content contains backticks (code snippets in `your_excerpt` strings, etc.), or simply if the response is large enough that the model breaks it across the fence boundary, the regex truncates and the extracted JSON is invalid.

This triggers **Parse Recovery 1/5, 2/5, …** with each retry round-tripping the LLM at full prompt size. After 5/5, the agent gives up and either falls into a downstream revise loop or fails the workflow.

### Location

- `src/conductor/executor/output.py:120` — primary fence-extraction regex
- `src/conductor/providers/copilot.py:1102` — provider-side fallback fence-extraction
- `src/conductor/providers/copilot.py:679-746` — parse recovery loop
- `src/conductor/providers/claude.py` — provider parity required for any change

### Cause

A non-greedy regex along the lines of:

```python
re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
```

The `.*?` matches as little as possible, so the regex always closes at the **first** ` ``` ` after the opening fence. Triple-backticks anywhere inside the JSON content (legitimate or not) split the match prematurely.

### Fix proposal

Two viable approaches, not mutually exclusive:

1. **Greedy match anchored to the last fence on the line** — change to `re.search(r"```(?:json)?\s*\n(.*)\n```\s*$", response, re.DOTALL)` so the regex closes at the *last* closing fence in the response. Add a `json.loads()` sanity check on the extracted content; if parse fails, fall back to:
2. **Brace-balanced extraction as a fallback** — find the first `{` (or `[`), walk the response character-by-character respecting string escapes, return the substring up to the matching closing brace. Bypasses the regex entirely for the common case where the model emits JSON with any prose around it (with or without fences).

The brace-balanced approach is more robust but ~30 LOC of careful code (must handle string escapes, brackets inside strings, etc.). The greedy-regex approach is ~3 LOC and fixes 90% of real cases.

**Recommendation:** ship the greedy regex first as a quick win; add the brace-balanced extractor as a follow-up for the long tail.

> *Update 2026-05-27 — Upstream shipped the greedy-regex change at `output.py:120-122`. The accompanying code comment reproduces this brainstorm's failure-mode description ("closes at the LAST ` ``` ` in the response, not the first inner ` ``` ` which may appear inside a JSON string field"). Phase 2 confirmed it works on small inputs; Phase 3 showed it does **not** scale (see "Update from Phase 3 validation" below). The brace-balanced extractor may still be worth adding, but the upstream priority should be Issue #2.*

### Blast radius

- All workflows with `output:` schemas and large structured responses
- Workflows where any string field in the JSON could contain triple-backticks (e.g. coaching observations quoting Markdown, code examples in prose, file paths)
- Both providers (copilot + claude) need the same fix per the [Provider Parity](../../AGENTS.md#provider-parity) rule

### Validation approach

Add tests under `tests/test_executor/test_output.py`:
- Fence-wrapped JSON with triple-backticks inside a string field ✅ (Phase 2 confirms this passes)
- Fence-wrapped JSON ~80 KB in size ❌ (Phase 3 demonstrates this still fails)
- Fence-wrapped JSON with prose before and after the fence
- Raw JSON with no fence
- Malformed JSON (must still fail cleanly)

### Update from Phase 3 validation (2026-05-27)

The 80KB synthesizer case still fails. Possible root causes (not yet root-caused):

1. **Model response truncation**: At ~80KB, models may emit incomplete output where the closing ` ``` ` is missing. Greedy regex can't recover from a truly absent closing fence.
2. **Prose interleaved with JSON**: When asked to "respond with JSON matching this schema," some models still emit "Here is the synthesized JSON:\n```json\n{...}\n```\nLet me know if..." — prose before AND after the fence. The current extractor handles prose-then-JSON and JSON-then-prose, but not the dual case cleanly.
3. **Field-shape mismatch inside the envelope**: The model may emit valid JSON whose **shape** doesn't match `{observations_json: string, pillar_summary: string}` — e.g. emitting the full observations object directly at top level instead of nesting it as a string in `observations_json`. Conductor's "Could not extract JSON" message may be misleading; the actual failure might be field validation.

The Conductor error at the parse failure surfaces "Response started with: ..." but truncates to "..." so the actual prefix isn't visible — diagnosis would benefit from a longer prefix in the error message (e.g. first 500 chars).

**Recommendation for upstream:** even with the greedy regex in place, ship Issue #2's `output_mode: raw` field. The greedy regex helps the small case; only `output_mode: raw` addresses the architectural problem at scale.

---

## Issue 2 — `output:` schema is the wrong default for prose / large JSON agents

> **Status (v0.1.17):** ❌ STILL OPEN. The `output_mode` field is not in `config/schema.py`. The recent `9d603a1` commit (`feat(script): allow script agents to declare output schemas`) is about *script* agents getting `output:`, which is a different feature. Phase 3 empirically validated that this issue is the **architectural root cause** of the workiq-coach failures. Headline upstream contribution candidate.

### Symptom

`output:` is intuitively the "right" way to declare what an agent produces. New users specify it for every agent. For agents that produce small, strictly-structured JSON, it works fine. For agents that produce:

- Large structured JSON (>10 KB)
- Markdown narrative wrapped in a string field
- Output that legitimately contains triple-backticks

…it actively hurts. The schema instruction + fence-extraction regex (Issue #1) combine to produce reliable parse recovery loops.

The workaround — drop `output:`, read the response from `<agent>.output.result` directly — exists in the codebase (`copilot.py:668-677`) and is mentioned obliquely in the README, but it isn't the discovered path for new users. We took six failed runs to find it.

### Location

- `src/conductor/providers/copilot.py:524-531` — schema instruction injection
- `src/conductor/providers/copilot.py:668-677` — the "no schema" code path that wraps response in `{"result": "..."}`
- `src/conductor/config/schema.py` — `AgentDef` and `OutputField` definitions

### Cause

`output:` semantically means two things at once:
1. **The shape the model should produce** (a hint to the LLM)
2. **The shape Conductor should extract from the response** (a parser instruction)

For prose-heavy or large outputs, you want neither — you want raw text passed through. There's no way to express that today except by omitting `output:` entirely, which feels like "I didn't tell Conductor what this produces," not "I told Conductor the right thing."

### Fix proposal

Add an explicit `output_mode` field to `AgentDef`:

```yaml
- name: synthesizer
  prompt: !file prompts/synthesizer.md
  output_mode: raw          # NEW — captures response as .result
  # ...

- name: structured_extractor
  prompt: !file prompts/structured.md
  output_mode: envelope     # default for backward compat; uses output: schema
  output:
    field1:
      type: string
    field2:
      type: integer
```

**Default behavior:** continue to behave as today (envelope mode if `output:` is present, raw mode if not), so this is non-breaking. But:

1. At `conductor validate` time, **warn** when an agent has `output:` but its prompt strongly suggests prose output (heuristic: prompt contains "narrative", "markdown", "essay", "summary", "synthesizer", or the output schema has a single string field with description suggesting a large block).
2. **Document prominently** in `docs/workflow-syntax.md` that for agents emitting large JSON or markdown, `output_mode: raw` (or omitting `output:`) is the right pattern.
3. Add a section to the schema docs explaining the trade-off: envelope mode lets you reference structured fields downstream, raw mode preserves arbitrary text.

### Blast radius

- New workflows benefit from clearer guidance and earlier warnings
- Existing workflows are unchanged (default behavior preserved)
- Workflows currently using the `output:` workaround would gain a way to express intent explicitly

### Validation approach

- Unit tests for the validation warning logic
- A `docs/workflow-syntax.md` example showing both modes side-by-side
- A migration note in `CHANGELOG.md`

---

## Issue 3 — Subprocess invocation fails intermittently on Windows forward-slash paths

> **Status (v0.1.17):** ❌ STILL OPEN. No forward-slash → backslash normalization in `executor/script.py`. Did not recur during Phase 3 validation, but workaround remains: set `$env:PYTHON` to a backslash absolute path.

### Symptom

A `type: script` step with `command: "${PYTHON:-python}"`, with `$env:PYTHON = "C:/Python314/python.exe"`, fails with:

```
Script 'schema_validator': command not found: 'C:/Python314/python.exe'
```

**…sometimes.** The same env var, same shell context, same command worked in three earlier runs in the same session. The path is correct; the executable exists; `where python` resolves to it. Conductor's subprocess invocation appears nondeterministic with forward-slash absolute paths on Windows.

### Location

- `src/conductor/executor/script.py:105-118` — `asyncio.create_subprocess_exec()` call site

### Cause

Likely some combination of:
1. **Path separator mismatch**: forward slashes work for most Windows APIs but `CreateProcessA` can be picky. Python's `subprocess` module on Windows resolves the executable differently depending on whether it sees a path-like string vs. a bare name.
2. **`shell=False` (implicit default)** doesn't run PATH resolution the way the shell does. A forward-slash absolute path *should* work directly with `CreateProcessA` but may interact badly with some `env=` setups.
3. **Inherited env from PowerShell** may carry stale `Path` variants between runs.

### Fix proposal

In `script.py` before calling `create_subprocess_exec`:

```python
import os
if sys.platform == "win32":
    rendered_command = rendered_command.replace("/", os.sep)
```

Normalize separators on Windows. Also improve the error message: when `FileNotFoundError` is caught, include the resolved command, the env vars referenced, and a hint about path separators if the resolved command contains `/` on Windows.

### Blast radius

- Windows users with forward-slash absolute paths in `command:` (common when copy-pasting from YAML examples that target POSIX)
- POSIX users unaffected

### Validation approach

- Unit test under `tests/test_executor/test_script.py` with a forward-slash Windows path
- Mock the subprocess call to verify the separator normalization happens

---

## Issue 4 — `${VAR:-DEFAULT}` regex splits on the first `:` in the default

> **Status (v0.1.17 and v0.1.16):** ❎ **DEBUNKED — this is not a bug.** The original brainstorm claim was wrong.
>
> The regex at `config/loader.py:23` is `r"\$\{([^}:]+)(?::-([^}]*))?\}"`. The variable-name portion `[^}:]+` excludes colons (so it can't accidentally absorb `C:`), and the default-value portion `[^}]*` correctly accepts colons. Empirical test:
>
> ```
> "${PYTHON:-C:/Python314/python.exe}" → VAR='PYTHON', DEFAULT='C:/Python314/python.exe'
> "${WORKIQ_COACH_ROOT:-Q:/src/workiq-coach}" → VAR='WORKIQ_COACH_ROOT', DEFAULT='Q:/src/workiq-coach'
> "${VAR:-default:with:colons}" → VAR='VAR', DEFAULT='default:with:colons'
> ```
>
> All cases resolve correctly. My original analysis confused the workiq-coach user's notes ("we tried `${PYTHON:-C:/...}` and it didn't work") with a regex bug. The actual problem at the time was almost certainly something else downstream (possibly the subprocess invocation issue from Issue #3). Leaving this section in place as a warning to future readers: validate empirically before proposing fixes to regex-shaped code.

### What I originally thought

That the regex split on the *first* `:` rather than `:-`, mangling `${PYTHON:-C:/path}` into `VAR=PYTHON, DEFAULT=C` (with the rest discarded). This is not what happens; the regex's variable-name class `[^}:]+` correctly stops at the first `:`, but then `(?::-...)?` requires the literal `:-` sequence (colon-dash) to enter the default group. A bare `:` after the var name does not match the optional default group.

### Lesson learned

When proposing a regex fix, run the regex against the exact failure input first. Five minutes with `re.compile().search()` would have caught this.

---

## Issue 5 — Dashboard web server dies during long-parked human gates

> **Status (v0.1.17):** ⚠️ PARTIAL. `web/server.py:315-345` now handles `gate_response`, `dialog_message`, and `iteration_limit_response` messages from clients, with `_disconnect_event` and grace timers for connection lifecycle. PR `dc29c2c` (*fix(engine,web): resolve max-iterations gate from dashboard in --web-bg*) brings gate resolution into the dashboard itself. **What's still missing:** a CLI `conductor gate-respond <run-id>` command for resolving gates from outside the browser when the dashboard is unreachable.

### Symptom

A workflow with a `human_gate` that sits awaiting user input for many hours (overnight is a realistic case) ends up in a zombie state:

- Conductor process: alive, idle (CPU 0)
- Dashboard URL: `connection refused` (server thread or socket died)
- Gate: unreachable from browser
- CLI: no escape hatch — only option is to kill the process and lose progress

### Location

- `src/conductor/web/server.py` — `WebDashboard` lifecycle
- `src/conductor/gates/human.py` — gate prompt logic
- Likely interaction with `interrupt/listener.py`

### Cause

Hypothesized (would need code archaeology to confirm):
1. **uvicorn or WebSocket idle timeout** closes the socket after some inactivity, but the workflow event loop keeps waiting on the gate
2. **No keepalive** between dashboard and client
3. **No CLI gate-response endpoint** — gates are resolved only through dashboard UI (or terminal stdin)

### Fix proposal

Three independent improvements; pick any subset:

1. **WebSocket keepalive**: emit a `{"type": "heartbeat"}` event every 30–60 seconds from the dashboard server; clients respond with pong. Detects and reopens dead connections.
2. **CLI gate-resolution command**: `conductor gate-accept <run-id>` and `conductor gate-respond <run-id> <choice>` that POSTs to a `/api/gate-response` endpoint on the dashboard server. Lets users resolve a parked gate without the browser dashboard, addresses the "dashboard zombie" failure mode directly.
3. **Optional webhook / notification on gate-waiting**: out of scope for v0.1.x but worth flagging as a usability improvement for long-running workflows.

The CLI gate-resolution command is the most impactful single change — it gives users an escape hatch when the dashboard becomes unresponsive for *any* reason, not just the specific failure mode we hit.

### Blast radius

- Long-running workflows with `human_gate` steps
- Workflows that combine `--web-bg` with gates (currently broken per Issue #8 below; would also benefit)
- Workflows without gates unaffected

### Validation approach

- Integration test: simulate dashboard socket close mid-gate; verify CLI gate-respond works
- Manual: park a gate for >5 min, check keepalive works

---

## Issue 6 — Parse-recovery retry budget hardcoded per provider

> **Status (v0.1.17):** ⚠️ PARTIAL. `max_parse_recovery_attempts` has been moved to an internal `_retry_config` field (visible in `providers/copilot.py:685` and `claude.py:191`), but is **not exposed in the workflow YAML schema**. The internal refactor is half the work; the user-facing knob is what would let workflows fail fast on doomed agents (especially with the new outer-retry budget — see Issue #10).

### Symptom

When parse recovery is needed, Copilot gets 5 retries, Claude gets 2 (per the user's notes; values from `copilot.py:81` and `claude.py:106`). These are class-level constants. For large outputs prone to parse failure, 5 may be too few; for short, fast outputs in CI/cost-sensitive contexts, 5 may be too many.

### Location

- `src/conductor/providers/copilot.py:81`
- `src/conductor/providers/claude.py:106`

### Cause

Not configurable.

### Fix proposal

Add `retry.max_parse_recovery_attempts` to the per-agent or per-workflow config (optional, with provider-specific defaults preserved for backward compat):

```yaml
- name: synthesizer
  retry:
    max_parse_recovery_attempts: 0   # fail fast; don't waste tokens on retries
```

Honors the [Provider Parity](../../AGENTS.md#provider-parity) rule — both providers need the same field surfaced.

### Blast radius

- Large-output workflows that would benefit from higher retry budgets
- CI/cost-sensitive workflows that prefer fail-fast
- Backward-compatible (default to current values)

### Validation approach

- Schema test: field accepts integer in range, rejects negative
- Provider tests: configured value propagates to both copilot and claude paths

---

## Issue 7 — `type: script` agents inside `parallel:` groups silently misbehave

> **Status (v0.1.17):** ✅ **SHIPPED.** `config/validator.py:489-492` rejects with: *"Agent '\<name\>' in parallel group '\<pg\>' is a script step. Script steps cannot be used in parallel groups."* This is Cluster A's quick-win item from the first revision of this brainstorm. Done.

### Symptom

A YAML like:

```yaml
parallel:
  - name: save_chain
    description: Save all artifacts in parallel
    agents:
      - save_a
      - save_b
      - save_c
    routes:
      - to: $end
```

where each of `save_a/b/c` has `type: script` — silently runs each as an LLM agent with no prompt. The scripts never execute; tokens are burned on nothing.

The user's `phase-b.yaml` originally had this structure and had to be refactored to a sequential save chain.

### Location

- `src/conductor/engine/workflow.py` — `_execute_parallel_group` function (cited as `:3519-3641` by the analysis; current line numbers may differ)

### Cause

The parallel executor dispatches all agents through the LLM executor path without checking `agent.type`. Scripts go through unchanged.

### Fix proposal

Two options, can ship either:

**A. Reject at validation time** (simpler): in `config/validator.py`, walk all parallel groups; if any agent in the group has `type: script`, raise `ValidationError`:

```
Parallel group 'save_chain' contains script agent 'save_a'.
Scripts cannot run inside parallel groups in Conductor v0.1.x.
Move script steps to a sequential chain before or after the parallel group.
```

**B. Support scripts in parallel** (proper fix): in `_execute_parallel_group`, dispatch on `agent.type`:

```python
if agent.type == "script":
    executor = ScriptExecutor()
else:
    executor = await self._get_executor_for_agent(agent)
```

Then execute concurrently. Requires careful error handling (`continue_on_error` semantics must apply to script failures too).

**Recommendation:** ship A first (1-day fix, no behavior change for valid workflows), then B as a follow-up if there's demand.

> *Update 2026-05-27 — Upstream shipped Option A at `config/validator.py:489-492`. The error message is essentially as proposed. Done.*

### Blast radius

- Workflows that put scripts in parallel groups (currently silently broken)
- Other workflows unaffected

### Validation approach

- `tests/test_config/test_validator.py`: parallel group with script agent → raises ValidationError
- If B is shipped: integration test with 3 script agents in a parallel group

---

## Issue 8 — `--web-bg` + `human_gate` crashes with EOFError

> **Status (v0.1.17):** ✅ **SHIPPED — verbatim.** `cli/app.py:158-191` defines `_abort_web_bg_if_human_gate` whose error message reproduces the brainstorm's proposed text essentially word-for-word, including the four-option remediation list ("Use --web (foreground)…", "Add --skip-gates…", "Remove human_gate steps…", "Wait for CLI gate-resolution support (planned follow-up)"). Honors `--skip-gates` as the documented escape hatch. Strong evidence that this brainstorm was read; thank you to whoever picked it up.

### Symptom

Running `conductor run --web-bg` with a workflow that includes a `human_gate` crashes the detached background process with an `EOFError` when the gate prompt tries to read stdin (which is redirected to /dev/null in the detached child).

### Location

- `src/conductor/gates/human.py` — `Prompt.ask()` call
- `src/conductor/cli/bg_runner.py` — background process forking
- `src/conductor/interrupt/listener.py:194-224` — separate stdin reader for interrupt handling

### Cause

`Prompt.ask()` (Rich) reads from `sys.stdin`. In `--web-bg`, stdin is detached. EOFError propagates, crashing the process.

### Fix proposal

Detect non-interactive stdin at workflow startup:

```python
import sys
is_interactive = sys.stdin.isatty()
```

If `not is_interactive` and the workflow contains gates without `--skip-gates`:

1. **Validate-time error** (clearest): refuse to start, with:
   ```
   --web-bg detected with workflow containing human_gate steps.
   Detached terminals cannot prompt for input.
   Options:
     1. Use --web (foreground) instead of --web-bg
     2. Add --skip-gates to auto-accept the first option
     3. Remove human_gate steps from the workflow
     4. Wait for CLI gate-resolution support (Issue #5 follow-up)
   ```
2. **Runtime fallback** (more complex): when a gate fires in detached mode, route it to the dashboard's `/api/gate-response` endpoint (see Issue #5 fix #2) and poll for the response. Requires the gate-respond endpoint to exist.

> *Update 2026-05-27 — Upstream shipped Option 1 verbatim at `cli/app.py:158-191`. The error message reproduces the proposed text including the four-option remediation list. Done.*

### Blast radius

- `--web-bg` + gate workflows (currently crash)
- Other workflows unaffected

### Validation approach

- Integration test: workflow with gate, `--web-bg`, no `--skip-gates` → ValidationError with the message above

---

## Issue 9 — Possible expansion-path divergence between `command:` and `args:`

> **Status (v0.1.17 and v0.1.16):** ❎ **NOT A BUG.** Confirmed both fields use `self.renderer.render()` on the same code path in `executor/script.py:86-87`. No divergence to fix. Original brainstorm was speculative; validation pass closed it out.
>
> The "anecdotal" observation that motivated this item was confused with Issue #4 (which itself turned out not to be a bug). Both go through the same Jinja2 template rendering. A test verifying parity is still a reasonable defensive addition for `tests/test_executor/test_script.py`, but the issue itself can be closed.

---

## Issue 10 — Agent-level outer retry budget amplifies sunk cost (NEW, from Phase 3)

> **Status (v0.1.17):** ❌ STILL OPEN. New finding from Phase 3 validation.

### Symptom

When an agent's inner parse-recovery cycle (5 attempts) exhausts, Conductor v0.1.17 retries **the whole agent up to 3 more times**. The outer-attempt counter is visible in the log as `Agent 'synthesizer' attempt 1/3 failed: ...`, after which Parse Recovery 1/5 begins again inside outer attempt 2/3.

This was not visible in v0.1.16 (or at least, I didn't observe it). It looks like a hardening pass that adds resilience to transient failures — but for **deterministic** schema-mismatch failures (the workiq-coach synthesizer case), it triples the sunk cost.

### Location

Likely `providers/copilot.py` or `engine/workflow.py` — not yet root-caused. Visible in v0.1.17 log output as `Agent '<name>' attempt N/3 failed: Failed to parse structured output...`.

### Cause

Hardening retry logic that doesn't distinguish *transient* failures (worth retrying) from *deterministic* configuration mismatches (won't change on retry). The error message includes `Retryable: True`, but the determination of retryability appears to be based on the failure category, not on whether retrying could actually succeed.

### Fix proposal

Three options, can ship any subset:

1. **Per-agent `max_outer_attempts` config** — let workflow authors opt out of the outer retry for agents known to fail-deterministically:
   ```yaml
   - name: synthesizer
     retry:
       max_outer_attempts: 1   # don't burn extra attempts on deterministic failures
       max_parse_recovery_attempts: 2
   ```

2. **Smarter retry classifier** — if the same parse error fires twice in a row inside one outer attempt, mark the failure as deterministic and skip remaining outer attempts. Saves cost without requiring user configuration.

3. **Surface a deprecation/warning when the outer retry is triggered** — make the cost visible. Many users (myself included) wouldn't notice the 3× spend amplification until reading the bill.

### Blast radius

- Workflows with deterministically-failing agents (e.g. envelope mismatch on large outputs)
- Production runs where cost amplification matters
- Backward-compatible if added as optional config with current behavior as the default

### Validation approach

- Integration test: agent configured to always fail parse — count outer attempts, verify budget config caps them
- Cost-tracking test: verify the budget tracker counts outer retries the same as parse recoveries

---

## Fragile patterns flagged (not yet hit in production)

These are things the analysis surfaced as "this will bite someone eventually" but that didn't directly affect the workiq-coach session. Listed for awareness, not for immediate fix.

### Unbounded event-history growth

- **Location:** `src/conductor/web/server.py` — `_event_history` list
- **Risk:** Multi-day workflows or high event rates exhaust memory
- **Fix:** ring buffer with configurable size (default ~10k events)

### Parallel context isolation race

- **Location:** `src/conductor/engine/workflow.py` — deep-copy snapshot at parallel-group entry
- **Risk:** Deep copy is safe at copy time, but if an agent mutates context via a tool during execution, isolation breaks
- **Fix:** wrap parallel-group context in a read-only proxy, or use immutable types

### Parse-recovery prompt is identical across retries

- **Location:** `src/conductor/providers/copilot.py:~714` — `_build_parse_recovery_prompt`
- **Risk:** Same prompt to same model produces correlated errors; retries don't learn from validator feedback
- **Fix:** include the *specific* validation error in the recovery prompt so the model knows what to change

### No timeout on dashboard startup

- **Location:** `src/conductor/web/server.py` — `WebDashboard.start()`
- **Risk:** uvicorn port binding can hang on Windows; engine waits indefinitely
- **Fix:** wrap startup in `asyncio.wait_for()` with a reasonable timeout (e.g. 30s); raise clearly if it fails

---

## Implementation plan — revised after v0.1.17

The first revision of this doc proposed three PR clusters. Phase 1/2/3 validation against v0.1.17 changes the picture significantly: **Cluster A is mostly already shipped**, **Cluster B is now the headline priority**, and **Cluster C shrinks**.

### Cluster A (largely shipped in v0.1.17 — leftover items only)

Original scope included Issues #4, #7, #8, and parts of #3. Updated:

- ✅ **Issue #7** — shipped (`config/validator.py:489-492`)
- ✅ **Issue #8** — shipped (`cli/app.py:158-191`)
- ❎ **Issue #4** — debunked (not actually a bug)
- ❌ **Issue #3** — Windows path normalization not yet shipped. Still a 10-line PR.

**Remaining Cluster A scope:** Issue #3 alone (~20 LOC including tests). Trivially mergeable.

### Cluster B: "Output mode: raw vs envelope" — NOW THE HEADLINE PRIORITY

**Goal:** introduce an explicit `output_mode: raw | envelope` field so that prose / large-JSON agents have a documented, first-class way to opt out of the JSON envelope contract that empirically fails at scale.

**Includes:**
- Issue #1 (partially shipped) — keep the greedy regex; consider the brace-balanced extractor as a follow-up only if Issue #2 doesn't subsume the need
- Issue #2 — add `output_mode: raw | envelope` to `AgentDef`; warn at `conductor validate` when `output:` is declared on prose-likely agents (heuristic on prompt content); documentation updates in `docs/workflow-syntax.md` and `docs/configuration.md`
- Issue #10 (new) — pair with a per-agent `retry.max_outer_attempts` knob. Without this, even with `output_mode: envelope` declared correctly, a transient failure burns 3× the necessary cost.

**Empirical justification:** Phase 3 demonstrated that the greedy regex alone is insufficient for the full-scale workflow that originally motivated this brainstorm. The `output_mode: raw` field is the architectural fix, not a workaround.

**Estimated size:** ~150 LOC across `config/schema.py`, `config/validator.py`, `executor/output.py` (touch only), `providers/copilot.py` + `providers/claude.py` (parity), plus docs updates and tests.

**Risk:** medium. Affects the hot path of every workflow with `output:`. Backward-compatible if the default behavior is preserved when `output_mode` is unspecified (existing workflows continue to behave as today). Worth a design discussion in this brainstorm before opening a PR — see Open question #1 below.

**Why ship this:** this is the architectural fix the validation pass empirically demands. Every other item on this list is comparatively cosmetic.

### Cluster C: "Runtime hardening" — shrunk

Original scope included Issues #5, #6, #9 + bonus patterns. Updated:

- ⚠️ **Issue #5** — partially shipped. Remaining: CLI `conductor gate-respond <run-id>` command for resolving gates outside the browser. ~50 LOC + tests.
- ⚠️ **Issue #6** — partially shipped (internal refactor). Remaining: expose `max_parse_recovery_attempts` in the YAML schema. ~10 LOC + schema test.
- ❎ **Issue #9** — confirmed not a bug. Closed.
- Bonus patterns — still flagged below; out of scope for an immediate PR.

**Remaining Cluster C scope:** Issue #5 (CLI gate-respond) + Issue #6 (YAML field). ~60 LOC combined.

### What I'd PR if I were doing this myself

In priority order:

1. **Issue #2 + #10** as a single design-discussion-first PR (Cluster B headline). Opens the conversation about the `output_mode` field with empirical Phase 3 evidence. Optionally bundles the `retry.max_outer_attempts` knob.
2. **Issue #3** as a small, focused Windows path normalization PR. Probably mergeable in a day.
3. **Issue #6** as a small YAML schema PR exposing the already-internal retry budget knob.
4. **Issue #5 CLI command** as a small feature PR.

---

## Open questions for maintainers

1. **Output mode default.** Cluster B proposes `output_mode` as an additive field with the existing default preserved. Phase 3 evidence suggests the current default (envelope-when-`output:`-is-present) produces parse-recovery loops on real-world large-output workflows. Would maintainers consider:
   (a) shipping `output_mode` additive with current default preserved,
   (b) shipping additive + warning loudly at `conductor validate` when `output:` is declared on a prose-likely agent, OR
   (c) flipping the default to `raw` in v0.2.x with a deprecation pass for explicit-envelope workflows?
2. **Provider parity for retry budget.** Issue #6 proposes per-agent `retry.max_parse_recovery_attempts`. The current Copilot default is 5; Claude is 2. Should the proposed field be a single value that both providers respect, or should the default per-provider be preserved (5 for Copilot, 2 for Claude) with the per-agent override applying uniformly? Phase 3 surfaces a related concern: the **outer** retry budget (Issue #10) is also unconfigurable. Worth bundling.
3. **Gate resolution endpoint security.** Adding a CLI gate-resolution surface (Cluster C Issue #5) and/or `POST /api/gate-response` to the dashboard server creates a new attack surface. Should it require a per-run token (passed via env var or CLI flag) to authorize gate responses? The dashboard is bound to localhost by default, but `--web-bg` users running on shared infrastructure might want explicit token-based auth.
4. **Outer retry classifier (Issue #10).** Is there appetite for a smarter classifier that detects deterministic failures (same parse error twice in a row inside one outer attempt) and skips remaining outer attempts? Saves cost without requiring user configuration. The simpler alternative is a user-facing `retry.max_outer_attempts` knob.

---

## Validation approach (cross-cutting)

For each cluster:

1. **Unit tests** in the relevant `tests/` subdirectory mirroring source layout (per AGENTS.md).
2. **Integration tests** in `tests/test_integration/` that reproduce the actual failure mode from this session, then verify the fix.
3. **Provider parity check** — any change to `providers/copilot.py` mirrored in `providers/claude.py` per [Provider Parity](../../AGENTS.md#provider-parity).
4. **Documentation updates** — `docs/workflow-syntax.md` and `docs/configuration.md` for Issue #2 (`output_mode`); `CHANGELOG.md` for all clusters.
5. **Real-world smoke test (the canonical "did we fix it" test):** re-run the workiq-coach Phase A workflow with the original `output:` schema on the synthesizer, against the fixed Conductor. This is Phase 3 of the validation pass that's already documented above. If it now completes first try, Issue #2 is fixed.

---

## References

- Conductor source: `Q:/src/conductor` (this repo, v0.1.17 as of 2026-05-27)
- `AGENTS.md` — architecture overview and contribution guide
- `docs/projects/usability-features/` — existing brainstorm/plan documents in this convention
- workiq-coach source: `Q:/src/workiq-coach`
  - `skills/executive-coach-assessor/workflows/phase-a.yaml` — the workflow that surfaced most issues
  - `skills/executive-coach-assessor/workflows/README.md` — contains the early diagnostic comment about output.py:120 fence-extraction bug
- **Validation cycle (2026-05-27):**
  - Phase 1 — static survey, free, ~10 min
  - Phase 2 — minimal repro of Issue #1, ~$0.005, ~15 sec runtime
  - Phase 3 — end-to-end against original config, ~$0.80, ~50 min runtime (killed mid outer-attempt 2/3)
  - Total cost: ~$0.81
- Conversation context: the original analysis was produced collaboratively over a multi-hour debugging session 2026-05-25/26; the validation pass and this update were produced on 2026-05-27.

---

## What would make this brainstorm into a `.plan.md`

- **For Issue #2 + #10:** maintainer agreement on Open question #1 (additive `output_mode` field with backward-compat default). Then a `.plan.md` for the implementation.
- **For Issue #3:** trivial enough to skip the `.plan.md` step and just open a PR.
- **For Issue #5 (CLI gate-respond) + #6 (YAML field):** maintainer agreement they're wanted. Then small focused PRs.

If maintainers want any subset of these implemented, the external contributor (Lucio) is happy to open issues + PRs for them. The Phase 3 validation evidence is reproducible — happy to provide repro workflows on request.
