# Provider Comparison: Copilot vs Claude

This guide helps you choose between GitHub Copilot and Anthropic Claude providers for your workflows.

## Quick Comparison

| Feature | Copilot | Claude | Winner |
|---------|---------|--------|--------|
| **Context Window** | 8K-128K | 200K (all models) | Claude |
| **Pricing Model** | Subscription ($10-39/mo) | Pay-per-token | Depends |
| **Setup** | GitHub auth | API key | Copilot (easier) |
| **Model Selection** | GPT-5.2, o1 | Haiku, Sonnet, Opus | Tie |
| **Streaming** | Yes | No (Phase 1) | Copilot |
| **Tool Support** | Yes (MCP) | No (Phase 1) | Copilot |
| **Speed** | Fast | Fast | Tie |
| **Output Quality** | Excellent | Excellent | Tie |
| **Cost Predictability** | High (flat rate) | Variable (usage-based) | Copilot |
| **Multi-provider** | No | Yes (via Conductor) | Claude |

## When to Use Copilot

### ✅ Choose Copilot if:

1. **You have a GitHub Copilot subscription**
   - Already paying $10-39/month
   - No additional costs for API usage
   - Predictable monthly billing

2. **You need tool support (MCP)**
   - Web search, code execution, file operations
   - Real-time data access
   - External API integrations

3. **You want streaming responses**
   - Real-time feedback as the model generates
   - Better UX for long-running workflows
   - Progress visibility

4. **You prefer enterprise support**
   - GitHub Enterprise integration
   - SSO and access controls
   - Enterprise SLA and support

5. **You need smaller context windows (cost optimization)**
   - GPT-4: 8K context (cheaper)
   - GPT-4 Turbo: 128K context (when needed)
   - Pay only for subscription, not per token

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

1. **You need a large context window**
   - 200K tokens (all models)
   - Process long documents, code, transcripts
   - Multi-agent workflows with extensive context

2. **You want fine-grained cost control**
   - Pay only for what you use
   - Scale to zero when idle
   - Optimize costs with model selection (Haiku vs Opus)

3. **You value output quality for reasoning tasks**
   - Claude excels at analysis, synthesis, reasoning
   - More verbose explanations
   - Better at following complex instructions

4. **You run low-volume or intermittent workflows**
   - Pay-per-use cheaper than subscription
   - No minimum monthly cost
   - Scale up/down as needed

5. **You want to avoid vendor lock-in**
   - Anthropic API works with multiple tools
   - Easier migration between platforms
   - Future-proof for multi-provider strategies

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
- ✅ Full MCP support
- Web search, code execution, file ops
- Workflow-level and agent-level tools

**Claude**:
- ❌ Not available in Phase 1
- Deferred to Phase 2
- Research needed for compatibility

**Winner**: Copilot (until Claude Phase 2+)

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
| Need tools (web search, code exec) | **Copilot** |
| Need streaming responses | **Copilot** |
| Heavy usage (>160 hrs/mo) | **Copilot** |
| Need 200K context window | **Claude** |
| Light usage (<10 hrs/mo) | **Claude** |
| Want pay-per-use pricing | **Claude** |
| Process long documents | **Claude** |
| Complex reasoning tasks | **Claude** (Opus) |
| Simple high-volume tasks | **Claude** (Haiku 4.5) |

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
- ✅ Tool support (MCP)
- ✅ Streaming responses
- ✅ Predictable costs (subscription)
- ✅ Heavy usage
- ✅ Enterprise features

**Choose Claude** for:
- ✅ Large context (200K tokens)
- ✅ Pay-per-use pricing
- ✅ Light/intermittent usage
- ✅ Long document processing
- ✅ Cost optimization (Haiku)

**Bottom line**: Both are excellent. Choose based on your usage patterns, budget, and feature requirements. Conductor makes it easy to switch between them or use both strategically.
