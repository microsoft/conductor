# ACA (Azure Container Apps) Provider Documentation

> **Experimental Provider** — `aca` delegates the entire agentic loop to a
> remote Azure Container Apps (ACA) dynamic-sessions sandbox instead of
> running it on the host. `conductor validate` catches workflows that
> depend on unsupported features, and the CLI prints a one-time banner at
> runtime. See [Experimental Providers](./experimental.md) for the
> stability policy and promotion criteria.

The `aca` provider is a thin host-side transport shim
(`AcaRuntimeProvider`) that relocates an agent's *entire* execution — the
agentic loop, its built-in tools, and any MCP tool calls — into an ACA
dynamic-sessions custom-container pool, instead of running it in the
Conductor host process. Only one agent's sub-loop moves into the sandbox
at a time; the workflow-level loop (routing, `WorkflowContext`,
checkpoints, the event bus) always stays on the host, unchanged.

Full architecture, design decisions, and open-question resolutions live in
the source design:
[`docs/projects/aca/aca-provider.design.md`](../projects/aca/aca-provider.design.md)
(issue [#284](https://github.com/microsoft/conductor/issues/284)).

## Table of Contents

- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Provisioning a Pool](#provisioning-a-pool)
- [Runner Contract](#runner-contract)
- [NDJSON Event Frame Schema](#ndjson-event-frame-schema)
- [Building / Extending the Runner Image](#building--extending-the-runner-image)
- [Authentication](#authentication)
  - [Inner Copilot Authentication](#inner-copilot-authentication)
- [Workflow Configuration](#workflow-configuration)
- [Capability Carve-outs](#capability-carve-outs)
  - [Known Gaps (Runner MVP)](#known-gaps-runner-mvp)
- [Cost Note](#cost-note)
- [Security](#security)
- [Troubleshooting](#troubleshooting)

## Quick Start

### 1. Install the azure-identity extra

```bash
# Using uv (recommended)
uv add 'conductor-cli[aca]'

# Using pip
pip install 'conductor-cli[aca]'
```

This pins `azure-identity` plus `azure-core[aio]` (which pulls in `aiohttp`),
used to acquire a `dynamicsessions.io` bearer token via the async
`DefaultAzureCredential` for the *Session Executor* role — `azure-identity`
alone does not include an async HTTP transport, so `azure-core[aio]` is
required for the credential to construct without an `ImportError`. `httpx`
(the host→runner client) is already a base dependency.

### 2. Authenticate to the pool

```bash
az login
```

Any `DefaultAzureCredential`-compatible identity (Azure CLI login,
managed identity, service principal env vars, …) with the *Session
Executor* role on the pool works — see
[Authentication](#authentication).

### 3. Authenticate the inner Copilot session

The pool identity above only gets the host into the sandbox — it says
nothing about the **in-container Copilot session** the runner drives on
your behalf. That session cannot perform interactive OAuth login inside a
headless container, so it needs its own credential, forwarded from the
**host** on every request (see
[Inner Copilot Authentication](#inner-copilot-authentication)).

**Default: run on your own Copilot capacity.** Create a fine-grained
GitHub personal access token with only the ***Copilot Requests*
permission**, then export it before `conductor run`:

```bash
export COPILOT_GITHUB_TOKEN=<your fine-grained PAT>
```

No `COPILOT_PROVIDER_*` configuration is required for this default path —
the provider forwards the token in-memory to the sandbox's inner Copilot
runtime, which authenticates against GitHub Copilot's own model routing
(the same capacity your `az login`/CLI identity already has). `GH_TOKEN`
and `GITHUB_TOKEN` are also recognized (in that priority order) for
environments that already export one of those, but a dedicated
`COPILOT_GITHUB_TOKEN` scoped to *Copilot Requests only* is recommended so
you aren't forwarding a broader-scoped token than necessary into the
sandbox.

**Fallback: BYOK custom routing.** If you need to route the sandbox at a
custom OpenAI-compatible / Azure / Anthropic endpoint instead (as with the
host's own [structured `runtime.provider`](../configuration.md)), export:

```bash
export COPILOT_PROVIDER_BEARER_TOKEN=<your Copilot-compatible token>
# or: COPILOT_PROVIDER_API_KEY (+ COPILOT_PROVIDER_BASE_URL)
```

`COPILOT_PROVIDER_BASE_URL` (if set) always wins over a GitHub token, so a
BYOK endpoint stays authoritative even when both are exported.

**Trusted-use posture:** whichever credential you export, it *does* enter
the sandbox and is readable by a model-driven shell there — ACA offers no
per-session secret isolation or per-destination egress allowlist. Keep it
narrowly scoped (a *Copilot Requests*-only PAT can spend nothing but your
Copilot quota) and give it a short expiry so a leak is bounded and
revocable. This mechanism is acceptable only for **trusted** workloads
(workflows and repos you control) — it is not safe for untrusted or
multi-tenant use. Keeping the credential entirely off the sandbox (a
host-side broker) is future work; see [Security](#security).

Skipping this step is not a silent no-op: every agent turn in the sandbox
fails because the in-container `CopilotProvider` has no credential to
construct a session with.

### 4. Provision a pool (bring-your-own — Conductor does not do this for you)

```bash
EGRESS=enabled ./scripts/aca/provision-pool.sh
```

This is a documented, runnable *example* of the two-step deploy: build/push
the `conductor-agent-runner` image to Azure Container Registry, then create
the dynamic-sessions custom-container pool from it and grant the caller the
*Session Executor* role. See the script's header comment for the full
prerequisite list (resource group, workload-profiles-enabled Container Apps
environment, ACR). `EGRESS=enabled` is required (the script defaults to
`disabled`, the safer choice for pools that don't need it) — both cloning a
repo and reaching the Copilot model backend from inside the sandbox require
outbound network access.

### 5. Update your workflow

```yaml
workflow:
  name: my-workflow
  runtime:
    provider:
      name: aca
      pool_endpoint: "https://my-agent-pool.<region>.azurecontainerapps.io"
      api_version: "2025-07-01"
      inner_provider: copilot
      identifier_scope: agent
      egress: enabled # must be enabled — the inner Copilot call always needs it
      lifecycle: timed
      auth: azure_default
    default_model: gpt-4.1

agents:
  - name: assistant
    prompt: |
      Answer the following question: {{ workflow.input.question }}
    output:
      answer:
        type: string
```

See [`examples/aca-coding-agent.yaml`](../../examples/aca-coding-agent.yaml)
for a complete, runnable coding-agent pattern (clone → implement → test →
loop back on failure) that stays in the same ACA session across
loop-backs.

## Architecture

```
┌───────────────── Conductor host (orchestrator) ──────────────────────┐
│ WorkflowEngine: routing · WorkflowContext · checkpoints · event bus    │
│                                                                        │
│ AcaRuntimeProvider(AgentProvider)             ← experimental tier      │
│   execute(agent, ctx, prompt, tools, event_callback, interrupt):       │
│     id  = identifier_for(scope)      # workflow | agent | item | none  │
│     tok = DefaultAzureCredential()            # aud dynamicsessions.io  │
│     POST {pool}/execute?identifier=id         # AAD token, exec role    │
│     for line in ndjson(resp): event_callback(line.type, line.data)     │
│     return AgentOutput(**final_result)                                 │
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
                 │  │  inner SDK → gateway ──┼──┼─▶ model inference (Copilot / Anthropic)
                 │  └────────────────────────┘  │
                 └──────────────────────────────┘
```

`AcaRuntimeProvider` (`src/conductor/providers/aca.py`) owns no agentic
logic itself. Per `execute()` call it:

1. Derives a session `identifier` from the effective `identifier_scope`
   (per-agent `sandbox.identifier_scope` override, else the workflow-wide
   default) — see [Workflow Configuration](#workflow-configuration).
2. Acquires a cached AAD bearer token for the `https://dynamicsessions.io`
   audience.
3. Issues a single streaming `POST {pool_endpoint}/execute?identifier=<id>`
   request; ACA routes to the session for `<id>` (auto-allocating from the
   warm pool if none exists yet) and forwards the body to the container's
   `/execute`.
4. Relays each NDJSON event frame verbatim to `event_callback` as the
   in-container `conductor-agent-runner` emits it — dashboard, JSONL, and
   console render exactly as for on-host providers.
5. Parses the terminal `result` frame into `AgentOutput` and returns it to
   the engine, which records sandbox elapsed seconds as a distinct
   `"<agent> (sandbox)"` usage row (see [Cost Note](#cost-note)).

**Identifier scoping (`identifier_scope`).** ACA keys sessions by a
free-form `identifier` (existing → routed; new → auto-allocated), with
state persisting for the session lifetime:

| `identifier_scope` | Reuse across *sequential* re-executions |
|---|---|
| `workflow` | one shared workspace for the whole workflow run |
| `agent` (default) | one workspace per agent (loop-backs / retries reuse it) |
| `item` | one workspace per for-each item |
| `none` | fresh workspace every execution, including retries (no reuse) |

Concurrent units (parallel-group members, for-each iterations under
`max_concurrent > 1`) always diverge onto distinct sessions regardless of
scope — a mandatory concurrency discriminator is mixed into the wire
identifier so `concurrent_safe=True` stays honest.

**File staging.** The session filesystem *is* the agent's workspace and is
**ephemeral** — there is no volume mount. Seed inputs at session start
(e.g. `git clone`) and push artifacts out (git push / blob upload) before
the session cools down; nothing survives past the session's lifetime.

## Provisioning a Pool

Conductor does **not** provision ACA infrastructure — v1 consumes an
operator-created `pool_endpoint` (the "bring-your-own pool" model).
[`scripts/aca/provision-pool.sh`](../../scripts/aca/provision-pool.sh) is a
documented, runnable example of the two-step deploy:

1. `az acr build` — build/push the `conductor-agent-runner` image to Azure
   Container Registry (builds in the cloud; no local Docker daemon
   required).
2. `az containerapp sessionpool create --container-type CustomContainer …`
   — create the dynamic-sessions pool from that image, then grant the
   caller (or a service principal / managed identity) the *Session
   Executor* RBAC role.

The pool's management endpoint (printed at the end of the script) is what
you set as `runtime.provider.pool_endpoint` in your workflow YAML.

## Runner Contract

The in-container `conductor-agent-runner` (`src/conductor/aca_runner/server.py`,
shipped in-package with `conductor-cli` — no separate runner package)
exposes two HTTP endpoints:

### `GET /health`

Readiness + version probe, used by `validate_connection()` to detect
host/runner version skew and by the image's own `HEALTHCHECK`.

```json
{"ready": true, "conductor_version": "0.4.0", "runner_version": "0.1.0"}
```

### `POST /execute?identifier=<id>&api-version=<v>`

Runs one agent turn and streams the result back as
`application/x-ndjson`. Request body:

```json
{
  "agent": {
    "name": "...", "model": "...", "system_prompt": "...", "output": {...},
    "max_agent_iterations": 10, "max_session_seconds": 900,
    "reasoning_effort": "medium", "working_dir": "/workspace/repo",
    "retry": {...}, "context_tier": "default"
  },
  "rendered_prompt": "...",
  "tools": ["..."] ,
  "mcp_servers": {"git": {"command": "git-mcp-server", "tools": ["*"]}},
  "context": {...},
  "inner_provider": "copilot",
  "inner_provider_settings": {...},
  "tool_output": {...}
}
```

- `tools` — the per-agent allowlist would be forwarded (`null` = all
  workflow tools) but is **not enforced**: the in-container
  `CopilotProvider` records it but never applies it to the SDK session, so
  every tool/MCP server available to that session is callable regardless
  of the declared allowlist (`workflow_tools_passthrough=False` — see
  [Capability Carve-outs](#capability-carve-outs)). Because `aca` also
  forwards the *full* configured `mcp_servers` set unconditionally
  (`mcp_tools=True`), there is no allowlist value — including `tools: []`
  — that the runner can honor today; `conductor validate` rejects any
  explicit `tools:` on an `aca`-backed agent for this reason. Omit
  `tools:` entirely to run with the provider's default tool preset.
- `mcp_servers` — the **full** `runtime.mcp_servers` definitions (not just
  tool names), so the in-container `CopilotProvider` can make the declared
  tools executable. This is the **runner-image contract**: stdio MCP
  servers must already be baked into the image (a declared-but-absent
  binary fails loudly at execute time, the same failure mode as a missing
  host binary — never a silent drop); remote (HTTP/SSE) MCP servers
  require pool egress enabled.
- `inner_provider_settings` — the credential for the sandbox's inner
  Copilot session (design DD4): either a GitHub token (default — your own
  Copilot capacity) or BYOK custom-routing settings (fallback), resolved
  host-side and delivered in-memory per request. See
  [Inner Copilot Authentication](#inner-copilot-authentication) and the
  design's Security Considerations.

## NDJSON Event Frame Schema

One JSON object per line; event types reuse Conductor's own vocabulary so
the host relays `(type, data)` to `event_callback` with **no translation**:

```
{"type":"agent_turn_start","data":{"turn":"awaiting_model"}}
{"type":"agent_message","data":{"content":"..."}}
{"type":"agent_tool_start","data":{"tool":"...","args":{...}}}
{"type":"agent_tool_complete","data":{"tool":"...","result":"..."}}
{"type":"agent_reasoning","data":{"content":"..."}}
...
{"type":"result","data":{"content":{...},"model":"...","input_tokens":N,"output_tokens":M,"session_seconds":S,"partial":false}}
```

The stream always terminates in exactly one of:

- `result` — the successful `AgentOutput` payload, including
  `session_seconds` (sandbox wall-clock time, parsed into
  `AgentOutput.session_seconds`; see [Cost Note](#cost-note)).
- `error` — `{"message": "..."}` on an inner-provider failure, surfaced
  host-side as a `ProviderError`.

Non-2xx HTTP responses instead carry an ACA-management-style structured
error body (`code` / `message` / `traceId`), so host-side error messages
can reference the same diagnostic identifiers an operator would use with
Azure support.

## Building / Extending the Runner Image

[`docker/aca-runner/Dockerfile`](../../docker/aca-runner/Dockerfile) is the
official base image: a pinned Conductor install (which ships the
`conductor.aca_runner` server in-package) plus `git`, Node.js/npm, and the
`git-mcp-server` stdio MCP binary the example workflow relies on.

```bash
# Build (context is the docker/aca-runner directory)
docker build -t conductor-agent-runner:<tag> docker/aca-runner
```

Extend it with extra MCP servers, language toolchains, or system
dependencies:

```dockerfile
FROM conductor-agent-runner:<tag>
RUN pip install --no-cache-dir my-extra-mcp-server
```

A fully custom, non-extending image only needs to implement the
[Runner Contract](#runner-contract) (`/execute` + `/health`) and the
[NDJSON frame schema](#ndjson-event-frame-schema) above — extending the
official base image is a convenience, not a requirement.

## Authentication

There are **two independent auth layers** — a workflow needs both:

### Host → Pool (Session Executor role)

`auth: azure_default` (the only supported strategy) means the host
acquires a `https://dynamicsessions.io` bearer token via
`DefaultAzureCredential` — no standing key is embedded in Conductor. Any
credential source `DefaultAzureCredential` supports works: `az login`,
managed identity, environment-variable service principal, Visual Studio
Code sign-in, etc. The identity needs the **Session Executor** RBAC role
on the pool (granted by `scripts/aca/provision-pool.sh` for the caller
identity as part of provisioning).

The token is cached host-side and refreshed ahead of expiry
(`_TOKEN_REFRESH_MARGIN_SECONDS`), so a long-running workflow does not
re-authenticate on every agent turn. This only gets the host's *request*
into the sandbox — it grants nothing to the inner Copilot session running
inside it (see below).

### Inner Copilot Authentication

The in-container runner wraps a real `CopilotProvider` that itself needs
model-backend credentials, and it cannot fall back to the normal
interactive OAuth device-code flow — there is no terminal/browser to
complete it from inside a headless sandbox session. Instead, the **host**
resolves one credential per request and forwards it to the runner, with
no `credential_mode` switch to configure — precedence mirrors the
Copilot CLI's own auth resolution (design *DD4*):

1. **BYOK custom routing** — if `COPILOT_PROVIDER_BASE_URL` is set on the
   host, it always wins: the base URL plus optional
   `COPILOT_PROVIDER_API_KEY` / `COPILOT_PROVIDER_BEARER_TOKEN` are
   forwarded unchanged.
2. **Default: your own Copilot capacity** — otherwise, if a GitHub token
   is present (`COPILOT_GITHUB_TOKEN` → `GH_TOKEN` → `GITHUB_TOKEN`, first
   non-empty wins), it is forwarded as `github_token` and the sandbox's
   inner Copilot runtime authenticates against **GitHub Copilot's own
   model routing** — the same capacity your host identity already has.
   **Recommended: a fine-grained PAT scoped to only the *Copilot
   Requests* permission.**
3. **Neither is set** → the provider fails loudly with setup guidance
   rather than running the sandbox unauthenticated or silently degraded.

```bash
# Default (Copilot capacity) — no COPILOT_PROVIDER_* required:
export COPILOT_GITHUB_TOKEN=<fine-grained PAT, "Copilot Requests" only>

# Fallback (BYOK custom routing):
export COPILOT_PROVIDER_BEARER_TOKEN=<token>
# or
export COPILOT_PROVIDER_API_KEY=<key>
export COPILOT_PROVIDER_BASE_URL=<url>   # required alongside API_KEY
```

`AcaRuntimeProvider._resolve_inner_provider_settings()` implements this
precedence and forwards the result as `inner_provider_settings` on the
`/execute` request body — the credential is delivered **in-memory**
(request body → the inner runtime's `create_session`/`resume_session`
call), never written to a sandbox environment variable or persisted as a
pool secret. The runner constructs
`ProviderSettings(name="copilot", **inner_provider_settings)` (BYOK case)
or passes `github_token` straight through to its inner `CopilotProvider`
(default case) instead of attempting an impossible interactive login.

**Trusted-use posture** (DD4): the credential *does* enter the sandbox
and is readable by a model-driven shell there — ACA offers no
per-session secret isolation or per-destination egress allowlist. The
defense is *scope and lifetime*, not concealment: a leaked *Copilot
Requests* PAT can only spend your Copilot quota until it expires and is
centrally revocable, which is what makes the default path safe for
**trusted** workloads (workflows and repos you control) — never a
long-lived personal token or a broadly-scoped API key. This mechanism is
not safe for untrusted or multi-tenant use; keeping the credential
entirely off the sandbox (a host-side broker/relay) is future work — see
[Security](#security). If neither a GitHub token nor a BYOK endpoint is
configured, every in-sandbox agent turn fails outright — there is no
silent degraded mode.

## Workflow Configuration

### `runtime.provider` (workflow-level, required for `aca`)

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | `"aca"` | — | Selects the ACA provider. |
| `pool_endpoint` | `str` | *(required)* | ACA dynamic-sessions pool management endpoint. **Must be `https://`** with a hostname and no query string / fragment — AAD bearer tokens and forwarded provider credentials (`inner_provider_settings`) are sent to this endpoint on every request, and `identifier` / `api-version` / the request path are appended to it; `conductor validate` rejects a plain-`http://` value, a bare `https://` with no host, or one that already carries `?query` / `#fragment`. |
| `api_version` | `str` | `"2025-07-01"` | ACA management API version. |
| `inner_provider` | `"copilot"` | `copilot` | SDK the in-sandbox runner drives. **MVP: `copilot` only** — `claude-agent-sdk` inside is a future extension; the bare `claude` (Anthropic-API) provider has no in-process tool runtime and is not valid here. |
| `identifier_scope` | `workflow \| agent \| item \| none` | `agent` | Default granularity for *sequential* session reuse (see [Architecture](#architecture)). Concurrent units always diverge regardless. |
| `egress` | `enabled \| disabled` | — | Advisory mirror of the pool's own `sessionNetworkConfiguration.status` (the pool governs actual egress). |
| `lifecycle` | `timed \| on_container_exit` | — | Advisory mirror of the pool's session lifecycle mode. |
| `auth` | `"azure_default"` | `azure_default` | Session Executor authentication strategy (currently the only one supported). |

### `sandbox:` (per-agent override block)

Only meaningful when the agent's effective provider is `aca`; validates
structurally regardless of provider.

```yaml
agents:
  - name: implement
    sandbox:
      identifier_scope: item      # overrides runtime.provider.identifier_scope
      working_dir: /workspace     # container-relative, NOT a host path
```

| Field | Type | Description |
|---|---|---|
| `identifier_scope` | `workflow \| agent \| item \| none` | Overrides the workflow-wide `identifier_scope` for this agent's session. |
| `working_dir` | `str` | Working directory **inside the sandbox session filesystem**. Unlike the top-level `agent.working_dir` (a *host* path resolved against the workflow file's directory), this is interpreted container-relative — a path inside the remote session filesystem. Defaults to the runner's own working directory when unset; **the path must already exist when the session starts** (a path that doesn't exist in the container is a runtime error, never a silent host fallback) — point it at a directory baked into the runner image (like `/workspace`, created by `docker/aca-runner/Dockerfile`), not at a subdirectory a tool call (e.g. `git clone`) is expected to create on first run. See [`examples/aca-coding-agent.yaml`](../../examples/aca-coding-agent.yaml). |

## Capability Carve-outs

`aca` declares the following `ProviderCapabilities` (experimental tier):

| Capability | Value | Notes |
|---|---|---|
| `mcp_tools` | ✅ `True` | Full `mcp_servers` forwarded — runner-image contract. |
| `workflow_tools_passthrough` | ❌ **`False`** | The per-agent `tools:` allowlist is forwarded to the runner in the request body, but the in-container `CopilotProvider` it wraps never applies that list to the SDK session — every tool/MCP server available to the session is callable regardless of the declared allowlist. Combined with `mcp_tools=True` (below), there is no allowlist value the runner can honor — not even `tools: []` — so `conductor validate` rejects any explicit `tools:` on an `aca`-backed agent. This is a known, allowed experimental carve-out (the same gap `claude_agent_sdk` and `hermes` already declare, though those declare `mcp_tools=False` so `tools: []` stays valid for them). |
| `streaming_events` | ✅ `True` | Single streaming request relays event frames incrementally. |
| `agent_reasoning_events` | ✅ `True` | Runner forwards reasoning frames from the inner provider. |
| `reasoning_effort` | ✅ Copilot's full tuple | Inner provider (Copilot) translates reasoning effort natively. |
| `structured_output` | `prompt_injection` | Inherits the real `CopilotProvider` — Copilot has no native JSON mode. |
| `interrupt` | ✅ `True` | Host-side: a real in-flight-stream interrupt is attempted, with a best-effort `DELETE {endpoint}/session` (session-deletion) call as a hard-abort fallback if the interrupt itself fails to send. **Known runner gap**: the shipped `conductor-agent-runner` (epic E4 MVP) does not yet expose the `/interrupt` endpoint the host calls, and ACA's session-delete data-plane operation is documented as unsupported for custom-container pools — so today neither fallback actually stops the *remote* execution. The host eventually gives up waiting on the stream and reports the turn `partial`, but this is **not instantaneous**: both cleanup calls use an explicit 10-second per-call timeout (overriding the client's longer connect/write/pool defaults), not a guaranteed immediate return. Either way, the sandbox call keeps running server-side until it finishes naturally or `max_session_seconds` elapses. See [Known Gaps](#known-gaps-runner-mvp). |
| `max_session_seconds` | ✅ `True` | Best-effort only: the value is forwarded into the wrapped `CopilotProvider`'s own `IdleRecoveryConfig` wall-clock check inside the container, which is Copilot-internal timeout behavior, not a runner-enforced guarantee of remote termination. There is no *separate* runner-level guard watching the request. If the inner call hangs in a way that check doesn't catch, there is no independent runner-side backstop in the MVP. See [Known Gaps](#known-gaps-runner-mvp). |
| `checkpoint_resume` | ❌ **`False`** | Sessions are ephemeral with no volume mount; `conductor resume` re-runs the agent rather than restoring in-sandbox state. |
| `usage_tracking` | ✅ `True` | The runner returns token counts (and `session_seconds`) on the terminal result frame. |
| `concurrent_safe` | ✅ `True` | Mandatory concurrency discriminator in identifier derivation. |
| `working_dir` | ❌ **`False`** | This capability field means "applies the generic, host-resolved `agent.working_dir` / `runtime.working_dir`". `aca` never reads that field — only the separate, container-relative `sandbox.working_dir` (see above) is honored. Setting the generic field on an `aca`-backed agent fails `conductor validate`. |

The **notable carve-outs are `workflow_tools_passthrough=False`,
`working_dir=False`, and `checkpoint_resume=False`**: the first two reflect
what the wrapped `CopilotProvider` and the sandbox filesystem actually do
today (not what a naive reading of the runner-image contract might
suggest), and the third exists because the session filesystem is ephemeral
with no volume mount, so there is nothing for `conductor resume` to restore
in-sandbox — a resumed workflow re-runs the `aca`-backed agent from scratch
rather than continuing an interrupted sandbox session.

There is a **known transport limit**: a single streaming request was
measured to be cut off at ~30 minutes (~1801s) wall-clock on default,
non-premium ACA ingress (Phase 0 spike, issue #312). This is comfortably
above the expected length of a single agent turn, but a turn that runs
longer will still hit the cap — plan `max_session_seconds` accordingly.

### Known Gaps (Runner MVP)

The host-side `AcaRuntimeProvider` (epic E3) implements real interrupt
signaling and declares `max_session_seconds` support, but the shipped
`conductor-agent-runner` (epic E4 MVP) does not fully back either one
yet:

- **No `/interrupt` endpoint.** The host's in-stream interrupt (Esc /
  Ctrl+G, or a dashboard Stop) POSTs to `<pool>/interrupt`, but the
  runner doesn't implement that route, so the POST itself fails and the
  host falls back to a best-effort `DELETE {endpoint}/session`
  (session-deletion) call — which ACA documents as unsupported for
  custom-container pools, so it's expected to fail too. Either way, the
  host then gives up waiting on the stream and reports the turn
  `partial`, but this handoff is **not immediate** — both cleanup calls
  (the interrupt POST and the session-delete fallback) use an explicit
  10-second per-call timeout rather than returning instantly. In practice, stopping an
  `aca`-backed agent today eventually stops the *host* from waiting on
  the stream but does **not** stop the sandbox from continuing to run
  the turn server-side.
- **No dedicated runner-level `max_session_seconds` guard.** The
  declared value is forwarded straight into the wrapped
  `CopilotProvider`'s own `IdleRecoveryConfig` wall-clock enforcement —
  a **best-effort, Copilot-internal timeout**, not a runner-enforced
  guarantee that the remote sandbox call actually terminates. There is
  no independent runner-level timeout watching the `/execute` request
  as a backstop if that inner enforcement doesn't fire (or is bypassed).

Both are tracked as follow-up work on the runner image, not the host
transport. Until they land, plan conservative `max_session_seconds`
values and treat a stopped `aca` workflow as "the host stopped waiting,"
not "the sandbox stopped computing."

See [Experimental Providers](./experimental.md) for the general carve-out
policy and promotion criteria.

## Cost Note

Sandbox time is surfaced as a **distinct usage row** (`"<agent>
(sandbox)"`, cost `None`), separate from token cost — mirroring how the
[Validator](../workflow-syntax.md#validator) feature records a
`"<agent> (validator)"` row. **This is a visibility proxy, not a billing
figure**: ACA custom-container pools bill by Dedicated node capacity
(currently E16-class) plus the idle warm pool
(`readySessionInstances`), not per session-second. Use the sandbox row to
understand how much wall-clock time your workflow spends in the sandbox,
and right-size `readySessionInstances` / per-session CPU/memory against
your actual Azure bill separately.

## Security

Because a model-driven shell inside the session can read any environment
variable or file there, `aca`'s credential model (DD4) accepts that the
forwarded credential *does* enter the sandbox and defends via **scope and
lifetime** instead of trying to keep it out entirely:

- **Default (recommended): a fine-grained *Copilot Requests* PAT**
  (`COPILOT_GITHUB_TOKEN`). It can spend nothing but your Copilot quota,
  and a short expiry plus central revocability bounds a leak — this is
  what makes the default path acceptable for **trusted** workloads
  (workflows and repos you control).
- **Fallback: BYOK custom routing** (`COPILOT_PROVIDER_BASE_URL` +
  `COPILOT_PROVIDER_API_KEY` / `COPILOT_PROVIDER_BEARER_TOKEN`) — same
  posture applies; use a scoped, short-lived credential for the custom
  endpoint, not a long-lived master key.
- **Never** bake a long-lived `GITHUB_TOKEN` / `ANTHROPIC_API_KEY` as a
  pool secret or image environment variable — this is the named
  anti-pattern and exposes the whole pool indefinitely. The credential is
  always delivered in-memory, per request, never as a persisted sandbox
  environment variable.
- This posture is **trusted-use only**. ACA offers no per-session secret
  isolation and no per-destination egress allowlist, so it is not safe
  for untrusted or multi-tenant workloads. Keeping the credential
  entirely off the sandbox (a host-side broker/relay) is future work.

See the design's [Security Considerations](../projects/aca/aca-provider.design.md#security-considerations)
section for the full threat model.

## Troubleshooting

### `aca provider requires the azure-identity package`

Install the extra: `pip install 'conductor-cli[aca]'` (or `uv add
'conductor-cli[aca]'`).

### `'pool_endpoint' is required when name='aca'`

Set `runtime.provider.pool_endpoint` in your workflow YAML — there is no
default; Conductor does not provision or discover a pool for you.

### `'pool_endpoint' must use https://`

`pool_endpoint` was set to a plain `http://` (or other non-`https`) URL.
AAD bearer tokens and forwarded provider credentials
(`inner_provider_settings`) are sent to this endpoint on every request, so
`conductor validate` rejects anything but `https://`. Use the endpoint
printed by `scripts/aca/provision-pool.sh` (or `az containerapp
sessionpool show`) verbatim — ACA dynamic-sessions management endpoints
are always `https://`.

### `'pool_endpoint' must include a hostname` / `must not include a query string or fragment`

`pool_endpoint` is a **base** URL: the runner transport appends
`/execute`, `/session`, `/interrupt`, and `/health` paths plus
`identifier` / `api-version` query params to it (see [NDJSON Event Frame
Schema](#ndjson-event-frame-schema)). A bare scheme with no host
(`https://`) or a URL that already carries a `?query` / `#fragment`
produces a malformed request URL, so `conductor validate` rejects both.
Set it to the pool's management endpoint alone, e.g.
`https://my-agent-pool.<region>.azurecontainerapps.io`.

### `there is no way to disable tools for this provider`

An `aca`-backed agent declared an explicit `tools:` value (including
`tools: []`). The runner forwards every configured `mcp_servers` entry to
the in-container `CopilotProvider` unconditionally and never applies a
per-agent allowlist (`workflow_tools_passthrough=False`), so there is no
list — empty or not — it can currently honor. Remove the agent's
`tools:` key (it will run with the provider's default tool preset) or
remove the workflow's `mcp_servers:` entirely if you want no tools
available.

### Requests failing with a 401/403

Confirm the identity `DefaultAzureCredential` resolves (`az login`, or the
appropriate managed-identity / service-principal environment variables) has
been granted the *Session Executor* role on the pool.

### A declared MCP server / tool isn't available in the sandbox

Stdio MCP binaries must be baked into the runner image (or an image
`FROM`-extending it) — Conductor cannot provision them into an existing
remote pool. See
[Building / Extending the Runner Image](#building--extending-the-runner-image).

### Every agent turn fails inside the sandbox (no pool/auth error)

Confirm the **inner** Copilot credential is set — `COPILOT_GITHUB_TOKEN`
(or `GH_TOKEN`/`GITHUB_TOKEN`), or the BYOK fallback
(`COPILOT_PROVIDER_BASE_URL` + `COPILOT_PROVIDER_API_KEY`/
`COPILOT_PROVIDER_BEARER_TOKEN`) — on the host, not just the pool's
`az login`. These are two separate auth layers; see
[Inner Copilot Authentication](#inner-copilot-authentication). Without
one of these, the in-container `CopilotProvider` has no credential and
cannot construct a session.

### Stopping the workflow doesn't stop the sandbox from running

Expected for the MVP runner: interrupting an `aca`-backed agent eventually
stops the host from waiting on the result — but not instantly; cleanup uses
an explicit 10-second per-call timeout (for both the interrupt POST and the
session-delete fallback, overriding the client's longer connect/write/pool
defaults) since the shipped runner image has no `/interrupt` endpoint yet and its
session-deletion fallback is documented as unsupported for custom-container
pools. Either way, the remote sandbox call itself keeps running until it
finishes naturally or Copilot's own best-effort, in-container
`max_session_seconds` timeout catches it — there is no runner-side guarantee
of remote termination. See [Known Gaps](#known-gaps-runner-mvp).

### `working_dir` (or `sandbox.working_dir`) fails with a not-found error

The directory must already exist in the container when the session
starts — Conductor never falls back silently. Point `working_dir` at a
directory baked into the runner image (e.g. `/workspace`) and have the
agent create any subdirectory itself (e.g. `git clone <url> repo`) on
first run, rather than setting `working_dir` to that not-yet-created
subdirectory. See [`examples/aca-coding-agent.yaml`](../../examples/aca-coding-agent.yaml).

## See Also

- [`docs/projects/aca/aca-provider.design.md`](../projects/aca/aca-provider.design.md) — full solution design
- [Experimental Providers](./experimental.md) — stability policy and promotion criteria
- [`examples/aca-coding-agent.yaml`](../../examples/aca-coding-agent.yaml) — runnable example
- [`docker/aca-runner/Dockerfile`](../../docker/aca-runner/Dockerfile) — official runner image
- [`scripts/aca/provision-pool.sh`](../../scripts/aca/provision-pool.sh) — pool provisioning example
- [Workflow Syntax](../workflow-syntax.md) — `sandbox:` block reference
