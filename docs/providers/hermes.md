# Hermes Provider Documentation

> **Experimental Provider** — Hermes is an experimental provider with known
> capability carve-outs (no MCP servers, no per-agent tools allowlist,
> structured output via prompt injection only). `conductor validate` catches
> workflows that depend on unsupported features, and the CLI prints a one-time
> banner at runtime. See [Experimental Providers](./experimental.md) for the
> stability policy and promotion criteria.

The Hermes provider enables Conductor workflows to use the [NousResearch hermes-agent](https://github.com/NousResearch/hermes-agent) library. Hermes is an agentic AI framework that manages its own tool ecosystem and supports models from multiple providers via OpenRouter-style model identifiers.

## Table of Contents

- [Quick Start](#quick-start)
- [Installation](#installation)
- [Model Format](#model-format)
- [Runtime Configuration](#runtime-configuration)
- [Toolset Control](#toolset-control)
- [Model Routing](#model-routing)
- [Structured Output](#structured-output)
- [Tool Use](#tool-use)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)

## Quick Start

### 1. Install the hermes-agent library

```bash
pip install hermes-agent
```

### 2. Set up API credentials

Hermes reads credentials from its own environment variables depending on the model provider you choose:

```bash
# For Anthropic models (e.g. anthropic/claude-sonnet-4)
export ANTHROPIC_API_KEY=sk-ant-...

# For OpenAI models (e.g. openai/gpt-4o)
export OPENAI_API_KEY=sk-...
```

### 3. Update your workflow

```yaml
workflow:
  name: my-workflow
  runtime:
    provider: hermes
    default_model: anthropic/claude-sonnet-4

agents:
  - name: assistant
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

## Installation

Hermes is an **optional dependency** — Conductor works without it. Install it only when you want to use the hermes provider:

```bash
pip install hermes-agent
```

If the library is not installed and you try to use `provider: hermes`, Conductor raises a `ProviderError` with an install hint at startup.

## Model Format

Hermes uses OpenRouter-style model identifiers in the form `provider/model-name`:

| Format | Example |
|--------|---------|
| `anthropic/model` | `anthropic/claude-sonnet-4` |
| `openai/model` | `openai/gpt-4o` |
| `openrouter/provider/model` | `openrouter/anthropic/claude-sonnet-4` |

Set the default model for all agents via `runtime.default_model`, or override per-agent with `model:`:

```yaml
workflow:
  runtime:
    provider: hermes
    default_model: anthropic/claude-sonnet-4

agents:
  - name: fast_task
    model: openai/gpt-4o-mini    # Override for this agent
    prompt: "Classify: {{ text }}"
```

If `model` is omitted entirely (neither per-agent nor `default_model`), hermes uses its own configured default model.

## Runtime Configuration

| Parameter | Forwarded to AIAgent | Default | Description |
|-----------|----------------------|---------|-------------|
| `default_model` | `model=` | hermes default | Model in `provider/model` format |
| `max_agent_iterations` | `max_iterations=` | 90 | Maximum tool-calling iterations per agent |
| `max_tokens` | `max_tokens=` | hermes default | Maximum output tokens |
| `temperature` | `temperature=` | hermes default | Sampling temperature |
| `max_session_seconds` | asyncio timeout | no limit | Wall-clock deadline per agent execution |

```yaml
workflow:
  runtime:
    provider: hermes
    default_model: anthropic/claude-sonnet-4
    max_agent_iterations: 25   # Limit tool iterations (default: 90)
    max_tokens: 4096
    temperature: 0.7
```

### Per-Agent Overrides

```yaml
agents:
  - name: light_task
    model: openai/gpt-4o-mini
    max_agent_iterations: 5    # Fewer iterations for simple tasks
    prompt: "Summarize: {{ text }}"
```

## Toolset Control

By default, hermes loads its full set of built-in tools (approximately 33). Use the `hermes_toolsets` provider setting to restrict which toolsets are available across the workflow.

> **Important**: The per-agent `tools:` field uses Conductor workflow tool names, which do not translate to Hermes toolset names. Setting a non-empty `tools:` list on an agent will raise a validation error. Use `tools: []` to disable all tools for a specific agent, or `hermes_toolsets` to restrict toolsets at the provider level.

| Configuration | Effect |
|---------------|--------|
| _(no `hermes_toolsets`)_ | All hermes tools active (default) |
| `hermes_toolsets: [web, filesystem]` | Only named toolsets enabled |
| `hermes_toolsets: []` | No tools for any agent |
| Per-agent `tools: []` | No tools for that specific agent |

```yaml
workflow:
  runtime:
    provider:
      name: hermes
      hermes_toolsets: [web, filesystem]  # Restrict at provider level

agents:
  - name: judgment_only
    tools: []          # Disables all hermes tools for this agent
    prompt: "Based on this data, what do you conclude? {{ context }}"

  - name: web_researcher
    prompt: "Research: {{ workflow.input.topic }}"
    # Gets the provider-level toolsets (web, filesystem)
```

**Why this matters**: Loading all 33 tools inflates the system prompt by ~25k tokens per agent step. For workflows that only need specific toolsets, restricting via `hermes_toolsets` reduces input token costs significantly.

## Model Routing

To route requests through a custom endpoint (e.g. OpenRouter, a litellm gateway, or a corporate API proxy), use the structured `provider:` config:

```yaml
workflow:
  runtime:
    provider:
      name: hermes
      base_url: "https://openrouter.ai/api/v1"
      api_key: "${OPENROUTER_API_KEY}"
    default_model: anthropic/claude-sonnet-4
```

Both `base_url` and `api_key` are forwarded directly to `AIAgent`. The `api_key` value supports `${ENV_VAR}` interpolation in YAML so the literal secret never appears in event logs or checkpoints.

## Structured Output

Hermes does not have a native structured output API. When an agent declares an `output:` schema, Conductor automatically appends a JSON instruction to the prompt:

```
Respond ONLY with a valid JSON object. Do not include any explanation,
markdown, or text outside the JSON object.
```

The response is then parsed and validated against your schema as usual:

```yaml
agents:
  - name: analyzer
    prompt: |
      Analyze the following text and return your findings.
      Text: {{ workflow.input.text }}
    output:
      sentiment:
        type: string
        description: "positive, negative, or neutral"
      confidence:
        type: number
        description: "0.0 to 1.0"
    routes:
      - to: $end
```

**Note**: Because structured output relies on prompt engineering rather than a native API, reliability can vary. For workflows where schema compliance is critical, the `copilot` or `claude` providers offer more robust structured output.

## Tool Use

Hermes manages its own toolsets internally. Conductor's per-agent `tools:` field contains workflow tool names (resolved via `runtime.tools`) — these are a different vocabulary from Hermes toolset names and cannot be forwarded. Use `hermes_toolsets` in provider settings to control which Hermes toolsets are active — see [Toolset Control](#toolset-control) above.

**Per-agent `tools: []`** is supported and disables all tools for that agent (judgment-only mode).

**Isolation flags**: Conductor always passes `skip_context_files=True`, `skip_memory=True`, and `quiet_mode=True` to the hermes library. This prevents hermes from loading workspace files (`AGENTS.md`, etc.) or its own persistent memory — the conductor workflow YAML and rendered prompts are the sole source of context.

**MCP servers**: The hermes provider does not support Conductor's `runtime.mcp_servers` configuration. Hermes has its own tool ecosystem separate from MCP.

## Limitations

| Limitation | Details |
|------------|---------|
| **No MCP server support** | `runtime.mcp_servers` is ignored; hermes uses its own tools |
| **No per-agent tools allowlist** | Per-agent `tools: [names]` is rejected; use `hermes_toolsets` in provider settings |
| **Structured output via prompt** | Less reliable than native schema enforcement (copilot/claude) |

## Troubleshooting

### Hermes library not installed

**Error**: `ProviderError: Hermes provider requires the hermes-agent package`

**Fix**:
```bash
pip install hermes-agent
```

### Model not found

**Error**: `ProviderError: Hermes agent execution failed (model='anthropic/...'): ...`

**Symptoms**: Hermes returns `failed: true` in the result dict.

**Fix**: Verify the model identifier follows the `provider/model` format and that the corresponding API key is set:
```bash
# Verify key is set
echo $ANTHROPIC_API_KEY
echo $OPENAI_API_KEY
```

### Output schema validation failures

**Error**: `ValidationError: missing required field 'answer'`

**Cause**: The model returned text that wasn't valid JSON, or JSON that didn't match the schema.

**Fix**: Make the prompt more explicit, or use the `claude` or `copilot` provider for strict schema compliance:
```yaml
agents:
  - name: analyzer
    prompt: |
      Analyze the input and respond ONLY with a JSON object with these exact fields:
      - sentiment: string (positive/negative/neutral)
      - confidence: number (0.0-1.0)

      Input: {{ workflow.input.text }}
```

### Enable debug logging

```bash
export CONDUCTOR_LOG_LEVEL=DEBUG
conductor run workflow.yaml
```

This logs:
- The resolved model and iteration limits per agent
- The full prompt sent to hermes (including the JSON instruction if applicable)
- Token counts from the hermes result
- The raw `final_response` before schema validation
