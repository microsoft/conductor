# Provider Comparison: Copilot vs Claude vs Claude Agent SDK vs Hermes

This guide helps you choose between GitHub Copilot, Anthropic Claude, Claude Agent SDK, and NousResearch Hermes providers for your workflows.

## Quick Comparison

| Feature | Copilot | Claude | Claude Agent SDK | Hermes |
|---------|---------|--------|------------------|--------|
| **Tier** | Stable | Stable | Experimental | Experimental |
| **Context Window** | per-model (SDK-reported) | per-model (SDK-reported) | 200K | per-model |
| **Pricing Model** | Subscription ($10-39/mo) | Pay-per-token | Via Claude Code CLI | Pay-per-token (via hermes) |
| **Setup** | GitHub auth | API key | `claude` CLI auth | API key (model-provider's key) |
| **Model Selection** | GPT-5.2, o1 | Haiku, Sonnet, Opus | Haiku, Sonnet, Opus | Any OpenRouter-style model |
| **Streaming** | Yes | No (Phase 1) | Yes | No |
| **Tool Support** | Yes (MCP, all types) | Yes (MCP, stdio only) | Yes (built-in, CLI-managed) | Yes (hermes internal tools) |
| **MCP Servers** | Yes | Yes (stdio) | No | No |
| **Reasoning / Extended Thinking** | Yes (`reasoning_effort` on session) | Yes (extended `thinking` budget) | Inherits from CLI config | No-op (hermes internal) |
| **Speed** | Fast | Fast | Fast | Depends on model |
| **Output Quality** | Excellent | Excellent | Excellent | Depends on model |
| **Cost Predictability** | High (flat rate) | Variable (usage-based) | Variable | Variable (usage-based) |
| **Structured Output** | Prompt injection | Native | Prompt injection | Prompt injection |
| **Session Resume** | Yes | No | No | No |

> **About the experimental tier.** `claude-agent-sdk` and `hermes` declare
> specific capability carve-outs (no MCP, no per-agent tools allowlist,
> etc.). `conductor validate` catches workflows that depend on those
> features against these providers, and the CLI prints a one-time banner
> when the workflow runs. See
> [docs/providers/experimental.md](./experimental.md) for the stability
> policy and promotion criteria.

## When to Use Copilot

### ✅ Choose Copilot if:

1. **You have a GitHub Copilot subscription** — Already paying $10-39/month, no additional API costs
2. **You need MCP tool support** — Web search, code execution, file operations, external API integrations
3. **You want streaming responses** — Real-time feedback, better UX for long-running workflows
4. **You prefer enterprise support** — GitHub Enterprise integration, SSO, access controls
5. **Heavy usage** — Flat rate beats pay-per-token at scale

### When to Use Copilot

1. **You have a GitHub Copilot subscription** — No additional costs, predictable billing
2. **You need MCP tool support** — Web search, code execution, file operations
3. **You want streaming responses** — Real-time feedback as the model generates
4. **Heavy usage** — Flat rate beats pay-per-token at scale
5. **Enterprise features** — SSO, GitHub Enterprise integration

### Example Copilot Workflow

```yaml
workflow:
  name: copilot-workflow
  runtime:
    provider: copilot
    default_model: gpt-5.2
    mcp_servers:
      web-search:
        command: npx
        args: ["-y", "open-websearch@latest"]
        tools: ["*"]

agents:
  - name: researcher
    tools: [web_search]
    prompt: "Research {{ topic }} using web search"
```

## When to Use Claude

### ✅ Choose Claude if:

1. **You need a large context window** — 200K tokens, all models; great for long documents and multi-agent workflows
2. **You want fine-grained cost control** — Pay only for what you use; scale to zero when idle
3. **You value reasoning quality** — Claude excels at analysis, synthesis, and following complex instructions
4. **Light/intermittent usage** — Pay-per-use is cheaper than a subscription at low volumes
5. **You need reliable structured output** — Native schema enforcement more robust than prompt injection

### Example Claude Workflow

```yaml
workflow:
  name: claude-workflow
  runtime:
    provider: claude
    default_model: claude-sonnet-4.5
    max_tokens: 4096
    temperature: 0.7

agents:
  - name: analyzer
    prompt: "Analyze the following document ({{ document | length }} chars)"
```

## When to Use Claude Agent SDK

### ✅ Choose Claude Agent SDK if:

1. **You want built-in tool support with Claude models**
   - WebSearch, WebFetch, Bash, file operations out of the box
   - No MCP server configuration needed for common tools
   - Full Claude Code toolset available

2. **You already use the `claude` CLI**
   - Authentication handled by the CLI
   - No separate API key management
   - Settings inherited from your Claude Code environment

3. **You want the SDK to manage the agentic loop**
   - Tool execution and structured output handled by the SDK / CLI
   - Less provider code to maintain
   - Native interrupt support
   - **Note:** the SDK does *not* retry transient API errors internally — Conductor classifies SDK errors and surfaces them so workflow-level `retry:` drives recovery.

4. **You need streaming with Claude models**
   - Real-time message streaming (unlike the raw Claude provider)
   - Typed message objects for each event

### Important: Tools and MCP Servers

The `claude-agent-sdk` provider does not bridge workflow-level tools/MCP into the CLI. Concretely:

- `runtime.mcp_servers` — **rejected at the factory** with a clear error. Translation to the CLI's MCP configuration is not implemented. Configure MCP servers through your Claude Code settings instead.
- Per-agent `tools: []` — disables all tools for that agent.
- Per-agent `tools: [list]` — **refused loudly**. Workflow tool names do not translate to Claude CLI tool IDs; silently passing them through would risk granting the wrong native tool.
- Omitting `tools:` entirely — grants the full `claude_code` preset (filesystem, bash, web), matching the bare `claude` CLI experience.
- `temperature` and `max_tokens` are **rejected at the factory** — sampling behavior is controlled by the CLI.

### Example Claude Agent SDK Workflow

```yaml
workflow:
  name: sdk-workflow
  runtime:
    provider: claude-agent-sdk
    default_model: claude-sonnet-4-6

agents:
  - name: researcher
    prompt: "Research {{ topic }} using web search"
```

## When to Use Hermes

> **Experimental Provider** — Hermes is an experimental provider. See
> [Experimental Providers](./experimental.md) for stability policy and
> known limitations.

### ✅ Choose Hermes if:

1. **You want access to many model providers** — Anthropic, OpenAI, and any OpenRouter-supported model via a single provider
2. **You need hermes's built-in tool ecosystem** — Hermes manages its own tools internally; no MCP config required
3. **You're already using hermes-agent** — Integrate your existing hermes workflows with Conductor's orchestration
4. **Model flexibility matters most** — Switch between `anthropic/claude-sonnet-4` and `openai/gpt-4o` by changing a single field

### ✅ Avoid Hermes if:

- You need **MCP server support** — use Copilot or Claude instead
- You need **reliable structured output** — Hermes uses prompt injection; Claude/Copilot use native APIs
- You need **reasoning effort control** — the `reasoning.effort` field is a no-op for Hermes

### Example Hermes Workflow

```yaml
workflow:
  name: hermes-workflow
  runtime:
    provider: hermes
    default_model: anthropic/claude-sonnet-4
    max_agent_iterations: 25

agents:
  - name: researcher
    prompt: "Research the following topic thoroughly: {{ workflow.input.topic }}"
    output:
      findings:
        type: string
    routes:
      - to: $end
```

## Cost Comparison

### Scenario 1: Light Usage (10 hours/month)

**Copilot**:
- Subscription: $10-39/month
- Total: **$10-39/month**

**Claude**:
- ~100 requests/month
- ~1000 tokens/request input, ~2000 tokens/request output
- Sonnet: (0.1M × $3) + (0.2M × $15) = $0.30 + $3.00 = **$3.30/month**
- Haiku: (0.1M × $1) + (0.2M × $5) = $0.10 + $1.00 = **$1.10/month**

**Winner**: Claude (3-35x cheaper)

### Scenario 2: Medium Usage (40 hours/month)

**Copilot**:
- Subscription: $10-39/month
- Total: **$10-39/month**

**Claude**:
- ~500 requests/month
- ~2000 tokens/request input, ~4000 tokens/request output
- Sonnet: (1M × $3) + (2M × $15) = $3.00 + $30.00 = **$33/month**
- Haiku: (1M × $1) + (2M × $5) = $1.00 + $10.00 = **$11/month**

**Winner**: Tie (depends on model choice and subscription tier)

### Scenario 3: Heavy Usage (160+ hours/month)

**Copilot**:
- Subscription: $10-39/month
- Total: **$10-39/month** (flat rate)

**Claude**:
- ~2000 requests/month
- ~3000 tokens/request input, ~5000 tokens/request output
- Sonnet: (6M × $3) + (10M × $15) = $18 + $150 = **$168/month**
- Haiku: (6M × $1) + (10M × $5) = $6 + $50 = **$56/month**

**Winner**: Copilot (3-17x cheaper)

### Cost Optimization Tips

**Copilot**:
- Use the subscription you already have
- Optimize prompts to reduce latency (not cost)
- No per-token optimization needed

**Claude**:
- Use Haiku for simple tasks (3x cheaper than Sonnet)
- Limit `max_tokens` to reduce output costs
- Use `context: mode: explicit` to reduce input tokens

## Feature Comparison

### Context Window

**Copilot**:
- GPT-5.2: 8K tokens
- GPT-5.2 Turbo: 128K tokens
- Model-dependent

**Claude**:
- All models: 200K tokens
- Consistent across tiers
- Better for large documents

**Winner**: Claude (200K vs 128K max)

### Model Selection

**Copilot**:
- `gpt-5.2` - Balanced performance
- `gpt-5.2-turbo` - Faster, larger context
- `gpt-5.2-mini` - Latest, optimized
- `o1-preview` - Advanced reasoning (limited availability)

**Claude**:
- `claude-haiku-4.5` - Fast, cheap
- `claude-sonnet-4.5` - Balanced (default)
- `claude-opus-4.5` - Premium reasoning

**Winner**: Tie (both offer good model tiers)

### Structured Output

**Copilot**:
- Native JSON mode
- Schema validation
- Reliable extraction

**Claude**:
- Tool-based structured output
- JSON fallback parsing
- Highly reliable with tool approach

**Winner**: Tie (both work well)

### Streaming

**Copilot**:
- ✅ Real-time streaming
- Progressive response display
- Better UX for long responses

**Claude**:
- ❌ Not available in Phase 1
- Planned for Phase 2+
- Currently non-streaming only

**Winner**: Copilot (until Claude Phase 2+)

### Tool Support (MCP)

**Copilot**:
- ✅ Full MCP support (stdio, http, sse)
- Web search, code execution, file ops
- Workflow-level and agent-level tools

**Claude**:
- ✅ MCP support for stdio servers
- Uses Conductor's built-in MCPManager
- HTTP/SSE servers not supported

**Winner**: Copilot (broader transport support)

See the [MCP Tools guide](../mcp-tools.md) for details.

### Reasoning / Extended Thinking

Both providers expose a unified [`reasoning.effort`](../configuration.md#reasoning-effort)
field (`low` | `medium` | `high` | `xhigh`) at workflow scope
(`runtime.default_reasoning_effort`) or per agent (`reasoning.effort`).
Conductor translates the value to each provider's native API:

**Copilot**:
- Forwarded as `reasoning_effort` on `CopilotClient.create_session`
- Validated against the model's advertised `supported_reasoning_efforts`

**Claude**:
- Translated to `messages.create(thinking={"type": "enabled", "budget_tokens": N})`
- Effort → budget: low=2048, medium=8192, high=16384, xhigh=32768 tokens
- Restricted to thinking-capable models (`claude-3-7-*`, `claude-opus-4*`,
  `claude-sonnet-4*`, `claude-haiku-4*`)
- Auto-coerces `temperature=1.0` and bumps `max_tokens` to fit the budget

Reasoning content from either provider surfaces as `agent_reasoning` events
in the dashboard, JSONL log, and `-vv` console output.

**Winner**: Tie (both support it; pick the provider on other grounds)

See [`examples/reasoning-effort.yaml`](../../examples/reasoning-effort.yaml).

## Migration Path

### From Copilot to Claude

Minimal changes required:

```yaml
# Before (Copilot)
workflow:
  runtime:
    provider: copilot
    default_model: gpt-5.2

# After (Claude)
workflow:
  runtime:
    provider: claude
    default_model: claude-sonnet-4.5
```

See the [Migration Guide](migration.md) for detailed instructions.

### From Claude to Copilot

Also straightforward:

```yaml
# Before (Claude)
workflow:
  runtime:
    provider: claude
    default_model: claude-sonnet-4.5
    max_tokens: 4096

# After (Copilot)
workflow:
  runtime:
    provider: copilot
    default_model: gpt-5.2
    # Remove Claude-specific fields
```

## Decision Matrix

Use this matrix to decide:

| Your Situation | Recommended Provider |
|----------------|---------------------|
| Already have Copilot subscription | **Copilot** |
| Need MCP tools (web search, code exec) | **Copilot** |
| Need streaming responses | **Copilot** |
| Heavy usage (>160 hrs/mo) | **Copilot** |
| Need 200K context window | **Claude** |
| Light usage (<10 hrs/mo) | **Claude** |
| Want pay-per-use pricing | **Claude** |
| Process long documents | **Claude** |
| Complex reasoning tasks | **Claude** (Opus) |
| Simple high-volume tasks | **Claude** (Haiku 4.5) |
| Already use `claude` CLI | **Claude Agent SDK** |
| Want streaming with Claude | **Claude Agent SDK** |
| Need multi-provider model access | **Hermes** |
| Already using hermes-agent | **Hermes** |
| Want hermes's built-in tool ecosystem | **Hermes** |

## Multi-Provider Strategy

You can use both providers in different workflows:

### Use Case Segregation

```bash
# Heavy-use, tool-enabled workflows → Copilot
conductor run research-workflow.yaml --provider copilot

# Long-document analysis → Claude
conductor run document-analysis.yaml --provider claude

# High-volume simple tasks → Claude Haiku
conductor run classification.yaml --provider claude
```

### Cost Optimization

1. **Copilot**: Production workflows with tools (flat rate)
2. **Claude Haiku**: High-volume batch processing (cheap)
3. **Claude Opus**: Complex one-off analyses (premium quality)

### Redundancy/Fallback

```yaml
workflow:
  runtime:
    provider: copilot  # Primary
    # If Copilot fails, manually retry with Claude
```

## Summary

**Choose Copilot** for:
- ✅ MCP tool support (web search, code execution)
- ✅ Streaming responses
- ✅ Predictable costs (subscription)
- ✅ Heavy usage
- ✅ Enterprise features

**Choose Claude** for:
- ✅ Large context (200K tokens)
- ✅ Pay-per-use pricing
- ✅ Light/intermittent usage
- ✅ Long document processing
- ✅ Reliable structured output
- ✅ Cost optimization (Haiku)

**Choose Claude Agent SDK** for:
- ✅ Built-in tools (WebSearch, Bash, etc.)
- ✅ Streaming with Claude models
- ✅ SDK-managed agentic loop
- ✅ Existing `claude` CLI users
- ✅ No API key management

**Choose Hermes** for:
- ✅ Multi-provider model access (Anthropic, OpenAI, OpenRouter)
- ✅ Hermes's built-in tool ecosystem (no MCP config)
- ✅ Existing hermes-agent workflows
- ✅ Maximum model flexibility

**Bottom line**: All four providers are excellent at what they do. Choose based on your usage patterns, budget, tool requirements, and model preferences. Conductor makes it easy to switch between them or use multiple strategically within a single multi-provider workflow.
