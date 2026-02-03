# Migration Guide: Copilot to Claude

This guide provides a step-by-step path to migrate workflows from GitHub Copilot to Anthropic Claude.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Configuration Changes](#configuration-changes)
- [Model Selection & Mapping](#model-selection--mapping)
- [Behavioral Differences](#behavioral-differences)
- [Testing Strategy](#testing-strategy)
- [Common Pitfalls](#common-pitfalls)
- [Rollback Procedures](#rollback-procedures)

## Prerequisites

### Before You Begin

1. **Anthropic API key**: Get one from [console.anthropic.com](https://console.anthropic.com)
2. **SDK installed**: `uv add 'anthropic>=0.77.0,<1.0.0'`
3. **Backup workflows**: Save copies of working Copilot workflows
4. **Test environment**: Non-production workspace for testing

### What You'll Need

- Access to your workflow YAML files
- Understanding of your workflow behavior/output expectations
- Time to test and validate (plan 30-60 minutes per workflow)

## Configuration Changes

### Step 1: Update Provider

Change the `provider` field from `copilot` to `claude`:

```yaml
# Before
workflow:
  runtime:
    provider: copilot

# After
workflow:
  runtime:
    provider: claude
```

### Step 2: Set API Key

Claude requires an API key (Copilot uses GitHub auth):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Add to your shell profile for persistence:

```bash
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.zshrc
```

### Step 3: Map Model Names

Update `default_model` and per-agent `model` fields:

```yaml
# Before (Copilot)
workflow:
  runtime:
    default_model: gpt-5.2

agents:
  - name: analyzer
    model: gpt-5.2-turbo

# After (Claude)
workflow:
  runtime:
    default_model: claude-sonnet-4.5

agents:
  - name: analyzer
    model: claude-sonnet-4.5  # See model mapping table below
```

### Step 4: Update Runtime Configuration

Claude has different configuration parameters:

```yaml
# Before (Copilot)
workflow:
  runtime:
    provider: copilot
    default_model: gpt-5.2
    temperature: 0.7
    max_tokens: 4096

# After (Claude)
workflow:
  runtime:
    provider: claude
    default_model: claude-sonnet-4.5
    temperature: 0.7  # Keep this (Claude also uses 0.0-1.0)
    max_tokens: 4096  # Controls output length (Claude-specific meaning)
```

**Key changes**:
- `max_tokens` now controls output length (different from Copilot's context trimming)

### Step 5: Remove Copilot-Specific Features

**MCP Servers** (tools) are not supported in Claude Phase 1:

```yaml
# Before (Copilot)
workflow:
  runtime:
    mcp_servers:
      web-search:
        command: npx
        args: ["-y", "open-websearch@latest"]
        tools: ["*"]

# After (Claude) - Remove this section
workflow:
  runtime:
    # mcp_servers not supported in Phase 1
```

**Agent tools** must also be removed:

```yaml
# Before (Copilot)
agents:
  - name: researcher
    tools: [web_search, code_exec]

# After (Claude)
agents:
  - name: researcher
    # Remove tools field
```

### Complete Example

**Before (Copilot)**:
```yaml
workflow:
  name: research-workflow
  runtime:
    provider: copilot
    default_model: gpt-5.2
    temperature: 0.7
    max_tokens: 4096
    mcp_servers:
      web-search:
        command: npx
        args: ["-y", "open-websearch@latest"]
        tools: ["*"]

agents:
  - name: researcher
    model: gpt-5.2-turbo
    tools: [web_search]
    prompt: "Research {{ topic }}"
```

**After (Claude)**:
```yaml
workflow:
  name: research-workflow
  runtime:
    provider: claude
    default_model: claude-sonnet-4.5
    temperature: 0.7
    max_tokens: 4096
    # Remove mcp_servers

agents:
  - name: researcher
    model: claude-sonnet-4.5
    # Remove tools
    prompt: "Research {{ topic }}"
```

## Model Selection & Mapping

### Model Mapping Table

Map your Copilot models to Claude equivalents based on use case:

| Copilot Model | Claude Equivalent | Reasoning | Cost Impact |
|---------------|------------------|-----------|-------------|
| `gpt-5.2` | `claude-sonnet-4.5` | Balanced performance, most workflows | Similar |
| `gpt-5.2-turbo` | `claude-sonnet-4.5` | General purpose, large context | Similar |
| `gpt-5.2-mini` | `claude-sonnet-4.5` | Standard model, widely used | Cheaper (Claude) |
| `gpt-3.5-turbo` | `claude-haiku-4.5` | Fast, cheap, simple tasks | Cheaper (Claude) |
| `o1-preview` | `claude-opus-4.5` | Advanced reasoning, complex tasks | More expensive |

### Model Selection Guidelines

**For most workflows**: Use `claude-sonnet-4.5`
- Direct replacement for GPT-5.2
- Excellent performance/cost balance
- 200K context (vs GPT-5.2 Turbo's 128K)

**For simple, high-volume tasks**: Use `claude-haiku-4.5`
- Replacement for GPT-3.5 Turbo
- 3-5x faster, 3x cheaper
- Classification, routing, simple Q&A

**For complex reasoning**: Use `claude-opus-4.5`
- Replacement for o1-preview
- Superior multi-step reasoning
- Worth the cost for critical workflows

### Context Window Comparison

| Copilot Model | Context | Claude Model | Context | Advantage |
|---------------|---------|--------------|---------|-----------|
| GPT-4 | 8K | Haiku/Sonnet/Opus | 200K | Claude (+192K) |
| GPT-4 Turbo | 128K | Haiku/Sonnet/Opus | 200K | Claude (+72K) |
| GPT-4o | 128K | Haiku/Sonnet/Opus | 200K | Claude (+72K) |

**Benefit**: Claude provides more context across all model tiers.

## Behavioral Differences

### 1. Temperature Range

**Copilot (OpenAI)**: 0.0 - 2.0
**Claude**: 0.0 - 1.0 (enforced by SDK)

**Migration**:
```yaml
# If you used temperature > 1.0
# Before (Copilot)
runtime:
  temperature: 1.5

# After (Claude) - Clamp to 1.0
runtime:
  temperature: 1.0  # Maximum allowed
```

### 2. Max Tokens Requirement

**IMPORTANT**: The `max_tokens` field in RuntimeConfig has DIFFERENT meanings for Claude vs other providers:
- **Copilot/OpenAI**: Context window trimming (optional, handled by workflow engine)
- **Claude**: Maximum OUTPUT tokens per response (required by Claude API)

**Migration**:
```yaml
# Before (Copilot) - max_tokens for context trimming
runtime:
  provider: copilot
  max_tokens: 4096  # Optional: trim context to fit window

# After (Claude) - max_tokens for output generation
runtime:
  provider: claude
  max_tokens: 8192  # Required: max response length
```

**Recommendation**: 
- Always specify `max_tokens` for Claude (default: 8192)
- Understand it controls OUTPUT length, not context window (Claude has 200K context)
- Use lower values (1024-2048) for concise responses, higher (4096-8192) for detailed output

### 3. Output Verbosity

**Claude** tends to be more verbose and explanatory than GPT-4:
- More detailed reasoning
- More explicit step-by-step thinking
- Longer responses for the same prompt

**Mitigation**:
1. Reduce `max_tokens` to enforce conciseness
2. Update prompts: "Answer concisely" or "Be brief"
3. Use Haiku for simple tasks (naturally more concise)

**Example**:
```yaml
agents:
  - name: analyzer
    prompt: |
      Answer the following question CONCISELY (2-3 sentences max):
      {{ question }}

workflow:
  runtime:
    max_tokens: 512  # Enforce brevity
```

### 4. System Prompt Sensitivity

**Claude** is more sensitive to system prompts than GPT-4:
- Follows system instructions more strictly
- May refuse or question problematic requests more often
- Better at maintaining persona/role

**Best practice**: Use clear, well-defined system prompts:

```yaml
agents:
  - name: analyst
    system_prompt: |
      You are a financial analyst. Provide objective, data-driven analysis.
      Do not make investment recommendations.
```

### 5. Streaming (Not Available)

**Copilot**: Real-time streaming supported
**Claude**: Phase 1 does NOT support streaming

**Impact**:
- No partial responses during execution
- Longer wait for first output
- Cannot cancel mid-generation

**Workarounds**:
1. Reduce `max_tokens` for faster responses (less to generate)
2. Use Haiku models (3-5x faster)
3. Break workflows into smaller agents

### 6. Tool Calling (Not Available)

**Copilot**: Full MCP tool support
**Claude**: Phase 1 does NOT support tools/MCP

**Impact**:
- No web search, code execution, file operations
- Cannot use external APIs via tools
- Agents are isolated (no external data)

**Workarounds**:
1. Pre-fetch data and pass as workflow input
2. Split tool-dependent workflows into separate steps
3. Wait for Phase 2 (tools support planned)

## Testing Strategy

### Phase 1: Validation Testing

Verify workflows run without errors:

```bash
# 1. Validate YAML syntax
conductor validate workflow.yaml

# 2. Dry-run to check execution plan
conductor run workflow.yaml --dry-run --provider claude

# 3. Test with minimal input
conductor run workflow.yaml --provider claude --input test="Hello"
```

### Phase 2: Output Comparison

Compare outputs side-by-side:

```bash
# 1. Run with Copilot (baseline)
conductor run workflow.yaml --provider copilot --input question="What is Python?" > copilot-output.json

# 2. Run with Claude (comparison)
conductor run workflow.yaml --provider claude --input question="What is Python?" > claude-output.json

# 3. Compare outputs
diff copilot-output.json claude-output.json
```

**What to check**:
- ✅ Both outputs contain required fields
- ✅ Outputs are semantically equivalent (content may differ)
- ✅ Claude output meets quality expectations
- ⚠️ Claude may be more verbose (expected)

### Phase 3: Acceptance Testing

Define acceptance criteria and validate:

```yaml
# acceptance-criteria.yaml
test_cases:
  - input:
      question: "What is Python?"
    expected_output:
      answer: # Contains "programming language"
      confidence: # One of: high, medium, low
  - input:
      question: "Explain quantum computing"
    expected_output:
      answer: # Contains "quantum mechanics" or "qubits"
      confidence: # high or medium
```

**Validation script** (pseudocode):
```python
for test_case in test_cases:
    result = run_workflow(test_case.input, provider="claude")
    assert all(check(result, expected) for expected in test_case.expected_output)
```

### Phase 4: Regression Testing

Ensure existing tests still pass:

```bash
# If you have existing tests
pytest tests/test_workflows.py --provider claude

# Or manual regression checklist:
# - Test all documented workflows
# - Test edge cases (empty input, max tokens, etc.)
# - Test error handling (invalid input, API failures)
```

### Phase 5: Performance Testing

Compare latency and throughput:

```bash
# Copilot baseline
time conductor run workflow.yaml --provider copilot --input question="Test"

# Claude comparison
time conductor run workflow.yaml --provider claude --input question="Test"
```

**Metrics to track**:
- Response time (end-to-end)
- Token usage (input/output)
- Cost per request
- Error rate

## Common Pitfalls

### Pitfall 1: Forgetting to Set API Key

**Error**: `AuthenticationError: Invalid API key`

**Solution**:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
# Verify it's set
echo $ANTHROPIC_API_KEY
```

### Pitfall 2: Using Copilot Model Names

**Error**: `NotFoundError: model 'gpt-5.2' not found`

**Solution**: Update all model references:
```yaml
# Bad
model: gpt-5.2

# Good
model: claude-sonnet-4.5
```

### Pitfall 3: Temperature > 1.0

**Error**: `ValidationError: temperature must be between 0.0 and 1.0`

**Solution**: Clamp to 1.0:
```yaml
# Bad
temperature: 1.5

# Good
temperature: 1.0
```

### Pitfall 4: Missing max_tokens

**Error**: `BadRequestError: max_tokens is required`

**Solution**: Always specify:
```yaml
runtime:
  max_tokens: 8192
```

### Pitfall 5: Expecting Tools to Work

**Error**: Workflow doesn't error but produces wrong results (no tool calls)

**Solution**: Remove tools from Phase 1 workflows:
```yaml
# Remove mcp_servers and agent tools fields
```

### Pitfall 6: Expecting Streaming

**Behavior**: Long wait with no partial output

**Solution**: 
1. Accept non-streaming in Phase 1
2. Reduce `max_tokens` for faster responses
3. Use Haiku models

### Pitfall 7: Cost Surprises

**Issue**: Higher costs than expected with subscription

**Solution**: Monitor token usage and optimize:
- Use Haiku for simple tasks
- Reduce `max_tokens` to limit response length
- Use `context: mode: explicit` to reduce input tokens

## Rollback Procedures

### If Migration Fails

**Option 1: Quick Rollback**

Revert YAML changes and switch back:

```bash
# Restore original workflow
cp workflow.yaml.backup workflow.yaml

# Run with Copilot
conductor run workflow.yaml --provider copilot
```

**Option 2: Keep Both Versions**

Maintain separate workflow files:

```bash
workflow-copilot.yaml  # Original
workflow-claude.yaml   # Migrated

# Use as needed
conductor run workflow-copilot.yaml --provider copilot
conductor run workflow-claude.yaml --provider claude
```

**Option 3: Gradual Migration**

Migrate one agent at a time:

```yaml
agents:
  # Keep working Copilot agents
  - name: agent1
    model: gpt-5.2

  # Test Claude on one agent
  - name: agent2
    model: claude-sonnet-4.5
```

### Monitoring After Migration

Track these metrics for 1-2 weeks:

1. **Error rate**: Should stay similar or improve
2. **Output quality**: Validate with spot-checks
3. **Cost**: Monitor token usage and costs
4. **Latency**: Track response times

### Rollback Triggers

Consider rolling back if:

- ❌ Error rate increases >20%
- ❌ Output quality degrades significantly
- ❌ Costs exceed budget by >50%
- ❌ Latency increases >2x
- ❌ Critical workflows break

## Migration Checklist

Use this checklist for each workflow:

### Configuration
- [ ] Change `provider: copilot` → `provider: claude`
- [ ] Set `ANTHROPIC_API_KEY` environment variable
- [ ] Map model names (GPT → Claude)
- [ ] Understand `max_tokens` meaning change (context → output length)
- [ ] Remove `mcp_servers` section
- [ ] Remove agent `tools` fields

### Testing
- [ ] Validate YAML syntax
- [ ] Run dry-run mode
- [ ] Test with sample input
- [ ] Compare output with Copilot baseline
- [ ] Run acceptance tests
- [ ] Check performance (latency, tokens, cost)

### Documentation
- [ ] Update workflow documentation
- [ ] Document any prompt changes
- [ ] Note behavioral differences observed
- [ ] Record cost comparison

### Deployment
- [ ] Test in staging/dev environment
- [ ] Monitor error rate
- [ ] Monitor output quality
- [ ] Monitor costs
- [ ] Have rollback plan ready

### Post-Migration
- [ ] Monitor for 1-2 weeks
- [ ] Collect user feedback
- [ ] Optimize prompts/configuration
- [ ] Document lessons learned

## Summary

Migrating from Copilot to Claude is straightforward:

1. **Configuration**: Change provider, set API key, map models
2. **Limitations**: Remove tools (Phase 1), accept non-streaming
3. **Testing**: Validate, compare outputs, acceptance test
4. **Monitoring**: Track errors, quality, cost, latency
5. **Rollback**: Keep backups, have rollback plan

**Time estimate**: 30-60 minutes per workflow

**Risk level**: Low (easy rollback, config-only changes)

**Recommended approach**: Gradual migration, one workflow at a time, with monitoring
