# OpenAI Provider Documentation

The OpenAI provider enables Conductor workflows to execute agents using OpenAI's models via Pydantic AI (`pydantic-ai` package, `OpenAIChatModel`).

## Table of Contents

- [Quick Start](#quick-start)
- [Architecture & Internal Design](#architecture--internal-design)
- [API Key Setup & Precedence](#api-key-setup--precedence)
- [Custom Endpoints and Gateways](#custom-endpoints-and-gateways)
- [Disambiguation: `provider: openai` vs Copilot `type: openai`](#disambiguation-provider-openai-vs-copilots-type-openai)
- [Model Selection & Runtime Configuration](#model-selection--runtime-configuration)
- [Reasoning Effort Matrix](#reasoning-effort-matrix)
- [MCP Tools Support](#mcp-tools-support)
- [Troubleshooting](#troubleshooting)

## Quick Start

### 1. Set up your API key

```bash
export OPENAI_API_KEY=sk-...
```

### 2. Update your workflow

```yaml
workflow:
  name: my-openai-workflow
  runtime:
    provider: openai
    default_model: gpt-5-mini

agents:
  - name: assistant
    model: gpt-5-mini
    prompt: |
      Answer the following question: {{ workflow.input.question }}
    output:
      answer:
        type: string
    routes:
      - to: $end
```

### 3. Run your workflow

```bash
conductor run my-openai-workflow.yaml --input question="What is Python?"
```

## Architecture & Internal Design

The OpenAI provider uses the shared Pydantic AI execution loop (`src/conductor/providers/_pydantic_ai/runner.py`). `OpenAIProvider` in `src/conductor/providers/openai.py` implements the `AgentProvider` interface and delegates agent execution to `run_agent_pipeline()`.

Key architectural properties:
- **Chat Completions API Only**: Speaks Chat Completions endpoint. There is no support for the OpenAI Responses API.
- **Shared Runner**: Utilizes the shared Pydantic AI runner pipeline (`run_agent_pipeline`), sharing toolset bridges, event callbacks, interrupts, and retry contracts with `ClaudeProvider`.
- **Eager Skill Injection**: OpenAI's API has no native skill-directory surface; skill files (`SKILL.md` and references) are eagerly injected into the prompt envelope by `AgentExecutor`.
- **No Native Plugins**: `plugins: False` capability. Subagents and plugin MCP surfaces are not natively supported.

## API Key Setup & Precedence

### Setting the API Key

You can supply the API key via environment variable or YAML:

```bash
export OPENAI_API_KEY=sk-...
```

Or in YAML:

```yaml
workflow:
  runtime:
    provider:
      name: openai
      api_key: "${OPENAI_API_KEY}"
```

### Precedence Rules

1. **YAML over Environment**: Values explicitly configured in YAML (`api_key`, `base_url`) override environment variables (`OPENAI_API_KEY`, `OPENAI_BASE_URL`).
2. **Environment Fallback**: When omitted in YAML, `OPENAI_API_KEY` and `OPENAI_BASE_URL` are read from the environment.
3. **No Ambient Rerouting**: Ambient environment variables (`OPENAI_API_KEY`, `OPENAI_BASE_URL`) never divert an unconfigured provider. If a workflow specifies `provider: copilot` (or omits provider), ambient `OPENAI_*` variables have zero effect and will not reroute execution.

## Custom Endpoints and Gateways

The OpenAI provider can route requests to any OpenAI-compatible API gateway, local model server, or proxy.

### Provider Configuration Schema

```yaml
workflow:
  runtime:
    provider:
      name: openai
      base_url: "http://localhost:11434/v1"
      api_key: "ollama"
```

| Field | Description | Env Fallback |
|-------|-------------|--------------|
| `base_url` | Custom OpenAI-compatible base URL (typically ending in `/v1`) | `OPENAI_BASE_URL` |
| `api_key` | API key for authentication | `OPENAI_API_KEY` |

### Recipe: Omniroute-style Proxy

```yaml
workflow:
  name: omniroute-workflow
  runtime:
    provider:
      name: openai
      base_url: "https://omniroute.example.com/v1"
      api_key: "${OMNIROUTE_API_KEY}"
    default_model: gpt-4o
```

### Recipe: OpenRouter

```yaml
workflow:
  name: openrouter-workflow
  runtime:
    provider:
      name: openai
      base_url: "https://openrouter.ai/api/v1"
      api_key: "${OPENROUTER_API_KEY}"
    default_model: meta-llama/llama-3.3-70b-instruct
```

### Recipe: Ollama

```yaml
workflow:
  name: ollama-workflow
  runtime:
    provider:
      name: openai
      base_url: "http://localhost:11434/v1"
      api_key: "ollama"  # Required string; Ollama ignores value but SDK expects one
    default_model: llama3.1
```

### Recipe: vLLM or LM Studio

```yaml
workflow:
  name: local-llm-workflow
  runtime:
    provider:
      name: openai
      # vLLM default: http://localhost:8000/v1
      # LM Studio default: http://localhost:1234/v1
      base_url: "http://localhost:8000/v1"
      api_key: "vllm"
    default_model: mistralai/Mistral-7B-Instruct-v0.3
```

## Disambiguation: `provider: openai` vs Copilot `type: openai`

Conductor offers two ways to use OpenAI-compatible models. They target different runtime engines:

> **Important Difference:**
>
> 1. **Native OpenAI Provider (`provider: openai` or `name: openai`)**:
>    Executes directly against the OpenAI Chat Completions API using Pydantic AI and Python's `openai` SDK. Supports full temperature range (0.0 to 2.0) and uses Conductor's shared Pydantic AI runner.
>
> 2. **Copilot Custom Routing (`provider: { name: copilot, type: openai, ... }`)**:
>    Routes the GitHub Copilot SDK to an OpenAI-compatible wire endpoint. Uses GitHub Copilot as the underlying agentic engine.

## Model Selection & Runtime Configuration

### Chat Completions Only

The OpenAI provider strictly uses the OpenAI Chat Completions endpoint. The OpenAI Responses API is not supported.

### Temperature Range (0.0 – 2.0)

Unlike the Claude and Copilot providers which cap temperature at `1.0`, the OpenAI provider accepts temperatures from `0.0` to `2.0`.

```yaml
workflow:
  runtime:
    provider: openai
    default_model: gpt-5-mini
    temperature: 1.5  # Valid for OpenAI (0.0 to 2.0 range)
```

`conductor validate` enforces provider-aware temperature bounds: values > 1.0 raise validation errors for Claude/Copilot but are permitted for OpenAI.

## Reasoning Effort Matrix

OpenAI reasoning models (such as `o1`, `o3-mini`) support the `reasoning.effort` setting.

| Effort Level | Supported by OpenAI | Note |
|--------------|----------------------|------|
| `low` | Yes (reasoning models) | Fast reasoning |
| `medium` | Yes (reasoning models) | Balanced reasoning |
| `high` | Yes (reasoning models) | Deep reasoning |
| `xhigh` | **No (Rejected)** | Only `low`/`medium`/`high` are accepted by the GPT-5.1-Codex-Max generation and by `o1`/`o3-mini`/`o4-mini`; `xhigh` is rejected. |
| `max` | **No (Rejected)** | Raises `ValidationError` |

Only reasoning models accept `reasoning.effort`; on a non-reasoning model such as `gpt-4o` the setting is validated against the model and also raises `ValidationError`.

```yaml
workflow:
  runtime:
    provider: openai
    default_model: o3-mini
    default_reasoning_effort: medium  # low, medium, high, xhigh
```

Attempting to set `reasoning.effort: max` with the OpenAI provider will be rejected during validation or execution.

## MCP Tools Support

The OpenAI provider supports stdio MCP servers via Conductor's `MCPManager`.

```yaml
workflow:
  runtime:
    provider: openai
    default_model: gpt-5-mini
    mcp_servers:
      fetch:
        command: uvx
        args: ["mcp-server-fetch"]
```

HTTP and SSE MCP server types are not supported by the OpenAI provider (`stdio` only).

## Troubleshooting

### Missing API Key

If no API key is found in environment or YAML:

```text
ValidationError: OPENAI_API_KEY environment variable is not set and no api_key was provided
```
