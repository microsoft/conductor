# Runner Wire Protocol

Status: **Implemented**

## Summary

`conductor.runner.protocol` is the backend-neutral, serialized wire contract
between the Conductor CLI host and a **remote agent runtime** — a process
(possibly on another machine) that executes one agent turn on the host's
behalf and streams back events. `conductor-agent-runner` (the image built
from `docker/aca-runner/Dockerfile`) is the reference remote runtime.

Both sides of the wire install the one `conductor` package, but they deploy
independently: the runner image is pinned by git SHA, so host and runner
frequently run different builds. The contract is designed for that skew in
both directions.

## Boundary with `conductor.execution`

Conductor has two distinct "backend" seams, and the boundary matters:

- **`conductor.execution`** is the *in-process* Python backend seam:
  stdlib-only, frozen dataclass contracts, used when the agent runtime lives
  in the same interpreter as the workflow engine.
- **`conductor.runner.protocol`** is the *serialized* wire contract for a
  *remote* runtime: Pydantic models defining the exact JSON bodies and NDJSON
  frame shapes exchanged over HTTP.

Nothing in `conductor.runner` executes anything; it only describes what
travels between processes.

## Message catalogue

### `RunnerAgentRequest` (POST `{endpoint}/execute`)

The single streaming request body. Strict `extra="forbid"` — a sender cannot
smuggle undeclared fields past receiver validation.

- `agent: RunnerAgentPayload` — the subset of `AgentDef` the runner needs to
  reconstruct the inner agent: `name`, `model`, `system_prompt`, `output`,
  `max_agent_iterations`, `max_session_seconds`, `reasoning_effort`,
  `working_dir`, `retry`, `context_tier`. Routing, dependencies, and
  validator configuration stay host-side.
- `rendered_prompt: str` — the fully rendered prompt.
- `tools: list[str] | None` — per-agent tool allowlist (`None` = all, `[]` =
  none).
- `mcp_servers: dict | None` — full `runtime.mcp_servers` definitions.
- `context: dict` — accumulated workflow context.
- `inner_provider: str` — the SDK the runner drives (`"copilot"` today).
- `inner_provider_settings: dict | None` — the credential the host forwards
  per call (BYOK settings or a `github_token`).
- `tool_output: dict | None` — `runtime.tool_output` limits for the inner
  provider.

#### Redaction discipline

Every credential value in `inner_provider_settings` (`api_key`,
`bearer_token`, `github_token`) is a `SecretStr`, enforced by the
`_redact_inner_provider_secrets` field validator on **every validated
construction path** (`__init__`, `model_validate`, `model_validate_json`).
`model_dump()`/`repr()` therefore redact secrets everywhere.

Plaintext exists in exactly one place: `request_to_wire_body()`, the
dedicated wire-serialization step that unwraps the `SecretStr` values
immediately before the request bytes leave the process.

### NDJSON event frames (response stream)

The response is an `application/x-ndjson` stream: one JSON object per line,
each of the shape `{"type": <str>, "data": <object>}` — documented by
`RunnerEventFrame`. Event types reuse Conductor's own event vocabulary
(`agent_turn_start`, `agent_message`, `agent_tool_start`, ...) so the host
relays each `(type, data)` pair verbatim to its `event_callback` with no
translation. The model is a documenting shape: the host parses frames with
`json.loads`, the runner serializes them with `json.dumps`.

### Terminal frames

- `result` — payload documented by `RunnerAgentResult` (`extra="ignore"`):
  `content`, `model`, token counters (`input_tokens`, `output_tokens`,
  `cache_read_tokens`, `cache_write_tokens`, `last_call_input_tokens`),
  `session_seconds`, `partial`. Parsed into `AgentOutput` host-side.
- `error` — payload documented by `RunnerErrorData` (`extra="ignore"`): a
  neutral `{"message": ...}` shape. A backend-specific host adapter may
  extend it with that platform's diagnostic identifiers (an error `code` /
  `traceId` from a cloud front-end, for example); the base model's
  `extra="ignore"` keeps it parseable against such richer bodies.

### `/health` and version advertisement

`GET /health` (deliberately unauthenticated — the runner image's own
`HEALTHCHECK` sends no token) returns `RunnerHealthResponse`:

- `ready: bool`
- `conductor_version`, `runner_version: str | None`
- `protocol_version: int | None` — the runner's `RUNNER_PROTOCOL_VERSION`
- `auth_required`, `auth_token_present: bool | None` — posture diagnostics so
  the host can warn when a gateway silently strips the transport-token header

**Compatibility warning:** when the host observes a `protocol_version` that
differs from its own `RUNNER_PROTOCOL_VERSION`, it keeps working and logs a
warning only. A missing `protocol_version` (`None`, from a runner that
predates version advertisement) skips the check entirely.

### Transport-token header

`RUNNER_TOKEN_HEADER` (`X-Conductor-Runner-Token`) is the one definition of
the transport-token header name, shared by host and runner so the two sides
of the token gate can never drift apart. It is checked on `/execute` only;
`/health` stays unauthenticated.

## Evolution rules

- **Additive-only.** New fields may be added to response-side payloads; old
  receivers ignore them.
- Response-side models (`RunnerAgentResult`, `RunnerErrorData`,
  `RunnerEventFrame`, `RunnerHealthResponse`) use `extra="ignore"` for
  forward/backward compatibility.
- Request-side models (`RunnerAgentPayload`, `RunnerAgentRequest`) are strict
  `extra="forbid"`.
- **Bump `RUNNER_PROTOCOL_VERSION` only on an incompatible change** — a
  removed or retyped field, never an addition. A version mismatch is a
  compatibility warning (warn-only), never a hard failure.

## Deprecation

`conductor.providers.aca_protocol` is now a deprecated re-export shim over
`conductor.runner.protocol`. Importing it emits a `DeprecationWarning`, and the
module will be removed in a future major release.

Legacy name mapping:

- `AcaAgentPayload` re-exports `RunnerAgentPayload`
- `AcaExecuteRequest` re-exports `RunnerAgentRequest`
- `AcaEventFrame` re-exports `RunnerEventFrame`
- `AcaResultData` re-exports `RunnerAgentResult`
- `AcaErrorData` re-exports `AcaGatewayErrorData` (the ACA error adapter subclass
  defined in `conductor.providers.aca`, while the backend-neutral base is
  `RunnerErrorData`)
- `RUNNER_TOKEN_HEADER` is re-exported unchanged

Importing the legacy shim pulls in `conductor.providers.aca` due to the
`AcaGatewayErrorData` binding, adding import overhead. In-tree modules and runner
images import only `conductor.runner.protocol`. All external callers should
update their imports to `conductor.runner.protocol`.
