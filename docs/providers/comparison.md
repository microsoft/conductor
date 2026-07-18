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
| **Streaming** | Yes | No (Phase 1) | Yes | Yes |
| **Tool Support** | Yes (MCP, all types) | Yes (MCP, stdio only) | Yes (built-in, CLI-managed) | Yes (hermes toolsets) |
| **MCP Servers** | Yes | Yes (stdio) | No | No |
| **Reasoning / Extended Thinking** | Yes (`reasoning_effort` on session) | Yes (extended `thinking` budget) | Inherits from CLI config | Yes (`reasoning_config`) |
| **Speed** | Fast | Fast | Fast | Depends on model |
| **Output Quality** | Excellent | Excellent | Excellent | Depends on model |
| **Cost Predictability** | High (flat rate) | Variable (usage-based) | Variable | Variable (usage-based) |
| **Multi-provider** | No | Yes (via Conductor) | No | Yes (native) |
| **Agentic Loop** | SDK-managed | Manual (provider code) | SDK-managed (delegated to CLI) | SDK-managed (delegated to hermes) |
| **Structured Output** | Prompt injection | Native | Prompt injection | Prompt injection |
| **Session Resume** | Yes | No | No | Yes |
| **Tool Output Limits** | native SDK spill (large_output) | conductor-side truncation+spill | native CLI env var | N/A |

> **About the experimental tier.** `claude-agent-sdk` and `hermes` declare
> specific capability carve-outs (e.g. no MCP servers). `conductor validate`
> catches workflows that depend on those features against these providers,
> and the CLI prints a one-time banner when the workflow runs. See
> [docs/providers/experimental.md](./experimental.md) for the stability
> policy and promotion criteria.
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
- Workflow-level `tools:` combined with an agent that omits `tools:` — **rejected at `conductor validate`**. The agent would otherwise inherit that non-empty list at runtime and hit the same refusal with a confusing message. Remove the workflow-level `tools:` (so omitting `tools:` grants the preset) or set the agent's `tools: []`.
- Omitting `tools:` entirely (with no workflow-level `tools:`) — grants the full `claude_code` preset (filesystem, bash, web), matching the bare `claude` CLI experience.
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
