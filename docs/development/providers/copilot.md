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
- The schema rejects combinations that would silently no-op at the SDK boundary: `type`, `wire_api`,
  `headers`, or `azure` without `base_url`/`api_key`/`bearer_token`; empty `headers`; empty or
  whitespace-only secrets; `azure` without `api_version` or without `type: azure`
  (`ProviderSettings._check_field_compatibility`).
- The same routing applies to agent sessions, resumed sessions, and dialog turns
  (`_apply_provider_config` is called on all three), so every session in a run hits one endpoint.
- `--provider <name>` replaces the whole `ProviderSettings` and warns when structured config is
  dropped: `src/conductor/cli/run.py::_apply_provider_override`.
- Tests: `tests/test_providers/test_copilot_provider_routing.py`.

### Existing runtime connection

- `runtime_url` (optional `runtime_token`) makes `_build_client` connect with
  `RuntimeConnection.for_uri(...)` instead of spawning a nested `copilot`. The SDK does not spawn
  for URI connections and its `stop()` leaves the external server running.
- `_resolve_runtime_connection` reads YAML first, then `COPILOT_PROVIDER_RUNTIME_URL` /
  `COPILOT_PROVIDER_RUNTIME_TOKEN`; the env vars activate the connection on their own (zero-YAML
  path for orchestrators).
- `has_external_runtime()` is a separate axis from `has_custom_routing()`; both may be active at
  once. `has_structured_config()` counts either, so neither collapses to bare-string serialization.
- Schema rejects `runtime_token` without `runtime_url`, empty/whitespace values, and both fields on
  any provider other than `copilot`.

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
- `_spawned_runtime_process` reads the SDK's `_cli_process` (the spawned child; `None` for URI and
  FFI modes). `_fix_pipe_blocking_mode` reads `_process` (the transport: a socket wrapper in TCP
  mode, an FFI adapter with its own `poll()`). Do not unify them onto `_process`; FFI mode would
  then look like a killable child. A missing `_cli_process` on a started spawned client warns once.
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

### Usage and pricing

- `assistant.usage` fires once per API call; totals accumulate across calls. A repeated event is
  deduplicated by `api_call_id`, falling back to `provider_call_id`, then `service_request_id` (all
  optional in the SDK schema; with none present the event counts as a new call).
- Parse-recovery calls add to all four billing counters, while `last_call_input_tokens` is replaced
  by the latest call's value, not summed.
- `CopilotProvider.get_model_pricing` derives USD from the SDK's `billing.token_prices` (AI Credits,
  `_COPILOT_USD_PER_CREDIT`, an observed 100 credits = $1). It MUST never raise; a missing or
  malformed price returns `None` so cost falls back to the static table.

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
- [ ] Session kwargs change applied to create, resume, and dialog paths.
- [ ] Anything touching `_client`/`_started` keeps the invalidate-before-rebuild order.
- [ ] Allowlist change covered in `tests/test_providers/test_copilot_tools_allowlist.py` for both
      create and resume paths.
- [ ] Usage change keeps dedup and the single-call `last_call_input_tokens` rule.

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
- Examples: `examples/copilot-local-llm.yaml`, `examples/copilot-existing-runtime.yaml`
- [Providers (shared contract)](../providers.md)
