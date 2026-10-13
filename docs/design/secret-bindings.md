# Secret Bindings Contract

Status: **Implemented**
Related: Architecture step 5 of #527 (Secret bindings and execution environments)

## Summary

This design introduces a formal, declarative secret binding contract for Conductor. Secret bindings decouple workflow-level secret requirements from machine-level credential storage. Workflow authors declare logical secret use-sites on executable steps and MCP servers, while execution environment documents bind those logical names to concrete credential sources (such as environment variables) and enforce consumer-class access policies. Conductor resolves secrets at runtime, injects them securely via targeted mechanisms (environment variables or HTTP headers), and redacts secret values across event streams, checkpoints, logs, and telemetry.

## Motivation and Purpose

Before this feature, workflows relied on ad-hoc ambient environment variable references (such as `${VAR}` expansion in MCP configurations) or inherited process-wide environment variables. This approach had several limitations:

- **Tight coupling**: Workflows were bound to specific environment variable names present on a developer's machine or in CI.
- **Ambient leakage**: Subprocesses and scripts inherited arbitrary environment variables from the host control process.
- **No access controls**: Any step could read any ambient credential present in the environment.
- **Scattered redaction**: Sensitive tokens lacked unified run-scoped redaction across logs, events, checkpoints, and diagnostics.
- **Lack of auditability**: Run manifests could not verify or audit which secrets a workflow step consumed.

The secret bindings contract addresses these problems by providing an explicit, validated boundary between workflow author intent and operator configuration. The boundary is about *declared* consumption: ambient inheritance stays the local default (see "Binding Authorization vs. Process Isolation" below), and the contract's guarantees are typed resolution, targeted delivery, audit, and sink redaction — not sandboxing of trusted local workflow code.

## Trust Model

The security model separates roles and responsibilities:

1. **Workflow Author**: Defines what logical secrets a step needs (`ref: "api_token"`, `scope: "script"`, `delivery: {env: "API_TOKEN"}`). The author cannot bind credentials directly to host sources.
2. **Environment Operator**: Controls the execution environment document (`.conductor/environments/<name>.yaml`). The operator maps logical secret references to actual sources (for example, host environment variables) and enforces consumer-class access restrictions via `allow` lists.
3. **Consumer Code**: Scripts, tools, and MCP servers receive delivered secrets. Any credential delivered to an LLM agent, script, or MCP server is considered accessible to that execution realm. Authors and operators must grant only the credentials required for that specific step.

### Binding Authorization vs. Process Isolation

The `allow` list governs typed resolution only: it decides which consumer *classes* may receive a delivered value through a binding. It is not process isolation. Local runner profiles inherit the control process's full `os.environ` by default (`inherit_control_environment` effective `true`), so a script step whose binding says `allow: [mcp]` — or even `allow: []` — can still read the source variable from its inherited environment if it knows the variable name. Operators who need that boundary set `inherit_control_environment: false` on the profile, which runs steps on a minimal environment plus the declared deliveries. Local workflow code remains trusted input regardless: it can always echo, transform, or exfiltrate any credential delivered to it, and sink redaction degrades such echoes to a marker only at Conductor-controlled sinks, never inside the consumer's own process.

## Grammar and Syntax

### Environment Document Bindings

Environment documents declare secret bindings under the top-level `secrets` mapping:

```yaml
default: shell

profiles:
  shell:
    backend: local
    inherit_control_environment: true

secrets:
  api_token:
    source:
      env: PRODUCTION_API_TOKEN
    allow: [script, mcp]

  database_key:
    source:
      env: DB_SECRET_KEY
    # allow omitted: permits all consumer classes forever

  quarantined_token:
    source:
      env: STALE_TOKEN
    allow: [] # fail-closed: permitted for no consumer classes
```

- **Secret Names**: Must match `[A-Za-z0-9_.-]+`.
- **Source Configuration**: `source` specifies the secret provider. In v1, the only supported source kind is `env`, which takes a valid shell identifier (`[A-Za-z_][A-Za-z0-9_]*`).
- **Access Policy (`allow`)**:
  - `None` (omitted): Allowed for all consumer classes now and in future extensions.
  - `[]` (empty list): Explicitly fail-closed. Denied for all consumers.
  - `["script"]` / `["mcp"]`: Restricted to the named consumer classes.

### Workflow Use-Sites

Workflows consume secrets in two locations:

#### 1. Executable Steps (`execution.secrets`)

Script steps declare secrets under `execution.secrets`:

```yaml
agents:
  - name: run_build
    type: script
    execution:
      secrets:
        - ref: api_token
          scope: script
          delivery:
            env: API_KEY
    command: python
    args: ["scripts/build.py"]
```

#### 2. Workflow MCP Servers (`runtime.mcp_servers.<name>.secrets`)

Declared MCP servers configure secrets directly:

```yaml
workflow:
  runtime:
    mcp_servers:
      remote_service:
        type: http
        url: https://api.example.com/mcp
        secrets:
          - ref: api_token
            scope: mcp
            delivery:
              header: Authorization
```

### Delivery Mechanisms and Scope Rules

- **Delivery Targets**: `SecretDelivery` supports `env` (valid shell identifier) or `header` (RFC 9110 token charset). Exactly one target must be specified.
- **Scope Dictionary**:
  - `script`: Valid on script steps. Delivery must be `env`.
  - `mcp`: Valid on MCP servers. Stdio servers require `env` delivery; HTTP/SSE servers support `env` and `header` deliveries, subject to the provider restrictions below.
  - `agent`: Allowed on remote agent steps with a declared stdio MCP server or an enabled MCP plugin that might provide one, and on stdio MCP servers with only remote agent consumers. The static check does not resolve plugin contents; if an enabled plugin ships no stdio server, the runner rejects the undeliverable overlay. `delivery.env` is delivered per call through `env_overlay` to the spawn environment of stdio MCP processes inside the realm, never to the model SDK environment. A remote agent with no possible stdio consumer (including `tools: []`) is rejected statically. Local agents inherit the control environment and do not support scoped agent delivery. The MCP child process environment remains readable to subjects authorized to inspect its `/proc/<pid>/environ`.

### Transport and Provider Restrictions

Delivery targets are bounded by the effective providers of the actual agent consumers. An agent's `provider:` override takes precedence over the workflow default; script steps and direct `type: mcp` steps are not provider consumers.

| Provider | stdio | HTTP/SSE | stdio env | remote header | remote env |
|---|---|---|---|---|---|
| Copilot | Yes | Yes | Yes | Yes | Yes |
| Claude | Yes | No (rejected before run) | Yes | n/a | n/a |
| OpenAI | Yes | No (rejected before run) | Yes | n/a | n/a |
| Claude Agent SDK | Yes | Yes | Yes | Yes | No (config shape accepts only url/headers) |
| Hermes | No MCP | No MCP | n/a | n/a | n/a |
| ACA | Forwarded to runner | Forwarded to runner | per inner-runner contract | per inner-runner contract | per inner-runner contract |

`header` delivery applies only to HTTP/SSE servers, and remote authentication should prefer `delivery.header`. A remote server used by an effective Claude or OpenAI consumer is rejected at `conductor validate` and at manifest compilation rather than silently passed through. Claude Agent SDK accepts remote headers but rejects remote environment delivery because its remote config shape carries only `url` and `headers`.

Direct `type: mcp` steps are a separate provider-independent path. They connect through the engine-owned MCP manager, support stdio only, and do not make the workflow default provider an MCP consumer.

### Collision Rules

Within a single step or MCP server, secret delivery targets share a namespace with literal configurations:

- Literal `env` and secret `env` targets cannot collide. Comparisons are case-insensitive on Windows and case-sensitive on POSIX systems.
- Literal `headers` and secret `header` targets cannot collide. Header comparisons are always case-insensitive (per RFC 9110).
- Multiple secret bindings delivering to the same variable or header name within the same consumer trigger a validation error.

## Resolver Seam and Architecture

The secrets architecture consists of several focused components:

1. **`SecretBindingSource` & `SecretBinding`** (`conductor.config.environment`): Schema models for environment document declarations.
2. **`StepSecretRef` & `SecretDelivery`** (`conductor.config.schema`): Schema models for workflow use-sites.
3. **`ResolvedSecret`** (`conductor.engine.secrets`): Holds a resolved secret value wrapped in a Pydantic `SecretStr`. Plaintext is exposed only via the `.value` property.
4. **`SecretValueCache`** (`conductor.engine.secrets`): The run-scoped resolver state. It validates bindings against the resolved environment, checks `allow` policies on every resolution (including cache hits), resolves sources from the environment, and registers values with the redactor.
5. **`SecretUseIndex` & `index_config`** (`conductor.engine.secrets`): Immutable per-config index mapping steps and servers to their secret uses. Contains references and delivery targets, but never plaintext values.
6. **`RunRedactor`** (`conductor.redaction`): Stdlib-only leaf module that maintains registered secret strings and performs recursive scrubbing over nested data structures.

### Extensibility

The `SecretBindingSource` model uses an explicit source kind pattern. While v1 exclusively supports `source.env`, the resolver seam is designed to accept future source kinds (such as Azure Key Vault, HashiCorp Vault, or macOS Keychain) without altering workflow use-site grammar or execution engine contracts.

## Lifecycle and Execution Flow

1. **Preflight & Manifest Compilation**:
   - `conductor validate` or engine startup resolves the execution environment document.
   - `compile_run_manifest` verifies all secret references, scopes, and deliveries, compiling `ResolvedSecretUse` records into the `ResolvedRunManifest`.
2. **Eager Root Indexing**:
   - Before workflow startup, `_prepare_run_secrets` creates the `SecretValueCache`, indexes root workflow references via `index_config`, and registers all resolved values into `RunRedactor`.
   - `redaction.set_current(redactor)` binds the redactor to the execution context.
3. **Child Workflow Resolution**:
   - Sub-workflows compile their own `SecretUseIndex` against the shared `SecretValueCache` at execution time.
4. **Step & MCP Delivery**:
   - `ScriptExecutor` merges secret environment variables into the subprocess command environment.
   - `resolve_mcp_server_config` injects resolved secret environment variables and HTTP headers into the MCP client configuration.
   - Remote agent-scoped uses are indexed per step and passed in `env_overlay` to in-realm stdio MCP subprocesses; MCP-server agent-scoped uses are not merged into the host MCP client configuration.
5. **Finalization & Cleanup**:
   - When execution finishes, the engine and CLI finalization blocks clear the cache and reset the active redactor context.

## Redaction Sink Inventory

Conductor scrubs registered secret values across all standard output, diagnostic, and telemetry sinks:

1. **Event Emitter** (`conductor.events`): `WorkflowEventEmitter.emit` scrubs event payloads before notifying subscribers.
2. **Checkpoint Payloads** (`conductor.engine.checkpoint`): `save_checkpoint` scrubs a deep copy of the checkpoint dictionary prior to serialization. Live memory context is not mutated.
3. **Terminal Run Records** (`conductor.fleet.records`): Completed run records (`record_output`, `record_error_type`, `record_error_message`) are scrubbed before being written to disk.
4. **Final Output Boundary** (`conductor.cli.run`): Final workflow results returned to stdout and caught `WorkflowTerminated` exceptions are scrubbed at the CLI boundary, as are the message and suggestion fields of any other exception that escapes the run or resume path before secret cleanup (the CLI error renderer runs after the redactor is cleared, so the scrub happens at the boundary).
5. **Telemetry Closure** (`conductor.telemetry`): Telemetry shutdown events and subscriber close payloads are sanitized before export.
6. **Dashboard & Event Replay**: Replayed events and seeded `workflow_started` metadata are scrubbed before client transmission.
7. **CLI Inputs Panel**: The startup input display scrubs values before rendering.
8. **MCP Diagnostics Log**: `MCPManager` sanitizes dynamic arguments, URLs, and errors written to `.mcp-diagnostics.log`.
9. **MCP Manager Log-Error Paths**: Internal exception messages, tool arguments, and cancellation details logged by `MCPManager` are scrubbed.
10. **Conductor-Managed MCP Error Text**: Tool execution errors returned to the engine have `redact_errors=True` enabled.

### What Is NOT Scrubbed (and Boundaries)

Certain surfaces are deliberately outside the automated scrubbing boundary:

- **Live stdout and context**: Modifying live strings inside the running context or subprocess pipes would corrupt data, break JSON parsing, and alter application behavior.
- **Provider-owned SDK channels**: In-memory message histories managed internally by Copilot or Anthropic SDKs are not modified.
- **Background capture logs**: `.bg.stderr.log` and `.bg.stdout.log` capture raw OS-level file descriptors for low-level crash debugging.
- **Tool-output spill files**: On-disk temporary files holding large tool outputs are written directly without secondary string replacement.
- **Historical logs**: Pre-existing log files on disk created prior to the run are not modified.
- **Transformed secrets**: Encoded (base64, URL-encoded), hashed, split, or structurally transformed secrets are not matched. Redaction performs exact character and UTF-8 byte matching.

## Inherit Policy (`inherit_control_environment`)

Environment document profiles support an optional `inherit_control_environment` setting:

- `None` (default): Backend decides. Local subprocess runners default to effective `true`; future remote/sandbox runners default to effective `false`.
- `true`: Commands executed under the profile inherit host environment variables.
- `false`: Commands run in a minimal, isolated environment without ambient host variables.

The compiled run manifest resolves and records the effective boolean value for every step profile.

## Plugin MCP Exclusion

In this release, secret binding use-sites are supported only on workflow-declared MCP servers (`runtime.mcp_servers`). Plugin-provided MCP servers (from `.mcp.json`) do not have a workflow use-site syntax and continue to use legacy `${VAR}` environment variable expansion. Bridging plugin MCP servers to secret bindings is deferred to a future update.

## ACA Provider Boundary

The experimental ACA provider (`conductor.providers.aca`) currently relies on host-side credential resolution and forwards credentials per request. Integrating ACA sandbox execution with the unified secret bindings and runner protocol will happen in architecture step 7.

## Reserved Errors and Future Compatibility

- **`scope: agent`**: Reserved for agent execution realms. Attempting to use `scope: agent` raises a clear error directing authors to wait for step 7.
- **Future Source Kinds**: `SecretBindingSource` rejects unknown fields with `extra="forbid"`, ensuring clean schema migration when new secret providers are added.
- **Header Delivery on Script Steps**: `delivery: {header: ...}` is valid only for MCP servers. Specifying header delivery on script steps raises a descriptive validation error.
