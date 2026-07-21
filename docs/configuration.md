# Configuration Documentation

This document describes the runtime configuration options available in Conductor workflows.

## Runtime Configuration

The `runtime` section of your workflow defines provider settings and global defaults.

### Basic Structure

```yaml
workflow:
  runtime:
    provider: copilot  # or 'claude', 'claude-agent-sdk', 'hermes'
    default_model: gpt-5.2
    temperature: 0.7
    max_tokens: 4096
    default_reasoning_effort: medium  # low | medium | high | xhigh | max (optional)
    default_context_tier: default  # default | long_context (optional, Copilot only)
    # Provider-specific settings...
```

The `default_reasoning_effort` field sets a workflow-wide default for model
reasoning / extended-thinking effort that every provider-backed agent inherits
unless it declares its own `reasoning.effort` override. See
[Reasoning Effort](#reasoning-effort) for the per-provider translation and
constraints.

The `default_context_tier` field sets a workflow-wide default for the model's
context-window tier that every provider-backed agent inherits unless it
declares its own `context_tier` override. See [Context Tier](#context-tier)
for details. This is a Copilot-only capability.

## Provider Selection

### Copilot Provider

Uses GitHub Copilot SDK for agent execution.

```yaml
workflow:
  runtime:
    provider: copilot
    default_model: gpt-5.2
    mcp_servers:
      web-search:
        command: npx
        args: ["-y", "open-websearch@latest"]
        tools: ["*"]
```

**Features**:
- Tool support (MCP servers)
- Streaming responses
- GitHub authentication

**Models**: `gpt-5.2`, `gpt-5.2-mini`, `o1-preview`

### Claude Provider

Uses Anthropic Claude SDK for agent execution.

```yaml
workflow:
  runtime:
    provider: claude
    default_model: claude-sonnet-4.5
    temperature: 0.7
    max_tokens: 4096
```

**Features**:
- 200K context window (all models)
- Pay-per-token pricing

**Models**: `claude-sonnet-4.5`, `claude-haiku-4.5`, `claude-opus-4.5`

**See**: [Claude Provider Documentation](providers/claude.md)

### Hermes Provider (Experimental)

> **Experimental** — see [Experimental Providers](providers/experimental.md) for stability policy.

Uses the NousResearch hermes-agent library for agent execution. Supports any OpenRouter-style model identifier.

```yaml
workflow:
  runtime:
    provider: hermes
    default_model: anthropic/claude-sonnet-4
    max_agent_iterations: 25
    max_tokens: 4096
    temperature: 0.7
```

**Features**:
- Access to Anthropic, OpenAI, and OpenRouter models via one provider
- Built-in hermes tool ecosystem (no MCP config required)
- Custom endpoint routing via structured `provider:` config

**Models**: `anthropic/claude-sonnet-4`, `openai/gpt-4o`, any OpenRouter `provider/model` string

**See**: [Hermes Provider Documentation](providers/hermes.md)

### Custom Provider Routing (Ollama / vLLM / Azure OpenAI)

`runtime.provider` accepts either the bare string shorthand
(`provider: copilot`) **or** a structured object that forwards a
`ProviderConfig` to the Copilot SDK's `create_session(provider=…)`
parameter. This lets workflows route the Copilot SDK at:

- Local OpenAI-compatible servers — Ollama, vLLM, LM Studio, llamafile
- Azure OpenAI deployments
- Anthropic-compatible proxies
- Any other OpenAI-compatible REST endpoint

```yaml
workflow:
  runtime:
    provider:
      name: copilot
      type: openai                          # openai | azure | anthropic
      wire_api: completions                 # completions | responses
      base_url: http://localhost:11434/v1
      api_key: ${OPENAI_API_KEY:-ollama}
    default_model: llama3.1                 # required for non-Copilot endpoints
```

Azure OpenAI variant:

```yaml
workflow:
  runtime:
    provider:
      name: copilot
      type: azure
      base_url: https://<your-resource>.openai.azure.com
      api_key: ${AZURE_OPENAI_API_KEY}
      azure:
        api_version: "2024-10-21"
    default_model: gpt-4o
```

#### Activation rule (opt-in)

Custom routing activates **only** when at least one non-`name` field is
set in YAML. Ambient `OPENAI_*` environment variables alone will NOT
divert default Copilot traffic — that would be too easy a way to break
a workflow based on unrelated shell state. A bare `provider: copilot`
always means default GitHub Copilot routing.

#### Environment-variable fallbacks

Once a structured object opts in, missing fields fall back to env vars
in this precedence:

| Field | Env-var chain |
|---|---|
| `base_url` | `COPILOT_PROVIDER_BASE_URL` → `OPENAI_BASE_URL` |
| `api_key` | `COPILOT_PROVIDER_API_KEY` *(only)* |
| `bearer_token` | `COPILOT_PROVIDER_BEARER_TOKEN` *(only)* |
| `type` | defaults to `"openai"` when `base_url` is set |

Ambient `OPENAI_API_KEY` is intentionally **not** consulted as an
implicit fallback — that would silently send an OpenAI dev credential
to whatever `base_url` points at, which is a real credential-leak risk.
Users who want OpenAI-environment-style behavior must opt in
explicitly via `api_key: ${OPENAI_API_KEY}` interpolation in YAML.

#### Secrets

`api_key` and `bearer_token` are stored as Pydantic `SecretStr` — they
redact in `model_dump`, dashboard payloads, event logs, and
checkpoints. Prefer `${VAR}` env interpolation for the values in YAML
so the literal secret never lands in `workflow_started` events:

```yaml
api_key: ${OPENAI_API_KEY}           # good — interpolated at load time
api_key: sk-aaaaaaaaaaaaaaaa         # avoid — literal in yaml_source
```

If both `api_key` and `bearer_token` resolve (from any combination of
YAML and env), both are forwarded; the Copilot SDK silently prefers
`bearer_token`, and conductor logs a warning so the precedence is
visible.

#### Validator rules

`ProviderSettings` is frozen after construction. The schema rejects
the following misconfigurations at config load time so they cannot
silently produce a no-op SDK call:

- `name != "copilot"` combined with **any** non-`name` field
  (structured config for `claude` / `openai-agents` is not yet
  implemented).
- `type: azure` without an `azure: { api_version: ... }` block
  (and the reverse: `azure` block without `type: azure`).
- Anchorless routing fields: `wire_api`, `type`, `headers`, or
  `azure` cannot stand alone — at least one of `base_url`, `api_key`,
  `bearer_token` must also be set (in YAML or via the
  `COPILOT_PROVIDER_*` env vars).
- Empty `headers: {}`, empty `api_key: ""`, empty `bearer_token: ""`,
  empty `azure: { api_version: null }`.

When custom routing activates but every resolved field ends up empty
(for example, the workflow expects `COPILOT_PROVIDER_*` env vars and
none are set), the resolver raises `ProviderError` with a clear
message rather than silently routing back to default Copilot.

#### CLI override

`--provider <name>` (and `-p`) replaces the entire `ProviderSettings`
with the bare-string default for that name. When YAML had structured
fields, conductor logs a notice telling the user the custom routing
was dropped:

```
Provider override: claude
Provider override discards structured runtime.provider settings (base_url/type/etc.) from YAML; using SDK defaults.
```

#### Custom routing and dialog mode

The resolved provider config is attached to **every** Copilot
`create_session` call this provider makes — including the dialog-mode
turns used by `agent.dialog` evaluators. All sessions hit the same
endpoint, so you can mix custom-routed agents with dialog mode without
worrying about per-call drift.

#### Example workflow

[`examples/copilot-local-llm.yaml`](../examples/copilot-local-llm.yaml)
demonstrates the full pattern with both Ollama (active) and Azure
OpenAI (commented variant).

### Connecting to an Existing Copilot Runtime

By default the Copilot provider spawns its own nested `copilot` runtime
process (one per provider instance) to run agents. Instead, you can point
Conductor at an **already-running** Copilot runtime that was started in
server mode by some other process. Conductor then connects to that runtime
and reuses the authenticated runtime process for every agent. Each agent
still gets its own SDK session, and no nested `copilot` process is spawned.

This is the recommended way to run Conductor inside an external
orchestrator that already owns an authenticated Copilot process. For
example, an orchestrator can launch one authenticated
`copilot --headless` process and hand Conductor a connection handle, so all of
Conductor's agents/models are just new sessions on that shared server.
Authentication is handled once, at the server — Conductor never needs the
runtime's GitHub credentials. The optional runtime connection token only
authenticates access to the server socket.

#### YAML

```yaml
workflow:
  runtime:
    provider:
      name: copilot
      runtime_url: localhost:3000          # "port", "host:port", or a full URL
      runtime_token: ${COPILOT_RUNTIME_TOKEN}   # optional shared secret
    default_model: gpt-4o
```

- `runtime_url` — where the running runtime is listening. Accepts a bare
  `"port"`, `"host:port"`, or a full URL. URL schemes are parsing syntax only:
  the SDK opens a raw TCP connection and does not provide TLS.
- `runtime_token` — the shared secret the runtime was started with, if
  any. Stored as a `SecretStr` (redacted in events/checkpoints/dashboard);
  prefer `${VAR}` interpolation. Requires `runtime_url`.

#### Environment variables (zero-YAML)

The connection also activates from environment variables alone, so an
orchestrator can enable it without editing the workflow YAML:

| Field | Env var |
|---|---|
| `runtime_url` | `COPILOT_PROVIDER_RUNTIME_URL` |
| `runtime_token` | `COPILOT_PROVIDER_RUNTIME_TOKEN` |

The YAML value takes precedence; the environment variable is used as a
fallback when the YAML field is unset. These variables are namespaced under `COPILOT_PROVIDER_*` on
purpose: unlike ambient `OPENAI_*` variables, they can safely activate the
connection because they are specific to this feature.

```bash
# Orchestrator side, conceptually:
export COPILOT_CONNECTION_TOKEN=<connection-token>
copilot --headless --port 3000 &          # one authenticated runtime
export COPILOT_PROVIDER_RUNTIME_URL=localhost:3000
export COPILOT_PROVIDER_RUNTIME_TOKEN="$COPILOT_CONNECTION_TOKEN"
conductor run review.yaml                 # connects; spawns no nested runtime
```

#### Rules and notes

- `runtime_url` may be combined with custom model-provider routing. The runtime
  URL selects the CLI transport; `base_url` / `api_key` / related fields are
  forwarded on each SDK session to select the model endpoint.
- `runtime_token` requires `runtime_url` (a token with nowhere to connect
  is a misconfiguration) and, like the other secrets, may not be empty.
- The connection token authenticates the socket but does not encrypt it. Keep
  the default loopback binding where possible. Remote runtimes require
  `copilot --headless --host ...` plus a trusted private network, firewall, or
  TLS tunnel.
- The external runtime executes against its own host environment. If it runs in
  another container or machine, make the Conductor workspace available at the
  same working-directory path.
- Closing the provider does **not** terminate the external runtime — the
  SDK only shuts down runtimes it spawned itself, so the orchestrator-owned
  server keeps running. The orchestrator is also responsible for runtime
  health checks and restarts.
- Runtime-spawn-only options (custom CLI path, injected env, etc.) do not
  apply when connecting to an existing runtime.

#### Example workflow

[`examples/copilot-existing-runtime.yaml`](../examples/copilot-existing-runtime.yaml)
demonstrates connecting to an already-running Copilot runtime.

## Common Configuration Options

These options work with both providers:

### Model Selection

Set the default model for all agents:

```yaml
workflow:
  runtime:
    default_model: gpt-5.2  # or claude-sonnet-4.5
```

Override per agent:

```yaml
agents:
  - name: fast_agent
    model: claude-haiku-4.5  # Override default
    prompt: "Quick task..."
```

### Temperature

Controls randomness (0.0 = deterministic, 1.0 = creative):

```yaml
workflow:
  runtime:
    temperature: 0.7  # Balanced
```

**Ranges**:
- **Copilot (OpenAI)**: 0.0 - 2.0
- **Claude**: 0.0 - 1.0 (enforced by SDK)

**Guidelines**:
- `0.0 - 0.3`: Factual, deterministic (data extraction, classification)
- `0.4 - 0.7`: Balanced (general Q&A, analysis)
- `0.8 - 1.0`: Creative (brainstorming, content generation)

## Claude-Specific Configuration

### Max Tokens

Maximum OUTPUT tokens per response:

```yaml
workflow:
  runtime:
    max_tokens: 4096  # Required for Claude
```

**Limits**:
- Haiku: 4096 max
- Sonnet/Opus: 8192 max

**Note**: This is output tokens, not context window (200K separate limit)

## Reasoning Effort

Conductor exposes a single, unified `reasoning.effort` knob that controls how
much "thinking" budget the underlying model uses, and translates it to each
provider's native API. Allowed values: `low`, `medium`, `high`, `xhigh`, `max`.

Set a workflow-wide default and/or override per agent:

```yaml
workflow:
  runtime:
    provider: copilot
    default_model: gpt-5.2
    default_reasoning_effort: medium    # workflow-wide default

agents:
  - name: explainer
    # No reasoning block — inherits `medium` from the runtime default.
    prompt: "Explain {{ workflow.input.topic }}"

  - name: architect
    reasoning:
      effort: high                      # per-agent override wins
    prompt: "Design a system for {{ workflow.input.topic }}"
```

Per-agent overrides always win over the workflow-wide default. The
`reasoning.effort` field is **only** valid on standard `agent`-type agents; it
is rejected on `script`, `human_gate`, `workflow`, `wait`, and `terminate`
agents (none of which call a model).

### Per-provider translation

- **Copilot** — Forwards the chosen effort as `reasoning_effort` to
  `CopilotClient.create_session`. The value is validated against the model's
  advertised `supported_reasoning_efforts` capability metadata; a
  `ValidationError` is raised at startup if the model does not support the
  requested effort. Validation is skipped in mock mode or when capability
  metadata is unavailable.
- **Claude** — Enables Anthropic's extended thinking via
  `messages.create(thinking={"type": "enabled", "budget_tokens": N})` with the
  following effort → budget mapping:

  | Effort   | Budget tokens |
  |----------|---------------|
  | `low`    | 2 048         |
  | `medium` | 8 192         |
  | `high`   | 16 384        |
  | `xhigh`  | 32 768        |
  | `max`    | 59 904        |

  `max` is pinned to `64000 − 4096` — the largest budget that still leaves the
  default answer headroom under the 64000-token extended-thinking output cap
  (at `max`, `max_tokens` lands exactly on the cap).

  Extended thinking is only valid on thinking-capable models
  (`claude-3-7-*`, `claude-opus-4*`, `claude-sonnet-4*`, `claude-haiku-4*`); a
  `ValidationError` is raised otherwise. The provider also auto-coerces
  `temperature` to `1.0` (required by the Anthropic API for extended thinking,
  logged at INFO) and bumps `max_tokens` to fit `budget + 4096`, capped at
  `64000` (logged at INFO when clamped).

Reasoning / thinking content emitted by the model is surfaced via
`agent_reasoning` events and rendered in the dashboard, JSONL logs, and
`-vv` console output for both providers.

## Context Tier

Some models expose a larger context window (e.g. a 1M-token tier) selected via
a separate session parameter rather than the model name. Conductor surfaces
this as a unified `context_tier` knob. Allowed values: `default`,
`long_context`.

Use `long_context` for heavy-reasoning agents that ingest large evidence
(multi-MB logs, many candidate source files) and would otherwise truncate at
the default (~200K) tier.

`context_tier` composes independently with `reasoning.effort` — they map to two
separate `create_session` kwargs, so an agent may set both.

Set a workflow-wide default and/or override per agent:

```yaml
workflow:
  runtime:
    provider: copilot
    default_context_tier: default       # workflow-wide default

agents:
  - name: triage
    # No context_tier — inherits `default` from the runtime default.
    prompt: "Triage {{ workflow.input.topic }}"

  - name: analyze
    context_tier: long_context          # per-agent override wins
    reasoning:
      effort: high                      # composes with context_tier
    prompt: "Deeply analyze {{ workflow.input.topic }}"
```

Per-agent overrides always win over the workflow-wide default. The
`context_tier` field is **only** valid on standard `agent`-type agents; it is
rejected on `script`, `human_gate`, and `workflow` agents (none of which call a
model).

### Per-provider translation

- **Copilot** — Forwards the chosen tier as `context_tier` to
  `CopilotClient.create_session`. No static capability validation is performed;
  the SDK accepts or rejects the value at session creation.
- **Other providers** — The value is ignored; there is no equivalent knob.

## MCP Servers

Configure [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers for tool access. Both the Copilot and Claude providers support MCP tools.

```yaml
workflow:
  runtime:
    provider: copilot
    mcp_servers:
      web-search:
        command: npx
        args: ["-y", "open-websearch@latest"]
        tools: ["*"]  # All tools, or ["search", "scrape"]
      
      context7:
        command: npx
        args: ["-y", "@upstash/context7-mcp@latest"]
        tools: ["*"]
```

> **Provider note:** The Claude provider supports `stdio` servers only. HTTP and SSE servers are Copilot-only.

For full details on server types, tool filtering, environment variables, and OAuth authentication, see the [MCP Tools guide](mcp-tools.md).

## Context Configuration

Control how context flows between agents:

```yaml
workflow:
  context:
    mode: accumulate  # or 'last_only', 'explicit'
    max_tokens: 4000
    trim_strategy: drop_oldest  # or 'truncate', 'summarize'
```

### Context Modes

**accumulate** (default):
- All prior agent outputs available
- Good for synthesis workflows
- Can grow large quickly

**last_only**:
- Only previous agent's output
- Good for linear workflows
- Minimal token usage

**explicit**:
- Only declared inputs available
- Good for complex workflows
- Maximum control, minimal tokens

Example:

```yaml
workflow:
  context:
    mode: explicit

agents:
  - name: agent2
    input:
      - workflow.input.question  # Explicit declaration
      - agent1.output.summary
```

## Limits

Safety limits prevent runaway execution:

```yaml
workflow:
  limits:
    max_iterations: 10  # Default: 10, max: 500
    timeout_seconds: 600  # Default: None (unlimited)
    budget_usd: 5.00      # Default: None (no budget tracking)
    budget_mode: audit    # Default: audit. Options: audit, enforce
```

**max_iterations**:
- Prevents infinite loops
- Counts agent executions in routing cycles

**timeout_seconds**:
- Total workflow timeout
- Includes all agent executions

**budget_usd** and **budget_mode**:
- Tracks cumulative cost and acts when the budget is exceeded
- `audit` mode (default): emits a `budget_exceeded` event and logs a warning,
  but the workflow continues — use this to discover cost profiles
- `enforce` mode: emits a `budget_exceeded` event, saves a checkpoint,
  and stops the workflow with a `BudgetExceededError`. Resuming with
  `conductor resume` starts a fresh budget window (cumulative spend resets
  to $0), so raising the budget first is optional
- Sub-workflow spend is merged into the parent budget, so a parent-level
  budget accounts for delegated `type: workflow` cost
- When `budget_usd` is not set, no budget tracking occurs

**Recommended graduation path**:

1. Run workflows without a budget to see costs in the summary
2. Add `budget_usd` in `audit` mode to track overshoots without breaking workflows
3. Switch to `enforce` mode once you know your cost profile

## Complete Examples

### Claude Configuration

```yaml
workflow:
  name: claude-example
  runtime:
    provider: claude
    default_model: claude-sonnet-4.5
    temperature: 0.7
    max_tokens: 4096

  context:
    mode: explicit

  limits:
    max_iterations: 15
    timeout_seconds: 600

agents:
  - name: classifier
    model: claude-haiku-4.5  # Fast model override
    input: [workflow.input.text]
    prompt: "Classify: {{ workflow.input.text }}"

  - name: analyzer
    model: claude-sonnet-4.5  # Use default
    input: [workflow.input.text, classifier.output]
    prompt: "Analyze based on classification..."
```

### Copilot Configuration

```yaml
workflow:
  name: copilot-example
  runtime:
    provider: copilot
    default_model: gpt-5.2
    temperature: 0.7
    mcp_servers:
      web-search:
        command: npx
        args: ["-y", "open-websearch@latest"]
        tools: ["*"]
  
  context:
    mode: accumulate
    max_tokens: 8000
  
  limits:
    max_iterations: 10
    timeout_seconds: 300

agents:
  - name: researcher
    tools: [web_search]
    prompt: "Research {{ topic }}"
  
  - name: synthesizer
    tools: []  # No tools needed
    prompt: "Synthesize findings..."
```

## Environment Variables

### Claude Provider

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### Copilot Provider

```bash
# Configured via GitHub authentication
# No environment variable needed
```

### Logging

```bash
export CONDUCTOR_LOG_LEVEL=DEBUG  # INFO, DEBUG, WARNING, ERROR
```

## Best Practices

### Model Selection

1. **Default to balanced models**: `claude-sonnet-4.5` or `gpt-5.2`
2. **Use fast models for simple tasks**: `claude-haiku-4.5` for classification
3. **Reserve premium models**: `claude-opus-4.5` or `o1-preview` for complex reasoning

### Temperature

1. **Low (0.0-0.3)**: Data extraction, classification, deterministic tasks
2. **Medium (0.4-0.7)**: General Q&A, balanced workflows
3. **High (0.8-1.0)**: Creative writing, brainstorming, diverse outputs

### Context Management

1. **Use `explicit` mode** for multi-agent workflows (reduce token costs)
2. **Use `accumulate`** for synthesis workflows (need full history)
3. **Use `last_only`** for linear pipelines (minimal overhead)

### Cost Optimization (Claude)

1. **Limit `max_tokens`** to minimum needed
2. **Use Haiku** for high-volume simple tasks
3. **Use `context: mode: explicit`** to reduce input tokens

### Safety

1. **Set conservative limits** initially (`max_iterations: 10`)
2. **Use timeout** to prevent long-running workflows
3. **Set a cost budget** — start with `budget_usd` in `audit` mode to learn your cost profile, then switch to `enforce`
4. **Test with dry-run** before production

## Troubleshooting

### "max_tokens is required" (Claude)

Always set `max_tokens`:

```yaml
runtime:
  max_tokens: 8192
```

### "temperature must be between 0.0 and 1.0" (Claude)

Claude enforces stricter range than OpenAI:

```yaml
runtime:
  temperature: 1.0  # Max for Claude (OpenAI allows 2.0)
```

### "model not found"

Check model name spelling:

```yaml
# Good
default_model: claude-sonnet-4.5

# Bad
default_model: claude-3.5-sonnet  # Wrong: dot instead of dash
```

### MCP servers not working (Claude)

The Claude provider only supports `stdio` MCP servers. If you are using `http` or `sse` servers, switch to the Copilot provider or use a stdio-based server instead. See the [MCP Tools guide](mcp-tools.md) for provider-specific details.

## See Also

- [MCP Tools](mcp-tools.md)
- [Claude Provider Documentation](providers/claude.md)
- [Provider Comparison](providers/comparison.md)
- [Migration Guide](providers/migration.md)
- [Workflow Syntax](workflow-syntax.md)
