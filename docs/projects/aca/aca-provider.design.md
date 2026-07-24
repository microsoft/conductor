# Solution Design: Agent-in-Sandbox via a remote `aca` provider

> Source issue: [microsoft/conductor#284](https://github.com/microsoft/conductor/issues/284)
> — *Idea: run agents inside Azure Container Apps sandboxes via a remote `aca`
> provider (Agent-in-Sandbox)*. Status: `idea` (speculative, not yet committed).
> The blocking Phase 0 transport spike ([#312](https://github.com/microsoft/conductor/issues/312),
> closed) has run; see DD3 and Open Questions for the resolved outcome (Branch S).
>
> This document is a **solution design** for engineering and architecture
> review. It covers *what* and *why*; the epic/task breakdown and file-by-file
> changes belong to a separate planning step that consumes this design.

## Executive Summary

Conductor runs every agent side effect — the SDK's built-in file/shell tools and
`type: script` steps — **on the host, as the invoking user**. This design adds an
**experimental `aca` provider** (`AcaRuntimeProvider`) that relocates an agent's
whole runtime — the agentic loop **and** its tool execution — into an **Azure
Container Apps (ACA) dynamic session** (custom-container session pool), Hyper-V
isolated off the host. Conductor stays the on-host orchestrator (routing,
`WorkflowContext`, checkpoints, event bus) and delegates only per-agent
`execute()` over HTTPS to an in-container "agent runner" that wraps Conductor's
*real* provider. LLM inference still flows to the remote Copilot/Anthropic API.
The outcome is an opt-in, isolated, ephemeral execution mode for untrusted or
model-generated code, coding agents, and multi-tenant runs — slotted into the
existing provider abstraction with no change to default on-host behavior.

**Scope note.** This provider isolates **agent (SDK) tool execution only**.
`type: script` steps keep running on the host; isolating those is a separate
change (Alternative A). The MVP wraps **Copilot only**; Anthropic-inside is
deferred (see Decision Status).

## Decision Status & Review Ask

**What reviewers are asked to approve now:** the **architecture direction** (a
remote `aca` `AgentProvider` that delegates `execute()` to an in-sandbox runner,
orchestrator on the host); and the **sequencing** (a blocking Phase 0 transport
spike gates the build; Alternative B may land first).
The Phase 0 transport spike has **run and resolved** (Branch S; see DD3) — the
transport branch and its two dependent capability values are no longer open and
are included below for approval alongside everything else. The **credential
model** (DD4) is likewise no longer an open ask — it's **Accepted** and shipped
(E8/E9): the provider forwards either a GitHub token or BYOK routing settings
to the sandbox, recommended default a fine-grained *Copilot Requests* PAT so
trusted workloads run on your own Copilot capacity, with BYOK custom routing
as the fallback (endpoint credentials optional within it) and no mode switch.

| Item | Status | Notes |
|---|---|---|
| `AgentProvider` at `execute()` granularity (DD1) | Proposed | Engine, routing, context, checkpoints stay on host |
| Runner wraps the real `CopilotProvider` (DD2) | Proposed | Event/output parity comes "for free" |
| Credential model = forward a narrowly-scoped credential (DD4) | **Accepted** | No modes; Copilot-Requests PAT recommended (default), BYOK fallback — shipped in E8/E9 |
| Transport: streaming vs submit+poll (DD3) | **Resolved — Branch S** | Phase 0 spike (#312) measured a ~30-min per-request cap; streaming chosen |
| `streaming_events` / `interrupt` capability values | **Resolved — both `True`** | Follow directly from the DD3 outcome |
| `concurrent_safe = True` via concurrency discriminator (DD5) | Proposed | Mechanism in Data Flow |
| Bring-your-own pool (DD6) | Proposed | No infra provisioning in v1 |
| `checkpoint_resume = False` | Accepted | Platform constraint (ephemeral sessions, no volume mount) |
| `inner_provider: copilot` only for the MVP | Accepted (scope) | `claude-agent-sdk` inside the runner is deferred |
| Isolate `type: script` steps | Deferred | Alternative A — separate issue |
| Sandbox-as-MCP-tool (loop-outside / exec-inside) | Deferred | Alternative B — separate issue; may precede this work |

Statuses: **Accepted** (settled), **Proposed** (up for approval now),
**Resolved** (Phase 0 spike outcome, now settled), **Deferred** (out of scope or
later).

**What is the credential posture?** The provider forwards either a GitHub
token or BYOK routing settings to the sandbox (DD4) — recommended: a
fine-grained *Copilot Requests* PAT, so trusted workloads run on your own
Copilot capacity. Because that token (or an optional BYOK endpoint
credential) is readable inside the session, `aca` targets **trusted**
workloads; keeping credentials entirely off the sandbox (a host-side broker)
for untrusted/multi-tenant use is future work. The full model is stated in
DD4 and Security Considerations.

## Background

### Current execution model

Conductor orchestrates multi-agent workflows on the host. The `WorkflowEngine`
loops over steps (agent / parallel / for-each / script / set / wait), evaluates
routes, and builds output. Agent execution is abstracted behind `AgentProvider`
(`src/conductor/providers/base.py`):

```python
async def execute(
    self, agent, context, rendered_prompt,
    tools=None, interrupt_signal=None, event_callback=None,
) -> AgentOutput
```

Plus `validate_connection()`, `close()`, an optional `execute_dialog_turn()`, and
best-effort metadata hooks. Providers return a normalized `AgentOutput`
(content, token counts, model, `partial`).

Two facts about *where side effects land* motivate this work:

1. **The SDKs run the loop client-side with local tools.** The Copilot provider
   creates its SDK session with `working_directory=agent.working_dir or
   os.getcwd()` (`src/conductor/providers/copilot.py:803`); the Claude Agent SDK
   provider spawns the `claude` CLI in-process. Built-in file/shell tools execute
   **locally, in the host process's filesystem and environment**.
2. **`type: script` steps run local subprocesses.** `ScriptExecutor`
   (`src/conductor/executor/script.py`) calls `asyncio.create_subprocess_exec`
   with the host `cwd`/`env`, and `command:` is Jinja2-templated — so
   `command: "{{ planner.output.cmd }}"` runs **LLM-generated shell on the host**.

### Prior art in the codebase

- **Provider abstraction and factory.** New providers slot into
  `create_provider()` (`src/conductor/providers/factory.py:86`, a `match` on
  `ProviderType`) and resolve for validation via `_PROVIDER_CLASS_PATHS`
  (`src/conductor/providers/capabilities.py`).
- **Structured `runtime.provider`.** `ProviderSettings`
  (`src/conductor/config/schema.py:1667`) already models a structured provider
  object with `SecretStr` redaction, env-var fallback, a strict
  `_check_field_compatibility` validator, and `has_custom_routing()`. It backs
  Copilot **custom routing** (`_resolve_sdk_provider_config`, `copilot.py:360`),
  which points the SDK at an arbitrary endpoint via `COPILOT_PROVIDER_BASE_URL` /
  `COPILOT_PROVIDER_BEARER_TOKEN` — the reuse hook for BYOK credential forwarding.
- **Experimental provider tier.** `ProviderCapabilities`
  (`capabilities.py`) is a declarative contract that `conductor validate`
  cross-checks (`config/validator.py`); carve-outs live in
  `docs/providers/experimental.md`. `claude-agent-sdk` is the closest precedent —
  it also delegates the loop to an external process while preserving parity.
- **Event pub/sub.** Providers emit via `event_callback(event_type: str, data:
  dict)`. The vocabulary (`agent_turn_start`, `agent_message`, `agent_reasoning`,
  `agent_tool_start`, `agent_tool_complete`, …) is consumed identically by the
  console subscriber, JSONL logger, and web dashboard.
- **Optional-dependency extras.** Experimental upstreams are isolated behind
  `[project.optional-dependencies]` in `pyproject.toml`.

### Why now / what changed

ACA dynamic sessions are purpose-built for this: Microsoft names "AI agents" and
"development environments" running user-provided code in Hyper-V isolated
sandboxes as target scenarios, ships a **custom-container** session-pool type, and
provides an official sample
([Azure-Samples/dynamic-sessions-custom-container](https://github.com/Azure-Samples/dynamic-sessions-custom-container)).
Its primitives (per-`identifier` routing, `Timed` / `OnContainerExit`
lifecycles, opt-in egress, warm pools, the *Session Executor* role) map onto
Conductor's per-run / per-agent / per-item scoping, and the recently landed
provider abstraction and structured-provider config give us the seams to add this
without engine surgery.

## Problem Statement

Conductor cannot run an agent's side effects off the host. Every file write,
shell command, and `type: script` invocation — including **model-generated** ones
— executes in the host's filesystem and environment with the host user's
privileges. This blocks or makes unsafe:

- **Untrusted / model-generated code** — a planner that emits shell for a `script`
  step runs it on the operator's machine or CI runner; no isolation boundary.
- **Coding agents (clone → edit → run → commit)** — need a disposable workspace
  with real compute, not the host repo, and must not reach host credentials.
- **Multi-tenant / concurrent runs** — parallel and for-each groups share the host
  filesystem; there is no per-unit isolation of tool side effects.

The underlying constraint: both bundled SDKs **fuse the agentic loop and its tool
execution into one client-side unit** with no seam to relocate *only* the tools.
So moving *all* of an agent's built-in tool side effects off-host requires moving
the **whole SDK** off-host — which no current provider type does.

## Goals and Non-Goals

### Goals

1. Add an experimental `aca` provider that runs an agent's whole runtime (loop +
   tools) inside an ACA custom-container session, off the host.
2. Keep the engine, routing, `WorkflowContext`, checkpoints, and event bus on the
   host — delegate only per-agent `execute()`.
3. Slot into existing seams (`create_provider()`, structured `runtime.provider`, a
   declared `ProviderCapabilities`, unchanged event/`AgentOutput` contracts) so
   dashboard/JSONL/console render identically.
4. Forward only a **narrowly-scoped credential** to the sandbox (recommended: a
   fine-grained *Copilot Requests* PAT — your Copilot capacity), never a broad or
   long-lived key baked as a pool secret.
5. Preserve isolation under concurrency: distinct ACA sessions per concurrency
   unit (`concurrent_safe=True`).
6. Ship behind an optional extra with an experimental banner, a docs page, one
   runnable `examples/` workflow, and a mocked-runner smoke test.

### Non-Goals

- **Not** changing default on-host execution for other providers — `aca` is opt-in.
- **Not** isolating *only* the tools while keeping the loop on host — that is
  Alternative B (a separate issue), unreachable from a provider with the bundled
  SDKs.
- **Not** making `conductor resume` restore in-sandbox state (`checkpoint_resume=
  False`).
- **Not** provisioning ACA infrastructure in v1 — bring-your-own pool.
- **Not** generalizing to non-ACA sandboxes (E2B/Modal/Daytona) in v1.
- **Not** GPU, persistent volumes, or per-session secret injection — ACA
  custom-container sessions do not support these.

## Requirements

### Functional

- **FR1.** `runtime.provider: { name: aca, ... }` parses and validates; the
  validator rejects incoherent field combinations, mirroring the existing
  `ProviderSettings` guardrails.
- **FR2.** `AcaRuntimeProvider` implements the full `AgentProvider` lifecycle and
  returns a well-formed `AgentOutput` on success.
- **FR3.** The provider maps an execution *scope* (run / agent / for-each item /
  step) to an ACA session `identifier`, reusing or auto-allocating accordingly.
- **FR4.** The in-container `conductor-agent-runner` drives a real
  `CopilotProvider` and relays events back — **streaming them (preferred, Branch S)
  or in polled batches (fallback, Branch P)** per the DD3 outcome — which the host
  remaps to the standard Conductor event vocabulary.
- **FR5.** The provider declares accurate `ProviderCapabilities` (experimental
  tier) and is registered for validation resolution.
- **FR6.** Management-endpoint auth uses the *Session Executor* role via
  `DefaultAzureCredential` (audience `https://dynamicsessions.io`).
- **FR7.** Sandbox time is surfaced as a **distinct usage dimension**
  (session-seconds), separate from token cost.

### Non-Functional

- **NFR1 — Isolation.** Tool side effects execute in the session filesystem, never
  the host's; distinct concurrency units get distinct sessions.
- **NFR2 — Credential safety.** The credential forwarded to the sandbox is narrowly
  scoped (recommended: a *Copilot Requests* PAT); a broad or long-lived key is never
  baked as a pool secret; secrets are `SecretStr`-redacted in events/checkpoints.
- **NFR3 — Parity.** Event types, emit points, and `AgentOutput` shape match the
  on-host providers.
- **NFR4 — Long-running tolerance.** A single step runs for minutes; the transport
  must survive ACA's request/idle-timeout limits (design in DD3; Phase 0 validated).
- **NFR5 — Honest capabilities.** Any capability not honorable under all conditions
  is declared as the weaker value.
- **NFR6 — Cost visibility.** Warm-pool and Dedicated-node costs are surfaced so
  operators can right-size.

## Proposed Design

### Architecture Overview

```
┌───────────────── Conductor host (orchestrator) ──────────────────────┐
│ WorkflowEngine: routing · WorkflowContext · checkpoints · event bus    │
│                                                                        │
│ AcaRuntimeProvider(AgentProvider)             ← new, experimental tier │
│   execute(agent, ctx, prompt, tools, event_callback, interrupt):       │
│     id  = identifier_for(scope)               # run | agent | item     │
│     tok = DefaultAzureCredential()            # aud dynamicsessions.io  │
│     POST {pool}/execute?identifier=id         # AAD token, exec role    │
│     for line in ndjson(resp): event_callback(line.type, line.data)     │
│     return AgentOutput(**final_result)                                 │
│                                                                        │
│ Per execute(): forward one narrowly-scoped credential to the runner    │
│   (Copilot-Requests PAT → Copilot capacity, or BYOK settings).         │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │ HTTPS · aud dynamicsessions.io
                               │ POST /<path>?identifier=<id> → <TARGET_PORT>/<path>
                 ┌─────────────▼───────────────┐  auto-allocate / reuse by identifier
                 │  ACA custom-container pool   │
                 │   session (Hyper-V isolated) │
                 │  ┌────────────────────────┐  │
                 │  │ conductor-agent-runner │  │  ← HTTP server (baked into image)
                 │  │  FastAPI /execute      │  │
                 │  │  wraps CopilotProvider │  │
                 │  │  SDK loop + CLI tools  │──┼─▶ edits/exec on CONTAINER fs (ephemeral)
                 │  │  inner SDK ────────────┼──┼─▶ model inference (Copilot capacity/BYOK)
                 │  └────────────────────────┘  │
                 └──────────────────────────────┘
```

The design is **"agent-in-sandbox at `execute()` granularity, orchestrator on
host."** Only one agent's sub-loop moves into the sandbox; the workflow-level loop
(the subject of the leaders' cautionary tale — Anthropic Cowork, "any VM failure
made Cowork unusable") stays on the host, which is precisely the posture Anthropic
retreated *to*.

### Security Boundary (at a glance)

A model-driven shell inside the session can read any env var or file there, so the
design's rule is: **only a narrowly-scoped credential is ever forwarded to the
sandbox.** The provider forwards one credential per `execute()` — recommended: a
fine-grained *Copilot Requests* PAT, which can do nothing on your account but make
Copilot inference calls (your Copilot capacity), with a bounded expiry — or BYOK
custom-routing settings as the fallback. Because that credential does enter the
session, `aca` targets **trusted** workloads; a broad or long-lived key is never
baked as a pool secret. ACA offers no per-session secret and no per-destination
egress allowlist, so keeping the credential entirely off the sandbox (a host-side
broker) is future work for untrusted/multi-tenant use. Full model, threat analysis,
and the managed-identity trade-off are in **Security Considerations**; the decision
rationale is **DD4**.

### Design Decisions

Read these before the component/data-flow/contract detail below — they are the
premises the rest of the design builds on.

- **DD1 — Slot in as an `AgentProvider`, not a new step type.** The `execute()`
  seam is where the agentic loop is dispatched; a provider is the only place that
  can relocate the *whole* SDK, keeping engine, routing, context, and checkpoints
  untouched. (Alternative A targets the `script` seam and cannot isolate an
  agent's built-in tools.)

- **DD2 — Runner wraps Conductor's real provider.** The runner imports Conductor
  and calls `CopilotProvider.execute()` in-container rather than reimplementing
  the loop. Event and output parity — and the same `structured_output` behavior —
  come "for free" because the same tested code runs; the host forwards its events
  verbatim.

- **DD3 — Transport is Branch S (single streaming request); resolved by Phase 0
  spike #312.** Two ACA limits bear on a multi-minute turn and must not be
  conflated: the **per-request forwarded-duration cap** (how long one request may
  stay open — undocumented for sessions; general ACA ingress idle-timeout defaults
  to 4 min, premium max 30 min; this is the real cut-off risk) and the
  **inter-request `Timed` cooldown** (300–3600 s; how long an *idle* session
  survives *between* requests, reset each time the session API is *called*). The
  runner API is therefore **not** one blocking request per turn. Two candidate
  branches were evaluated, with identical event/result semantics:

  - **Branch S (chosen) — single streaming request.** One `POST /execute`
    streams NDJSON event frames until a terminal `result` frame. Simplest; gives
    true incremental events and mid-call partials.
  - **Branch P (not needed, but scaffolded and verified working) — submit + poll.**
    `POST /execute` returns a job id immediately; the host polls for event batches
    until a terminal frame. Each poll is a fresh request that stays under the
    per-request cap and resets the cooldown. Survives a short cap at the cost of
    coarser streaming and interrupt.

  **Phase 0 spike results (issue #312, closed).** Against a real ACA
  custom-container session pool (default, non-premium ingress), a single held
  streaming request was cut off at **~1801 seconds (~30 minutes)**. A
  disambiguation run — requesting a 2400s (40 min) turn — was cut off at the
  *same* ~1801s mark, ruling out the cutoff being an artifact of the first run's
  own requested duration and confirming a real, reproducible per-request cap of
  ~30 minutes, reached **without needing premium ingress**. Separately, a single
  stream was confirmed durable for ≥10 minutes (900s, steady ~1s inter-frame
  gaps, clean terminal frame), and a dropped stream resumed cleanly from a
  `Last-Event-ID` cursor with no gap or duplicate. Branch P (submit + poll) was
  also exercised end-to-end and completed with zero missing frames, so it remains
  available as a fallback mechanism if a future workload needs it.

  **Decision: Branch S**, because ~30 minutes is comfortably above the expected
  length of a single agent turn (NFR4: "a single step runs for minutes"). This
  fixes `streaming_events=True` and `interrupt=True` (real, via the in-flight
  stream) rather than the conditional values previously carried here. **Caveat:**
  a turn that runs longer than ~30 minutes wall-clock will still hit this cap:
  the recommended mitigation is automatic reconnect-and-resume over
  `Last-Event-ID` (backed by a *bounded* server-side event log, unlike the
  spike's unbounded in-memory log) rather than falling back to Branch P by
  default. Whether to build that resume path for v1 or defer it is now a Pre-MVP
  open question (see Open Questions).

- **DD4 — Forward one narrowly-scoped credential; no modes.** Per `execute()` the
  provider forwards a single credential to the sandbox runner, mirroring the Copilot
  CLI's own auth precedence (no `credential_mode` switch): if
  `COPILOT_PROVIDER_BASE_URL` is set on the host → forward the BYOK custom-routing
  settings (`base_url` + `api_key`/`bearer_token`); otherwise, if a GitHub token is
  present (`COPILOT_GITHUB_TOKEN` → `GH_TOKEN` → `GITHUB_TOKEN`) → forward it and the
  sandbox's inner Copilot runtime authenticates to **GitHub Copilot's own model
  routing** (your Copilot capacity) — **recommended: a fine-grained PAT with only the
  *Copilot Requests* permission**; otherwise → fail loudly with setup guidance. The
  credential is delivered in-memory per call (request body → the inner runtime's
  `github_token`), never a persisted pool secret. **Posture:** the credential *does*
  enter the sandbox and is readable by a shell there, so the defense is *scope and
  lifetime* — a leaked *Copilot Requests* PAT can only spend your Copilot quota until
  it expires and is centrally revocable — which makes `aca` suitable for *trusted*
  workloads. Baking a long-lived, broadly-scoped token as a pool secret is the named
  anti-pattern and is rejected. Keeping the credential entirely off the sandbox (a
  host-side broker/relay) for untrusted/multi-tenant use is future work. Full model in
  **Security Considerations**.

- **DD5 — `identifier` is the isolation/persistence knob.** ACA keys sessions by a
  free-form `identifier` (existing → routed; new → auto-allocated), with state
  persisting for the session lifetime. `identifier_scope` governs workspace
  *reuse* across an agent's sequential re-executions; `concurrent_safe=True` is
  made truthful by a **mandatory concurrency discriminator** (see Data Flow) that
  always diverges the identifier per concurrent unit — otherwise the default
  `agent` scope would collapse a concurrent `for_each` onto one shared session.

- **DD6 — Bring-your-own pool.** v1 consumes an operator-created `pool_endpoint`
  (`az containerapp sessionpool create --container-type CustomContainer …`; a
  two-step deploy: push image to ACR, then create pool). Matches the BYO-endpoint
  philosophy of custom routing and keeps Conductor out of provisioning.

- **DD7 — Experimental tier with honest carve-outs.** Ships behind an extra with
  the experimental banner and capabilities matching observed behavior — most
  notably `checkpoint_resume=False` (ephemeral sessions, no volume mount), so
  resume re-runs the agent rather than restoring in-sandbox state.

**Declared `ProviderCapabilities` (experimental).** Cells are terse; rationale
lives in the DD noted.

| Capability | Value | Rationale |
|---|---|---|
| `tier` | `experimental` | Delegates the loop to a remote runtime. |
| `mcp_tools` | `True` | Full `mcp_servers` forwarded; runner-image contract (API Contracts). |
| `workflow_tools_passthrough` | `True` | Per-agent `tools:` allowlist forwarded and enforced by the inner SDK. |
| `streaming_events` | `True` | Resolved by Phase 0 spike #312 — Branch S chosen (DD3). |
| `agent_reasoning_events` | `True` | Runner forwards reasoning frames. |
| `reasoning_effort` | Copilot's tuple | Inner provider translates effort natively. |
| `structured_output` | `prompt_injection` | Inherits the real `CopilotProvider` (`copilot.py:220`); DD2. |
| `interrupt` | `True` | Real, via the in-flight stream (Branch S, DD3); `stopSession` remains available as a hard-abort. |
| `max_session_seconds` | `True` | Runner-side guard (Timed lifecycle ignores pool `maxAlivePeriodInSeconds`; see note). |
| `checkpoint_resume` | **`False`** | Ephemeral sessions, no volume mount; DD7. |
| `usage_tracking` | `True` | Runner returns token counts on the result frame. |
| `concurrent_safe` | `True` | Mandatory concurrency discriminator; DD5. |
| `working_dir` | `True` | Interpreted **container-relative** (path inside the session fs); resolved in Open Questions. |

`structured_output=prompt_injection` is load-bearing: Copilot has no native
JSON-mode (schemas are prompt-appended), and declaring `native` would both be
wrong and suppress the experimental `prompt_injection` validation warning that
should fire (`config/validator.py`, #241). `max_session_seconds` is enforced by a
runner-side wall-clock guard because the default `Timed` lifecycle does not honor
the pool `maxAlivePeriodInSeconds` field (that applies only under
`on_container_exit`).

### Key Components

**1. `AcaRuntimeProvider` (host, new — `src/conductor/providers/aca.py`).** A thin
transport shim implementing `AgentProvider`; owns no agentic logic. It derives the
session `identifier` from `identifier_scope` (DD5), acquires a
`dynamicsessions.io` bearer token via `DefaultAzureCredential` (cached), issues
the request to the pool, relays the runner's event frames verbatim to
`event_callback`, parses the terminal `result` frame into `AgentOutput`, and on
`interrupt_signal` sends an interrupt frame (Branch S) or hard-aborts via
`stopSession`. `validate_connection()` does a lightweight management-plane probe;
`close()` releases the HTTP client. It declares `CAPABILITIES` and is added to
`_PROVIDER_CLASS_PATHS` so `conductor validate` resolves it without instantiation.
Parity is structural: because event types are plain strings, the runner emits
Conductor's own event names and the host forwards them.

**2. `conductor-agent-runner` (in-container, new).** An async FastAPI/uvicorn
server (both already base deps) baked into the pool image. It imports Conductor
and wraps the real provider:

- `POST /execute` — deserialize, construct/reuse a `CopilotProvider`, run
  `execute()` with an `event_callback` that emits event frames (Branch S: streamed
  NDJSON; Branch P: buffered for polling), terminating with the `AgentOutput`
  result frame.
- **Tools / MCP** — constructs the inner provider with the full `mcp_servers`
  definitions from the request plus the per-agent `tools:` allowlist, honoring
  `mcp_tools` / `workflow_tools_passthrough`. This imposes a **runner-image
  contract** (stdio binaries baked in; remote MCP needs egress) detailed in API
  Contracts.
- `GET /health` — readiness/liveness for probes and `validate_connection`.
- **Auth** — the runner authenticates the inner `CopilotProvider` with the
  credential the host forwards per call (DD4): a GitHub token → Copilot's own model
  routing (Copilot capacity), or BYOK custom-routing settings. Only a
  narrowly-scoped credential is forwarded; no broad key is baked into the image.

The runner is deliberately minimal: loop, tools, structured output, and retries
are the *existing, tested* `CopilotProvider` running in a new location. The
container filesystem is the agent's workspace.

**3. `ProviderSettings` extension (host — `src/conductor/config/schema.py`).** Add
`aca` to the `name` Literal plus `aca`-scoped fields (gated for other names by
`_check_field_compatibility`, as with copilot/claude/hermes):

- `pool_endpoint: str` (required for `name: aca`) — ACA pool management endpoint.
- `api_version: str` — management API version (e.g. `2025-07-01`).
- `inner_provider: Literal["copilot", "claude-agent-sdk"] = "copilot"` — SDK the
  runner drives. **MVP: `copilot` only.** Claude-inside requires
  `claude-agent-sdk` (the containerizable `claude` CLI); the bare `claude`
  (Anthropic-API) provider has no in-process tool runtime and is not valid here.
- `identifier_scope: Literal["workflow","agent","item","none"] = "agent"` — default
  granularity for *sequential* reuse (concurrent units always diverge; DD5).
- `egress: Literal["enabled","disabled"]` — advisory mirror of the pool's
  `sessionNetworkConfiguration.status` (the pool governs).
- `lifecycle: Literal["timed","on_container_exit"]` — advisory mirror.
- `auth: Literal["azure_default"] = "azure_default"` — Session Executor strategy.

Per-agent `identifier_scope` override via an agent-level `sandbox:` block (exact
shape is an open question).

**4. Factory + capabilities registration (host).** `create_provider()` gains an
`aca` arm; `ProviderType` (`factory.py`) and `_PROVIDER_CLASS_PATHS`
(`capabilities.py`) gain `"aca"`; a new optional extra `aca` pins `azure-identity`
(`httpx` is already base).

**5. Cost surfacing (host — usage tracking).** Sandbox time is a **distinct usage
row** (as the validator feature records `"<agent> (validator)"`), shown separately
from token cost. Session-seconds is a **visibility proxy, not billing**: ACA
custom-container pools bill by Dedicated **E16** node capacity plus the idle warm
pool, not per session-second.

### Data Flow

**Single agent execution under `aca` (illustrated with Branch S; Branch P has the
same event/result semantics, delivered in polled batches):**

1. Engine reaches an agent step and calls `AcaRuntimeProvider.execute(...)` (via
   `AgentExecutor`, `src/conductor/executor/agent.py:288`). Prompt is already
   rendered and tools resolved on the host.
2. Provider computes `identifier = identifier_for(scope)` and acquires a
   `dynamicsessions.io` bearer token.
3. Provider issues the request to
   `{pool_endpoint}/execute?identifier=<id>&api-version=<v>`; ACA routes to the
   session for `<id>` (auto-allocating from the warm pool if none) and forwards the
   body to the container's `<TARGET_PORT>/execute`.
4. The runner runs `CopilotProvider.execute()`, relaying each SDK event as a frame
   over one long-lived streaming request (Branch S, DD3), bounded by the measured
   ~30-minute per-request cap. The session stays alive through the multi-minute
   turn.
5. The host relays each frame to `event_callback(type, data)` — dashboard, JSONL,
   and console render exactly as for on-host providers.
6. The terminal `result` frame carries the structured `AgentOutput`; the host
   returns it to the engine and records sandbox elapsed seconds as a distinct usage
   row.
7. The engine commits output to `WorkflowContext`, evaluates routes, and proceeds —
   unchanged from any other provider.

**Identifier derivation (parallel-safe).**
`identifier = f"cond-{run_salt}-{scope_key}{concurrency_suffix}"`, where
`run_salt` is a per-run random token, `scope_key` derives from `identifier_scope`,
and `concurrency_suffix` is a **mandatory** discriminator appended for any
concurrent unit (empty otherwise):

| `identifier_scope` | `scope_key` | Reuse across *sequential* re-executions |
|---|---|---|
| `workflow` | constant per run | one shared workspace for the whole workflow run |
| `agent` (default) | agent name | one workspace per agent (loop-backs / retries reuse it) |
| `item` | for-each item key | one workspace per for-each item |
| `none` | agent name + execution index | fresh workspace every execution, including retries (no reuse) |

The concurrency discriminator is what makes `concurrent_safe=True` honest.
`scope_key` alone is insufficient: the default `agent` scope derives one key from
the agent name, so a `for_each` with `max_concurrent > 1` over one agent would
otherwise collapse every iteration onto one shared session — and the validator,
gating for-each/parallel purely on the static `concurrent_safe` flag
(`config/validator.py`), would pass it and break isolation at runtime. So the
provider always mixes in a discriminator for concurrent units: the for-each loop
keys already in per-iteration `context` (`_key` when `key_by` is set, else
`_index`; `engine/workflow.py`), and for parallel groups the distinct member agent
name. Concurrent siblings therefore always resolve to distinct sessions; scope only
governs sequential reuse. The value is charset-normalized and truncated to ≤128
chars with a hash suffix, generated with a cryptographic salt (it is the
routing/isolation key).

**File staging.** The session filesystem *is* the workspace and is **ephemeral**
(no volume mount): seed inputs at session start (e.g. `git clone`), push artifacts
out (git push / blob) before cooldown. One session = one agent's (or run's)
workspace, aligning with the default `identifier_scope: agent`.

### API Contracts

**Host → runner (preferred, Branch S — per `execute`):**

```
POST {pool_endpoint}/execute?identifier=<id>&api-version=<v>
Authorization: <AAD access token — audience https://dynamicsessions.io>
Content-Type: application/json

{ "agent": { "name": "...", "model": "...", "output": {...}, "system_prompt": "...",
             "max_agent_iterations": N, "max_session_seconds": S, "reasoning": {...} },
  "rendered_prompt": "...",
  "tools": ["...", ...] | [] | null,     # per-agent allowlist (workflow_tools_passthrough)
  "mcp_servers": { "<name>": { ... } },  # full runtime.mcp_servers definitions (mcp_tools)
  "context": { ... } }                   # accumulated context needed by the agent
```

The request forwards the **full `runtime.mcp_servers` definitions** (not just tool
names) so the in-container `CopilotProvider` can make the declared tools
executable. This is the **runner-image contract**: stdio MCP servers must be baked
into the image (a declared-but-absent binary fails loudly at execute time — the
same failure mode as a missing host binary, not a silent drop), and remote
(HTTP/SSE) MCP servers require egress enabled.

**Runner → host (streaming response, `application/x-ndjson`):** one JSON object
per line; event frames reuse Conductor's vocabulary; the terminal frame is the
result.

```
{"type":"agent_turn_start","data":{"turn":"awaiting_model"}}
{"type":"agent_message","data":{"content":"..."}}
{"type":"agent_tool_start","data":{"tool":"...","args":{...}}}
{"type":"agent_tool_complete","data":{"tool":"...","result":"..."}}
{"type":"agent_reasoning","data":{"content":"..."}}
...
{"type":"result","data":{"content":{...},"model":"...","input_tokens":N,"output_tokens":M,"partial":false}}
```

**Fallback (Branch P) variant, not required for v1.** `POST /execute` returns
`{"job_id": "..."}` immediately; the host polls
`GET /execute/<job_id>/events?since=<cursor>`, each response returning a batch of
the same NDJSON frames until the terminal `result` frame. Identical event
vocabulary and `AgentOutput` shape; only the delivery differs. The Phase 0 spike
(#312) confirmed Branch P works end to end, so this contract stays documented as
a fallback the runner *could* expose later, without being required for the MVP
(DD3: Branch S ships).

**On-host `AgentProvider` contract (unchanged).** `AcaRuntimeProvider` conforms to
`execute()/validate_connection()/close()` and returns `AgentOutput`; no change to
the provider call site.

## Alternatives Considered

In the source issue's taxonomy this proposal is **Option C — Pattern 1
(Agent-in-Sandbox)**: the whole SDK (loop *and* tools) runs in the sandbox. The
alternatives keep the issue's original lettering (**A**, **B**, **D**), so **C**
is unambiguously this design. Each is distinct enough to track as its own issue.

### A — Sandbox as a `type: code` / `script` backend

Route `type: script` (and a new `type: code`) through the sandbox instead of the
local subprocess, at the `ScriptExecutor` seam.

- **Pros:** Smallest blast radius; cleanest seam; orthogonal to the LLM provider;
  contains the most obviously dangerous case (`command: "{{ llm.output.cmd }}"`).
- **Cons:** Does **not** isolate the *agent's built-in* tool calls.
- **Verdict:** Complementary, not a substitute. Separate issue.

### B — Sandbox-as-MCP-tool (Pattern 2)

Ship an MCP server (fits `runtime.mcp_servers`) exposing
`run_code`/`run_command`/file ops that execute in the sandbox; agents opt in via
`tools:`.

- **Pros:** Zero engine change; **keeps the loop and credentials on the host**
  (safer); the hybrid the leaders converged on (Anthropic Cowork moved the loop
  out; the official Azure sample is loop-outside/exec-inside); the only way to get
  "loop outside / exec inside" with the bundled SDKs.
- **Cons:** Isolates only what the agent routes through the MCP tools, not *all*
  built-in tool side effects.
- **Verdict:** **Safer and simpler**; recommended to sequence **before** this
  proposal if the goal is safe containment rather than maximal isolation.

### D — Whole-workflow isolation

Run `conductor run` itself inside a session/Job.

- **Pros:** Coarse, simple hosting story.
- **Cons:** Not an integration; loses host-side orchestration; a deployment
  topology, not a feature.
- **Verdict:** Out of scope.

**Framing.** For *maximum isolation of every built-in tool side effect off-host*,
this proposal (Option C) delivers it. For *safe containment of untrusted execution
with credentials on the host*, **B** is safer and simpler. They are not mutually
exclusive — B and A are the lighter, more defensive plays and can land first.

## Dependencies

### External

- **ACA dynamic sessions**, custom-container pool — a workload-profiles-enabled ACA
  environment, a runner image in ACR, and a session pool; requires the *Session
  Executor* RBAC role.
- **`azure-identity`** (`DefaultAzureCredential`) — new optional dep behind the
  `aca` extra.
- **`github-copilot-sdk`** — already base; runs *inside* the runner image.
- **`httpx`** (host→runner client) and **FastAPI + uvicorn** (runner server) —
  already base deps.

### Internal

- `AgentProvider` / `AgentOutput` (`providers/base.py`); `ProviderCapabilities` +
  resolver and validator cross-checks; `ProviderSettings` / `RuntimeConfig` and
  the factory; the event vocabulary and its consumers; the experimental-tier
  machinery.

### Sequencing constraints

- **Phase 0 transport spike is resolved** (DD3, issue #312 closed): a ~30-minute
  per-request cap was measured and confirmed reproducible; **Branch S** is
  chosen. Everything else is buildable; this was the true unknown and no longer
  blocks the build.
- **Credential model** (DD4): forward one narrowly-scoped credential (recommended a
  *Copilot Requests* PAT — your Copilot capacity); trusted-use posture. Keeping the
  credential off the sandbox for untrusted/multi-tenant use is future work — not a
  build blocker.
- **Alternative B** is recommended to precede this work if the immediate goal is
  safe containment.

## Impact Analysis

### Components affected

- **New:** `src/conductor/providers/aca.py`; the `conductor-agent-runner` server +
  image; an `examples/` workflow; a docs page; a mocked-runner smoke test.
- **Modified (additive):** `ProviderSettings` (`config/schema.py`);
  `create_provider()` + `ProviderType` (`providers/factory.py`);
  `_PROVIDER_CLASS_PATHS` (`capabilities.py`); `pyproject.toml` extras;
  session-seconds usage surfacing; `docs/providers/experimental.md` + AGENTS.md.

### Backward compatibility

Fully additive and opt-in. `runtime.provider` already accepts both the string and
the object form; a new `name` value with `name`-gated fields does not affect
existing workflows, and default on-host execution is unchanged. `ProviderSettings`
is `frozen` with `extra="forbid"`, so new fields are declared explicitly.

### Performance

- **Latency:** an extra network hop per `execute()` plus session allocation; warm
  pools (`readySessionInstances`) give near-instant allocation at idle-billing cost.
- **Throughput:** parallel/for-each fan-out maps to distinct sessions; warm-pool
  size bounds concurrency before cold-allocation latency appears.

### Operational (deploy / monitor / debug)

- **Deploy:** two-step (build/push image to ACR → create pool), operator-owned.
- **Monitor/cost:** the distinct session-seconds row surfaces Dedicated-E16 + warm
  pool cost (a proxy; see Key Components §5).
- **Debug:** streamed events land in the existing JSONL/dashboard pipeline;
  runner-side failures surface as real `ProviderError`s with the ACA structured
  error (`code`/`message`/`traceId`) attached for correlation.

## Security Considerations

This is the authoritative treatment of the credential boundary summarized after
the architecture; it follows directly from ACA's documented model.

- **ACA session security model.** Sessions are Hyper-V isolated **from each
  other**, but *everything within a single session — files and environment
  variables — is accessible to the session's own code*. Microsoft: "only
  configure or upload sensitive data to a session if you trust the users of the
  session." A model-driven shell **is** an untrusted user of that session.
- **No per-session secrets; no native egress allowlist.** Pool-level secrets are
  identical for every session, and egress is a single on/off
  (`sessionNetworkConfiguration.status`) with no per-destination filtering. The
  platform cannot keep a key from a compromised session or constrain exfiltration.
- **Credential model (DD4).** The provider forwards one narrowly-scoped credential
  to the sandbox per `execute()` — recommended: a fine-grained *Copilot Requests*
  PAT (authorizes only Copilot inference on your capacity, nothing else on the
  account) with a bounded expiry; BYOK custom-routing settings are the fallback.
  **The credential does enter the session** — a shell (possibly root; the Cowork
  root-escape shows root is reachable) can read any env var or file — so the
  defense is *scope and lifetime*, not concealment: a leaked *Copilot Requests* PAT
  can only spend your Copilot quota until it expires, and is centrally revocable.
  This makes `aca` suitable for **trusted** workloads (repos/workflows you control).
  Keeping the credential entirely off the sandbox (a host-side broker/relay that
  makes the model call on the sandbox's behalf) is the stronger boundary for
  untrusted/multi-tenant use and is **future work** — noted because ACA offers no
  per-session secret and no per-destination egress allowlist to lean on instead.
- **Anti-pattern (rejected).** Baking a long-lived `GITHUB_TOKEN`/
  `ANTHROPIC_API_KEY` as a pool secret or image env exposes the whole pool
  indefinitely.
- **Access control.** Management-endpoint calls require the *Session Executor* role
  (audience `https://dynamicsessions.io`) via `DefaultAzureCredential`; no standing
  key is embedded in Conductor.
- **Session managed identity (weighed, not the key path).** An ACA session can
  carry a managed identity, but its token is reachable by the in-session shell via
  IMDS — the same exposure class as a short-lived token — so it does not substitute
  for the boundary. If used (e.g. blob artifact staging), scope its RBAC to the
  minimum and never grant it the upstream model credential.
- **Secret hygiene.** Forwarded credentials are `SecretStr`, redacted in events,
  checkpoints, and the dashboard via existing `ProviderSettings` redaction.
- **Identifier as a capability.** The `identifier` is the routing/isolation key:
  cryptographically salted, never guessable, always over HTTPS.
- **Attack-surface delta.** New: a host→ACA management call and an in-container HTTP
  server. The *agent's* attack surface moves
  off-host into an ephemeral isolated VM (the point); the new host-side surface
  must stay minimal and credential-light.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Sessions endpoint drops long/streamed requests (the #1 unknown) | ~~High~~ Resolved | ~~High~~ Low | Phase 0 spike (#312, closed) measured a reproducible ~30-min per-request cap on default ingress; Branch S chosen (DD3); `streaming_events=True`/`interrupt=True` set honestly. Residual risk: a turn exceeding ~30 min still hits the cap (see DD3 caveat / Open Questions). |
| Credential leak from a compromised session | Medium | Medium | Forward only a narrowly-scoped credential — recommended a *Copilot Requests* PAT (Copilot-quota-only, bounded expiry, revocable); never bake a broad/long-lived pool secret. Trusted-use posture; off-sandbox isolation is future work (DD4). |
| Cost surprise (E16 nodes; idle warm pool) | Medium | Medium | Distinct session-seconds row; right-size `readySessionInstances`; modest per-session `--cpu/--memory`. |
| Required MCP stdio binary / egress absent (runner-image contract) | Medium | Medium | Bake stdio binaries into the image; enable egress for remote MCP; fail loudly at execute time. |
| Ephemeral FS loses artifacts (no volume mount) | High | Medium | Seed inputs at start; push artifacts out before cooldown; `checkpoint_resume=False`. |
| `conductor resume` can't restore in-sandbox state | High | Low | `checkpoint_resume=False`; resume re-runs the agent; blob-persisted workspace is an open question. |
| Runner ↔ Conductor version skew | Medium | Medium | Pin the image to a Conductor version; check on `validate_connection`; document the contract. |
| Interrupt weaker than on-host | Low | Low | Resolved to real interrupt via the in-flight stream (Branch S, DD3); `stopSession` hard-abort remains available as a fallback. |
| Provider-parity erosion (experimental) | Low | Medium | Reuse the real `CopilotProvider` (parity for free); mocked-runner smoke test; accurate capabilities. |
| Agent turn exceeds the measured ~30-min per-request cap | Low | Medium | Turns are expected to run "minutes" (NFR4), well under the cap; long-term mitigation is automatic reconnect-and-resume over `Last-Event-ID` (Open Questions); until built, an over-cap turn fails and must be retried. |

## Open Questions

Classified by when they must resolve: **Blocking** (Phase 0 gates the build, now
resolved — kept for record), **Pre-MVP** (affects the v1 contract — resolve within
Phase 1), **Future** (post-MVP / v2).

**Blocking (Phase 0) — resolved:**

- **Runner API branch.** Streaming (S) vs submit+poll (P) vs both, per the measured
  per-request/idle cap? How to resume a dropped stream mid-agent (`Last-Event-ID` +
  replayable server event log)? Fixes `streaming_events` / `interrupt`.

  **Resolved — Branch S, via the Phase 0 spike (issue #312, closed).** A throwaway
  custom-container pool ran a heartbeat runner (one NDJSON frame/sec, then a
  terminal `result` frame) against real ACA infrastructure, on **default (not
  premium) ingress**. Measured results:

  - **True per-request forwarded-duration cap ≈ 1801 seconds (~30 minutes).** A
    first run requesting a 1800s turn was cut off at 1801.18s; a disambiguation
    run requesting **2400s (40 min)** was cut off at the *same* ~1801s mark,
    ruling out coincidence with the first run's own requested duration and
    confirming a real, reproducible platform cap.
  - **≥10-minute streaming durability: confirmed.** A 900s stream completed
    cleanly with steady ~1s inter-frame gaps (no buffering/batching) and reached
    its terminal frame.
  - **`Last-Event-ID` resume: confirmed viable.** A stream dropped at 60s and
    resumed from `Last-Event-ID` continued with no gap and no duplicate frame.
  - **Branch P (submit + poll): also confirmed working**, end to end, with zero
    missing frames — kept as an available fallback mechanism, not required for
    the MVP.

  **Decision: Branch S.** ~30 minutes is well above the expected length of a
  single agent turn (NFR4), so `streaming_events=True` and `interrupt=True`
  (real, via the in-flight stream) replace the conditional values this question
  previously carried (see DD3, the capabilities table, and the Risks table).
  **Residual item carried to Pre-MVP:** whether v1 needs to *build*
  reconnect-and-resume for turns that exceed the ~30-minute cap, or defer it (see
  below) — the spike showed resume is *mechanically* viable, but a production
  server-side event log needs to be bounded, unlike the spike's unbounded
  in-memory one.

**Pre-MVP (v1 contract):**

- **Reconnect-and-resume for turns exceeding the ~30-minute cap.** Build automatic
  reconnect via `Last-Event-ID` for v1, or defer and let an over-cap turn simply
  fail/retry? The Phase 0 spike confirmed resume is mechanically viable (no
  gap/duplicate on a clean reconnect), but the spike's in-memory, unbounded
  per-stream event log is not production-shaped — a v1 implementation needs a
  *bounded* server-side log (oldest frames evicted once acknowledged/replayed) so
  a long-idle or never-reconnected session cannot grow its event log unboundedly.
  Given turns are expected to run "minutes" (NFR4) and ~30 min is a generous
  margin, deferring this and treating an over-cap turn as a hard failure is a
  reasonable MVP scope cut — revisit if real usage shows turns routinely
  approaching the cap.

- **Identifier scoping.** Per-agent (default) vs per-workflow; exact naming scheme
  (parallel-safe, unpredictable, ≤128 chars); final shape of the per-agent
  `sandbox:` override block.

  **Answer — default to per-agent scope.** `identifier_scope: agent` is the
  default: one session per agent, reused across that agent's *sequential*
  re-executions (loop-backs) so a coding agent keeps its workspace between turns,
  while the **mandatory concurrency discriminator (DD5)** always diverges the
  identifier per concurrent unit so parallel / for-each runs stay isolated and
  `concurrent_safe=True` stays truthful. `workflow`, `item`, and `none` remain opt-in
  overrides. Naming scheme: a slugified `<workflow>-<agent>` prefix + the
  concurrency discriminator + an unpredictable random suffix, truncated to the ACA
  ≤128-char limit. The per-agent `sandbox:` block carries the `identifier_scope`
  override (and `working_dir`, below).
- **Image ownership.** Publish an official `conductor-agent-runner` image, or
  document a runner *contract* and let users bring their own?

  **Answer — do both.** Publish an official `conductor-agent-runner` **base image**
  (the runner + a pinned Conductor + the common stdio MCP binaries) that works
  out-of-the-box for the Copilot MVP, **and** document the runner *contract* — the
  `/execute` + `/health` HTTP surface, the NDJSON event-frame schema, and the
  credential-forwarding contract — so users can `FROM conductor-agent-runner:<tag>` to
  extend it (extra MCP servers, system deps, language toolchains) or build a fully
  custom conformant image. Base image = zero-config fast path; contract = escape
  hatch. `validate_connection()` checks the runner/Conductor version to catch skew.
- **`working_dir` semantics.** Define container-relative `working_dir` (capability
  `True`) or keep `False` for the MVP?

  **Answer — container-relative, capability `True`.** A host path is meaningless in
  a remote container, so interpret `working_dir` as a path **inside the session
  filesystem** (e.g. `/workspace/repo`), defaulting to the runner's working
  directory when unset. A supplied path that does not exist in the container is a
  runtime error, never a silent host fallback. This flips the capability row from
  `False (MVP)` to `True`; the value rides on the per-agent `sandbox:` block
  alongside `identifier_scope`.
- **MCP in the sandbox.** How are stdio MCP binaries provisioned in the image, and
  should an unsupported stdio server be a validation error under `aca` rather than
  a runtime failure?

  **Answer — allow at validate time, fail at run time.** Do **not** make an
  unsupported stdio MCP server a `conductor validate` error under `aca`: validation
  cannot know a given image's contents (users extend the base image, above), so a
  static rejection would be wrong for anyone who baked the binary in. `mcp_servers`
  config passes validation unchanged (`mcp_tools=True`), and a missing stdio binary
  surfaces as a **runtime error** from the runner, propagated to the host as a
  `ProviderError` — the same failure mode as a missing binary on-host.
  Provisioning: the official base image bakes the common stdio servers; additional
  ones are added by extending the image; remote MCP requires pool egress. A
  non-blocking `validate` *warning* (stdio MCP under `aca` depends on image
  contents) is acceptable, but not a hard error.
- **Dialog turns.** Must `execute_dialog_turn()` route through the sandbox
  for consistency in v1, or is it deferred?

  **Answer — route through the sandbox for consistency.** A "dialog turn"
  is the follow-up interactive turn some flows issue on an existing session (e.g.
  the dialog evaluator behind human-gate refinement). For v1, `execute_dialog_turn()`
  should hit the **same** in-sandbox runner as `execute()`, reusing
  the agent's `identifier` (DD5) so every turn for that agent shares one isolation
  and credential boundary; running dialog turns on the host would leak to the host
  filesystem and bypass the sandbox boundary — exactly the inconsistency to avoid. If the
  runner does not expose a dialog endpoint in the MVP, the fallback is to disable
  dialog turns under `aca` with a clear error rather than silently run them
  on-host.

**Future (post-MVP):**

- **Stronger credential isolation (untrusted/multi-tenant).** Keep the forwarded
  credential entirely off the sandbox via a host-side broker/relay that makes the
  model call on the sandbox's behalf; pair with egress allowlisting (VNet + NSG).
  Out of scope for the trusted-use MVP.
- **Resume meaningfulness.** Persist the workspace to blob so `conductor resume`
  becomes meaningful despite `checkpoint_resume=False`?
- **Inner provider coverage.** Add `claude-agent-sdk` (the containerizable `claude`
  CLI) inside the runner — when and how? (The bare `claude` provider is not a
  candidate.)
- **Cost model surfacing.** Exact CLI-summary / dashboard representation of
  session-seconds.
- **Generalization.** Abstract the remote-runner mechanism to non-ACA sandboxes
  (E2B/Modal/Daytona) behind one interface — does that change the contract now?

## References

**Conductor seams (verified in-repo):**

- `src/conductor/providers/base.py` — `AgentProvider` ABC, `AgentOutput`,
  `execute()` signature, `__init_subclass__` capability enforcement.
- `src/conductor/providers/capabilities.py` — `ProviderCapabilities`,
  `get_capabilities`, `_PROVIDER_CLASS_PATHS`.
- `src/conductor/providers/factory.py:86` — `create_provider()` match;
  `ProviderType` (`factory.py:27`).
- `src/conductor/config/schema.py:1667` — `ProviderSettings`;
  `_check_field_compatibility` (`:1784`); `has_custom_routing` (`:1875`);
  `RuntimeConfig` (`:1962`).
- `src/conductor/providers/copilot.py:360` — `_resolve_sdk_provider_config`
  (custom routing + `COPILOT_PROVIDER_*` env fallbacks);
  `copilot.py:803` — `resolved_cwd = agent.working_dir or os.getcwd()`;
  `copilot.py:220` — `structured_output="prompt_injection"`.
- `src/conductor/config/validator.py` — capability cross-checks (`~:1561`+).
- `src/conductor/executor/agent.py:288` — `provider.execute()` call site.
- `src/conductor/executor/script.py` — `ScriptExecutor` (Alternative A seam).
- `src/conductor/providers/claude_agent_sdk.py` — canonical experimental
  provider (event/output parity while delegating the loop).
- `docs/providers/experimental.md` — experimental tier policy;
  `pyproject.toml` `[project.optional-dependencies]` — extras pattern.

**Phase 0 transport spike (resolved DD3):**

- [Issue #312](https://github.com/microsoft/conductor/issues/312) (closed) — spike
  tracking issue, decision rule, and acceptance criteria.
- `spikes/aca-transport/` (branch `feature/312-aca-transport-spike`) — the
  heartbeat runner, probe client, provisioning/teardown scripts, and
  `RESULTS.md` with the full measured data (raw JSON under `results/`).

**Azure Container Apps (Microsoft Learn — verified):**

- [Dynamic sessions concepts / key concepts](https://learn.microsoft.com/azure/container-apps/sessions)
- [Use dynamic sessions (send requests, identifiers, security, egress, auth)](https://learn.microsoft.com/azure/container-apps/sessions-usage)
  — `<POOL_MANAGEMENT_ENDPOINT>/<path>?identifier=<id>` → `<TARGET_PORT>/<path>`;
  session security model; `sessionNetworkConfiguration.status` egress; *Session
  Executor* role; audience `https://dynamicsessions.io`; `DefaultAzureCredential`.
- [Use session pools (custom container pool; lifecycle)](https://learn.microsoft.com/azure/container-apps/session-pool)
  — `Timed` cooldown 300–3600 s (reset by each request) vs `OnContainerExit` +
  `maxAlivePeriodInSeconds`; `readySessionInstances`.
- [Custom-container sessions](https://learn.microsoft.com/azure/container-apps/sessions-custom-container)
- [Session Pools ARM/Bicep reference](https://learn.microsoft.com/azure/templates/microsoft.app/2025-07-01/sessionpools)
- [Premium ingress (request idle timeout: default 4 min, max 30 min)](https://learn.microsoft.com/azure/container-apps/premium-ingress)
- [Billing](https://learn.microsoft.com/azure/container-apps/billing)
- [`az containerapp sessionpool` CLI](https://learn.microsoft.com/cli/azure/containerapp/sessionpool)
- [Official sample: Azure-Samples/dynamic-sessions-custom-container](https://github.com/Azure-Samples/dynamic-sessions-custom-container)

**Prior art:**

- [LangChain — the two patterns by which agents connect sandboxes](https://www.langchain.com/blog/the-two-patterns-by-which-agents-connect-sandboxes)
- [Cloudflare — Sandbox auth (Outbound Workers)](https://blog.cloudflare.com/sandbox-auth/)
- [Anthropic "How We Contain Claude" (analysis)](https://the-agent-report.com/2026/05/anthropic-contains-claude-sandbox-vm-agent-security/)
- [Claude Code sandbox environments](https://code.claude.com/docs/en/sandbox-environments)
- [GitHub Copilot coding-agent firewall](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/customize-the-agent-firewall)
- [AG-UI protocol (resumable event streams)](https://github.com/ag-ui-protocol/ag-ui/)
