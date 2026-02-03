# Token Usage & Cost Tracking Brainstorm

## Overview

Track token usage (input, output, cache) per agent and workflow, estimate costs based on model pricing, and optionally enforce budgets.

## Motivating Use Case

A developer runs a research workflow with 5 agents during development. After an hour of testing, they check their provider billing and discover $50 in charges. They have no visibility into which agents consumed the most tokens or how to optimize.

**Current behavior:**
```
conductor run research.yaml
# Output shows final result only
# No token counts, no cost estimate
# Billing surprise at month end
```

**Desired behavior:**
```
conductor run research.yaml --verbose

[1/5] researcher... ✓ (2,341 in / 1,205 out = $0.12)
[2/5] analyzer... ✓ (5,892 in / 2,103 out = $0.28)
...
═══════════════════════════════════════════════════
Workflow completed in 45.2s
Total tokens: 18,234 input / 6,891 output
Estimated cost: $0.89
═══════════════════════════════════════════════════
```

## Goals

1. **Track token usage** from AgentOutput.tokens_used (already available)
2. **Estimate costs** using built-in pricing tables for common models
3. **Display cost summary** in verbose mode and final output
4. **Per-agent attribution** to identify expensive agents
5. **Optional budgets** to fail-fast when limits exceeded

## Non-Goals (for now)

- Full observability/tracing (defer to OpenTelemetry integration)
- Latency tracking (useful but separate concern)
- Historical cost analytics (would need persistent storage)

## Design Decisions

### 1. Pricing Data Source

| Option | Pros | Cons | Decision |
|--------|------|------|----------|
| Hardcoded tables | Simple, no external deps | Stale quickly | ✅ **Default** |
| User-provided YAML | Accurate for their use | Manual maintenance | ✅ **Override** |
| Fetch from API | Always current | Network dependency | Future |

**Decision**: Ship with hardcoded defaults for common models, allow user override in workflow YAML.

### 2. Token Count Source

The `AgentOutput` dataclass already has `tokens_used: int | None`. This comes from:
- Copilot SDK: `response.usage.total_tokens`
- Claude SDK: `response.usage.input_tokens + response.usage.output_tokens`

**Enhancement needed**: Track input/output separately for accurate cost calculation.

```python
@dataclass
class AgentOutput:
    content: dict[str, Any]
    raw_response: Any
    tokens_used: int | None = None
    input_tokens: int | None = None   # NEW
    output_tokens: int | None = None  # NEW
    cache_read_tokens: int | None = None   # NEW (Claude)
    cache_write_tokens: int | None = None  # NEW (Claude)
    model: str | None = None
```

### 3. Cost Calculation

```python
def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    pricing: ModelPricing | None = None,
) -> float:
    """Calculate cost in USD."""
    pricing = pricing or get_default_pricing(model)

    cost = (
        (input_tokens / 1_000_000) * pricing.input_per_mtok +
        (output_tokens / 1_000_000) * pricing.output_per_mtok +
        (cache_read_tokens / 1_000_000) * pricing.cache_read_per_mtok +
        (cache_write_tokens / 1_000_000) * pricing.cache_write_per_mtok
    )
    return cost
```

## YAML Syntax

### Basic (defaults)
```yaml
workflow:
  name: my-workflow
  # Cost tracking is automatic when tokens are available
```

### With Budget
```yaml
workflow:
  name: my-workflow
  cost:
    budget_usd: 1.00           # Hard limit, fail if exceeded
    warn_usd: 0.50             # Warning threshold
    show_per_agent: true       # Show cost per agent in verbose
```

### With Custom Pricing
```yaml
workflow:
  name: my-workflow
  cost:
    pricing:
      gpt-4-turbo:
        input_per_mtok: 10.00
        output_per_mtok: 30.00
      claude-sonnet-4:
        input_per_mtok: 3.00
        output_per_mtok: 15.00
        cache_read_per_mtok: 0.30
        cache_write_per_mtok: 3.75
```

## Default Pricing Table (as of 2026-01)

```python
DEFAULT_PRICING = {
    # OpenAI
    "gpt-4-turbo": ModelPricing(input=10.00, output=30.00),
    "gpt-4o": ModelPricing(input=2.50, output=10.00),
    "gpt-4o-mini": ModelPricing(input=0.15, output=0.60),
    "gpt-3.5-turbo": ModelPricing(input=0.50, output=1.50),

    # Anthropic Claude 4
    "claude-opus-4": ModelPricing(input=15.00, output=75.00, cache_read=1.50, cache_write=18.75),
    "claude-sonnet-4": ModelPricing(input=3.00, output=15.00, cache_read=0.30, cache_write=3.75),
    "claude-haiku-4": ModelPricing(input=0.25, output=1.25, cache_read=0.03, cache_write=0.30),

    # Anthropic Claude 4.5
    "claude-opus-4-5": ModelPricing(input=5.00, output=25.00, cache_read=0.50, cache_write=6.25),
    "claude-sonnet-4-5": ModelPricing(input=3.00, output=15.00, cache_read=0.30, cache_write=3.75),
    "claude-haiku-4-5": ModelPricing(input=1.00, output=5.00, cache_read=0.10, cache_write=1.25),
}
```

## Output Format

### Verbose Mode (per-agent)
```
[1/5] 🤖 researcher (claude-sonnet-4)
    ├─ 🔧 web_search
    │  ✓ web_search
    └─ ✓ 45.2s | 2,341 in / 1,205 out | $0.12
    → fact_checker

[2/5] 🤖 fact_checker (claude-sonnet-4)
    └─ ✓ 12.1s | 892 in / 423 out | $0.04
    → summarizer
```

### Final Summary
```
═══════════════════════════════════════════════════════════════
Workflow completed in 2m 15s

Token Usage:
  Input:  18,234 tokens
  Output:  6,891 tokens
  Cache:   2,100 read / 500 write

Cost Breakdown:
  researcher:    $0.12 (34%)
  fact_checker:  $0.04 (11%)
  analyzer:      $0.08 (22%)
  synthesizer:   $0.06 (17%)
  summarizer:    $0.05 (14%)
  ─────────────────────────
  Total:         $0.35

Model: claude-sonnet-4-20250514
═══════════════════════════════════════════════════════════════
```

### JSON Output (when --format json)
```json
{
  "result": { ... },
  "usage": {
    "total_input_tokens": 18234,
    "total_output_tokens": 6891,
    "cache_read_tokens": 2100,
    "cache_write_tokens": 500,
    "estimated_cost_usd": 0.35,
    "per_agent": {
      "researcher": {
        "input_tokens": 2341,
        "output_tokens": 1205,
        "cost_usd": 0.12
      }
    }
  }
}
```

## Implementation Components

### 1. Pricing Data (`engine/pricing.py`)

```python
@dataclass
class ModelPricing:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0

DEFAULT_PRICING: dict[str, ModelPricing] = { ... }

def get_pricing(model: str, overrides: dict | None = None) -> ModelPricing:
    """Get pricing for a model, with optional overrides."""
    ...

def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    pricing: ModelPricing | None = None,
) -> float:
    ...
```

### 2. Usage Tracker (`engine/usage.py`)

```python
@dataclass
class AgentUsage:
    agent_name: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    elapsed_seconds: float

@dataclass
class WorkflowUsage:
    agents: list[AgentUsage]
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read: int
    total_cache_write: int
    total_cost_usd: float
    elapsed_seconds: float

class UsageTracker:
    def __init__(self, pricing_overrides: dict | None = None):
        self.agents: list[AgentUsage] = []
        self.pricing_overrides = pricing_overrides or {}

    def record(
        self,
        agent_name: str,
        output: AgentOutput,
        elapsed: float,
    ) -> AgentUsage:
        ...

    def get_summary(self) -> WorkflowUsage:
        ...

    def check_budget(self, budget_usd: float) -> bool:
        ...
```

### 3. Schema Extensions (`config/schema.py`)

```python
class CostConfig(BaseModel):
    budget_usd: float | None = None
    warn_usd: float | None = None
    show_per_agent: bool = True
    pricing: dict[str, PricingOverride] = Field(default_factory=dict)

class WorkflowDef(BaseModel):
    # ... existing fields
    cost: CostConfig = Field(default_factory=CostConfig)
```

### 4. AgentOutput Enhancement (`providers/base.py`)

```python
@dataclass
class AgentOutput:
    content: dict[str, Any]
    raw_response: Any
    tokens_used: int | None = None
    input_tokens: int | None = None    # NEW
    output_tokens: int | None = None   # NEW
    cache_read_tokens: int | None = None   # NEW
    cache_write_tokens: int | None = None  # NEW
    model: str | None = None
```

### 5. Provider Updates

Update CopilotProvider and ClaudeProvider to populate the new fields:

```python
# copilot.py
return AgentOutput(
    content=content,
    raw_response=response,
    input_tokens=response.usage.prompt_tokens,
    output_tokens=response.usage.completion_tokens,
    tokens_used=response.usage.total_tokens,
    model=response.model,
)

# claude.py
return AgentOutput(
    content=content,
    raw_response=response,
    input_tokens=response.usage.input_tokens,
    output_tokens=response.usage.output_tokens,
    cache_read_tokens=getattr(response.usage, 'cache_read_input_tokens', 0),
    cache_write_tokens=getattr(response.usage, 'cache_creation_input_tokens', 0),
    tokens_used=response.usage.input_tokens + response.usage.output_tokens,
    model=response.model,
)
```

## Files Affected

### New Files
- `src/conductor/engine/pricing.py` - Pricing tables and cost calculation
- `src/conductor/engine/usage.py` - Usage tracking
- `tests/test_engine/test_usage.py` - Unit tests

### Modified Files
- `src/conductor/providers/base.py` - Enhance AgentOutput
- `src/conductor/providers/copilot.py` - Populate token fields
- `src/conductor/providers/claude.py` - Populate token fields (when implemented)
- `src/conductor/config/schema.py` - Add CostConfig
- `src/conductor/engine/workflow.py` - Integrate usage tracking
- `src/conductor/cli/run.py` - Display cost summary

## Open Questions

1. **Parallel group cost attribution**: Aggregate to group or show individual agents?

2. **For-each cost display**: Show per-item or aggregate? (Could be 100+ items)

3. **Cache token handling**: Are cache_read tokens already included in input_tokens? (Provider-specific)

4. **Budget enforcement timing**: Check after each agent, or allow overage on final agent?

## Future Enhancements

- Cost history persistence (SQLite)
- Cost trends visualization
- Budget alerts via webhook
- Cost comparison between providers
- Integration with OpenTelemetry metrics
