# Claude Provider Documentation

The Claude provider enables Conductor workflows to use Anthropic's Claude models via the official Anthropic Python SDK.

## Table of Contents

- [Quick Start](#quick-start)
- [API Key Setup](#api-key-setup)
- [Model Selection](#model-selection)
- [Runtime Configuration](#runtime-configuration)
- [Streaming Limitations](#streaming-limitations)
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

## Model Selection

Claude offers multiple model tiers optimized for different use cases. All models support a 200K token context window.

### Available Models

| Model | Best For | Speed | Cost (Input/Output) | Max Output Tokens | Recommended Use |
|-------|----------|-------|---------------------|-------------------|-----------------|
| **claude-sonnet-4.5** | General purpose, most workflows | Medium | $3/$15 per MTok | 8192 | **Default recommendation** - stable, avoids deprecation |
| claude-sonnet-4.5-20250929 | Latest features, cutting-edge | Medium | $3/$15 per MTok | 8192 | When you need the newest capabilities |
| claude-sonnet-4.5-20241022 | Stable, well-tested | Medium | $3/$15 per MTok | 8192 | Production workloads requiring stability |
| claude-opus-4.5 | Complex reasoning, creative tasks | Slowest | $5/$25 per MTok | 8192 | Critical analysis, complex decision-making |
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
| `max_tokens` | int | 1 - 8192 | 8192 | Maximum OUTPUT tokens per response |

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
- This is OUTPUT tokens (response length), not context window
- Context window is 200K tokens for all models (separate limit)
- Sonnet/Opus: maximum 8192 output tokens
- Haiku: maximum 4096 output tokens
- Exceeding the limit causes an error

**Use Cases**:
- Limit to 1024-2048 for concise responses
- Increase to 4096-8192 for comprehensive reports
- Reduce for faster responses and lower costs

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

## Streaming Limitations

**Phase 1 Implementation Status**: The Claude provider currently does NOT support real-time streaming.

### Current Behavior

- All responses are returned after completion (non-streaming)
- The provider uses `client.messages.create()` instead of `client.messages.stream()`
- You will not see partial responses during execution

### Why?

Real-time streaming requires:
1. UI integration for displaying partial responses
2. Event-driven architecture for handling streaming events
3. Buffering and state management for partial content
4. Error recovery during streaming

These features are complex and deferred to Phase 2+ to keep Phase 1 focused on core functionality.

### Workarounds

If you need faster responses:

1. **Reduce `max_tokens`**: Smaller responses complete faster
   ```yaml
   runtime:
     max_tokens: 1024  # Faster than 8192
   ```

2. **Use Haiku models**: 3-5x faster than Sonnet/Opus
   ```yaml
   runtime:
     default_model: claude-haiku-4.5
   ```

3. **Break workflows into smaller agents**: Multiple short responses instead of one long response

### Phase 2+ Timeline

Streaming support is planned for Phase 2 (estimated 2-3 weeks):
- Real-time response streaming
- Terminal UI for partial content display
- Progress indicators and status updates
- Streaming event handling and error recovery

Track progress in the project roadmap or GitHub issues.

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
- **Sonnet/Opus**: Maximum 8192 output tokens
- **Haiku**: Maximum 4096 output tokens

```yaml
# For Haiku
agents:
  - name: simple_task
    model: claude-haiku-4.5
    # Bad: max_tokens: 8192 (exceeds Haiku limit)
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
conductor validate workflow.yaml --provider claude
```

This validates:
- API key is set and valid
- Provider can connect to Claude API
- Workflow YAML is syntactically correct

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
  max_tokens: 1024  # Instead of default 8192
```

**Potential savings**: 
- 8192 → 1024 tokens = 8x reduction in output costs
- Example: $15/MTok → $1.88/MTok for 1M output tokens

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
- **Model selection**: 3-15x (Haiku vs Opus)
- **Max tokens**: 2-8x (1024 vs 8192)
- **Prompt optimization**: 1.5-2x (concise prompts)
- **Context mode**: 2-10x (explicit vs accumulate)

**Total potential savings**: 10-100x reduction in costs for optimized workflows
