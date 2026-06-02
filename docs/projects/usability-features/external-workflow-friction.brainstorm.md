# External Workflow Friction — Findings and Fix Brainstorm

> **Status:** brainstorm — open for discussion before any of the proposals here become a `.plan.md`.
> **Author:** Lucio Tinoco (external user contributor)
> **Source of evidence:** real-world execution of the `workiq-coach` Conductor workflow set (Phase A: WorkIQ fan-out → synthesizer → schema validator → save; Phase B: four parallel artifact generators → consistency check → 5 saves). Seven failed runs across May 25–26, 2026, before a successful end-to-end execution was achieved.
> **Audience:** Conductor maintainers, and any contributor who'd help upstream these fixes.

---

## Why this document exists

A single user trying to run two non-trivial workflows against real WorkIQ + Copilot + Anthropic data hit nine distinct rough edges in Conductor v0.1.16. Each one looked like a one-off the first time it appeared; each turned out to be a real bug or design fragility. Most are small fixes. A few are architectural choices worth discussing.

This document records what was learned so the cost of that session pays back across the codebase rather than being a private tale. It is **not** a request to fix everything at once — it's a structured analysis with a phased implementation plan that maintainers can carve into PR-sized work, reshape, reject, or defer.

The unifying theme: each issue produces a **silent or confusing failure mode** that consumes minutes-to-hours of an external user's time before the actual cause becomes diagnosable. The fixes are mostly about **earlier and clearer errors**, **safer defaults**, and **eliminating brittle implicit behaviors**.

---

## Executive summary

| Tier | Theme | Issues | PR cluster |
|---|---|---|---|
| **1** | Silent/confusing failures → clear errors | #4 var expansion, #7 script in parallel, #8 web-bg + gate, parts of #3 | "Better validation + error messages" |
| **2** | Architectural fragility in JSON envelope extraction | #1 fence regex, #2 `output:` schema default | "Output mode: raw vs envelope" |
| **3** | Operational reliability | #3 subprocess intermittency, #5 dashboard zombie, #6 retry budget, #9 expansion parity | "Runtime hardening" |
| **Bonus** | Patterns that look fragile but haven't surfaced in our use yet | event-history unbounded growth, parallel context race, recovery prompt reuses identical schema, no dashboard-startup timeout | Future cleanup |

The single highest-leverage change is **#1 + #2 together** — the JSON envelope extraction + the `output:` schema default. These are responsible for the majority of "Parse Recovery 1/5 → 5/5 → workflow times out" experiences we hit. Tier 1 fixes are individually cheap and add up to a much smoother first-run experience for new external users.

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

The other six issues (#3, #4, #6, #7, #8, #9) surfaced in supporting fashion — each cost minutes, and each is independently a real bug.

---

## Issue 1 — Fence-extraction regex breaks on large or nested JSON

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

### Blast radius

- All workflows with `output:` schemas and large structured responses
- Workflows where any string field in the JSON could contain triple-backticks (e.g. coaching observations quoting Markdown, code examples in prose, file paths)
- Both providers (copilot + claude) need the same fix per the [Provider Parity](../../AGENTS.md#provider-parity) rule

### Validation approach

Add tests under `tests/test_executor/test_output.py`:
- Fence-wrapped JSON with triple-backticks inside a string field
- Fence-wrapped JSON ~80 KB in size
- Fence-wrapped JSON with prose before and after the fence
- Raw JSON with no fence
- Malformed JSON (must still fail cleanly)

---

## Issue 2 — `output:` schema is the wrong default for prose / large JSON agents

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

### Symptom

`${PYTHON:-C:/Python314/python.exe}` fails to expand correctly: the first `:` (after `C`) is misread as the `:-` default separator, producing nonsensical variable resolution. Forces users to either set `$env:PYTHON` to absolute and use `${PYTHON:-python}` (with a colon-free default), or to avoid env-var defaults entirely for Windows paths.

### Location

- `src/conductor/config/loader.py:23` — env var expansion regex

### Cause

The regex (or equivalent string-split) treats `:` greedily as the var/default separator. Splits on the *first* `:` encountered. Windows drive letters violate this assumption.

### Fix proposal

Replace the regex with a parser that scans the token from `${` to `}` and uses `rfind(":-")` to locate the default separator only at the **last** `:-` occurrence:

```python
def parse_var_token(token: str) -> tuple[str, str | None]:
    """Parse ${VAR:-DEFAULT} content (the text between ${ and })."""
    sep = ":-"
    idx = token.rfind(sep)
    if idx == -1:
        return token, None
    return token[:idx], token[idx + len(sep):]
```

Walk the source string for `${...}` blocks and apply this parser.

Alternative: document that defaults can't contain `:` and validate at load time — but the parser fix is small and removes a real footgun.

### Blast radius

- Windows users with absolute-path defaults
- POSIX users unaffected (`:` is rare in defaults)
- Backward-compatible (existing defaults without colons still work identically)

### Validation approach

- `tests/test_config/test_loader.py` cases:
  - `${VAR:-C:/path/with/colons}` resolves to `C:/path/with/colons` when VAR unset
  - `${VAR:-default}` still works (no colon)
  - `${VAR}` (no default) still works
  - Edge: nested `${...}` inside default value

---

## Issue 5 — Dashboard web server dies during long-parked human gates

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

### Blast radius

- Workflows that put scripts in parallel groups (currently silently broken)
- Other workflows unaffected

### Validation approach

- `tests/test_config/test_validator.py`: parallel group with script agent → raises ValidationError
- If B is shipped: integration test with 3 script agents in a parallel group

---

## Issue 8 — `--web-bg` + `human_gate` crashes with EOFError

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

### Blast radius

- `--web-bg` + gate workflows (currently crash)
- Other workflows unaffected

### Validation approach

- Integration test: workflow with gate, `--web-bg`, no `--skip-gates` → ValidationError with the message above

---

## Issue 9 — Possible expansion-path divergence between `command:` and `args:`

### Symptom

Anecdotal: `${VAR:-default}` expansion appears to behave differently in `command:` vs. `args:` fields of a script step. The user's debugging notes for Issue #4 mention this is what led to the colon-in-default workaround being needed for `command:` but not `args:`. Hasn't been root-caused; may be related to Issue #4 or may be a separate code path.

### Location

- `src/conductor/executor/script.py:86-87` — template rendering for both fields

### Cause

Unknown without deeper investigation. Possible candidates:
1. Different render order (env var resolution at YAML load time vs. Jinja2 template render time)
2. One field passes through a parser that the other doesn't
3. The observation was incorrect and both behave identically once Issue #4 is fixed

### Fix proposal

Audit `script.py` to confirm both `command:` and `args:` use the same render path. Add unit tests that verify equivalence:

```python
def test_command_and_args_env_var_parity():
    """Same ${VAR:-default} resolves identically in command: and args: fields."""
    # ... test that command="${X:-foo}" and args=["${X:-foo}"] both produce "foo"
```

If a divergence exists, unify the paths.

### Blast radius

- Probably small — would have surfaced more widely if significant
- Test coverage improvement is valuable regardless

### Validation approach

- New test in `tests/test_executor/test_script.py`

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

## Implementation plan — three PR clusters

### Cluster A: "Better validation + error messages" (highest value-to-effort)

**Goal:** turn silent and confusing failures into clear errors at validate time or at the failure site.

**Includes:**
- Issue #4: `${VAR:-DEFAULT}` parser fix
- Issue #7: reject `type: script` in `parallel:` at validate
- Issue #8: detect non-interactive stdin + workflow gates at startup
- Issue #3: normalize Windows path separators + improve subprocess error message

**Estimated size:** ~60 LOC across `config/loader.py`, `config/validator.py`, `executor/script.py`, `gates/human.py`. Plus tests.

**Risk:** very low. All changes are either pure parser fixes or earlier-validation. No behavioral change for valid workflows.

**Why ship first:** every one of these is an instance of "user hits a confusing failure, takes 30+ minutes to diagnose, fix is 2 lines of code." Each PR independently improves the new-user experience.

### Cluster B: "Output mode: raw vs envelope" (architectural)

**Goal:** make raw-response the explicit, documented, recommended pattern for agents producing prose or large JSON.

**Includes:**
- Issue #1: greedy fence regex (quick fix) + optional brace-balanced extractor (proper fix)
- Issue #2: add `output_mode: raw | envelope` to AgentDef; warn at validate when `output:` is declared on prose-likely agents; documentation updates

**Estimated size:** ~100 LOC across `executor/output.py`, `providers/copilot.py`, `providers/claude.py` (parity), `config/schema.py`, `config/validator.py`. Plus docs updates and tests. The fence-regex piece is small; the `output_mode` field + validator warning + doc cohesion is most of the work.

**Risk:** medium. Affects the hot path of every workflow with `output:`. Needs careful provider-parity work. Worth a design discussion in this brainstorm before opening a PR.

**Why ship together:** the regex fix without the documented `output_mode` field still leaves new users tripping into the bad default. The field without the regex fix doesn't help existing workflows with valid `output:` declarations that happen to contain backticks.

### Cluster C: "Runtime hardening"

**Goal:** improve operational reliability of long-running workflows and dashboard interactions.

**Includes:**
- Issue #5: dashboard WebSocket keepalive + CLI gate-resolution command
- Issue #6: configurable `max_parse_recovery_attempts`
- Issue #9: unified expansion path + parity test
- Bonus: event-history ring buffer, dashboard-startup timeout

**Estimated size:** ~200 LOC, primarily in `web/server.py`, `cli/`, `providers/`. Plus the new `conductor gate-accept` CLI command.

**Risk:** medium-low. The keepalive and ring buffer are additive. The CLI gate-resolution is a net-new feature.

**Why ship last:** these are quality-of-life improvements rather than fixes for immediately-broken behavior. Maintainers may want to defer until A + B prove the brainstorm's value.

---

## Open questions for maintainers

1. **Output mode default.** Cluster B proposes `output_mode` as an additive field with the existing default preserved. Would maintainers consider flipping the default to `raw` in a future v0.2.x, given that the current default produces parse recovery loops on real-world workflows? (Backward-compat strategy: behave as today when `output_mode` is unspecified AND `output:` is specified; warn loudly via deprecation when this combination appears in `conductor validate`.)
2. **Provider parity for retry budget.** Issue #6 proposes per-agent `retry.max_parse_recovery_attempts`. The current Copilot default is 5; Claude is 2. Should the proposed field be a single value that both providers respect, or should the default per-provider be preserved (5 for Copilot, 2 for Claude) with the per-agent override applying uniformly?
3. **Gate resolution endpoint security.** Adding `POST /api/gate-response` to the dashboard server creates a new attack surface. Should it require a per-run token (passed via env var or CLI flag) to authorize gate responses? The dashboard is bound to localhost by default, but `--web-bg` users running on shared infrastructure might want explicit token-based auth.
4. **Issue #8 fix shape.** Validate-time error vs. runtime dashboard fallback for `--web-bg + human_gate`. The dashboard fallback is the better UX but depends on the gate-response endpoint from Issue #5 existing. Order the work?

---

## Validation approach (cross-cutting)

For each cluster:

1. **Unit tests** in the relevant `tests/` subdirectory mirroring source layout (per AGENTS.md).
2. **Integration tests** in `tests/test_integration/` that reproduce the actual failure mode from this session, then verify the fix.
3. **Provider parity check** — any change to `providers/copilot.py` mirrored in `providers/claude.py` per [Provider Parity](../../AGENTS.md#provider-parity).
4. **Documentation updates** — `docs/workflow-syntax.md` for Issue #2; `docs/configuration.md` for Issues #4, #6; `CHANGELOG.md` for all clusters.
5. **A real-world smoke test**: re-run the workiq-coach Phase A workflow (with the original `output:` schema on the synthesizer) and verify it now completes. This is the canonical "did we fix the actual problem" test.

---

## References

- Conductor source: `Q:/src/conductor` (this repo, v0.1.16)
- `AGENTS.md` — architecture overview and contribution guide
- `docs/projects/usability-features/` — existing brainstorm/plan documents in this convention
- workiq-coach source: `Q:/src/workiq-coach`
  - `skills/executive-coach-assessor/workflows/phase-a.yaml` — the workflow that surfaced most issues
  - `skills/executive-coach-assessor/workflows/README.md` — contains the early diagnostic comment about output.py:120 fence-extraction bug
- Conversation context: the analysis was produced collaboratively over a multi-hour debugging session in May 2026. The diagnostic narrative under "Source of evidence" is condensed from that session.

---

## What would make this brainstorm into a `.plan.md`

- Maintainer agreement that Cluster A is welcome → opens the door to a PR cluster
- Open question #1 (output mode default) decided → enables a coherent Cluster B
- Anyone disagrees with any of the nine issues → discussion happens here before any code

If maintainers want any subset of these implemented, the external contributor (Lucio) is happy to open issues + PRs for them.
