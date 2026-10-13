# Providers

The shared contract every agent provider (`copilot`, `claude`, `openai`, `claude-agent-sdk`,
`hermes`, `aca`) must honor: lifecycle, capability declarations, events, output and usage
accounting, recovery, and how a new provider is registered. Provider-specific deep notes live in
[providers/copilot.md](providers/copilot.md) and
[providers/claude-agent-sdk.md](providers/claude-agent-sdk.md).

## When to read

- Editing anything under `src/conductor/providers/` (including
  `src/conductor/providers/_pydantic_ai/`).
- Editing `src/conductor/aca_runner/` or `src/conductor/runner/protocol.py`.
- Adding a provider, a `ProviderCapabilities` field, or an `AgentProvider.execute()` kwarg.
- Touching cost, token, billing, or context-window accounting (`src/conductor/billing.py`,
  `src/conductor/engine/pricing.py`, `src/conductor/engine/usage.py`).
- Touching provider-related validation in `src/conductor/config/validator.py` or `ProviderSettings`
  in `src/conductor/config/schema.py`.
- Touching OpenTelemetry wiring in `src/conductor/telemetry/`.

## Required invariants

### Parity and lifecycle

- Every provider MUST keep the `AgentProvider` lifecycle (`validate_connection()`, `execute()`,
  `close()`) and return a fully shaped `AgentOutput` on success (fields may be `None`):
  `src/conductor/providers/base.py`.
- A change to one provider's behavior, contract, or capability MUST be checked against every other
  provider. The dashboard, JSONL log, console subscriber, and engine all assume consistent behavior.
- Every concrete provider MUST declare a class-level `CAPABILITIES: ProviderCapabilities`;
  `AgentProvider.__init_subclass__` raises `TypeError` at import time otherwise. Test fakes opt out
  with `class _Fake(AgentProvider, abstract=True)`: `src/conductor/providers/base.py`.

### Capabilities are contracts

- `CAPABILITIES` MUST describe observed behavior. If a capability cannot be honored under all
  conditions, declare the weaker value:
  `src/conductor/providers/capabilities.py::ProviderCapabilities`.
- Every capability-gated feature is enforced twice: statically by `conductor validate`
  (`src/conductor/config/validator.py`) and again at run time, because `conductor run` never calls
  the static validator. Run-time halves live in `src/conductor/executor/agent.py`
  (`_reject_unsupported_skills`, `_reject_unsupported_plugins`, `_reject_unsupported_settings_dir`,
  `_reject_unsupported_session_key`, ...) and
  `src/conductor/providers/factory.py::_enforce_temperature`. A new gated capability MUST get both
  halves.
- `reasoning_effort=()` is invalid (use `None` for "no concept"); `plugins=True` requires a
  non-`None` `plugin_flavor` (model validator).
- Capability lookups that must not instantiate a provider go through `get_capabilities`,
  `uses_native_skills`, `uses_native_plugins`, `plugin_flavor_for`, which return `None` when the
  answer cannot be determined so callers skip rather than guess.

### Registering a provider

- Add the name to `ProviderName` in `src/conductor/config/schema.py` (`ProviderType` in
  `src/conductor/providers/factory.py` is an alias of it), wire construction in
  `src/conductor/providers/factory.py::create_provider`, add a credential spec to
  `_CREDENTIAL_SPECS` in `src/conductor/providers/diagnostics.py`, and add the class path to
  `_PROVIDER_CLASS_PATHS` in `src/conductor/providers/capabilities.py`.
- Every runtime field a provider needs MUST be forwarded by
  `src/conductor/providers/registry.py::ProviderRegistry._get_or_create_provider` to
  `create_provider`; a missing kwarg there is a latent forwarding bug that no unit test of the
  provider alone will catch.
- Provider creation is serialized per registry: `_get_or_create_provider` holds one `asyncio.Lock`
  across `create_provider(...)` and re-checks the cache inside it, so concurrent agents share one
  instance. Cached reads stay lock-free. Do not move construction outside the lock.
- Structured `runtime.provider` settings are passed only to the provider whose name matches
  `runtime.provider.name`; a per-agent override onto another provider gets `None` and must fall back
  to safe defaults.
- Per-name `ProviderSettings` field acceptance is enforced in
  `src/conductor/config/schema.py::ProviderSettings._check_field_compatibility`. The user-facing
  table is canonical in [configuration](../configuration.md#field-compatibility-by-provider); keep
  it in sync when adding a field.

### Event callbacks

- Emit the same event types at the same semantic points:
  - `agent_turn_start` with `{"turn": "awaiting_model"}` immediately before each API call;
  - `agent_turn_start` with `{"turn": N}` at the start of each agentic-loop iteration;
  - `agent_message` for response text, `agent_reasoning` for thinking content;
  - `agent_tool_start` / `agent_tool_complete` around tool executions.
- Any reasoning content the model returns MUST surface as `agent_reasoning`, or the provider
  declares `agent_reasoning_events=False`.

### Retry and error handling

- Retry semantics, retryable-vs-fatal classification, and timeout behavior MUST match across
  providers. Deterministic failures (bad config, bad path, refused option) raise
  `ProviderError(is_retryable=False)`.
- Pydantic AI providers (`claude`, `openai`) build agents with `AgentRetries(tools=0, output=N)`:
  tool retries stay 0 because Conductor's `execute_with_retry` is the only retry layer for API and
  tool failures: `src/conductor/providers/_pydantic_ai/agent_builder.py`.

### Structured-output recovery (#343)

- Schema validation MUST happen inside the provider's in-session recovery loop, never only after
  returning. `validate_output` in `src/conductor/executor/agent.py` is a backstop; by the time it
  runs Copilot has disconnected its session, so a shape failure there is unrecoverable.
- Every provider calls `src/conductor/providers/_output_shape.py::normalize_agent_output` before
  `validate_output`. It rejects a non-object response and applies `unwrap_scalar_wrappers`, which
  unwraps only when exactly one candidate slot (the field name, `value`, `result`) has the expected
  scalar type; ambiguity leaves the object untouched so the caller re-prompts. Do not add coercion
  to `src/conductor/executor/output.py`; it also validates `set`/`script` output and is strict by
  design.
- Copilot and Hermes run an explicit loop: parse, normalize, validate; on failure emit
  `agent_parse_recovery` via `src/conductor/providers/_event_format.py::emit_parse_recovery_event`
  and re-prompt with `src/conductor/providers/_recovery_prompt.py::build_parse_recovery_prompt`,
  whose wording differs for schema vs syntax failures. Pinned by
  `tests/test_providers/test_copilot_schema_recovery.py` and
  `tests/test_providers/test_hermes_schema_recovery.py`.
- On budget exhaustion re-raise the original `ValidationError` for a schema failure (it names the
  field and type); raise `ProviderError` only for a syntax failure.
- Trap: `parse_json_output` wraps JSON syntax errors in `ValidationError` too, so syntax and schema
  failures cannot be told apart by exception type; split them by which call failed (see
  `src/conductor/providers/hermes.py::HermesProvider._parse_and_validate`).
- `claude` and `openai` request structured output through a Pydantic AI `ToolOutput` built from the
  schema; Pydantic AI validates and re-prompts in-session up to `max_parse_recovery_attempts`, and
  each output retry emits `agent_parse_recovery` via
  `src/conductor/providers/_pydantic_ai/events.py::emit_output_recovery_event`. The result then
  still passes through `normalize_agent_output` + `validate_output` in
  `src/conductor/providers/_pydantic_ai/structured_output.py::extract_content`; a plain-text answer
  goes through `parse_text_fallback`.
- `claude-agent-sdk` has no in-session loop: it relies on the SDK `output_format` and declares
  `structured_output="prompt_injection"`; the executor backstop is the only validation.

### Plugin component kwargs (#378)

- `execute()` accepts `skill_directories`, `custom_agents`, and `extra_mcp_servers` on every
  provider. A provider declaring `plugins=True` MUST honor all three and declare
  `supports_native_plugins`; `__init_subclass__` rejects the signature drift at import time, and
  `tests/test_plugins/test_providers.py` covers "accepted then ignored". There is no eager-injection
  fallback for a subagent or an MCP server. See [plugins and skills](plugins-and-skills.md).

### Output, usage, and context-window accounting

- `AgentOutput.input_tokens` is the total prompt across all calls and MUST include
  `cache_read_tokens` and `cache_write_tokens` (they are subsets, never additions). Copilot and
  Pydantic AI report it that way natively; `claude-agent-sdk` folds them in via
  `src/conductor/providers/claude_agent_sdk.py::_read_usage`, the single parser for those keys.
- `src/conductor/engine/pricing.py::calculate_cost` subtracts a cache bucket from the input total
  only when the pricing has a non-zero rate for it, then clamps a negative residual and logs once
  per model (never raises). Pinned by `tests/test_engine/test_pricing.py`.
- `AgentOutput.last_call_input_tokens` MUST be the prompt size of the most recent single API call,
  or `None`; never an aggregate (#412). Claude Agent SDK sums `input_tokens +
  cache_read_input_tokens + cache_creation_input_tokens` for it. A provider that cannot isolate one
  call reports `None` (Hermes after a parse-recovery run). The engine builds
  `context_window_used`/`context_window_max` only in `WorkflowEngine._context_window_fields` in
  `src/conductor/engine/workflow.py`, drops both to `None` when used exceeds max, and warns once per
  run (`_context_window_anomaly_warned`). Pinned by
  `tests/test_engine/test_context_window_events.py`.
- Max-token hooks `get_max_prompt_tokens` / `get_max_output_tokens` are optional (base returns
  `None`). `claude`, `openai`, `copilot` implement them; `None` from the others is a first-class
  "fall through to registry/fallback" signal for compaction, not an error.
- Pricing hook `get_model_pricing(model)` is optional (base returns `None`) and MUST never raise.
  Resolution order in `src/conductor/engine/pricing.py::get_pricing`: workflow `cost.pricing`
  override, provider hook, `DEFAULT_PRICING`, `None`. The engine resolves each model once via
  `WorkflowEngine._ensure_pricing_resolved` before every `UsageTracker.record`. Only Copilot
  implements the hook (see [copilot](providers/copilot.md)). Partial totals MUST surface
  `unpriced_agents` / `has_unpriced` from `src/conductor/engine/usage.py` (`~$X (N agents
  unpriced)`), never as complete.

### Billing provenance (#573)

- `AgentOutput.billing_mode` has three states: `None` (the provider made no statement; renders
  exactly as before), `"unknown"` (tried and could not prove it), or a known mode (`metered_api`,
  `subscription`). A provider MUST never invent a value.
- Only `claude-agent-sdk` populates it, via `_derive_billing` (evidence-based, defaulting to
  `"unknown"`); every other provider leaves `None`.
- `src/conductor/billing.py` is a stdlib-only leaf importing nothing from `conductor`, so providers,
  engine, fleet, and CLI can all depend on it. Aggregates are `AggregateBilling` with a derived
  `state` (`mixed` whenever more than one class is present; no dominant mode, no tie-break).
  Rendered labels are constants from that module plus integer counts, so a logged value never
  reaches rendered text or Rich markup. `coerce_billing_mode` maps an unknown string to `"unknown"`
  and a non-string to `None`; `count_mode` is the only place a mode becomes a class.
- A billing label MUST be counted on the same events and under the same predicate as the cost figure
  it sits beside (Fleet does this in `src/conductor/fleet/summary.py` via `_contributes_usage` /
  `_count_billing`), never taken from a terminal record or engine aggregate. Sub-workflow usage
  rolls up through the parent's `subworkflow_completed` event; see
  [Fleet TUI](fleet-tui.md#event-log-derived-state) for the accounting rule. Tests:
  `tests/test_billing.py`, `tests/test_providers/test_claude_agent_sdk_billing.py`,
  `tests/test_engine/test_billing_provenance.py`.

### Reasoning effort

- The unified vocabulary is `low | medium | high | xhigh | max`
  (`src/conductor/providers/reasoning.py`). Each provider declares the subset it supports in
  `CAPABILITIES.reasoning_effort` (or `None` for no concept), translates it natively, and re-checks
  the resolved value at run time (it may only be known after Jinja rendering), raising
  `ValidationError` with a clear message.
- Current declarations: `copilot`, `claude`, `aca` all five; `hermes` omits `max`; `openai` declares
  `("low", "medium", "high")`; `claude-agent-sdk` declares `None` (not wired), and the validator
  rejects any effort on it, templated or not.
- Copilot validates per model against `supported_reasoning_efforts`
  (`CopilotProvider._validate_reasoning_effort_for_model`). Claude maps effort to an extended
  thinking budget via `EFFORT_TO_BUDGET_TOKENS` (low=2048, medium=8192, high=16384, xhigh=32768,
  max=59904, i.e. 64000 minus 4096 answer headroom), and
  `src/conductor/providers/_pydantic_ai/agent_builder.py::_coerce_for_thinking` forces temperature
  to 1.0 and raises `max_tokens` above the budget. OpenAI checks the model profile's
  `openai_supports_reasoning` at agent-build time; an unavailable attribute skips the check.

### In-memory continuation

- A provider that can continue a completed execution declares `supports_continuation=True`, sets
  `AgentOutput.continuation_state` on every completed non-partial output, and treats
  `rendered_prompt` as the sole next user turn when handed the state back. Today: `claude`/`openai`
  (`outcome.result.all_messages()` in `src/conductor/providers/_pydantic_ai/runner.py`) and `hermes`
  (`result["messages"]`, withheld when a parse-recovery call produced the output).
- A provider that cannot MUST ignore the kwarg (an "Ignored." docstring entry plus `del`) and leave
  the field `None`, which tells the executor to rebuild the prompt statelessly.
- The state is provider-opaque (`object` at the boundary), MUST never be handed to another provider,
  and MUST never be serialized to checkpoints or event logs. The executor refuses state from a
  provider without support: `src/conductor/executor/agent.py`.

### MCP-free executions

- `execute(suppress_mcp_servers=True)` (keyword-only, default `False`) runs with no MCP servers at
  all: neither workflow `runtime.mcp_servers` nor per-call `extra_mcp_servers`. Every provider
  declaring `mcp_tools` MUST honor it; `hermes` (`mcp_tools=False`) ignores it with `del`.
- The only caller is `src/conductor/engine/validator.py::OutputValidator`. The signal is
  execution-level on purpose: nothing in YAML can set it, so authored `tools: []` + MCP refusals
  stay load-bearing. Pinned by `tests/test_providers/test_base.py::TestSuppressMcpServersContract`
  (all providers' signatures) and
  `tests/test_engine/test_validator_integration.py::TestValidatorRunsWithoutMcpServers` (drives the
  real validator, which is fail-open and would otherwise swallow a refusal).

### Shared Pydantic AI runner

- `claude` and `openai` delegate `execute()` to
  `src/conductor/providers/_pydantic_ai/runner.py::run_agent_pipeline`, which owns toolset wiring,
  retry/interrupt/extract, partial output, and model-name resolution. A provider wrapper only
  resolves its MCP manager, builds a `build_agent_fn(toolsets, *, max_parse_recovery_attempts,
  compaction)` closure, converts its `RetryConfig`, and delegates.
- The runner imports its seam helpers (`run_with_interrupt`, `execute_with_retry`,
  `extract_content`, `MCPManagerToolset`, `build_agent_output`) inside `run_agent_pipeline`, not at
  module scope. Keep it that way: tests patch them at their defining modules.

### OpenAI provider

- YAML `api_key`/`base_url` win over `OPENAI_API_KEY`/`OPENAI_BASE_URL`. Once a custom `base_url` is
  in effect (YAML or `OPENAI_BASE_URL`), an ambient `OPENAI_API_KEY` is never forwarded: client
  construction raises `ValidationError` unless `api_key` was passed explicitly
  (`src/conductor/providers/openai.py::OpenAIProvider._initialize_client`). `self._api_key` stays
  `None` on the ambient path so the matching guard in `_resolve_openai_model` stays reachable.
- Chat Completions only; hence `agent_reasoning_events=False` (Pydantic AI only builds thinking
  parts from DeepSeek/Moonshot-style `reasoning_content`).
- `ProviderSettings(name="openai")` rejects Copilot-only fields; `type` and `wire_api` get targeted
  "always speaks Chat Completions" messages, the rest the generic Copilot-only message.

### Temperature bounds

- The schema allows `0..2`; each provider's ceiling is `CAPABILITIES.max_temperature` (`1.0` for all
  but `openai`, which is `None`). It is enforced statically in `src/conductor/config/validator.py`
  and at construction in `src/conductor/providers/factory.py::_enforce_temperature`, the choke point
  `run`, `resume`, and `validate` share. `claude-agent-sdk` rejects `temperature`/`max_tokens`
  outright.

### Hermes

- Experimental; `mcp_tools=False` (so `tools: []` genuinely disables tools and stays valid),
  `interrupt=False`, `reasoning_effort` without `max` (re-checked at execute time), skills via eager
  injection. It keys session ids by bare agent name, like Copilot, so the two can still shadow each
  other in a merged checkpoint map (known limitation). See [Hermes](../providers/hermes.md).

### ACA provider

- `src/conductor/providers/aca.py::AcaRuntimeProvider` is a host-side transport shim: it derives a
  session identifier, authenticates with `DefaultAzureCredential`, and relays NDJSON frames from the
  in-container runner (`src/conductor/aca_runner/server.py`, which wraps a real `CopilotProvider`)
  verbatim to `event_callback`. The wire contract is `src/conductor/runner/protocol.py`;
  `src/conductor/providers/aca_protocol.py` is a deprecated re-export shim that warns on import.
- Inner credential precedence (`_resolve_inner_provider_settings`): `COPILOT_PROVIDER_BASE_URL`
  (BYOK, with optional `COPILOT_PROVIDER_API_KEY`/`COPILOT_PROVIDER_BEARER_TOKEN`), then
  `COPILOT_GITHUB_TOKEN`/`GH_TOKEN`/`GITHUB_TOKEN`, then `gh auth token` (`_resolve_gh_cli_token`,
  where every failure means "no token"), else `ProviderError`. Do NOT read the editor plugins'
  `~/.config/github-copilot/auth.db`. Secrets stay `SecretStr` until wire serialization.
- Tests asserting the "no credential" error MUST stub the `gh` subprocess (see
  `TestAcaCredentialPrecedence._clear_credential_env` in `tests/test_providers/test_aca.py`), or
  they pick up the developer's real token.
- Capabilities (re-verified): `mcp_tools=True`, `workflow_tools_passthrough=False`,
  `working_dir=False` (only the container-relative `sandbox.working_dir` is honored), `skills=False`
  and `plugins=False` (host paths the sandbox cannot read), `checkpoint_resume=False` (ephemeral
  sessions), `interrupt=True` and `max_session_seconds=True` declared host-side.
- Tools policy enforced by `src/conductor/config/validator.py` (`_check_agent_tools`): a non-empty
  `tools:` is refused; `tools: []` is refused only when the workflow declares `mcp_servers` (the
  runner attaches every server unconditionally); an omitted `tools:` that would inherit a non-empty
  workflow `tools:` is refused. The runner does forward `tools` to its inner `CopilotProvider`,
  which since #585 applies it as `available_tools`; the host capability has not been re-audited for
  that, so the refusals stand until it is.
- Runner MVP gaps: there is no `/interrupt` endpoint and no runner-level `max_session_seconds` guard
  (only the inner Copilot's own check). The host falls back to a best-effort session delete; both
  cleanup calls use a 10-second timeout. Stopping an `aca` agent stops the host waiting, not the
  sandbox computing.
- Runner hardening (#396), five independent layers: loopback bind by default
  (`src/conductor/aca_runner/__main__.py`; the image sets `ACA_RUNNER_HOST=0.0.0.0`); opt-in
  `ACA_RUNNER_AUTH_TOKEN` checked on `/execute` before the inner provider is built
  (`src/conductor/aca_runner/auth.py::resolve_runner_token`, constant-time compare); an
  `inner_provider_settings` key allowlist (`check_inner_provider_settings`,
  `ALLOWED_INNER_PROVIDER_SETTINGS_KEYS`) plus optional `ACA_RUNNER_ALLOWED_BASE_URLS`; the
  `X-Conductor-Runner-Token` header sent only on `execute()`, `_send_interrupt()`, and
  `validate_connection()`, never `_stop_session()`; and `identifier` treated as routing metadata,
  not authentication. A new key in `_resolve_inner_provider_settings` MUST also be added to the
  runner allowlist. `/health` stays unauthenticated and reports header presence only.

### Experimental tier

- Allowed carve-outs and promotion criteria are canonical in [experimental
  providers](../providers/experimental.md). An experimental provider MUST still: keep the lifecycle
  and `AgentOutput` shape; raise real exceptions; declare accurate capabilities; ship a mocked-SDK
  smoke test; keep `concurrent_safe=True` or fail validation in concurrent groups.
- `skills` is not a carve-out: a provider reaches `skills=True` natively
  (`supports_native_skills=True`) or through `AgentExecutor`'s eager injection; declare `False` only
  when neither can work (`aca`). The validator cross-checks `skills:` against the flag.
- `plugins` is a carve-out because there is no fallback; `plugins=True` requires honoring all three
  component kwargs (see above).

### Telemetry

- Native provider spans are nested under Conductor's orchestration spans; a provider whose SDK emits
  tool spans declares `native_otel_spans=True` (`copilot`, `claude`, `openai`) so Conductor does not
  duplicate them. Per-run activation is computed by
  `src/conductor/providers/capabilities.py::native_otel_spans_active` and carried as
  `native_otel_spans_active` on agent start events.
- Copilot native spans need an active telemetry run, an `http/protobuf` or `http/json` OTLP
  protocol, and a spawned runtime (not `runtime_url`). Under gRPC (the default) the client is built
  without telemetry and `src/conductor/telemetry/guards.py::warn_copilot_grpc_once` warns once per
  run.
- The OTLP protocol and endpoint are latched per run and shared by the engine and the Copilot
  client; do not re-read the environment mid-run. Global spans dispatch through
  `src/conductor/telemetry/delegating.py::_DelegatingTracerProvider`. See
  [telemetry](../telemetry.md).

### Tool output limits

- `runtime.tool_output` is a per-result cap, not a context budget; user semantics are canonical in
  [MCP tools](../mcp-tools.md#tool-output-limits).
- `claude`/`openai` truncate Conductor-side in `MCPManagerToolset` and emit
  `agent_tool_output_truncated`; `copilot` maps the config onto the SDK `large_output` option
  (`_large_output_config`, `max_chars` as bytes; `spill_to_file: false` disables limiting and
  warns); `aca` forwards the config to the runner; `claude-agent-sdk` ignores it (the CLI's
  `MAX_MCP_OUTPUT_TOKENS` governs); `hermes` has no MCP. Spill files hold raw output and are never
  deleted by Conductor.

### Context compaction (`claude`, `openai`)

- Always on. Plan in
  `src/conductor/providers/_pydantic_ai/compaction_window.py::resolve_compaction_plan`: `trigger =
  window - output_limit - min(buffer, 25% of window)`, target `min(55% of window, trigger - 5%
  margin)`; a trigger under `MIN_VIABLE_TRIGGER` disables compaction for the execution
  (`insufficient_headroom`) rather than arming a degenerate threshold. `output_limit` is the minimum
  of the effective `max_tokens` and the provider/registry output cap; `buffer = 2 * ceil(max_chars /
  4) + 15000` (`tool_buffer_tokens`).
- A density-calibrated second estimate guards the hard window against token-dense content; when it
  fires the tier chain is driven against that measurement. Telemetry stays on the primary scale.
- Fail-open (`_FailOpenCompactionWrapper` in `src/conductor/providers/_pydantic_ai/compaction.py`):
  an inner strategy failure emits an errored complete event and latches compaction off for the rest
  of the execution; both estimators failing emits `agent_compaction_skipped` without latching.
- The summarizing tier's nested model call consumes a request slot from the agent's
  `max_agent_iterations` budget (not refunded) and inherits the parent run's usage limits.

## Change checklist

- [ ] Applied the change to every provider it concerns, or declared the carve-out in `CAPABILITIES`.
- [ ] New capability-gated feature has both a validator check and a run-time refusal.
- [ ] New `execute()` kwarg added to every provider (unused ones `del` it with an "Ignored." note)
      and to `tests/test_providers/test_base.py` contract tests where applicable.
- [ ] New provider: `ProviderName`, `create_provider`, `_CREDENTIAL_SPECS`, `_PROVIDER_CLASS_PATHS`,
      registry forwarding, smoke test, user doc under `docs/providers/`.
- [ ] Token fields: `input_tokens` cache-inclusive, `last_call_input_tokens` single-call or `None`.
- [ ] Billing: `billing_mode` left `None` unless the provider has evidence.
- [ ] Reasoning tuple declared accurately and re-checked at run time.
- [ ] `ProviderSettings` field added to `_check_field_compatibility` and the configuration table.
- [ ] Ran `uv run pytest tests/test_providers tests/test_config/test_validator_capabilities.py`.

## Rationale and evidence

**Dual enforcement.** `conductor run` never calls `validate_workflow_config`, so a capability
checked only statically holds in one command and is silently contradicted in the other.

**Recovery inside the loop (#343).** Copilot disconnects its SDK session before returning, so only
an in-session check can re-prompt. `normalize_agent_output` refuses to guess on ambiguity so that
`{"answer": {"error": "..."}}` is never laundered into an answer.

**Cache-inclusive totals.** Billing cache buckets additively overstated a cached tool-calling run by
roughly an order of magnitude; a `0.0` cache rate in `DEFAULT_PRICING` means "no published rate", so
subtracting it unconditionally would price a cached prompt at nothing. This matches `genai_prices`
except that Conductor clamps instead of raising, because a cost annotation must never abort a run.
Reading cache keys inline at several sites once let defaults drift and double-count.

**Context window (#412).** Reusing the billing sum produced false ">100%" bars on multi-turn agents.
A debug-only anomaly log never reaches an operator, hence the once-per-run warning.

**Billing provenance (#573).** A cost figure is an estimate, not an invoice; labelling it requires
evidence, and the label must describe the exact figure next to it.

**Serialized creation.** Without the lock, concurrent agents each built a provider and the last
write won, losing per-instance state.

**OpenAI key rule.** Forwarding a personal `OPENAI_API_KEY` to an endpoint the author did not pair
it with is a credential leak; Copilot custom routing refuses the same fallback.

**MCP-free validator.** The synthetic grader asks for `tools: []`; on `claude-agent-sdk` that plus
attached servers is refused, and the validator's fail-open would then pass every graded output.

**ACA.** The sandbox cannot do interactive OAuth, so the host forwards a credential; `auth.db`
belongs to the Copilot Language Server, has changed format twice, and is slated for encryption. The
runner token rides `X-Conductor-Runner-Token` because the ACA gateway consumes `Authorization`, and
`_stop_session` is a management-plane call where the header would leak to Azure (#396).

**Telemetry.** Native Copilot spans come from the CLI child, which only exports over HTTP OTLP;
latching avoids mid-run protocol drift between engine and client.

**Compaction.** The 50-request default limit Pydantic AI applies to a nested agent run would refuse
summaries exactly when long runs need them, so the nested run inherits the parent's limits.

## Related references

- [Experimental providers](../providers/experimental.md) (carve-outs, promotion criteria)
- [Provider comparison](../providers/comparison.md), [OpenAI](../providers/openai.md),
  [Hermes](../providers/hermes.md), [ACA](../providers/aca.md),
  [Claude](../providers/claude-provider.md)
- [Configuration: field compatibility](../configuration.md#field-compatibility-by-provider),
  [reasoning effort](../configuration.md#reasoning-effort)
- [MCP tools: tool output limits](../mcp-tools.md#tool-output-limits), [telemetry](../telemetry.md)
- [Runner protocol design](../design/runner-protocol.md), [ACA provider
  design](../projects/aca/aca-provider.design.md)
- Deep notes: [Copilot](providers/copilot.md), [Claude Agent SDK](providers/claude-agent-sdk.md)
- Related guides: [plugins and skills](plugins-and-skills.md), [workflow
  engine](workflow-engine.md), [testing](testing.md)
