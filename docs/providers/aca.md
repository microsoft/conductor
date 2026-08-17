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

The command depends on how Conductor itself was installed. `conductor run`
and `conductor doctor` print the right one when the extra is missing, so you
can also just run your workflow and copy what it says. (`conductor validate`
does not — the provider is constructed lazily, so the guard only fires once
an `aca`-backed agent actually runs.)

```bash
# Installed via the install script (uv tool install)
uv tool install --force 'conductor-cli[aca] @ git+https://github.com/microsoft/conductor.git@v<version>'

# A source checkout
uv sync --extra aca

# A wheel from a GitHub Release or a private index
pip install 'conductor-cli[aca]'
```

`conductor-cli` is not published to PyPI, so the `pip` form resolves only
where pip can already see an installed `conductor-cli` — never inside a uv
tool venv (issue #441). The `uv tool install` form must name every
extra you want to keep — `--force` replaces the tool's entire requirement
set, so `[aca]` alone would remove an already-installed `[tui]`. Conductor's
own hint builds that list for you, and `conductor update` preserves it
across upgrades.

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
managed identity, service principal, …) with the *Session
Executor* role on the pool works — see
[Authentication](#authentication).

> **Multi-tenant / guest accounts:** plain `az login` targets the
> `organizations` endpoint and may land on a tenant where your account is
> a disabled guest, failing with `AADSTS500571`. Pass the tenant holding
> your subscription explicitly: `az login --tenant <tenant-id>`. Some
> corporate tenants additionally block the device-code flow via
> Conditional Access (`AADSTS53003`), so `--use-device-code` is not a
> workaround there — use the default interactive browser flow.

> **WSL:** the interactive flow works from WSL2 (the loopback redirect is
> forwarded from Windows), but if no browser opens, copy the printed URL
> into a Windows browser manually. Note that credentials are per-Linux-user
> — being signed in to `az` on Windows does **not** authenticate WSL.

### 3. Authenticate the inner Copilot session

The pool identity above only gets the host into the sandbox — it says
nothing about the **in-container Copilot session** the runner drives on
your behalf. That session cannot perform interactive OAuth login inside a
headless container, so the **host** resolves either a GitHub token or
BYOK routing settings and forwards them on every request (see
[Inner Copilot Authentication](#inner-copilot-authentication)).

**Default: nothing to do.** If you are signed in with the GitHub CLI,
Conductor picks that token up automatically via `gh auth token` — no
ACA-specific credential setup at all. GitHub documents the `gh` CLI's
OAuth token as a supported Copilot credential source, and no special
Copilot scope is required (entitlement is evaluated per-user seat, not via
OAuth scopes):

```bash
gh auth login   # only if you aren't already signed in
```

**To use a different credential**, export it — an explicit environment
variable always beats the ambient `gh` identity, so you can point a single
workflow at a narrower token without logging out of `gh`:

```bash
export COPILOT_GITHUB_TOKEN=<token>
```

The narrowest option, and the recommended one for CI, service accounts, or
any host where you'd rather not forward your full `gh` identity into a
sandbox, is a fine-grained GitHub personal access token with only the
***Copilot Requests* permission** (see
[Inner Copilot Authentication](#inner-copilot-authentication) for how to
create one). `GH_TOKEN` and `GITHUB_TOKEN` are also recognized, in that
priority order.

Either way the sandbox runs on **the Copilot capacity of the GitHub
account owning the resolved token** — independent of the Azure identity
from step 2 (`az login`/`DefaultAzureCredential`), which only grants
access to the pool itself.

**Fallback: BYOK custom routing.** If you need to route the sandbox at a
custom OpenAI-compatible endpoint instead (as with the host's own
[structured `runtime.provider`](../configuration.md)), export a base URL
— it, not the presence of a credential, is what activates BYOK routing:

```bash
export COPILOT_PROVIDER_BASE_URL=<your OpenAI-compatible endpoint>
export COPILOT_PROVIDER_BEARER_TOKEN=<your Copilot-compatible token>
# or: COPILOT_PROVIDER_API_KEY=<key>
```

`COPILOT_PROVIDER_BEARER_TOKEN` / `COPILOT_PROVIDER_API_KEY` alone (with
`COPILOT_PROVIDER_BASE_URL` unset) do **not** activate BYOK routing — the
provider falls through to the GitHub-token default above. Unlike the
host's own [structured `runtime.provider`](../configuration.md), `aca`
does not forward a provider `type` or `wire_api`; the runner's inner
`CopilotProvider` always treats the forwarded endpoint as
OpenAI-compatible. `COPILOT_PROVIDER_BASE_URL` (if set) always wins over
a GitHub token, so a BYOK endpoint stays authoritative even when both are
exported.

**Trusted-use posture:** whichever credential is resolved, it *does* enter
the sandbox and is readable by a model-driven shell there — ACA offers no
per-session secret isolation or per-destination egress allowlist. The
`gh auth token` default trades a little scope for a lot of convenience: a
`gh` OAuth token is broader than a *Copilot Requests*-only PAT, so when
blast radius matters more than setup cost (CI, shared hosts, anything
running untrusted-ish code), export a narrowly scoped PAT with a short
expiry instead — it can spend nothing but your Copilot quota, and a leak
stays bounded and centrally revocable. This mechanism is acceptable only
for **trusted** workloads (workflows and repos you control) — it is not
safe for untrusted or multi-tenant use. Keeping the credential entirely
off the sandbox (a host-side broker) is future work; see
[Security](#security).

If no credential can be resolved from *any* source, the failure is not a
silent no-op and surfaces **host-side, before the sandbox is ever
contacted**: the host raises `ProviderError` while building the `/execute`
request (no credential to forward), so the request is never dispatched
rather than failing inside the sandbox.

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

The script needs the `containerapp` Azure CLI extension, which it installs
for you. That install shells out to `pip`, so if you installed the Azure
CLI into an isolated environment that omits `pip` (e.g. `uv tool install
azure-cli`), it fails with `No module named pip` — reinstall including
pip (`uv tool install azure-cli --with pip`) or use a distro/installer
package.

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
host/runner version skew and by the image's own `HEALTHCHECK`. Deliberately
unauthenticated (issue #396) — the image's `HEALTHCHECK` sends no header at
all, so gating this endpoint would break it.

```json
{
  "ready": true,
  "conductor_version": "0.4.0",
  "runner_version": "0.1.0",
  "auth_required": false,
  "auth_token_present": false
}
```

- `auth_required` — whether the runner has `ACA_RUNNER_AUTH_TOKEN`
  configured (the transport-token gate on `/execute` is opt-in — see
  below).
- `auth_token_present` — whether *a* `X-Conductor-Runner-Token` header
  arrived on **this** request, never whether it matched. This is what
  actually detects a gateway that strips custom headers: the caller already
  knows whether it sent a header, so this leaks nothing and cannot be used
  as a brute-force oracle. `validate_connection()` warns when this
  disagrees with the host's own configured posture in either direction.

### `POST /execute?identifier=<id>&api-version=<v>`

Runs one agent turn and streams the result back as
`application/x-ndjson`. Optionally gated by an
`X-Conductor-Runner-Token` header (see [Security](#security)) — a
missing/incorrect header when `ACA_RUNNER_AUTH_TOKEN` is configured returns
a `401` with `{"error": {"message": "..."}}` before the inner Copilot
provider is ever constructed. Request body:

```json
{
  "agent": {
    "name": "...", "model": "...", "system_prompt": "...", "output": {...},
    "max_agent_iterations": 10, "max_session_seconds": 900,
    "reasoning_effort": "medium", "working_dir": "/workspace",
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
  design's Security Considerations. The runner rejects (`400`) any key
  outside `base_url`/`api_key`/`bearer_token`/`github_token` — the four the
  host ever sends — and, when `ACA_RUNNER_ALLOWED_BASE_URLS` is configured,
  any `base_url` not on that allowlist (issue #396).
- `identifier` (query parameter) — gateway routing metadata **only**. ACA
  routes by it, auto-allocating a session if none exists yet; it is
  deliberately never validated as a caller-authentication signal — the
  runner has no independent source of truth for which identifier it should
  be serving, and the container's own `HEALTHCHECK` sends none at all. The
  `X-Conductor-Runner-Token` header above is the actual runner-side
  authentication control.


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
resolves either a GitHub token or BYOK routing settings per request and
forwards them to the runner, with no `credential_mode` switch to
configure — precedence mirrors the Copilot CLI's own auth resolution
(design *DD4*):

1. **BYOK custom routing** — if `COPILOT_PROVIDER_BASE_URL` is set on the
   host, it always wins: the base URL plus optional
   `COPILOT_PROVIDER_API_KEY` / `COPILOT_PROVIDER_BEARER_TOKEN` are
   forwarded unchanged. `COPILOT_PROVIDER_API_KEY` / `_BEARER_TOKEN` alone,
   without `COPILOT_PROVIDER_BASE_URL`, do **not** activate this path —
   the resolver falls through to step 2. Only `base_url` and an optional
   credential are forwarded — no provider `type` or `wire_api`, so the
   runner's inner `CopilotProvider` always treats the endpoint as
   OpenAI-compatible.
2. **Explicit GitHub token** — otherwise, if a GitHub token is present in
   the environment (`COPILOT_GITHUB_TOKEN` → `GH_TOKEN` → `GITHUB_TOKEN`,
   first non-empty wins), it is forwarded as `github_token`.
   **Recommended for CI/service accounts: a fine-grained PAT scoped to
   only the *Copilot Requests* permission**, which is the narrowest
   credential that works.
3. **Default: `gh auth token`** — otherwise the host shells out to the
   GitHub CLI. This is what makes the zero-setup path work: if you are
   signed in with `gh`, nothing else is required. GitHub documents `gh`'s
   OAuth token as a supported Copilot credential source, and no special
   OAuth scope is needed — Copilot entitlement is evaluated per-user
   (seat), not via scopes. Every failure mode (`gh` not installed, not
   signed in, wedged keyring, empty output) is treated as "no token" and
   falls through to step 4 rather than raising.
4. **Nothing resolves** → the provider fails loudly with setup guidance
   rather than running the sandbox unauthenticated or silently degraded.

In cases 2 and 3 the sandbox's inner Copilot runtime authenticates against
**GitHub Copilot's own model routing**, using the Copilot capacity of
**the GitHub account that owns the forwarded token** (independent of the
Azure identity used to reach the pool).

> **Not implemented on purpose:** reading the Copilot editor plugins'
> credential store (`~/.config/github-copilot/auth.db`). That store belongs
> to the Copilot *Language Server*, not the CLI/SDK this provider drives;
> its format has already changed twice (`hosts.json` → `apps.json` →
> `auth.db`), and encryption-at-rest has shipped once before being rolled
> back — so any plaintext read is a temporary accident, not a contract.
> `gh auth token` is the supported equivalent.

**Caveats for the `gh` path.** `gh` tokens are *user* OAuth tokens, so they
are subject to SAML SSO authorization and org OAuth-app restrictions; if an
org enforces either, authorize the GitHub CLI app for it (or export an
explicit token instead). For GHEC-with-data-residency you must also
propagate `GH_HOST`/`COPILOT_GH_HOST` and select the matching host with
`gh auth token --hostname`.

```bash
# Default (Copilot capacity) — nothing to export if `gh` is signed in:
gh auth login

# Explicit override — beats the ambient `gh` identity:
export COPILOT_GITHUB_TOKEN=<fine-grained PAT, "Copilot Requests" only>

# Fallback (BYOK custom routing) — COPILOT_PROVIDER_BASE_URL is required;
# credentials alone do not activate this path:
export COPILOT_PROVIDER_BASE_URL=<url>
export COPILOT_PROVIDER_BEARER_TOKEN=<token>
# or: export COPILOT_PROVIDER_API_KEY=<key>
```

`AcaRuntimeProvider._resolve_inner_provider_settings()` implements this
precedence and forwards the result as `inner_provider_settings` on the
`/execute` request body — the resolved GitHub token or BYOK routing
settings are delivered **in-memory** (request body → the inner runtime's
`create_session`/`resume_session` call), never written to a sandbox
environment variable or persisted as a pool secret. The runner constructs
`ProviderSettings(name="copilot", **inner_provider_settings)` (BYOK case)
or passes `github_token` straight through to its inner `CopilotProvider`
(default case) instead of attempting an impossible interactive login.

**Trusted-use posture** (DD4): the credential *does* enter the sandbox
and is readable by a model-driven shell there — ACA offers no
per-session secret isolation or per-destination egress allowlist. The
defense is *scope and lifetime*, not concealment: a leaked *Copilot
Requests* PAT can only spend your Copilot quota until it expires and is
centrally revocable, which is what makes that path safe for
**trusted** workloads (workflows and repos you control) — never a
long-lived personal token or a broadly-scoped API key. The zero-setup
`gh auth token` default is the convenience end of that trade: it forwards
your full `gh` OAuth identity, which is broader than a *Copilot
Requests*-only PAT, so prefer an explicit scoped token wherever blast
radius matters. This mechanism is
not safe for untrusted or multi-tenant use; keeping the credential
entirely off the sandbox (a host-side broker/relay) is future work — see
[Security](#security). If no GitHub token can be resolved and no BYOK
endpoint is
configured, the failure happens **host-side**: `_resolve_inner_provider_settings()`
raises `ProviderError` while `_build_request()` constructs the `/execute`
body, before any request reaches the sandbox — there is no silent
degraded mode and no in-sandbox failure.

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
| `workflow_tools_passthrough` | ❌ **`False`** | The per-agent `tools:` allowlist is forwarded to the runner in the request body, but the in-container `CopilotProvider` it wraps never applies that list to the SDK session — every tool/MCP server available to the session is callable regardless of the declared allowlist. Combined with `mcp_tools=True` (below), there is no allowlist value the runner can honor — not even `tools: []` — so `conductor validate` rejects any explicit `tools:` on an `aca`-backed agent. This is a known, allowed experimental carve-out (the same gap `claude_agent_sdk` and `hermes` already declare; `hermes` declares `mcp_tools=False` so `tools: []` stays valid for it, and `claude_agent_sdk` behaves like `aca` here only when the workflow declares `mcp_servers`). |
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

- **Default (zero-setup): `gh auth token`.** If the operator is signed in
  with the GitHub CLI, that OAuth token is used automatically. It is a
  documented Copilot credential source, but it is the operator's *full*
  `gh` identity — broader than the PAT below. Fine for **trusted**
  workloads on a machine you control; prefer an explicit scoped token
  anywhere else.
- **Recommended when blast radius matters: a fine-grained *Copilot
  Requests* PAT** (`COPILOT_GITHUB_TOKEN`, which overrides the `gh`
  default). It can spend nothing but your Copilot quota,
  and a short expiry plus central revocability bounds a leak — this is
  what makes the path acceptable for CI, service accounts, and shared
  hosts.
- **Fallback: BYOK custom routing** (`COPILOT_PROVIDER_BASE_URL`, which
  alone activates it — `COPILOT_PROVIDER_API_KEY` / `COPILOT_PROVIDER_BEARER_TOKEN`
  are optional and only needed when the endpoint itself requires
  credentials, e.g. an unauthenticated local gateway needs neither). When
  a credential is supplied, use a scoped, short-lived one for the custom
  endpoint, not a long-lived master key.
- **Never** bake a long-lived `COPILOT_GITHUB_TOKEN` / `COPILOT_PROVIDER_API_KEY`
  / `COPILOT_PROVIDER_BEARER_TOKEN` as a pool secret or image environment
  variable — this is the named anti-pattern and exposes the whole pool
  indefinitely. The credential is always delivered in-memory, per
  request, never as a persisted sandbox environment variable.
- **Not implemented on purpose:** harvesting the Copilot editor plugins'
  credential store (`~/.config/github-copilot/auth.db`). Besides being an
  undocumented store owned by a different product (the Copilot Language
  Server) that has already changed format twice and is slated for
  encryption at rest, reading another tool's secret database is a
  credential-harvesting pattern Conductor deliberately avoids.
  `gh auth token` is the supported equivalent.
- This posture is **trusted-use only**. ACA offers no per-session secret
  isolation and no per-destination egress allowlist, so it is not safe
  for untrusted or multi-tenant workloads. Keeping the credential
  entirely off the sandbox (a host-side broker/relay) is future work.

See the design's [Security Considerations](../projects/aca/aca-provider.design.md#security-considerations)
section for the full threat model.

### Runner transport hardening (issue #396)

The MVP runner's posture depended entirely on the assumption that the
runner port is unreachable except through the ACA session gateway. That
boundary is now defended in depth, though every added layer is opt-in
except the two that are pure narrowing (binding loopback, the
`inner_provider_settings` key allowlist):

- **The runner port must never be reachable outside the session gateway.**
  This was always the intended posture; issue #396 hardens the runner so
  it does not depend *solely* on that boundary holding.
- **Transport credential vs. model credential.** `ACA_RUNNER_AUTH_TOKEN`
  (below) is a **transport** credential — it authenticates a caller
  reaching the runner's HTTP endpoints at all. It is a distinct concern
  from `inner_provider_settings` (the **model** credential, DD4) covered
  above. Setting `ACA_RUNNER_AUTH_TOKEN` as a pool-level environment
  variable is *not* an instance of the "never bake a long-lived secret as a
  pool secret" anti-pattern above — that bullet is about the model
  credential, which authorizes Copilot inference spend; the transport token
  only gates reachability of the runner's own HTTP surface.
- **Opt-in transport-token gate.** Set `ACA_RUNNER_AUTH_TOKEN` to the same
  value on both the runner pool and the host to require an
  `X-Conductor-Runner-Token` header on `/execute`. `GET /health` stays
  unauthenticated (the image's own `HEALTHCHECK` sends no header) but
  reports `auth_required`/`auth_token_present` so `validate_connection()`
  can detect a gateway silently stripping the header before you rely on
  the gate. Not mandatory — the runner works unchanged with this unset.
- **`inner_provider_settings` key allowlist.** The runner rejects any key
  outside `base_url`/`api_key`/`bearer_token`/`github_token` (the four the
  host ever sends), closing off a caller sending e.g. `runtime_url` or
  `headers` directly at the runner. Set `ACA_RUNNER_ALLOWED_BASE_URLS`
  (comma-separated) on the pool to additionally restrict which BYOK
  `base_url` values are accepted.
- **`identifier` is routing metadata, not authentication.** The runner has
  no independent source of truth for which identifier it should be
  serving — Azure allocates/reuses sessions from a warm pool and routes to
  the container — so `identifier` is deliberately never validated as a
  caller-authentication signal; the transport-token gate above is the
  runner-side control for that.

## Troubleshooting

### `aca provider requires the azure-identity package`

Install the `aca` extra. The error's own `suggestion` carries the exact
command for how this Conductor was installed — see
[Install the azure-identity extra](#1-install-the-azure-identity-extra) for
the three forms and why a hardcoded `pip install 'conductor-cli[aca]'`
does not work on the documented install path.

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

### `aca runner: missing or invalid runner auth token`

The runner has `ACA_RUNNER_AUTH_TOKEN` configured and rejected the
request's `X-Conductor-Runner-Token` header (missing or not matching).
Set the same `ACA_RUNNER_AUTH_TOKEN` value on both the host and the runner
pool, or unset it on the pool if you don't need the transport-token gate.

### `aca runner: unsupported inner_provider_settings key`

The request's `inner_provider_settings` carried a key outside the runner's
allowlist (`base_url`/`api_key`/`bearer_token`/`github_token`), or a
`base_url` not on the pool's configured `ACA_RUNNER_ALLOWED_BASE_URLS`. No
in-repo caller produces an unlisted key — `AcaRuntimeProvider
._resolve_inner_provider_settings` only ever returns those four — so this
means either a hand-rolled request or a stale/mismatched host and runner
version. See [Runner transport hardening](#runner-transport-hardening-issue-396).

### Runner requires an auth token but the header didn't arrive

`validate_connection()` logged a warning that the runner reports
`auth_required: true` but `auth_token_present: false` on the `/health`
probe. This means a gateway between the host and the runner is stripping
the `X-Conductor-Runner-Token` header — every `/execute` call will fail
with a 401 until that's fixed. If the header provably cannot survive the
trip, unset `ACA_RUNNER_AUTH_TOKEN` on the runner pool (the gate is opt-in;
the runner works unchanged without it).

### A declared MCP server / tool isn't available in the sandbox

Stdio MCP binaries must be baked into the runner image (or an image
`FROM`-extending it) — Conductor cannot provision them into an existing
remote pool. See
[Building / Extending the Runner Image](#building--extending-the-runner-image).

### Missing inner Copilot credential

Confirm the **inner** Copilot credential is available on the host — the
easiest fix is `gh auth login` (the CLI's token is picked up
automatically). Otherwise export `COPILOT_GITHUB_TOKEN` (or
`GH_TOKEN`/`GITHUB_TOKEN`), or the BYOK fallback
`COPILOT_PROVIDER_BASE_URL` (`COPILOT_PROVIDER_API_KEY`/
`COPILOT_PROVIDER_BEARER_TOKEN` are optional, only needed when the
endpoint itself requires a credential). Note this is separate from the
pool's `az login` — they are two independent auth layers; see
[Inner Copilot Authentication](#inner-copilot-authentication). Failure
means **no** GitHub token could be resolved from any source **and** no
BYOK base URL is set — in
that case the host raises `ProviderError` while building the
`/execute` request — before the sandbox is ever contacted, not inside
it.

If `gh` is installed and signed in but still isn't being used, check that
`gh auth token` prints a token in the same shell that runs `conductor`
(SAML SSO authorization or an org OAuth-app restriction can suppress it),
or export the token explicitly.

### `Model "<name>" is not available.`

The model named in `runtime.default_model` (or an agent's `model:`) is not
available to the Copilot account owning the forwarded credential — model
availability varies by account, plan, and enterprise policy. The failure
happens *inside* the sandbox at `session.create` time and is surfaced
host-side as a `ProviderError`.

Pick a model your account actually has. `gpt-5-mini` and
`claude-sonnet-4.5` are verified working with
[`examples/aca-coding-agent.yaml`](../../examples/aca-coding-agent.yaml);
`gpt-4.1` and `gpt-4o` are **not** available on all accounts. Prefer
setting `runtime.default_model` once rather than pinning a per-agent
`model:` you may not be able to use.

### Pool creation fails with `ImageManifestNotFound` / `MANIFEST_UNKNOWN`

The image tag contains uppercase characters. The ACA session-pool API
lowercases the image reference it is given, but OCI/Docker tags are
case-**sensitive**, so a tag pushed as `...T1816Z-...` is looked up as
`...t1816z-...` and never found — the image builds and pushes fine, then
pool creation fails minutes later.

Use an all-lowercase `IMAGE_TAG`.
[`scripts/aca/provision-pool.sh`](../../scripts/aca/provision-pool.sh)
generates a lowercase tag by default and rejects an uppercase override up
front.

### Requests failing with a 429

The pool is at its session ceiling. `provision-pool.sh` defaults to
`--max-sessions 20`; a lower value (or many concurrent
parallel/for-each units, which each get their own session) exhausts it
quickly. Raise `MAX_SESSIONS`, lower workflow concurrency, or wait for
sessions to hit their cooldown. Note a 429 comes from the pool's front
end, so the runner may never have been reached — the error message
includes the raw response body to make that distinguishable.

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
