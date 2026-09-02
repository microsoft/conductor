# Claude Provider Documentation

The Claude provider enables Conductor workflows to use Anthropic's Claude models via Pydantic AI (`pydantic-ai` package, `AnthropicModel`).

## Table of Contents

- [Quick Start](#quick-start)
- [Architecture & Internal Design](#architecture--internal-design)
- [Behavioral & Migration Notes](#behavioral--migration-notes)
- [API Key Setup](#api-key-setup)
- [Custom Endpoints and Gateways](#custom-endpoints-and-gateways)
- [Model Selection](#model-selection)
- [Runtime Configuration](#runtime-configuration)
- [System Prompt](#system-prompt)
- [Streaming Limitations](#streaming-limitations)
- [Extended Thinking](#extended-thinking)
- [Context Compaction](#context-compaction)
- [Troubleshooting](#troubleshooting)
- [Cost Optimization](#cost-optimization)

## Quick Start

### 1. Install the Anthropic SDK

```bash
# Using uv (recommended)
uv add 'anthropic>=0.77.0,<1.0.0'

# Using pip
pip install 'anthropic>=0.77.0,<1.0.0'
```

### 2. Set up your API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Update your workflow

```yaml
workflow:
  name: my-workflow
  runtime:
    provider: claude  # Change from 'copilot' to 'claude'
    default_model: claude-sonnet-4.5

agents:
  - name: assistant
    model: claude-sonnet-4.5
    prompt: |
      Answer the following question: {{ workflow.input.question }}
    output:
      answer:
        type: string
    routes:
      - to: $end
```

### 4. Run your workflow

```bash
conductor run my-workflow.yaml --input question="What is Python?"
```

## Architecture & Internal Design

The Claude provider delegates its agentic loop, tool execution, and structured output processing to Pydantic AI (`pydantic-ai` package, `AnthropicModel`). `ClaudeProvider` in `src/conductor/providers/claude.py` implements the `AgentProvider` interface while delegating execution details to internal helpers in `src/conductor/providers/_pydantic_ai/`.

### Package Structure (`src/conductor/providers/_pydantic_ai/`)

- **`agent_builder.py`**: Factory (`build_agent`) mapping Conductor `AgentDef` configurations, system prompts, reasoning effort settings, temperature/max_tokens coercion, and output schemas to a Pydantic AI `Agent`.
- **`converters.py`**: Recursively converts workflow `output` schemas into dynamic Pydantic models for Pydantic AI `ToolOutput`, enforcing scalar type checks and boolean rejection.
- **`events.py`**: Bridges streaming Pydantic AI events (`PartStartEvent`, `PartDeltaEvent`, `FunctionToolCallEvent`, `FunctionToolResultEvent`) to Conductor `EventCallback` payloads (`agent_message`, `agent_reasoning`, `agent_tool_start`, `agent_tool_complete`, `agent_tool_output_truncated`), and emits turn boundary events.
- **`interrupt.py`**: Drives agent execution via `Agent.iter()` while honoring Conductor's `interrupt_signal`, wall-clock `max_session_seconds`, and `UsageLimits`.
- **`mcp_toolset.py`**: Wraps Conductor's `MCPManager` as a Pydantic AI `AbstractToolset`, managing tool naming, truncation, spill-to-file, and error signaling (`ToolFailed`).
- **`retry.py`**: Provides Conductor-level retries (`execute_with_retry`) with exponential backoff, jitter, and Anthropic error classification. Pydantic AI tool retries are disabled, while output retries use `max_parse_recovery_attempts` for native structured-output correction.
- **`structured_output.py`**: Handles post-processing (`extract_content`, `parse_text_fallback`), converting `ToolOutput` model dumps or fenced JSON text fallbacks into validated dicts via Conductor's `validate_output()`.
- **`usage.py`**: Maps Pydantic AI `RunUsage` (token counts, cache reads and writes) to `AgentOutput` fields for tracking and pricing calculation by `UsageTracker`.

## Behavioral & Migration Notes

There are no user-facing breaking changes. Workflow YAML syntax, `runtime.provider: claude` configuration, provider contracts, and CLI commands remain completely unchanged.

The transition to Pydantic AI includes the following internal behavioral changes:

- **Native parse recovery**: The `retry.max_parse_recovery_attempts` YAML field controls Pydantic AI's output-validation retry budget. Each correction attempt emits `agent_parse_recovery`, matching the observable provider contract used by Copilot and Hermes.
- **Truncation-hint path rewriting removed**: Legacy conductor-side path replacement in tool result text was removed. Tool output truncation and spill-to-file behavior are managed directly by `MCPManagerToolset`, and `agent_tool_output_truncated` events are emitted natively with original character length, truncated length, and spill path.
- **Thinking signature preservation**: Thinking/reasoning block handling and signature preservation are delegated to Pydantic AI's native Anthropic model adapter.

## API Key Setup

### Getting an API Key

1. Sign up or log in at [console.anthropic.com](https://console.anthropic.com)
2. Navigate to **Settings** → **API Keys**
3. Click **Create Key**
4. Copy the key (it starts with `sk-ant-`)
5. Store it securely

### Setting the API Key

#### Option 1: Environment Variable (Recommended)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Add to your shell profile (`.bashrc`, `.zshrc`, etc.) for persistence:

```bash
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.zshrc
```

#### Option 2: `.env` File

Create a `.env` file in your project root:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

**Warning**: Never commit `.env` files to version control. Add to `.gitignore`:

```bash
echo '.env' >> .gitignore
```

## Custom Endpoints and Gateways

You can route Claude requests through custom API gateways, LiteLLM proxies, or enterprise endpoints such as Databricks AI Gateway. Configure these targets by passing a structured `provider` object under `runtime`.

### Provider Options

```yaml
workflow:
  runtime:
    provider:
      name: claude
      base_url: "https://gateway.example.com"
      auth_token: "${GATEWAY_TOKEN}"
      # For an endpoint that expects an Anthropic key instead, use api_key
      # and omit auth_token. Do not set both.
      # api_key: "${ANTHROPIC_API_KEY}"
```

| Field | Description | Env Fallback |
|-------|-------------|--------------|
| `base_url` | Custom Anthropic-compatible endpoint URL. The SDK appends `/v1/messages` itself — whether the `/v1` prefix belongs in `base_url` depends on the gateway (see the note below) | `ANTHROPIC_BASE_URL` |
| `api_key` | Key sent in `x-api-key` header | `ANTHROPIC_API_KEY` |
| `auth_token` | Token sent in `Authorization: Bearer` header | `ANTHROPIC_AUTH_TOKEN` |

### Configuration Rules and Precedence

- **`base_url` precedence**: YAML `base_url` overrides `ANTHROPIC_BASE_URL`; when omitted, the env var is used.
- **`base_url` and the `/v1` prefix**: the Anthropic SDK appends `/v1/messages` (and `/v1/...` for other endpoints) to `base_url` itself. LiteLLM-style gateways therefore expect `base_url` **without** `/v1` — a `base_url` of `https://gateway.example.com/v1` would send requests to `/v1/v1/messages`. Some gateways (e.g. Databricks AI Gateway) do require the `/v1` prefix in `base_url`. Check your gateway's documentation.
- **Credential precedence**: `api_key` and `auth_token` are resolved together, not independently. Setting **either** in YAML makes the Anthropic SDK skip environment-variable credential resolution entirely, so a YAML `auth_token` also suppresses `ANTHROPIC_API_KEY`, and vice versa. If you set one credential in YAML and expect the other from the environment, it resolves to `None` with no warning.
- **Authentication header selection**: Use `api_key` for standard Anthropic keys (`x-api-key` header). Use `auth_token` for gateways expecting bearer authentication (`Authorization: Bearer` header). **Set exactly one.** If both are configured, the Anthropic SDK does not choose between them: it sends `X-Api-Key` and `Authorization: Bearer` on every request, so your Anthropic key reaches whatever `base_url` points at. Conductor forwards both without arbitrating and logs a warning.

### Startup Connection Validation

`conductor run` probes the endpoint with `client.models.list()` when it lazily constructs the
Claude provider — before the first agent on that provider runs. `conductor doctor --check` /
`--models` run the same probe. `conductor validate` does **not** — it is a static YAML/schema
check that never constructs a provider or contacts the endpoint. Not every Anthropic-compatible
endpoint implements model listing — Azure AI
Foundry's Anthropic endpoint (`https://<resource>.services.ai.azure.com/anthropic`) and some
LiteLLM/Databricks AI Gateway configurations answer it with a 404 while `/v1/messages` (the
endpoint agents actually use) works fine. To avoid failing startup on those endpoints, the probe
only fails when there is positive evidence of a broken setup:

- An unreachable host (connection error) — fails startup.
- Rejected credentials (HTTP 401/403) — fails startup.
- A non-HTTP error (no status code and not a connection error) — fails startup.
- Any other HTTP status (e.g. 404, 400, 405, 429, 5xx) — logs a warning naming the status code
  and **continues**. On these endpoints your credentials are first verified when the first agent
  actually calls `/v1/messages`, and model-discovery-derived features (context-window reporting,
  `conductor doctor --models`) are unavailable since the model list could never be fetched.

### Security Warning

Secrets must always use environment variable interpolation (such as `${ANTHROPIC_API_KEY}` or `${GATEWAY_TOKEN}`), never literal string values. Conductor embeds raw workflow source code inside the `yaml_source` attribute of `workflow_started` events. Hardcoding a literal secret key in YAML exposes it in JSONL event logs and the web dashboard.

### Example Configurations

#### Example 1: LiteLLM or Enterprise Gateway (Bearer Auth)

To route requests through a LiteLLM proxy or Databricks AI Gateway using `base_url` and `auth_token`:

```yaml
workflow:
  name: gateway-workflow
  runtime:
    provider:
      name: claude
      base_url: "https://litellm.internal.company.com"
      auth_token: "${GATEWAY_BEARER_TOKEN}"
    default_model: claude-sonnet-4.5

agents:
  - name: processor
    prompt: "Process this input: {{ workflow.input.text }}"
    routes:
      - to: $end
```

#### Example 2: Direct Anthropic Endpoint with YAML Key

To target the standard Anthropic endpoint while managing `api_key` in YAML:

```yaml
workflow:
  name: direct-anthropic-workflow
  runtime:
    provider:
      name: claude
      api_key: "${ANTHROPIC_API_KEY}"
    default_model: claude-sonnet-4.5

agents:
  - name: processor
    prompt: "Summarize: {{ workflow.input.text }}"
    routes:
      - to: $end
```

## Model Selection

Claude offers multiple model tiers optimized for different use cases. All current Claude models default to a 200K-token context window; the dashboard's "context remaining" bar sources the cap from the Anthropic SDK at runtime, so it always reflects the actual limit your account has access to (rather than a hand-maintained number that can drift), and shows the prompt size of the most recent single API call — not a running total across every call in the agent's execution — against that cap. Beta context modes such as Claude's 1M-token window are not enabled by default in conductor today.

### Available Models

| Model | Best For | Speed | Cost (Input/Output) | Max Output Tokens | Recommended Use |
|-------|----------|-------|---------------------|-------------------|-----------------|
| **claude-sonnet-4.5** | General purpose, most workflows | Medium | $3/$15 per MTok | 16384 (capped by ModelInfo) | **Default recommendation** - stable, avoids deprecation |
| claude-sonnet-4.5-20250929 | Latest features, cutting-edge | Medium | $3/$15 per MTok | 16384 (capped by ModelInfo) | When you need the newest capabilities |
| claude-sonnet-4.5-20241022 | Stable, well-tested | Medium | $3/$15 per MTok | 16384 (capped by ModelInfo) | Production workloads requiring stability |
| claude-opus-4.5 | Complex reasoning, creative tasks | Slowest | $5/$25 per MTok | 16384 (capped by ModelInfo) | Critical analysis, complex decision-making |
| claude-haiku-4.5 | Simple tasks, high volume | Fastest | $1/$5 per MTok | 4096 | Classification, routing, simple Q&A |
| claude-3-opus-20240229 | Legacy - complex reasoning | Slow | $15/$75 per MTok | 4096 | Legacy workflows (not recommended) |

**Note**: Pricing verified as of 2026-02-01 from Anthropic documentation. Always verify current rates at [anthropic.com/pricing](https://www.anthropic.com/pricing) before production deployment.

### Model Naming Patterns

Claude models follow different naming conventions:

- **Latest stable**: `claude-sonnet-4.5` (recommended for stability)
- **Claude 4.5 series**: `claude-sonnet-4.5-YYYYMMDD`
- **Claude 4 series**: `claude-opus-4.5-YYYYMMDD`
- **Claude 3.x series**: `claude-3-5-sonnet-YYYYMMDD`, `claude-3-opus-YYYYMMDD`

The provider will log available models at startup and warn if your requested model is not available.

### Choosing a Model

**For most workflows**: Use `claude-sonnet-4.5`
- Excellent balance of performance and cost
- Automatic updates to latest stable version
- No dated model deprecation risk

**For simple, high-volume tasks**: Use `claude-haiku-4.5`
- 3-5x faster than Sonnet
- 3x cheaper ($1/$5 vs $3/$15 per MTok)
- Best for classification, routing, simple transformations

**For complex reasoning**: Use `claude-opus-4.5`
- Superior performance on multi-step reasoning
- Better at following complex instructions
- Worth the cost for critical workflows

**For latest features**: Use dated model like `claude-sonnet-4.5-20250929`
- Access to newest capabilities
- More predictable behavior (no automatic updates)
- May require migration when deprecated

### Example Configuration

```yaml
workflow:
  runtime:
    provider: claude
    default_model: claude-sonnet-4.5

agents:
  # Use default model
  - name: general_agent
    prompt: "Analyze this data..."

  # Override with Haiku for simple task
  - name: classifier
    model: claude-haiku-4.5
    prompt: "Classify this as positive or negative: {{ input }}"

  # Override with Opus for complex reasoning
  - name: strategic_analyzer
    model: claude-opus-4.5
    prompt: "Develop a comprehensive strategy for..."
```

## Runtime Configuration

The Claude provider supports several runtime configuration options that control model behavior.

### Available Options

| Parameter | Type | Range | Default | Description |
|-----------|------|-------|---------|-------------|
| `temperature` | float | 0.0 - 1.0 | 1.0 | Controls randomness (0=deterministic, 1=creative) |
| `max_tokens` | int | >= 1 | 16384 | Maximum output tokens per response, capped at runtime by `ModelInfo.max_tokens` |

### Temperature

Controls the randomness of responses:

```yaml
workflow:
  runtime:
    provider: claude
    temperature: 0.0  # Deterministic responses
```

**Guidelines**:
- `0.0 - 0.3`: Deterministic, factual responses (data extraction, classification)
- `0.4 - 0.7`: Balanced creativity (general Q&A, analysis)
- `0.8 - 1.0`: Creative responses (brainstorming, content generation)

**Note**: Claude enforces the range [0.0, 1.0]. Values outside this range will cause a validation error.

### Maximum Tokens

Controls the maximum number of OUTPUT tokens Claude can generate:

```yaml
workflow:
  runtime:
    provider: claude
    max_tokens: 4096  # Limit response length
```

**Important**:
- This is output tokens representing response length, not the context window.
- Context window is 200K tokens for all models, which is a separate limit.
- Standard models are capped at runtime by `ModelInfo.max_tokens` from the provider, supporting up to 16384 tokens.
- Exceeding the model limit causes an error.

**Use Cases**:
- Limit to 1024 or 2048 for concise responses.
- Increase to 4096 or up to 16384 for comprehensive reports.
- Reduce for faster responses and lower costs.

### Complete Example

```yaml
workflow:
  name: comprehensive-example
  runtime:
    provider: claude
    default_model: claude-sonnet-4.5
    temperature: 0.7
    max_tokens: 4096

agents:
  - name: analyzer
    prompt: "Analyze the following..."
    routes:
      - to: $end
```

## System Prompt

When an agent defines a `system_prompt`, the Claude provider forwards this value as the native top-level `system` parameter in the Anthropic Messages API. 
Key details of this integration:

- **Consistent Application**: The `system_prompt` is sent on every API call in the agent's execution path, including the main loop, tool-use iterations, interrupt partial output requests, and retries.
- **Empty Prompts**: Any empty or whitespace-only `system_prompt` is normalized to `None` and is not sent to the API.
- **Caching**: Anthropic `cache_control` support for the `system` parameter is not implemented yet and is planned as a follow-up.

## Streaming

The Claude provider streams model text, reasoning, tool lifecycle, and parse-recovery events incrementally through Pydantic AI. The dashboard, console, and JSONL event log receive updates while the agent is running rather than only after completion.

## Extended Thinking

The Claude provider supports Anthropic's extended thinking via the unified
[`reasoning.effort`](../configuration.md#reasoning-effort) field. Set a
workflow-wide default with `runtime.default_reasoning_effort` and/or override
per agent with an `reasoning.effort` block:

```yaml
workflow:
  runtime:
    provider: claude
    default_model: claude-sonnet-4.5
    default_reasoning_effort: medium

agents:
  - name: planner
    reasoning:
      effort: high          # per-agent override
    prompt: "Plan a deployment for {{ workflow.input.service }}"
```

### Effort → thinking budget

The unified effort level is translated into Anthropic's
`messages.create(thinking={"type": "enabled", "budget_tokens": N})` parameter:

| Effort   | Budget tokens |
|----------|---------------|
| `low`    | 2 048         |
| `medium` | 8 192         |
| `high`   | 16 384        |
| `xhigh`  | 32 768        |
| `max`    | 59 904        |

`max` is pinned to `64000 − 4096` — the largest budget that still fits the
default `+ 4096` answer headroom under the 64000-token cap (see
[auto-coercion](#auto-coercion-of-temperature-and-max_tokens) below). At `max`,
the effective `max_tokens` lands exactly on the 64000-token cap.

### Supported models

Extended thinking is only valid on thinking-capable models. The provider
accepts any model whose name starts with one of:

- `claude-3-7-*`
- `claude-opus-4*`
- `claude-sonnet-4*`
- `claude-haiku-4*`

Requesting `reasoning.effort` on any other model raises a `ValidationError` at
startup so you fail fast instead of silently dropping the budget.

### Auto-coercion of `temperature` and `max_tokens`

When extended thinking is enabled, the Anthropic API requires `temperature=1.0`
and a `max_tokens` value large enough to contain both the thinking budget and
the visible response. The provider handles this for you:

- **`temperature`**: coerced to `1.0` (logged at INFO if you configured a
  different value).
- **`max_tokens`**: bumped to `budget + 4096`, capped at `64000` (logged at INFO
  when clamped).

This means you don't need to hand-tune `max_tokens` when raising the effort —
the provider will widen the output budget to fit. If you've explicitly set a
`max_tokens` higher than `budget + 4096`, your value is preserved.

### Reasoning content in events

Any thinking content the model returns is surfaced as `agent_reasoning` events
alongside the regular `agent_message` stream, and shows up in the dashboard
detail panel, the JSONL log, and the `-vv` console output. The Copilot provider
emits the same event shape so workflows that mix providers render consistently.

See [`examples/reasoning-effort.yaml`](../../examples/reasoning-effort.yaml) for
a runnable end-to-end example.

## Context Compaction

The Claude provider supports automatic, client-side context compaction using a tiered strategy. When context usage crosses a calculated threshold, the history is compacted.

### How Compaction Resolves on Claude

*   **Context Window:** The provider queries the Anthropic SDK (`models.list()`, with full pagination) to dynamically retrieve the maximum input tokens for the configured model. If the query fails or a custom `base_url` is configured, it falls back to the `genai-prices` registry, and finally to the default 128,000 tokens fallback.
*   **Output Limit:** The provider queries the effective `max_tokens` sent to the API, which is either explicitly configured under `runtime.max_tokens` (source `settings`) or defaults to 16384 (source `default`, including any adjustments after Claude thinking coercion). This is capped at runtime by `ModelInfo.max_tokens` from the provider metadata (source `provider-cap`).
*   **Trigger Threshold:** Calculated using the formula:
    $$\text{Trigger} = \text{Context Window} - (\text{Output Limit} + \text{Buffer})$$
    where the tool buffer is resolved dynamically from the configured tool limits (defaulting to 40,000 tokens).

For more details on the compaction tiers, hysteresis gap, and usage limits, see the [Workflow Syntax Guide](../workflow-syntax.md#context-compaction).

## Troubleshooting

### Common Errors and Solutions

#### 1. Authentication Errors

**Error**: `AuthenticationError: Invalid API key`

**Solutions**:
- Verify your API key is set: `echo $ANTHROPIC_API_KEY`
- Check the key starts with `sk-ant-`
- Ensure no extra spaces or newlines
- Regenerate the key at [console.anthropic.com](https://console.anthropic.com)

```bash
# Test API key manually
curl https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-4.5","max_tokens":100,"messages":[{"role":"user","content":"Hi"}]}'
```

#### 2. Model Not Found

**Error**: `NotFoundError: model 'claude-xxx' not found`

**Solutions**:
- Check available models: see the provider logs at startup
- Verify model name spelling
- Check if model is deprecated: [Anthropic docs](https://docs.anthropic.com/en/docs/models-overview)

**Valid model names**:
```yaml
# Good
default_model: claude-sonnet-4.5
default_model: claude-sonnet-4.5-20250929

# Bad (typos)
default_model: claude-3.5-sonnet  # Wrong: uses dot instead of dash
default_model: claude-sonnet      # Wrong: missing version number
```

#### 3. Rate Limit Errors

**Error**: `RateLimitError: rate limit exceeded`

**Solutions**:
- **Wait and retry**: The provider automatically retries with exponential backoff
- **Reduce concurrent workflows**: Run fewer workflows simultaneously
- **Upgrade tier**: Check your rate limits at [console.anthropic.com](https://console.anthropic.com)
- **Add delays**: Space out agent executions

**Check rate limits**:
- Free tier: 5 requests/minute
- Tier 1: 50 requests/minute  
- Tier 2+: Higher limits based on usage

#### 4. Temperature Validation Errors

**Error**: `ValidationError: temperature must be between 0.0 and 1.0`

**Solution**: Claude enforces temperature range [0.0, 1.0] (unlike OpenAI which allows 0-2)

```yaml
# Bad
runtime:
  temperature: 1.5  # Error: out of range

# Good
runtime:
  temperature: 1.0  # Maximum allowed
```

#### 5. Max Tokens Exceeded

**Error**: `BadRequestError: max_tokens exceeds model limit`

**Solutions**:
- The maximum output capability of your model is capped at runtime by `ModelInfo.max_tokens` from the provider.
- Adjust `runtime.max_tokens` in your workflow config to match the model capability.

```yaml
# For Haiku
agents:
  - name: simple_task
    model: claude-haiku-4.5
    # Bad: max_tokens: 8192 (exceeds Haiku capability)
    # Good:
    runtime:
      max_tokens: 4096
```

#### 6. Output Schema Validation Errors

**Error**: `OutputValidationError: missing required field 'answer'`

**Solutions**:
- Ensure your prompt clearly requests all output fields
- Use explicit instructions: "Return JSON with fields: answer, confidence"
- Check if Claude returned text instead of structured output
- Review the raw response in logs (set `CONDUCTOR_LOG_LEVEL=DEBUG`)

**Example fix**:
```yaml
agents:
  - name: analyzer
    prompt: |
      Analyze the input and return your response in JSON format with these fields:
      - answer: string (your analysis)
      - confidence: string (high/medium/low)
      
      Input: {{ workflow.input.text }}
    output:
      answer:
        type: string
      confidence:
        type: string
```

#### 7. SDK Version Warnings

**Warning**: `Anthropic SDK version 0.75.0 is older than 0.77.0`

**Solution**: Upgrade the SDK:

```bash
uv add 'anthropic>=0.77.0,<1.0.0'
# or
pip install --upgrade 'anthropic>=0.77.0,<1.0.0'
```

**Warning**: `Anthropic SDK version 1.0.0 is >= 1.0.0`

**Solution**: This provider was tested with 0.77.x. Version 1.0.0 may have breaking changes. Pin to 0.77.x:

```bash
uv add 'anthropic>=0.77.0,<1.0.0'
```

#### 8. `models.list()` Not Supported (Azure AI Foundry, some gateways)

**Warning**: `Could not verify connection via models.list() (HTTP 404): ...`

**Cause**: The endpoint (e.g. Azure AI Foundry's Anthropic endpoint, or a LiteLLM/Databricks-style
gateway) does not implement `/v1/models`, even though `/v1/messages` works fine. This is not
treated as a startup failure — see [Startup Connection
Validation](#startup-connection-validation) above.

**Solutions**:
- No action needed if agents run successfully afterward; the warning is informational.
- If agent execution then fails with an authentication error, your credentials really are wrong —
  check `api_key`/`auth_token` as in [Authentication Errors](#1-authentication-errors) above.
- Context-window reporting and `conductor doctor --models` will be unavailable on this endpoint,
  since they depend on the same model-listing call.

### Debugging Tips

#### Enable Debug Logging

```bash
export CONDUCTOR_LOG_LEVEL=DEBUG
conductor run workflow.yaml
```

This will log:
- Available Claude models at startup
- Full API requests and responses
- Token usage per request
- Retry attempts and delays

#### Test Provider Connection

```bash
conductor doctor --check -p claude
```

This validates:
- API key is set and (on endpoints that implement `/v1/models`) verified against the API — on
  endpoints that don't (e.g. Azure AI Foundry), credentials are instead verified at first agent
  execution; see [Startup Connection Validation](#startup-connection-validation) above
- Provider can connect to Claude API

Separately, `conductor validate workflow.yaml` checks that the workflow YAML is syntactically
correct — it never contacts the Claude endpoint.

#### Check SDK Installation

```python
import anthropic
print(anthropic.__version__)  # Should be >= 0.77.0
```

## Cost Optimization

Claude API charges based on input and output tokens. Here are strategies to minimize costs.

### Pricing Overview

Current pricing (verify at [anthropic.com/pricing](https://www.anthropic.com/pricing)):

| Model | Input (per 1M tokens) | Output (per 1M tokens) | Notes |
|-------|----------------------|------------------------|-------|
| Haiku 4.5 | $1 | $5 | Best value for simple tasks |
| Sonnet 3.5/4.5 | $3 | $15 | Balanced cost/performance |
| Opus 4.5 | $5 | $25 | Premium performance |
| Claude 3 Opus | $15 | $75 | Legacy (not recommended) |

**Cost Example**: 
- 1000 requests to Sonnet 3.5
- 500 input tokens/request = 500K input tokens = $1.50
- 2000 output tokens/request = 2M output tokens = $30
- **Total**: $31.50

### Strategy 1: Choose the Right Model

Use the cheapest model that meets your needs:

```yaml
workflow:
  runtime:
    provider: claude
    
agents:
  # Simple classification: Haiku (3x cheaper)
  - name: categorize
    model: claude-haiku-4.5
    prompt: "Categorize as positive/negative: {{ text }}"

  # General analysis: Sonnet (balanced)
  - name: analyze
    model: claude-sonnet-4.5
    prompt: "Analyze the following..."

  # Complex reasoning: Opus (only when necessary)
  - name: strategic_planning
    model: claude-opus-4.5
    prompt: "Develop a comprehensive strategy..."
```

**Potential savings**: 3-15x by choosing Haiku over Opus for simple tasks

### Strategy 2: Limit Output Tokens

Reduce `max_tokens` to limit response length:

```yaml
runtime:
  max_tokens: 1024  # Instead of default 16384
```

**Potential savings**: 
*   Reducing from 16384 to 1024 tokens can yield up to a 16x reduction in output costs.
*   Example: $15/MTok to $0.94/MTok for 1M output tokens.

### Strategy 3: Optimize Prompts

Shorter prompts = lower input token costs:

```yaml
# Inefficient (verbose)
prompt: |
  You are a helpful assistant. I need you to carefully analyze
  the following text and provide a comprehensive analysis including
  all relevant details. Please be thorough and detailed in your
  response. Here is the text to analyze:
  {{ text }}

# Efficient (concise)
prompt: |
  Analyze: {{ text }}
```

**Potential savings**: 50-70% reduction in input tokens

### Strategy 4: Use Context Mode Wisely

Limit context accumulation to avoid sending redundant data:

```yaml
workflow:
  context:
    mode: explicit  # Only send declared inputs

agents:
  - name: agent1
    input:
      - workflow.input.question  # Only what's needed
```

vs.

```yaml
workflow:
  context:
    mode: accumulate  # Sends ALL prior agent outputs
```

**Potential savings**: 2-10x reduction in input tokens for multi-agent workflows

### Strategy 5: Batch Similar Requests

Group similar requests into a single agent with for-each:

```yaml
agents:
  - name: batch_classifier
    for_each:
      source: workflow.input.items
    prompt: "Classify: {{ item }}"
```

**Benefits**:
- Shared prompt prefix (potential cache hits)
- Lower per-request overhead
- Better rate limit utilization

### Strategy 6: Monitor Usage

Track token usage to identify optimization opportunities:

```bash
# Enable debug logging to see token usage
export CONDUCTOR_LOG_LEVEL=DEBUG
conductor run workflow.yaml
```

Look for:
- High input token counts (optimize prompts/context)
- High output token counts (reduce max_tokens)
- Expensive models for simple tasks (switch to Haiku)

**Monitoring output**:
```
[INFO] Agent 'analyzer' completed: 1245 input tokens, 3421 output tokens
[INFO] Cost estimate: $0.012 input + $0.051 output = $0.063 total
```

### Cost Optimization Checklist

- [ ] Use Haiku for simple tasks (classification, routing)
- [ ] Use Sonnet for general purpose (default)
- [ ] Use Opus only for complex reasoning
- [ ] Set `max_tokens` to minimum necessary
- [ ] Keep prompts concise
- [ ] Use `context: mode: explicit` for multi-agent workflows
- [ ] Monitor token usage with debug logging
- [ ] Batch similar requests with for-each

### Expected Savings

Applying all strategies:
*   **Model selection**: 3 to 15x (Haiku vs Opus)
*   **Max tokens**: up to 16x (1024 vs 16384)
*   **Prompt optimization**: 1.5 to 2x (concise prompts)
*   **Context mode**: 2 to 10x (explicit vs accumulate)

**Total potential savings**: 10 to 100x reduction in costs for optimized workflows
