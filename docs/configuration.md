# Configuration Documentation

This document describes the runtime configuration options available in Conductor workflows.

## Runtime Configuration

The `runtime` section of your workflow defines provider settings and global defaults.

### Basic Structure

```yaml
workflow:
  runtime:
    provider: copilot  # or 'claude'
    default_model: gpt-5.2
    # Provider-specific settings...
```

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

## Copilot-Specific Configuration

### MCP Servers

Configure Model Context Protocol servers for tool access:

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

**Note**: MCP servers not available with Claude provider in Phase 1

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
    max_iterations: 10  # Default: 10, max: 100
    timeout_seconds: 600  # Default: 600, max: 3600
```

**max_iterations**:
- Prevents infinite loops
- Counts agent executions in routing cycles

**timeout_seconds**:
- Total workflow timeout
- Includes all agent executions

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
3. **Test with dry-run** before production

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

### "MCP servers not supported" (Claude)

Remove `mcp_servers` when using Claude provider (Phase 1):

```yaml
runtime:
  provider: claude
  # Remove mcp_servers section
```

## See Also

- [Claude Provider Documentation](providers/claude.md)
- [Provider Comparison](providers/comparison.md)
- [Migration Guide](providers/migration.md)
- [Workflow Syntax](workflow-syntax.md)
