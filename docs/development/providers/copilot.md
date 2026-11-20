# Copilot provider

Deep notes for `src/conductor/providers/copilot.py`: custom model routing, connecting to an existing
runtime, dead-runtime recovery, usage and pricing, and per-agent tool allowlists. Read
[providers](../providers.md) first for the shared contract.

## When to read

- Editing `src/conductor/providers/copilot.py`.
- Editing `ProviderSettings` routing or runtime fields in `src/conductor/config/schema.py`.
- Changing how `tools:` resolves for Copilot agents, or how MCP tool names are formed.
- Touching Copilot usage, pricing, or telemetry wiring.

## Required invariants

### Custom routing (`runtime.provider` object form)

- Custom routing activates only when at least one non-`name` field is set in YAML
  (`ProviderSettings.has_custom_routing` in `src/conductor/config/schema.py`). Ambient `OPENAI_*`
  variables MUST never divert a bare `provider: copilot`.
- Once active, `CopilotProvider._resolve_sdk_provider_config` fills missing fields from env:
  `base_url` from `COPILOT_PROVIDER_BASE_URL` then `OPENAI_BASE_URL`; `api_key` only from
  `COPILOT_PROVIDER_API_KEY`; `bearer_token` only from `COPILOT_PROVIDER_BEARER_TOKEN`. Ambient
  `OPENAI_API_KEY` MUST NOT be a fallback. It raises `ProviderError` when routing is active but
  every resolved field is empty.
- `api_key`, `bearer_token`, `runtime_token` are `SecretStr`; `ProviderSettings` is frozen.
- With `resolve_environment=False`, only explicit settings are used: neither custom routing
  fields nor runtime URL/token fall back to environment variables. The runner uses this mode
  for host-effective settings, including missing fields.
- The schema rejects combinations that would silently no-op at the SDK boundary: `type`, `wire_api`,
  `headers`, or `azure` without `base_url`/`api_key`/`bearer_token`; empty `headers`; empty or
  whitespace-only secrets; `azure` without `api_version` or without `type: azure`
  (`ProviderSettings._check_field_compatibility`).
- The same routing applies to agent sessions, resumed sessions, and dialog turns
  (`_apply_provider_config` is called on all three), so every session in a run hits one endpoint.
- `--provider <name>` replaces the whole `ProviderSettings` and warns when structured config is
  dropped: `src/conductor/cli/run.py::_apply_provider_override`.
- Tests: `tests/test_providers/test_copilot_provider_routing.py`.

### SDK 2.x boundary

- The dependency is `microsoft-copilot-sdk>=2.0.1,<3` (renamed from `github-copilot-sdk`, still
  imported as `copilot`). SDK 2.x negotiates only SDK protocol 4 (Copilot CLI 1.0.113 and later);
  1.x negotiates only protocol 3, and the two distributions share the `copilot` package, so one
  install cannot speak both. A runtime on the other protocol fails `client.start()`;
  `_start_sdk_client` turns that into a non-retryable `ProviderError` naming the CLI to update.
- Every client is built with an explicit `mode="coding-agent"` (`_COPILOT_CLIENT_MODE`), the
  renamed 1.x default, so agents keep the CLI's built-in tools unless `tools:` narrows them.
- The owned runtime is always `RuntimeConnection.for_spawned_stdio()`, never the SDK 2.x default
  (in-process FFI). Dead-runtime recovery, managed workspaces and crash isolation need the child
  process, and the child inherits the environment (`COPILOT_CLI_PATH`, CAPI and integration-id
  variables). Do not let `COPILOT_SDK_DEFAULT_CONNECTION` or a missing connection select
  in-process hosting.
- Session options are built as one flat mapping (create, resume, dialog, and the
  `_ManagedCopilotSession.resume_kwargs` a managed continuation stores) and translated to SDK 2.x
  nested groups only at the call by `_sdk_session_options`. A new session option needs an entry
  there; an unknown key raises instead of being dropped. Custom routing (`provider`) becomes one
  named BYOK connection (`_BYOK_PROVIDER_NAME`) with `github.capi.enabled: False`, and
  `_apply_provider_config` adds `byok_model`: the session model, registered on that connection
  under its own name and selected as `conductor/<name>`. Unregistered, SDK 2.x would read a
  slash-containing name such as `org/model` as a provider prefix and `auto` as Copilot routing;
  registered, every name reaches the endpoint unchanged as with the 1.x singular `provider`.
  Resume (checkpoint and managed continuation) must re-send the registration or the persisted
  selection fails to resolve. `assistant.usage.model` reports the endpoint name, not
  `conductor/<name>`, so usage attribution and `cost.pricing` keys are unaffected.
- Tests: `tests/test_providers/test_copilot_sdk_options.py`.

### Existing runtime connection

- `runtime_url` (optional `runtime_token`) makes `_build_client` attach with
  `RuntimeConnection.for_tcp(host, port, ...)` instead of spawning a nested `copilot`.
  `_parse_runtime_url` keeps the forms SDK 1.x's `for_uri` accepted (`port`, `host:port`,
  `[ipv6]:port`, `http://host:port`) and refuses `https://`. The SDK does not spawn for attached
  connections and its `stop()` leaves the external server running.
- `_resolve_runtime_connection` reads YAML first, then `COPILOT_PROVIDER_RUNTIME_URL` /
  `COPILOT_PROVIDER_RUNTIME_TOKEN`; the env vars activate the connection on their own (zero-YAML
  path for orchestrators).
- `has_external_runtime()` is a separate axis from `has_custom_routing()`; both may be active at
  once. `has_structured_config()` counts either, so neither collapses to bare-string serialization.
- Schema rejects `runtime_token` without `runtime_url`, empty/whitespace values, and both fields on
  any provider other than `copilot`.
- Retained local calls require an owned native stdio runtime. `_managed_call` refuses an
  external `runtime_url`, including the ambient `COPILOT_PROVIDER_RUNTIME_URL`, and an injected
  external client. Keep this guard scoped to managed calls; the unmanaged external runtime path
  remains supported. Each managed call registers its process scope and uses targeted workspace
  release as described in the [local retained workspace design](../../design/local-retained-workspace.md).

### Dead spawned runtime recovery (#483)

- `_ensure_client_started` runs under `_start_lock`. When the client is already started it checks
  `_runtime_is_dead()` and, if the spawned child exited, `_restart_spawned_runtime()` rebuilds the
  client before returning, so the next SDK call lands on a fresh runtime.
- The rebuild MUST set `_client = None` and `_started = False` before building and starting the
  replacement, so a failed spawn (for example OOM) never leaves a never-started client marked
  started.
- `_MAX_CONSECUTIVE_RUNTIME_RESTARTS` (fixed, not configurable) caps restarts with no intervening
  successful agent call; the counter resets on a successful execute call and on `close()`. Auxiliary
  callers of `_ensure_client_started` can increment it without resetting it. Exceeding the cap
  raises a non-retryable `ProviderError`.
- An externally owned runtime (`runtime_url`) is never respawned: `_spawned_runtime_process` returns
  `None` for it.
- `_spawned_runtime_process` reads the SDK's `_cli_process` (the spawned child; `None` for attached
  TCP and in-process FFI). `_fix_pipe_blocking_mode` reads `_process` (the transport: a socket
  wrapper in TCP mode, an FFI adapter with its own `poll()`). Do not unify them onto `_process`; FFI
  mode would then look like a killable child. A missing `_cli_process` on a started spawned client
  warns once.
- Tests: `tests/test_providers/test_copilot_runtime_recovery.py`.

### Per-agent tools allowlist (#585)

- Copilot declares `workflow_tools_passthrough=True` and MUST forward the resolved list as
  `available_tools` on both `create_session` and `resume_session`. Both use one value computed by
  `src/conductor/providers/copilot.py::copilot_tool_allowlist`, so the two paths cannot drift.
- Semantics: an explicit agent list, including `tools: []`, is enforced; an omitted agent `tools:`
  inherits a non-empty workflow list; an omitted agent list with an omitted or empty workflow list
  returns `None` and keeps the SDK default catalog. The user table is in [workflow
  syntax](../../workflow-syntax.md#agent-level-tools).
- MCP tool names: Conductor names them `server__tool`, the Copilot runtime registers `server-tool`.
  A name whose prefix is an attached MCP server (workflow or plugin `extra_mcp_servers`) is sent as
  `mcp:server-tool`; anything else (built-ins) passes through. Servers match longest-first so a
  server name containing `__` still resolves. Forwarding untranslated names hides the allowed tool.
- Tests: `tests/test_providers/test_copilot_tools_allowlist.py`.

### Routing preferences: Fusion and Auto tiers (#637)

- `CapiSessionOptions` (sent as `github.capi`) carries `fusion_tier`; SDKs that predate it drop
  unknown `capi` keys silently, so the SDK floor is the guard.
- One resolver, `src/conductor/providers/copilot_routing.py::resolve_routing_preference`, owns the
  precedence: an agent `fusion_tier`/`auto_tier` replaces both runtime defaults as a unit and must
  suit the model (`fusion_tier` needs `fusion` or `auto`, `auto_tier` needs `auto`); a default applies
  only to a matching routed model. The schema, static validator, executor, Copilot and `aca`
  providers, and the runner use it or its compatibility check; do not re-derive the rules.
- `CopilotProvider._routing_preference` sends the result as `capi={"fusion_tier": ...}` or
  `capi={"auto_tier": ...}` on create, resume and dialog sessions, and omits `capi` when nothing
  resolves so runtime defaults and persisted resume tiers survive. Managed continuations resume with
  the stored session kwargs, so they carry the same `capi`. A failed resume falls back to create with
  the same `capi`; a preference is never dropped to make a call succeed.
- BYOK custom routing (`has_custom_routing()`) refuses any resolved preference. `runtime_url` is a
  transport, not SDK remote/cloud sessions, and does not refuse.
- The Fusion-to-Auto tier mapping is runtime-owned; never hard-code HydraFusion routes. Deprecated
  HydraFusion model and tier identifiers pass through unchanged.

### Usage and pricing

- `assistant.usage` fires once per API call; totals accumulate across calls. A repeated event is
  deduplicated by `api_call_id`, falling back to `provider_call_id`, then `service_request_id` (all
  optional in the SDK schema; with none present the event counts as a new call).
- Parse-recovery calls add to all four billing counters, while `last_call_input_tokens` is replaced
  by the latest call's value, not summed.
- `CopilotProvider.get_model_pricing` derives USD from the SDK's `billing.token_prices` (AI Credits,
  `_COPILOT_USD_PER_CREDIT`, an observed 100 credits = $1). It MUST never raise; a missing or
  malformed price returns `None` so cost falls back to the static table.
- Composite turns (`SDKResponse.composite`: a requested Fusion tier, any `session.fusion_*` or
  `assistant.fusion_*` event, or `assistant.usage` from more than one model) keep the configured
  selector as `AgentOutput.model` (`_attributed_model`). Their aggregate tokens MUST NOT be priced at
  the last constituent's rate, so routing selectors (`auto`, `fusion`, HydraFusion) get no provider
  price or context window; only a workflow `cost.pricing` entry prices them. Plain Auto reports the
  concrete model when usage names exactly one. Parse-recovery calls merge their model history into
  the same decision. Follow-ups account only for their own calls, while `_fusion_session_ids`
  retains the session's Fusion preference until a replacement is confirmed. Fusion
  attribution uses the configured selector (`fusion` or `auto`); a `cost.pricing` override must
  use that selector as its key.
- `session.fusion_completed` aggregate counts are a fallback used only when a turn produced no
  `assistant.usage`; they cover the same calls, so adding them would double-count. Never convert
  `total_nano_aiu` into a cost. A `degraded_reason` or `session.fusion_route_failed` logs a warning.

### Other Copilot-specific rules

- Structured output: explicit in-session recovery loop with `max_parse_recovery_attempts` (default
  5); see the shared rules in [providers](../providers.md).
- `runtime.tool_output` maps to the SDK `large_output` option (`_large_output_config`): `max_chars`
  becomes `max_size_bytes` 1:1, `enabled: false` and `spill_to_file: false` both send `{"enabled":
  False}` (the latter warns), and `output_directory` is set only for an explicit `spill_dir`. No
  `agent_tool_output_truncated` event is possible here.
- Native OTel spans: the client is built with telemetry only for a spawned runtime under HTTP OTLP
  (`_build_client_telemetry`); gRPC warns once per run.
- Reasoning effort is validated per model against `supported_reasoning_efforts`
  (`_validate_reasoning_effort_for_model`).

## Change checklist

- [ ] New routing field: added to `_check_field_compatibility`, `has_custom_routing` /
      `has_structured_config`, `_resolve_sdk_provider_config`, and the configuration doc table.
- [ ] Session kwargs change applied to create, resume, and dialog paths, and mapped to its SDK
      option group in `_sdk_session_options`.
- [ ] Anything touching `_client`/`_started` keeps the invalidate-before-rebuild order.
- [ ] Allowlist change covered in `tests/test_providers/test_copilot_tools_allowlist.py` for both
      create and resume paths.
- [ ] Usage change keeps dedup and the single-call `last_call_input_tokens` rule.
- [ ] Routing change keeps create, resume, dialog and continuation parity and is covered in
      `tests/test_providers/test_copilot_routing.py` (kwargs) and
      `tests/test_providers/test_copilot_managed_workspace.py` (real-SDK camelCase wire).

## Rationale and evidence

**No ambient key fallback.** Sending a personal `OPENAI_API_KEY` to whatever `base_url` points at is
a credential leak; users opt in by setting `api_key` explicitly, typically to an
environment-variable reference.

**Separate runtime axis.** Runtime transport and per-session model routing are independent, so an
orchestrator can share one authenticated `copilot --headless` while still routing models.

**Restart cap (#483).** A runtime that keeps dying before any success (for example persistent OOM)
must fail fast instead of looping; success resets the counter, so long runs that legitimately
restart are unaffected.

**Allowlist (#585).** Before #585 the resolved list never reached the SDK, so every agent, including
`tools: []` reviewers, received the CLI's full catalog (shell, files, web, all MCP servers).

**Dedup.** Without accumulation a multi-turn agent billed only its final call; without dedup a
repeated event double-billed.

## Related references

- [Configuration: custom
  routing](../../configuration.md#custom-provider-routing-ollama--vllm--azure-openai)
- [Configuration: existing
  runtime](../../configuration.md#connecting-to-an-existing-copilot-runtime)
- [Workflow syntax: agent-level tools](../../workflow-syntax.md#agent-level-tools)
- [Telemetry](../../telemetry.md), [MCP tools](../../mcp-tools.md#tool-output-limits)
- [Configuration: Copilot Fusion and Auto
  routing](../../configuration.md#copilot-fusion-and-auto-routing)
- Examples: `examples/copilot-local-llm.yaml`, `examples/copilot-existing-runtime.yaml`,
  `examples/copilot-fusion.yaml`
- [Providers (shared contract)](../providers.md)
