# Solution Design: External Workflow Friction v2 — Remaining Gaps

**Status:** DONE (EPICs 1–4 SHIPPED)  
**Revision:** 3 — Rebased onto actual v0.1.17 codebase state (score 68 review)  
**Prior plan:** [external-workflow-friction.plan.md](external-workflow-friction.plan.md) (SHIPPED — all four items landed in v0.1.17)  
**Source brainstorm:** [external-workflow-friction.brainstorm.md](external-workflow-friction.brainstorm.md) (updated 2026-05-27 with Phase 1/2/3 validation evidence)  
**Author:** Lucio Tinoco (external contributor) + Copilot design  
**Conductor version:** v0.1.17

---

## 1. Executive Summary

The prior plan shipped four fixes in v0.1.17: greedy fence regex (#1), script-in-parallel rejection (#7), `--web-bg` + `human_gate` guard (#8), and documentation for the "omit `output:`" pattern (#2 docs-only). A three-phase validation pass against v0.1.17 identified five remaining gaps. **Two of those five have since been fully implemented:**

- **`output_mode: raw | envelope`** (Issue #2) — SHIPPED. The `output_mode` field is implemented in `schema.py:508-523` with full validation rules, provider support (`copilot.py:524`, `claude.py:922`), documentation (`docs/workflow-syntax.md:159-203`), and tests.
- **Parse-exhaustion `is_retryable=False`** (Issue #10) — SHIPPED. Both providers mark parse-exhaustion as non-retryable (`copilot.py:748`, `claude.py:1887`), preventing the 3× outer-retry amplification. Error messages include 500-char response prefixes and suggest `output_mode: raw`.

**Three items constituted this plan's active scope — all now SHIPPED (EPICs 2–4 on branch `epics-1-4`, PR #234):**

1. **YAML-exposed `max_parse_recovery_attempts`** (Issue #6) — the internal `_retry_config` field from v0.1.17 is now user-configurable from the YAML `retry:` block, and the resolved per-agent value reaches the parse recovery loop in both providers.
2. **CLI `conductor gate-respond`** (Issue #5) — adds a CLI fallback (and `POST /api/gate-respond` endpoint) for resolving a parked gate when the dashboard is unreachable. Hardened during adversarial review with `agent_name` mismatch rejection (`409`) and header-based `hmac` token auth.
3. **Subprocess command resolution** (Issue #3) — resolves `command:` against PATH/PATHEXT (`shutil.which`) in `executor/script.py`.

The plan was sequenced: EPIC 2 (parse recovery config) first, then EPIC 3 (gate-respond), then EPIC 4 (command resolution). EPIC 1 is retained as a reference section with all tasks marked DONE.

---

## 2. Background

### Current State

Conductor v0.1.17 orchestrates multi-agent workflows defined in YAML. When an agent declares an `output:` schema, the provider injects a "respond with JSON matching this schema" instruction and attempts to parse the model's response through:

1. **Direct `json.loads`** → code-block extraction → brace-pattern extraction (copilot: `_extract_json` at `copilot.py:1081-1122`; claude: `_extract_json_fallback` + `emit_output` tool)
2. **Parse recovery loop** — up to 5 attempts (copilot, `copilot.py:680-748`) or 2 attempts (claude, `claude.py:1805-1888`) sending a correction prompt in the same session
3. **Outer retry loop** — up to 3 attempts (both providers, `copilot.py:75` / `claude.py:100`) restarting the entire agent session. Parse-exhaustion errors are now marked `is_retryable=False` (copilot: line 748, claude: line 1887), so the outer loop does **not** amplify parse failures.

Agents can now declare `output_mode: raw` (`schema.py:508-523`) to bypass JSON extraction entirely, receiving their response as `{"result": "<text>"}`. Both providers check `has_schema = agent.output and agent.output_mode != "raw"` (copilot: line 524, claude: line 922) before injecting schema instructions. This resolves the ~80 KB response parse failure root cause.

**What remains unsolved** is that `max_parse_recovery_attempts` (the inner loop limit) is not configurable from YAML. Both providers store it in an internal `_retry_config` (`copilot.py:81`, `claude.py:106`) or instance variable (`claude.py:191`), but `_resolve_retry_config()` always copies from the provider-level default, never from the YAML `RetryPolicy`.

### What Changed

The prior plan (v1) deliberately deferred `output_mode` as a non-goal. Phase 3 validation empirically disproved this reasoning. The `output_mode` field was then implemented (along with `is_retryable=False` and 500-char error prefixes) in the period between v0.1.17 and the current HEAD. This revision acknowledges those implementations as DONE and focuses the remaining plan on the three genuinely open items.

**Provider retry classification parity note:** Copilot's outer retry loop catches `ProviderError` and checks `e.is_retryable` (line 388). Claude's outer retry loop catches `Exception` broadly (line 1026) and uses `self._is_retryable_error(e)` which does `isinstance()` checks against Anthropic SDK exception types (line 713-769). A `ProviderError` raised from parse exhaustion doesn't match any SDK exception type, so `_is_retryable_error()` returns `False`. Both providers achieve the same behavioral outcome (no outer retry on parse exhaustion) through different mechanisms. The explicit `is_retryable=False` on Claude's parse-exhaustion `ProviderError` (line 1887) is correct for documentation clarity but is not the mechanism that prevents outer retry in Claude — `_is_retryable_error()` would return `False` regardless. This asymmetry is a known architectural difference with no behavioral impact for parse-exhaustion specifically.

---

## 3. Problem Statement

Five issues were identified after v0.1.17. The first two were resolved before this plan's active scope; the remaining three were addressed by EPICs 2–4 (PR #234):

1. ~~**No `output_mode` field**~~ → **RESOLVED.** `output_mode: raw | envelope` is implemented at `schema.py:508-523` with provider support, validation, tests, and documentation.

2. ~~**Hidden 3× outer retry on parse exhaustion**~~ → **RESOLVED.** Both providers now raise parse-exhaustion with `is_retryable=False` (`copilot.py:748`, `claude.py:1887`). Error messages include 500-char prefixes and suggest `output_mode: raw`.

The remaining three (now SHIPPED) were:

3. ~~**`max_parse_recovery_attempts` not YAML-configurable**~~ → **RESOLVED (EPIC 2).** Both providers respected an internal `_retry_config.max_parse_recovery_attempts` value, but `_resolve_retry_config()` always copied from the provider-level default (`copilot.py:303`, `claude.py:685`), never from the YAML `RetryPolicy`, and the resolved per-agent config did not reach the parse recovery loop. EPIC 2 threads the YAML value through to the recovery loop in both providers.

4. ~~**No CLI gate-resolution path**~~ → **RESOLVED (EPIC 3).** Gate responses previously flowed exclusively through WebSocket (`web/server.py:326-327`); when the dashboard was unreachable there was no fallback. EPIC 3 adds the `conductor gate-respond` CLI command and `POST /api/gate-respond` endpoint (hardened post-review with `agent_name` 409 validation and header-based `hmac` token auth).

5. ~~**Subprocess command not resolved against PATH**~~ → **RESOLVED (EPIC 4).** `script.py` passed `rendered_command` to `create_subprocess_exec` without resolving bare names against PATH/PATHEXT. EPIC 4 resolves absolute paths and bare names via `shutil.which`. (Note: the originally-reported intermittent Windows forward-slash `FileNotFoundError` could not be reproduced — forward-slash and extension-less commands execute fine via `create_subprocess_exec`.)

---

## 4. Goals and Non-Goals

### Goals

| ID | Goal | Status |
|----|------|--------|
| G1 | Agents producing large/prose output can declare `output_mode: raw` to bypass JSON envelope extraction, receiving their response as `{result: <raw text>}`. | ✅ DONE |
| G2 | Parse-exhaustion failures are marked `is_retryable=False` by default across both providers (deterministic failures should not retry). Users can opt into outer retries for parse failures via `retry.max_attempts`. | ✅ DONE |
| G3 | `max_parse_recovery_attempts` is configurable from YAML via the `retry:` block on `AgentDef`, with provider-specific defaults preserved for backward compat (Copilot=5, Claude=2). | ✅ DONE |
| G4 | A `conductor gate-respond` CLI command resolves a parked gate by POSTing to the dashboard's HTTP API, with optional token-based auth for shared infrastructure. | DONE |
| G5 | Bare command names and absolute paths in `command:` are resolved against PATH/PATHEXT via `shutil.which` before `create_subprocess_exec` (non-destructive: unresolved commands fall through unchanged; relative paths with a separator are left for `working_dir` resolution), and the `FileNotFoundError` message includes the resolved command and a Windows hint. | DONE |

### Non-Goals

| ID | Non-Goal | Rationale |
|----|----------|-----------|
| NG1 | Brace-balanced JSON extractor | Diminishing returns once `output_mode: raw` exists. Consider as a follow-up only if residual parse failures persist. |
| NG2 | Validate-time heuristic warning for `output:` on prose-likely agents | Fragile heuristic (would need NLP on prompt content). Documentation + `output_mode` field suffice. |
| NG3 | Default-flip to `output_mode: raw` in v0.2.x | Too disruptive. Current plan is additive-only. Revisit in a future major version. |
| NG4 | WebSocket keepalive / heartbeat | Orthogonal to the gate-resolution gap. The CLI command is the fallback that makes keepalive optional. |
| NG5 | Webhook notification on gate-waiting | Out of scope for v0.1.x. |

---

## 5. Requirements

### Functional Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| FR-1 | `AgentDef` in `config/schema.py` accepts `output_mode: raw \| envelope` (optional, default `None` = current behavior). | ✅ DONE (`schema.py:508-523`) |
| FR-2 | When `output_mode: raw` is set, the provider skips schema instruction injection and JSON envelope extraction, wrapping the response in `{"result": <raw text>}`. | ✅ DONE (`copilot.py:524,670-678`, `claude.py:922`) |
| FR-3 | `output_mode: envelope` with `output:` declared behaves identically to current behavior (full backward compat). | ✅ DONE |
| FR-4 | `output_mode: raw` with `output:` declared raises `ValidationError` at config load time ("output_mode 'raw' is incompatible with output schema declaration"). | ✅ DONE (`schema.py:798-802`) |
| FR-5 | Parse-exhaustion `ProviderError` in both `copilot.py` and `claude.py` is raised with `is_retryable=False`. | ✅ DONE (`copilot.py:748`, `claude.py:1887`) |
| FR-6 | `RetryPolicy` in `config/schema.py` accepts `max_parse_recovery_attempts: int` (optional, default `None` = provider default). | ✅ DONE |
| FR-7 | `_resolve_retry_config()` in both providers propagates the per-agent `max_parse_recovery_attempts` when set. | ✅ DONE |
| FR-8 | Parse-failure error messages include the first 500 characters of the response. | ✅ DONE (`copilot.py:744`, `copilot.py:1122`) |
| FR-9 | `conductor gate-respond` CLI command accepts `--port` (to identify the running instance) and `--choice` / `--input` to resolve the gate. | DONE |
| FR-10 | `web/server.py` exposes a `POST /api/gate-respond` HTTP endpoint accepting JSON `{agent_name, selected_value, additional_input?}`. The endpoint returns `409` when no gate is currently waiting or when `agent_name` does not match the waiting gate (post-review: prevents a mismatched response being silently queued so the gate never resolves). | DONE |
| FR-11 | Gate-respond endpoint validates an optional `CONDUCTOR_GATE_TOKEN` env var when set. The token is read from the `Authorization: Bearer <token>` header (post-review: moved off the JSON body) and compared with `hmac.compare_digest` for constant-time matching. | DONE |
| FR-12 | `script.py` resolves `rendered_command` via `shutil.which` when it is an absolute path or a bare name (no separator) before calling `create_subprocess_exec`, falling back to the rendered value when unresolved. Args are never resolved (they may contain URLs or flags with `/`). |
| FR-13 | The `FileNotFoundError` handler in `script.py` includes the resolved command, working directory, and (on Windows) a hint to include the file extension or use an absolute path. |

### Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR-1 | Every FR has at least one unit/integration test that fails before the fix and passes after. |
| NFR-2 | `make check` (lint + typecheck) and `make test` pass after each epic. |
| NFR-3 | Provider parity: any behavioral change in `copilot.py` is mirrored in `claude.py`. |
| NFR-4 | Run/resume parity: any new CLI flag on `run` is mirrored on `resume` per AGENTS.md. |

---

## 6. Proposed Design

### 6.1 Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                      config/schema.py                       │
│  AgentDef                                                   │
│  ├── output_mode: Literal["raw","envelope"] | None  ✅ DONE │
│  ├── output: dict[str, OutputField] | None                  │
│  └── retry: RetryPolicy | None                              │
│       └── max_parse_recovery_attempts: int | None   (NEW)   │
├─────────────────────────────────────────────────────────────┤
│               providers/copilot.py + claude.py              │
│  _execute_sdk_call / _execute_with_retry                    │
│  ├── Check agent.output_mode → skip schema if raw   ✅ DONE │
│  ├── Parse-exhaustion → is_retryable=False           ✅ DONE │
│  └── _resolve_retry_config → propagate per-agent            │
│       max_parse_recovery_attempts                   (FIX)   │
├─────────────────────────────────────────────────────────────┤
│                    executor/script.py                        │
│  ├── Resolve command via shutil.which (PATH/PATHEXT)  (NEW)  │
│  └── Improved FileNotFoundError message             (FIX)   │
├─────────────────────────────────────────────────────────────┤
│                     web/server.py                            │
│  └── POST /api/gate-respond endpoint                (NEW)   │
├─────────────────────────────────────────────────────────────┤
│                      cli/app.py                             │
│  └── conductor gate-respond command                 (NEW)   │
└─────────────────────────────────────────────────────────────┘
```

### 6.2 Key Components

#### 6.2.1 `output_mode` Field on `AgentDef` (Issue #2) — ✅ SHIPPED

**Status:** Fully implemented and tested. Retained here as a reference for the design rationale.

**Location:** `src/conductor/config/schema.py`, `AgentDef` class (line 508)

The `output_mode` field is defined at `schema.py:508-523`:

```python
output_mode: Literal["raw", "envelope"] | None = None
```

**Validation rules (model_validator on AgentDef, line 700):**
- `output_mode: raw` + `output:` declared → `ValidationError` (line 798-802)
- `output_mode` on `human_gate` → `ValidationError` (line 717-718)
- `output_mode` on `script` → `ValidationError` (line 753-754)
- `output_mode` on `workflow` → `ValidationError` (line 782-783)

**Provider behavior (implemented):**

| `output_mode` | `output:` | Schema injected? | JSON extracted? | Content shape |
|---|---|---|---|---|
| `None` | present | Yes | Yes (current) | `{field1: ..., field2: ...}` |
| `None` | absent | No | No (current) | `{"result": "<text>"}` |
| `raw` | absent | No | No | `{"result": "<text>"}` |
| `envelope` | present | Yes | Yes | `{field1: ..., field2: ...}` |
| `envelope` | absent | No | No | `{"result": "<text>"}` |
| `raw` | present | ❌ ValidationError | — | — |

**Implemented in providers:**

- **Copilot** (`copilot.py:524`): `has_schema = agent.output and agent.output_mode != "raw"` — skips schema instruction injection when `output_mode == "raw"`.
- **Copilot** (`copilot.py:670-678`): When `not has_schema`, wraps as `{"result": response_content}`.
- **Claude** (`claude.py:922`): `has_schema = agent.output is not None and agent.output_mode != "raw"` — skips `emit_output` tool injection.

**Tests:** `tests/test_config/test_output_mode.py` (10 schema tests), `tests/test_providers/test_output_mode.py` (10+ provider behavior tests).

**Docs:** `docs/workflow-syntax.md:159-203`, `CHANGELOG.md:11-16`.

#### 6.2.2 Parse Recovery Config + Outer Retry Budget (Issues #10 + #6)

**Issue #10 status: ✅ RESOLVED.** Both providers mark parse-exhaustion as `is_retryable=False` (`copilot.py:748`, `claude.py:1887`). The 3× outer-retry amplification no longer occurs.

**Issue #6 status: ✅ RESOLVED (EPIC 2, PR #234).** `max_parse_recovery_attempts` is now user-configurable from YAML and threaded through to the parse recovery loop in both providers. The root-cause analysis below is retained as design context.

**Root cause of Issue #6:**

In `copilot.py`, the `RetryConfig` dataclass defaults `max_parse_recovery_attempts=5` (line 81). When the agent has a per-agent `retry:` policy, `_resolve_retry_config()` (line 278-304) builds a new `RetryConfig` but copies `max_parse_recovery_attempts` from `self._retry_config.max_parse_recovery_attempts` (line 303) — always the provider-level default, never from YAML.

In `claude.py`, the same `RetryConfig` defaults `max_parse_recovery_attempts=2` (line 106). `_resolve_retry_config()` (line 660-686) copies from the provider-level default at line 685.

**Critical threading gap (resolved by EPIC 2):**

Even if `_resolve_retry_config()` propagated a per-agent value, it would not reach the parse recovery loop:

- **Copilot**: `_execute_with_retry` (line 306) resolves the config at line 335, but `_execute_sdk_call` reads `self._retry_config.max_parse_recovery_attempts` at line 681 (the provider-level default, not the per-agent resolved config). Fix: pass the resolved `RetryConfig` from `_execute_with_retry` into `_execute_sdk_call` as a parameter, and use it at line 681.
- **Claude**: `_execute_with_parse_recovery` (line 1738) reads `self._max_parse_recovery_attempts` (an instance variable set at line 191 from the provider-level default, not the resolved config). Fix: add a `max_parse_recovery_attempts` parameter to `_execute_with_parse_recovery` and pass the resolved value from the agentic loop.

**Fix — Expose `max_parse_recovery_attempts` in YAML schema:**

Add to `RetryPolicy` in `config/schema.py` (after line 395):

```python
max_parse_recovery_attempts: int | None = Field(default=None, ge=0, le=10)
"""Maximum in-session parse-recovery attempts before giving up.

When an agent's response fails JSON extraction, Conductor sends a
correction prompt in the same session. This field controls how many
correction prompts to send.

- ``None`` (default): Use the provider default (Copilot=5, Claude=2).
- ``0``: Disable parse recovery entirely (fail immediately on bad JSON).
- ``1-10``: Custom limit.
"""
```

Update `_resolve_retry_config()` in both providers (`copilot.py:278-304`, `claude.py:660-686`) to propagate the per-agent value:

```python
# In _resolve_retry_config, when building RetryConfig from RetryPolicy:
max_parse = retry.max_parse_recovery_attempts  # from YAML
if max_parse is None:
    max_parse = self._retry_config.max_parse_recovery_attempts  # provider default
return RetryConfig(
    ...
    max_parse_recovery_attempts=max_parse,
)
```

Then thread the resolved config to the parse recovery loop (see EPIC 2 tasks for details).

**Part C — Longer error prefix in parse-failure messages: ✅ SHIPPED**

Both truncation points already use 500-char prefixes:
- `copilot.py:744`: `response_content[:500]`
- `copilot.py:1122`: `content[:500]` (in `_extract_json`)
- Suggestion text mentions `output_mode: raw` (`copilot.py:745-746`, `claude.py:1884-1885`)

#### 6.2.3 CLI `gate-respond` Command (Issue #5) — ✅ SHIPPED

> **Hardening (post-review):** Two refinements were applied during adversarial
> review. (1) The endpoint now rejects responses that don't match the waiting
> gate — if no gate is waiting or `agent_name` differs from the waiting agent it
> returns `409` instead of silently queuing a payload that would never resolve
> the gate (and could poison a later, unrelated gate). (2) The auth token moved
> off the JSON body to the `Authorization: Bearer <token>` header and is compared
> with `hmac.compare_digest` for constant-time matching.

**HTTP endpoint** in `web/server.py`:

```python
@app.post("/api/gate-respond")
async def gate_respond_api(request: Request) -> JSONResponse:
    """Resolve a parked human gate via HTTP POST.

    Body: {"agent_name": str, "selected_value": str, "additional_input": str?}
    Auth: optional `Authorization: Bearer <token>` header.
    """
    # Validate token (header, hmac.compare_digest) if CONDUCTOR_GATE_TOKEN is set
    # Reject (409) if no gate waiting or agent_name != waiting agent
    ...
    self._gate_response_queue.put_nowait(body)
    return JSONResponse({"status": "accepted"})
```

This is a simple adapter that puts the same payload onto `_gate_response_queue` that the WebSocket handler at `server.py:326-327` does today. The `wait_for_gate_response()` method (which sets/clears `self._gate_waiting_agent` in a try/finally) is unchanged.

**New CLI command** in `cli/app.py`:

```python
@app.command()
def gate_respond(
    port: Annotated[int, typer.Option("--port", "-p", help="Dashboard port")],
    choice: Annotated[str, typer.Option("--choice", "-c", help="Selected gate option value")],
    agent: Annotated[str | None, typer.Option("--agent", "-a", help="Gate agent name")] = None,
    input_text: Annotated[str | None, typer.Option("--input", help="Additional input")] = None,
    token: Annotated[str | None, typer.Option("--token", help="Auth token")] = None,
):
    """Resolve a parked human gate from the command line."""
    import httpx
    ...
```

The `--port` flag identifies the running dashboard. If `--agent` is omitted, the command queries `/api/status` (new trivial endpoint returning the currently-waiting gate name, if any) to discover it.

**Security model (Open Question #3 resolution):**

- When `CONDUCTOR_GATE_TOKEN` env var is set on the workflow process, the `POST /api/gate-respond` endpoint requires a matching token supplied via the `Authorization: Bearer <token>` header, compared with `hmac.compare_digest`.
- When unset (default), no auth is required. The dashboard binds to `127.0.0.1` by default, which limits the attack surface to local processes.
- This is proportional to the current security posture: `POST /api/stop` and `POST /api/kill` already exist without auth. The gate-respond endpoint follows the same pattern.

**Run/Resume parity:** No new flag on `run`/`resume` — the gate-respond command operates independently on a running instance. The `--port` flag comes from the PID file or user knowledge.

#### 6.2.4 Command Resolution (Issue #3)

**Location:** `src/conductor/executor/script.py:83-118`

> **Correction (post-review):** The original plan called for blindly replacing
> `/` with `\` on Windows. That was rejected during adversarial review: the swap
> is a deterministic transform that cannot explain an *intermittent*
> `FileNotFoundError`, it can mangle command strings, and the hint checked the
> unrendered `agent.command` template. Empirical testing on Windows confirmed
> that forward-slash paths and extension-less commands already execute fine via
> `create_subprocess_exec`. The shipped fix instead resolves the command against
> PATH/PATHEXT with `shutil.which`, which is non-destructive and adds real value
> (bare-name → executable resolution).

After template rendering, resolve the command (not args) when it is an absolute
path or a bare name without a separator; relative paths with a separator are
left untouched so they resolve against `working_dir`:

```python
import os
import shutil

has_separator = os.sep in rendered_command or (
    os.altsep is not None and os.altsep in rendered_command
)
if os.path.isabs(rendered_command) or not has_separator:
    rendered_command = shutil.which(rendered_command) or rendered_command
```

Improve the `FileNotFoundError` handler:

```python
except FileNotFoundError as exc:
    hint = ""
    if sys.platform == "win32":
        hint = (
            " Hint: on Windows, include the file extension (e.g. .exe) "
            "or use an absolute path."
        )
    raise ExecutionError(
        f"Script '{agent.name}': command not found: '{rendered_command}'"
        f" (working_dir={rendered_working_dir or 'cwd'}){hint}",
        agent_name=agent.name,
        suggestion=f"Ensure '{rendered_command}' is installed and on PATH",
    ) from exc
```

### 6.3 Design Decisions

| Decision | Rationale | Alternatives Considered | Status |
|----------|-----------|------------------------|--------|
| `output_mode` is additive with `None` default (Open Q #1 → option (a)) | Preserves full backward compat. Existing workflows continue unchanged. No deprecation churn. | (b) additive + warn — rejected as too heuristic-dependent; (c) default-flip in v0.2.x — too disruptive for a point release. | ✅ SHIPPED |
| Parse-exhaustion marked `is_retryable=False` (Open Q #4 → simpler option) | Deterministic failures don't benefit from retries. Users opt in via YAML `retry.max_attempts`. | Smarter classifier that detects same-error-twice — more complex, fragile, and solves the same problem less transparently. | ✅ SHIPPED |
| Per-agent `max_parse_recovery_attempts` on `RetryPolicy` with `None` default (Open Q #2 → single field, provider defaults preserved) | A single field that both providers respect. `None` means "use provider default" so Copilot=5 and Claude=2 remain the out-of-box experience. | Separate per-provider defaults in YAML — over-engineered; users shouldn't need to know which provider they're targeting. | ✅ DONE |
| Gate-respond via HTTP POST, not WebSocket (Open Q #3) | HTTP POST is simpler for CLI tooling (`httpx` one-shot). The existing WebSocket path continues to work for the dashboard. | WebSocket-only — would require the CLI to maintain a persistent connection, which is overkill for a one-shot operation. | ✅ DONE |
| Token auth is opt-in via env var, not mandatory; supplied via `Authorization: Bearer` header and compared with `hmac.compare_digest` (post-review) | Matches current security posture (`POST /api/stop` has no auth). Avoids breaking changes for localhost-only deployments. Header + constant-time compare avoids body-borne secrets and timing leaks. | Mandatory token — would break existing `--web-bg` setups that don't set env vars. | ✅ DONE |

---

## 7. Dependencies

### External Dependencies

- **httpx** — for the `gate-respond` CLI command to POST to the dashboard. Already a direct dependency at `pyproject.toml:46` (`httpx>=0.27.0`). No addition needed.

### Internal Dependencies

- **EPIC 1** (output_mode + retry fixes) is **SHIPPED**. No dependencies remain.
- **EPIC 2** (max_parse_recovery in YAML) depends on EPIC 1 for the `is_retryable=False` fix (otherwise the retry budget change would be undermined by outer retries). EPIC 1 is done, so EPIC 2 can proceed immediately.
- **EPIC 3** (gate-respond) is independent of EPICs 1-2.
- **EPIC 4** (Windows paths) is independent of all other epics.

### Sequencing Constraints

- EPIC 1 is shipped. The `is_retryable=False` prerequisite for EPIC 2 is satisfied.
- EPICs 2, 3, and 4 are fully independent and can be parallelized across PRs.

---

## 8. Impact Analysis

### Components Affected

| Component | Change Type | Risk | Status |
|-----------|-------------|------|--------|
| `config/schema.py` | New field on `AgentDef`, new field on `RetryPolicy` | Low — additive, validated | `output_mode` ✅ DONE; `max_parse_recovery_attempts` ✅ DONE |
| `providers/copilot.py` | Skip schema injection for `raw`, mark parse-exhaustion non-retryable, propagate per-agent parse recovery | Medium — hot path | `output_mode` + `is_retryable` ✅ DONE; parse recovery config ✅ DONE |
| `providers/claude.py` | Mirror all copilot changes per Provider Parity | Medium — hot path | `output_mode` + `is_retryable` ✅ DONE; parse recovery config ✅ DONE |
| `executor/script.py` | `shutil.which` command resolution + error message | Low — non-destructive, cross-platform | DONE |
| `web/server.py` | New HTTP endpoint (with `agent_name` 409 validation + header/hmac token auth) | Low — additive | ✅ DONE |
| `cli/app.py` | New command | Low — additive | ✅ DONE |
| `executor/output.py` | No changes needed | None | — |

### Backward Compatibility

- **`output_mode`**: ✅ Shipped. Default `None` preserves current behavior. No existing YAML breaks.
- **`is_retryable=False`**: ✅ Shipped. Workflows that relied on outer-retry-after-parse-exhaustion (unlikely intentional) now fail after inner recovery exhausts. Mitigation: set `retry.max_attempts: 3` to restore old behavior.
- **`max_parse_recovery_attempts`**: Default `None` = provider default. No existing YAML breaks.
- **Gate-respond endpoint**: Additive. No existing clients break.
- **Command resolution**: `shutil.which` is cross-platform and non-destructive — unresolved commands fall through to the original rendered value.

---

## 9. Security Considerations

### Gate-Respond Endpoint (Issue #5)

The new `POST /api/gate-respond` endpoint creates a control plane surface:

- **Default posture**: Dashboard binds to `127.0.0.1`. Gate-respond is local-only, same as `POST /api/stop` and `POST /api/kill`.
- **Shared infrastructure**: When `CONDUCTOR_GATE_TOKEN` is set, the endpoint validates a bearer token. This prevents unauthorized gate resolution in multi-user environments.
- **No escalation path**: Gate-respond submits a choice from the pre-defined options list. It cannot inject arbitrary commands or modify workflow state beyond gate resolution.

### `output_mode: raw`

- No security impact. Raw mode returns the model's text as-is without parsing — this is *less* code executed, not more.

---

## 10. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `is_retryable=False` breaks workflows that accidentally depended on outer-retry-for-parse | Low | Medium | Document in CHANGELOG; provide `retry.max_attempts` opt-in |
| `output_mode` naming confusion with `output:` | Low | Low | Clear docstring and YAML validation error messages |
| Gate-respond endpoint used for unauthorized gate resolution | Low | Medium | Token auth when `CONDUCTOR_GATE_TOKEN` is set |
| Windows path normalization breaks non-path args containing `/` | Low | Low | Only normalize `rendered_command`, not args. Args may contain URL-like values or flags with `/` and must not be altered. |

---

## 11. Open Questions (from Brainstorm) — Resolutions

| # | Question | Resolution |
|---|----------|------------|
| 1 | Output mode default: additive vs additive+warn vs default-flip? | **Additive with `None` default.** No warn heuristic (too fragile), no default-flip (too disruptive). Revisit in v0.2.x if adoption data warrants. |
| 2 | Provider parity for retry budget: single value or per-provider defaults? | **Single YAML field, provider defaults preserved via `None`.** When `max_parse_recovery_attempts` is unset, Copilot uses 5 and Claude uses 2. When set, both use the specified value. |
| 3 | Gate-resolution endpoint security? | **Opt-in `CONDUCTOR_GATE_TOKEN` env var.** Proportional to current security posture. No mandatory auth for localhost. |
| 4 | Smarter retry classifier vs per-agent knob? | **Per-agent `is_retryable=False` on parse-exhaustion + YAML `retry.max_attempts` for opt-in.** Simpler, more transparent, no false-positive risk from heuristic classifier. |

---

## 12. Implementation Phases

### Phase 1: Output Mode + Retry Fixes (Issues #2, #10) — ✅ SHIPPED

**Exit criteria (met):** A workflow declaring `output_mode: raw` on a large-output agent completes without parse-recovery loops. Parse-exhaustion failures do not trigger outer retries. All tests pass.

### Phase 2: YAML-Configurable Parse Recovery (Issue #6) — ✅ SHIPPED

**Exit criteria:** `max_parse_recovery_attempts` is configurable from YAML `retry:` block. Per-agent value reaches the parse recovery loop in both providers. Provider defaults preserved when field is omitted.

### Phase 3: CLI Gate-Respond (Issue #5) — DONE

**Exit criteria:** `conductor gate-respond --port <port> --choice <value>` resolves a parked gate. Token auth works when `CONDUCTOR_GATE_TOKEN` is set.

### Phase 4: Windows Path Normalization (Issue #3) — DONE

**Exit criteria:** A script step with `command: "C:/Python314/python.exe"` succeeds on Windows without manual backslash workaround.

---

## 13. Files Affected

### New Files

| File Path | Purpose |
|-----------|---------|
| `tests/test_providers/test_parse_recovery_config.py` | Tests for per-agent `max_parse_recovery_attempts` propagation |
| `tests/test_cli/test_gate_respond.py` | CLI gate-respond command tests |
| `tests/test_web/test_gate_respond_api.py` | HTTP gate-respond endpoint tests |

### Modified Files

| File Path | Changes |
|-----------|---------|
| `src/conductor/config/schema.py` | Add `max_parse_recovery_attempts` to `RetryPolicy` (after line 395) |
| `src/conductor/providers/copilot.py` | Propagate per-agent `max_parse_recovery_attempts` in `_resolve_retry_config` (line 303), thread resolved config into `_execute_sdk_call` for the parse recovery loop (line 681) |
| `src/conductor/providers/claude.py` | Propagate per-agent `max_parse_recovery_attempts` in `_resolve_retry_config` (line 685), add parameter to `_execute_with_parse_recovery` (line 1738) to accept resolved value instead of reading instance variable (line 1813) |
| `src/conductor/executor/script.py` | Add Windows path normalization after line 86, improve `FileNotFoundError` message at line 113 |
| `src/conductor/web/server.py` | Add `POST /api/gate-respond` endpoint (after line 234), add `/api/gate-status` endpoint |
| `src/conductor/cli/app.py` | Add `gate_respond` command, update `--web-bg` + `human_gate` message text (line 191) |
| ~~`pyproject.toml`~~ | ~~Add `httpx` to dependencies~~ — **No change needed:** `httpx>=0.27.0` already present at line 46 |
| `CHANGELOG.md` | Document `max_parse_recovery_attempts`, `gate-respond`, and Windows path normalization |
| `docs/workflow-syntax.md` | Document `retry.max_parse_recovery_attempts` field with examples |

### Deleted Files

| File Path | Reason |
|-----------|--------|
| (none) | |

---

## 14. Implementation Plan

### EPIC 1: `output_mode` Field + Parse-Exhaustion Retry Fix — ✅ SHIPPED

**Goal:** Add `output_mode: raw | envelope` to `AgentDef` and fix the outer-retry amplification on parse-exhaustion failures.

**Prerequisites:** None

**All tasks verified as implemented in the current codebase:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E1-T1 | IMPL | `output_mode: Literal["raw", "envelope"] \| None = None` field on `AgentDef` with docstring and model_validator rules (raw+output → error, reject on script/human_gate/workflow). | `config/schema.py:508-523, 717-718, 753-754, 782-783, 798-802` | DONE |
| E1-T2 | TEST | Unit tests for `output_mode` validation: raw+no output, envelope+output, raw+output→error, raw on script→error, raw on human_gate→error, raw on workflow→error, None+output, None+no output, envelope+no output, invalid value. | `tests/test_config/test_output_mode.py` (10 tests) | DONE |
| E1-T3 | IMPL | **Copilot provider**: `has_schema = agent.output and agent.output_mode != "raw"` gates schema injection (line 524). Raw path wraps as `{"result": ...}` (lines 670-678). | `providers/copilot.py` | DONE |
| E1-T4 | IMPL | **Claude provider**: `has_schema = agent.output is not None and agent.output_mode != "raw"` gates `emit_output` tool injection (line 922). When `not has_schema`, `output_schema=None` is passed to `_execute_with_parse_recovery` (line 1460). | `providers/claude.py` | DONE |
| E1-T5 | TEST | Provider tests: raw wraps as result, no schema in prompt, envelope backward compat, parse-exhaustion `is_retryable=False`, no outer retry triggered. Both providers. | `tests/test_providers/test_output_mode.py` (10+ tests) | DONE |
| E1-T6 | IMPL | **Copilot**: `is_retryable=False` on parse-exhaustion (line 748). Suggestion text mentions `output_mode: raw` (lines 745-746). | `providers/copilot.py` | DONE |
| E1-T7 | IMPL | **Claude**: `is_retryable=False` on parse-exhaustion (line 1887). Suggestion text mentions `output_mode: raw` (lines 1884-1885). Note: Claude's outer retry uses `_is_retryable_error()` isinstance checks (lines 713-769) which would return `False` for any `ProviderError` regardless; the explicit flag is for clarity and forward compatibility. | `providers/claude.py` | DONE |
| E1-T8 | IMPL | **Copilot**: 500-char error prefix on parse-exhaustion (`response_content[:500]` at line 744) and in `_extract_json` (`content[:500]` at line 1122). | `providers/copilot.py` | DONE |
| E1-T9 | TEST | Regression tests: parse-exhaustion produces `is_retryable=False`, no outer retry triggered. Both providers. | `tests/test_providers/test_output_mode.py` | DONE |
| E1-T10 | IMPL | Documentation: `output_mode` in `docs/workflow-syntax.md:159-203` with examples, `CHANGELOG.md:11-25` with migration note. | `docs/workflow-syntax.md`, `CHANGELOG.md` | DONE |

**Acceptance Criteria (all met):**
- [x] `output_mode: raw` agent produces `{"result": ...}` with no parse recovery
- [x] `output_mode: raw` + `output:` declared → validation error
- [x] Parse-exhaustion `ProviderError` has `is_retryable=False`
- [x] Error prefix shows first 500 chars of response (both `_execute_sdk_call` and `_extract_json`)
- [x] All existing tests pass (no regressions)
- [x] Provider parity: copilot and claude behave identically for `output_mode` semantics

### EPIC 2: YAML-Configurable `max_parse_recovery_attempts` — ✅ SHIPPED

**Goal:** Expose `max_parse_recovery_attempts` in the YAML schema so workflow authors can tune or disable parse recovery per agent.

**Prerequisites:** EPIC 1 (✅ SHIPPED — parse-exhaustion is non-retryable; this epic provides the correct knob)

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E2-T1 | IMPL | Add `max_parse_recovery_attempts: int \| None = Field(default=None, ge=0, le=10)` to `RetryPolicy` in `config/schema.py` with docstring. | `config/schema.py` | DONE |
| E2-T2 | IMPL | **Copilot**: In `_resolve_retry_config` (~line 296-304), when building `RetryConfig` from `RetryPolicy`, use `retry.max_parse_recovery_attempts` if not None, else fall back to `self._retry_config.max_parse_recovery_attempts`. **Additionally**, thread the resolved config into the parse recovery loop: update `_execute_with_retry` (~line 335) to pass the resolved `config` to `_execute_sdk_call` as a new parameter (e.g., `retry_config=config`). Update `_execute_sdk_call` signature to accept `retry_config: RetryConfig | None = None`, and at line 681 change `max_recovery = self._retry_config.max_parse_recovery_attempts` to `max_recovery = (retry_config or self._retry_config).max_parse_recovery_attempts`. Without this threading, the resolved per-agent value never reaches the parse recovery loop. | `providers/copilot.py` | DONE |
| E2-T3 | IMPL | **Claude**: Mirror E2-T2 in `_resolve_retry_config` (~line 678-686). Also update `_execute_with_parse_recovery` (line 1738) to accept a `max_parse_recovery_attempts: int` parameter instead of reading from `self._max_parse_recovery_attempts` (instance variable, line 191). Update the call sites in the agentic loop (lines 1404 and 1460) to pass the resolved value from the retry config. The resolved config is available at `_execute_with_retry` (line 873) but currently is not threaded through `_execute_agentic_loop` → `_execute_with_parse_recovery`. | `providers/claude.py` | DONE |
| E2-T4 | TEST | Schema tests: (a) `max_parse_recovery_attempts: 0` → valid, (b) `max_parse_recovery_attempts: 10` → valid, (c) `max_parse_recovery_attempts: -1` → validation error, (d) `max_parse_recovery_attempts: 11` → validation error, (e) omitted → None (provider default). | `tests/test_config/` | DONE |
| E2-T5 | TEST | Provider tests: (a) agent with `retry.max_parse_recovery_attempts: 2` → copilot uses 2 (not default 5), (b) agent with `retry.max_parse_recovery_attempts: 0` → no parse recovery attempted, (c) agent without the field → copilot uses 5, claude uses 2. Both providers. | `tests/test_providers/test_parse_recovery_config.py` | DONE |
| E2-T6 | IMPL | Update `docs/workflow-syntax.md` retry section with `max_parse_recovery_attempts` docs and example. | `docs/` | DONE |

**Acceptance Criteria:**
- [x] `retry.max_parse_recovery_attempts: 0` disables parse recovery
- [x] Provider defaults preserved when field is omitted (Copilot=5, Claude=2)
- [x] Per-agent value overrides provider default for both providers
- [x] Validation rejects out-of-range values

### EPIC 3: CLI Gate-Respond Command — ✅ SHIPPED

**Goal:** Allow users to resolve parked gates from the command line when the dashboard is unreachable.

**Prerequisites:** None (independent of EPICs 1-2)

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E3-T1 | IMPL | Add `POST /api/gate-respond` endpoint to `web/server.py`. Accepts JSON body `{agent_name, selected_value, additional_input?}`. Validates `CONDUCTOR_GATE_TOKEN` via the `Authorization: Bearer` header using `hmac.compare_digest` when set. Returns `409` when no gate is waiting or `agent_name` doesn't match the waiting gate. Puts payload onto `_gate_response_queue`. | `web/server.py` | DONE |
| E3-T2 | IMPL | Add `GET /api/gate-status` endpoint to `web/server.py`. Returns JSON `{waiting: bool, agent_name: str?}` reflecting whether a gate is currently waiting. Requires the engine to set a flag on the dashboard when a gate is entered/exited. | `web/server.py` | DONE |
| E3-T3 | IMPL | Add `gate-respond` command to `cli/app.py`. Options: `--port` (required), `--choice` (required), `--agent` (optional, auto-discovered via `/api/gate-status`), `--input` (optional additional text), `--token` (optional auth token, also reads from `CONDUCTOR_GATE_TOKEN` env). Uses `httpx.post` to `http://127.0.0.1:<port>/api/gate-respond`. | `cli/app.py` | DONE |
| E3-T4 | — | ~~Add `httpx` to `pyproject.toml` dependencies.~~ **No-op:** `httpx>=0.27.0` is already a direct dependency at `pyproject.toml:46`. No action needed. | `pyproject.toml` | DONE |
| E3-T5 | TEST | Unit tests for `POST /api/gate-respond`: (a) valid request → 200 + payload on queue, (b) missing `selected_value` → 422, (c) token mismatch via `Authorization` header when `CONDUCTOR_GATE_TOKEN` set → 403, (d) no token required when env var unset → 200, (e) token in JSON body is rejected, (f) no gate waiting / `agent_name` mismatch → 409. | `tests/test_web/test_gate_respond_api.py` | DONE |
| E3-T6 | TEST | CLI tests for `gate-respond` command: (a) happy path with mock server, (b) unreachable port → clear error, (c) token passed from `--token` and from env var. | `tests/test_cli/test_gate_respond.py` | DONE |
| E3-T7 | IMPL | Update `cli/app.py` line 191 text: change "Wait for CLI gate-resolution support (planned follow-up)" → "Use `conductor gate-respond --port <port> --choice <value>` to resolve from CLI". | `cli/app.py` | DONE |

**Acceptance Criteria:**
- [x] `conductor gate-respond --port 8080 --choice approve` resolves a parked gate
- [x] Token auth rejects unauthorized requests when `CONDUCTOR_GATE_TOKEN` is set
- [x] Auto-discovery of gate agent name via `/api/gate-status` works
- [x] Error messages are clear when port is unreachable or no gate is waiting
- [x] `--web-bg` + `human_gate` error message references the new command

### EPIC 4: Command Resolution — ✅ SHIPPED

**Goal:** Resolve bare command names and absolute paths in script `command:` against PATH/PATHEXT via `shutil.which`, and emit a clearer `FileNotFoundError` message.

**Prerequisites:** None (independent)

> **Correction (post-review):** The original tasks below called for blindly
> replacing `/` with `\` on Windows. That deterministic transform was rejected
> during adversarial review (it cannot explain an *intermittent* failure, can
> mangle command strings, and the hint checked the unrendered template). The
> shipped fix resolves the command via `shutil.which` instead — non-destructive
> and adds bare-name → executable resolution. Empirical testing confirmed
> forward-slash and extension-less commands already run fine.

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E4-T1 | IMPL | In `script.py`, after rendering `rendered_command`, resolve it via `shutil.which` when it is an absolute path or a bare name (no separator); leave relative paths with a separator for `working_dir` resolution; fall back to the rendered value when unresolved. Only resolve `rendered_command`, never args. | `executor/script.py` | DONE |
| E4-T2 | IMPL | Improve `FileNotFoundError` handler: include the resolved command, working directory, and (on Windows) a hint to include the file extension or use an absolute path. | `executor/script.py` | DONE |
| E4-T3 | TEST | Unit tests (patching `shutil.which`): bare name resolved via `which`, absolute path resolved via `which`, `which` returning None falls back to rendered, relative path with separator not resolved, args not resolved, `FileNotFoundError` message includes the Windows hint, no hint on Linux. | `tests/test_executor/test_script.py` | DONE |

**Acceptance Criteria:**
- [x] Bare command names and absolute paths are resolved against PATH/PATHEXT via `shutil.which`
- [x] Unresolved commands fall through unchanged; relative paths with a separator are left for `working_dir`
- [x] `FileNotFoundError` message includes resolved command and Windows-specific hint
- [x] Args are not resolved (may contain `/` for legitimate purposes)

---

## 15. References

- [external-workflow-friction.brainstorm.md](external-workflow-friction.brainstorm.md) — source analysis with Phase 1/2/3 validation evidence
- [external-workflow-friction.plan.md](external-workflow-friction.plan.md) — prior plan (v1), shipped in v0.1.17
- [AGENTS.md](../../../AGENTS.md) — architecture overview, Provider Parity rule, Run/Resume Parity rule
- `src/conductor/config/schema.py` — `AgentDef` (line 450), `output_mode` (line 508), `RetryPolicy` (line 359), `OutputField` (line 61), `validate_agent_type` (line 700)
- `src/conductor/providers/copilot.py` — `RetryConfig` (line 59), `_resolve_retry_config` (line 278), `_execute_with_retry` (line 306), `has_schema` check (line 524), parse recovery loop (line 681), parse-exhaustion error (line 740, `is_retryable=False` at line 748), `_extract_json` (line 1081, truncation at line 1122)
- `src/conductor/providers/claude.py` — `RetryConfig` (line 85), `_resolve_retry_config` (line 660), `_execute_with_retry` (line 834), `_is_retryable_error` (line 713, now honors `ProviderError.is_retryable` first, then falls back to isinstance-based SDK checks), `has_schema` check (line 922), `_execute_with_parse_recovery` (line 1738), parse-exhaustion error (line 1878, `is_retryable=False` at line 1887), `_max_parse_recovery_attempts` instance variable (line 191)
- `src/conductor/executor/script.py` — `create_subprocess_exec` (line 105), `FileNotFoundError` handler (line 113)
- `src/conductor/web/server.py` — gate response queue (line 85), WebSocket handler (line 326), `wait_for_gate_response` (line 712)
- `src/conductor/cli/app.py` — `_abort_web_bg_if_human_gate` (line 158), command definitions
- `src/conductor/cli/pid.py` — PID file schema for port discovery
- `tests/test_config/test_output_mode.py` — 10 schema validation tests for `output_mode`
- `tests/test_providers/test_output_mode.py` — provider behavior tests for `output_mode` and `is_retryable=False`
