# Implementation Plan: Agent-in-Sandbox via a remote `aca` provider

> **Source design (authoritative):**
> [`docs/projects/aca/aca-provider.design.md`](./aca-provider.design.md)
> — *Solution Design: Agent-in-Sandbox via a remote `aca` provider*
> (issue [microsoft/conductor#284](https://github.com/microsoft/conductor/issues/284)).
> **Preview UX:** [`docs/projects/aca/aca-provider-example.yaml`](./aca-provider-example.yaml).
>
> **Revision notes:** Initial draft.
>
> This plan consumes an already-reviewed design. It does **not** re-derive or
> re-litigate design decisions; each epic references the design section it
> delivers (e.g. *DD3*, *Key Components §2*, *Data Flow*). Genuine gaps that
> blocked confident planning are surfaced in **Open Questions** rather than
> silently resolved.

---

## Open Questions

These are gaps, ambiguities, or inconsistencies in the source design (or between
the design and the current code) that affect *how* a task is built. Each is
tagged with the epic it blocks. None block starting Phase 1; most need an answer
before the epic they tag reaches "done".

1. **Concurrency-unit detection at `execute()` (blocks E3 · identifier
   derivation).** *Data Flow* makes `concurrent_safe=True` honest by appending a
   **mandatory concurrency discriminator** for "any concurrent unit (empty
   otherwise)". But the provider's only per-call context is the `execute()`
   signature (`agent`, `context`, `rendered_prompt`, `tools`, `interrupt_signal`,
   `event_callback` — `providers/base.py:234`). The design says the discriminator
   derives from the for-each keys `_key`/`_index` "already in per-iteration
   `context`" — yet the engine injects `_index`/`_key` for **both** serial
   (`max_concurrent: 1`) and concurrent for-each iterations, and never for a
   plain agent self-loop. So "append when concurrent, empty otherwise" is not
   directly observable from `context`. **Decision needed:** either (a) the engine
   passes an explicit concurrency signal into `execute()` (new field/kwarg, a
   provider-contract change touching `executor/agent.py:288` and every provider),
   or (b) the provider diverges the identifier whenever a loop key is present —
   which sacrifices cross-item workspace reuse for a *serial* `for_each` under
   the default `agent` scope. The design asserts the outcome but not the seam.

2. **Session-seconds → usage mechanism (blocks E6 · FR7).** *FR7* / *Key
   Components §5* require sandbox time as a **distinct usage row** ("as the
   validator feature records `\"<agent> (validator)\"`"), separate from token
   cost. But `AgentOutput` (`providers/base.py:66`) has no session-seconds field,
   and `UsageTracker.record()` (`engine/usage.py:178`) derives everything from
   `AgentOutput` token/model fields plus an `elapsed` argument the engine already
   measures. **Decision needed:** the surfacing seam. Recommended (and assumed by
   E6): add an optional `session_seconds: float | None` to `AgentOutput`,
   populate it from the runner's terminal `result` frame, and have the engine
   record a distinct `\"<agent> (sandbox)\"` row (cost `None`, `elapsed_seconds =
   session_seconds`) mirroring the validator-row pattern
   (`engine/workflow.py:2778`). The design names the *what* (distinct row) but not
   this *how*; the exact CLI-summary / dashboard rendering is already a design
   *Future* open question ("Cost model surfacing").

3. **`sandbox.working_dir` vs. existing `agent.working_dir` (blocks E1 · schema)
   — RESOLVED.**
   The *Open Questions → working_dir* answer maps `working_dir` to the existing
   `working_dir` capability (`capabilities.py:133`, flipped to `True`). But the
   codebase already has a top-level `AgentDef.working_dir` (`schema.py:765`) that
   the engine resolves **against the workflow file's directory** (a *host* path)
   and the validator gates on `capabilities.working_dir`
   (`validator.py:1020,1738`). The preview YAML instead nests `working_dir` inside
   a new `sandbox:` block with **container-relative** semantics (and the file
   itself flags this as "NOT yet decided"). **Decision needed:** either reuse
   `agent.working_dir` and have the `aca` provider reinterpret it container-
   relative (skipping the engine's host-path resolution for `aca`-backed agents),
   or introduce `sandbox.working_dir` and define how it interacts with the
   `working_dir` capability check. This changes the schema shape (E1), the
   engine's path-resolution branch, and the validator.

   **Answer (E1) — introduce `sandbox.working_dir` as a distinct,
   container-relative field.** `AgentDef.working_dir` keeps its existing host-path
   semantics unchanged (still resolved against the workflow file's directory for
   every provider, still rejected on wait/set/terminate/human_gate/workflow).
   `SandboxConfig.working_dir` (new, `schema.py`) is a *separate* field, only
   reachable via the per-agent `sandbox:` block, and is documented as
   container-relative — a path inside the remote session filesystem, never
   resolved against the host workflow directory. This avoids overloading one
   field with two incompatible path semantics (host vs. container) and keeps the
   existing `working_dir` capability check meaningful for every other provider
   unchanged. The engine's path-resolution branch and the `aca` provider's
   consumption of `sandbox.working_dir` (skipping host resolution entirely) are
   E3 work — E1 only carries the schema shape.

4. **`conductor-agent-runner` location & packaging (blocks E4/E5).** *DD2* says
   "the runner imports Conductor and calls `CopilotProvider.execute()`
   in-container". The design does not pin where the runner lives. **Decision
   needed:** in-package (`src/conductor/aca_runner/`, ships in the wheel, imported
   as `python -m conductor.aca_runner` — mirrors `src/conductor/web/server.py`)
   vs. a separate top-level `runner/` built only into the image. In-package is
   assumed by this plan (simplest import-parity story) but grows the published
   wheel; confirm that is acceptable, or scope the runner out of the wheel via
   `[tool.hatch.build.targets.wheel] exclude` (as the frontend already is,
   `pyproject.toml:71`).

   **Answer (E4) — in-package, wheel-inclusion left as-is.** Built as
   `src/conductor/aca_runner/` (package with `server.py` + `__main__.py`,
   `python -m conductor.aca_runner` entrypoint), mirroring
   `src/conductor/web/server.py`'s pattern. `pyproject.toml`'s
   `[tool.hatch.build.targets.wheel] exclude` was **not** touched — the runner
   ships in the wheel like every other subpackage; excluding it (E2-T3's
   deferred packaging question) is left for a follow-up if the wheel-size
   tradeoff turns out to matter in practice.

5. **Dialog-turn scope for v1 (blocks E4).** *Open Questions → Dialog turns*
   says `execute_dialog_turn()` should route through the same in-sandbox runner +
   gateway, **with a fallback** to disable dialog turns under `aca` with a clear
   error if the runner exposes no dialog endpoint. **Decision needed:** does v1
   build a runner dialog endpoint (extra `/execute_dialog_turn` surface + host
   plumbing), or ship the disable-with-clear-error fallback? This sets E4's scope
   and whether `AcaRuntimeProvider` overrides `execute_dialog_turn`
   (`base.py:270`).

   **Answer (E4) — disable-with-clear-error fallback; no runner dialog endpoint.**
   `AcaRuntimeProvider.execute_dialog_turn` now overrides the base class and
   raises a `ProviderError` naming the sandbox-boundary reason (rather than
   inheriting the generic `NotImplementedError`). Every existing caller
   (`gates/dialog.py`, `engine/dialog_evaluator.py`, `engine/validator.py`)
   already catches `Exception` around this call and degrades gracefully (skip
   dialog / log a warning), so this fails safely without new engine plumbing.
   Building a real runner dialog endpoint is deferred — the identity/session
   continuity a dialog turn would need (`execute_dialog_turn`'s signature has
   no `agent`/`identifier` parameter to key a session lookup on) is a bigger
   seam than this epic's scope.

6. **MVP credential stopgap shape (affects E3/E4 request contract & Security).**
   *DD4* permits a "short-lived scoped token in the request body" for the
   *trusted* Phase 1 MVP, deferring the host-side gateway to Phase 2. The design
   does not specify how that token is acquired, scoped, or consumed by the inner
   `CopilotProvider` (which normally authenticates via the GitHub Copilot SDK).
   **Decision needed:** the concrete stopgap for E3/E4 — e.g. host forwards a
   short-lived Copilot bearer token in the `/execute` body and the runner
   constructs `CopilotProvider(provider_settings=…)` with it (reusing the custom-
   routing `bearer_token` path, `copilot.py:360`) — so the request contract and
   `SecretStr` redaction are settled before the runner is built.

   **Answer (E4) — runner side confirms the E3 shape.** The runner's
   `_InnerProviderCache` builds `ProviderSettings(name="copilot",
   **inner_provider_settings)` from the plaintext dict the host forwards in
   `AcaExecuteRequest.inner_provider_settings` (`bearer_token`/`api_key`/
   `base_url`), exactly the shape E3 assumed. The Phase 2 gateway seam is a
   single call site (`_InnerProviderCache.get`, inline comment) — replacing it
   with a gateway lookup does not touch the wire contract.

7. **Per-agent `provider: aca` override (scope confirmation, affects E1).** The
   preview drives `aca` at `runtime.provider` (workflow-wide); every agent
   inherits it, so `AgentDef.provider` (`schema.py:659`, Literal without `aca`)
   need not change for the MVP. **Confirm** that mixing `aca` and non-`aca` agents
   in one workflow (per-agent `provider: aca`) is out of scope for v1 — adding it
   requires per-agent provider instantiation with distinct pool settings and is
   not exercised by the example.

---

## Implementation Phases

### Phase 0 — Transport spike *(COMPLETE — listed for lineage)*
Delivered outside this plan. The blocking transport question (*DD3*) was resolved
by issue [#312](https://github.com/microsoft/conductor/issues/312) (closed):
Branch S (single streaming request) chosen, `streaming_events=True` /
`interrupt=True` fixed, ~30-minute per-request cap measured.
**Exit criteria (met):** decision recorded in *DD3* and the capabilities table; no
build work remains here.

### Phase 1 — Experimental MVP (trusted use)
Ship the opt-in `aca` provider end-to-end for *trusted* workloads using the
short-lived-token stopgap (OQ#6); the credential gateway is deferred to Phase 2
(*DD4* — the gateway is **not** a Phase 1 release requirement, only a requirement
before untrusted/multi-tenant use). Covers Epics **E1–E7**.

**Exit criteria:**
- [ ] `conductor validate examples/aca-coding-agent.yaml` passes and the
  experimental banner + dashboard `exp` badge appear for `aca`.
- [ ] `runtime.provider: {name: aca, …}` and the per-agent `sandbox:` block parse
  and reject incoherent field combinations (FR1).
- [ ] `AcaRuntimeProvider` implements the full `AgentProvider` lifecycle, relays
  runner event frames verbatim, and returns a well-formed `AgentOutput` (FR2–FR4).
- [ ] Declared `ProviderCapabilities` match the design's table exactly; the
  provider is registered in `_PROVIDER_CLASS_PATHS` and resolves under `validate`
  without instantiation (FR5).
- [ ] Mocked-runner smoke tests (host provider **and** runner server) pass with
  the `aca` extra installed; the suite is skipped cleanly when it is not.
- [ ] Session-seconds surface as a distinct usage row (FR7).
- [ ] `docs/providers/aca.md` publishes the runner contract, image/extend guide,
  auth model, and capability carve-outs; `docs/providers/experimental.md` lists
  `aca`.
- [ ] A buildable `docker/aca-runner` image and a bring-your-own-pool provisioning
  example exist (DD6).

### Phase 2 — Credential gateway (required before untrusted/multi-tenant)
Add the host-side gateway (*DD4* / *Security Considerations*) so the real upstream
key never enters the sandbox; point the runner's inner `CopilotProvider` at it via
Copilot custom routing. Covers Epic **E8**.

**Exit criteria:**
- [ ] The upstream model key resides only on the host; the sandbox holds at most a
  short-lived, scoped token, `SecretStr`-redacted in events/checkpoints (NFR2).
- [ ] Gateway path has a mocked smoke test; docs state the trusted-vs-untrusted
  boundary and that Phase 2 is required before untrusted/multi-tenant workloads.

---

## Files Affected

### New Files
| File Path | Purpose |
|-----------|---------|
| `src/conductor/providers/aca.py` | `AcaRuntimeProvider` — host transport shim implementing `AgentProvider`: identifier derivation (DD5), `DefaultAzureCredential` token (FR6), NDJSON streaming client (Branch S), event relay, `AgentOutput` parse, interrupt, `validate_connection`/`close`, `CAPABILITIES` (experimental). *(Key Components §1)* |
| `src/conductor/providers/aca_protocol.py` | Shared host↔runner contract types (request body, NDJSON event frame, terminal `result` frame) as Pydantic models, imported by both `aca.py` and the runner. *(API Contracts)* |
| `src/conductor/aca_runner/__init__.py` | In-container runner package (OQ#4). |
| `src/conductor/aca_runner/server.py` | FastAPI/uvicorn app: `POST /execute` (stream NDJSON), `GET /health` (readiness + Conductor/runner version), wraps a real `CopilotProvider`, forwards `mcp_servers` + per-agent `tools:`. *(Key Components §2)* |
| `src/conductor/aca_runner/__main__.py` | `python -m conductor.aca_runner` entrypoint for the image. |
| `docker/aca-runner/Dockerfile` | Official `conductor-agent-runner` base image: runner + pinned Conductor + common stdio MCP binaries (DD6, runner-image contract). |
| `docker/aca-runner/.dockerignore` | Keep the image build context minimal. |
| `scripts/aca/provision-pool.sh` | Bring-your-own-pool example: two-step `az containerapp` deploy (push image to ACR → create custom-container session pool). *(DD6)* |
| `docs/providers/aca.md` | Provider docs page: runner contract, image build/extend, `DefaultAzureCredential`/Session Executor auth, capability carve-outs, cost note. *(Goals #6)* |
| `examples/aca-coding-agent.yaml` | The one runnable example (verified successor to the `docs/` preview). *(Goals #6)* |
| `tests/test_config/test_provider_settings_aca.py` | Schema/validator tests for the `aca` `ProviderSettings` fields and the `sandbox:` block. |
| `tests/test_providers/test_aca.py` | `AcaRuntimeProvider` unit tests + mocked-runner smoke test (mock httpx transport + `DefaultAzureCredential`). |
| `tests/test_aca_runner/test_server.py` | Runner server tests with a mocked `CopilotProvider` (construct + `/execute` streaming + `/health`). |
| `tests/test_engine/test_aca_usage.py` | Session-seconds distinct-usage-row test (E6). |

### Modified Files
| File Path | Changes |
|-----------|---------|
| `src/conductor/config/schema.py` | Add `"aca"` to `ProviderSettings.name` Literal (`:1693`); add `aca`-scoped fields (`pool_endpoint`, `api_version`, `inner_provider`, `identifier_scope`, `egress`, `lifecycle`, `auth`) and gate them in `_check_field_compatibility` (`:1784`) as copilot/claude/hermes fields are; add a `SandboxConfig` model + `sandbox: SandboxConfig | None` on `AgentDef` with per-type gating in `validate_agent_type` (`~:1150–1510`). *(Key Components §3; OQ#3)* |
| `src/conductor/providers/factory.py` | Add `"aca"` to `ProviderType` (`:27`); add an `aca` arm to `create_provider()` (`:86`) that checks `azure-identity` availability and wires `pool_endpoint`/`api_version`/`inner_provider`/`identifier_scope` from `provider_settings`. *(Key Components §4)* |
| `src/conductor/providers/capabilities.py` | Add `"aca": "conductor.providers.aca:AcaRuntimeProvider"` to `_PROVIDER_CLASS_PATHS` (`:222`) so `get_capabilities("aca")` resolves under `validate` without instantiation. *(FR5)* |
| `src/conductor/providers/base.py` | Add optional `session_seconds: float | None = None` to `AgentOutput` (`:66`) for FR7 (E6; contingent on OQ#2). |
| `src/conductor/engine/workflow.py` | Record a distinct `"<agent> (sandbox)"` usage row when `output.session_seconds` is set, at the main-loop, parallel-group, and for-each `record()` sites (`:3851`, `:4927`, `:5424`). *(FR7; OQ#2)* |
| `src/conductor/engine/usage.py` | If needed by OQ#2, a helper to record a session-seconds row (cost `None`); otherwise reuse `record()` with a synthetic `AgentOutput`. |
| `pyproject.toml` | Add `[project.optional-dependencies] aca = ["azure-identity>=…"]` (`httpx`/`fastapi`/`uvicorn` are already base deps, `:43–46`). Optionally exclude the runner from the wheel (OQ#4). |
| `src/conductor/config/validator.py` | *(Optional)* non-blocking **warning** that stdio MCP under `aca` depends on image contents (design allows a warning, not a hard error — *Open Questions → MCP*). |
| `docs/providers/experimental.md` | Add `aca` to the experimental-provider narrative and carve-out notes. |
| `docs/workflow-syntax.md` | Document `runtime.provider: {name: aca, …}` and the per-agent `sandbox:` block. |
| `AGENTS.md` / `CLAUDE.md` | Add an `aca.py` provider-parity notes subsection (mirroring the `claude_agent_sdk.py` parity notes) recording the experimental carve-outs and the runner delegation. |

### Deleted Files
| File Path | Reason |
|-----------|--------|
| `docs/projects/aca/aca-provider-example.yaml` | Superseded once `examples/aca-coding-agent.yaml` is verified. The design's *Goals #6* and the preview's own header state the runnable workflow "becomes … living at `examples/` instead of `docs/`." Remove only after the `examples/` version validates and runs. |

---

## Implementation Plan

### E1 — `aca` configuration surface (schema + validation) — **DONE**
- **Goal:** Parse and validate `runtime.provider: {name: aca, …}` and the per-agent
  `sandbox:` block; reject incoherent field combinations, mirroring the existing
  `ProviderSettings` guardrails (FR1; *Key Components §3*).
- **Prerequisites:** None. (OQ#3 must be answered before "done".)

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E1-T1 | IMPL | Add `"aca"` to `ProviderSettings.name` Literal; add `pool_endpoint` (required for `aca`), `api_version`, `inner_provider` (`Literal["copilot","claude-agent-sdk"]="copilot"`), `identifier_scope` (`Literal["workflow","agent","item","none"]="agent"`), `egress`, `lifecycle`, `auth`. | `src/conductor/config/schema.py` | DONE |
| E1-T2 | IMPL | Extend `_check_field_compatibility` to gate the new fields to `name=="aca"` and require `pool_endpoint` when `name=="aca"` (reject copilot/claude/hermes fields alongside `aca`, and vice-versa), following the existing per-name gating pattern. | `src/conductor/config/schema.py` | DONE |
| E1-T3 | IMPL | Add a `SandboxConfig` model (`identifier_scope` override + `working_dir` per OQ#3) and `sandbox: SandboxConfig \| None` on `AgentDef`; forbid `sandbox` on non-provider step types in `validate_agent_type` (script/human_gate/set/wait/terminate/workflow). | `src/conductor/config/schema.py` | DONE |
| E1-T4 | TEST | Valid `aca` config round-trips; missing `pool_endpoint` fails; `aca` fields under a non-`aca` name fail; copilot/claude fields under `aca` fail; `sandbox:` accepted on `agent`, rejected on other types; `SecretStr` fields (if any) redact in `model_dump`. | `tests/test_config/test_provider_settings_aca.py` | DONE |

- **Acceptance Criteria:**
  - [x] `aca` config parses; guardrails reject every incoherent combination (FR1).
  - [x] `sandbox:` block validates only on provider-backed agents.
  - [x] OQ#3 resolved and reflected in the `working_dir` handling.

### E2 — Factory, capability registration, and optional extra — **DONE**
- **Goal:** Instantiate `AcaRuntimeProvider` through `create_provider()` and make its
  capabilities resolvable at `validate` time; isolate `azure-identity` behind an
  `aca` extra (*Key Components §4*; FR5).
- **Prerequisites:** E3-T1 (the `AcaRuntimeProvider` class + `CAPABILITIES` must be
  importable for `_PROVIDER_CLASS_PATHS` resolution and the factory arm). E3 had not
  started; a minimal `AcaRuntimeProvider` skeleton (class + `CAPABILITIES`, `execute`/
  `validate_connection`/`close` stubs raising `NotImplementedError` pending E3) was
  added as a byproduct so E2's factory/capability wiring has something real to
  resolve against. The full transport shim remains E3's scope.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E2-T1 | IMPL | Add `"aca"` to `ProviderType`; add an `aca` arm to `create_provider()` that raises a clear `ProviderError` when `azure-identity` is absent (mirroring the claude/hermes availability guards) and constructs `AcaRuntimeProvider` from `provider_settings`. | `src/conductor/providers/factory.py` | DONE |
| E2-T2 | IMPL | Register `"aca": "conductor.providers.aca:AcaRuntimeProvider"` in `_PROVIDER_CLASS_PATHS`. | `src/conductor/providers/capabilities.py` | DONE |
| E2-T3 | IMPL | Add `[project.optional-dependencies] aca = ["azure-identity>=…"]`; decide wheel packaging of the runner (OQ#4). | `pyproject.toml` | DONE |
| E2-T4 | TEST | `get_capabilities("aca")` returns the declared descriptor without instantiation; factory raises a helpful error when `azure-identity` is missing and succeeds when present (mocked). | `tests/test_providers/test_aca.py` | DONE |

- **Acceptance Criteria:**
  - [x] `create_provider("aca", …)` returns an `AcaRuntimeProvider` (or a clear
    install error).
  - [x] `conductor validate` resolves `aca` capabilities with no API keys/network.
  - [x] `azure-identity` is only required when the `aca` extra is installed.

  Note on E2-T3 / OQ#4: the runner's wheel-packaging decision is deferred — the
  `conductor-agent-runner` package (`src/conductor/aca_runner/`) does not exist
  yet (E4 scope), so there is nothing to exclude from the wheel at this time.

### E3 — `AcaRuntimeProvider` host transport shim *(core)* — **DONE**
- **Goal:** Implement the `AgentProvider` that delegates `execute()` to the in-sandbox
  runner over a single streaming request, relays events verbatim, and returns
  `AgentOutput` (FR2–FR4, FR6; NFR3; *Key Components §1*, *Data Flow*, *DD1/DD3/DD5*).
- **Prerequisites:** E1 (schema fields). Resolve OQ#1 (concurrency seam) and OQ#6
  (stopgap token) before "done".

  **Review fixes (this pass).** A prior implementation pass of this epic failed
  review on two points: (1) the in-flight identifier registry tracked only a
  *count* of concurrent callers sharing a logical identifier, which is unsafe
  under out-of-order completion — e.g. call A gets the base identifier, call B
  gets `-conc1`, A finishes and releases first, and a third call C arriving
  while B is still active would be handed `-conc1` again (colliding with B,
  which never released it); and (2) `AgentOutput.session_seconds` (plus the
  matching `AcaResultData.session_seconds` wire field) had been added to this
  epic's change set, but that field and its usage-row plumbing are E6's
  responsibility (OQ#2 explicitly blocks E6, not E3), not part of the six
  transport-shim fixes this pass. Both are fixed here: `_acquire_wire_identifier`/
  `_release_wire_identifier` now track the *set* of reserved slot numbers per
  logical identifier (not a count), so a release always frees the exact slot a
  call acquired and a subsequent caller reuses the smallest free slot — never
  the slot of another call still in flight; `AgentOutput.session_seconds` and
  the `AcaResultData.session_seconds` field are removed from this pass entirely
  (deferred to E6, which will add them together with the usage-row wiring).

  **Prior review fixes.** A pass before that failed review for a different set
  of six issues: concurrent siblings sharing a constant `scope_key` (e.g. a
  parallel group under `identifier_scope: workflow`) could collide on
  identifier; the charset-normalization regex could collapse two distinct raw
  identifiers onto the same normalized string; the interrupt/hard-abort
  endpoints didn't match the real ACA data-plane contract (verified against
  Microsoft Learn); the `/health` probe omitted the `identifier` query
  parameter the container-path-forwarding proxy requires on every request; a
  non-2xx streamed `/execute` response could raise `httpx.ResponseNotRead`
  before its ACA diagnostics were parsed, discarding `code`/`message`/`traceId`;
  and the `tool_output` config / `cache_read_tokens` / `cache_write_tokens` were
  captured host-side but never forwarded to the runner or read back from the
  result frame. All six were fixed in that pass (see the updated OQ#1
  resolution below, `_normalize_and_truncate`, `_send_interrupt`/
  `_stop_session`, `validate_connection`, `_error_from_response`, and
  `AcaExecuteRequest.tool_output` / `AcaResultData.cache_*_tokens`).

  **OQ#1 resolution (taken by this epic).** No `execute()` signature change — the
  acceptance criteria require the call site (`executor/agent.py:288`) to stay
  untouched, which rules out option (a). Option (b) alone (diverging only when a
  for-each loop signal is present in `context`) turned out to be insufficient: a
  `parallel` group carries no `_key`/`_index` at all, and under a non-default
  `identifier_scope` (`workflow`/`none`) `scope_key` doesn't already vary by
  agent name either, so concurrent parallel-group siblings could resolve to the
  *same* identifier — a real, deterministic collision, not just a race. The fix
  layers a provider-local, in-memory "in-flight" registry on top of the loop-key
  divergence: `execute()` acquires a wire identifier for the call's `scope_key`
  base (via `_acquire_wire_identifier`/`_release_wire_identifier`), and only
  appends a numeric discriminator when another call for that *same* base is
  already in flight. The registry tracks the *set* of currently-reserved slot
  numbers per logical identifier (not a count), so a release always frees the
  exact slot that call acquired and a later caller reuses the smallest free
  slot — never a slot a still-active sibling holds, even under out-of-order
  completion (review fix; see `TestAcaConcurrencyIsolation.
  test_out_of_order_release_does_not_collide_with_still_active_slot`). Two
  calls that never overlap in time (including sequential `for_each` iterations
  and sequential different agents under `identifier_scope: workflow`) reuse
  the identical identifier, matching the *Data Flow* reuse table; two calls
  racing concurrently for the same base always diverge, matching the *Data
  Flow* concurrency-safety guarantee — all without any `execute()`/engine
  signal, since `identifier_for()` (the pure scope/loop-key function exercised
  by `TestIdentifierDerivation`) is unchanged and the registry wraps only the
  actual wire call in `execute()`.

  **OQ#6 resolution (taken by this epic).** The `aca`-scoped `ProviderSettings`
  fields intentionally exclude `bearer_token`/`api_key`/`base_url` (those remain
  copilot-only per `_check_field_compatibility`), so the stopgap credential is
  sourced from the same environment variables the Copilot custom-routing resolver
  already reads (`COPILOT_PROVIDER_BEARER_TOKEN`, `COPILOT_PROVIDER_API_KEY`,
  `COPILOT_PROVIDER_BASE_URL` — `copilot.py:_resolve_sdk_provider_config`) and
  forwarded verbatim as the request's `inner_provider_settings` field so the
  runner can construct `ProviderSettings(name=inner_provider,
  **inner_provider_settings)` for its inner `CopilotProvider`. `None` when no env
  var is set — an accepted Phase 1 gap (DD4); the Phase 2 gateway (E8) removes the
  need for this field.

  Note (from E2): a **minimal skeleton** of `AcaRuntimeProvider` already exists in
  `src/conductor/providers/aca.py` — the class, the `CAPABILITIES` descriptor (matches
  the design's table), and a constructor that stores `provider_settings` and raises
  `ProviderError` when `azure-identity` is absent. `execute()`, `validate_connection()`,
  and `close()` are still stubs that raise `NotImplementedError`. E3-T1 is not fully
  "done" — the `run_salt` and httpx-client-owning `close()` remain outstanding — but
  E2 needed *something* real to resolve via `_PROVIDER_CLASS_PATHS`, so the skeleton
  was pulled forward. E3 should extend, not replace, this file.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E3-T1 | IMPL | Provider skeleton: `AcaRuntimeProvider(AgentProvider)` with the experimental `CAPABILITIES` descriptor matching the design's table exactly (`tier=experimental`, `checkpoint_resume=False`, `structured_output="prompt_injection"`, `concurrent_safe=True`, `working_dir=True`, etc.); `__init__` storing `ProviderSettings` + a per-run `run_salt`; `close()` releasing the httpx client. | `src/conductor/providers/aca.py` | DONE |
| E3-T2 | IMPL | Define the shared request/frame Pydantic models (`agent`, `rendered_prompt`, `tools`, `mcp_servers`, `context`; NDJSON event frame; terminal `result` frame). | `src/conductor/providers/aca_protocol.py` | DONE — includes the `tool_output` request field + `cache_read_tokens`/`cache_write_tokens` result fields (review fix) |
| E3-T3 | IMPL | `identifier_for(scope)` per *Data Flow*: `cond-{run_salt}-{scope_key}{concurrency_suffix}`, charset-normalized, ≤128 chars with hash suffix; scope from `identifier_scope`; concurrency discriminator per OQ#1. | `src/conductor/providers/aca.py` | DONE — includes the in-flight acquire/release registry (slot-set tracking, not a count — review fix) + unconditional hash suffix |
| E3-T4 | IMPL | AAD auth: acquire + cache a `dynamicsessions.io` bearer token via `DefaultAzureCredential` (Session Executor role); attach to `POST {pool_endpoint}/execute?identifier=…&api-version=…`. | `src/conductor/providers/aca.py` | DONE |
| E3-T5 | IMPL | Streaming transport (Branch S): read `application/x-ndjson` line-by-line, call `event_callback(type, data)` verbatim, parse the terminal `result` frame into `AgentOutput`; classify runner/ACA errors into `ProviderError` with the ACA `code`/`message`/`traceId` attached. | `src/conductor/providers/aca.py` | DONE — `_error_from_response` now `await response.aread()`s before parsing a streamed error body (review fix). `session_seconds` deliberately **not** parsed into `AgentOutput` here — that field and its usage-row wiring are E6's scope (OQ#2 blocks E6, not E3); a review fix removed it from this pass. |
| E3-T6 | IMPL | Interrupt: on `interrupt_signal`, send an in-stream interrupt frame (Branch S) and fall back to a best-effort session delete as a hard-abort; `validate_connection()` does a lightweight management-plane + `/health` version probe (skew check). | `src/conductor/providers/aca.py` | DONE — interrupt/hard-abort endpoints now match the real ACA data-plane contract; `/health` includes `identifier` (review fixes) |
| E3-T7 | TEST | Mocked-runner smoke test: patch the httpx stream + `DefaultAzureCredential`; assert event frames relay 1:1 to `event_callback`, `AgentOutput` parses from `result`, identifier derivation is parallel-safe/≤128 chars, interrupt path fires, and errors surface as `ProviderError`. | `tests/test_providers/test_aca.py` | DONE — includes concurrency-isolation (incl. a three-call out-of-order-release regression), normalization-collision, corrected-endpoint, and dropped-field regression tests |

- **Acceptance Criteria:**
  - [x] Full lifecycle (`execute`/`validate_connection`/`close`) implemented; no
    change to the `provider.execute()` call site (`executor/agent.py:288`).
  - [x] Event types/emit points and `AgentOutput` shape match on-host providers
    (NFR3); secrets are `SecretStr`-redacted (NFR2) — the only plaintext secret on
    the wire is the OQ#6 stopgap credential, which is a deliberate, documented
    Phase 1 trusted-use exception (DD4) and is never logged or included in
    exception messages.
  - [x] Concurrency discriminator yields distinct identifiers for concurrent
    siblings and reuse for sequential re-executions (per the OQ#1 decision;
    enforced at runtime by the `execute()`-level in-flight registry, which
    tracks reserved slot *numbers* rather than a count so out-of-order release
    never collides with a still-active sibling — review fix).

### E4 — `conductor-agent-runner` in-container server — **DONE**
- **Goal:** The in-sandbox HTTP server that wraps the real `CopilotProvider` and
  streams event frames back, honoring the tools/MCP runner-image contract (*Key
  Components §2*, *API Contracts*, *DD2*).
- **Prerequisites:** E3-T2 (shared protocol). Resolve OQ#4 (location), OQ#5
  (dialog), OQ#6 (stopgap token) before "done".

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E4-T1 | IMPL | FastAPI app + `python -m conductor.aca_runner` entrypoint; `GET /health` returns readiness + Conductor/runner version for `validate_connection` skew checks. | `src/conductor/aca_runner/__init__.py`, `server.py`, `__main__.py` | DONE |
| E4-T2 | IMPL | `POST /execute`: deserialize the request, construct/reuse a `CopilotProvider` (inner provider = `copilot` for the MVP), run `execute()` with an `event_callback` that streams NDJSON frames, terminating with the `AgentOutput` `result` frame (incl. `session_seconds`). | `src/conductor/aca_runner/server.py` | DONE |
| E4-T3 | IMPL | Tools/MCP: reconstruct the inner provider with the full `mcp_servers` definitions + per-agent `tools:` allowlist; a declared-but-absent stdio binary fails loudly at execute time (runner-image contract; *Open Questions → MCP*). | `src/conductor/aca_runner/server.py` | DONE |
| E4-T4 | IMPL | Auth: point the inner `CopilotProvider` at the credential source per OQ#6 (Phase 1 stopgap token from the request body via the custom-routing `bearer_token` path); leave a seam for the Phase 2 gateway. | `src/conductor/aca_runner/server.py` | DONE |
| E4-T5 | IMPL | Dialog turns per OQ#5: either a runner dialog endpoint or a documented disable-with-clear-error under `aca`. | `src/conductor/aca_runner/server.py`, `src/conductor/providers/aca.py` | DONE — chose the disable-with-clear-error fallback (`AcaRuntimeProvider.execute_dialog_turn` raises `ProviderError`); no runner dialog endpoint was built |
| E4-T6 | TEST | Mocked-`CopilotProvider` server tests: `/execute` streams the expected frame sequence and terminal `result`; `/health` reports version; a missing stdio MCP binary surfaces as a runner error; tools/MCP passthrough is forwarded. | `tests/test_aca_runner/test_server.py` | DONE |

- **Acceptance Criteria:**
  - [x] Event/output parity comes "for free" from the wrapped `CopilotProvider`
    (DD2); frames use Conductor's own event vocabulary.
  - [x] Runner-image contract holds: full `mcp_servers` forwarded; absent binary
    fails loudly (not silently dropped).
  - [x] Dialog-turn behavior matches the OQ#5 decision.

  **Note:** two behaviors implied elsewhere in the design (a runner-side
  `max_session_seconds` wall-clock guard, and a `/interrupt` endpoint for the
  host's in-stream interrupt to land on) were **not** built by this epic — no
  E4 task or acceptance criterion assigns either, so they are left as explicit
  gaps for a follow-up task rather than scope-crept into E4. See the
  `aca_runner/server.py` module docstring.

### E5 — Runner image + bring-your-own pool
- **Goal:** A buildable official base image and a documented two-step provisioning
  path (DD6; *Open Questions → Image ownership*).
- **Prerequisites:** E4.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E5-T1 | IMPL | `Dockerfile` for `conductor-agent-runner`: install a pinned Conductor + the runner + common stdio MCP binaries; expose the runner `TARGET_PORT`; `CMD python -m conductor.aca_runner`. | `docker/aca-runner/Dockerfile`, `docker/aca-runner/.dockerignore` | TO DO |
| E5-T2 | IMPL | Provisioning example script: push image to ACR, then `az containerapp sessionpool create --container-type CustomContainer …` (advisory `egress`/`lifecycle` mirrors). | `scripts/aca/provision-pool.sh` | TO DO |
| E5-T3 | TEST | CI lint/build check for the Dockerfile (e.g. `hadolint` if already available, else a build smoke in a marked/optional job) and a shellcheck of the provisioning script. Only add if such tooling already exists in the repo. | `docker/aca-runner/Dockerfile`, `scripts/aca/provision-pool.sh` | TO DO |

- **Acceptance Criteria:**
  - [ ] The image builds and starts the runner; `/health` responds.
  - [ ] The provisioning example documents the ACR → session-pool two-step and the
    Session Executor role assignment.

### E6 — Session-seconds usage surfacing (FR7)
- **Goal:** Surface sandbox time as a distinct usage dimension, separate from token
  cost (FR7; *Key Components §5*).
- **Prerequisites:** E3 (transport shim; `AcaResultData`'s wire frame already
  carries the runner's `session_seconds` in `raw_response` via `extra="ignore"`,
  but E3 deliberately does not parse it into `AgentOutput` — see E3-T5). Resolve
  OQ#2 first.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E6-T1 | IMPL | Add `session_seconds: float \| None = None` to `AgentOutput`; populate it in `AcaRuntimeProvider` from the terminal `result` frame. | `src/conductor/providers/base.py`, `src/conductor/providers/aca.py` | TO DO |
| E6-T2 | IMPL | In the engine, when `output.session_seconds` is set, record a distinct `"<agent> (sandbox)"` usage row (cost `None`, `elapsed_seconds = session_seconds`) at the main-loop / parallel / for-each `record()` sites — mirroring the `"(validator)"` row pattern. | `src/conductor/engine/workflow.py`, `src/conductor/engine/usage.py` | TO DO |
| E6-T3 | TEST | A run where the provider returns `session_seconds` produces a separate `"(sandbox)"` row with no token cost, without disturbing the primary row's tokens/cost. | `tests/test_engine/test_aca_usage.py` | TO DO |

- **Acceptance Criteria:**
  - [ ] Session-seconds appear as a distinct, non-billing row separate from token
    cost; non-`aca` providers are unaffected (`session_seconds` stays `None`).

### E7 — Docs, example, experimental registration, parity notes
- **Goal:** Ship the experimental banner surface, a docs page, one runnable
  `examples/` workflow, and parity documentation (Goals #6; DD7).
- **Prerequisites:** E1–E6.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E7-T1 | IMPL | Author `docs/providers/aca.md`: architecture recap (link the design), runner `/execute`+`/health` contract, NDJSON frame schema, image build/extend (`FROM conductor-agent-runner:<tag>`), `DefaultAzureCredential`/Session Executor auth, capability carve-outs, cost note. | `docs/providers/aca.md` | TO DO |
| E7-T2 | IMPL | Add `aca` to `docs/providers/experimental.md`; document `runtime.provider: {name: aca}` + the `sandbox:` block in `docs/workflow-syntax.md`. | `docs/providers/experimental.md`, `docs/workflow-syntax.md` | TO DO |
| E7-T3 | IMPL | Add an `aca.py` parity-notes subsection to `AGENTS.md`/`CLAUDE.md` (mirroring the `claude_agent_sdk.py` notes): experimental carve-outs, runner delegation, `checkpoint_resume=False`. | `AGENTS.md`, `CLAUDE.md` | TO DO |
| E7-T4 | IMPL | Create `examples/aca-coding-agent.yaml` (verified successor to the preview); once it validates, remove `docs/projects/aca/aca-provider-example.yaml`. | `examples/aca-coding-agent.yaml`, `docs/projects/aca/aca-provider-example.yaml` (delete) | TO DO |
| E7-T5 | TEST | `conductor validate examples/aca-coding-agent.yaml` passes in CI (add to the examples-validation set, e.g. `make validate-examples`); assert the experimental banner metadata is present. | `examples/aca-coding-agent.yaml` | TO DO |

- **Acceptance Criteria:**
  - [ ] Docs page, experimental-tier registration, and one validating example ship.
  - [ ] The preview is removed only after the `examples/` version validates.

### E8 — Credential gateway *(Phase 2)*
- **Goal:** Route the in-sandbox SDK's inference through a host-side gateway so the
  real upstream key never enters the sandbox (*DD4*, *Security Considerations*);
  required before untrusted/multi-tenant workloads.
- **Prerequisites:** Phase 1 (E1–E7) complete. Depends on the design *Future* open
  questions on egress posture / minimum viable gateway.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E8-T1 | IMPL | Minimal host-side gateway that injects the real upstream key and forwards inference; point the runner's inner `CopilotProvider` at it via `COPILOT_PROVIDER_BASE_URL` / `COPILOT_PROVIDER_BEARER_TOKEN` custom routing (`copilot.py:360`). | `src/conductor/providers/aca_gateway.py` (or equivalent), `src/conductor/aca_runner/server.py` | TO DO |
| E8-T2 | IMPL | Replace the Phase 1 stopgap for untrusted use: sandbox holds only a short-lived scoped token; `SecretStr` redaction throughout. | `src/conductor/providers/aca.py`, `src/conductor/aca_runner/server.py` | TO DO |
| E8-T3 | TEST | Mocked gateway smoke test: the real key never appears in the request forwarded into the sandbox; redaction holds in emitted events. | `tests/test_providers/test_aca_gateway.py` | TO DO |

- **Acceptance Criteria:**
  - [ ] Real key resides only on the host (NFR2); docs state the trusted-vs-
    untrusted boundary and that the gateway is required before untrusted/multi-
    tenant workloads.

---

## References

- **Source design (authoritative):**
  [`docs/projects/aca/aca-provider.design.md`](./aca-provider.design.md) — all
  design decisions (DD1–DD7), the declared `ProviderCapabilities` table, Data
  Flow, API Contracts, Security Considerations, Risks, and Open Questions.
- **Preview UX:** [`docs/projects/aca/aca-provider-example.yaml`](./aca-provider-example.yaml).
- **Source issue:** [microsoft/conductor#284](https://github.com/microsoft/conductor/issues/284).
- **Phase 0 transport spike (resolved):**
  [issue #312](https://github.com/microsoft/conductor/issues/312) (closed);
  `spikes/aca-transport/` on branch `feature/312-aca-transport-spike`.

**Conductor seams (verified in-repo):**
- `src/conductor/providers/base.py` — `AgentProvider` ABC (`execute` `:234`,
  `execute_dialog_turn` `:270`), `AgentOutput` (`:66`), `__init_subclass__`
  capability enforcement (`:210`).
- `src/conductor/providers/capabilities.py` — `ProviderCapabilities` (`:55`),
  `_PROVIDER_CLASS_PATHS` (`:222`), `get_capabilities` (`:268`).
- `src/conductor/providers/factory.py` — `ProviderType` (`:27`),
  `create_provider()` `match` (`:86`).
- `src/conductor/config/schema.py` — `ProviderSettings` (`:1667`), `name` Literal
  (`:1693`), `_check_field_compatibility` (`:1784`), `has_custom_routing`
  (`:1875`); `RuntimeConfig` (`:1962`); `AgentDef` (`:614`), `provider` Literal
  (`:659`), `working_dir` (`:765`), `validate_agent_type` per-type gating
  (`~:1150–1510`).
- `src/conductor/providers/copilot.py` — `CAPABILITIES` (`:198`),
  `_resolve_sdk_provider_config` custom routing + `COPILOT_PROVIDER_*` fallbacks
  (`:360`).
- `src/conductor/providers/claude_agent_sdk.py` — canonical **experimental**
  provider: `CAPABILITIES` (`:121`), `validate_connection` (`:388`), `close`
  (`:440`); smoke-test pattern at `tests/test_providers/test_claude_agent_sdk.py`.
- `src/conductor/config/validator.py` — capability cross-checks
  (`_validate_provider_capabilities` `:1551`), `working_dir` checks (`:1738`,
  `:1815`), concurrency checks (`:1866`).
- `src/conductor/engine/usage.py` — `AgentUsage` (`:18`), `UsageTracker.record`
  (`:178`); validator distinct-row precedent in `engine/workflow.py` (`:2778`,
  `:2828`).
- `src/conductor/executor/agent.py` — `provider.execute()` call site (`:288`).
- `pyproject.toml` — `[project.optional-dependencies]` extras pattern (`:50`),
  base deps incl. `fastapi`/`uvicorn`/`httpx` (`:43–46`), wheel exclude (`:71`).
- `docs/providers/experimental.md` — experimental-tier policy, carve-out table,
  banner format.

**Azure Container Apps (Microsoft Learn — from the design):**
- [Dynamic sessions concepts](https://learn.microsoft.com/azure/container-apps/sessions),
  [usage / identifiers / auth](https://learn.microsoft.com/azure/container-apps/sessions-usage),
  [session pools / lifecycle](https://learn.microsoft.com/azure/container-apps/session-pool),
  [custom-container sessions](https://learn.microsoft.com/azure/container-apps/sessions-custom-container),
  [premium ingress timeouts](https://learn.microsoft.com/azure/container-apps/premium-ingress),
  [`az containerapp sessionpool`](https://learn.microsoft.com/cli/azure/containerapp/sessionpool),
  [official custom-container sample](https://github.com/Azure-Samples/dynamic-sessions-custom-container).
